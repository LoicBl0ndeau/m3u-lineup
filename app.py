#!/usr/bin/env python3
"""
Lineup — curate a filtered M3U playlist from a source M3U link.

Single-process Flask app, SQLite storage, no heavy dependencies.
Fetch a source .m3u, browse/search its channels, group multiple stream
links into a single "virtual channel" with automatic fallback, then
export a clean .m3u ready for Jellyfin's M3U Tuner.
"""

import logging
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import base64
from contextlib import closing
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from flask import (
    Flask, g, jsonify, request, Response, render_template,
    stream_with_context, abort
)

DB_PATH = os.environ.get("DB_PATH", "/data/lineup.sqlite3")
FETCH_TIMEOUT = int(os.environ.get("FETCH_TIMEOUT", "20"))
STREAM_TIMEOUT = int(os.environ.get("STREAM_TIMEOUT", "8"))
CHECK_TIMEOUT = int(os.environ.get("CHECK_TIMEOUT", "5"))
USER_AGENT = os.environ.get("STREAM_USER_AGENT", "Mozilla/5.0 (Lineup)")
LOGO_CACHE_DIR = os.environ.get("LOGO_CACHE_DIR", "/data/logos")

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("lineup")

app = Flask(__name__)


@app.after_request
def _log_request(response):
    log.info("%s %s %s", request.method, request.path, response.status_code)
    return response

ATTR_RE = re.compile(r'([a-zA-Z0-9_-]+)="([^"]*)"')
HLS_URI_ATTR_RE = re.compile(r'URI="([^"]+)"')
ABSOLUTE_URI_RE = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.\-]*://')
RESOLUTION_RE = re.compile(r'RESOLUTION=(\d+x\d+)')
MEDIA_SEGMENT_EXT_RE = re.compile(r'\.(ts|aac|mp4|m4s|m4v|fmp4|cmfv|cmfa|cmft)(\?.*)?$', re.IGNORECASE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    label TEXT DEFAULT '',
    last_fetched_at TEXT,
    last_status TEXT DEFAULT '',
    last_error TEXT DEFAULT '',
    channel_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imported_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    group_title TEXT DEFAULT '',
    tvg_logo TEXT DEFAULT '',
    tvg_id TEXT DEFAULT '',
    url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS virtual_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    group_title TEXT DEFAULT '',
    logo TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS virtual_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    virtual_channel_id INTEGER NOT NULL REFERENCES virtual_channels(id) ON DELETE CASCADE,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    match_key TEXT DEFAULT '',
    label TEXT DEFAULT '',
    url TEXT NOT NULL,
    position INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def get_setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    db.commit()


def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def migrate_schema(db):
    """Additive migrations for people upgrading from an older Lineup DB."""
    additions = [
        ("imported_channels", "source_id", "INTEGER REFERENCES sources(id) ON DELETE CASCADE"),
        ("virtual_sources", "source_id", "INTEGER REFERENCES sources(id) ON DELETE SET NULL"),
        ("virtual_sources", "match_key", "TEXT DEFAULT ''"),
    ]
    for table, col, decl in additions:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass  # column already exists
    # Channels imported by the old single-source flow have no source_id and
    # can no longer be refreshed or matched against anything — drop them.
    db.execute("DELETE FROM imported_channels WHERE source_id IS NULL")
    db.execute("DELETE FROM settings WHERE key = 'source_url'")
    db.commit()


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as db:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        db.executescript(SCHEMA)
        db.commit()
        migrate_schema(db)


# ---------------------------------------------------------------------------
# M3U parsing
# ---------------------------------------------------------------------------

def parse_extinf(line):
    """Split an #EXTINF line into (attrs dict, title), robust to commas
    that appear *inside* a quoted attribute value (e.g. a user-agent
    string containing "(KHTML, like Gecko)")."""
    rest = line[len("#EXTINF:"):]
    dur_m = re.match(r'^-?\d+(?:\.\d+)?', rest)
    rest = rest[dur_m.end():] if dur_m else rest

    attrs = {}
    last_end = 0
    for am in ATTR_RE.finditer(rest):
        attrs[am.group(1)] = am.group(2)
        last_end = am.end()

    tail = rest[last_end:]
    comma_idx = tail.find(",")
    title = tail[comma_idx + 1:].strip() if comma_idx != -1 else tail.strip()
    return attrs, title


def parse_m3u(text):
    """Yield dicts: name, group_title, tvg_logo, tvg_id, url."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            attrs, title = parse_extinf(line)
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith("#"):
                j += 1
            if j < len(lines) and lines[j].strip():
                yield {
                    "name": title or attrs.get("tvg-name", "Sans nom"),
                    "group_title": attrs.get("group-title", ""),
                    "tvg_logo": attrs.get("tvg-logo", ""),
                    "tvg_id": attrs.get("tvg-id", ""),
                    "url": lines[j].strip(),
                }
                i = j + 1
                continue
        i += 1


# ---------------------------------------------------------------------------
# Routes: pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Routes: source playlists
# ---------------------------------------------------------------------------

def channel_match_key(channel):
    """Stable-ish identifier for a channel within a source, used to
    reconcile stream URLs across refreshes even if they rotate."""
    return channel["tvg_id"] or channel["name"].strip().lower()


def refresh_source(db, source_row):
    """Fetch one source, replace its imported channels, and propagate any
    changed stream URL into the virtual_sources that were built from it."""
    try:
        resp = requests.get(source_row["url"], timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        channels = list(parse_m3u(resp.text))
        if not channels:
            raise ValueError("Aucune chaîne trouvée dans ce m3u")
    except Exception as e:
        log.warning("source %s refresh failed: %s", source_row["url"], e)
        db.execute(
            "UPDATE sources SET last_fetched_at = ?, last_status = 'error', last_error = ? WHERE id = ?",
            (now_iso(), str(e), source_row["id"]),
        )
        db.commit()
        return {"ok": False, "error": str(e)}

    lookup = {channel_match_key(ch): ch for ch in channels}

    updated_links = 0
    vsources = db.execute(
        "SELECT id, match_key, url FROM virtual_sources WHERE source_id = ?",
        (source_row["id"],),
    ).fetchall()
    for vs in vsources:
        new_ch = lookup.get(vs["match_key"]) if vs["match_key"] else None
        if new_ch and new_ch["url"] != vs["url"]:
            db.execute(
                "UPDATE virtual_sources SET url = ?, label = ? WHERE id = ?",
                (new_ch["url"], new_ch["name"], vs["id"]),
            )
            updated_links += 1

    db.execute("DELETE FROM imported_channels WHERE source_id = ?", (source_row["id"],))
    db.executemany(
        "INSERT INTO imported_channels (source_id, name, group_title, tvg_logo, tvg_id, url) "
        "VALUES (:source_id, :name, :group_title, :tvg_logo, :tvg_id, :url)",
        [{**ch, "source_id": source_row["id"]} for ch in channels],
    )
    db.execute(
        "UPDATE sources SET last_fetched_at = ?, last_status = 'ok', last_error = '', channel_count = ? "
        "WHERE id = ?",
        (now_iso(), len(channels), source_row["id"]),
    )
    db.commit()
    log.info("source %s refreshed: %d channels, %d links updated", source_row["url"], len(channels), updated_links)
    return {"ok": True, "count": len(channels), "updated_links": updated_links}


@app.route("/api/sources", methods=["GET"])
def api_sources_list():
    rows = get_db().execute("SELECT * FROM sources ORDER BY created_at").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/sources", methods=["POST"])
def api_sources_create():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    label = (data.get("label") or "").strip()
    if not url:
        return jsonify(error="URL manquante"), 400

    db = get_db()
    if db.execute("SELECT 1 FROM sources WHERE url = ?", (url,)).fetchone():
        return jsonify(error="Cette source est déjà ajoutée"), 409

    cur = db.execute(
        "INSERT INTO sources (url, label, created_at) VALUES (?, ?, ?)",
        (url, label, now_iso()),
    )
    db.commit()
    source_row = db.execute("SELECT * FROM sources WHERE id = ?", (cur.lastrowid,)).fetchone()

    result = refresh_source(db, source_row)
    if not result["ok"]:
        db.execute("DELETE FROM sources WHERE id = ?", (source_row["id"],))
        db.commit()
        return jsonify(error=f"Échec du téléchargement : {result['error']}"), 502

    source_row = db.execute("SELECT * FROM sources WHERE id = ?", (source_row["id"],)).fetchone()
    return jsonify(dict(source_row)), 201


@app.route("/api/sources/<int:source_id>", methods=["DELETE"])
def api_sources_delete(source_id):
    db = get_db()
    db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/sources/<int:source_id>/refresh", methods=["POST"])
def api_sources_refresh(source_id):
    db = get_db()
    row = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        abort(404)
    result = refresh_source(db, row)
    row = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return jsonify(source=dict(row), result=result)


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    db = get_db()
    interval = int(get_setting(db, "refresh_interval_seconds", "0") or 0)
    return jsonify(refresh_interval_seconds=interval)


@app.route("/api/settings", methods=["POST"])
def api_settings_update():
    data = request.get_json(force=True, silent=True) or {}
    try:
        interval = max(0, int(data.get("refresh_interval_seconds", 0) or 0))
    except (TypeError, ValueError):
        return jsonify(error="Valeur invalide"), 400
    if 0 < interval < 60:
        return jsonify(error="L'intervalle minimum est de 60 secondes."), 400
    db = get_db()
    set_setting(db, "refresh_interval_seconds", interval)
    return jsonify(refresh_interval_seconds=interval)


def split_groups(group_title):
    """A channel's group-title can list several groups separated by ';'
    (some IPTV panels do this, e.g. "Entertainment;Interactive")."""
    return [g.strip() for g in (group_title or "").split(";") if g.strip()]


@app.route("/api/channels")
def api_channels():
    q = (request.args.get("q") or "").strip()
    group = (request.args.get("group") or "").strip()
    limit = min(int(request.args.get("limit", 300)), 1000)

    sql = (
        "SELECT id, source_id, name, group_title, tvg_logo, tvg_id, url "
        "FROM imported_channels WHERE 1=1"
    )
    params = []
    if q:
        sql += " AND name LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY name COLLATE NOCASE"

    rows = get_db().execute(sql, params).fetchall()
    if group:
        rows = [r for r in rows if group in split_groups(r["group_title"])]
    rows = rows[:limit]

    return jsonify([dict(r) for r in rows])


@app.route("/api/groups")
def api_groups():
    rows = get_db().execute(
        "SELECT DISTINCT group_title FROM imported_channels WHERE group_title != ''"
    ).fetchall()
    groups = set()
    for r in rows:
        groups.update(split_groups(r["group_title"]))
    return jsonify(sorted(groups, key=str.lower))


# ---------------------------------------------------------------------------
# Routes: virtual channels
# ---------------------------------------------------------------------------

def serialize_virtual_channel(db, row):
    sources = db.execute(
        "SELECT id, label, url, position FROM virtual_sources "
        "WHERE virtual_channel_id = ? ORDER BY position",
        (row["id"],),
    ).fetchall()
    d = dict(row)
    d["active"] = bool(d["active"])
    d["sources"] = [dict(s) for s in sources]
    return d


@app.route("/api/virtual", methods=["GET"])
def api_virtual_list():
    db = get_db()
    rows = db.execute("SELECT * FROM virtual_channels ORDER BY id").fetchall()
    return jsonify([serialize_virtual_channel(db, r) for r in rows])


@app.route("/api/virtual", methods=["POST"])
def api_virtual_create():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(error="Nom manquant"), 400
    source = data.get("source")  # optional {label, url, tvg_logo, tvg_id, source_id}
    logo = data.get("logo") or (source or {}).get("tvg_logo", "") or ""

    db = get_db()
    cur = db.execute(
        "INSERT INTO virtual_channels (name, group_title, logo, active, created_at) "
        "VALUES (?, ?, ?, 1, ?)",
        (name, data.get("group_title", ""), logo, time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    vc_id = cur.lastrowid
    if source and source.get("url"):
        label = source.get("label", "")
        match_key = source.get("tvg_id") or label.strip().lower()
        db.execute(
            "INSERT INTO virtual_sources (virtual_channel_id, source_id, match_key, label, url, position) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (vc_id, source.get("source_id"), match_key, label, source["url"]),
        )
    db.commit()
    row = db.execute("SELECT * FROM virtual_channels WHERE id = ?", (vc_id,)).fetchone()
    return jsonify(serialize_virtual_channel(db, row)), 201


@app.route("/api/virtual/<int:vc_id>", methods=["PATCH"])
def api_virtual_update(vc_id):
    data = request.get_json(force=True, silent=True) or {}
    db = get_db()
    row = db.execute("SELECT * FROM virtual_channels WHERE id = ?", (vc_id,)).fetchone()
    if not row:
        abort(404)
    name = data.get("name", row["name"])
    active = int(bool(data.get("active", row["active"])))
    db.execute("UPDATE virtual_channels SET name = ?, active = ? WHERE id = ?", (name, active, vc_id))
    db.commit()
    row = db.execute("SELECT * FROM virtual_channels WHERE id = ?", (vc_id,)).fetchone()
    return jsonify(serialize_virtual_channel(db, row))


@app.route("/api/virtual/<int:vc_id>", methods=["DELETE"])
def api_virtual_delete(vc_id):
    db = get_db()
    db.execute("DELETE FROM virtual_channels WHERE id = ?", (vc_id,))
    db.commit()
    return jsonify(ok=True)


@app.route("/api/virtual/<int:vc_id>/sources", methods=["POST"])
def api_virtual_add_source(vc_id):
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify(error="URL manquante"), 400
    label = data.get("label", "")
    match_key = data.get("tvg_id") or label.strip().lower()
    db = get_db()
    if not db.execute("SELECT 1 FROM virtual_channels WHERE id = ?", (vc_id,)).fetchone():
        abort(404)
    max_pos = db.execute(
        "SELECT COALESCE(MAX(position), -1) p FROM virtual_sources WHERE virtual_channel_id = ?",
        (vc_id,),
    ).fetchone()["p"]
    db.execute(
        "INSERT INTO virtual_sources (virtual_channel_id, source_id, match_key, label, url, position) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (vc_id, data.get("source_id"), match_key, label, url, max_pos + 1),
    )
    db.commit()
    row = db.execute("SELECT * FROM virtual_channels WHERE id = ?", (vc_id,)).fetchone()
    return jsonify(serialize_virtual_channel(db, row)), 201


@app.route("/api/virtual/<int:vc_id>/sources/<int:src_id>", methods=["DELETE"])
def api_virtual_remove_source(vc_id, src_id):
    db = get_db()
    db.execute("DELETE FROM virtual_sources WHERE id = ? AND virtual_channel_id = ?", (src_id, vc_id))
    # renumber positions to stay contiguous
    rows = db.execute(
        "SELECT id FROM virtual_sources WHERE virtual_channel_id = ? ORDER BY position", (vc_id,)
    ).fetchall()
    for idx, r in enumerate(rows):
        db.execute("UPDATE virtual_sources SET position = ? WHERE id = ?", (idx, r["id"]))
    db.commit()
    row = db.execute("SELECT * FROM virtual_channels WHERE id = ?", (vc_id,)).fetchone()
    return jsonify(serialize_virtual_channel(db, row))


@app.route("/api/virtual/<int:vc_id>/sources/reorder", methods=["POST"])
def api_virtual_reorder_sources(vc_id):
    data = request.get_json(force=True, silent=True) or {}
    order = data.get("order") or []  # list of source ids, in desired order
    db = get_db()
    for idx, src_id in enumerate(order):
        db.execute(
            "UPDATE virtual_sources SET position = ? WHERE id = ? AND virtual_channel_id = ?",
            (idx, src_id, vc_id),
        )
    db.commit()
    row = db.execute("SELECT * FROM virtual_channels WHERE id = ?", (vc_id,)).fetchone()
    return jsonify(serialize_virtual_channel(db, row))


@app.route("/api/virtual/check")
def api_virtual_check():
    db = get_db()
    rows = db.execute(
        "SELECT vs.id AS src_id, vs.virtual_channel_id AS vc_id, vs.url "
        "FROM virtual_sources vs"
    ).fetchall()
    tasks = [(r["vc_id"], r["src_id"], r["url"]) for r in rows]

    def check_one(task):
        vc_id, src_id, url = task
        try:
            resp = requests.get(url, stream=True, timeout=CHECK_TIMEOUT, headers={"User-Agent": USER_AGENT})
            if resp.status_code >= 400:
                resp.close()
                return vc_id, src_id, "down", None
            content_type = resp.headers.get("Content-Type", "")
            first_chunk = next(resp.iter_content(chunk_size=8192), b"")
            resp.close()
            looks_like_hls = (
                first_chunk.lstrip().startswith(b"#EXTM3U")
                or "mpegurl" in content_type.lower()
                or url.lower().split("?")[0].endswith(".m3u8")
            )
            resolution = None
            if looks_like_hls:
                matches = RESOLUTION_RE.findall(first_chunk.decode("utf-8", errors="replace"))
                if matches:
                    resolution = max(matches, key=lambda r: int(r.split("x")[0]))
            return vc_id, src_id, "ok", resolution
        except requests.RequestException:
            return vc_id, src_id, "down", None

    results = {}
    with ThreadPoolExecutor(max_workers=20) as pool:
        for vc_id, src_id, status, resolution in pool.map(check_one, tasks):
            results.setdefault(vc_id, {})[src_id] = {"status": status, "resolution": resolution}
    return jsonify(results)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

@app.route("/api/export")
def api_export():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM virtual_channels WHERE active = 1 ORDER BY name COLLATE NOCASE"
    ).fetchall()
    base = request.url_root.rstrip("/")

    lines = ["#EXTM3U"]
    for row in rows:
        has_source = db.execute(
            "SELECT 1 FROM virtual_sources WHERE virtual_channel_id = ? LIMIT 1", (row["id"],)
        ).fetchone()
        if not has_source:
            continue
        attrs = f'tvg-id="{row["id"]}" group-title="{row["group_title"] or "Lineup"}"'
        if row["logo"]:
            attrs += f' tvg-logo="{base}/channels/logos/{row["id"]}/cache"'
        lines.append(f'#EXTINF:-1 {attrs},{row["name"]}')
        lines.append(f"{base}/stream/{row['id']}")

    body = "\n".join(lines) + "\n"
    return Response(
        body,
        mimetype="audio/x-mpegurl",
        headers={"Content-Disposition": 'attachment; filename="lineup.m3u"'},
    )


# ---------------------------------------------------------------------------
# Channel logo cache
# ---------------------------------------------------------------------------

@app.route("/channels/logos/<int:vc_id>/cache")
def channel_logo(vc_id):
    db = get_db()
    row = db.execute("SELECT logo FROM virtual_channels WHERE id = ?", (vc_id,)).fetchone()
    if not row or not row["logo"]:
        abort(404)

    os.makedirs(LOGO_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(LOGO_CACHE_DIR, f"{vc_id}.bin")
    ctype_path = cache_path + ".ctype"

    if not os.path.exists(cache_path):
        try:
            resp = requests.get(row["logo"], timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
        except requests.RequestException:
            abort(502)
        with open(cache_path, "wb") as f:
            f.write(resp.content)
        with open(ctype_path, "w") as f:
            f.write(resp.headers.get("Content-Type", "image/png"))

    content_type = "image/png"
    if os.path.exists(ctype_path):
        content_type = open(ctype_path).read().strip() or content_type

    with open(cache_path, "rb") as f:
        data = f.read()
    return Response(data, mimetype=content_type)


# ---------------------------------------------------------------------------
# Streaming proxy with automatic fallback
# ---------------------------------------------------------------------------

def resolve_segment_uri(uri):
    """Extract the real segment URL from beacon/analytics redirect URLs.
    Some IPTV servers wrap segment URLs in a tracking beacon of the form
    .../beacon?redirect_url=https%3A%2F%2F...%2Fsegment.ts — FFmpeg's HLS
    parser rejects these because the beacon path has no recognized extension."""
    params = parse_qs(urlparse(uri).query)
    if "redirect_url" in params:
        return unquote(params["redirect_url"][0])
    return uri


def rewrite_hls_manifest(text, base_url, proxy_base=None):
    """Rewrite URIs in an HLS manifest to absolute URLs.

    For master playlists, if proxy_base is given, variant playlist URLs are
    routed through /hls-proxy/ so Lineup can intercept the media playlist and
    resolve any beacon segment URLs before FFmpeg sees them.
    Media playlist segment URLs are resolved directly (beacon → real .ts URL).
    """
    is_master = "#EXT-X-STREAM-INF" in text

    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
            continue
        if stripped.startswith("#"):
            if stripped.startswith("#EXT-X-MEDIA") and "TYPE=SUBTITLES" in stripped:
                continue
            if stripped.startswith("#EXT-X-STREAM-INF"):
                line = re.sub(r',?SUBTITLES="[^"]*"', "", line)
            def _repl(m):
                uri = m.group(1)
                abs_uri = uri if ABSOLUTE_URI_RE.match(uri) else urljoin(base_url, uri)
                return f'URI="{abs_uri}"'
            out_lines.append(HLS_URI_ATTR_RE.sub(_repl, line))
        else:
            abs_uri = stripped if ABSOLUTE_URI_RE.match(stripped) else urljoin(base_url, stripped)
            if is_master and proxy_base:
                encoded = base64.urlsafe_b64encode(abs_uri.encode()).decode().rstrip("=")
                abs_uri = f"{proxy_base}/hls-proxy/{encoded}"
            else:
                abs_uri = resolve_segment_uri(abs_uri)
                # Segment URL still looks like a beacon (no recognized media extension):
                # proxy it so requests follows the HTTP redirect to the real .ts file.
                if proxy_base and not MEDIA_SEGMENT_EXT_RE.search(urlparse(abs_uri).path):
                    encoded = base64.urlsafe_b64encode(abs_uri.encode()).decode().rstrip("=")
                    abs_uri = f"{proxy_base}/hls-proxy/{encoded}.ts"
            out_lines.append(abs_uri)
    return "\n".join(out_lines) + "\n"


@app.route("/hls-proxy/<path:encoded>")
def hls_proxy(encoded):
    if encoded.endswith(".ts"):
        encoded = encoded[:-3]
    padding = (4 - len(encoded) % 4) % 4
    try:
        url = base64.urlsafe_b64decode(encoded + "=" * padding).decode()
    except Exception:
        abort(400)

    try:
        resp = requests.get(url, stream=True, timeout=STREAM_TIMEOUT, headers={"User-Agent": USER_AGENT})
        if resp.status_code >= 400:
            abort(resp.status_code)

        content_type = resp.headers.get("Content-Type", "")
        first_chunk = next(resp.iter_content(chunk_size=8192), b"")
        looks_like_hls = (
            first_chunk.lstrip().startswith(b"#EXTM3U")
            or "mpegurl" in content_type.lower()
        )

        if looks_like_hls:
            body = first_chunk + b"".join(resp.iter_content(chunk_size=8192))
            resp.close()
            manifest_text = body.decode("utf-8", errors="replace")
            proxy_base = request.url_root.rstrip("/")
            rewritten = rewrite_hls_manifest(manifest_text, url, proxy_base=proxy_base)
            return Response(rewritten, mimetype="application/vnd.apple.mpegurl")

        def generate(r=resp, first=first_chunk):
            try:
                if first:
                    yield first
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk
            finally:
                r.close()

        return Response(stream_with_context(generate()), content_type=content_type or "video/mp2t")
    except requests.RequestException as e:
        log.error("hls-proxy %s: %s", url, e)
        abort(502)


@app.route("/stream/<int:vc_id>")
def stream(vc_id):
    db = get_db()
    sources = db.execute(
        "SELECT url, label FROM virtual_sources WHERE virtual_channel_id = ? ORDER BY position",
        (vc_id,),
    ).fetchall()
    if not sources:
        abort(404)

    last_error = None
    for src in sources:
        try:
            upstream = requests.get(
                src["url"],
                stream=True,
                timeout=STREAM_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
            if upstream.status_code >= 400:
                upstream.close()
                last_error = f"HTTP {upstream.status_code} sur {src['url']}"
                continue

            content_type = upstream.headers.get("Content-Type", "")
            chunk_iter = upstream.iter_content(chunk_size=8192)
            first_chunk = next(chunk_iter, b"")

            # Sniff the actual content rather than trusting Content-Type,
            # since a lot of IPTV panels mislabel it.
            looks_like_hls = (
                first_chunk.lstrip().startswith(b"#EXTM3U")
                or "mpegurl" in content_type.lower()
                or src["url"].lower().split("?")[0].endswith(".m3u8")
            )

            if looks_like_hls:
                body = first_chunk + b"".join(chunk_iter)
                manifest_text = body.decode("utf-8", errors="replace")
                base_url = upstream.url  # final URL after redirects
                upstream.close()
                is_master = "#EXT-X-STREAM-INF" in manifest_text
                if is_master:
                    resolutions = re.findall(r'RESOLUTION=(\d+x\d+)', manifest_text)
                    codecs_list = re.findall(r'CODECS="([^"]*)"', manifest_text)
                    bandwidths = re.findall(r'BANDWIDTH=(\d+)', manifest_text)
                    log.info(
                        "stream %d: MASTER PLAYLIST — %d variants, resolutions=%s, codecs=%s, bandwidths=%s",
                        vc_id, len(bandwidths), resolutions, codecs_list, bandwidths,
                    )
                else:
                    log.info("stream %d: media playlist (single quality)", vc_id)
                proxy_base = request.url_root.rstrip("/")
                rewritten = rewrite_hls_manifest(manifest_text, base_url, proxy_base=proxy_base)
                return Response(rewritten, mimetype="application/vnd.apple.mpegurl")

            def generate(resp=upstream, first=first_chunk, it=chunk_iter):
                try:
                    if first:
                        yield first
                    for chunk in it:
                        if chunk:
                            yield chunk
                finally:
                    resp.close()

            return Response(
                stream_with_context(generate()),
                content_type=content_type or "video/mp2t",
            )
        except requests.RequestException as e:
            last_error = str(e)
            log.warning("stream %d: source %s failed: %s", vc_id, src["url"], e)
            continue

    log.error("stream %d: all sources failed, last error: %s", vc_id, last_error)
    return Response(f"Toutes les sources ont échoué : {last_error}", status=503)


def scheduler_loop():
    """Every 30s, check whether any source is due for a refresh according
    to the configured global interval, and refresh it if so."""
    while True:
        try:
            with closing(sqlite3.connect(DB_PATH)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys = ON")
                interval = int(get_setting(db, "refresh_interval_seconds", "0") or 0)
                if interval > 0:
                    now = time.time()
                    for source_row in db.execute("SELECT * FROM sources").fetchall():
                        last = source_row["last_fetched_at"]
                        due = True
                        if last:
                            try:
                                due = (now - time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%S"))) >= interval
                            except ValueError:
                                due = True
                        if due:
                            refresh_source(db, source_row)
        except Exception as e:
            log.error("scheduler error: %s", e)
        time.sleep(30)


if __name__ == "__main__":
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()

    from waitress import serve

    port = int(os.environ.get("LISTEN_PORT", "9999"))
    serve(app, host="0.0.0.0", port=port, threads=8)
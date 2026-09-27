# Lineup

**Compose your IPTV channel list, one working channel at a time.**

Lineup is a lightweight self-hosted app that merges several `.m3u` playlists,
lets you pick exactly which channels to keep, group multiple stream links for
the same channel with automatic failover, then exports the result as a clean
`.m3u` playlist — ready for Jellyfin, VLC, or any other M3U-compatible player.

<img width="1920" height="1080" alt="UI" src="https://github.com/user-attachments/assets/5a5e12ec-5023-4a27-b824-76918d177ace" />

![Docker Image](https://img.shields.io/badge/ghcr.io-m3u--lineup-blue)

---

## ✨ Features

- **Multi-source**: add as many `.m3u` links as you want; their channels are
  merged and searchable from a single interface.
- **Search & filters**: by name or by group.
- **Automatic failover**: group multiple links for the same channel (e.g. an
  HD source + a backup) — if the first one fails, the next is tried
  automatically.
- **Drag-and-drop reordering**: sort your lineup by hand; the order defines
  the channel numbers (`tvg-chno`) in the export.
- **Automatic stream health checks**: Lineup tests each source on a
  configurable schedule and encodes the result into the exported channel name
  (`🟢 Available`, `🟡 Primary offline, fallback available`, `🔴 Unavailable`).
  In Jellyfin, set the same refresh interval on your M3U Tuner to see channel
  status directly in the UI before you even click.
- **Automatic refresh**: re-downloads your sources on a configurable schedule.
- **Advanced HLS support**: `.m3u8` manifests are detected and rewritten so
  the player fetches segments directly from the origin; tracking/beacon URLs
  are filtered out automatically. fmp4/CMAF streams with separate audio tracks
  are fully supported.
- **Logo cache**: channel artwork is downloaded and cached locally once.
- **Lightweight**: a single Python/Flask + SQLite container, designed to run
  on a small VPS (20–40 MB RAM).

## 🚀 Quick start

No need to clone the repo or build anything — the image is published
automatically to GitHub Container Registry.

Create a `docker-compose.yml` file:

```yaml
services:
  lineup:
    image: ghcr.io/loicbl0ndeau/m3u-lineup:latest
    container_name: lineup
    restart: unless-stopped
    environment:
      - DB_PATH=/data/lineup.sqlite3
      - LISTEN_PORT=9999
      - FETCH_TIMEOUT=20
      - STREAM_TIMEOUT=8
      - TZ=Europe/Paris
    volumes:
      - lineup-data:/data
    ports:
      - "127.0.0.1:9999:9999"

    # --- hardening ---
    read_only: true
    tmpfs:
      - /tmp
    cap_drop:
      - ALL
    cap_add:
      - CHOWN      # entrypoint.sh needs to chown /data at startup
      - SETUID     # required by su-exec to drop to the non-root user
      - SETGID     # same, for the group
    security_opt:
      - no-new-privileges:true

volumes:
  lineup-data:
```

Then:

```bash
docker compose up -d
```

The UI is available at `http://<your-server>:9999`.

## 🖥️ Usage

1. In **M3U Sources**, add one or more `.m3u` links. Channels appear merged
   in the search list.
2. Search for a channel, click **+** → create a new channel in your lineup,
   or add the link as a fallback to an existing one.
3. Reorder the links for a channel (the first is tried first), toggle,
   rename or delete from **Your lineup**.
4. Drag channel cards to reorder them — the order defines the channel numbers
   in the exported file.
5. Configure the **Channel check** interval in the Sources panel (default:
   1 hour). Lineup will automatically test each source in the background; the
   result appears as a coloured dot in the UI and as an emoji in the exported
   `.m3u`.
6. Click **Export .m3u** to download the final playlist, or give the URL
   `http://<your-server>:9999/api/export` directly to your player (in
   Jellyfin: **Dashboard → Live TV → Tuner Devices → M3U Tuner**). To see
   status emojis update in Jellyfin, set the same refresh interval on your
   M3U Tuner.

## ⚙️ Configuration

All environment variables are optional:

| Variable            | Default                   | Description                                      |
|---------------------|---------------------------|--------------------------------------------------|
| `DB_PATH`           | `/data/lineup.sqlite3`    | SQLite database path                             |
| `LOGO_CACHE_DIR`    | `/data/logos`             | Channel logo cache directory                     |
| `LISTEN_PORT`       | `9999`                    | HTTP port inside the container                   |
| `FETCH_TIMEOUT`     | `20`                      | Timeout (s) for downloading a source m3u         |
| `STREAM_TIMEOUT`    | `8`                       | Timeout (s) for probing / proxying a stream      |
| `STREAM_USER_AGENT` | `Mozilla/5.0 (Lineup)`    | User-Agent sent to IPTV servers                  |

## 🧠 How it works

- Each `.m3u` source is parsed and stored in the database; channels are
  identified by their `tvg-id` (or lowercased name if absent).
- A lineup "channel" is an ordered list of source links. `/stream/<id>` tries
  each link in order and serves the first that responds — for an HLS manifest,
  relative paths are rewritten to absolute URLs and segments are fetched
  directly from the origin server.
- When a source is refreshed (manually or automatically), Lineup finds the
  lineup channels linked to that source and updates their URL if it changed,
  then regenerates the available channel list. `/api/export` always reads live
  from the database — the exported file reflects every change immediately.

## ⚠️ Known limitations

- Failover is checked when the stream is opened, not during playback — if a
  link goes down mid-stream, the player must restart to trigger a new attempt.
- A channel added from a source that is later deleted is no longer linked to
  any source and will not be reconciled automatically.
- Single process, no heavy queue or cache: designed for personal use with a
  few concurrent streams, not large-scale distribution.
- No built-in authentication — if you expose the port beyond `127.0.0.1`,
  add authentication in front via your reverse proxy.

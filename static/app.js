const state = {
  virtualChannels: [],
  checkResults: {},
  pendingAdd: null, // { name, url }
};

let dragSrcId = null;

const el = (id) => document.getElementById(id);

function debounce(fn, delay) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), delay);
  };
}

async function api(path, options) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Erreur ${res.status}`);
  return data;
}

// ---------------------------------------------------------------------------
// Sources (multiple m3u files, merged)
// ---------------------------------------------------------------------------

function formatDateTime(isoLike) {
  if (!isoLike) return "jamais";
  // stored as "YYYY-MM-DDTHH:MM:SS" in the server's local time
  const d = new Date(isoLike);
  if (isNaN(d.getTime())) return isoLike;
  return d.toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function loadSources() {
  const sources = await api("/api/sources");
  const list = el("sources-list");
  list.innerHTML = "";

  if (!sources.length) {
    list.innerHTML = '<p class="empty-sources">Aucune source ajoutée pour l\'instant.</p>';
    return;
  }

  for (const src of sources) {
    const li = document.createElement("li");
    li.className = "source-item " + (src.last_status || "");
    const lastFetch = `dernier fetch : ${formatDateTime(src.last_fetched_at)}`;
    const detail = src.last_status === "error"
      ? `Erreur : ${src.last_error} — ${lastFetch}`
      : `${src.channel_count} chaîne(s) — ${lastFetch} — ${src.url}`;
    li.innerHTML = `
      <span class="status-dot" title="${escapeAttr(src.last_status || "en attente")}"></span>
      <div class="meta">
        <div class="name">${escapeHtml(src.label || src.url)}</div>
        <div class="detail" title="${escapeAttr(src.url)}">${escapeHtml(detail)}</div>
      </div>
      <div class="actions">
        <button class="icon small refresh-source" title="Rafraîchir maintenant">↻</button>
        <button class="icon small remove-source-file" title="Supprimer cette source">✕</button>
      </div>
    `;
    li.querySelector(".refresh-source").addEventListener("click", async (e) => {
      e.target.disabled = true;
      try {
        await api(`/api/sources/${src.id}/refresh`, { method: "POST" });
        await Promise.all([loadSources(), loadGroups(), loadChannels(), loadVirtualChannels()]);
      } catch (err) {
        alert(err.message);
      }
    });
    li.querySelector(".remove-source-file").addEventListener("click", async () => {
      if (!confirm(`Supprimer la source « ${src.label || src.url} » ?`)) return;
      await api(`/api/sources/${src.id}`, { method: "DELETE" });
      await Promise.all([loadSources(), loadGroups(), loadChannels()]);
    });
    list.appendChild(li);
  }
}

el("source-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = el("source-url").value.trim();
  const label = el("source-label").value.trim();
  if (!url) return;
  try {
    await api("/api/sources", { method: "POST", body: JSON.stringify({ url, label }) });
    el("source-url").value = "";
    el("source-label").value = "";
    await Promise.all([loadSources(), loadGroups(), loadChannels()]);
  } catch (err) {
    alert(err.message);
  }
});

function secondsToValueUnit(seconds) {
  if (!seconds || seconds <= 0) return { value: "", unit: "60" };
  if (seconds % 86400 === 0) return { value: seconds / 86400, unit: "86400" };
  if (seconds % 3600 === 0) return { value: seconds / 3600, unit: "3600" };
  return { value: Math.max(1, Math.round(seconds / 60)), unit: "60" };
}

async function loadRefreshSetting() {
  const data = await api("/api/settings");
  const { value, unit } = secondsToValueUnit(data.refresh_interval_seconds || 0);
  el("refresh-value").value = value;
  el("refresh-unit").value = unit;
  const { value: chkVal, unit: chkUnit } = secondsToValueUnit(data.check_interval_seconds || 3600);
  el("check-interval-value").value = chkVal;
  el("check-interval-unit").value = chkUnit;
}

el("save-refresh-btn").addEventListener("click", async () => {
  const value = Number(el("refresh-value").value) || 0;
  const unit = Number(el("refresh-unit").value);
  const seconds = value > 0 ? Math.round(value * unit) : 0;
  const chkValue = Number(el("check-interval-value").value) || 0;
  const chkUnit = Number(el("check-interval-unit").value);
  const checkSeconds = chkValue > 0 ? Math.round(chkValue * chkUnit) : 0;
  const statusEl = el("refresh-status");
  try {
    await api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ refresh_interval_seconds: seconds, check_interval_seconds: checkSeconds }),
    });
    statusEl.textContent = "Enregistré ✓";
    statusEl.className = "refresh-status ok";
  } catch (err) {
    statusEl.textContent = err.message;
    statusEl.className = "refresh-status error";
  }
});

// Light polling so source status (last fetch, errors) stays current even
// when the automatic background refresh runs without any user action.
setInterval(loadSources, 60000);

// ---------------------------------------------------------------------------
// Source channel browsing
// ---------------------------------------------------------------------------

async function loadGroups() {
  const groups = await api("/api/groups");
  const select = el("group-filter");
  const current = select.value;
  select.innerHTML = '<option value="">Tous les groupes</option>' +
    groups.map((g) => `<option value="${escapeAttr(g)}">${escapeHtml(g)}</option>`).join("");
  select.value = current;
}

async function loadChannels() {
  const q = el("search-input").value.trim();
  const group = el("group-filter").value;
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (group) params.set("group", group);
  const channels = await api(`/api/channels?${params.toString()}`);

  el("source-count").textContent = channels.length;
  const list = el("channel-list");
  list.innerHTML = "";
  for (const ch of channels) {
    const li = document.createElement("li");
    li.className = "channel-row";
    li.innerHTML = `
      <div class="meta">
        <div class="name">${escapeHtml(ch.name)}</div>
        ${ch.group_title ? `<div class="group">${escapeHtml(ch.group_title.split(";").map((g) => g.trim()).filter(Boolean).join(" · "))}</div>` : ""}
      </div>
      <button class="icon small add-btn" title="Ajouter à la lineup">+</button>
    `;
    li.querySelector(".add-btn").addEventListener("click", () => openAddPopover(ch));
    list.appendChild(li);
  }
}

el("search-input").addEventListener("input", debounce(loadChannels, 250));
el("group-filter").addEventListener("change", loadChannels);

// ---------------------------------------------------------------------------
// Add popover
// ---------------------------------------------------------------------------

function openAddPopover(channel) {
  state.pendingAdd = {
    name: channel.name,
    url: channel.url,
    tvg_logo: channel.tvg_logo || "",
    tvg_id: channel.tvg_id || "",
    source_id: channel.source_id || null,
  };
  el("add-popover-name").textContent = `« ${channel.name} »`;
  el("new-channel-name").value = channel.name;

  const select = el("existing-channel-select");
  select.innerHTML = '<option value="">— choisir —</option>' +
    state.virtualChannels
      .map((vc) => `<option value="${vc.id}">${escapeHtml(vc.name)} (${vc.sources.length} lien${vc.sources.length > 1 ? "s" : ""})</option>`)
      .join("");

  el("add-popover").hidden = false;
  el("new-channel-name").focus();
}

function closeAddPopover() {
  el("add-popover").hidden = true;
  state.pendingAdd = null;
}

el("cancel-add-btn").addEventListener("click", closeAddPopover);
el("add-popover").addEventListener("click", (e) => {
  if (e.target.id === "add-popover") closeAddPopover();
});

el("create-channel-btn").addEventListener("click", async () => {
  const name = el("new-channel-name").value.trim();
  if (!name || !state.pendingAdd) return;
  try {
    await api("/api/virtual", {
      method: "POST",
      body: JSON.stringify({
        name,
        source: {
          label: state.pendingAdd.name,
          url: state.pendingAdd.url,
          tvg_logo: state.pendingAdd.tvg_logo,
          tvg_id: state.pendingAdd.tvg_id,
          source_id: state.pendingAdd.source_id,
        },
      }),
    });
    closeAddPopover();
    await loadVirtualChannels();
  } catch (err) {
    alert(err.message);
  }
});

el("add-to-existing-btn").addEventListener("click", async () => {
  const vcId = el("existing-channel-select").value;
  if (!vcId || !state.pendingAdd) return;
  try {
    await api(`/api/virtual/${vcId}/sources`, {
      method: "POST",
      body: JSON.stringify({
        label: state.pendingAdd.name,
        url: state.pendingAdd.url,
        tvg_id: state.pendingAdd.tvg_id,
        source_id: state.pendingAdd.source_id,
      }),
    });
    closeAddPopover();
    await loadVirtualChannels();
  } catch (err) {
    alert(err.message);
  }
});

// ---------------------------------------------------------------------------
// Virtual channels (the lineup)
// ---------------------------------------------------------------------------

async function loadVirtualChannels() {
  state.virtualChannels = await api("/api/virtual");
  // Sync checkResults from persisted DB status, keeping live resolution if available
  const fresh = {};
  for (const vc of state.virtualChannels) {
    if (vc.check_status) {
      const live = state.checkResults[vc.id] || {};
      fresh[vc.id] = {};
      for (const src of vc.sources) {
        fresh[vc.id][src.id] = {
          status: src.check_status || "",
          resolution: (live[src.id] || {}).resolution || null,
        };
      }
    }
  }
  state.checkResults = { ...state.checkResults, ...fresh };
  renderVirtualChannels();
}

async function checkAllChannels() {
  const btn = el("check-btn");
  btn.disabled = true;
  btn.textContent = "Vérification…";
  try {
    state.checkResults = await api("/api/virtual/check");
    renderVirtualChannels();
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Vérifier";
  }
}

el("check-btn").addEventListener("click", checkAllChannels);

function renderVirtualChannels() {
  const list = el("virtual-list");
  const empty = el("virtual-empty");
  list.innerHTML = "";

  empty.style.display = state.virtualChannels.length ? "none" : "block";

  for (const vc of state.virtualChannels) {
    const card = document.createElement("li");
    card.className = "virtual-card" + (vc.active ? "" : " inactive");

    const srcResults = state.checkResults[vc.id] || null;
    const checkStatus = !srcResults ? ""
      : Object.values(srcResults).every(r => r.status === "ok") ? "ok"
      : Object.values(srcResults).some(r => r.status === "ok") ? "partial"
      : "down";
    const dotTitle = { ok: "Toutes les sources en ligne", partial: "Certaines sources hors ligne", down: "Toutes les sources hors ligne" }[checkStatus] || "Non vérifié";

    const sourcesHtml = vc.sources.map((src, idx) => {
      const result = srcResults ? (srcResults[src.id] || null) : null;
      const s = result ? result.status : "";
      const res = result ? result.resolution : null;
      const sTitle = s === "ok" ? "En ligne" : s === "down" ? "Hors ligne" : "Non vérifié";
      return `
      <li class="source-row" data-source-id="${src.id}">
        <span class="source-rank">${idx + 1}</span>
        <span class="src-status ${s}" title="${sTitle}"></span>
        <span class="url" title="${escapeAttr(src.url)}">${escapeHtml(src.label || src.url)}</span>
        ${res ? `<span class="src-res">${escapeHtml(res)}</span>` : ""}
        <span class="source-actions">
          <button class="icon small move-up" title="Monter" ${idx === 0 ? "disabled" : ""}>↑</button>
          <button class="icon small move-down" title="Descendre" ${idx === vc.sources.length - 1 ? "disabled" : ""}>↓</button>
          <button class="icon small remove-source" title="Retirer">✕</button>
        </span>
      </li>
    `;
    }).join("");

    card.innerHTML = `
      <div class="virtual-card-head">
        <span class="drag-handle" draggable="true" title="Réordonner">⠿</span>
        <span class="vc-status ${checkStatus}" title="${dotTitle}"></span>
        <label class="toggle">
          <input type="checkbox" class="active-toggle" ${vc.active ? "checked" : ""}>
          <span class="track"></span>
          <span class="thumb"></span>
        </label>
        <input class="name-input" type="text" value="${escapeAttr(vc.name)}">
        <button class="danger small delete-btn">Supprimer</button>
      </div>
      <ul class="source-list">${sourcesHtml || '<li class="empty-state">Aucun lien source.</li>'}</ul>
    `;

    card.querySelector(".active-toggle").addEventListener("change", (e) =>
      updateVirtualChannel(vc.id, { active: e.target.checked })
    );
    card.querySelector(".name-input").addEventListener("change", (e) =>
      updateVirtualChannel(vc.id, { name: e.target.value.trim() || vc.name })
    );
    card.querySelector(".delete-btn").addEventListener("click", () => deleteVirtualChannel(vc.id));

    card.querySelectorAll(".remove-source").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        const srcId = e.target.closest(".source-row").dataset.sourceId;
        removeSource(vc.id, srcId);
      });
    });
    card.querySelectorAll(".move-up").forEach((btn) => {
      btn.addEventListener("click", (e) => reorderSource(vc, e.target.closest(".source-row").dataset.sourceId, -1));
    });
    card.querySelectorAll(".move-down").forEach((btn) => {
      btn.addEventListener("click", (e) => reorderSource(vc, e.target.closest(".source-row").dataset.sourceId, 1));
    });

    const handle = card.querySelector(".drag-handle");

    handle.addEventListener("dragstart", (e) => {
      dragSrcId = vc.id;
      card.classList.add("dragging");
      list.classList.add("is-dragging");
      e.dataTransfer.effectAllowed = "move";
    });

    handle.addEventListener("dragend", () => {
      dragSrcId = null;
      card.classList.remove("dragging");
      list.classList.remove("is-dragging");
      list.querySelectorAll(".virtual-card.drag-over").forEach((c) => c.classList.remove("drag-over"));
    });

    card.addEventListener("dragover", (e) => {
      if (dragSrcId == null || dragSrcId === vc.id) return;
      e.preventDefault();
      list.querySelectorAll(".virtual-card.drag-over").forEach((c) => { if (c !== card) c.classList.remove("drag-over"); });
      card.classList.add("drag-over");
    });

    card.addEventListener("drop", async (e) => {
      e.preventDefault();
      card.classList.remove("drag-over");
      if (dragSrcId == null || dragSrcId === vc.id) return;
      const srcId = dragSrcId;
      const ids = state.virtualChannels.map((v) => v.id);
      const from = ids.indexOf(srcId);
      const to = ids.indexOf(vc.id);
      const newIds = [...ids];
      newIds.splice(from, 1);
      newIds.splice(to, 0, srcId);
      state.virtualChannels = newIds.map((id) => state.virtualChannels.find((v) => v.id === id));
      renderVirtualChannels();
      try {
        await api("/api/virtual/reorder", { method: "POST", body: JSON.stringify({ ids: newIds }) });
      } catch (err) {
        alert(err.message);
        await loadVirtualChannels();
      }
    });

    list.appendChild(card);
  }
}

async function updateVirtualChannel(id, patch) {
  await api(`/api/virtual/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
  await loadVirtualChannels();
}

async function deleteVirtualChannel(id) {
  if (!confirm("Supprimer cette chaîne de la lineup ?")) return;
  await api(`/api/virtual/${id}`, { method: "DELETE" });
  await loadVirtualChannels();
}

async function removeSource(vcId, srcId) {
  await api(`/api/virtual/${vcId}/sources/${srcId}`, { method: "DELETE" });
  await loadVirtualChannels();
}

async function reorderSource(vc, srcId, delta) {
  const ids = vc.sources.map((s) => s.id);
  const idx = ids.indexOf(Number(srcId));
  const target = idx + delta;
  if (target < 0 || target >= ids.length) return;
  [ids[idx], ids[target]] = [ids[target], ids[idx]];
  await api(`/api/virtual/${vc.id}/sources/reorder`, {
    method: "POST",
    body: JSON.stringify({ order: ids }),
  });
  await loadVirtualChannels();
}

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

el("export-btn").addEventListener("click", () => {
  window.location.href = "/api/export";
});

// ---------------------------------------------------------------------------
// Utils + init
// ---------------------------------------------------------------------------

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}
function escapeAttr(str) {
  return escapeHtml(str);
}

(async function init() {
  await Promise.all([
    loadSources(),
    loadRefreshSetting(),
    loadGroups(),
    loadChannels(),
    loadVirtualChannels(),
  ]);
})();

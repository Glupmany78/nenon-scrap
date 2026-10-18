// NEON GRAB popup controller.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

let currentTab = null;
let pollTimer = null;
let lastJobsSnapshot = "";

// ── helpers ────────────────────────────────────────────────────────────

function sendBg(msg) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(msg, (res) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      if (!res) return reject(new Error("no response from background"));
      if (res.error) return reject(new Error(res.error));
      resolve(res);
    });
  });
}

function toast(msg, ms = 2000) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove("show"), ms);
}

function fmtBytes(b) {
  if (!b || b <= 0) return "—";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0, n = b;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 100 ? 0 : 1) + " " + u[i];
}

function fmtSpeed(bps) {
  if (!bps || bps <= 0) return "";
  return fmtBytes(bps) + "/s";
}

function fmtEta(s) {
  if (!s || s <= 0) return "";
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
  return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
}

function timeAgo(ts) {
  if (!ts) return "";
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "just now";
  if (d < 3600) return Math.floor(d / 60) + "m ago";
  if (d < 86400) return Math.floor(d / 3600) + "h ago";
  return Math.floor(d / 86400) + "d ago";
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
}

// ── status ─────────────────────────────────────────────────────────────

async function checkBackend() {
  const dot = $("#statusDot");
  const txt = $("#statusText");
  dot.className = "status-dot checking";
  txt.textContent = "checking…";
  try {
    const res = await sendBg({ type: "api:health" });
    if (res && res.ok) {
      dot.className = "status-dot online";
      txt.textContent = "backend online";
      return true;
    }
    dot.className = "status-dot offline";
    txt.textContent = "backend offline";
    return false;
  } catch (e) {
    dot.className = "status-dot offline";
    txt.textContent = "offline";
    return false;
  }
}

// ── tabs ───────────────────────────────────────────────────────────────

function setupTabs() {
  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const target = tab.dataset.tab;
      $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      $$(".panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === target));
    });
  });
}

// ── enable toggle ──────────────────────────────────────────────────────

async function loadEnabled() {
  const s = await chrome.storage.local.get(["enabled", "showVideoGlow", "showImageGlow"]);
  $("#enableToggle").checked = !!s.enabled;
  $("#showVideoGlow").checked = s.showVideoGlow !== false;
  $("#showImageGlow").checked = s.showImageGlow !== false;
}

$("#enableToggle").addEventListener("change", async (e) => {
  const enabled = e.target.checked;
  await chrome.storage.local.set({ enabled });
  toast(enabled
    ? "NEON GRAB enabled. Reload the page if you don't see overlays."
    : "NEON GRAB disabled. Active downloads will continue.");
});

// ── media list ─────────────────────────────────────────────────────────

async function loadMedia() {
  try {
    const res = await sendBg({ type: "media:getForActiveTab" });
    const items = (res && res.items) || [];
    currentTab = (res && res.tab) || null;

    if (currentTab) {
      try {
        const u = new URL(currentTab.url);
        $("#pageHost").textContent = u.hostname;
      } catch { $("#pageHost").textContent = currentTab.url || "—"; }
      $("#pageTitle").textContent = currentTab.title || "";
    }

    $("#mediaCount").textContent = items.length + " item" + (items.length === 1 ? "" : "s");
    renderMedia(items);
  } catch (e) {
    console.error("loadMedia failed", e);
  }
}

function renderMedia(items) {
  const grid = $("#mediaGrid");
  const empty = $("#mediaEmpty");
  grid.innerHTML = "";
  if (!items.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  items.forEach((item) => {
    const card = document.createElement("div");
    card.className = "media-card";

    const thumb = document.createElement("div");
    thumb.className = "media-thumb";
    if (item.type === "image" && item.url) {
      const img = document.createElement("img");
      img.src = item.url;
      img.style.cssText = "width:100%;height:100%;object-fit:cover;border-radius:5px;";
      img.onerror = () => { thumb.textContent = "🖼"; };
      thumb.appendChild(img);
    } else if (item.type === "video") {
      thumb.textContent = "▶";
    } else {
      thumb.textContent = "•";
    }

    const info = document.createElement("div");
    info.className = "media-info";
    const typeEl = document.createElement("div");
    typeEl.className = "media-type";
    typeEl.textContent = item.type.toUpperCase() + (item.ext ? " · " + item.ext.toUpperCase() : "");
    const titleEl = document.createElement("div");
    titleEl.className = "media-title";
    titleEl.textContent = item.alt || decodeURIComponent((item.url || "").split("/").pop() || "media");
    titleEl.title = item.url || "";
    const metaEl = document.createElement("div");
    metaEl.className = "media-meta";
    if (item.width && item.height) metaEl.textContent = `${item.width}×${item.height}`;
    else metaEl.textContent = (item.pageUrl || "").split("/").slice(0, 3).join("/");
    info.appendChild(typeEl);
    info.appendChild(titleEl);
    info.appendChild(metaEl);

    const actions = document.createElement("div");
    actions.className = "media-actions";
    const btn = document.createElement("button");
    btn.className = "primary-btn";
    btn.textContent = item.type === "image" ? "GRAB" : "FORMATS";
    btn.addEventListener("click", () => handleMediaAction(item, btn));
    actions.appendChild(btn);

    card.appendChild(thumb);
    card.appendChild(info);
    card.appendChild(actions);
    grid.appendChild(card);
  });
}

async function handleMediaAction(item, btn) {
  if (item.type === "image") {
    btn.disabled = true;
    btn.textContent = "…";
    try {
      const res = await sendBg({
        type: "api:startDownload",
        url: item.url,
        mediaType: "image",
        pageUrl: item.pageUrl,
        formatId: "direct",
        title: item.alt || item.url.split("/").pop(),
      });
      if (res && res.ok) {
        btn.textContent = "QUEUED";
        toast("Image queued");
        refreshDownloads();
      } else {
        btn.textContent = "FAILED";
        btn.disabled = false;
      }
    } catch (e) {
      btn.textContent = "FAILED";
      btn.disabled = false;
      toast("Failed: " + e.message);
    }
    return;
  }
  await openFormatsInPopup(item);
}

// Popup-side format picker for videos.
async function openFormatsInPopup(item) {
  const host = $("#mediaGrid");
  const existing = $("#popupFormatPanel");
  if (existing) existing.remove();

  const panel = document.createElement("div");
  panel.id = "popupFormatPanel";
  panel.style.cssText = `
    background:#0f1a16;border:1px solid rgba(0,255,136,0.25);
    border-radius:10px;padding:10px;margin-bottom:10px;
  `;
  panel.innerHTML = `<div style="color:#00ff88;font:700 10px ui-monospace;letter-spacing:.14em;">FETCHING FORMATS…</div>`;
  host.parentNode.insertBefore(panel, host);

  try {
    const res = await sendBg({
      type: "api:getFormats",
      url: item.url,
      mediaType: "video",
      pageUrl: item.pageUrl,
    });
    const formats = (res && res.formats) || [];
    if (!formats.length) {
      const errMsg = (res && res.error) ? res.error : "No downloadable formats found for this media.";
      panel.innerHTML = `<div style="color:#ff7b7b;font-size:11px;word-break:break-word;">${escapeHtml(errMsg)}</div>`;
      return;
    }
    let selected = formats[0].format_id;
    const render = () => {
      panel.innerHTML = `
        <div style="color:#00ff88;font:700 10px ui-monospace;letter-spacing:.14em;margin-bottom:6px;">
          SELECT FORMAT
        </div>
        ${formats.map((f) => {
          const size = f.filesize ? (f.filesize / 1048576).toFixed(1) + " MB" : "—";
          const audioFlag = f.has_video && !f.has_audio ? "video only" : (f.has_video ? "video+audio" : "audio only");
          const meta = [f.ext, f.fps ? f.fps + "fps" : "", audioFlag].filter(Boolean).join(" · ");
          const sel = f.format_id === selected ? "border-color:#00ff88;background:rgba(0,255,136,.08);" : "";
          return `<div class="pop-fmt" data-id="${f.format_id}" style="
            display:flex;justify-content:space-between;align-items:center;
            padding:6px 10px;margin-bottom:4px;border-radius:6px;
            border:1px solid rgba(255,255,255,.06);cursor:pointer;${sel}
          ">
            <div><div style="font-weight:700;font-size:12px;color:#d7f5e4;">${escapeHtml(f.resolution)}</div>
            <div style="font-size:10px;color:#6f9488;font-family:ui-monospace;">${escapeHtml(meta)}</div></div>
            <div style="font-size:10px;color:#a7d8c4;font-family:ui-monospace;">${size}</div>
          </div>`;
        }).join("")}
        <div style="display:flex;gap:6px;margin-top:8px;">
          <button class="ghost-btn" id="popFmtCancel">Cancel</button>
          <button class="primary-btn" id="popFmtGo">Download</button>
        </div>
      `;
      panel.querySelectorAll(".pop-fmt").forEach((el) => {
        el.addEventListener("click", () => { selected = el.dataset.id; render(); });
      });
      panel.querySelector("#popFmtCancel").addEventListener("click", () => panel.remove());
      panel.querySelector("#popFmtGo").addEventListener("click", async (e) => {
        e.target.disabled = true;
        e.target.textContent = "QUEUED…";
        try {
          // Auto-merge best audio for video-only formats (e.g. YouTube 1080p).
          const fmt = formats.find((f) => f.format_id === selected);
          let spec = selected;
          if (fmt && fmt.has_video && !fmt.has_audio && selected !== "direct") {
            spec = `${selected}+bestaudio/${selected}`;
          }
          const r = await sendBg({
            type: "api:startDownload",
            url: item.url,
            mediaType: "video",
            pageUrl: item.pageUrl,
            formatId: spec,
            title: item.alt || document.title || "video",
          });
          if (r && r.ok) {
            e.target.textContent = "QUEUED ✓";
            toast("Download queued");
            setTimeout(() => panel.remove(), 700);
            refreshDownloads();
          } else {
            e.target.textContent = "FAILED";
            e.target.disabled = false;
            if (r && r.error) {
              panel.innerHTML = `<div style="color:#ff7b7b;font-size:11px;">${escapeHtml(r.error)}</div>`;
            }
          }
        } catch (err) {
          e.target.textContent = "FAILED";
          e.target.disabled = false;
        }
      });
    };
    render();
  } catch (e) {
    panel.innerHTML = `<div style="color:#ff7b7b;font-size:11px;">${escapeHtml(e.message || e)}</div>`;
  }
}

// ── jobs ──────────────────────────────────────────────────────────────

async function refreshDownloads() {
  try {
    const res = await sendBg({ type: "api:listDownloads" });
    const jobs = (res && res.jobs) || [];
    const snap = JSON.stringify(jobs.map((j) => j.id + "|" + j.status + "|" + Math.round(j.progress || 0)));
    if (snap === lastJobsSnapshot) return;
    lastJobsSnapshot = snap;
    renderJobs(jobs);
  } catch (e) {
    $("#activeEmpty").textContent = "Backend unreachable.";
    $("#activeEmpty").classList.remove("hidden");
  }
}

function renderJobs(all) {
  const active = all.filter((j) => j.status === "running" || j.status === "queued");
  const finished = all.filter((j) => j.status !== "running" && j.status !== "queued");

  const activeList = $("#activeList");
  const activeEmpty = $("#activeEmpty");
  activeList.innerHTML = "";
  if (!active.length) {
    activeEmpty.classList.remove("hidden");
  } else {
    activeEmpty.classList.add("hidden");
    active.forEach((j) => activeList.appendChild(renderJobCard(j, true)));
  }

  const historyList = $("#historyList");
  const historyEmpty = $("#historyEmpty");
  historyList.innerHTML = "";
  if (!finished.length) {
    historyEmpty.classList.remove("hidden");
  } else {
    historyEmpty.classList.add("hidden");
    finished.forEach((j) => historyList.appendChild(renderJobCard(j, false)));
  }

  const activeTabBtn = document.querySelector('.tab[data-tab="downloads"]');
  if (activeTabBtn) activeTabBtn.textContent = active.length ? `Active (${active.length})` : "Active";
}

function renderJobCard(job, isActive) {
  const card = document.createElement("div");
  card.className = "job";

  const head = document.createElement("div");
  head.className = "job-head";
  const title = document.createElement("div");
  title.className = "job-title";
  title.title = job.title || job.filename || job.url;
  title.textContent = job.title || job.filename || (job.url || "").split("/").pop() || "download";
  const statusEl = document.createElement("span");
  statusEl.className = "job-status " + job.status;
  statusEl.textContent = job.status.toUpperCase();
  head.appendChild(title);
  head.appendChild(statusEl);
  card.appendChild(head);

  if (isActive) {
    const prog = document.createElement("div");
    prog.className = "job-progress";
    const bar = document.createElement("div");
    bar.className = "job-progress-bar";
    const pct = job.progress || 0;
    if (job.total_bytes && job.total_bytes > 0) {
      bar.style.width = Math.min(100, pct) + "%";
    } else if (job.status === "running") {
      bar.classList.add("indeterminate");
    }
    prog.appendChild(bar);
    card.appendChild(prog);
  }

  const meta = document.createElement("div");
  meta.className = "job-meta";
  const left = document.createElement("span");
  if (isActive) {
    if (job.total_bytes && job.total_bytes > 0) {
      left.textContent = `${fmtBytes(job.downloaded_bytes || 0)} / ${fmtBytes(job.total_bytes)}`;
    } else if (job.downloaded_bytes > 0) {
      left.textContent = fmtBytes(job.downloaded_bytes);
    } else {
      left.textContent = job.status === "queued" ? "queued…" : "starting…";
    }
  } else {
    left.textContent = job.completed_at ? timeAgo(job.completed_at) : "";
  }
  const right = document.createElement("span");
  if (isActive) {
    const spd = fmtSpeed(job.speed);
    const eta = fmtEta(job.eta);
    right.textContent = [spd, eta ? "ETA " + eta : ""].filter(Boolean).join(" · ");
  } else if (job.status === "failed" && job.error) {
    right.textContent = job.error.slice(0, 40);
    right.style.color = "#ff7b7b";
  }
  meta.appendChild(left);
  meta.appendChild(right);
  card.appendChild(meta);

  if (isActive) {
    const actions = document.createElement("div");
    actions.className = "job-actions";
    const cancel = document.createElement("button");
    cancel.textContent = "CANCEL";
    cancel.addEventListener("click", async () => {
      try {
        await sendBg({ type: "api:cancelDownload", jobId: job.id });
        lastJobsSnapshot = "";
        refreshDownloads();
      } catch (e) {
        toast("Cancel failed: " + e.message);
      }
    });
    actions.appendChild(cancel);
    card.appendChild(actions);
  }

  return card;
}

// ── history ────────────────────────────────────────────────────────────

$("#clearHistoryBtn").addEventListener("click", async () => {
  if (!confirm("Clear download history?\n\nActive downloads will NOT be cancelled and files will NOT be deleted.")) {
    return;
  }
  try {
    const res = await sendBg({ type: "api:clearHistory" });
    toast(`Removed ${res.removed || 0} history record(s)`);
    lastJobsSnapshot = "";
    refreshDownloads();
  } catch (e) {
    toast("Clear failed: " + e.message);
  }
});

// ── settings ───────────────────────────────────────────────────────────

async function loadSettings() {
  const s = await chrome.storage.local.get(["apiBase", "apiToken", "showVideoGlow", "showImageGlow"]);
  $("#apiBaseInput").value = s.apiBase || "http://127.0.0.1:8765";
  $("#apiTokenInput").value = s.apiToken || "";
  $("#showVideoGlow").checked = s.showVideoGlow !== false;
  $("#showImageGlow").checked = s.showImageGlow !== false;

  try {
    const res = await sendBg({ type: "api:getConfig" });
    if (res && res.config) {
      $("#outputDirInput").value = res.config.output_dir || "";
      $("#maxConcurrentInput").value = res.config.max_concurrent || 3;
    }
  } catch { /* backend offline — leave blank */ }
}

$("#saveSettingsBtn").addEventListener("click", async () => {
  const apiBase = $("#apiBaseInput").value.trim() || "http://127.0.0.1:8765";
  const apiToken = $("#apiTokenInput").value.trim();
  const showVideoGlow = $("#showVideoGlow").checked;
  const showImageGlow = $("#showImageGlow").checked;
  await chrome.storage.local.set({ apiBase, apiToken, showVideoGlow, showImageGlow });
  toast("Settings saved");
  checkBackend();
});

$("#saveConfigBtn").addEventListener("click", async () => {
  const patch = {};
  const out = $("#outputDirInput").value.trim();
  const mc = parseInt($("#maxConcurrentInput").value, 10);
  if (out) patch.output_dir = out;
  if (mc > 0) patch.max_concurrent = mc;
  if (!Object.keys(patch).length) {
    toast("Nothing to save");
    return;
  }
  try {
    const res = await sendBg({ type: "api:patchConfig", patch });
    if (res && res.ok) toast("Backend config updated");
    else toast("Update failed");
  } catch (e) {
    toast("Update failed: " + e.message);
  }
});

$("#testBackendBtn").addEventListener("click", async () => {
  const ok = await checkBackend();
  toast(ok ? "Backend reachable ✓" : "Backend unreachable");
});

$("#settingsBtn").addEventListener("click", () => {
  document.querySelector('.tab[data-tab="settings"]').click();
});

$("#rescanBtn").addEventListener("click", async () => {
  try {
    const res = await sendBg({ type: "content:requestRescan" });
    if (res && res.ok) {
      setTimeout(loadMedia, 300);
      toast("Rescan triggered");
    } else {
      toast("Rescan failed — reload the page");
    }
  } catch (e) {
    toast("Rescan failed — reload the page");
  }
});

// ── init ───────────────────────────────────────────────────────────────

async function init() {
  setupTabs();
  await loadEnabled();
  await checkBackend();
  await loadSettings();
  await loadMedia();
  await refreshDownloads();

  pollTimer = setInterval(async () => {
    if (document.visibilityState === "visible") {
      await refreshDownloads();
      await loadMedia();
    }
  }, 1000);
}

document.addEventListener("DOMContentLoaded", init);
window.addEventListener("unload", () => {
  if (pollTimer) clearInterval(pollTimer);
});
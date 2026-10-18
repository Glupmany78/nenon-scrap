// NEON GRAB — background service worker.
// Single source of truth for API calls; content scripts and popup message us.

const tabMedia = new Map();

// ── helpers ──────────────────────────────────────────────────────────

async function getSettings() {
  const data = await chrome.storage.local.get([
    "apiBase", "apiToken", "enabled", "showVideoGlow", "showImageGlow",
  ]);
  return {
    apiBase: data.apiBase || "http://127.0.0.1:8765",
    apiToken: data.apiToken || "",
    enabled: !!data.enabled,
    showVideoGlow: data.showVideoGlow !== false,
    showImageGlow: data.showImageGlow !== false,
  };
}

async function apiFetch(path, options = {}) {
  const { apiBase, apiToken } = await getSettings();
  const url = apiBase.replace(/\/+$/, "") + path;
  const headers = new Headers(options.headers || {});
  headers.set("Content-Type", "application/json");
  if (apiToken) headers.set("Authorization", "Bearer " + apiToken);

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 110000);

  try {
    const res = await fetch(url, { ...options, headers, signal: controller.signal });
    clearTimeout(timeoutId);
    const text = await res.text();
    let body;
    try { body = text ? JSON.parse(text) : {}; } catch { body = { raw: text }; }
    if (!res.ok) {
      const msg = body.detail || body.error || `HTTP ${res.status}`;
      console.warn("[neon-grab] apiFetch failed", path, res.status, msg);
      throw new Error(msg);
    }
    return body;
  } catch (e) {
    clearTimeout(timeoutId);
    if (e.name === "AbortError") {
      throw new Error("Backend timed out (110s). Is the backend running?");
    }
    throw e;
  }
}

async function pingBackend() {
  try {
    const { apiBase } = await getSettings();
    const res = await fetch(apiBase.replace(/\/+$/, "") + "/api/health", { method: "GET" });
    if (!res.ok) return { ok: false, error: `HTTP ${res.status}` };
    const data = await res.json();
    return { ok: true, data };
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
}

async function tryOpenPopup() {
  try {
    if (chrome.action && typeof chrome.action.openPopup === "function") {
      await chrome.action.openPopup();
      console.log("[neon-grab] popup opened");
      return true;
    }
    console.warn("[neon-grab] openPopup not available in this Chrome");
  } catch (e) {
    console.warn("[neon-grab] openPopup failed:", e && e.message ? e.message : e);
  }
  return false;
}

// ── tab bookkeeping ──────────────────────────────────────────────────

chrome.tabs.onRemoved.addListener((tabId) => tabMedia.delete(tabId));
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === "loading") tabMedia.delete(tabId);
});

// ── message router ───────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  console.log("[neon-grab bg] message:", msg.type);
  handleMessage(msg, sender).then((r) => {
    console.log("[neon-grab bg] response for", msg.type, ":", r);
    sendResponse(r);
  }).catch((err) => {
    console.error("[neon-grab bg] error for", msg.type, ":", err);
    sendResponse({ error: String(err.message || err) });
  });
  return true;
});

async function handleMessage(msg, sender) {
  const { type } = msg;

  switch (type) {
    case "media:report": {
      const tabId = sender.tab && sender.tab.id;
      if (typeof tabId === "number") tabMedia.set(tabId, msg.items || []);
      return { ok: true };
    }

    case "media:getForActiveTab": {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab) return { ok: true, items: [], tab: null };
      return {
        ok: true,
        items: tabMedia.get(tab.id) || [],
        tab: { id: tab.id, url: tab.url, title: tab.title },
      };
    }

    case "media:getForTab": {
      return { ok: true, items: tabMedia.get(msg.tabId) || [] };
    }

    case "api:health": {
      return await pingBackend();
    }

    case "api:getFormats": {
      const body = {
        url: msg.url,
        media_type: msg.mediaType || "video",
        page_url: msg.pageUrl || null,
        referer: msg.pageUrl || null,
      };
      return await apiFetch("/api/formats", { method: "POST", body: JSON.stringify(body) });
    }

    case "api:startDownload": {
      const body = {
        url: msg.url,
        media_type: msg.mediaType || "video",
        page_url: msg.pageUrl || null,
        format_id: msg.formatId || null,
        title: msg.title || null,
        referer: msg.pageUrl || null,
      };
      const result = await apiFetch("/api/downloads", { method: "POST", body: JSON.stringify(body) });
      tryOpenPopup();
      return result;
    }

    case "api:listDownloads": {
      return await apiFetch("/api/downloads", { method: "GET" });
    }

    case "api:getDownload": {
      return await apiFetch("/api/downloads/" + encodeURIComponent(msg.jobId), { method: "GET" });
    }

    case "api:cancelDownload": {
      return await apiFetch(
        "/api/downloads/" + encodeURIComponent(msg.jobId) + "/cancel",
        { method: "POST", body: "{}" }
      );
    }

    case "api:clearHistory": {
      return await apiFetch("/api/history", { method: "DELETE" });
    }

    case "api:getConfig": {
      return await apiFetch("/api/config", { method: "GET" });
    }

    case "api:patchConfig": {
      return await apiFetch("/api/config", {
        method: "PATCH",
        body: JSON.stringify(msg.patch || {}),
      });
    }

    case "tab:reload": {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (tab && tab.id !== undefined) {
        chrome.tabs.reload(tab.id);
        return { ok: true };
      }
      return { ok: false, error: "no active tab" };
    }

    case "content:requestRescan": {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab) return { ok: false, error: "no active tab" };
      try {
        const response = await chrome.tabs.sendMessage(tab.id, { type: "content:rescan" });
        return { ok: true, response };
      } catch (e) {
        return { ok: false, error: String(e.message || e) };
      }
    }

    case "popup:open": {
      return { ok: await tryOpenPopup() };
    }

    default:
      return { error: "unknown message type: " + type };
  }
}

// ── initial storage defaults ─────────────────────────────────────────

chrome.runtime.onInstalled.addListener(async () => {
  const current = await chrome.storage.local.get(null);
  const defaults = {
    enabled: current.enabled !== undefined ? current.enabled : false,
    apiBase: current.apiBase || "http://127.0.0.1:8765",
    apiToken: current.apiToken || "",
    showVideoGlow: current.showVideoGlow !== false,
    showImageGlow: current.showImageGlow !== false,
  };
  await chrome.storage.local.set(defaults);
});
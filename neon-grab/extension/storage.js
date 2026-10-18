// Thin wrapper over chrome.storage.local with defaults.
(function () {
  const DEFAULTS = {
    enabled: false,
    apiBase: "http://127.0.0.1:8765",
    apiToken: "",
    showVideoGlow: true,
    showImageGlow: true,
  };

  async function getAll() {
    const data = await chrome.storage.local.get(Object.keys(DEFAULTS));
    const merged = { ...DEFAULTS };
    for (const k of Object.keys(DEFAULTS)) {
      if (data[k] !== undefined) merged[k] = data[k];
    }
    return merged;
  }

  async function get(key) {
    const all = await getAll();
    return all[key];
  }

  async function set(partial) {
    await chrome.storage.local.set(partial);
  }

  window.NeonGrabStorage = { getAll, get, set, DEFAULTS };
})();
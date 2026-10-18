// NEON GRAB — content script. Injects neon overlays and the format modal.
(function () {
  if (window.__neonGrabInjected) return;
  window.__neonGrabInjected = true;

  let enabled = false;
  let showVideoGlow = true;
  let showImageGlow = true;
  let lastReportedJSON = "";

  // ── utilities ────────────────────────────────────────────────────

  function sendBg(msg, timeoutMs = 60000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new Error(`Background timed out after ${timeoutMs}ms for ${msg.type}`));
      }, timeoutMs);
      try {
        chrome.runtime.sendMessage(msg, (res) => {
          clearTimeout(timer);
          if (chrome.runtime.lastError) {
            return reject(new Error("Extension error: " + chrome.runtime.lastError.message));
          }
          if (!res) return reject(new Error("No response from background"));
          if (res.error) return reject(new Error(res.error));
          resolve(res);
        });
      } catch (e) {
        clearTimeout(timer);
        reject(new Error("sendMessage failed: " + (e.message || e) +
          " — try reloading the page"));
      }
    });
  }

  function showToast(msg, type = "info", duration = 4000) {
    try {
      const old = document.getElementById("neon-grab-toast");
      if (old) old.remove();
      const parent = document.body || document.documentElement;
      if (!parent) return;
      const host = document.createElement("div");
      host.id = "neon-grab-toast";
      host.dataset.neonGrab = "1";
      host.style.cssText = "position: fixed; top: 16px; right: 16px; z-index: 2147483647; pointer-events: none;";
      const shadow = host.attachShadow({ mode: "open" });
      const colors = {
        info: { bg: "rgba(0, 20, 12, 0.95)", border: "rgba(0, 255, 136, 0.55)", text: "#a9ffd1" },
        error: { bg: "rgba(40, 5, 5, 0.95)", border: "rgba(255, 91, 91, 0.55)", text: "#ffb0b0" },
        success: { bg: "rgba(0, 20, 12, 0.95)", border: "rgba(0, 255, 136, 0.9)", text: "#00ff88" },
      };
      const c = colors[type] || colors.info;
      const safe = String(msg).replace(/[&<>"']/g, (ch) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[ch]));
      shadow.innerHTML = `
        <style>
          .toast {
            background: ${c.bg};
            border: 1px solid ${c.border};
            border-radius: 8px;
            padding: 10px 14px;
            color: ${c.text};
            font: 600 12px ui-monospace, SFMono-Regular, Menlo, monospace;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
            max-width: 360px;
            word-wrap: break-word;
            line-height: 1.4;
          }
        </style>
        <div class="toast">${safe}</div>
      `;
      parent.appendChild(host);
      setTimeout(() => { try { host.remove(); } catch (_) {} }, duration);
    } catch (e) {
      console.warn("[neon-grab] toast failed:", e);
    }
  }

  // ── media reporting ──────────────────────────────────────────────

  function reportMedia() {
    if (!enabled) return;
    const items = window.NeonGrabDetector.detectAll();
    const json = JSON.stringify(items.map((i) => i.type + "|" + i.url));
    if (json === lastReportedJSON) return;
    lastReportedJSON = json;
    sendBg({ type: "media:report", items }, 5000).catch(() => {});
  }

  // ── global pointer tracking ──────────────────────────────────────

  const pointer = { x: -1, y: -1, inside: false };
  document.addEventListener("mousemove", (e) => {
    pointer.x = e.clientX;
    pointer.y = e.clientY;
    pointer.inside = true;
  }, { passive: true, capture: true });
  document.addEventListener("mouseout", (e) => {
    if (!e.relatedTarget && !e.toElement) pointer.inside = false;
  }, { passive: true, capture: true });
  window.addEventListener("blur", () => { pointer.inside = false; });

  // ── video overlay ────────────────────────────────────────────────

  const videoOverlays = new Map();

  function isVideoElementVisible(v) {
    if (!v.isConnected) return false;
    const rect = v.getBoundingClientRect();
    if (rect.width < 100 || rect.height < 100) return false;
    const style = window.getComputedStyle(v);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
    return true;
  }

  function createVideoOverlay(video) {
    const overlay = document.createElement("div");
    overlay.dataset.neonGrab = "1";
    overlay.style.cssText = `
      position: fixed; pointer-events: none; z-index: 2147483646;
      border-radius: 8px;
      box-shadow: 0 0 0 2px rgba(0,255,136,0.55), 0 0 22px 4px rgba(0,255,136,0.28);
      transition: opacity 200ms ease;
    `;
    const shadow = overlay.attachShadow({ mode: "open" });
    shadow.innerHTML = `
      <style>
        .badge {
          position: absolute; top: 8px; left: 8px;
          padding: 3px 8px; border-radius: 6px;
          background: rgba(0, 20, 12, 0.82);
          border: 1px solid rgba(0, 255, 136, 0.55);
          color: #a9ffd1; font: 600 10px ui-monospace, SFMono-Regular, Menlo, monospace;
          letter-spacing: 0.08em; pointer-events: none;
        }
        .btn {
          position: absolute; top: 8px; right: 8px; pointer-events: auto;
          padding: 6px 12px; border-radius: 6px;
          background: linear-gradient(180deg, #10e081 0%, #00b86b 100%);
          border: 1px solid rgba(0, 255, 136, 0.7);
          color: #041a10; font: 700 11px ui-monospace, SFMono-Regular, Menlo, monospace;
          letter-spacing: 0.08em; cursor: pointer;
          opacity: 0;
          transition: opacity 120ms ease;
          box-shadow: 0 0 0 1px rgba(0, 255, 136, 0.25), 0 6px 20px rgba(0, 255, 136, 0.25);
        }
        .btn.visible { opacity: 1; }
        .btn:hover { box-shadow: 0 0 0 1px rgba(0, 255, 136, 0.5), 0 8px 26px rgba(0, 255, 136, 0.45); }
        .btn:disabled { opacity: 0.5; cursor: wait; }
      </style>
      <div class="badge">NEON READY</div>
      <button class="btn">DOWNLOAD</button>
    `;
    const btn = shadow.querySelector(".btn");

    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const url = video.currentSrc || video.src;
      if (!url) {
        showToast("NEON GRAB: no video URL detected on this element.", "error", 5000);
        return;
      }
      try {
        openFormatModal(video, url);
      } catch (err) {
        console.error("[neon-grab] openFormatModal threw:", err);
        showToast("Modal failed: " + (err.message || err), "error", 6000);
      }
    });

    const parent = document.body || document.documentElement;
    parent.appendChild(overlay);
    return { overlay, btn, isVisible: false };
  }

  function positionOverlay(overlay, target) {
    if (!target.isConnected) {
      overlay.remove();
      return false;
    }
    const rect = target.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0 ||
        rect.bottom < 0 || rect.top > window.innerHeight ||
        rect.right < 0 || rect.left > window.innerWidth) {
      overlay.style.display = "none";
      return true;
    }
    overlay.style.display = "";
    overlay.style.left = rect.left + "px";
    overlay.style.top = rect.top + "px";
    overlay.style.width = rect.width + "px";
    overlay.style.height = rect.height + "px";
    return true;
  }

  function pointInRect(x, y, r) {
    return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
  }

  function tick() {
    for (const [video, state] of videoOverlays) {
      if (!video.isConnected) {
        state.overlay.remove();
        videoOverlays.delete(video);
        continue;
      }
      const alive = positionOverlay(state.overlay, video);
      if (!alive) {
        state.overlay.remove();
        videoOverlays.delete(video);
        continue;
      }
      if (state.overlay.style.display === "none") {
        if (state.isVisible) {
          state.btn.classList.remove("visible");
          state.isVisible = false;
        }
        continue;
      }
      let shouldShow = false;
      if (pointer.inside) {
        const vr = video.getBoundingClientRect();
        const br = state.btn.getBoundingClientRect();
        shouldShow = pointInRect(pointer.x, pointer.y, vr) ||
                     pointInRect(pointer.x, pointer.y, br);
      }
      if (shouldShow !== state.isVisible) {
        state.isVisible = shouldShow;
        state.btn.classList.toggle("visible", shouldShow);
      }
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  function decorateVideos() {
    if (!enabled || !showVideoGlow) return;
    for (const [video, state] of videoOverlays) {
      if (!video.isConnected) {
        state.overlay.remove();
        videoOverlays.delete(video);
      }
    }
    for (const v of document.querySelectorAll("video")) {
      if (videoOverlays.has(v)) continue;
      if (!isVideoElementVisible(v)) continue;
      const state = createVideoOverlay(v);
      videoOverlays.set(v, state);
      positionOverlay(state.overlay, v);
    }
  }

  // ── image hover overlay ──────────────────────────────────────────

  const imageOverlays = new WeakMap();
  const imageHoverBound = new WeakSet();

  function bindImageHover(img) {
    if (imageHoverBound.has(img)) return;
    imageHoverBound.add(img);

    img.addEventListener("mouseenter", () => {
      if (!enabled || !showImageGlow) return;
      if (img.clientWidth < 60 || img.clientHeight < 60) return;
      const overlay = createImageOverlay(img);
      imageOverlays.set(img, overlay);
      positionOverlay(overlay, img);
    });
    img.addEventListener("mouseleave", () => {
      const overlay = imageOverlays.get(img);
      if (overlay) {
        overlay.remove();
        imageOverlays.delete(img);
      }
    });
  }

  function createImageOverlay(img) {
    const overlay = document.createElement("div");
    overlay.dataset.neonGrab = "1";
    overlay.style.cssText = `
      position: fixed; pointer-events: none; z-index: 2147483645;
      border-radius: 8px;
      box-shadow: 0 0 0 2px rgba(0,255,136,0.55), 0 0 18px 3px rgba(0,255,136,0.25);
      transition: opacity 150ms ease;
    `;
    const shadow = overlay.attachShadow({ mode: "open" });
    shadow.innerHTML = `
      <style>
        .btn {
          position: absolute; bottom: 8px; right: 8px; pointer-events: auto;
          padding: 6px 12px; border-radius: 6px;
          background: linear-gradient(180deg, #10e081 0%, #00b86b 100%);
          border: 1px solid rgba(0, 255, 136, 0.7);
          color: #041a10; font: 700 11px ui-monospace, SFMono-Regular, Menlo, monospace;
          letter-spacing: 0.08em; cursor: pointer;
          box-shadow: 0 0 0 1px rgba(0, 255, 136, 0.25), 0 6px 20px rgba(0, 255, 136, 0.25);
        }
      </style>
      <button class="btn">DOWNLOAD</button>
    `;
    const btn = shadow.querySelector(".btn");
    btn.addEventListener("click", async (e) => {
      e.preventDefault(); e.stopPropagation();
      btn.disabled = true;
      btn.textContent = "QUEUED…";
      try {
        const src = img.currentSrc || img.src;
        const resp = await sendBg({
          type: "api:startDownload",
          url: src,
          mediaType: "image",
          pageUrl: location.href,
          formatId: "direct",
          title: img.alt || src.split("/").pop(),
        }, 30000);
        if (resp && resp.ok) {
          btn.textContent = "QUEUED ✓";
          showToast("Image queued ✓", "success", 2500);
        } else {
          btn.textContent = "FAILED";
          btn.disabled = false;
          showToast("Image failed: " + ((resp && resp.error) || "unknown"), "error", 6000);
        }
      } catch (err) {
        btn.textContent = "FAILED";
        btn.disabled = false;
        showToast("Image failed: " + (err.message || err), "error", 6000);
      }
    });
    const parent = document.body || document.documentElement;
    parent.appendChild(overlay);
    return overlay;
  }

  function decorateImages() {
    if (!enabled) return;
    for (const img of document.querySelectorAll("img")) bindImageHover(img);
  }

  // ── format modal — ONE-CLICK DOWNLOAD PER ROW ────────────────────

  function groupFormats(formats) {
    const combined = [];   
    const videoOnly = [];  
    const audioOnly = [];  
    for (const f of formats) {
      if (f.has_video && f.has_audio) combined.push(f);
      else if (f.has_video) videoOnly.push(f);
      else if (f.has_audio) audioOnly.push(f);
    }
    const byHeight = (a, b) => (b.height || 0) - (a.height || 0) || (b.tbr || 0) - (a.tbr || 0);
    combined.sort(byHeight);
    videoOnly.sort(byHeight);
    audioOnly.sort((a, b) => (b.abr || 0) - (a.abr || 0));
    return { combined, videoOnly, audioOnly };
  }

  function pickBestFormat(formats) {
    const { combined, videoOnly, audioOnly } = groupFormats(formats);
    if (combined.length) return combined[0];
    if (videoOnly.length) return videoOnly[0];
    if (audioOnly.length) return audioOnly[0];
    return formats[0];
  }

  function openFormatModal(video, mediaUrl) {
    console.log("[neon-grab] openFormatModal:", mediaUrl);
    showToast("Fetching formats…", "info", 2000);

    const existingHost = document.getElementById("neon-grab-modal-host");
    if (existingHost) existingHost.remove();

    const host = document.createElement("div");
    host.id = "neon-grab-modal-host";
    host.dataset.neonGrab = "1";
    // Bulletproof full-screen overlay to prevent YouTube from hiding it
    host.style.cssText = "position: fixed !important; top: 0 !important; left: 0 !important; width: 100vw !important; height: 100vh !important; z-index: 2147483647 !important; display: block !important; pointer-events: auto !important;";
    
    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
      <style>
        .backdrop {
          position: absolute; inset: 0; background: rgba(2, 8, 6, 0.85);
          backdrop-filter: blur(4px);
          display: flex; align-items: center; justify-content: center;
        }
        .panel {
          background: #0c1412; border: 1px solid rgba(0, 255, 136, 0.28);
          border-radius: 14px; width: min(620px, 92vw); max-height: 86vh; overflow: hidden;
          display: flex; flex-direction: column;
          box-shadow: 0 20px 60px rgba(0, 255, 136, 0.18), 0 0 0 1px rgba(0, 255, 136, 0.1);
          color: #d7f5e4; font: 13px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }
        .header {
          padding: 14px 18px; border-bottom: 1px solid rgba(255,255,255,0.06);
          display: flex; justify-content: space-between; align-items: center;
        }
        .title { font: 700 12px ui-monospace, SFMono-Regular, Menlo, monospace;
                 color: #00ff88; letter-spacing: 0.14em; }
        .subtitle { font-size: 10px; color: #6f9488; margin-top: 2px; letter-spacing: 0.06em; }
        .close { background: transparent; color: #7ba393; border: none; font-size: 18px; cursor: pointer; padding: 0 6px; }
        .close:hover { color: #00ff88; }
        .body { padding: 14px 18px; overflow-y: auto; }
        .body::-webkit-scrollbar { width: 8px; }
        .body::-webkit-scrollbar-thumb { background: rgba(0,255,136,0.25); border-radius: 4px; }
        .status { padding: 22px; text-align: center; color: #7ba393; font-size: 12px; }
        .status.error { color: #ff7b7b; word-break: break-word; text-align: left; }

        .best-card {
          display: flex; justify-content: space-between; align-items: center;
          padding: 14px 16px; margin-bottom: 14px;
          background: linear-gradient(180deg, rgba(0,255,136,0.16), rgba(0,255,136,0.06));
          border: 1.5px solid rgba(0,255,136,0.75); border-radius: 10px;
          cursor: pointer; transition: all 150ms ease;
          box-shadow: 0 0 24px rgba(0,255,136,0.25);
        }
        .best-card:hover { background: linear-gradient(180deg, rgba(0,255,136,0.22), rgba(0,255,136,0.09)); transform: translateY(-1px); }
        .best-left { display: flex; flex-direction: column; gap: 3px; }
        .best-title { font-weight: 800; font-size: 14px; color: #00ff88; letter-spacing: 0.04em; }
        .best-meta { font-size: 11px; color: #a7d8c4; font-family: ui-monospace; }
        .best-btn {
          padding: 9px 18px; border-radius: 8px; font-weight: 800; cursor: pointer;
          background: linear-gradient(180deg, #10e081 0%, #00b86b 100%);
          color: #041a10; border: none;
          font: 800 12px ui-monospace, SFMono-Regular, Menlo, monospace;
          letter-spacing: 0.1em;
        }

        .section-title {
          font: 700 10px ui-monospace; color: #00ff88; letter-spacing: 0.14em;
          margin: 14px 0 6px; text-transform: uppercase;
        }
        .section-title:first-of-type { margin-top: 6px; }

        .fmt-row {
          display: flex; justify-content: space-between; align-items: center;
          padding: 10px 12px; border-radius: 8px; cursor: pointer;
          border: 1px solid rgba(255,255,255,0.06); margin-bottom: 6px;
          transition: all 120ms ease;
          background: rgba(255,255,255,0.02);
        }
        .fmt-row:hover {
          background: rgba(0, 255, 136, 0.08);
          border-color: rgba(0, 255, 136, 0.5);
          transform: translateX(2px);
        }
        .fmt-left { display: flex; flex-direction: column; gap: 3px; flex: 1; }
        .fmt-res { font-weight: 700; font-size: 13px; color: #e6fef3; }
        .fmt-meta { font-size: 11px; color: #6f9488; font-family: ui-monospace; }
        .fmt-right { display: flex; align-items: center; gap: 10px; }
        .fmt-size { font-size: 11px; color: #a7d8c4; font-family: ui-monospace; }
        .dl-icon {
          display: inline-flex; align-items: center; justify-content: center;
          width: 26px; height: 26px; border-radius: 6px;
          background: linear-gradient(180deg, #10e081 0%, #00b86b 100%);
          color: #041a10; font-weight: 800; font-size: 13px;
        }

        .footer {
          padding: 12px 18px; border-top: 1px solid rgba(255,255,255,0.06);
          display: flex; justify-content: space-between; align-items: center;
          font-size: 11px; color: #6f9488;
        }
        .footer button {
          background: transparent; color: #a7d8c4;
          border: 1px solid rgba(0, 255, 136, 0.28); padding: 7px 14px;
          border-radius: 8px; cursor: pointer;
          font: 600 11px ui-monospace, SFMono-Regular, Menlo, monospace;
        }
        .footer button:hover { background: rgba(0,255,136,0.08); }
      </style>
      <div class="backdrop">
        <div class="panel">
          <div class="header">
            <div>
              <div class="title">NEON GRAB · DOWNLOAD</div>
              <div class="subtitle">Click any row to download immediately</div>
            </div>
            <button class="close">✕</button>
          </div>
          <div class="body"><div class="status">Fetching formats…</div></div>
          <div class="footer">
            <span>Backend: NEON GRAB local</span>
            <button class="cancel">CLOSE</button>
          </div>
        </div>
      </div>
    `;
    
    // Append to body, which is much more reliable on YouTube than documentElement
    const parent = document.body || document.documentElement;
    parent.appendChild(host);
    console.log("[neon-grab] modal appended to", parent.tagName);

    const close = () => { try { host.remove(); } catch (_) {} };
    shadow.querySelector(".close").addEventListener("click", close);
    shadow.querySelector(".cancel").addEventListener("click", close);
    shadow.querySelector(".backdrop").addEventListener("click", (e) => {
      if (e.target.classList && e.target.classList.contains("backdrop")) close();
    });

    const bodyEl = shadow.querySelector(".body");

    async function startDownloadForFormat(f, rowEl) {
      bodyEl.style.pointerEvents = "none";
      bodyEl.style.opacity = "0.6";

      let spec = f.format_id;
      // Auto-merge audio if it's a video-only stream
      if (f.has_video && !f.has_audio && f.format_id !== "direct") {
        spec = `${f.format_id}+bestaudio/${f.format_id}`;
      }

      try {
        const r = await sendBg({
          type: "api:startDownload",
          url: mediaUrl,
          mediaType: "video",
          pageUrl: location.href,
          formatId: spec,
          title: `${document.title || "video"} (${f.resolution || f.ext || "media"})`,
        }, 30000);

        if (r && r.ok) {
          showToast(`Started: ${f.resolution || f.ext || "download"} ✓ — check the popup`, "success", 6000);
          close();
        } else {
          const errMsg = (r && r.error) || "unknown error";
          showToast("Failed: " + errMsg, "error", 8000);
          bodyEl.style.pointerEvents = "";
          bodyEl.style.opacity = "";
        }
      } catch (err) {
        showToast("Failed: " + (err.message || err), "error", 8000);
        bodyEl.style.pointerEvents = "";
        bodyEl.style.opacity = "";
      }
    }

    sendBg({
      type: "api:getFormats",
      url: mediaUrl,
      mediaType: "video",
      pageUrl: location.href,
    }, 120000).then((res) => {
      const formats = (res && res.formats) || [];
      if (!formats.length) {
        const errMsg = (res && res.error) ? res.error : "No downloadable formats found.";
        bodyEl.innerHTML = `<div class="status error">${escapeHtml(errMsg)}<br/><br/>` +
          `This media may be DRM-protected, blob-only, or require authentication.</div>`;
        showToast("No formats found", "error", 5000);
        return;
      }

      const best = pickBestFormat(formats);
      const { combined, videoOnly, audioOnly } = groupFormats(formats);

      bodyEl.innerHTML = "";

      const bestCard = document.createElement("div");
      bestCard.className = "best-card";
      const bestSize = best.filesize ? (best.filesize / 1048576).toFixed(1) + " MB" : "size unknown";
      bestCard.innerHTML = `
        <div class="best-left">
          <div class="best-title">⚡ BEST QUALITY</div>
          <div class="best-meta">${escapeHtml(best.resolution || "original")} · ${escapeHtml(best.ext || "")} · ${bestSize}</div>
        </div>
        <button class="best-btn">DOWNLOAD</button>
      `;
      bestCard.addEventListener("click", () => startDownloadForFormat(best));
      bodyEl.appendChild(bestCard);

      function addSection(title, list) {
        if (!list.length) return;
        const hdr = document.createElement("div");
        hdr.className = "section-title";
        hdr.textContent = title + " · click to download";
        bodyEl.appendChild(hdr);
        for (const f of list) {
          const row = document.createElement("div");
          row.className = "fmt-row";
          const size = f.filesize ? (f.filesize / 1048576).toFixed(1) + " MB" : "—";
          const fps = f.fps ? ` · ${f.fps}fps` : "";
          row.innerHTML = `
            <div class="fmt-left">
              <span class="fmt-res">${escapeHtml(f.resolution || "unknown")}</span>
              <span class="fmt-meta">${escapeHtml(f.ext || "")}${fps}</span>
            </div>
            <div class="fmt-right">
              <span class="fmt-size">${size}</span>
              <span class="dl-icon">↓</span>
            </div>
          `;
          row.addEventListener("click", () => startDownloadForFormat(f, row));
          bodyEl.appendChild(row);
        }
      }

      addSection("Video + Audio", combined);
      addSection("Video Only (audio auto-merged)", videoOnly);
      addSection("Audio Only", audioOnly);

      showToast(`${formats.length} format(s) available — click any row to download`, "success", 3000);
    }).catch((err) => {
      bodyEl.innerHTML = `<div class="status error">${escapeHtml(String(err.message || err))}</div>`;
      showToast("Backend error: " + (err.message || err), "error", 8000);
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
  }

  // ── observer + setup ─────────────────────────────────────────────

  function scanAndDecorate() {
    if (!enabled) return;
    decorateVideos();
    decorateImages();
    reportMedia();
  }

  let scanTimer = null;
  function debouncedScan() {
    if (scanTimer) clearTimeout(scanTimer);
    scanTimer = setTimeout(scanAndDecorate, 250);
  }

  let observer = null;
  function startObserver() {
    if (observer) return;
    observer = new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (m.addedNodes && m.addedNodes.length) { debouncedScan(); return; }
        if (m.type === "attributes" && (m.attributeName === "src" || m.attributeName === "srcset")) {
          debouncedScan(); return;
        }
      }
    });
    observer.observe(document.documentElement, {
      childList: true, subtree: true,
      attributes: true, attributeFilter: ["src", "srcset", "poster"],
    });
  }

  function stopObserver() {
    if (observer) { observer.disconnect(); observer = null; }
  }

  function clearOverlays() {
    for (const el of document.querySelectorAll('[data-neon-grab="1"]')) el.remove();
    videoOverlays.clear();
  }

  async function readEnabled() {
    const s = await chrome.storage.local.get(["enabled", "showVideoGlow", "showImageGlow"]);
    enabled = !!s.enabled;
    showVideoGlow = s.showVideoGlow !== false;
    showImageGlow = s.showImageGlow !== false;
  }

  async function start() {
    await readEnabled();
    if (!enabled) return;
    scanAndDecorate();
    startObserver();
  }

  async function stop() {
    enabled = false;
    stopObserver();
    clearOverlays();
    sendBg({ type: "media:report", items: [] }, 5000).catch(() => {});
  }

  chrome.storage.onChanged.addListener((changes) => {
    if (changes.enabled) {
      if (changes.enabled.newValue) start();
      else stop();
    }
  });

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === "content:rescan") {
      scanAndDecorate();
      sendResponse({ ok: true });
    }
  });

  setTimeout(start, 300);
})();
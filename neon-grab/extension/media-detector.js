// Detects supported media on the page. No side effects — returns arrays.
(function () {
  const IMAGE_EXT_RE = /\.(jpe?g|png|gif|webp|bmp|tiff?|avif|heic)(\?|#|$)/i;
  const VIDEO_EXT_RE = /\.(mp4|mkv|webm|avi|mov|flv|wmv|m4v|ts|mpg|mpeg|3gp|m3u8|m3u)(\?|#|$)/i;

  function inferExt(url) {
    try {
      const u = new URL(url, location.href);
      const m = u.pathname.match(/\.[a-z0-9]{2,5}$/i);
      return m ? m[0].toLowerCase().slice(1) : "";
    } catch { return ""; }
  }

  function classify(url) {
    if (!url) return null;
    const clean = url.split("#")[0];
    if (VIDEO_EXT_RE.test(clean)) return "video";
    if (IMAGE_EXT_RE.test(clean)) return "image";
    return null;
  }

  // Collect srcset candidates, pick the largest one.
  function bestSrcset(srcset) {
    if (!srcset) return null;
    const parts = srcset.split(",").map((s) => s.trim()).filter(Boolean);
    let best = null, bestScore = -1;
    for (const p of parts) {
      const [url, desc] = p.split(/\s+/);
      let score = 0;
      if (desc && desc.endsWith("w")) score = parseInt(desc, 10) || 0;
      else if (desc && desc.endsWith("x")) score = (parseFloat(desc) || 1) * 1000;
      if (score > bestScore) { bestScore = score; best = url; }
    }
    return best;
  }

  function detectVideos() {
    const out = [];
    for (const v of document.querySelectorAll("video")) {
      const srcs = new Set();
      if (v.currentSrc) srcs.add(v.currentSrc);
      if (v.src) srcs.add(v.src);
      for (const s of v.querySelectorAll("source")) {
        if (s.src) srcs.add(s.src);
      }
      const poster = v.poster || null;
      const size = { w: v.videoWidth || v.clientWidth, h: v.videoHeight || v.clientHeight };
      for (const src of srcs) {
        if (!src) continue;
        if (src.startsWith("blob:")) {
          // Can't resolve blob directly — hand to backend via page URL.
          out.push({
            type: "video", url: src, pageUrl: location.href,
            poster, isBlob: true, width: size.w, height: size.h,
            ext: "mp4",
          });
        } else if (src.startsWith("http") || src.startsWith("//")) {
          out.push({
            type: "video", url: src, pageUrl: location.href,
            poster, width: size.w, height: size.h,
            ext: inferExt(src) || "mp4",
          });
        }
      }
    }
    return out;
  }

  function detectImages() {
    const out = [];
    for (const img of document.querySelectorAll("img")) {
      // Skip invisible / tiny / tracking pixels
      if (!img.offsetParent && img.clientWidth === 0 && img.clientHeight === 0) continue;
      if (img.clientWidth < 32 || img.clientHeight < 32) continue;
      const candidate = bestSrcset(img.srcset)
        || img.dataset.src
        || img.dataset.original
        || img.currentSrc
        || img.src;
      if (!candidate) continue;
      if (candidate.startsWith("data:")) continue;
      // Resolve relative
      let url;
      try { url = new URL(candidate, location.href).toString(); } catch { continue; }
      // Only keep if the extension looks like an image or we can't tell but it's clearly large
      const ext = inferExt(url);
      const isImage = IMAGE_EXT_RE.test(url) || ext === "" || ["jpg","jpeg","png","gif","webp","avif","bmp"].includes(ext);
      if (!isImage) continue;
      out.push({
        type: "image",
        url,
        pageUrl: location.href,
        width: img.naturalWidth || img.clientWidth,
        height: img.naturalHeight || img.clientHeight,
        ext: ext || "jpg",
        alt: img.alt || "",
      });
    }
    return out;
  }

  function detectMetaVideo() {
    const out = [];
    const selectors = [
      'meta[property="og:video:secure_url"]',
      'meta[property="og:video:url"]',
      'meta[property="og:video"]',
      'meta[name="twitter:player:stream"]',
    ];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (!el) continue;
      const content = el.getAttribute("content");
      if (!content) continue;
      try {
        const url = new URL(content, location.href).toString();
        out.push({
          type: "video", url, pageUrl: location.href,
          fromMeta: true, ext: inferExt(url) || "mp4",
        });
      } catch { /* ignore */ }
    }
    return out;
  }

  function dedupe(items) {
    const seen = new Set();
    const out = [];
    for (const it of items) {
      const key = it.type + "|" + it.url;
      if (seen.has(key)) continue;
      seen.add(key);
      // Assign an id
      it.id = "m_" + Math.random().toString(36).slice(2, 10);
      out.push(it);
    }
    return out;
  }

  function detectAll() {
    return dedupe([...detectVideos(), ...detectImages(), ...detectMetaVideo()]);
  }

  window.NeonGrabDetector = { detectAll, classify, inferExt };
})();
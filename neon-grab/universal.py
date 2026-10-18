#!/usr/bin/env python3
"""
universal.py — one-command downloader. auto-bootstraps venv + deps on first run.
Generic multi-strategy crawler handles any gallery/video page. Known sites
(erome, betterome, fapeza, fapello, aznude) use tuned fast paths. Cloudflare-
protected hosts auto-route through Playwright. Working strategies cache per-host.

usage (CLI):
    python3 universal.py <url> [-o DIR] [-c cookies.txt] [-p proxy] [-j N] [-f N]

usage (library):
    from universal import extract_media_formats, download_with_progress, download_direct_file
"""

import os
import sys
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
VENV_DIR = SCRIPT_DIR / ".venv"
IS_WINDOWS = os.name == "nt"
VENV_PYTHON = VENV_DIR / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")

REQUIRED = ["yt-dlp", "gallery-dl", "curl_cffi", "playwright"]


def _in_venv() -> bool:
    try:
        return sys.prefix != sys.base_prefix and Path(sys.executable).resolve() == VENV_PYTHON.resolve()
    except Exception:
        return False


def _deps_ok() -> bool:
    try:
        import yt_dlp, gallery_dl, curl_cffi, playwright  # noqa
        return True
    except ImportError:
        return False


def _bootstrap():
    if not VENV_PYTHON.exists():
        print("[*] creating virtual environment (first run, one-time)...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
    if not _deps_ok():
        print("[*] installing dependencies (one-time)...")
        subprocess.run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip", "-q"], check=True)
        subprocess.run([str(VENV_PYTHON), "-m", "pip", "install", "-q"] + REQUIRED, check=True)
        print("[*] installing playwright chromium (one-time)...")
        subprocess.run([str(VENV_PYTHON), "-m", "playwright", "install", "chromium"], check=True)
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), __file__] + sys.argv[1:])


# IMPORTANT: only bootstrap when run directly. Importing must never relaunch.
if __name__ == "__main__" and not _in_venv():
    _bootstrap()

import re
import base64
import json
import shutil
import time
import logging
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, List, Dict, Tuple, Set, Callable, Any
from urllib.parse import urlparse, urljoin, urlunparse, parse_qsl, urlencode

import yt_dlp
from curl_cffi import requests as cffi_requests
from gallery_dl import config as gdl_config
from gallery_dl.job import DownloadJob as GDLJob
from gallery_dl.extractor import find as gdl_find
from playwright.sync_api import sync_playwright


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv", ".m4v", ".ts", ".mpg", ".mpeg", ".3gp", ".m3u8", ".m3u"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".avif", ".heic"}
ALL_MEDIA_EXTS = VIDEO_EXTS | IMAGE_EXTS

MEDIA_URL_RE = re.compile(
    r'https?://[^"\'\s<>()]+?\.(?:' + '|'.join(e.lstrip('.') for e in ALL_MEDIA_EXTS) + r')(?:\?[^"\'\s<>()]*)?',
    re.IGNORECASE,
)

THUMB_PATTERNS = [
    (re.compile(r'[_-](\d{2,4})px(?=\.[a-z]+(?:\?|$))', re.I), ''),
    (re.compile(r'[_-](?:thumb|thumbnail|small|preview|medium)(?=\.[a-z]+(?:\?|$))', re.I), ''),
    (re.compile(r'/thumbs?/(?:[^/]+/)?', re.I), '/'),
    (re.compile(r'/(?:small|medium|preview|thumbs?)/', re.I), '/'),
    (re.compile(r'[_-]\d{2,4}x\d{2,4}(?=\.[a-z]+(?:\?|$))', re.I), ''),
]

JUNK_SUBSTRINGS = (
    "/logo", "/icon", "/favicon", "/sprite", "/emoji", "/smiley",
    "/banner", "/ad_", "/ads/", "/advert", "/placeholder", "/spinner",
    "/avatar", "/apple-touch", "/welcomebar", "/button.",
    "/loading", "/preloader", "/bg.", "/background",
    "/vtt/", "sprite", "pemsrv", "beacon.min.js",
    "camsoda", "porndude", "chaturbate", "stripchat", "bongacams",
    "livejasmin", "cam4", "xlovecam", "jerkmate", "onlyfans.com/",
    "google-analytics", "googletagmanager", "doubleclick",
)

JUNK_BASENAMES = {
    "logo.png", "logo.jpg", "logo.jpeg", "logo.svg", "logo.webp", "logo.gif",
    "favicon.ico", "favicon.png", "favicon.svg",
    "apple-touch-icon.png", "apple-touch-icon-precomposed.png",
    "button.png", "button.svg", "site.webm", "site.mp4",
    "welcomebar15.png", "spinner.gif", "loading.gif", "placeholder.jpg",
    "pixel.gif", "pixel.png", "1x1.gif", "1x1.png",
    "spacer.gif", "transparent.gif", "blank.gif", "clear.gif",
}

CACHE_PATH = Path.home() / ".universal_dl_cache.json"
CACHE_LOCK = threading.Lock()


class Progress:
    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.done = 0
        self.total = 0

    def start(self, total: int):
        self.total = total

    def begin(self, label: str):
        with self._lock:
            self.active += 1
            print(f"  ▶ {label}  [active: {self.active}]", flush=True)

    def finish(self, label: str, ok: bool, extra: str = ""):
        with self._lock:
            self.active -= 1
            self.done += 1
            mark = "✓" if ok else "✗"
            tail = f"  {extra}" if extra else ""
            print(f"  {mark} {label}{tail}  [{self.done}/{self.total}, active: {self.active}]", flush=True)


# ══════════════════════════════════════════════════════════════════════
#  shared helpers
# ══════════════════════════════════════════════════════════════════════

def _host_key(url: str) -> str:
    return urlparse(url).netloc.lower()


def _load_cache() -> Dict:
    try:
        if CACHE_PATH.exists():
            return json.loads(CACHE_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_cache(data: Dict):
    try:
        with CACHE_LOCK:
            CACHE_PATH.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def _normalize_thumb(url: str) -> str:
    path = url
    for pat, repl in THUMB_PATTERNS:
        new = pat.sub(repl, path, count=1)
        if new != path:
            path = new
            break
    return path


def _dedupe_urls(urls: List[str]) -> List[str]:
    seen = set()
    out = []
    for u in urls:
        key = urlparse(u)._replace(query="").geturl()
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


def _is_junk_media(u: str) -> bool:
    low = u.lower()
    if any(s in low for s in JUNK_SUBSTRINGS):
        return True
    name = Path(urlparse(low).path).name
    if name in JUNK_BASENAMES:
        return True
    return False


def _filter_by_name_fragment(urls: List[str], fragment: str) -> List[str]:
    frag_norm = fragment.lower().replace("-", "_")
    out = []
    for u in urls:
        fname = Path(urlparse(u).path).name.lower().replace("-", "_")
        if frag_norm in fname:
            out.append(u)
    return out


def _discover_pagination(html: str, base_url: str) -> List[str]:
    out: List[str] = []
    parsed = urlparse(base_url)
    host = parsed.netloc
    for m in re.finditer(
        r'<a[^>]+href\s*=\s*"([^"]+)"[^>]*>\s*(?:Next|Next\s*›|›|»|&raquo;|\d+)\s*</a>',
        html, re.IGNORECASE,
    ):
        href = m.group(1)
        full = urljoin(base_url, href)
        if urlparse(full).netloc == host and full != base_url:
            out.append(full)
    return _dedupe_urls(out)[:50]


def _discover_ajax_endpoints(html: str, base_url: str) -> List[str]:
    out: List[str] = []
    parsed = urlparse(base_url)
    host = parsed.netloc
    for m in re.finditer(
        r'["\'](/[^"\']*(?:api|ajax|json|load|feed|data|list)[^"\']*)["\']',
        html, re.IGNORECASE,
    ):
        path = m.group(1)
        full = urljoin(base_url, path)
        if urlparse(full).netloc == host:
            out.append(full)
    return _dedupe_urls(out)[:30]


def _extract_media_from_html(html: str) -> Tuple[List[str], List[str]]:
    imgs: List[str] = []
    vids: List[str] = []

    for m in re.finditer(
        r'<img[^>]+(?:data-original|data-src|data-lazy|data-url|srcset|src)\s*=\s*"([^"]+)"',
        html, re.IGNORECASE
    ):
        raw = m.group(1)
        if "," in raw and " " in raw:
            raw = raw.split(",")[0].strip().split(" ")[0]
        if not raw.startswith("http"):
            continue
        for candidate in (raw, _normalize_thumb(raw)):
            if any(candidate.lower().endswith(e) for e in IMAGE_EXTS):
                imgs.append(candidate)

    for m in re.finditer(r'<(?:video|source)[^>]+src\s*=\s*"([^"]+)"', html, re.IGNORECASE):
        raw = m.group(1)
        if raw.startswith("http") and any(raw.lower().endswith(e) for e in VIDEO_EXTS):
            vids.append(raw)

    for m in re.finditer(
        r'<(?:video|source)[^>]+(?:data-src|data-url|data-video|data-source|data-file)\s*=\s*"([^"]+)"',
        html, re.IGNORECASE
    ):
        raw = m.group(1)
        if raw.startswith("http"):
            vids.append(raw)

    for m in re.finditer(
        r'<meta[^>]+(?:property|name)\s*=\s*"(?:og:video(?::secure_url|:url)?|twitter:player:stream)"[^>]+content\s*=\s*"([^"]+)"',
        html, re.IGNORECASE
    ):
        vids.append(m.group(1))

    for m in re.finditer(
        r'["\'](?:video[_-]?url|video[_-]?src|source|file|mp4|playlist)["\']\s*:\s*["\']([^"\']+\.(?:mp4|m3u8|webm|mkv)[^"\']*)["\']',
        html, re.IGNORECASE
    ):
        vids.append(m.group(1))

    for u in MEDIA_URL_RE.findall(html):
        low = u.lower()
        if any(low.endswith(e) or (e + "?") in low for e in VIDEO_EXTS):
            vids.append(u)
        elif any(low.endswith(e) or (e + "?") in low for e in IMAGE_EXTS):
            imgs.append(u)

    for m in re.finditer(
        r'<a[^>]+href\s*=\s*"([^"]+\.(?:' + '|'.join(e.lstrip('.') for e in ALL_MEDIA_EXTS) + r')(?:\?[^"]*)?)"',
        html, re.IGNORECASE
    ):
        u = m.group(1)
        if u.startswith("http"):
            if any(u.lower().endswith(e) for e in VIDEO_EXTS):
                vids.append(u)
            else:
                imgs.append(u)

    return _dedupe_urls(imgs), _dedupe_urls(vids)


def _post_filter(images: List[str], videos: List[str], url: str,
                 model_filter: Optional[str] = None) -> Tuple[List[str], List[str]]:
    imgs = [u for u in images if not _is_junk_media(u)]
    vids = [u for u in videos if not _is_junk_media(u)]
    imgs = [_normalize_thumb(u) for u in imgs]
    if model_filter:
        imgs = _filter_by_name_fragment(imgs, model_filter)
        vids = _filter_by_name_fragment(vids, model_filter)
    return _dedupe_urls(imgs), _dedupe_urls(vids)


# ══════════════════════════════════════════════════════════════════════
#  main downloader
# ══════════════════════════════════════════════════════════════════════

class UniversalDownloader:
    def __init__(
        self,
        output_dir: str = "./downloads",
        cookies_file: Optional[str] = None,
        proxy: Optional[str] = None,
        workers: int = 16,
        fragments: int = 24,
    ):
        self.root = Path(output_dir)
        self.videos_dir = self.root / "videos"
        self.images_dir = self.root / "images"
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)

        self.cookies_file = cookies_file
        self.proxy = proxy
        self.workers = workers
        self.fragments = fragments
        self._tls = threading.local()
        self.progress = Progress()
        self._aria2_warned = False
        self._pw_lock = threading.Lock()
        self._pw = None
        self._pw_browser = None

        self.aria2 = shutil.which("aria2c")
        print(f"[*] aria2c {'found — multi-connection enabled' if self.aria2 else 'NOT found — sudo apt install aria2'}")

        self.cache = _load_cache()
        self._configure_gallery_dl()

    def _ensure_aria2_conf(self):
        conf_dir = Path.home() / ".aria2"
        conf_dir.mkdir(parents=True, exist_ok=True)
        conf = conf_dir / "aria2.conf"
        wanted = [
            "max-connection-per-server=16", "split=16", "min-split-size=1M",
            "file-allocation=none", "console-log-level=error",
            "allow-overwrite=false", "auto-file-renaming=false",
            "retry-wait=2", "max-tries=5", "continue=true",
            "enable-http-pipelining=true", "max-concurrent-downloads=16",
        ]
        existing = conf.read_text() if conf.exists() else ""
        lines = [l for l in existing.splitlines() if l.strip() and not l.strip().startswith("#")]
        keys = {l.split("=")[0].strip() for l in wanted}
        merged = [l for l in lines if l.split("=")[0].strip() not in keys]
        merged.extend(wanted)
        conf.write_text("\n".join(sorted(set(merged))) + "\n")

    def _configure_gallery_dl(self):
        gdl_config.load()
        gdl_config.set(("extractor",), "threads", self.workers)
        if self.aria2:
            self._ensure_aria2_conf()
            gdl_config.set(("downloader",), "aria2", self.aria2)
        gdl_config.set(("downloader",), "threads", self.workers)
        gdl_config.set(("extractor",), "filename", "{filename}.{extension}")
        gdl_config.set(("extractor",), "base-directory", str(self.images_dir))
        gdl_config.set((), "quiet", True)
        gdl_config.set((), "retries", 10)
        gdl_config.set((), "timeout", 30)
        gdl_config.set((), "skip", True)
        if not logging.getLogger().handlers:
            logging.basicConfig(level=logging.WARNING)
        if self.cookies_file and Path(self.cookies_file).exists():
            gdl_config.set((), "cookies", str(self.cookies_file))
        if self.proxy:
            gdl_config.set((), "proxy", self.proxy)

    def _is_gallery_site(self, url: str) -> bool:
        try:
            return gdl_find(url) is not None
        except Exception:
            return False

    def _download_gallery(self, url: str) -> bool:
        print(f"[gallery-dl] {url}")
        try:
            GDLJob(url).run()
            return True
        except Exception as e:
            print(f"[gallery-dl] failed: {e}")
            return False

    def _sort_gallery_output(self):
        moved = 0
        for src in list(self.images_dir.rglob("*")):
            if src.is_file() and src.suffix.lower() in VIDEO_EXTS:
                dst = self.videos_dir / src.name
                if dst.exists():
                    stem, suffix = dst.stem, dst.suffix
                    n = 1
                    while dst.exists():
                        dst = self.videos_dir / f"{stem}_{n}{suffix}"
                        n += 1
                shutil.move(str(src), str(dst))
                moved += 1
        for d in sorted(self.images_dir.rglob("*"), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                except Exception:
                    pass
        if moved:
            print(f"[*] sorted {moved} video(s)")

    def _get_session(self) -> cffi_requests.Session:
        if not hasattr(self._tls, "session"):
            s = cffi_requests.Session(impersonate="chrome124")
            s.headers.update({
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Upgrade-Insecure-Requests": "1",
            })
            if self.proxy:
                s.proxies = {"http": self.proxy, "https": self.proxy}
            if self.cookies_file and Path(self.cookies_file).exists():
                self._load_cookies_into(s)
            self._tls.session = s
        return self._tls.session

    def _load_cookies_into(self, session):
        try:
            with open(self.cookies_file) as f:
                for line in f:
                    if line.startswith("#") or not line.strip():
                        continue
                    parts = line.strip().split("\t")
                    if len(parts) >= 7:
                        domain, _, path, _, _, name, value = parts[:7]
                        session.cookies.set(name, value, domain=domain.lstrip("."), path=path)
        except Exception as e:
            print(f"[cookies] {e}")

    def _get_playwright(self):
        with self._pw_lock:
            if self._pw is None:
                self._pw = sync_playwright().start()
                kwargs = {"headless": True, "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ]}
                if self.proxy:
                    kwargs["proxy"] = {"server": self.proxy}
                self._pw_browser = self._pw.chromium.launch(**kwargs)
            return self._pw, self._pw_browser

    def _pw_context(self, browser):
        ctx_kwargs = {
            "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "viewport": {"width": 1920, "height": 1080},
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }
        ctx = browser.new_context(**ctx_kwargs)
        if self.cookies_file and Path(self.cookies_file).exists():
            pw_cookies = []
            with open(self.cookies_file) as f:
                for line in f:
                    if line.startswith("#") or not line.strip():
                        continue
                    parts = line.strip().split("\t")
                    if len(parts) >= 7:
                        domain, _, path, _, _, name, value = parts[:7]
                        pw_cookies.append({"name": name, "value": value, "domain": domain, "path": path})
            if pw_cookies:
                ctx.add_cookies(pw_cookies)
        return ctx

    def _render_page(self, url: str, scroll_iters: int = 8,
                     intercept_media: bool = False,
                     wait_ms: int = 5000) -> Tuple[Optional[str], List[str]]:
        captured: List[str] = []
        captured_lock = threading.Lock()

        try:
            _, browser = self._get_playwright()
            ctx = self._pw_context(browser)
            page = ctx.new_page()

            if intercept_media:
                def on_response(resp):
                    u = resp.url
                    low = u.lower()
                    if any(low.endswith(e) or (e + "?") in low for e in VIDEO_EXTS) or \
                       ".m3u8" in low or any(low.endswith(e) or (e + "?") in low for e in IMAGE_EXTS):
                        if _is_junk_media(u):
                            return
                        with captured_lock:
                            captured.append(u)
                page.on("response", on_response)

            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_selector("img, video, a, source", timeout=15_000)
            except Exception:
                pass

            for _ in range(3):
                title = page.title() or ""
                if "just a moment" in title.lower() or "checking" in title.lower():
                    print("[playwright] cloudflare challenge detected, waiting...")
                    page.wait_for_timeout(6000)
                else:
                    break

            if intercept_media:
                page.wait_for_timeout(wait_ms)

            last_h = 0
            stable = 0
            for _ in range(scroll_iters):
                page.mouse.wheel(0, 6000)
                page.wait_for_timeout(700)
                h = page.evaluate("document.body.scrollHeight")
                if h == last_h:
                    stable += 1
                    if stable >= 2:
                        break
                else:
                    stable = 0
                    last_h = h

            html = page.content()
            page.close()
            ctx.close()
            return html, captured
        except Exception as e:
            print(f"[playwright] render failed: {e}")
            return None, captured

    def _ydl_opts(self, extra: Optional[Dict] = None) -> Dict:
        opts = {
            "quiet": True, "no_warnings": True, "ignoreerrors": True,
            "retries": 10, "fragment_retries": 10, "socket_timeout": 30,
            "concurrent_fragment_downloads": self.fragments,
            "http_chunk_size": 10 * 1024 * 1024,
            "continuedl": True, "overwrites": False, "noprogress": True,
            "skip_unavailable_fragments": True,
        }
        if self.aria2:
            opts["external_downloader"] = {"default": self.aria2}
            opts["external_downloader_args"] = {"aria2c": ["-x", "16", "-s", "16", "-k", "1M",
                "--min-split-size", "1M", "--max-connection-per-server", "16",
                "--file-allocation", "none", "--console-log-level", "error"]}
        if self.cookies_file:
            opts["cookiefile"] = self.cookies_file
        if self.proxy:
            opts["proxy"] = self.proxy
        if extra:
            opts.update(extra)
        return opts

    def _scrape_aznude(self, url: str) -> List[Dict]:
        print(f"[aznude] rendering (cloudflare-protected)...")
        html, captured = self._render_page(url, scroll_iters=6, intercept_media=True, wait_ms=6000)
        if not html and not captured:
            print("[aznude] render produced nothing")
            return []
        html_text = html or ""
        imgs, vids = _extract_media_from_html(html_text)
        for m in re.finditer(r'\bh\s*=\s*"([^"]+)"', html_text, re.IGNORECASE):
            u = m.group(1).replace("&amp;", "&")
            if ".m3u8" in u.lower():
                vids.append(u)
        for m in re.finditer(r'https?://[^"\'\s<>()]+?\.m3u8(?:\?[^"\'\s<>()]*)?', html_text, re.IGNORECASE):
            vids.append(m.group(0).replace("&amp;", "&"))
        for m in re.finditer(r'eid\s*=\s*"(/embed/[^"]+\.html)"', html_text, re.IGNORECASE):
            embed_url = urljoin(url, m.group(1))
            try:
                r = self._get_session().get(embed_url, timeout=(10, 30), headers={"Referer": url})
                if r.status_code == 200:
                    for mm in re.finditer(r'\bh\s*=\s*"([^"]+)"', r.text, re.IGNORECASE):
                        u = mm.group(1).replace("&amp;", "&")
                        if ".m3u8" in u.lower():
                            vids.append(u)
            except Exception:
                pass
        for u in captured:
            low = u.lower()
            if ".m3u8" in low or any(low.endswith(e) or (e + "?") in low for e in VIDEO_EXTS):
                vids.append(u)
            elif any(low.endswith(e) or (e + "?") in low for e in IMAGE_EXTS):
                imgs.append(u)
        for m in re.finditer(r'(https?://cdn\d?\.aznude\.com/[^"\'\s<>]+?\.(?:jpg|jpeg|png|webp|gif))', html_text, re.IGNORECASE):
            imgs.append(m.group(1))
        if any(".m3u8" in v.lower() for v in vids):
            vids = [v for v in vids if not v.lower().endswith(".ts")]
        imgs, vids = _post_filter(imgs, vids, url)
        items: List[Dict] = []
        idx = 0
        for u in vids:
            idx += 1
            stem = Path(urlparse(u).path).stem or f"video_{idx:03d}"
            fname = f"{stem}.mp4"
            items.append({"type": "video", "url": u, "filename": fname, "title": fname, "index": idx,
                          "referer": url, "via_aria2": False})
        for u in imgs:
            idx += 1
            fname = Path(urlparse(u).path).name or f"image_{idx:03d}.jpg"
            items.append({"type": "image", "url": u, "filename": fname, "title": fname, "index": idx,
                          "ext": Path(fname).suffix.lower() or ".jpg", "referer": url, "via_aria2": True})
        return items

    def _scrape_generic(self, url: str) -> List[Dict]:
        host = _host_key(url)
        print(f"[*] generic engine → {host}")
        static_html = ""
        try:
            r = self._get_session().get(url, timeout=(10, 60))
            if r.status_code == 200:
                static_html = r.text
        except Exception as e:
            print(f"[generic] static fetch failed: {e}")
        static_imgs, static_vids = _extract_media_from_html(static_html) if static_html else ([], [])
        rendered_html = ""
        captured: List[str] = []
        if len(static_imgs) + len(static_vids) < 5:
            print(f"[generic] static yielded {len(static_imgs) + len(static_vids)} item(s), rendering...")
            rendered_html, captured = self._render_page(url, intercept_media=True) or ("", [])
            rendered_html = rendered_html or ""
        rendered_imgs, rendered_vids = _extract_media_from_html(rendered_html) if rendered_html else ([], [])
        all_imgs = static_imgs + rendered_imgs
        all_vids = static_vids + rendered_vids
        for u in captured:
            low = u.lower()
            if ".m3u8" in low or any(low.endswith(e) or (e + "?") in low for e in VIDEO_EXTS):
                all_vids.append(u)
            elif any(low.endswith(e) or (e + "?") in low for e in IMAGE_EXTS):
                all_imgs.append(u)
        html_for_pagination = rendered_html or static_html
        sibling_pages = _discover_pagination(html_for_pagination, url) if html_for_pagination else []
        ajax_hits: List[str] = []
        if html_for_pagination and len(all_imgs) + len(all_vids) < 20:
            ajax_hits = _discover_ajax_endpoints(html_for_pagination, url)
        extra_urls = sibling_pages + ajax_hits
        if extra_urls:
            print(f"[generic] following {len(sibling_pages)} page(s) + {len(ajax_hits)} endpoint(s)")
            def fetch(u):
                try:
                    r = self._get_session().get(u, timeout=(10, 45),
                        headers={"X-Requested-With": "XMLHttpRequest", "Referer": url})
                    if r.status_code == 200:
                        return (u, r.text)
                except Exception:
                    return None
                return None
            with ThreadPoolExecutor(max_workers=min(24, max(8, self.workers))) as pool:
                for f in as_completed([pool.submit(fetch, u) for u in extra_urls]):
                    res = f.result()
                    if not res:
                        continue
                    _, body = res
                    fragment = body
                    try:
                        j = json.loads(body)
                        for k in ("html", "content", "data", "result"):
                            if isinstance(j, dict) and isinstance(j.get(k), str):
                                fragment = j[k]
                                break
                    except Exception:
                        pass
                    i2, v2 = _extract_media_from_html(fragment)
                    all_imgs.extend(i2)
                    all_vids.extend(v2)
        all_imgs, all_vids = _post_filter(all_imgs, all_vids, url)
        items: List[Dict] = []
        idx = 0
        for u in all_vids:
            idx += 1
            fname = Path(urlparse(u).path).name or f"video_{idx:03d}.mp4"
            items.append({"type": "video", "url": u, "filename": fname, "title": fname, "index": idx,
                         "referer": url, "via_aria2": True})
        for u in all_imgs:
            idx += 1
            fname = Path(urlparse(u).path).name or f"image_{idx:03d}.jpg"
            items.append({"type": "image", "url": u, "filename": fname, "title": fname, "index": idx,
                         "ext": Path(fname).suffix.lower() or ".jpg", "referer": url, "via_aria2": True})
        if items:
            self.cache[host] = {"strategy": "generic", "last_ok": int(time.time())}
            _save_cache(self.cache)
        return items

    @staticmethod
    def _decode_betterome_url(encoded: str) -> Optional[str]:
        if not encoded:
            return None
        b64 = encoded.split("v1.", 1)[1] if "v1." in encoded else encoded
        if "." in b64:
            b64 = b64.split(".", 1)[0]
        b64 = b64.replace("-", "+").replace("_", "/")
        b64 += "=" * (-len(b64) % 4)
        try:
            obj = json.loads(base64.b64decode(b64).decode("utf-8"))
            return obj.get("u")
        except Exception:
            return None

    def _scrape_betterome(self, url: str) -> List[Dict]:
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
        if not slug:
            return []
        try:
            r = self._get_session().get(f"https://betterome.com/api/album?slug={slug}", timeout=(10, 60))
            data = r.json()
        except Exception:
            return []
        if not data.get("ok"):
            return []
        payload = data.get("payload") or {}
        title = self._sanitize(payload.get("pageTitle") or slug)
        vids: List[str] = []
        for key in ("mp4", "m3u8"):
            for enc in ((payload.get(key) or {}).get("unique") or []):
                real = self._decode_betterome_url(enc)
                if real:
                    vids.append(real)
        imgs: List[str] = []
        ib = payload.get("images") or {}
        for enc in ib.get("unique") or []:
            real = self._decode_betterome_url(enc)
            if real:
                imgs.append(real)
        if not imgs:
            for enc in ib.get("fromDom") or []:
                real = self._decode_betterome_url(enc)
                if real:
                    imgs.append(real)
        imgs, vids = _post_filter(imgs, vids, url)
        items: List[Dict] = []
        idx = 0
        ref = "https://www.erome.com/"
        for u in vids:
            idx += 1
            fname = Path(urlparse(u).path).name or f"{title}_{idx:02d}.mp4"
            items.append({"type": "video", "url": u, "filename": fname, "title": fname, "index": idx, "referer": ref, "via_aria2": True})
        for u in imgs:
            idx += 1
            fname = Path(urlparse(u).path).name or f"{title}_{idx:02d}.jpg"
            items.append({"type": "image", "url": u, "filename": fname, "title": fname, "index": idx,
                         "ext": Path(fname).suffix.lower() or ".jpg", "referer": ref, "via_aria2": True})
        return items

    def _scrape_erome(self, url: str) -> List[Dict]:
        try:
            html = self._get_session().get(url, timeout=(10, 60)).text
        except Exception:
            return []
        title_m = re.search(r'<title>([^<]+)</title>', html)
        title = self._sanitize(title_m.group(1)) if title_m else "album"
        imgs, vids = _extract_media_from_html(html)
        imgs, vids = _post_filter(imgs, vids, url)
        items: List[Dict] = []
        idx = 0
        for u in vids:
            idx += 1
            fname = Path(urlparse(u).path).name or f"{title}_{idx:02d}.mp4"
            items.append({"type": "video", "url": u, "filename": fname, "title": fname, "index": idx, "referer": url, "via_aria2": True})
        for u in imgs:
            idx += 1
            fname = Path(urlparse(u).path).name or f"{title}_{idx:02d}.jpg"
            items.append({"type": "image", "url": u, "filename": fname, "title": fname, "index": idx,
                         "ext": Path(fname).suffix.lower() or ".jpg", "referer": url, "via_aria2": True})
        return items

    def _scrape_fapeza(self, url: str) -> List[Dict]:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            return []
        slug = parts[0]
        base = f"{parsed.scheme}://{parsed.netloc}"
        album_root = f"{base}/{slug}/"
        try:
            root_html = self._get_session().get(album_root, timeout=(10, 60)).text
        except Exception:
            return []
        m = re.search(r'<span>\s*(\d+)\s*</span>\s*<span>\s*Posts\s*</span>', root_html, re.IGNORECASE)
        post_count = int(m.group(1)) if m else 0
        page_urls = [f"{base}/{slug}/{i}/" for i in range(1, post_count + 1)] if post_count else [album_root]
        all_imgs, all_vids = _extract_media_from_html(root_html)
        def fetch(pu):
            try:
                r = self._get_session().get(pu, timeout=(10, 45))
                if r.status_code == 200:
                    return r.text
            except Exception:
                return None
            return None
        with ThreadPoolExecutor(max_workers=min(20, max(8, self.workers))) as pool:
            for html in pool.map(fetch, page_urls):
                if not html:
                    continue
                i2, v2 = _extract_media_from_html(html)
                all_imgs.extend(i2)
                all_vids.extend(v2)
        all_imgs, all_vids = _post_filter(all_imgs, all_vids, url)
        items: List[Dict] = []
        idx = 0
        for u in all_vids:
            idx += 1
            fname = Path(urlparse(u).path).name or f"video_{idx:03d}.mp4"
            items.append({"type": "video", "url": u, "filename": fname, "title": fname, "index": idx, "referer": url, "via_aria2": False})
        for u in all_imgs:
            idx += 1
            fname = Path(urlparse(u).path).name or f"image_{idx:03d}.jpg"
            items.append({"type": "image", "url": u, "filename": fname, "title": fname, "index": idx,
                         "ext": Path(fname).suffix.lower() or ".jpg", "referer": url, "via_aria2": False})
        return items

    def _scrape_fapello(self, url: str) -> List[Dict]:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if not parts:
            return []
        model = parts[0]
        base = f"{parsed.scheme}://{parsed.netloc}"
        model_root = f"{base}/{model}/"
        try:
            html = self._get_session().get(model_root, timeout=(10, 60)).text
        except Exception:
            return []
        post_re = re.compile(rf'href="({re.escape(base)}/{re.escape(model)}/[a-z0-9][a-z0-9._-]*/)"', re.IGNORECASE)
        post_urls: Set[str] = set()
        for m in post_re.finditer(html):
            post_urls.add(m.group(1))
        page_nums: Set[int] = set()
        for m in re.finditer(rf'{re.escape(base)}/{re.escape(model)}/page-(\d+)/', html):
            page_nums.add(int(m.group(1)))
        max_page = min(max(page_nums) if page_nums else 1, 500)
        def fetch_page_html(page_num: int) -> Optional[str]:
            candidates = [
                f"{base}/ajax/model/{model}/page-{page_num}/",
                f"{base}/{model}/page-{page_num}/",
            ]
            for u in candidates:
                try:
                    r = self._get_session().get(u, timeout=(10, 30),
                        headers={"X-Requested-With": "XMLHttpRequest", "Referer": model_root})
                    if r.status_code != 200 or len(r.text) < 200:
                        continue
                    body = r.text
                    try:
                        j = json.loads(body)
                        if isinstance(j, dict):
                            for k in ("html", "content", "data", "result"):
                                if isinstance(j.get(k), str) and len(j[k]) > 200:
                                    return j[k]
                    except Exception:
                        pass
                    if post_re.search(body):
                        return body
                except Exception:
                    continue
            return None
        if max_page > 1:
            with ThreadPoolExecutor(max_workers=min(12, max(4, self.workers))) as pool:
                for frag in pool.map(fetch_page_html, range(2, max_page + 1)):
                    if not frag:
                        continue
                    for m in post_re.finditer(frag):
                        post_urls.add(m.group(1))
        all_imgs: List[str] = []
        all_vids: List[str] = []
        def fetch_post(pu: str):
            try:
                r = self._get_session().get(pu, timeout=(10, 30), headers={"Referer": model_root})
                if r.status_code == 200 and len(r.text) >= 400:
                    return r.text
            except Exception:
                pass
            return None
        with ThreadPoolExecutor(max_workers=min(20, max(8, self.workers))) as pool:
            for phtml in pool.map(fetch_post, list(post_urls)):
                if not phtml:
                    continue
                i2, v2 = _extract_media_from_html(phtml)
                all_imgs.extend(i2)
                all_vids.extend(v2)
        grid_imgs, grid_vids = _extract_media_from_html(html)
        all_imgs.extend(grid_imgs)
        all_vids.extend(grid_vids)
        all_imgs, all_vids = _post_filter(all_imgs, all_vids, url, model_filter=model)
        items: List[Dict] = []
        idx = 0
        for u in all_vids:
            idx += 1
            fname = Path(urlparse(u).path).name or f"video_{idx:03d}.mp4"
            items.append({"type": "video", "url": u, "filename": fname, "title": fname, "index": idx, "referer": model_root, "via_aria2": False, "url_fallbacks": []})
        for u in all_imgs:
            idx += 1
            fname = Path(urlparse(u).path).name or f"image_{idx:03d}.jpg"
            ext = Path(fname).suffix.lower() or ".jpg"
            items.append({"type": "image", "url": u, "filename": fname, "title": fname, "index": idx,
                         "ext": ext, "referer": model_root, "via_aria2": False, "url_fallbacks": []})
        return items

    def enumerate_items(self, url: str) -> List[Dict]:
        host = _host_key(url)
        fast_paths = [
            ("aznude.com", self._scrape_aznude),
            ("fapello.com", self._scrape_fapello),
            ("fapeza.com", self._scrape_fapeza),
            ("betterome.com", self._scrape_betterome),
            ("erome.com", self._scrape_erome),
        ]
        for needle, fn in fast_paths:
            if needle in host:
                items = fn(url)
                if items:
                    return items
                break
        if self._is_gallery_site(url):
            self._download_gallery(url)
            self._sort_gallery_output()
            return []
        try:
            with yt_dlp.YoutubeDL(self._ydl_opts({"skip_download": True})) as ydl:
                info = ydl.extract_info(url, download=False)
            if info:
                entries = info.get("entries") or [info]
                classified = [self._classify(e, i) for i, e in enumerate(entries, 1) if e]
                if classified:
                    return classified
        except Exception:
            pass
        return self._scrape_generic(url)

    def _classify(self, entry: Dict, idx: int) -> Dict:
        title = self._sanitize(entry.get("title") or entry.get("id") or f"item_{idx:02d}")
        formats = entry.get("formats") or []
        if next((f for f in formats if (f.get("vcodec") or "none") != "none"), None):
            return {"type": "video", "url": entry.get("webpage_url") or entry.get("url"),
                    "title": title, "index": idx, "entry": entry}
        thumb = entry.get("thumbnail") or entry.get("url")
        if not thumb and entry.get("thumbnails"):
            thumb = entry["thumbnails"][-1].get("url")
        ext = ".jpg"
        if thumb:
            pe = Path(urlparse(thumb).path).suffix.lower()
            if pe in IMAGE_EXTS:
                ext = pe
        return {"type": "image", "url": thumb, "title": title, "index": idx,
                "ext": ext, "referer": entry.get("webpage_url") or entry.get("url")}

    @staticmethod
    def _sanitize(name: str) -> str:
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
        return re.sub(r"\s+", " ", name).strip()[:180] or "untitled"

    def _target_path(self, item: Dict, folder: Path, ext: str) -> Path:
        fname = item.get("filename")
        if fname:
            return folder / fname
        return folder / f"{item['index']:02d} - {item['title']}{ext}"

    def _existing(self, item: Dict, folder: Path) -> Optional[Path]:
        fname = item.get("filename")
        if fname:
            p = folder / fname
            return p if p.exists() and p.stat().st_size > 0 else None
        hits = list(folder.glob(f"{item['index']:02d} - {item['title']}.*"))
        return hits[0] if hits and hits[0].stat().st_size > 0 else None

    def _download_video(self, item: Dict) -> Tuple[str, bool, str]:
        label = f"[video] {item['index']:02d} - {item['title'][:60]}"
        if self._existing(item, self.videos_dir):
            self.progress.begin(label); self.progress.finish(label, True, "(cached)")
            return ("video", True, "skip")
        self.progress.begin(label)
        t0 = time.time()
        url = item.get("url", "")
        if any(e in url.lower() for e in (".mp4", ".webm", ".mkv", ".flv", ".avi", ".mov")):
            ok, msg = self._stream_download(item, self.videos_dir, ".mp4", "video")
            self.progress.finish(label, ok, f"({time.time()-t0:.1f}s)")
            return ("video", ok, msg)
        target = self._target_path(item, self.videos_dir, ".mp4")
        referer = item.get("referer") or url
        origin = f"https://{urlparse(referer).netloc}" if referer else ""
        opts = self._ydl_opts({
            "outtmpl": str(target.with_suffix(".%(ext)s")),
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "http_headers": {"Referer": referer, "Origin": origin},
        })
        if ".m3u8" in url.lower():
            opts.pop("external_downloader", None)
            opts.pop("external_downloader_args", None)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            self.progress.finish(label, True, f"({time.time()-t0:.1f}s)")
            return ("video", True, "ok")
        except Exception as e:
            self.progress.finish(label, False, f"({e})")
            return ("video", False, str(e))

    def _download_image(self, item: Dict) -> Tuple[str, bool, str]:
        label = f"[image] {item['index']:02d} - {item['title'][:60]}"
        if self._existing(item, self.images_dir):
            self.progress.begin(label); self.progress.finish(label, True, "(cached)")
            return ("image", True, "skip")
        self.progress.begin(label)
        t0 = time.time()
        ok, msg = self._stream_download(item, self.images_dir, item.get("ext", ".jpg"), "image")
        self.progress.finish(label, ok, f"({time.time()-t0:.1f}s)")
        return ("image", ok, msg)

    def _stream_download(self, item: Dict, folder: Path, ext: str, kind: str) -> Tuple[bool, str]:
        url = item["url"]
        if not url:
            return (False, "no url")
        target = self._target_path(item, folder, ext)
        if target.exists() and target.stat().st_size > 0:
            return (True, f"skip {target.name}")
        candidates = [url] + list(item.get("url_fallbacks") or [])
        last_err = "no attempt"
        for idx_try, try_url in enumerate(candidates):
            if item.get("via_aria2", True) and self.aria2 and idx_try == 0:
                ok, msg = self._aria2_download(try_url, target, item.get("referer") or try_url)
                if ok:
                    return (True, msg)
            ok, msg = self._curl_download(try_url, target, item.get("referer") or try_url, item["title"])
            if ok:
                return (True, msg)
            last_err = msg
        return (False, last_err)

    def _aria2_download(self, url: str, target: Path, referer: str) -> Tuple[bool, str]:
        origin = f"https://{urlparse(referer).netloc or 'localhost'}"
        cmd = [self.aria2, url, "-d", str(target.parent), "-o", target.name,
            "-x", "16", "-s", "16", "-k", "1M", "--min-split-size", "1M",
            "--max-connection-per-server", "16", "--file-allocation", "none",
            "--console-log-level", "error", "--allow-overwrite", "false",
            "--auto-file-renaming", "false", "--retry-wait", "2", "--max-tries", "5",
            "--header", f"Referer: {referer}", "--header", f"Origin: {origin}",
            "--user-agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if target.exists() and target.stat().st_size > 0:
                return (True, f"ok ({target.stat().st_size/1048576:.1f}MB)")
            errs = [l.strip() for l in (proc.stderr or "").splitlines() if l.strip()]
            return (False, errs[0] if errs else f"rc={proc.returncode}")
        except Exception as e:
            return (False, str(e))

    def _curl_download(self, url: str, target: Path, referer: str, title: str) -> Tuple[bool, str]:
        origin = f"https://{urlparse(referer).netloc}" if referer else "https://www.erome.com"
        headers = {"Referer": referer, "Origin": origin,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
        tmp = target.with_suffix(target.suffix + ".part")
        last = None
        for attempt in range(2):
            try:
                r = self._get_session().get(url, headers=headers, stream=True, timeout=(10, 120))
                if r.status_code != 200:
                    last = f"HTTP {r.status_code}"
                    if r.status_code in (403, 404, 410):
                        return (False, last)
                    continue
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1048576):
                        if chunk:
                            f.write(chunk)
                tmp.rename(target)
                return (True, f"ok ({target.stat().st_size/1048576:.1f}MB)")
            except Exception as e:
                last = str(e)
                if tmp.exists():
                    try: tmp.unlink()
                    except Exception: pass
                time.sleep(1.2 * (attempt + 1))
        return (False, str(last))

    def _download_item(self, item: Dict) -> Tuple[str, bool, str]:
        return self._download_video(item) if item["type"] == "video" else self._download_image(item)

    def download(self, url: str) -> bool:
        print(f"[*] {url}")
        items = self.enumerate_items(url)
        if not items:
            if self._is_gallery_site(url):
                return True
            print("[!] nothing found")
            return False
        vids = sum(1 for i in items if i["type"] == "video")
        imgs = sum(1 for i in items if i["type"] == "image")
        print(f"[*] {len(items)} items ({vids}v / {imgs}i) → {self.workers} workers\n")
        self.progress.start(len(items))
        ok = fail = 0
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="dl") as pool:
            for fut in as_completed({pool.submit(self._download_item, it): it for it in items}):
                _, success, _ = fut.result()
                ok += success
                fail += not success
        print(f"\n[*] done: {ok} ok, {fail} failed in {time.time()-t0:.1f}s")
        return fail == 0


# ══════════════════════════════════════════════════════════════════════
#  LIBRARY API — used by the FastAPI backend
# ══════════════════════════════════════════════════════════════════════

def _simplify_formats(formats: List[Dict]) -> List[Dict]:
    """Reduce yt-dlp's exhaustive format list to a user-friendly set.

    FIX: yt-dlp's generic extractor leaves vcodec/acodec as None for direct
    media URLs (e.g. https://media.redgifs.com/xxx.mp4). Previously we
    treated None as "none" and skipped the format — now we assume a direct
    media file has both video+audio. This was the reason RedGifs (and any
    other CDN-hosted MP4) returned "No formats available".
    """
    bucket: Dict[Any, Dict] = {}
    for f in formats:
        vcodec_raw = f.get("vcodec")
        acodec_raw = f.get("acodec")

        if vcodec_raw is None and acodec_raw is None:
            # Both unknown — likely a direct media file.
            ext = (f.get("ext") or "").lower()
            if ext in ("mp4", "webm", "mkv", "mov", "flv", "avi", "m4v"):
                has_video = True
                has_audio = True
            else:
                continue
        else:
            has_video = (vcodec_raw or "none") != "none"
            has_audio = (acodec_raw or "none") != "none"

        if not has_video and not has_audio:
            continue
        if f.get("format_note") == "storyboard":
            continue

        if has_video:
            key = ("v", f.get("height") or 0, has_audio, f.get("ext"))
        else:
            key = ("a", f.get("abr") or 0, f.get("ext"))
        tbr = f.get("tbr") or 0
        if key not in bucket or (bucket[key].get("tbr") or 0) < tbr:
            enriched = dict(f)
            enriched["_has_video"] = has_video
            enriched["_has_audio"] = has_audio
            bucket[key] = enriched

    out = []
    for f in bucket.values():
        height = f.get("height")
        ext = f.get("ext") or "mp4"
        has_audio = f["_has_audio"]
        has_video = f["_has_video"]
        if has_video:
            res = f"{height}p" if height else (f.get("resolution") or "video")
        else:
            res = "audio only"
        out.append({
            "format_id": f.get("format_id"),
            "ext": ext,
            "resolution": res,
            "height": height,
            "width": f.get("width"),
            "fps": f.get("fps"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "has_video": has_video,
            "has_audio": has_audio,
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "tbr": f.get("tbr"),
            "abr": f.get("abr"),
            "note": f.get("format_note"),
        })

    out.sort(key=lambda f: (
        0 if f["has_video"] else 1,
        -(f.get("height") or 0),
        -(f.get("tbr") or 0),
    ))
    return out


def _guess_ext(url: str) -> str:
    path = urlparse(url).path.lower()
    if "." in path.rsplit("/", 1)[-1]:
        return path.rsplit(".", 1)[-1]
    return ""


def extract_media_formats(url: str,
                          cookies_file: Optional[str] = None,
                          proxy: Optional[str] = None,
                          referer: Optional[str] = None) -> Dict:
    """Return {ok, title, thumbnail, duration, formats: [...]} for a URL.

    FIX: When yt-dlp returns success but with an empty `formats` list (common
    for direct-media URLs), synthesize a single usable format from `info.url`.
    Also, if yt-dlp extraction raises but the URL looks like a direct media
    file, return a pseudo-format so the user can still download it.
    """
    opts: Dict[str, Any] = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "noplaylist": True, "socket_timeout": 30, "retries": 5,
    }
    if cookies_file and Path(cookies_file).exists():
        opts["cookiefile"] = str(cookies_file)
    if proxy:
        opts["proxy"] = proxy
    if referer:
        opts["http_headers"] = {"Referer": referer, "Origin": f"https://{urlparse(referer).netloc}"}

    title_guess = urlparse(url).path.rsplit("/", 1)[-1] or url

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        print(f"[extract] yt-dlp raised on {url[:100]}: {e}", flush=True)
        # If the URL looks like a direct media file, we can still try it.
        ext = _guess_ext(url)
        if ext in ("mp4", "webm", "mkv", "mov", "jpg", "jpeg", "png", "gif", "webp", "avif"):
            is_video = ext in ("mp4", "webm", "mkv", "mov")
            print(f"[extract] falling back to direct pseudo-format (ext={ext})", flush=True)
            return {
                "ok": True,
                "title": title_guess,
                "formats": [{
                    "format_id": "direct",
                    "ext": ext,
                    "resolution": "original",
                    "has_video": is_video,
                    "has_audio": is_video,
                    "filesize": None,
                    "note": "direct",
                }],
            }
        return {"ok": False, "error": str(e), "formats": []}

    if not info:
        return {"ok": False, "error": "no info returned", "formats": []}

    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0]

    raw_formats = info.get("formats") or []
    print(f"[extract] {url[:100]} → {len(raw_formats)} raw format(s), extractor={info.get('extractor_key') or info.get('extractor')}", flush=True)

    # Synthesize from info.url if the formats list is empty but a URL exists.
    if not raw_formats and info.get("url"):
        print(f"[extract] synthesizing format from info.url (ext={info.get('ext')})", flush=True)
        raw_formats = [{
            "url": info["url"],
            "format_id": info.get("format_id") or "direct",
            "ext": info.get("ext") or "mp4",
            "width": info.get("width"),
            "height": info.get("height"),
            "fps": info.get("fps"),
            "vcodec": info.get("vcodec"),
            "acodec": info.get("acodec"),
            "filesize": info.get("filesize") or info.get("filesize_approx"),
            "tbr": info.get("tbr"),
            "format_note": info.get("format_note"),
        }]

    formats = _simplify_formats(raw_formats)
    print(f"[extract] after simplification: {len(formats)} format(s)", flush=True)

    if not formats and info.get("url"):
        print(f"[extract] simplification ate everything; forcing direct pseudo-format", flush=True)
        ext = info.get("ext") or _guess_ext(url) or "mp4"
        is_video = ext in ("mp4", "webm", "mkv", "mov")
        formats = [{
            "format_id": "direct",
            "ext": ext,
            "resolution": "original",
            "height": info.get("height"),
            "has_video": is_video,
            "has_audio": is_video,
            "filesize": info.get("filesize"),
            "note": "direct",
        }]

    return {
        "ok": True,
        "title": info.get("title") or title_guess,
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "webpage_url": info.get("webpage_url") or url,
        "formats": formats,
    }


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:150] or "download"


def download_with_progress(url: str,
                           format_id: Optional[str],
                           output_dir: str,
                           filename_base: str,
                           cookies_file: Optional[str] = None,
                           proxy: Optional[str] = None,
                           referer: Optional[str] = None,
                           progress_cb: Optional[Callable[[Dict], None]] = None,
                           cancel_event: Optional[threading.Event] = None) -> Dict:
    """Download a URL via yt-dlp with progress callbacks. Returns result dict."""
    outdir = Path(output_dir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    filename_base = _sanitize_filename(filename_base)

    def _hook(d: Dict):
        if cancel_event is not None and cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled("cancelled by user")
        if not progress_cb:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            pct = (downloaded / total * 100.0) if total else 0.0
            progress_cb({
                "status": "downloading",
                "downloaded_bytes": downloaded,
                "total_bytes": total,
                "percent": pct,
                "speed": d.get("speed"),
                "eta": d.get("eta"),
                "filename": d.get("filename") or "",
            })
        elif status == "finished":
            progress_cb({
                "status": "processing",
                "downloaded_bytes": d.get("downloaded_bytes") or 0,
                "total_bytes": d.get("total_bytes") or 0,
                "percent": 100.0,
                "filename": d.get("filename") or "",
            })

    # "direct" or empty means we don't have a real yt-dlp format_id —
    # use the safe default merge spec.
    if format_id in (None, "", "direct"):
        format_spec = "bestvideo+bestaudio/best"
    else:
        format_spec = format_id

    outtmpl = str(outdir / (filename_base + ".%(ext)s"))
    opts: Dict[str, Any] = {
        "outtmpl": outtmpl,
        "format": format_spec,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "progress_hooks": [_hook],
        "quiet": True,
        "no_warnings": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 16,
        "socket_timeout": 30,
        "continuedl": True,
        "overwrites": False,
    }
    if cookies_file and Path(cookies_file).exists():
        opts["cookiefile"] = str(cookies_file)
    if proxy:
        opts["proxy"] = proxy
    if referer:
        opts["http_headers"] = {"Referer": referer, "Origin": f"https://{urlparse(referer).netloc}"}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        path = ""
        if info:
            for d in info.get("requested_downloads") or []:
                if d.get("filepath"):
                    path = d["filepath"]
                    break
            if not path:
                path = ydl.prepare_filename(info)
        return {"ok": True, "path": str(path)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def download_direct_file(url: str,
                         output_dir: str,
                         filename_base: str,
                         extension: str = "",
                         referer: Optional[str] = None,
                         progress_cb: Optional[Callable[[Dict], None]] = None,
                         cancel_event: Optional[threading.Event] = None) -> Dict:
    """Stream-download a direct file (image, mp4, etc.) with progress."""
    outdir = Path(output_dir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    filename_base = _sanitize_filename(filename_base)

    if not extension:
        extension = _guess_ext(url)
    if extension and not extension.startswith("."):
        extension = "." + extension

    target = outdir / (filename_base + extension)
    n = 1
    original = target
    while target.exists():
        target = original.with_name(f"{original.stem}_{n}{original.suffix}")
        n += 1

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
        try:
            headers["Origin"] = f"https://{urlparse(referer).netloc}"
        except Exception:
            pass

    tmp = target.with_suffix(target.suffix + ".part")

    def _write_bytes(content_bytes: bytes) -> None:
        total = len(content_bytes)
        with open(tmp, "wb") as f:
            for i in range(0, total, 65536):
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("cancelled by user")
                f.write(content_bytes[i:i + 65536])
                if progress_cb:
                    done = min(i + 65536, total)
                    progress_cb({
                        "status": "downloading",
                        "downloaded_bytes": done,
                        "total_bytes": total,
                        "percent": (done / total * 100.0) if total else 0.0,
                    })

    err1 = None
    try:
        session = cffi_requests.Session(impersonate="chrome124")
        r = session.get(url, headers=headers, timeout=(10, 180))
        try:
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}"}
            ct = (r.headers.get("Content-Type") or "").lower()
            if "text/html" in ct and not url.lower().endswith((".html", ".htm")):
                return {"ok": False,
                        "error": f"got HTML instead of media (likely blocked) — {ct}"}
            content = r.content
            if not isinstance(content, (bytes, bytearray)) or len(content) == 0:
                return {"ok": False, "error": f"empty/invalid body (type={type(content).__name__})"}
            _write_bytes(bytes(content))
            tmp.rename(target)
            return {"ok": True, "path": str(target)}
        finally:
            try: r.close()
            except Exception: pass
    except Exception as e:
        err1 = str(e)
        try:
            if tmp.exists(): tmp.unlink()
        except Exception: pass

    try:
        import urllib.request
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=180) as resp:
            content = resp.read()
            _write_bytes(content)
        tmp.rename(target)
        return {"ok": True, "path": str(target)}
    except Exception as e2:
        try:
            if tmp.exists(): tmp.unlink()
        except Exception: pass
        return {"ok": False, "error": f"curl_cffi: {err1} | urllib: {e2}"}


def main():
    p = argparse.ArgumentParser(usage="python3 universal.py <url> [-o DIR] [-c COOKIES] [-p PROXY] [-j N] [-f N]")
    p.add_argument("url")
    p.add_argument("-o", "--output", default="./downloads")
    p.add_argument("-c", "--cookies")
    p.add_argument("-p", "--proxy")
    p.add_argument("-j", "--workers", type=int, default=16)
    p.add_argument("-f", "--fragments", type=int, default=24)
    args = p.parse_args()
    ok = UniversalDownloader(args.output, args.cookies, args.proxy, args.workers, args.fragments).download(args.url)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
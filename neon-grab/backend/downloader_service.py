"""Bridges JobManager <-> universal.py for downloads and format extraction."""
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import urlparse

from . import database
from .job_manager import JobManager

# Import the downloader library
import sys
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
import universal  # noqa: E402


VIDEO_EXTS = universal.VIDEO_EXTS
IMAGE_EXTS = universal.IMAGE_EXTS


class DownloaderService:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.job_manager = JobManager(max_workers=int(config.get("downloads", {}).get("max_concurrent", 3)))

    # ── formats ────────────────────────────────────────────────────

    def extract_formats(self, url: str, media_type: str, page_url: Optional[str],
                        referer: Optional[str]) -> Dict:
        print(f"[formats] request url={url[:100]!r} media_type={media_type} "
              f"page_url={(page_url or '')[:100]!r}", flush=True)

        if media_type == "image" or self._is_direct_image(url):
            return {
                "ok": True,
                "title": self._filename_from_url(url),
                "thumbnail": url,
                "formats": [{
                    "format_id": "direct",
                    "ext": self._ext_from_url(url) or "jpg",
                    "resolution": "original",
                    "has_video": False,
                    "has_audio": False,
                    "filesize": None,
                    "note": "direct",
                }],
            }

        cfg = self.config.get("engine", {})
        cookies = cfg.get("cookies_file")
        proxy = cfg.get("proxy")
        ref = referer or page_url

        # blob: / non-http URLs → use the page URL
        effective_url = url
        if url.startswith("blob:") or not url.startswith(("http://", "https://")):
            if not page_url:
                return {"ok": False, "error": "blob URL with no page URL — cannot resolve", "formats": []}
            effective_url = page_url
            print(f"[formats] blob URL → using page_url {effective_url[:100]}", flush=True)

        result = universal.extract_media_formats(effective_url, cookies_file=cookies,
                                                 proxy=proxy, referer=ref)

        # If URL-based extraction failed and we have a different page URL, retry on page
        if (not result.get("ok") or not result.get("formats")) and \
                page_url and page_url != effective_url:
            print(f"[formats] retrying on page_url {page_url[:100]}", flush=True)
            result2 = universal.extract_media_formats(page_url, cookies_file=cookies,
                                                      proxy=proxy, referer=ref)
            if result2.get("ok") and result2.get("formats"):
                result = result2

        print(f"[formats] → ok={result.get('ok')} "
              f"formats={len(result.get('formats') or [])} "
              f"error={result.get('error')!r}", flush=True)
        return result

    # ── downloads ──────────────────────────────────────────────────

    def start_download(self, url: str, media_type: str, page_url: Optional[str],
                       format_id: Optional[str], title: Optional[str],
                       referer: Optional[str]) -> Dict:
        job_id = uuid.uuid4().hex
        filename = self._filename_from_url(url)
        stem = Path(filename).stem or (title or "download")
        safe_title = (title or stem)[:200]

        database.create_job(
            job_id=job_id, url=url, page_url=page_url, media_type=media_type,
            format_id=format_id, title=safe_title, filename=filename,
            referer=referer or page_url,
        )
        print(f"[job {job_id[:8]}] queued: type={media_type} fmt={format_id!r} "
              f"url={url[:100]}", flush=True)
        self.job_manager.start(job_id, self._worker)
        return {"job_id": job_id}

    def cancel_download(self, job_id: str) -> bool:
        return self.job_manager.cancel(job_id)

    def _worker(self, job_id: str, cancel_event: threading.Event) -> Dict:
        job = database.get_job(job_id)
        if not job:
            return {"ok": False, "error": "job vanished"}

        cfg = self.config.get("engine", {})
        output_dir = self.config.get("downloads", {}).get("output_dir") or "./downloads"
        cookies = cfg.get("cookies_file")
        proxy = cfg.get("proxy")
        referer = job.get("referer") or job.get("page_url")

        stem = Path(job.get("filename") or "download").stem or (job.get("title") or "download")
        stem = "".join(c for c in stem if c not in '<>:"/\\|?*\x00-\x1f').strip() or "download"

        def _progress_cb(data: Dict):
            try:
                pct = float(data.get("percent") or 0)
                database.update_job(
                    job_id,
                    progress=pct,
                    downloaded_bytes=int(data.get("downloaded_bytes") or 0),
                    total_bytes=int(data.get("total_bytes") or 0),
                    speed=float(data.get("speed") or 0),
                    eta=int(data["eta"]) if data.get("eta") is not None else None,
                )
            except Exception as e:
                print(f"[job {job_id[:8]}] progress update failed: {e}", flush=True)

        raw_url = job["url"]
        page_url = job.get("page_url")
        fmt = job.get("format_id") or ""

        print(f"[job {job_id[:8]}] worker started url={raw_url[:100]!r} fmt={fmt!r}", flush=True)

        try:
            # Determine effective URL for yt-dlp — blob: URLs can't be fetched directly.
            if raw_url.startswith("blob:") or not raw_url.startswith(("http://", "https://")):
                if not page_url:
                    return {"ok": False, "error": "blob URL with no page URL — cannot resolve"}
                effective_url = page_url
            else:
                effective_url = raw_url

            # Images → direct download
            if job.get("media_type") == "image" or self._is_direct_image(raw_url):
                ext = self._ext_from_url(raw_url) or "jpg"
                result = universal.download_direct_file(
                    url=raw_url,
                    output_dir=output_dir,
                    filename_base=stem,
                    extension=ext,
                    referer=referer,
                    progress_cb=_progress_cb,
                    cancel_event=cancel_event,
                )
            elif fmt == "direct" and raw_url.startswith(("http://", "https://")):
                # Pseudo-format from synthesized direct URL — bypass yt-dlp.
                ext = self._ext_from_url(raw_url) or "mp4"
                result = universal.download_direct_file(
                    url=raw_url,
                    output_dir=output_dir,
                    filename_base=stem,
                    extension=ext,
                    referer=referer,
                    progress_cb=_progress_cb,
                    cancel_event=cancel_event,
                )
            else:
                # Video via yt-dlp. If fmt is "direct" but the URL is a page
                # (blob fallback), switch to the default merge spec.
                effective_fmt = fmt if fmt and fmt != "direct" else "bestvideo+bestaudio/best"
                result = universal.download_with_progress(
                    url=effective_url,
                    format_id=effective_fmt,
                    output_dir=output_dir,
                    filename_base=stem,
                    cookies_file=cookies,
                    proxy=proxy,
                    referer=referer,
                    progress_cb=_progress_cb,
                    cancel_event=cancel_event,
                )
        except Exception as e:
            err = f"worker exception: {e}"
            print(f"[job {job_id[:8]}] {err}", flush=True)
            traceback.print_exc()
            return {"ok": False, "error": err}

        if not result:
            result = {"ok": False, "error": "worker returned no result"}

        if not result.get("ok"):
            err = result.get("error") or "unknown error"
            print(f"[job {job_id[:8]}] FAILED: {err}", flush=True)
        else:
            print(f"[job {job_id[:8]}] OK → {result.get('path')}", flush=True)

        return result

    # ── helpers ────────────────────────────────────────────────────

    @staticmethod
    def _is_direct_image(url: str) -> bool:
        path = urlparse(url).path.lower()
        return any(path.endswith(e) for e in IMAGE_EXTS)

    @staticmethod
    def _ext_from_url(url: str) -> str:
        path = urlparse(url).path
        if "." in path.rsplit("/", 1)[-1]:
            return path.rsplit(".", 1)[-1].lower()
        return ""

    @staticmethod
    def _filename_from_url(url: str) -> str:
        path = urlparse(url).path
        name = Path(path).name
        return name or "download"

    def shutdown(self):
        self.job_manager.shutdown()
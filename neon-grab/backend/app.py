"""NEON GRAB backend — FastAPI server bound to 127.0.0.1 with token auth."""
import os
import sys
import json
import time
import secrets
import subprocess
import threading
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
IS_WINDOWS = os.name == "nt"
VENV_PYTHON = VENV_DIR / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
CONFIG_DIR = Path.home() / ".neon-grab"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_PATH = CONFIG_DIR / "token"
CONFIG_PATH = CONFIG_DIR / "config.json"


# ── venv bootstrap (mirror of universal.py) ────────────────────────────

def _in_venv() -> bool:
    try:
        return sys.prefix != sys.base_prefix and Path(sys.executable).resolve() == VENV_PYTHON.resolve()
    except Exception:
        return False


def _deps_ok() -> bool:
    try:
        import fastapi, uvicorn, yt_dlp, curl_cffi, playwright  # noqa
        return True
    except ImportError:
        return False


def _bootstrap():
    if not VENV_PYTHON.exists():
        print("[*] creating virtual environment (one-time)...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
    if not _deps_ok():
        print("[*] installing backend dependencies (one-time)...")
        subprocess.run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip", "-q"], check=True)
        subprocess.run([str(VENV_PYTHON), "-m", "pip", "install", "-q",
                        "-r", str(BACKEND_DIR / "requirements.txt")], check=True)
        print("[*] installing playwright chromium (one-time)...")
        subprocess.run([str(VENV_PYTHON), "-m", "playwright", "install", "chromium"], check=True)
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), __file__] + sys.argv[1:])


if not _in_venv() or not _deps_ok():
    _bootstrap()


# ── now safe to import ─────────────────────────────────────────────────

from fastapi import FastAPI, Header, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
import uvicorn

# Make sure backend package imports resolve
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import database                # noqa: E402
from backend.downloader_service import DownloaderService  # noqa: E402


# ── config + token ─────────────────────────────────────────────────────

def load_config() -> dict:
    defaults = {
        "backend": {"host": "127.0.0.1", "port": 8765},
        "downloads": {
            "output_dir": str(Path.home() / "Downloads" / "NeonGrab"),
            "max_concurrent": 3,
        },
        "engine": {"cookies_file": None, "proxy": None, "workers": 16, "fragments": 24},
    }
    if CONFIG_PATH.exists():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text())
            # shallow merge
            for k, v in user_cfg.items():
                if isinstance(v, dict) and isinstance(defaults.get(k), dict):
                    defaults[k].update(v)
                else:
                    defaults[k] = v
        except Exception as e:
            print(f"[config] failed to load: {e}")
    else:
        CONFIG_PATH.write_text(json.dumps(defaults, indent=2))
    return defaults


def load_or_create_token() -> str:
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text().strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token + "\n")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except Exception:
        pass
    return token


CONFIG = load_config()
API_TOKEN = load_or_create_token()

# Ensure download directory exists
Path(CONFIG["downloads"]["output_dir"]).expanduser().mkdir(parents=True, exist_ok=True)

# Init DB and service
database.init_db()
service = DownloaderService(CONFIG)


# ── FastAPI app ────────────────────────────────────────────────────────

app = FastAPI(title="NEON GRAB", version="1.0.0", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(chrome-extension|moz-extension|ms-browser-extension)://.*$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_token(authorization: Optional[str] = Header(None)) -> bool:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    provided = authorization[7:].strip()
    if not secrets.compare_digest(provided, API_TOKEN):
        raise HTTPException(403, "Invalid token")
    return True


Auth = Depends(verify_token)


# ── schemas ────────────────────────────────────────────────────────────

class FormatRequest(BaseModel):
    url: str = Field(..., min_length=1)
    media_type: str = Field("video")
    page_url: Optional[str] = None
    referer: Optional[str] = None

    @field_validator("url")
    @classmethod
    def _url_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("url required")
        return v


class DownloadRequest(BaseModel):
    url: str = Field(..., min_length=1)
    media_type: str = Field("video")
    page_url: Optional[str] = None
    format_id: Optional[str] = None
    title: Optional[str] = None
    referer: Optional[str] = None

    @field_validator("url")
    @classmethod
    def _url_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("url required")
        # Allow blob: because it's handled via page_url fallback
        if not v.startswith(("http://", "https://", "blob:")):
            raise ValueError("invalid url scheme")
        return v


class ConfigPatch(BaseModel):
    output_dir: Optional[str] = None
    max_concurrent: Optional[int] = Field(None, ge=1, le=16)
    cookies_file: Optional[str] = None
    proxy: Optional[str] = None


# ── endpoints ──────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {
        "ok": True,
        "service": "neon-grab",
        "version": "1.0.0",
        "auth_required": True,
        "time": time.time(),
    }


@app.post("/api/formats")
def formats(req: FormatRequest, _: bool = Auth):
    try:
        return service.extract_formats(req.url, req.media_type, req.page_url, req.referer)
    except Exception as e:
        return {"ok": False, "error": str(e), "formats": []}


@app.post("/api/downloads")
def create_download(req: DownloadRequest, _: bool = Auth):
    try:
        result = service.start_download(
            url=req.url, media_type=req.media_type, page_url=req.page_url,
            format_id=req.format_id, title=req.title, referer=req.referer,
        )
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/downloads")
def list_downloads(_: bool = Auth):
    jobs = database.list_jobs(include_finished=True, limit=500)
    return {"ok": True, "jobs": jobs}


@app.get("/api/downloads/{job_id}")
def get_download(job_id: str, _: bool = Auth):
    job = database.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return {"ok": True, "job": job}


@app.post("/api/downloads/{job_id}/cancel")
def cancel_download(job_id: str, _: bool = Auth):
    job = database.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job["status"] not in ("queued", "running"):
        return {"ok": False, "error": f"job is {job['status']}, cannot cancel"}
    ok = service.cancel_download(job_id)
    return {"ok": ok}


@app.delete("/api/history")
def clear_history(_: bool = Auth):
    try:
        removed = database.clear_history()
        return {"ok": True, "removed": removed}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/config")
def get_config(_: bool = Auth):
    return {
        "ok": True,
        "config": {
            "output_dir": CONFIG["downloads"]["output_dir"],
            "max_concurrent": CONFIG["downloads"]["max_concurrent"],
            "cookies_file": CONFIG["engine"].get("cookies_file"),
            "proxy": CONFIG["engine"].get("proxy"),
        },
    }


@app.patch("/api/config")
def patch_config(patch: ConfigPatch, _: bool = Auth):
    if patch.output_dir is not None:
        CONFIG["downloads"]["output_dir"] = patch.output_dir
        Path(patch.output_dir).expanduser().mkdir(parents=True, exist_ok=True)
    if patch.max_concurrent is not None:
        CONFIG["downloads"]["max_concurrent"] = patch.max_concurrent
    if patch.cookies_file is not None:
        CONFIG["engine"]["cookies_file"] = patch.cookies_file or None
    if patch.proxy is not None:
        CONFIG["engine"]["proxy"] = patch.proxy or None
    CONFIG_PATH.write_text(json.dumps(CONFIG, indent=2))
    return {"ok": True}


@app.on_event("shutdown")
def _shutdown():
    try:
        service.shutdown()
    except Exception:
        pass


# ── startup banner ─────────────────────────────────────────────────────

def _banner():
    print()
    print("═" * 62)
    print("  NEON GRAB BACKEND")
    print("═" * 62)
    print(f"  Listening:   http://{CONFIG['backend']['host']}:{CONFIG['backend']['port']}")
    print(f"  Output dir:  {CONFIG['downloads']['output_dir']}")
    print(f"  Auth token:  {API_TOKEN}")
    print(f"  Token file:  {TOKEN_PATH}")
    print("─" * 62)
    print("  Paste the token into the extension's Settings tab.")
    print("═" * 62)
    print()


if __name__ == "__main__":
    _banner()
    uvicorn.run(
        app,
        host=CONFIG["backend"]["host"],
        port=int(CONFIG["backend"]["port"]),
        log_level="info",
    )
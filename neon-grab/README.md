# NEON GRAB

Universal browser media downloader. Chrome / Edge extension + local Python backend.
Backend must run on your machine at `127.0.0.1:8765` — no cloud, no subscriptions.

```
neon-grab/
├── universal.py
├── config.example.json
├── README.md
├── backend/
│   ├── app.py
│   ├── database.py
│   ├── downloader_service.py
│   ├── job_manager.py
│   └── requirements.txt
└── extension/
    ├── manifest.json
    ├── storage.js
    ├── background.js
    ├── media-detector.js
    ├── content.js
    ├── popup.html
    ├── popup.css
    ├── popup.js
    └── icons/icon.svg
```

---

## 1. One-time setup

### Prerequisites
- Python 3.10+
- Chrome / Edge (recent version)
- **ffmpeg** on `PATH` (needed to merge separate video + audio streams)
- **aria2c** recommended (multi-connection downloads) — `sudo apt install aria2` / `brew install aria2`

### Install backend (first run only)

```bash
cd neon-grab
python backend/app.py
```

The first launch will:
1. Create `.venv/` and install `fastapi`, `uvicorn`, `yt-dlp`, `gallery-dl`, `curl_cffi`, `playwright`.
2. Install Playwright's bundled Chromium.
3. Start the server on `127.0.0.1:8765`.

The startup banner prints your **API token** and its on-disk location (`~/.neon-grab/token`).

Subsequent launches are fast — just `python backend/app.py`.

### Load the extension

1. Open `chrome://extensions` (or `edge://extensions`).
2. Enable **Developer mode**.
3. Click **Load unpacked** → select the `neon-grab/extension/` folder.
4. Click the NEON GRAB icon → open the **Settings** tab.
5. Paste the **API token** into the *API Token* field → **Save Settings**.
6. Click **Test Connection** — the status dot should turn green.

---

## 2. Usage

1. In the popup, flip the **enable toggle**. (Reload the page if you already had it open — content scripts only inject on page load.)
2. Reload the target page.
3. Detected video elements get a neon-green outline with a floating `DOWNLOAD` button.
   Images glow on hover with a `DOWNLOAD` button.
4. Click `DOWNLOAD` — a format picker appears (real formats from the backend — resolutions, container, size).
5. Choose a format, click **Download**.
6. Close the popup — the download continues (it runs on the backend, not the extension).
7. Reopen the popup to see live progress.
8. Completed jobs stay in **History** until you click **Clear**.

---

## 3. Architecture

- **`universal.py`** — the download engine. When executed as a script it bootstraps its venv (unchanged CLI). When imported as a library it exposes:
  - `extract_media_formats(url, cookies_file, proxy, referer)` → yt-dlp format metadata
  - `download_with_progress(url, format_id, output_dir, ...)` → yt-dlp + progress callbacks
  - `download_direct_file(url, output_dir, ...)` → stream download with progress + cancellation
- **`backend/app.py`** — FastAPI server, binds to `127.0.0.1:8765`, requires `Authorization: Bearer <token>` on all endpoints except `/api/health`. CORS is limited to browser-extension origins only.
- **`backend/job_manager.py`** — ThreadPoolExecutor with per-job `threading.Event` cancellation.
- **`backend/database.py`** — SQLite (`~/.neon-grab/neon-grab.db`). WAL mode.
- **`backend/downloader_service.py`** — glue layer between jobs and `universal.py`.
- **`extension/background.js`** — single point of API access; content scripts and popup message it (never talk to backend directly).
- **`extension/content.js`** — Shadow DOM overlays, hover interactions, in-page format modal.
- **`extension/popup.*`** — status, media list, download manager, history, settings.

---

## 4. API

All endpoints except `/api/health` require `Authorization: Bearer <token>`.

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/api/health` | Liveness check (public) |
| `POST` | `/api/formats` | Extract available formats for a media/page URL |
| `POST` | `/api/downloads` | Queue a download |
| `GET`  | `/api/downloads` | List jobs (newest first) |
| `GET`  | `/api/downloads/{id}` | Get one job |
| `POST` | `/api/downloads/{id}/cancel` | Cancel a running job |
| `DELETE` | `/api/history` | Clear finished/failed/cancelled jobs (active jobs untouched; files untouched) |
| `GET`  | `/api/config` | Get current backend settings |
| `PATCH` | `/api/config` | Update `output_dir`, `max_concurrent`, `cookies_file`, `proxy` |

---

## 5. Troubleshooting

**Backend offline in popup**
- Confirm the server is running (`python backend/app.py`).
- Check `http://127.0.0.1:8765/api/health` in a browser tab — should return `{"ok":true}`.
- Token mismatch → copy from `~/.neon-grab/token` and re-paste in Settings.

**"No formats available" on a video**
- The site is DRM-protected or requires authentication. Export cookies (see below) or use a different source.
- Some `blob:` videos need the page URL — NEON GRAB falls back automatically, but some players use encrypted MediaSource Extensions which cannot be extracted.

**Missing ffmpeg / merge fails**
- Install `ffmpeg` (`apt install ffmpeg` / `brew install ffmpeg`). Restart the backend.

**Cookies / auth-gated content**
- NEON GRAB does *not* transmit your browser cookies. To download from sites that require login, export a Netscape-format `cookies.txt` (e.g. with the "Get cookies.txt" extension) and set `engine.cookies_file` in `~/.neon-grab/config.json`, then restart the backend.

**Icons don't show on some Chrome versions**
- Recent Chrome (116+) and Edge support SVG icons. For older versions, convert `icons/icon.svg` to PNGs (16/32/48/128) and update `manifest.json`.

---

## 6. CLI still works

```bash
python universal.py "https://example.com/album" -o ./downloads -j 16
```

The bootstrap, deps, cookies, proxy, aria2, Playwright and per-site fast paths are all unchanged. The library API was appended below `UniversalDownloader` and does not affect the CLI.

---

## 7. Security notes

- Backend binds to `127.0.0.1` only.
- Every non-health request requires the bearer token; tokens are compared with `secrets.compare_digest`.
- CORS is restricted to `chrome-extension://…`, `moz-extension://…`, `ms-browser-extension://…`. Regular web pages cannot reach the API.
- Filenames are sanitized before writing.
- Browser cookies / session state are **never** sent to any remote service.
- Only download content you are authorised to save.
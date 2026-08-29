"""ComfyBlockout-app sidecar — serves the editor, persists scenes/assets/recordings,
and runs cloud generations via `comfy generate <model> ...` modules.

No ComfyUI in the loop. Auth lives in comfy-cli (OAuth via `comfy cloud login`,
or COMFY_API_KEY env var as a fallback)."""

from __future__ import annotations

import asyncio
import httpx
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# Windows-only: uvicorn's --reload mode picks SelectorEventLoop, which raises
# NotImplementedError the moment `asyncio.create_subprocess_exec` runs. Every
# git-clone / pip-install / restart-comfy call would crash. Force the Proactor
# policy at import time so the loop uvicorn spawns actually supports
# subprocesses. No-op on macOS/Linux.
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        # Fall through — old Python versions or unusual runtimes will error
        # on the specific tool calls instead of at import time, which is fine.
        pass

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------- paths ----------

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "output"
WEB_DIR = APP_DIR / "web"
ENV_PATH = APP_DIR / ".env"
DATA_DIR.mkdir(parents=True, exist_ok=True)
# Type-partitioned output folders — created on first run so a fresh install
# has a browsable structure. Renders route into these by kind (images/videos/3d);
# existing flat files in output/ root stay put for backwards compat.
for _sub in ("images", "videos", "3d"):
    (DATA_DIR / _sub).mkdir(parents=True, exist_ok=True)


def _load_env_file(*, override: bool = True) -> bool:
    """Read .env at the project root into os.environ. Called at startup and on
    every /api/auth/status hit so editing .env doesn't require a server restart."""
    if not ENV_PATH.exists():
        return False
    loaded_any = False
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(ENV_PATH, override=override)
        return True
    except Exception:
        pass
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if not k:
            continue
        if override or k not in os.environ:
            os.environ[k] = v
            loaded_any = True
    return loaded_any


_load_env_file()
# Backfill COMFY_CLOUD_API_KEY from COMFY_API_KEY for users whose .env predates
# the split. `comfy run --where cloud` reads the _CLOUD_ variant; `comfy generate`
# reads the plain one. Keeping both aligned means one key covers both paths.
if os.environ.get("COMFY_API_KEY") and not os.environ.get("COMFY_CLOUD_API_KEY"):
    os.environ["COMFY_CLOUD_API_KEY"] = os.environ["COMFY_API_KEY"]


def _read_env_kv() -> dict[str, str]:
    """Parse the current .env into a dict so we can update one key without
    blowing away the others."""
    out: dict[str, str] = {}
    if not ENV_PATH.exists():
        return out
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if not k:
            continue
        out[k] = v.strip().strip('"').strip("'")
    return out


def _write_env_kv(kv: dict[str, str]) -> None:
    """Persist the kv dict back to .env, preserving order of keys we set."""
    lines = [f"{k}={v}" for k, v in kv.items() if v]
    ENV_PATH.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _resolve_comfy_bin() -> str | None:
    """Find the comfy console-script even when subprocess PATH doesn't match the
    activated venv. Prefer the venv's Scripts dir, then PATH, then sys.executable
    sibling."""
    import sys
    candidates = []
    venv_scripts = Path(sys.executable).parent  # .venv\Scripts on Windows
    for name in ("comfy.exe", "comfy.cmd", "comfy.bat", "comfy"):
        candidates.append(venv_scripts / name)
    for c in candidates:
        if c.exists():
            return str(c)
    found = shutil.which("comfy")
    return found


COMFY_BIN: str | None = _resolve_comfy_bin()
if COMFY_BIN:
    print(f"[cb-app] resolved comfy CLI: {COMFY_BIN}")
else:
    print("[cb-app] comfy CLI not resolved — generations will fail until comfy-cli is on PATH")

# Immutable base system prompt — fires on every generation, NOT editable from the
# UI. The editor's "Prompt Tweaks" textarea is appended on top of this, so users
# can add scene-specific guidance without ever losing the spatial-ControlNet base.
BASE_PROMPT = (
    "COMPOSITION-GUIDED IMAGE GENERATION.\n\n"
    "**ASPECT RATIO (locked).** Output width:height matches image 1 exactly. "
    "Landscape blockout → landscape output; portrait → portrait; square → square. "
    "No cropping, padding, or reshape.\n\n"
    "**CAMERA (locked).** Match image 1's camera angle, perspective, lens "
    "compression, framing, horizon line, and vanishing points EXACTLY. Do not "
    "reframe, re-tilt, dolly, pan, or change FOV. The camera is fixed by image 1 "
    "regardless of any other setting below.\n\n"
    "**IMAGE 1 IS A 3D BLOCKOUT.** Low-fidelity scene of primitive shapes with "
    "flat tint colors — a wireframe used to plan the shot. It defines the "
    "composition and camera. It does NOT define the look. Ignore its flat "
    "colors, primitive silhouettes, checker patterns, and grid overlays in the "
    "output — those are scaffolding.\n\n"
    "**ADDITIONAL IMAGES (image 2+) ARE MATERIAL SWATCHES.** Pull ONLY surface "
    "qualities from them: color, texture, finish, pattern, micro-detail. IGNORE "
    "their framing, scale, subjects, lighting, and any other content — treat each "
    "as a flat material chip from a sample book. The SCENE INVENTORY below maps "
    "each swatch to a named object in image 1.\n\n"
    "**YOUR JOB.** Produce a single unified image (one photograph or one painting, "
    "not a composite) that preserves image 1's camera and composition exactly, "
    "but replaces each colored blockout shape with the subject described in the "
    "user prompt, surfaced with the matching swatch material. Lighting, "
    "environment, mood, weather, and background are invented from the user "
    "prompt. The output must read as physically-plausible: consistent lighting, "
    "matching color temperature, correct contact shadows, no visible seams or "
    "'pasted-on' edges."
)
DEFAULT_PROMPT = BASE_PROMPT  # back-compat alias for any old references

# ---------- ffmpeg ----------

def _resolve_ffmpeg() -> str | None:
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if r.returncode == 0:
            return "ffmpeg"
    except Exception:
        pass
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        r = subprocess.run([exe, "-version"], capture_output=True, timeout=5)
        if r.returncode == 0:
            print(f"[cb-app] using bundled ffmpeg: {exe}")
            return exe
    except Exception:
        pass
    return None


FFMPEG_BIN = _resolve_ffmpeg()
if not FFMPEG_BIN:
    print("[cb-app] ffmpeg not found — recordings will stay as .webm")

# ---------- in-memory stores (mirrored to disk) ----------

_video_store: dict[str, dict] = {}
_image_store: dict[str, dict] = {}
# Per-node run status — keyed by node_id, holds the current phase of an
# in-flight generation ("submitting" | "generating" | "fetching" |
# "downloaded" | "cloud_done_no_download" | "failed"). Modules that want to
# report progress accept a `status_cb` kwarg and call it with a phase string;
# the /api/run/status/{node_id} endpoint reads from here. Only the LATEST
# run's status is retained (a new run overwrites, terminal states expire
# once the frontend has seen them).
_run_status: dict[str, dict] = {}
_scene_store: dict[str, Any] = {}
_prompt_store: dict[str, str] = {}


def _hydrate_stores() -> None:
    """Re-populate the stores from existing files in DATA_DIR so a restart
    doesn't lose track of scenes/snapshots already on disk."""
    for f in DATA_DIR.iterdir():
        if not f.is_file():
            continue
        name = f.name
        m = re.match(r"^node_(.+?)_image(\..+)?$", name)
        if m and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            _image_store[m.group(1)] = {"path": str(f)}
            continue
        m = re.match(r"^node_(.+?)\.mp4$", name)
        if m:
            _video_store[m.group(1)] = {"path": str(f)}
            continue
        m = re.match(r"^node_(.+?)\.scene\.json$", name)
        if m:
            try:
                _scene_store[m.group(1)] = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                pass
            continue
        m = re.match(r"^node_(.+?)\.prompt\.txt$", name)
        if m:
            try:
                _prompt_store[m.group(1)] = f.read_text(encoding="utf-8")
            except Exception:
                pass


_hydrate_stores()

# ---------- modules ----------

MODULES: dict[str, Any] = {}


def _load_modules() -> None:
    mods_dir = Path(__file__).resolve().parent / "modules"
    for f in sorted(mods_dir.glob("*.py")):
        if f.name.startswith("_") or f.name == "__init__.py":
            continue
        try:
            mod = importlib.import_module(f"server.modules.{f.stem}")
            if hasattr(mod, "MODULE"):
                m = mod.MODULE
                MODULES[m.id] = m
                print(f"[cb-app] loaded module: {m.id}")
        except Exception as e:
            print(f"[cb-app] FAILED to load module {f.name}: {e}")

    # Workflow-manifest modules (source="workflow") come after so hand-written
    # Python modules win on id collisions — a manifest that shadows an existing
    # module gets skipped with a warning instead of silently replacing it.
    try:
        from server.modules._workflow_shared import discover_manifest_modules
        for m in discover_manifest_modules():
            if m.id in MODULES:
                print(f"[cb-app] workflow {m.id} conflicts with existing module, skipped")
                continue
            MODULES[m.id] = m
    except Exception as e:
        print(f"[cb-app] workflow discovery failed: {e}")


_load_modules()

# ---------- app ----------

app = FastAPI(title="ComfyBlockout App", version="0.1.0")

_SAFE_ASSET_ID = re.compile(r"^[a-zA-Z0-9_\-]+$")
_SAFE_EXT = re.compile(r"^[a-zA-Z0-9]{1,6}$")
_SAFE_PROJECT_NAME = re.compile(r"^[a-zA-Z0-9 _\-\.\(\)]{1,80}$")
_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}


def _asset_dir(node_id: str) -> Path:
    d = DATA_DIR / "assets" / node_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _projects_root() -> Path:
    d = DATA_DIR / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- editor + static ----------

@app.get("/")
async def root() -> FileResponse:
    return FileResponse(WEB_DIR / "editor.html")


@app.get("/pivot-test")
async def pivot_test() -> FileResponse:
    return FileResponse(WEB_DIR / "pivot-test.html")


@app.get("/freemocap-mockup")
async def freemocap_mockup() -> FileResponse:
    return FileResponse(WEB_DIR / "freemocap-mockup.html")


@app.get("/mixamo-mockup")
async def mixamo_mockup() -> FileResponse:
    return FileResponse(WEB_DIR / "mixamo-mockup.html")


# Editor still references /extensions/ComfyBlockout/icons/... — keep that path live.
@app.get("/extensions/ComfyBlockout/icons/{name}")
async def legacy_icon(name: str) -> FileResponse:
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "bad name")
    p = WEB_DIR / "icons" / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


# ---------- /comfyblockout/* (ports nodes.py contract) ----------

@app.post("/comfyblockout/save_video")
async def save_video(
    node_id: str = Form(...),
    aspect: str = Form(""),
    video: UploadFile = File(...),
):
    file_bytes = await video.read()
    if not node_id or not file_bytes:
        return JSONResponse({"success": False, "error": "Missing node_id or video"}, status_code=400)

    suffix = Path(video.filename or "").suffix.lower() or ".webm"
    raw_path = DATA_DIR / f"node_{node_id}_raw{suffix}"
    raw_path.write_bytes(file_bytes)

    crop_filter = None
    if aspect and ":" in aspect:
        try:
            a, b = aspect.split(":")
            a, b = int(a), int(b)
            crop_filter = (
                f"crop='if(gt(a,{a}/{b}),ih*{a}/{b},iw)':'if(gt(a,{a}/{b}),ih,iw*{b}/{a})',"
                "scale=trunc(iw/2)*2:trunc(ih/2)*2"
            )
        except Exception:
            crop_filter = None

    final_path = raw_path
    converted = False
    if FFMPEG_BIN and suffix != ".mp4":
        mp4_path = DATA_DIR / f"node_{node_id}.mp4"
        cmd = [FFMPEG_BIN, "-y", "-i", str(raw_path)]
        if crop_filter:
            cmd += ["-vf", crop_filter]
        cmd += [
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an",
            str(mp4_path),
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode == 0 and mp4_path.exists():
            final_path = mp4_path
            converted = True
            try:
                raw_path.unlink()
            except Exception:
                pass
        else:
            print(f"[cb-app] ffmpeg rc={r.returncode}: {r.stderr.decode(errors='ignore')[:300]}")

    _video_store[node_id] = {"path": str(final_path)}

    # Also drop a timestamped copy into videos/ so the recording shows up in the
    # Output panel (rglob "out_*") and can be dragged into a Seedance Ref video
    # slot. Distinct filename per recording preserves history — the primary
    # node_<UID>.mp4 keeps getting overwritten as before for /comfyblockout/video/<id>.
    out_url = None
    out_filename = None
    try:
        import shutil, time
        out_ext = final_path.suffix.lower() or ".mp4"
        out_filename = f"out_rec_{node_id}_{int(time.time() * 1000)}{out_ext}"
        videos_dir = DATA_DIR / "videos"
        videos_dir.mkdir(parents=True, exist_ok=True)
        out_path = videos_dir / out_filename
        shutil.copyfile(final_path, out_path)
        out_url = f"/output/videos/{out_filename}"
    except Exception as e:
        print(f"[cb-app] recording asset copy failed: {e}")

    return JSONResponse({
        "success": True,
        "path": str(final_path),
        "bytes": len(file_bytes),
        "mp4": converted,
        "out_url": out_url,
        "out_filename": out_filename,
    })


@app.post("/comfyblockout/save_image")
async def save_image_route(
    node_id: str = Form(...),
    image: UploadFile = File(...),
):
    file_bytes = await image.read()
    if not node_id or not file_bytes:
        return JSONResponse({"success": False, "error": "Missing node_id or image"}, status_code=400)
    suffix = Path(image.filename or "").suffix.lower() or ".png"
    out_path = DATA_DIR / f"node_{node_id}_image{suffix}"
    out_path.write_bytes(file_bytes)
    _image_store[node_id] = {"path": str(out_path)}
    return JSONResponse({"success": True, "path": str(out_path), "bytes": len(file_bytes)})


@app.post("/comfyblockout/save_blockout")
async def save_blockout_route(
    node_id: str = Form(...),
    image: UploadFile = File(...),
):
    """Versioned blockout snapshot — lands in DATA_DIR as
    `out_blockout_<node_id>_<epoch_ms>.<ext>` so the Output pane treats it
    as a historical asset (filterable via the "Blockout" tab). Separate
    from /save_image which just keeps the ONE current preview per node
    for the ComfyUI graph."""
    import time as _time
    file_bytes = await image.read()
    if not node_id or not file_bytes:
        return JSONResponse({"success": False, "error": "Missing node_id or image"}, status_code=400)
    suffix = Path(image.filename or "").suffix.lower() or ".webp"
    ts = int(_time.time() * 1000)
    fname = f"out_blockout_{node_id}_{ts}{suffix}"
    (DATA_DIR / fname).write_bytes(file_bytes)
    return JSONResponse({"success": True, "url": f"/output/{fname}", "filename": fname, "bytes": len(file_bytes)})


@app.post("/comfyblockout/save_chat_image")
async def save_chat_image_route(image: UploadFile = File(...)):
    """Save a chat-paste image under a unique name in DATA_DIR and return its
    /output/ URL. Separate from /save_image (which is keyed by node_id and gets
    overwritten each snapshot) so multiple pastes in one conversation don't
    collide. Called by the assistant textarea's paste handler."""
    import uuid
    file_bytes = await image.read()
    if not file_bytes:
        return JSONResponse({"success": False, "error": "empty upload"}, status_code=400)
    suffix = Path(image.filename or "").suffix.lower() or ".png"
    fname = f"chat_paste_{uuid.uuid4().hex[:12]}{suffix}"
    (DATA_DIR / fname).write_bytes(file_bytes)
    return JSONResponse({"success": True, "url": f"/output/{fname}", "filename": fname, "bytes": len(file_bytes)})


@app.post("/comfyblockout/save_prompt")
async def save_prompt(request: Request):
    data = await request.json()
    node_id = str(data.get("node_id", "")).strip()
    prompt = data.get("prompt", "")
    if not node_id:
        return JSONResponse({"success": False, "error": "Missing node_id"}, status_code=400)
    _prompt_store[node_id] = prompt
    (DATA_DIR / f"node_{node_id}.prompt.txt").write_text(prompt, encoding="utf-8")
    return JSONResponse({"success": True})


@app.get("/comfyblockout/load_prompt")
async def load_prompt(node_id: str = ""):
    """Returns the user's saved Prompt Tweaks (defaults to empty). The base
    system prompt lives in code and is appended automatically at gen time."""
    node_id = node_id.strip()
    if not node_id:
        return JSONResponse({"prompt": "", "base": BASE_PROMPT})
    if node_id in _prompt_store:
        return JSONResponse({"prompt": _prompt_store[node_id], "base": BASE_PROMPT})
    p = DATA_DIR / f"node_{node_id}.prompt.txt"
    if p.exists():
        text = p.read_text(encoding="utf-8")
        # Migrate: anyone whose saved file still contains the base prompt verbatim
        # gets it stripped so the textarea becomes empty (= no tweaks). The base
        # always fires regardless; nothing is lost.
        if text.strip() == BASE_PROMPT.strip():
            text = ""
            p.write_text(text, encoding="utf-8")
        _prompt_store[node_id] = text
        return JSONResponse({"prompt": text, "base": BASE_PROMPT})
    return JSONResponse({"prompt": "", "base": BASE_PROMPT})


@app.post("/comfyblockout/save_asset")
async def save_asset(
    node_id: str = Form(...),
    asset_id: str = Form(...),
    ext: str = Form(...),
    file: UploadFile = File(...),
):
    file_bytes = await file.read()
    if not node_id or not asset_id or not ext or not file_bytes:
        return JSONResponse({"success": False, "error": "Missing field"}, status_code=400)
    ext = ext.lower().lstrip(".")
    if not _SAFE_ASSET_ID.match(asset_id) or not _SAFE_EXT.match(ext):
        return JSONResponse({"success": False, "error": "Invalid id/ext"}, status_code=400)
    path = _asset_dir(node_id) / f"{asset_id}.{ext}"
    path.write_bytes(file_bytes)
    return JSONResponse({"success": True})


@app.get("/comfyblockout/asset/{node_id}/{asset_name}")
async def serve_asset(node_id: str, asset_name: str):
    if "/" in asset_name or "\\" in asset_name or ".." in asset_name:
        raise HTTPException(400, "Invalid asset name")
    path = _asset_dir(node_id) / asset_name
    if not path.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="application/octet-stream")


@app.post("/comfyblockout/save_scene")
async def save_scene(request: Request):
    try:
        data = await request.json()
    except Exception:
        raw = await request.body()
        data = json.loads(raw.decode("utf-8"))
    node_id = str(data.get("node_id", "")).strip()
    scene = data.get("scene", {})
    if not node_id:
        return JSONResponse({"success": False, "error": "Missing node_id"}, status_code=400)
    _scene_store[node_id] = scene
    sp = DATA_DIR / f"node_{node_id}.scene.json"
    sp.write_text(json.dumps(scene), encoding="utf-8")
    return JSONResponse({"success": True, "path": str(sp)})


@app.get("/comfyblockout/load_scene")
async def load_scene(node_id: str = ""):
    node_id = node_id.strip()
    if not node_id:
        return JSONResponse({"scene": None}, headers=_NO_CACHE)
    if node_id in _scene_store:
        return JSONResponse({"scene": _scene_store[node_id]}, headers=_NO_CACHE)
    p = DATA_DIR / f"node_{node_id}.scene.json"
    if p.exists():
        s = json.loads(p.read_text(encoding="utf-8"))
        _scene_store[node_id] = s
        return JSONResponse({"scene": s}, headers=_NO_CACHE)
    return JSONResponse({"scene": None}, headers=_NO_CACHE)


@app.get("/comfyblockout/video_url")
async def video_url(node_id: str = ""):
    info = _video_store.get(node_id.strip())
    if not info or not Path(info["path"]).exists():
        return JSONResponse({"url": None})
    return JSONResponse({"url": f"/comfyblockout/video/{node_id.strip()}"})


@app.get("/comfyblockout/video/{node_id}")
async def serve_video(node_id: str):
    info = _video_store.get(node_id)
    if not info:
        raise HTTPException(404)
    path = Path(info["path"])
    if not path.exists():
        raise HTTPException(404)
    ctype = "video/mp4" if path.suffix.lower() == ".mp4" else "video/webm"
    return FileResponse(path, media_type=ctype)


@app.delete("/comfyblockout/video/{node_id}")
async def delete_video(node_id: str):
    """Drop the last-recorded viewport clip from the store — used by the
    util pane's X button so the preview slot resets to "no source" instead
    of showing an unrelated earlier recording. The file on disk stays put
    (assets folder keeps the timestamped copy for the Assets modal); this
    only clears the primary node_<id>.mp4 alias the workflow runner reads."""
    _video_store.pop(node_id, None)
    return {"cleared": True}


@app.delete("/comfyblockout/image/{node_id}")
async def delete_image(node_id: str):
    """Same as delete_video but for the still-image alias — util pane's X
    button on image inputs calls this so a stale scene-image doesn't hang
    around after the user clears the preview."""
    _image_store.pop(node_id, None)
    return {"cleared": True}


@app.get("/comfyblockout/image_url")
async def image_url(node_id: str = ""):
    info = _image_store.get(node_id.strip())
    if not info or not Path(info["path"]).exists():
        return JSONResponse({"url": None})
    return JSONResponse({"url": f"/comfyblockout/image/{node_id.strip()}"})


@app.get("/comfyblockout/image/{node_id}")
async def serve_image(node_id: str):
    info = _image_store.get(node_id)
    if not info:
        raise HTTPException(404)
    path = Path(info["path"])
    if not path.exists():
        raise HTTPException(404)
    ext = path.suffix.lower().lstrip(".")
    ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
    return FileResponse(path, media_type=ctype)


@app.get("/comfyblockout/projects/list")
async def list_projects():
    root = _projects_root()
    entries = []
    for p in root.iterdir():
        scene = p / "scene.json"
        if not p.is_dir() or not scene.exists():
            continue
        try:
            st = scene.stat()
            # created = ctime (Windows: file creation, Unix: inode change).
            # modified = mtime (last time the scene.json was saved).
            entries.append({"name": p.name, "created": st.st_ctime, "modified": st.st_mtime})
        except OSError:
            entries.append({"name": p.name, "created": 0, "modified": 0})
    # Newest-modified first — matches how the assets pane sorts by recency.
    entries.sort(key=lambda e: e.get("modified", 0), reverse=True)
    return JSONResponse({"projects": entries})


@app.post("/comfyblockout/projects/save")
async def save_project(request: Request):
    data = await request.json()
    node_id = str(data.get("node_id", "")).strip()
    name = str(data.get("name", "")).strip()
    scene = data.get("scene")
    if not node_id or not name or scene is None:
        return JSONResponse({"success": False, "error": "Missing node_id, name, or scene"}, status_code=400)
    if not _SAFE_PROJECT_NAME.match(name):
        return JSONResponse({"success": False, "error": "Invalid project name"}, status_code=400)

    pdir = _projects_root() / name
    if pdir.exists():
        shutil.rmtree(pdir)
    (pdir / "assets").mkdir(parents=True, exist_ok=True)

    src_assets = _asset_dir(node_id)
    copied = 0
    for im in (scene.get("imports", []) or []):
        asset_id = im.get("assetId")
        filename = im.get("filename") or ""
        ext = filename.split(".")[-1].lower() if "." in filename else "bin"
        if not asset_id:
            continue
        src = src_assets / f"{asset_id}.{ext}"
        if src.exists():
            shutil.copy2(src, pdir / "assets" / src.name)
            copied += 1

    (pdir / "scene.json").write_text(json.dumps(scene, indent=2), encoding="utf-8")
    return JSONResponse({"success": True, "assets": copied})


@app.post("/comfyblockout/projects/load")
async def load_project(request: Request):
    data = await request.json()
    node_id = str(data.get("node_id", "")).strip()
    name = str(data.get("name", "")).strip()
    if not node_id or not name or not _SAFE_PROJECT_NAME.match(name):
        return JSONResponse({"success": False, "error": "Bad node_id or name"}, status_code=400)
    pdir = _projects_root() / name
    sp = pdir / "scene.json"
    if not sp.exists():
        return JSONResponse({"success": False, "error": "Project not found"}, status_code=404)

    dest = _asset_dir(node_id)
    src_assets = pdir / "assets"
    copied = 0
    if src_assets.exists():
        for f in src_assets.iterdir():
            if f.is_file():
                shutil.copy2(f, dest / f.name)
                copied += 1

    scene = json.loads(sp.read_text(encoding="utf-8"))
    _scene_store[node_id] = scene
    (DATA_DIR / f"node_{node_id}.scene.json").write_text(json.dumps(scene), encoding="utf-8")
    return JSONResponse({"success": True, "scene": scene, "assets": copied})


@app.post("/comfyblockout/projects/delete")
async def delete_project(request: Request):
    data = await request.json()
    name = str(data.get("name", "")).strip()
    if not name or not _SAFE_PROJECT_NAME.match(name):
        return JSONResponse({"success": False, "error": "Bad name"}, status_code=400)
    pdir = _projects_root() / name
    if pdir.exists():
        shutil.rmtree(pdir)
    return JSONResponse({"success": True})


@app.post("/comfyblockout/projects/rename")
async def rename_project(request: Request):
    data = await request.json()
    old = str(data.get("old", "")).strip()
    new = str(data.get("new", "")).strip()
    if not old or not _SAFE_PROJECT_NAME.match(old):
        return JSONResponse({"success": False, "error": "Bad old name"}, status_code=400)
    if not new or not _SAFE_PROJECT_NAME.match(new):
        return JSONResponse({"success": False, "error": "Bad new name"}, status_code=400)
    if old == new:
        return JSONResponse({"success": True})
    root = _projects_root()
    src = root / old
    dst = root / new
    if not src.exists():
        return JSONResponse({"success": False, "error": "Project not found"}, status_code=404)
    if dst.exists():
        return JSONResponse({"success": False, "error": "A project with that name already exists"}, status_code=409)
    src.rename(dst)
    return JSONResponse({"success": True})


# ---------- auth (comfy-cli) ----------

_CLI_VERSION_CACHE: tuple[bool, str | None] | None = None
_CLI_LOGIN_CACHE: tuple[bool, str | None, float] | None = None  # (logged_in, msg, expires_at)


def _comfy_cli_available() -> tuple[bool, str | None]:
    """Cached: comfy-cli's cold-start can take 10–20s on first call."""
    global _CLI_VERSION_CACHE
    if _CLI_VERSION_CACHE is not None:
        return _CLI_VERSION_CACHE
    if not COMFY_BIN:
        _CLI_VERSION_CACHE = (False, "binary not found")
        return _CLI_VERSION_CACHE
    try:
        r = subprocess.run([COMFY_BIN, "--version"], capture_output=True, timeout=45, text=True)
        if r.returncode == 0:
            _CLI_VERSION_CACHE = (True, (r.stdout or "").strip() or None)
        else:
            _CLI_VERSION_CACHE = (False, (r.stderr or r.stdout or f"rc={r.returncode}").strip())
    except Exception as e:
        _CLI_VERSION_CACHE = (False, str(e))
    return _CLI_VERSION_CACHE


def _cli_logged_in() -> tuple[bool, str | None]:
    """Parses `comfy cloud whoami`'s envelope and returns the actual signed-in
    state — not just the rc=0 of the command. NOTE: whoami only reports OAuth
    sessions; COMFY_API_KEY auth doesn't show up here, that's checked separately."""
    global _CLI_LOGIN_CACHE
    now = time.time()
    if _CLI_LOGIN_CACHE and _CLI_LOGIN_CACHE[2] > now:
        return _CLI_LOGIN_CACHE[0], _CLI_LOGIN_CACHE[1]
    if not COMFY_BIN:
        _CLI_LOGIN_CACHE = (False, None, now + 30)
        return False, None
    try:
        r = subprocess.run([COMFY_BIN, "cloud", "whoami"], capture_output=True, timeout=45, text=True)
        if r.returncode == 0:
            try:
                env = json.loads((r.stdout or "").strip())
                data = env.get("data") or {}
                signed_in = bool(data.get("signed_in"))
                method = data.get("auth_method") or None
                _CLI_LOGIN_CACHE = (signed_in, method, now + 30)
                return signed_in, method
            except Exception:
                pass
    except Exception:
        pass
    _CLI_LOGIN_CACHE = (False, None, now + 30)
    return False, None


@app.get("/api/auth/status")
async def auth_status():
    # Re-read .env on every poll so the UI doesn't need a server restart after edits.
    _load_env_file()
    # If the env var changed, drop the cached whoami so the next call re-checks.
    global _CLI_LOGIN_CACHE
    _CLI_LOGIN_CACHE = None
    cli, _ = _comfy_cli_available()
    has_key = bool(os.environ.get("COMFY_API_KEY"))
    signed_in, auth_method = _cli_logged_in() if cli else (False, None)
    return {
        "cli_installed": cli,
        "cli_path": COMFY_BIN,
        "api_key_set": has_key,
        "signed_in": signed_in,
        "auth_method": auth_method,
        "ready": cli and (signed_in or has_key),
    }


@app.post("/api/auth/login")
async def auth_login():
    if not COMFY_BIN:
        raise HTTPException(500, "comfy-cli not installed (run: pip install comfy-cli)")
    try:
        subprocess.Popen(
            [COMFY_BIN, "cloud", "login"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"started": True, "message": "Browser opened — finish sign-in, then poll /api/auth/status."}
    except Exception as e:
        raise HTTPException(500, f"login spawn failed: {e}")


@app.post("/api/auth/logout")
async def auth_logout():
    if not COMFY_BIN:
        raise HTTPException(500, "comfy-cli not installed")
    try:
        r = subprocess.run([COMFY_BIN, "cloud", "logout"], capture_output=True, timeout=10, text=True)
        return {"ok": r.returncode == 0, "output": (r.stdout or r.stderr or "").strip()}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/auth/key")
async def auth_key(request: Request):
    """Persist a Comfy Cloud API key to .env and load it into the current process
    so subsequent `comfy generate` and `comfy run --where cloud` calls authenticate
    as the user. The CLI reads two different names depending on the mode:
      COMFY_API_KEY       — partner-node auth (`comfy generate <provider>`)
      COMFY_CLOUD_API_KEY — Cloud runner auth (`comfy run/upload/download --where cloud`)
    We mirror the same key into both so the user only pastes it once."""
    body = await request.json()
    key = str(body.get("key", "")).strip()
    if not key:
        raise HTTPException(400, "key is required")
    if not key.startswith("comfyui-"):
        raise HTTPException(400, "key should start with 'comfyui-'")
    os.environ["COMFY_API_KEY"] = key
    os.environ["COMFY_CLOUD_API_KEY"] = key
    kv = _read_env_kv()
    kv["COMFY_API_KEY"] = key
    kv["COMFY_CLOUD_API_KEY"] = key
    _write_env_kv(kv)
    return {"saved": True}


@app.delete("/api/auth/key")
async def auth_key_clear():
    os.environ.pop("COMFY_API_KEY", None)
    os.environ.pop("COMFY_CLOUD_API_KEY", None)
    kv = _read_env_kv()
    kv.pop("COMFY_API_KEY", None)
    kv.pop("COMFY_CLOUD_API_KEY", None)
    _write_env_kv(kv)
    return {"cleared": True}


@app.get("/api/paths")
async def paths_get():
    """Return the currently-configured ComfyUI paths so the settings modal can
    pre-fill on open. Blanks mean 'fall back to auto-detect' — the finders
    already treat empty env vars that way."""
    return {
        "custom_nodes_dir": os.environ.get("COMFY_CUSTOM_NODES_DIR", ""),
        "models_dir": os.environ.get("COMFY_MODELS_DIR", ""),
        "python_exe": os.environ.get("COMFY_PYTHON_EXE", ""),
    }


@app.post("/api/paths")
async def paths_set(request: Request):
    """Persist ComfyUI paths to .env and load them into the current process so
    the very next tool call (install_custom_node, download_model, etc.) picks
    them up — no restart needed for the paths themselves. Any field the user
    left blank in the modal reverts to auto-detect."""
    body = await request.json()
    fields = {
        "COMFY_CUSTOM_NODES_DIR": str(body.get("custom_nodes_dir", "")).strip(),
        "COMFY_MODELS_DIR": str(body.get("models_dir", "")).strip(),
        "COMFY_PYTHON_EXE": str(body.get("python_exe", "")).strip(),
    }
    kv = _read_env_kv()
    for k, v in fields.items():
        if v:
            os.environ[k] = v
            kv[k] = v
        else:
            os.environ.pop(k, None)
            kv.pop(k, None)
    _write_env_kv(kv)
    return {"saved": True}


@app.post("/api/paths/detect")
async def paths_detect():
    """Run the same finders the tools use, so users can see what would be
    picked up if they leave the fields blank. Doesn't persist anything."""
    cn = _find_local_custom_nodes_dir()
    md = _find_local_models_dir()
    py = _find_local_python()
    return {
        "custom_nodes_dir": str(cn) if cn else "",
        "models_dir": str(md) if md else "",
        "python_exe": str(py) if py else "",
    }


@app.post("/api/llm/key")
async def llm_key(request: Request):
    """Persist the Anthropic API key alongside the Comfy key in .env."""
    body = await request.json()
    key = str(body.get("key", "")).strip()
    if not key:
        raise HTTPException(400, "key is required")
    if not key.startswith("sk-ant-"):
        raise HTTPException(400, "key should start with 'sk-ant-'")
    os.environ["ANTHROPIC_API_KEY"] = key
    kv = _read_env_kv()
    kv["ANTHROPIC_API_KEY"] = key
    _write_env_kv(kv)
    return {"saved": True}


@app.delete("/api/llm/key")
async def llm_key_clear():
    os.environ.pop("ANTHROPIC_API_KEY", None)
    kv = _read_env_kv()
    kv.pop("ANTHROPIC_API_KEY", None)
    _write_env_kv(kv)
    return {"cleared": True}


@app.get("/api/llm/status")
async def llm_status():
    _load_env_file()
    return {"api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY"))}


# ---------- LLM usage tracking ----------
#
# Every /api/llm/chat turn already returns per-turn usage in the response
# (input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens).
# We roll those into data/llm_usage.json so the user can see today's spend +
# lifetime total across app restarts. Cost estimates use Sonnet 4.6 pricing
# (per Anthropic's docs, 2026-07); update _PRICING when the default model changes.

_USAGE_PATH = DATA_DIR / "llm_usage.json"
# Per-turn detail log (JSONL). One line per Claude turn — timestamp, node,
# usage, tool_uses, first ~200 chars of user message. Small, append-only,
# hot on writes but cheap to tail. Reader endpoint slices the last N lines.
_TURN_LOG_PATH = DATA_DIR / "llm_turns.jsonl"
# USD per 1M tokens — Sonnet 4.6 rates. Cache-write is priced higher than
# regular input (25% premium); cache-read is a 90% discount.
_PRICING = {
    "input_per_M":       3.00,
    "output_per_M":     15.00,
    "cache_read_per_M":  0.30,
    "cache_write_per_M": 3.75,
}

def _load_usage() -> dict:
    try:
        if not _USAGE_PATH.exists():
            return {"total": _zero_usage(), "by_day": {}}
        d = json.loads(_USAGE_PATH.read_text(encoding="utf-8"))
        d.setdefault("total", _zero_usage())
        d.setdefault("by_day", {})
        return d
    except Exception:
        return {"total": _zero_usage(), "by_day": {}}

def _zero_usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "turns": 0}

def _save_usage(d: dict) -> None:
    _USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _USAGE_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")

def _record_turn_usage(u: dict) -> None:
    """Merge one turn's usage into today's bucket + the lifetime total."""
    from datetime import date
    today = date.today().isoformat()
    data = _load_usage()
    day_bucket = data["by_day"].setdefault(today, _zero_usage())
    for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        v = int(u.get(k) or 0)
        day_bucket[k] += v
        data["total"][k] += v
    day_bucket["turns"] += 1
    data["total"]["turns"] += 1
    _save_usage(data)

def _estimate_cost_usd(u: dict) -> float:
    return (
        u.get("input_tokens", 0)                / 1_000_000 * _PRICING["input_per_M"]
      + u.get("output_tokens", 0)               / 1_000_000 * _PRICING["output_per_M"]
      + u.get("cache_read_input_tokens", 0)     / 1_000_000 * _PRICING["cache_read_per_M"]
      + u.get("cache_creation_input_tokens", 0) / 1_000_000 * _PRICING["cache_write_per_M"]
    )


def _record_turn_detail(entry: dict) -> None:
    """Append one turn's detail as a JSONL line. Best-effort — a write failure
    never fails a chat turn. Keeps the last ~5000 lines by truncating from
    the head when the file grows past ~10k lines (cheap enough to eyeball,
    no rotation infrastructure needed)."""
    try:
        _TURN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Head-truncate periodically so we don't accumulate a giant file.
        # 10k check happens on the write path (very occasional cost).
        if _TURN_LOG_PATH.exists() and _TURN_LOG_PATH.stat().st_size > 5_000_000:
            try:
                lines = _TURN_LOG_PATH.read_text(encoding="utf-8").splitlines()
                if len(lines) > 5000:
                    _TURN_LOG_PATH.write_text("\n".join(lines[-5000:]) + "\n", encoding="utf-8")
            except Exception:
                pass
        with _TURN_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _read_recent_turns(limit: int = 100) -> list[dict]:
    """Return the last N turn-detail entries, newest first. Missing file → []."""
    try:
        if not _TURN_LOG_PATH.exists():
            return []
        lines = _TURN_LOG_PATH.read_text(encoding="utf-8").splitlines()
        # Only decode the tail we care about — cheap even at 5000-line cap.
        tail = lines[-max(1, min(500, int(limit))) :]
        out = []
        for line in reversed(tail):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except Exception:
        return []

_AGENT_DOCS_DIR = APP_DIR / "docs" / "agent"


@app.get("/api/agent/docs/{topic}")
async def agent_docs(topic: str):
    """Serve extended-topic markdown for the agent's read_docs tool. Docs live
    in server/docs/agent/*.md so they can be edited without a server restart
    (they're just read from disk on every call). The topic slug is validated
    against the on-disk file set so we never trust arbitrary paths from the
    client — no `..` traversal, no absolute paths."""
    slug = (topic or "").strip().lower()
    if not slug.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(400, "invalid topic slug")
    path = _AGENT_DOCS_DIR / f"{slug}.md"
    try:
        # resolve() flattens any symlink/relative shenanigans; the parents check
        # then confirms the resolved path is still under the docs dir.
        real = path.resolve()
        if _AGENT_DOCS_DIR.resolve() not in real.parents:
            raise HTTPException(400, "topic path escapes docs dir")
        if not real.is_file():
            raise HTTPException(404, f"no doc for topic '{slug}'")
        return JSONResponse({"topic": slug, "content": real.read_text(encoding="utf-8")})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"read failed: {e}")


@app.get("/api/llm/usage")
async def llm_usage():
    """Return today's + lifetime AI Agent token usage with estimated USD cost."""
    from datetime import date
    data = _load_usage()
    today_key = date.today().isoformat()
    today = data["by_day"].get(today_key, _zero_usage())
    total = data["total"]
    return {
        "today": {**today, "estimated_usd": round(_estimate_cost_usd(today), 4)},
        "total": {**total, "estimated_usd": round(_estimate_cost_usd(total), 4)},
        "pricing": _PRICING,
    }


@app.get("/api/llm/turns")
async def llm_turns(limit: int = 100):
    """Return the last N per-turn detail entries, newest first. Powers the
    Debug page — one row per Claude turn showing tokens burned + which tools
    the agent reached for. Cheap tail-read; caps at 500."""
    return {"turns": _read_recent_turns(limit)}


# ---------- user MCP servers ----------
#
# Users can extend the agent's tool surface by registering HTTP MCP servers
# (Puppeteer, GitHub, custom internal tools, etc.). We piggyback on the same
# Anthropic MCP beta (`mcp-client-2025-11-20`) that Comfy Cloud is wired
# through, so no local MCP client library is needed — Claude handles the
# connections server-side. Stdio-transport MCP servers can't be exposed this
# way; users needing those would run a local HTTP wrapper.
#
# Persisted as data/mcp_servers.json. Schema:
#   {"servers": [{"name": str, "url": str,
#                 "authorization_token": str | None,
#                 "enabled": bool}]}

_MCP_CFG_PATH = APP_DIR / "data" / "mcp_servers.json"

def _load_mcp_servers() -> list[dict]:
    try:
        if not _MCP_CFG_PATH.exists():
            return []
        data = json.loads(_MCP_CFG_PATH.read_text(encoding="utf-8"))
        arr = data.get("servers") if isinstance(data, dict) else None
        return arr if isinstance(arr, list) else []
    except Exception:
        return []

def _save_mcp_servers(servers: list[dict]) -> None:
    _MCP_CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MCP_CFG_PATH.write_text(
        json.dumps({"servers": servers}, indent=2),
        encoding="utf-8",
    )

def _redact_mcp_server(s: dict) -> dict:
    """Never send raw tokens back to the client — the settings UI only needs
    to know whether one exists, not its value."""
    return {
        "name": s.get("name", ""),
        "url": s.get("url", ""),
        "has_token": bool(s.get("authorization_token")),
        "enabled": bool(s.get("enabled", True)),
    }

@app.get("/api/mcp/servers")
async def list_mcp_servers():
    return {"servers": [_redact_mcp_server(s) for s in _load_mcp_servers()]}

@app.post("/api/mcp/servers")
async def add_mcp_server(request: Request):
    body = await request.json()
    name = str(body.get("name", "")).strip()
    url = str(body.get("url", "")).strip()
    token = body.get("authorization_token")
    if not name:
        raise HTTPException(400, "name is required")
    # Names become part of the Claude tool namespace; keep them tame.
    if not all(c.isalnum() or c in "-_" for c in name):
        raise HTTPException(400, "name may only contain letters, digits, - and _")
    if not url.startswith("https://") and not url.startswith("http://"):
        raise HTTPException(400, "url must start with http(s)://")
    # Reserve 'comfy-cloud' — it's hardcoded elsewhere and gets its auth from
    # the Cloud API key, not the user-server config.
    if name == "comfy-cloud":
        raise HTTPException(400, "'comfy-cloud' is reserved")
    servers = _load_mcp_servers()
    if any(s.get("name") == name for s in servers):
        raise HTTPException(400, f"a server named '{name}' already exists")
    entry = {"name": name, "url": url, "enabled": True}
    if isinstance(token, str) and token.strip():
        entry["authorization_token"] = token.strip()
    servers.append(entry)
    _save_mcp_servers(servers)
    return {"server": _redact_mcp_server(entry)}

@app.delete("/api/mcp/servers/{name}")
async def delete_mcp_server(name: str):
    servers = _load_mcp_servers()
    new = [s for s in servers if s.get("name") != name]
    if len(new) == len(servers):
        raise HTTPException(404, f"no server named '{name}'")
    _save_mcp_servers(new)
    return {"deleted": name}

@app.patch("/api/mcp/servers/{name}")
async def patch_mcp_server(name: str, request: Request):
    body = await request.json()
    servers = _load_mcp_servers()
    hit = next((s for s in servers if s.get("name") == name), None)
    if not hit:
        raise HTTPException(404, f"no server named '{name}'")
    if "enabled" in body:
        hit["enabled"] = bool(body["enabled"])
    if "url" in body:
        url = str(body["url"]).strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise HTTPException(400, "url must start with http(s)://")
        hit["url"] = url
    if "authorization_token" in body:
        tok = body["authorization_token"]
        if tok is None or tok == "":
            hit.pop("authorization_token", None)
        elif isinstance(tok, str) and tok.strip():
            hit["authorization_token"] = tok.strip()
    _save_mcp_servers(servers)
    return {"server": _redact_mcp_server(hit)}


# ---------- reference upload (signed-URL via comfy-cli) ----------

_REF_DIR = DATA_DIR / "refs"
_REF_DIR.mkdir(parents=True, exist_ok=True)

# In-memory cache of refs per node_id: list of {"id", "filename", "local_url", "signed_url"}
_refs_store: dict[str, list[dict]] = {}
_REF_INDEX = DATA_DIR / "refs_index.json"
try:
    if _REF_INDEX.exists():
        _refs_store.update(json.loads(_REF_INDEX.read_text(encoding="utf-8")))
except Exception:
    pass


def _persist_refs() -> None:
    try:
        _REF_INDEX.write_text(json.dumps(_refs_store, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[cb-app] failed to persist refs index: {e}")


@app.post("/api/refs/upload")
async def refs_upload(
    node_id: str = Form(...),
    file: UploadFile = File(...),
):
    """Save the uploaded reference locally, then call `comfy generate upload`
    to host it on Comfy's storage and grab a signed URL we can hand to other
    `comfy generate` calls (nano-banana --image, seedance --image, etc.)."""
    if not COMFY_BIN:
        raise HTTPException(500, "comfy-cli not available")
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(400, "empty upload")

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename or "ref")
    ref_id = uuid.uuid4().hex[:10]
    suffix = Path(safe_name).suffix or ".bin"
    local_path = _REF_DIR / f"{node_id}_{ref_id}{suffix}"
    local_path.write_bytes(file_bytes)

    # Shell out via the thread-runner pattern (Windows + uvicorn-reload safe).
    # Force a wide terminal width so `rich`/the CLI doesn't line-wrap the signed
    # URL across multiple lines — that's the bug we hit before.
    import asyncio as _asyncio
    def _shell() -> tuple[int, str, str]:
        env_v = os.environ.copy()
        env_v["COLUMNS"] = "10000"
        env_v["NO_COLOR"] = "1"
        r = subprocess.run(
            [COMFY_BIN, "--json", "generate", "upload", str(local_path)],
            capture_output=True, text=True, timeout=180, env=env_v,
        )
        return r.returncode, r.stdout or "", r.stderr or ""
    rc, out, err = await _asyncio.to_thread(_shell)
    if rc != 0:
        try:
            local_path.unlink()
        except Exception:
            pass
        msg = (err.strip() or out.strip() or f"upload rc={rc}")[:400]
        raise HTTPException(502, f"comfy upload failed: {msg}")

    signed_url = None
    # 1) try JSON envelope (if upload ever emits one)
    try:
        env_json = json.loads(out.strip())
        data = env_json.get("data") or {}
        signed_url = data.get("url") or data.get("signed_url") or data.get("storage_url")
    except Exception:
        pass
    # 2) regex-extract a URL from the (possibly wrapped) plain-text output
    if not signed_url:
        # Collapse all whitespace, then grab the first https:// run
        joined = re.sub(r"\s+", "", out)
        m = re.search(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", joined)
        if m:
            signed_url = m.group(0)
    if not signed_url:
        raise HTTPException(502, f"could not parse signed URL from output: {out[:400]}")
    print(f"[cb-app] uploaded ref → {signed_url[:80]}…")

    entry = {
        "id": ref_id,
        "filename": file.filename or safe_name,
        "local_url": f"/output/refs/{local_path.name}",
        "signed_url": signed_url,
    }
    _refs_store.setdefault(node_id, []).append(entry)
    _persist_refs()
    return entry


@app.get("/api/refs")
async def refs_list(node_id: str = "preview"):
    return {"refs": list(_refs_store.get(node_id, []))}


@app.delete("/api/refs/{ref_id}")
async def refs_delete(ref_id: str, node_id: str = "preview"):
    lst = _refs_store.get(node_id) or []
    keep, dropped = [], None
    for r in lst:
        if r["id"] == ref_id:
            dropped = r
        else:
            keep.append(r)
    if dropped is None:
        raise HTTPException(404, "ref not found")
    _refs_store[node_id] = keep
    _persist_refs()
    # Best-effort local cleanup
    try:
        local = _REF_DIR / Path(dropped["local_url"]).name
        if local.exists():
            local.unlink()
    except Exception:
        pass
    return {"deleted": ref_id}


# ---------- modules ----------

def _module_dict(m) -> dict:
    return {
        "id": m.id,
        "label": m.label,
        "kind": m.kind,
        "inputs": m.inputs,
        "output_ext": m.output_ext,
        "source": getattr(m, "source", "python"),
        "runner": getattr(m, "runner", "local"),
        "util": bool(getattr(m, "util", False)),
        "icon": getattr(m, "icon", "") or "",
        "presets": list(getattr(m, "presets", []) or []),
    }


@app.get("/api/modules")
async def list_modules():
    return {"modules": [_module_dict(m) for m in MODULES.values()]}


# Requirements probe for the Motion tool. AnimoFlow doesn't publish prebuilt
# Docker images — it ships a git repo containing Dockerfiles + a compose
# stack. We install it INTO the ComfyBlockout app tree (`tools/animoflow`)
# rather than under a ComfyUI custom_nodes folder — that lets us talk to the
# containers' HTTP endpoints directly and skips the ComfyUI restart dance.
# Checklist:
#   1. Docker running                       (docker version → 0)
#   2. AnimoFlow repo cloned in tools/      (tools/animoflow/.git exists)
#   3. AnimoFlow containers up              (docker ps --filter name=animoflow)
# Each check has a hard timeout so the pane doesn't stall if Docker is
# unresponsive. Frontend re-hits this on Re-check + on pane render.
_ANIMOFLOW_DIR = APP_DIR / "tools" / "animoflow"
_ANIMOFLOW_REPO_URL = "https://github.com/AnimoFlow/comfyui-animoflow"

_TRIPOSPLAT_DIR = APP_DIR / "tools" / "triposplat"
_TRIPOSPLAT_HOST = os.environ.get("TRIPOSPLAT_HOST", "127.0.0.1")
_TRIPOSPLAT_PORT = int(os.environ.get("TRIPOSPLAT_PORT", "8004"))


def _animoflow_installed() -> bool:
    """True if the AnimoFlow repo has been cloned into tools/animoflow.
    Checks for the .git subfolder rather than the parent — a bare mkdir
    shouldn't register as installed."""
    return (_ANIMOFLOW_DIR / ".git").exists()


def _triposplat_scaffold_present() -> bool:
    """True if the TripoSplat tool folder is on disk — we ship the Dockerfile
    + server.py in-repo (unlike AnimoFlow which requires a separate clone),
    so this is a sanity check that the user is on a build that includes it,
    not a "did you clone it yet" prompt."""
    return (_TRIPOSPLAT_DIR / "docker-compose.yml").exists()


@app.get("/api/motion/requirements")
async def motion_requirements():
    import subprocess
    docker_ok = False
    docker_installed = False
    docker_error = ""
    try:
        # `docker version` (client only, no --format that hits the server) tells
        # us if the CLI is present + installed. Then we probe the daemon
        # separately so we can distinguish "not installed" from "installed but
        # not running" — very different fix instructions for the user.
        r = subprocess.run(
            ["docker", "--version"],
            capture_output=True, text=True, timeout=3,
        )
        docker_installed = r.returncode == 0
        if docker_installed:
            r2 = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True, text=True, timeout=3,
            )
            docker_ok = r2.returncode == 0 and bool(r2.stdout.strip())
            if not docker_ok:
                # Daemon not responding — common when Docker Desktop is
                # installed but the tray app hasn't been launched yet.
                docker_error = "Installed, not running"
        else:
            docker_error = (r.stderr or r.stdout or "").strip()[:200] or "docker command failed"
    except FileNotFoundError:
        docker_error = "docker command not on PATH"
    except subprocess.TimeoutExpired:
        # Timeout on --version is a real hang (rare); on `version` (with daemon
        # probe) means Docker Desktop is starting up or wedged.
        docker_error = "docker daemon not responding"
        docker_installed = True  # if we got past --version we know it exists
    except Exception as e:
        docker_error = str(e)[:200]

    # AnimoFlow install — filesystem check against APP_DIR/tools/animoflow.
    node_ok = _animoflow_installed()
    node_path = str(_ANIMOFLOW_DIR) if node_ok else ""

    # AnimoFlow containers running — use plain `docker ps` and match names
    # that start with "animoflow" (compose defaults the project name to the
    # cloned folder, and services become `<project>-<service>-1` in modern
    # docker compose). More robust than `docker compose ps --format` which
    # has inconsistent output between docker versions.
    containers_ok = False
    containers_detail = ""
    if docker_ok and _animoflow_installed():
        try:
            r = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=5,
            )
            names = [n.strip() for n in (r.stdout or "").splitlines() if n.strip()]
            animoflow_names = [n for n in names if n.lower().startswith("animoflow")]
            containers_ok = len(animoflow_names) > 0
            containers_detail = ", ".join(animoflow_names[:3]) if animoflow_names else ""
        except Exception:
            containers_ok = False

    return {
        "docker": {
            "ok": docker_ok,
            "installed": docker_installed,
            "detail": docker_error,
            "install_url": "https://www.docker.com/products/docker-desktop/",
        },
        "animoflow_node": {
            "ok": node_ok,
            "path": node_path,
            "target": str(_ANIMOFLOW_DIR),
            "repo_url": _ANIMOFLOW_REPO_URL,
        },
        "animoflow_containers": {
            "ok": containers_ok,
            "detail": containers_detail,
            "installed": node_ok,
        },
    }


def _check_docker_state() -> tuple[bool, bool, str]:
    """Shared docker probe — returns (running, installed, error_detail).
    Split out so the TripoSplat + AnimoFlow requirements endpoints don't
    duplicate the whole subprocess+timeout dance."""
    import subprocess
    try:
        r = subprocess.run(
            ["docker", "--version"],
            capture_output=True, text=True, timeout=3,
        )
        installed = r.returncode == 0
        if not installed:
            return False, False, (r.stderr or r.stdout or "").strip()[:200] or "docker command failed"
        r2 = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=3,
        )
        running = r2.returncode == 0 and bool(r2.stdout.strip())
        return running, installed, "" if running else "Installed, not running"
    except FileNotFoundError:
        return False, False, "docker command not on PATH"
    except subprocess.TimeoutExpired:
        return False, True, "docker daemon not responding"
    except Exception as e:
        return False, False, str(e)[:200]


@app.get("/api/requirements/triposplat")
async def triposplat_requirements():
    """Requirements pane data for the TripoSplat standalone Tools tile.

    Four gates, all must be green before /generate will succeed:
      1. docker — daemon reachable
      2. scaffold — tools/triposplat/ shipped with this build (should
         always be true; guards against a stripped-down deployment)
      3. container — the `triposplat` service is running
      4. weights — the container's /health reports weights_ready
    """
    docker_ok, docker_installed, docker_detail = _check_docker_state()

    scaffold_ok = _triposplat_scaffold_present()

    container_ok = False
    container_detail = ""
    if docker_ok:
        import subprocess
        try:
            r = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=5,
            )
            names = [n.strip() for n in (r.stdout or "").splitlines() if n.strip()]
            hit = next((n for n in names if n.lower() == "triposplat" or n.lower().startswith("triposplat")), None)
            container_ok = hit is not None
            container_detail = hit or ""
        except Exception:
            container_ok = False

    # Container /health probe — done via httpx sync-in-async since the
    # rest of this endpoint is synchronous subprocess work; a 3s cap
    # keeps the whole endpoint under ~15s worst-case.
    weights_ok = False
    pipeline_ok = False
    weights_detail = ""
    if container_ok:
        try:
            async with httpx.AsyncClient() as client:
                # 10s timeout so a momentary /health delay (busy event loop
                # right after a big generate returned, GC pause, etc.)
                # doesn't flash the row red and hide the source-image
                # section underneath.
                r = await client.get(
                    f"http://{_TRIPOSPLAT_HOST}:{_TRIPOSPLAT_PORT}/health",
                    timeout=10.0,
                )
                if r.status_code == 200:
                    j = r.json()
                    weights_ok = bool(j.get("weights_ready"))
                    pipeline_ok = bool(j.get("pipeline_ready"))
                    if not weights_ok:
                        weights_detail = "downloading from HuggingFace…"
                else:
                    weights_detail = f"/health returned {r.status_code}"
        except Exception as e:
            weights_detail = f"container up but /health unreachable: {e}"[:200]

    return {
        "docker": {
            "ok": docker_ok,
            "installed": docker_installed,
            "detail": docker_detail,
            "install_url": "https://www.docker.com/products/docker-desktop/",
        },
        "scaffold": {
            "ok": scaffold_ok,
            "path": str(_TRIPOSPLAT_DIR),
        },
        "container": {
            "ok": container_ok,
            "detail": container_detail,
            "compose_dir": str(_TRIPOSPLAT_DIR),
        },
        "weights": {
            "ok": weights_ok,
            # Pipeline-ready flips true after the first generate warms the
            # model; not a blocker for showing the tile as available, but
            # useful as a "next generate will be fast" indicator.
            "pipeline_ready": pipeline_ok,
            "detail": weights_detail,
        },
    }


@app.post("/api/requirements/triposplat/start_container")
async def triposplat_start_container():
    """Convenience helper — `docker compose up --build -d` inside
    tools/triposplat/. First run pulls the CUDA base image + installs deps
    + downloads weights, which is 10-15 minutes on a fresh box; the pane
    surfaces build progress via docker logs the user can tail themselves."""
    import subprocess
    if not _triposplat_scaffold_present():
        raise HTTPException(500, f"scaffold missing at {_TRIPOSPLAT_DIR}")
    docker_ok, _, docker_detail = _check_docker_state()
    if not docker_ok:
        raise HTTPException(400, f"Docker isn't running: {docker_detail}")
    try:
        # Detach with -d so this endpoint returns immediately; the build/
        # download runs in the background and the requirements poll picks
        # up state changes as they happen.
        r = subprocess.run(
            ["docker", "compose", "up", "--build", "-d"],
            cwd=str(_TRIPOSPLAT_DIR),
            capture_output=True, text=True, timeout=15,
        )
        # `up -d` returns quickly (build streams to logs); rc 0 = compose
        # accepted the request. Not a build-succeeded signal — that comes
        # later via the /health poll.
        if r.returncode != 0:
            return {"ok": False, "detail": (r.stderr or r.stdout).strip()[:500]}
        return {"ok": True, "detail": (r.stdout or "").strip()[:500]}
    except subprocess.TimeoutExpired:
        # `up -d` shouldn't take this long — compose is probably chewing
        # on a fresh build. Still fine to return ok:true since the build
        # is now underway and health polling will surface completion.
        return {"ok": True, "detail": "compose command timed out returning, build likely still running"}


@app.post("/api/motion/start_containers")
async def motion_start_containers():
    """Build + start the AnimoFlow containers via docker compose. First run
    downloads model weights + builds images — can take 10+ minutes on a slow
    connection. Returns as soon as `docker compose up -d --build` returns
    (which is *after* the images are built — compose blocks). The frontend
    should show a long-running spinner and periodically Re-check to detect
    when containers land in `docker ps`."""
    import subprocess
    if not _animoflow_installed():
        raise HTTPException(400, "AnimoFlow not installed yet — click Install on the AnimoFlow row first")
    # Detect the compose file — repo ships either docker-compose.yml or
    # compose.yaml (newer convention). Bail early with a clear message if
    # neither is present rather than letting docker error confusingly.
    compose_candidates = [
        _ANIMOFLOW_DIR / "docker-compose.yml",
        _ANIMOFLOW_DIR / "docker-compose.yaml",
        _ANIMOFLOW_DIR / "compose.yml",
        _ANIMOFLOW_DIR / "compose.yaml",
    ]
    if not any(p.exists() for p in compose_candidates):
        raise HTTPException(500, f"no docker-compose file found in {_ANIMOFLOW_DIR} — check the repo layout")
    # Workaround for an upstream inconsistency — mdm's Dockerfile.cpu does
    # `COPY weights/ ./weights/` but the `weights/` folder is meant to be
    # bind-mounted at runtime, not baked in. The COPY still requires the source
    # dir to exist at build time, so we create an empty one. The compose file's
    # volume mount overlays the real weights when the container starts.
    (_ANIMOFLOW_DIR / "containers" / "mdm" / "weights").mkdir(parents=True, exist_ok=True)
    try:
        # Start only the momask service for the MVP text-to-motion path.
        # Skipping the rest for now because:
        #   - mdm needs external weights bind-mounted (MDM_WEIGHTS_DIR)
        #   - priormdm downloads from Google Drive — flaky rate-limits
        #   - retargeter's Dockerfile COPYs Mixamo FBX files that aren't in the repo
        #   - kimodo is behind a `gpu` profile and needs an NVIDIA GPU
        # MoMask alone is self-contained (bakes checkpoints via gdown in its
        # own build) and reachable at http://localhost:8003.
        # No `--build` flag: docker compose builds the image on first run when
        # it doesn't exist, and re-uses the cached image on subsequent starts.
        # Force-rebuild belongs in a separate "Rebuild" button we'll add later.
        r = subprocess.run(
            ["docker", "compose", "up", "-d", "momask"],
            capture_output=True, text=True, timeout=1800, cwd=str(_ANIMOFLOW_DIR),
        )
        if r.returncode != 0:
            # Docker compose spams progress lines before the actual failure —
            # take the TAIL of the combined output so the important part isn't
            # buried under "Image alpine Pulling / Pulled" noise. 1500 chars
            # is usually enough to see the failing RUN step + its error.
            combined = ((r.stderr or "") + "\n" + (r.stdout or "")).strip()
            tail = combined[-1500:] if len(combined) > 1500 else combined
            raise HTTPException(500, f"docker compose failed:\n{tail}")
    except FileNotFoundError:
        raise HTTPException(500, "docker command not on PATH — restart run.bat after installing Docker")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "docker compose timed out after 30 minutes — check Docker Desktop's build logs")
    return {"success": True}


@app.post("/api/motion/generate")
async def motion_generate(request: Request):
    """Proxy the client's prompt to MoMask's /generate endpoint, decode the
    returned NPZ (which is a numpy zip carrying poses: (T, 22, 3) joint
    positions in HumanML3D order), and return the frames as plain JSON. Doing
    the NPZ decode server-side sidesteps needing a JS numpy zip parser.
    HumanML3D coord frame is Y-up, meters-scale — the client can plot the
    joints directly as world positions."""
    import base64
    import io
    import httpx
    try:
        import numpy as np
    except ImportError:
        raise HTTPException(500, "numpy not installed — required to decode MoMask output")
    body = await request.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    payload = {
        "prompt": prompt,
        "max_frames": int(body.get("max_frames") or 120),
        "seed": int(body.get("seed") or 42),
    }
    # 5 min hard cap — CPU inference on a longer prompt can take a couple of
    # minutes; anything beyond that likely means the container's model isn't
    # loaded or the request wedged. httpx AsyncClient handles the wait cleanly.
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            r = await client.post("http://localhost:8003/generate", json=payload)
            if r.status_code != 200:
                raise HTTPException(r.status_code, f"MoMask error: {r.text[:500]}")
            data = r.json()
    except httpx.ConnectError:
        raise HTTPException(503, "can't reach MoMask at localhost:8003 — is the container running?")
    npz_b64 = data.get("npz_b64")
    metadata = data.get("metadata") or {}
    if not npz_b64:
        raise HTTPException(500, "MoMask response missing npz_b64")
    # Decode the NPZ envelope. Only `poses` is required — MoMask also stuffs the
    # prompt in there but we already have it client-side. `poses` shape is
    # (T frames, 22 joints, 3 xyz).
    try:
        buf = io.BytesIO(base64.b64decode(npz_b64))
        with np.load(buf, allow_pickle=True) as npz:
            poses = npz["poses"].astype(float)
    except Exception as e:
        raise HTTPException(500, f"NPZ decode failed: {e}")
    if poses.ndim != 3 or poses.shape[1] != 22 or poses.shape[2] != 3:
        raise HTTPException(500, f"unexpected poses shape {poses.shape} — expected (T, 22, 3)")
    return {
        "prompt": prompt,
        "num_frames": int(poses.shape[0]),
        "joints_per_frame": 22,
        # Round to 4 decimals — enough precision for visualization, cuts JSON
        # payload roughly in half vs full float precision.
        "poses": poses.round(4).tolist(),
        "metadata": metadata,
    }


# ── Skybox generation (T2I via Comfy Cloud partner API) ──────────────
# The Skybox object's Generate button posts here. Backend appends a short
# system-prompt suffix onto the user's brief so they just type the aesthetic
# ("misty pine forest at dawn") without having to remember "equirectangular"
# or "seamless wrap". Shells out via `comfy generate <partner>` — see the
# CLI id below; swap to a different partner id if the CLI catalog changes.
_SKYBOX_SYSTEM_PROMPT_SUFFIX = (
    ". Seamless 360-degree equirectangular skybox panorama, 2:1 aspect, "
    "no text, no watermarks, no figures."
)


@app.post("/api/skybox/generate")
async def skybox_generate(request: Request):
    """T2I skybox via Comfy Cloud's Nano Banana Pro partner API. Prepends the
    backend skybox system prompt so the user's brief gets the equirectangular
    guardrails without having to type them. Returns the same {filename, path,
    ext} shape as /api/run/<module> so the frontend can reuse its existing
    _applySkyboxTexture call."""
    if not COMFY_BIN:
        raise HTTPException(500, "comfy CLI not resolved — install comfy-cli in the app's venv")
    body = await request.json()
    node_id = str(body.get("node_id", "")).strip() or "default"
    user_prompt = (body.get("prompt") or "").strip()
    if not user_prompt:
        raise HTTPException(400, "prompt is required")
    # User's aesthetic brief FIRST so it carries the strongest token weight,
    # then the technical constraints as a short suffix.
    full_prompt = f"{user_prompt}{_SKYBOX_SYSTEM_PROMPT_SUFFIX}"
    # Reuse the same asset directory pattern the other modules use so the
    # generated image lands in the user's Assets pane alongside their other
    # renders (via /output/<filename>). Flux 2 lets us specify --width/--height
    # explicitly — we ask for 2048×1024 (a native 2:1 equirectangular aspect)
    # so the model doesn't guess and paint a square that then reads warped
    # when the client wraps it around the sphere.
    from server.modules._base import new_output_path, run_cli
    out = new_output_path(DATA_DIR, "skybox-flux2", "png")
    code, stdout, stderr = await run_cli([
        COMFY_BIN, "generate", "flux-2",
        "--prompt", full_prompt,
        "--width", "2048",
        "--height", "1024",
        "--download", str(out),
    ], timeout=600)
    if code != 0 or not out.exists():
        # Surface the last chunk of stderr so the client can render something
        # actionable — usually a partner-quota error from BFL or an unknown
        # flag if the CLI's `flux-2` schema drifted. `comfy generate schema
        # flux-2` from a terminal is the shortest way to check current args.
        detail = (stderr.strip() or stdout.strip() or f"comfy generate failed (rc={code})")
        raise HTTPException(500, detail[-800:])
    # Return a canonical URL that respects new_output_path's ext-partitioned
    # subfolders (images/ / videos/ / 3d/). Client used to construct
    # `/output/${filename}` and 404 because the file actually lives at
    # `/output/images/<filename>`.
    url = "/output/" + out.relative_to(DATA_DIR).as_posix()
    return {"filename": out.name, "path": str(out), "url": url, "ext": "png"}


# ── Heightmap generation (T2I via Comfy Cloud) ────────────────────────
# Terrain object's Heightmap → Generate button posts here. Same shape as
# the skybox endpoint — user types the aesthetic ("misty pine forest at
# dawn") and the server appends the grayscale + orthographic guardrails
# so the resulting image samples correctly as elevation. Square aspect
# with 1024×1024 dimensions matches the PlaneGeometry the client wraps
# it around — the client samples R channel per vertex.
_HEIGHTMAP_SYSTEM_PROMPT_SUFFIX = (
    ". Grayscale heightmap, top-down orthographic view, "
    "white areas are high elevation and black areas are low elevation, "
    "smooth gradient between elevations, no text, no watermarks, no color."
)


@app.post("/api/heightmap/generate")
async def heightmap_generate(request: Request):
    """T2I heightmap via Comfy Cloud partner API. Same pattern as
    /api/skybox/generate — server owns the domain-specific prompt suffix
    (grayscale + orthographic + elevation semantics) so the client just
    passes the user's brief."""
    if not COMFY_BIN:
        raise HTTPException(500, "comfy CLI not resolved — install comfy-cli in the app's venv")
    body = await request.json()
    node_id = str(body.get("node_id", "")).strip() or "default"
    user_prompt = (body.get("prompt") or "").strip()
    if not user_prompt:
        raise HTTPException(400, "prompt is required")
    full_prompt = f"{user_prompt}{_HEIGHTMAP_SYSTEM_PROMPT_SUFFIX}"
    from server.modules._base import new_output_path, run_cli
    out = new_output_path(DATA_DIR, "heightmap-flux2", "png")
    code, stdout, stderr = await run_cli([
        COMFY_BIN, "generate", "flux-2",
        "--prompt", full_prompt,
        "--width", "1024",
        "--height", "1024",
        "--download", str(out),
    ], timeout=600)
    if code != 0 or not out.exists():
        detail = (stderr.strip() or stdout.strip() or f"comfy generate failed (rc={code})")
        raise HTTPException(500, detail[-800:])
    # Canonical URL respecting the ext-partitioned subfolder (heightmap lands
    # under output/images/). Client fallback to /output/${filename} used to
    # 404 for the same reason skybox generate did.
    url = "/output/" + out.relative_to(DATA_DIR).as_posix()
    return {"filename": out.name, "path": str(out), "url": url, "ext": "png"}


# ── Camera Track (mockup) ──────────────────────────────────────────────
# Phase 1: accepts a video, ignores it, returns a placeholder circular-orbit
# trajectory in the same shape the frontend uses for camera keyframes:
#   [{ t, pos: [x,y,z], target: [x,y,z], up: [x,y,z], fov, ease }]
# Phase 2 will swap the placeholder for a real solver (DUSt3R via Comfy
# workflow, Blender headless, or MegaSAM). Keeping the endpoint shape stable
# means the frontend Apply-to-Camera flow doesn't change when solvers swap.
@app.post("/api/camera_track")
async def camera_track(request: Request) -> dict:
    import math
    form = await request.form()
    video = form.get("video")
    # Frontend probes duration via <video>.duration and forwards it — the
    # server-side alternative would be spawning ffprobe on the upload, which
    # is fine but heavier. Fallback 5s covers the "duration missing" case.
    try:
        duration_s = float(form.get("duration") or 5.0)
    except (TypeError, ValueError):
        duration_s = 5.0
    duration_s = max(0.5, min(60.0, duration_s))
    filename = None
    if hasattr(video, "filename"):
        filename = video.filename
        # Phase 2 will persist and hand this to the solver. For Phase 1 we
        # just consume the bytes so upload completes cleanly.
        _blob = await video.read()
        _ = len(_blob)
    # One keyframe per source frame at 30fps — real solvers emit pose per
    # frame, so density here should match. Keeps the timeline realistic
    # (a 10s clip gives 300 keys, not 60) and validates timeline perf ahead
    # of Phase 2. Camera orbits (0,0,0) at radius 5m, height 1.5m, full 360°.
    FPS = 30
    N = max(2, int(round(duration_s * FPS)))
    R = 5.0
    H = 1.5
    keyframes = []
    for i in range(N):
        u = i / (N - 1)
        t = u * duration_s
        theta = u * 2.0 * math.pi
        keyframes.append({
            "t": round(t, 4),
            "pos": [round(R * math.cos(theta), 4), round(H, 4), round(R * math.sin(theta), 4)],
            "target": [0.0, 0.0, 0.0],
            "up": [0.0, 1.0, 0.0],
            "fov": 45,
            "ease": "linear",
        })
    return {
        "ok": True,
        "solver": "placeholder-orbit",
        "filename": filename,
        "duration": duration_s,
        "fps": FPS,
        "keyframes": keyframes,
    }


# ── Splat editing / compression (@playcanvas/splat-transform) ─────────
# Shells out to `npx @playcanvas/splat-transform` to run whitelisted actions
# on a splat file (decimate, filter-nan, morton-order, filter-floaters,
# filter-box, translate/rotate/scale) and/or convert to a compressed format
# (sog / compressed.ply / spz). First run downloads the package via npx and
# takes ~30s; subsequent runs are near-instant. Requires Node.js on PATH.
_SPLAT_XFORM_TIMEOUT = 600

# Whitelist — restricts what shell flags the endpoint will emit. Keeps
# arbitrary CLI options out of user-controllable data.
def _splat_action_to_args(action: dict) -> list[str]:
    op = str(action.get("op", "")).strip()
    if op == "filter_nan":
        return ["--filter-nan"]
    if op == "morton":
        return ["--morton-order"]
    if op == "filter_floaters":
        return ["--filter-floaters"]
    if op == "filter_harmonics":
        band = int(action.get("band", 0))
        if band < 0 or band > 3:
            raise HTTPException(400, "filter_harmonics band must be 0..3")
        return ["--filter-harmonics", str(band)]
    if op == "decimate":
        pct = float(action.get("percent", 100))
        if pct <= 0 or pct >= 100:
            raise HTTPException(400, "decimate percent must be in (0, 100)")
        return ["--decimate", f"{pct:g}%"]
    if op == "filter_box":
        mn = action.get("min") or []
        mx = action.get("max") or []
        if len(mn) != 3 or len(mx) != 3:
            raise HTTPException(400, "filter_box requires min[3] and max[3]")
        coords = [float(v) for v in list(mn) + list(mx)]
        return ["--filter-box", ",".join(f"{c:g}" for c in coords)]
    if op == "translate":
        v = [float(action.get(k, 0)) for k in ("x", "y", "z")]
        return ["--translate", ",".join(f"{c:g}" for c in v)]
    if op == "rotate":
        v = [float(action.get(k, 0)) for k in ("x", "y", "z")]
        return ["--rotate", ",".join(f"{c:g}" for c in v)]
    if op == "scale":
        factor = float(action.get("factor", 1))
        return ["--scale", f"{factor:g}"]
    raise HTTPException(400, f"unknown splat-transform op: {op}")


def _resolve_splat_src(body: dict) -> Path:
    """Accept src_path (absolute) or src_url. src_url may be:
      * /output/<...>            — generation output under DATA_DIR
      * /comfyblockout/asset/<node_id>/<file> — per-node upload cache
                                                 (routes through _asset_dir)
    Reject anything that escapes DATA_DIR to prevent path traversal."""
    src_path = body.get("src_path")
    src_url = body.get("src_url")
    if src_path:
        p = Path(str(src_path)).resolve()
    elif src_url:
        rel = str(src_url).lstrip("/")
        if rel.startswith("output/"):
            rel = rel[len("output/"):]
            p = (DATA_DIR / rel).resolve()
        elif rel.startswith("comfyblockout/asset/"):
            # Route to _asset_dir(node_id) rather than DATA_DIR + rel — the
            # HTTP endpoint at /comfyblockout/asset/<node>/<file> reads from
            # DATA_DIR/assets/<node>/<file>, not the literal URL path. Splat
            # tools invoked on a user-imported PLY / SPZ hit this branch.
            parts = rel[len("comfyblockout/asset/"):].split("/", 1)
            if len(parts) != 2 or not parts[0] or not parts[1]:
                raise HTTPException(400, f"bad asset src_url: {src_url}")
            node_id, asset_name = parts
            p = (_asset_dir(node_id) / asset_name).resolve()
        else:
            p = (DATA_DIR / rel).resolve()
    else:
        raise HTTPException(400, "src_path or src_url required")
    try:
        p.relative_to(DATA_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "src must live under output/")
    if not p.exists():
        raise HTTPException(404, f"splat not found: {p}")
    return p


def _resolve_npx() -> str:
    """Windows exposes npx as npx.cmd; POSIX as plain npx. Fall through to
    shutil.which so PATH lookup handles both."""
    exe = shutil.which("npx") or shutil.which("npx.cmd")
    if not exe:
        raise HTTPException(
            500,
            "Node.js is required for splat editing/compression — install from "
            "nodejs.org, then reopen the app. The `npx` command must be on PATH.",
        )
    return exe


def _resolve_node() -> str | None:
    return shutil.which("node") or shutil.which("node.exe")


# Marker file written after the first successful `npx @playcanvas/splat-transform
# --version` run. Its presence means the package is in npx's local cache and
# subsequent calls skip the ~30s download. Cheaper than shelling out to inspect
# npm's cache layout, which varies across Node installers.
_SPLAT_PREFETCH_MARKER = DATA_DIR / ".splat_transform_prefetched"


@app.get("/api/splat/requirements")
async def splat_requirements():
    """Report whether Node.js is on PATH and whether we've warmed the
    splat-transform package cache. Frontend uses this to show an inline
    "Install Node.js" card in the Splat Tools panel when node is missing,
    and to decide whether to fire a background prefetch on panel open."""
    node = _resolve_node()
    node_ok = node is not None
    node_version = ""
    if node_ok:
        try:
            r = subprocess.run(
                [node, "--version"], capture_output=True, text=True, timeout=5,
            )
            node_version = (r.stdout or "").strip()
        except Exception:
            node_ok = False
    return {
        "node": {
            "ok": node_ok,
            "version": node_version,
            "install_url": "https://nodejs.org/en/download",
        },
        "splat_transform": {
            # `cached` = we've run the prefetch at least once. Not a hard
            # guarantee (user could clear their npx cache) but a strong hint
            # that the next transform call won't stall on a first-use download.
            "cached": _SPLAT_PREFETCH_MARKER.exists(),
        },
    }


@app.post("/api/splat/save_ply")
async def splat_save_ply(request: Request):
    """Persist a client-generated PLY (post-lasso-delete filtered bytes) into
    output/3d/ so the splat viewer can fetch it via a normal URL. Body is the
    raw PLY bytes; no re-parsing on the server. Path traversal is impossible
    because the filename is generated here, not taken from the client."""
    body = await request.body()
    if not body or len(body) < 128:
        raise HTTPException(400, "empty or truncated PLY payload")
    (DATA_DIR / "3d").mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = DATA_DIR / "3d" / f"splat_lasso_{stamp}_{uuid.uuid4().hex[:6]}.ply"
    dst.write_bytes(body)
    return {
        "filename": dst.name,
        "path": str(dst),
        "url": "/output/" + dst.relative_to(DATA_DIR).as_posix(),
        "ext": "ply",
    }


@app.post("/api/splat/prefetch")
async def splat_prefetch():
    """Warm npx's cache by running `@playcanvas/splat-transform --version` once.
    First run downloads ~30MB and takes 15-30s; subsequent transform calls skip
    that stall. Frontend fires this in the background the first time the Splat
    Tools panel is opened, so the user rarely sees the download latency."""
    npx = _resolve_npx()
    from server.modules._base import run_cli
    code, stdout, stderr = await run_cli(
        [npx, "-y", "@playcanvas/splat-transform", "--version"],
        timeout=120,
    )
    if code != 0:
        detail = (stderr.strip() or stdout.strip() or f"prefetch failed (rc={code})")
        raise HTTPException(500, detail[-800:])
    try:
        _SPLAT_PREFETCH_MARKER.write_text(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} · {(stdout or '').strip()[:200]}",
            encoding="utf-8",
        )
    except Exception:
        pass
    return {"ok": True, "version": (stdout or "").strip()[:200]}


@app.post("/api/splat/transform")
async def splat_transform(request: Request):
    body = await request.json()
    src = _resolve_splat_src(body)
    output_ext = str(body.get("output_ext", "ply")).lower().lstrip(".")
    # Whitelist of output formats so a bad value can't turn into an arbitrary
    # filename. Extensions map straight to what splat-transform recognises.
    valid_out = {"ply", "compressed.ply", "sog", "spz", "webp", "glb", "csv"}
    if output_ext not in valid_out:
        raise HTTPException(400, f"output_ext must be one of {sorted(valid_out)}")
    actions = body.get("actions") or []
    if not isinstance(actions, list):
        raise HTTPException(400, "actions must be a list")

    from server.modules._base import run_cli
    npx = _resolve_npx()

    # Build output path directly — new_output_path's _EXT_KIND lookup doesn't
    # cover .sog / .compressed.ply, and 3d assets all belong in output/3d/
    # regardless of the specific format.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    subdir = "images" if output_ext == "webp" else "3d"
    (DATA_DIR / subdir).mkdir(parents=True, exist_ok=True)
    dst = DATA_DIR / subdir / f"splat_edit_{stamp}_{uuid.uuid4().hex[:6]}.{output_ext}"

    action_args: list[str] = []
    for a in actions:
        if not isinstance(a, dict):
            raise HTTPException(400, "each action must be an object")
        action_args.extend(_splat_action_to_args(a))

    cmd = [npx, "-y", "@playcanvas/splat-transform", str(src), *action_args, str(dst), "--overwrite"]
    code, stdout, stderr = await run_cli(cmd, timeout=_SPLAT_XFORM_TIMEOUT)
    if code != 0 or not dst.exists():
        detail = (stderr.strip() or stdout.strip() or f"splat-transform failed (rc={code})")
        raise HTTPException(500, detail[-800:])

    url = "/output/" + dst.relative_to(DATA_DIR).as_posix()
    return {"filename": dst.name, "path": str(dst), "url": url, "ext": output_ext}


@app.post("/api/motion/install")
async def motion_install_animoflow():
    """Clone the AnimoFlow repo into APP_DIR/tools/animoflow. Runs synchronously
    with a hard timeout — clone is a few dozen MB so it should complete inside
    2 minutes on a normal connection. Returns success even if the folder was
    already present (idempotent) so the frontend can re-trigger without
    thinking about state."""
    import subprocess
    if _animoflow_installed():
        return {"success": True, "already_installed": True, "path": str(_ANIMOFLOW_DIR)}
    _ANIMOFLOW_DIR.parent.mkdir(parents=True, exist_ok=True)
    # If the target dir exists but isn't a git repo (partial prior attempt),
    # bail early with a clear message rather than letting git error confusingly.
    if _ANIMOFLOW_DIR.exists() and not (_ANIMOFLOW_DIR / ".git").exists():
        raise HTTPException(400, f"target exists but isn't a git repo: {_ANIMOFLOW_DIR} — delete it and retry")
    try:
        r = subprocess.run(
            ["git", "clone", "--depth", "1", _ANIMOFLOW_REPO_URL, str(_ANIMOFLOW_DIR)],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip()[:400]
            raise HTTPException(500, f"git clone failed: {msg}")
    except FileNotFoundError:
        raise HTTPException(
            500,
            "git not found on PATH — install Git for Windows (https://git-scm.com/download/win) then Re-check.",
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "git clone timed out after 180s — check your connection")
    return {"success": True, "path": str(_ANIMOFLOW_DIR)}


# ---------- workflow import (upload + AI-analyze) ----------
#
# The WORKFLOW section's "+ Import workflow" chip uploads a ComfyUI workflow
# JSON here, then streams the AI-analyze step over SSE so the user can watch
# the agent inspect the graph, propose input mappings, and register the module
# — no server restart needed.

_WORKFLOWS_DIR = APP_DIR / "server" / "workflows"
_SAFE_WF_STEM = re.compile(r"^[a-zA-Z0-9_\-]+$")


def _sanitize_workflow_stem(filename: str) -> str:
    """Turn an arbitrary upload filename into a filesystem-safe stem. Collisions
    get a numeric suffix so re-uploading the same workflow doesn't clobber a
    manifest the user already tuned."""
    stem = Path(filename).stem or "workflow"
    stem = re.sub(r"[^a-zA-Z0-9_\-]", "_", stem).strip("_") or "workflow"
    _WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    candidate = stem
    i = 1
    while (_WORKFLOWS_DIR / f"{candidate}.json").exists():
        i += 1
        candidate = f"{stem}_{i}"
    return candidate


@app.post("/api/workflows/upload")
async def workflows_upload(file: UploadFile = File(...)):
    """Save a picked workflow JSON into server/workflows/. Returns the stem the
    client should pass to /api/workflows/analyze. The manifest is written by
    the analyze step, not here — this endpoint just parks the file."""
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty upload")
    try:
        wf = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise HTTPException(400, f"not a valid JSON workflow: {e}")
    if not isinstance(wf, dict) or "nodes" not in wf:
        raise HTTPException(400, "JSON has no `nodes` array — not a ComfyUI workflow?")
    stem = _sanitize_workflow_stem(file.filename or "workflow")
    dst = _WORKFLOWS_DIR / f"{stem}.json"
    dst.write_text(json.dumps(wf, indent=2), encoding="utf-8")
    return {
        "stem": stem,
        "filename": dst.name,
        "node_count": len(wf.get("nodes", [])),
    }


_SAFE_MODULE_ID = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


@app.get("/api/workflows/{module_id}/source")
async def workflows_get_source(module_id: str):
    """Return the manifest + raw workflow JSON for an already-registered
    workflow module. Used by the AI Agent's `get_workflow_module` tool so it
    can inspect what's actually saved before proposing a fix — the alternative
    is Claude hallucinating what the workflow "probably" looks like."""
    if not _SAFE_MODULE_ID.match(module_id):
        raise HTTPException(400, "invalid module id")
    mod = MODULES.get(module_id)
    if mod is None:
        raise HTTPException(404, f"unknown module: {module_id}")
    if getattr(mod, "source", "python") != "workflow":
        raise HTTPException(403, "not a workflow module")
    wf_path = _WORKFLOWS_DIR / f"{module_id}.json"
    meta_path = _WORKFLOWS_DIR / f"{module_id}.meta.json"
    if not wf_path.exists() or not meta_path.exists():
        raise HTTPException(500, "workflow or manifest missing on disk")
    return {
        "id": module_id,
        "manifest": json.loads(meta_path.read_text(encoding="utf-8")),
        "workflow": json.loads(wf_path.read_text(encoding="utf-8")),
    }


@app.delete("/api/workflows/{module_id}")
async def workflows_delete(module_id: str):
    """Permanently delete a workflow module — removes the `<id>.json` and
    `<id>.meta.json` from server/workflows/ and unregisters the entry from
    MODULES. Refuses to touch hand-written Python modules (source="python")
    since those live in code, not on the workflows/ side.

    Called by the frontend when the user clicks × on a WORKFLOW cell — that
    action is destructive by design so re-boots don't resurrect the module
    unless the user asks the agent to build it again."""
    if not _SAFE_MODULE_ID.match(module_id):
        raise HTTPException(400, "invalid module id")
    mod = MODULES.get(module_id)
    if mod is None:
        raise HTTPException(404, f"unknown module: {module_id}")
    if getattr(mod, "source", "python") != "workflow":
        raise HTTPException(403, "refuse to delete built-in module")

    # Best-effort file removal — a manifest missing on disk when we get here
    # (already deleted, or renamed by hand) shouldn't stop us unregistering
    # the runtime entry.
    for name in (f"{module_id}.json", f"{module_id}.meta.json"):
        p = _WORKFLOWS_DIR / name
        if p.exists():
            try:
                p.unlink()
            except OSError as e:
                print(f"[cb-app] failed to remove {p}: {e}")
    # Also try the `<id>.local.json` companion the "prepare for local" flow
    # would write; safe no-op if it doesn't exist.
    local_p = _WORKFLOWS_DIR / f"{module_id}.local.json"
    if local_p.exists():
        try:
            local_p.unlink()
        except OSError:
            pass

    MODULES.pop(module_id, None)
    return {"deleted": module_id}


_LOCAL_COMFY_URL = "http://127.0.0.1:8188"


# ComfyUI models directory — first existing candidate wins. Override with
# COMFY_MODELS_DIR for non-standard installs. Kept parallel to
# `local_triposplat.COMFY_OUTPUT_CANDIDATES` so the two "where is ComfyUI"
# questions get answered consistently.
def _find_local_models_dir() -> Path | None:
    env_v = os.environ.get("COMFY_MODELS_DIR", "").strip()
    candidates: list[Path] = [Path(env_v)] if env_v else []
    candidates += [
        Path(r"H:\ComfyUI-Easy-Install\ComfyUI\models"),
        Path(r"H:\Comfy-Desktop\ComfyUI-Installs\ComfyDesktop\ComfyUI\models"),
        Path(r"H:\ComfyUI_windows_portable\ComfyUI\models"),
        Path(r"H:\Krita\ComfyUI\ComfyUI\models"),
    ]
    for p in candidates:
        try:
            if p.exists() and p.is_dir():
                return p
        except OSError:
            continue
    return None


_SAFE_MODEL_FILENAME = re.compile(r"^[A-Za-z0-9._\-]+$")
_SAFE_MODEL_FOLDER = re.compile(r"^[A-Za-z0-9._\-/]+$")


def _find_local_python() -> Path | None:
    """Locate ComfyUI's embedded Python interpreter. Non-technical users can't
    be expected to know which `python` on PATH matches their ComfyUI env — so
    we sniff the same install roots we use for models/custom_nodes and look
    for the standard portable/embedded layouts (python_embeded, venv/Scripts,
    etc.). COMFY_PYTHON_EXE env override wins for weird layouts."""
    env_v = os.environ.get("COMFY_PYTHON_EXE", "").strip()
    if env_v:
        p = Path(env_v)
        if p.exists() and p.is_file():
            return p
    candidates: list[Path] = []
    models_dir = _find_local_models_dir()
    roots: list[Path] = []
    if models_dir:
        # ComfyUI root is the parent of `models/`. Two layers up covers the
        # Easy-Install-style `ComfyUI-Easy-Install/ComfyUI/models` where the
        # portable python sits at `ComfyUI-Easy-Install/python_embeded/`.
        roots.append(models_dir.parent)
        roots.append(models_dir.parent.parent)
    roots.extend([
        Path(r"H:\ComfyUI-Easy-Install"),
        Path(r"H:\ComfyUI-Easy-Install\ComfyUI"),
        Path(r"H:\Comfy-Desktop\ComfyUI-Installs\ComfyDesktop\ComfyUI"),
        Path(r"H:\ComfyUI_windows_portable"),
        Path(r"H:\ComfyUI_windows_portable\ComfyUI"),
        Path(r"H:\Krita\ComfyUI\ComfyUI"),
    ])
    rel_paths = [
        Path("python_embeded/python.exe"),
        Path("python_embedded/python.exe"),
        Path("venv/Scripts/python.exe"),
        Path(".venv/Scripts/python.exe"),
        Path("python/python.exe"),
        Path("bin/python.exe"),
        # POSIX
        Path("python_embeded/bin/python"),
        Path("venv/bin/python"),
        Path(".venv/bin/python"),
    ]
    for root in roots:
        for rel in rel_paths:
            p = root / rel
            candidates.append(p)
    for c in candidates:
        try:
            if c.exists() and c.is_file():
                return c
        except OSError:
            continue
    return None


def _find_local_custom_nodes_dir() -> Path | None:
    """Locate `<ComfyUI>/custom_nodes/`. Same candidate list as models —
    custom_nodes and models are siblings under the ComfyUI root — plus a
    COMFY_CUSTOM_NODES_DIR override for exotic layouts."""
    env_v = os.environ.get("COMFY_CUSTOM_NODES_DIR", "").strip()
    candidates: list[Path] = [Path(env_v)] if env_v else []
    models_dir = _find_local_models_dir()
    if models_dir:
        candidates.append(models_dir.parent / "custom_nodes")
    # Fall back to the same hardcoded roots, appending /custom_nodes.
    for hard in (
        Path(r"H:\ComfyUI-Easy-Install\ComfyUI\custom_nodes"),
        Path(r"H:\Comfy-Desktop\ComfyUI-Installs\ComfyDesktop\ComfyUI\custom_nodes"),
        Path(r"H:\ComfyUI_windows_portable\ComfyUI\custom_nodes"),
        Path(r"H:\Krita\ComfyUI\ComfyUI\custom_nodes"),
    ):
        candidates.append(hard)
    for p in candidates:
        try:
            if p.exists() and p.is_dir():
                return p
        except OSError:
            continue
    return None


@app.post("/api/local/check-custom-nodes")
async def local_check_custom_nodes(request: Request):
    """Query local /object_info and report which of the requested class_types
    the local ComfyUI doesn't know about. Used before firing a workflow so the
    agent can install missing custom nodes before ComfyUI errors out at run
    time with a cryptic KeyError.

    Body: {"class_types": ["TripoSplatToFile3D", "TripoAPI", ...]}"""
    body = await request.json()
    class_types = body.get("class_types") or []
    if not isinstance(class_types, list):
        raise HTTPException(400, "class_types must be a list")

    import httpx
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(f"{_LOCAL_COMFY_URL}/object_info")
            r.raise_for_status()
            oi = r.json()
    except Exception as e:
        return {
            "reachable": False,
            "error": f"Local ComfyUI unreachable at {_LOCAL_COMFY_URL}: {e}",
            "missing": [str(ct) for ct in class_types if ct],
            "present": [],
        }

    known = set((oi or {}).keys())
    present, missing = [], []
    for ct in class_types:
        s = str(ct).strip()
        if not s:
            continue
        (present if s in known else missing).append(s)
    return {"reachable": True, "missing": missing, "present": present}


_SAFE_REPO_NAME = re.compile(r"^[A-Za-z0-9._\-]{1,120}$")
_GIT_URL_RE = re.compile(r"^https?://[A-Za-z0-9._\-]+(?:/[A-Za-z0-9._\-/]+)+?(?:\.git)?$")


async def _stream_process(args: list[str]):
    """Run a subprocess in a background thread and yield ('line', str) events
    as output streams, then a final ('done', int) or ('error', str) event.

    Why not `asyncio.create_subprocess_exec`? On Windows, uvicorn's --reload
    mode uses SelectorEventLoopPolicy inside the reloader subprocess, and
    SelectorEventLoop can't spawn processes — every call raises
    NotImplementedError. Setting Proactor policy at module import doesn't
    stick past uvicorn's supervisor. A blocking `subprocess.Popen` in a
    daemon thread sidesteps the whole event-loop compatibility problem —
    line reads happen off-loop, and a threadsafe put pushes them onto a
    Queue the async caller awaits."""
    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue()

    def _put(item):
        loop.call_soon_threadsafe(q.put_nowait, item)

    def _runner():
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                _put(("line", line.rstrip()))
            proc.wait()
            _put(("done", proc.returncode))
        except Exception as e:
            _put(("error", str(e)))

    threading.Thread(target=_runner, daemon=True).start()
    while True:
        item = await q.get()
        yield item
        if item[0] in ("done", "error"):
            return


@app.post("/api/local/install-custom-node")
async def local_install_custom_node(request: Request):
    """SSE stream that runs `git clone <url> <custom_nodes>/<name>` in the
    user's ComfyUI install. If the repo ships a requirements.txt, we surface
    it in the final event so the agent can tell the user what to `pip install`
    (we do NOT auto-pip because the ComfyUI Python env is usually not the same
    as ours, and installing into the wrong venv is worse than doing nothing).

    Body: {"git_url": "https://github.com/foo/bar", "name": "bar" (optional)}"""
    body = await request.json()
    git_url = str(body.get("git_url", "")).strip()
    name = str(body.get("name", "")).strip()
    # force=true → nuke any existing folder and re-clone. Used when a prior
    # install landed in a broken state (partial clone, wrong ComfyUI, deps
    # never installed, etc.) and the agent wants a clean-slate retry.
    force = bool(body.get("force", False))
    if not git_url or not _GIT_URL_RE.match(git_url):
        raise HTTPException(400, "git_url must be a public https/http git URL")
    # Derive name from URL when not supplied. Strip trailing .git and pull the
    # last path segment — standard `git clone` naming.
    if not name:
        stem = git_url.rstrip("/")
        if stem.endswith(".git"):
            stem = stem[:-4]
        name = stem.rsplit("/", 1)[-1] or "custom_node"
    if not _SAFE_REPO_NAME.match(name):
        raise HTTPException(400, "invalid derived repo name")

    cn_dir = _find_local_custom_nodes_dir()
    if not cn_dir:
        raise HTTPException(
            500,
            "ComfyUI custom_nodes directory not found. Set COMFY_CUSTOM_NODES_DIR "
            "in .env to the path (typically <ComfyUI>/custom_nodes).",
        )
    dst = cn_dir / name

    async def _run_pip(req_path: Path):
        """Run pip against ComfyUI's own Python. Yields SSE progress events
        AND captures the last chunk of output so a non-zero exit can surface
        the actual failing package to the agent."""
        py = _find_local_python()
        if not py:
            yield ("evt", _sse("progress", message="requirements.txt found but ComfyUI's Python interpreter wasn't detected — user will need to run pip manually"))
            yield ("result", False, None, "python interpreter not found")
            return
        # Pre-flight: sanity-check the interpreter itself.
        yield ("evt", _sse("progress", message=f"Verifying {py}"))
        probe_lines: list[str] = []
        probe_rc: int | None = None
        async for kind, payload in _stream_process([str(py), "-m", "pip", "--version"]):
            if kind == "line" and payload:
                probe_lines.append(payload)
            elif kind == "done":
                probe_rc = payload
            elif kind == "error":
                yield ("result", True, False, f"failed to invoke {py}: {payload}")
                return
        if probe_rc != 0:
            probe_text = "\n".join(probe_lines) or f"rc={probe_rc}"
            yield ("evt", _sse("progress", message=f"pip probe failed: {probe_text}"))
            yield ("result", True, False, f"pip is not usable in {py}: {probe_text}")
            return
        yield ("evt", _sse("progress", message=(probe_lines[0] if probe_lines else "pip available")))
        yield ("evt", _sse("progress", message=f"Installing requirements from {req_path}"))
        # Ring-buffer the last N lines so we can attach them to a failure
        # report — pip's actual error line is usually within the last ~30.
        tail: list[str] = []
        prc: int | None = None
        async for kind, payload in _stream_process([str(py), "-m", "pip", "install", "-r", str(req_path)]):
            if kind == "line":
                if payload:
                    yield ("evt", _sse("progress", message=payload))
                    tail.append(payload)
                    if len(tail) > 60:
                        tail.pop(0)
            elif kind == "done":
                prc = payload
            elif kind == "error":
                yield ("result", True, False, str(payload))
                return
        if prc == 0:
            yield ("result", True, True, None)
        else:
            if not tail:
                yield ("result", True, False, f"pip exited with rc={prc} and produced no output — the interpreter or the requirements file may be corrupt")
                return
            summary_lines = [l for l in tail if l.startswith(("ERROR", "WARNING")) or "not found" in l.lower() or "conflict" in l.lower() or "no matching distribution" in l.lower()]
            if not summary_lines:
                summary_lines = tail[-20:]
            summary = "\n".join(summary_lines[-20:])
            yield ("result", True, False, f"pip exited with rc={prc}\n{summary}")

    async def gen():
        try:
            # Force: nuke the existing folder so the clone-from-scratch path
            # runs. Common recovery when a prior install landed in the wrong
            # ComfyUI (Easy Install vs Desktop) or half-succeeded and left
            # deps unresolvable in-place.
            if force and dst.exists():
                yield _sse("progress", message=f"force=true — removing existing {dst}")
                try:
                    shutil.rmtree(dst)
                except Exception as e:
                    yield _sse("error", message=f"failed to remove {dst}: {e}")
                    return
            if dst.exists():
                yield _sse("progress", message=f"{name} already present at {dst} — running pip in case deps were never installed")
                req = dst / "requirements.txt"
                pip_ran = False
                pip_ok = None
                pip_error = None
                if req.exists():
                    async for item in _run_pip(req):
                        if item[0] == "evt":
                            yield item[1]
                        else:
                            _, pip_ran, pip_ok, pip_error = item
                yield _sse(
                    "done",
                    message=f"already installed at {dst}" + (" + pip installed requirements" if pip_ok else ""),
                    path=str(dst),
                    requirements=str(req) if req.exists() else None,
                    pip_ran=pip_ran,
                    pip_ok=pip_ok,
                    pip_error=pip_error,
                    # Even a re-pip means ComfyUI needs to restart to pick up
                    # any newly-installed classes.
                    restart_required=bool(pip_ok),
                )
                return

            yield _sse("progress", message=f"Cloning {git_url} into {dst}")
            git = shutil.which("git")
            if not git:
                yield _sse("error", message="`git` not on PATH — install Git for Windows or add it to PATH.")
                return

            # Stream git output as-is so the user watches "Receiving objects: 42%..."
            # etc. instead of just staring at a spinner. _stream_process runs
            # the clone in a worker thread — works under any event loop policy.
            rc: int | None = None
            async for kind, payload in _stream_process([git, "clone", "--depth=1", git_url, str(dst)]):
                if kind == "line":
                    if payload:
                        yield _sse("progress", message=payload)
                elif kind == "done":
                    rc = payload
                elif kind == "error":
                    if dst.exists():
                        try: shutil.rmtree(dst)
                        except OSError: pass
                    yield _sse("error", message=f"git clone failed: {payload}")
                    return
            if rc != 0:
                # Clean up partial clone so a retry doesn't hit "already present".
                if dst.exists():
                    try:
                        shutil.rmtree(dst)
                    except OSError:
                        pass
                yield _sse("error", message=f"git clone failed (rc={rc})")
                return

            req = dst / "requirements.txt"
            requirements_preview = None
            pip_ran = False
            pip_ok = None
            pip_error = None
            if req.exists():
                try:
                    lines = [l.strip() for l in req.read_text(encoding="utf-8").splitlines() if l.strip() and not l.strip().startswith("#")]
                    requirements_preview = lines[:20]
                except Exception:
                    pass
                # Auto-run pip against ComfyUI's own Python interpreter. This
                # is the critical UX unlock for non-technical users — cloning
                # the repo without installing its deps means ComfyUI won't
                # register the custom node classes, and the whole "install
                # then restart" loop stalls.
                py = _find_local_python()
                if not py:
                    yield _sse("progress", message="requirements.txt found but ComfyUI's Python interpreter wasn't detected — will report as manual step")
                else:
                    yield _sse("progress", message=f"Installing requirements with {py}")
                    pip_ran = True
                    fresh_prc: int | None = None
                    fresh_err: str | None = None
                    async for kind, payload in _stream_process([str(py), "-m", "pip", "install", "-r", str(req)]):
                        if kind == "line":
                            if payload:
                                yield _sse("progress", message=payload)
                        elif kind == "done":
                            fresh_prc = payload
                        elif kind == "error":
                            fresh_err = str(payload)
                    if fresh_err:
                        pip_ok = False
                        pip_error = fresh_err
                    else:
                        pip_ok = fresh_prc == 0
                        if not pip_ok:
                            pip_error = f"pip exited with rc={fresh_prc}"

            yield _sse(
                "done",
                message=f"Cloned to {dst}" + (" + pip installed requirements" if pip_ok else ""),
                path=str(dst),
                requirements=str(req) if req.exists() else None,
                requirements_preview=requirements_preview,
                pip_ran=pip_ran,
                pip_ok=pip_ok,
                pip_error=pip_error,
                restart_required=True,
            )
        except Exception as e:
            if dst.exists() and not any(dst.iterdir()):
                try:
                    dst.rmdir()
                except OSError:
                    pass
            yield _sse("error", message=f"{type(e).__name__}: {e}")

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/local/download-model")
async def local_download_model(request: Request):
    """Stream a model download from a URL into ComfyUI's models/{folder}/ dir.
    SSE progress: {type: "progress", bytes, total, percent}, {type: "done", path},
    {type: "error", message}. Uses httpx.stream to avoid buffering the whole
    file in memory — matters for 4-8 GB safetensors.

    Body: {"url": "...", "folder": "diffusion_models", "filename": "flux-2-klein-4b.safetensors"}
    """
    body = await request.json()
    url = str(body.get("url", "")).strip()
    folder = str(body.get("folder", "")).strip()
    filename = str(body.get("filename", "")).strip()
    if not url or not url.startswith(("http://", "https://")):
        raise HTTPException(400, "url must be http(s)")
    if not filename or not _SAFE_MODEL_FILENAME.match(filename):
        raise HTTPException(400, "invalid filename")
    if not folder or not _SAFE_MODEL_FOLDER.match(folder):
        raise HTTPException(400, "invalid folder")
    if ".." in folder or folder.startswith("/"):
        raise HTTPException(400, "folder must be a relative subdir")

    models_dir = _find_local_models_dir()
    if not models_dir:
        raise HTTPException(
            500,
            "ComfyUI models directory not found. Set COMFY_MODELS_DIR in .env "
            "to your ComfyUI install's models/ path.",
        )

    dst_dir = models_dir / folder
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / filename
    # Write to a .part file and rename on success so a killed download doesn't
    # leave a truncated file that ComfyUI would happily try to load.
    tmp = dst.with_name(dst.name + ".part")

    async def gen():
        import httpx
        try:
            yield _sse("progress", message=f"Fetching {url}", bytes=0, total=0, percent=0)
            async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
                async with client.stream("GET", url) as r:
                    if r.status_code != 200:
                        yield _sse("error", message=f"HTTP {r.status_code} from {url}")
                        return
                    total = int(r.headers.get("content-length") or 0)
                    got = 0
                    last_pct = -1
                    with tmp.open("wb") as f:
                        async for chunk in r.aiter_bytes(chunk_size=1024 * 1024):
                            f.write(chunk)
                            got += len(chunk)
                            if total > 0:
                                pct = int(got * 100 / total)
                                if pct != last_pct:
                                    last_pct = pct
                                    yield _sse("progress", message=f"{pct}%",
                                               bytes=got, total=total, percent=pct)
                            else:
                                mb = got / (1024 * 1024)
                                yield _sse("progress", message=f"{mb:.1f} MB",
                                           bytes=got, total=0, percent=0)
            tmp.replace(dst)
            yield _sse("done", message=f"Saved to {dst}", path=str(dst))
        except Exception as e:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            yield _sse("error", message=f"{type(e).__name__}: {e}")

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/local/check-models")
async def local_check_models(request: Request):
    """Query the local ComfyUI's /object_info catalog and check which of the
    requested model filenames are already present. Reads every combo widget's
    choices list and treats any exact-match filename as "present" — path-
    agnostic, so shared model dirs configured via extra_model_paths.yaml just
    work.

    Body: {"models": [{"filename": "...", "folder": "diffusion_models"}, ...]}
    """
    body = await request.json()
    models = body.get("models") or []
    if not isinstance(models, list):
        raise HTTPException(400, "models must be a list")

    import httpx
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(f"{_LOCAL_COMFY_URL}/object_info")
            r.raise_for_status()
            oi = r.json()
    except Exception as e:
        return {
            "reachable": False,
            "error": f"Local ComfyUI unreachable at {_LOCAL_COMFY_URL}: {e}",
            "missing": [dict(m) for m in models if isinstance(m, dict)],
            "present": [],
        }

    # Flatten every combo choice across the catalog. ComfyUI reports model
    # widget lists as `[[choice1, choice2, ...], {tooltip: ...}]` under
    # object_info[class_type].input.required[widget_name]. Some choices are
    # nested paths ("subdir/file.safetensors") — add the basename too so a
    # user's manifest that says just "file.safetensors" still matches.
    known: set[str] = set()
    for _class_type, info in (oi or {}).items():
        if not isinstance(info, dict):
            continue
        input_spec = info.get("input") or {}
        for section in ("required", "optional"):
            for _name, spec in ((input_spec.get(section) or {}) or {}).items():
                if not isinstance(spec, list) or not spec:
                    continue
                choices = spec[0]
                if not isinstance(choices, list):
                    continue
                for choice in choices:
                    if not isinstance(choice, str):
                        continue
                    known.add(choice)
                    base = choice.replace("\\", "/").rsplit("/", 1)[-1]
                    if base and base != choice:
                        known.add(base)

    missing: list[dict] = []
    present: list[str] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        fn = str(m.get("filename", "")).strip()
        if not fn:
            continue
        if fn in known or fn.replace("\\", "/").rsplit("/", 1)[-1] in known:
            present.append(fn)
        else:
            missing.append({"filename": fn, "folder": m.get("folder", "")})

    return {"reachable": True, "missing": missing, "present": present}


@app.get("/api/local/widget-options")
async def local_widget_options(class_type: str, widget: str):
    """Return the current combo choice list for a specific node widget from
    local ComfyUI's /object_info. Manifest inputs with
    `options_source: {class_type, widget}` use this to render dropdowns that
    always reflect what's actually installed instead of a stale hardcoded list."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{_LOCAL_COMFY_URL}/object_info/{class_type}")
            r.raise_for_status()
            oi = r.json()
    except Exception as e:
        raise HTTPException(503, f"Local ComfyUI unreachable: {e}")
    info = (oi or {}).get(class_type)
    if not isinstance(info, dict):
        raise HTTPException(404, f"unknown class_type: {class_type}")
    input_spec = info.get("input") or {}
    for section in ("required", "optional"):
        specs = input_spec.get(section) or {}
        entry = specs.get(widget)
        if not isinstance(entry, list) or not entry:
            continue
        # Old format: `[[opt1, opt2, ...], {tooltip: ...}]` — choices at [0].
        if isinstance(entry[0], list):
            return {"options": entry[0]}
        # New format: `["COMBO", {options: [...], multiselect: false}]` — used
        # by newer ComfyUI core / custom nodes (SetUnionControlNetType, etc.).
        if entry[0] == "COMBO" and len(entry) > 1 and isinstance(entry[1], dict):
            opts = entry[1].get("options")
            if isinstance(opts, list):
                return {"options": opts}
    raise HTTPException(404, f"widget {widget} on {class_type} is not a combo")


@app.post("/api/workflows/register")
async def workflows_register(request: Request):
    """Save a workflow + its manifest atomically, then hot-register the module
    so it appears in the WORKFLOW section without a restart. Called by the AI
    Agent's `create_workflow_module` tool — the agent constructs a workflow
    (from its own knowledge or via the Comfy Cloud MCP), decides which node
    widgets should be user inputs, and posts the whole bundle here."""
    body = await request.json()
    mod_id = str(body.get("id", "")).strip()
    label = str(body.get("label", "")).strip() or mod_id
    kind = str(body.get("kind", "image")).strip().lower()
    output_ext = str(body.get("output_ext", "png")).strip().lower().lstrip(".")
    runner = str(body.get("runner", "cloud")).strip().lower() or "cloud"
    workflow = body.get("workflow")
    inputs = body.get("inputs") or []
    intermediates = body.get("intermediates") or []
    if not isinstance(intermediates, list):
        raise HTTPException(400, "intermediates must be a list")

    if not mod_id or not _SAFE_MODULE_ID.match(mod_id):
        raise HTTPException(400, "id must be snake_case, [a-zA-Z0-9_-] up to 64 chars")
    if kind not in ("image", "video", "3d", "audio"):
        raise HTTPException(400, "kind must be one of image/video/3d/audio")
    if runner not in ("cloud", "local"):
        raise HTTPException(400, "runner must be cloud or local")
    if not isinstance(workflow, dict) or not workflow:
        raise HTTPException(400, "workflow must be a non-empty JSON object")
    if not isinstance(inputs, list):
        raise HTTPException(400, "inputs must be a list")
    # Refuse to overwrite a hand-written Python module — those are the primary
    # generators and should never get shadowed by an agent-created one.
    if mod_id in MODULES and getattr(MODULES[mod_id], "source", "python") == "python":
        raise HTTPException(409, f"module id `{mod_id}` conflicts with a built-in generator")

    _WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    wf_path = _WORKFLOWS_DIR / f"{mod_id}.json"
    meta_path = _WORKFLOWS_DIR / f"{mod_id}.meta.json"

    manifest = {
        "id": mod_id,
        "label": label,
        "kind": kind,
        "output_ext": output_ext,
        "runner": runner,
        "inputs": inputs,
    }
    if intermediates:
        manifest["intermediates"] = intermediates
    # Utility flag + optional icon — set from the register payload so the
    # AI-agent create_workflow_module flow can register a workflow directly
    # as a Tools-grid button (bypassing the WORKFLOWS section).
    if body.get("util"):
        manifest["util"] = True
    icon = body.get("icon")
    if isinstance(icon, str) and icon.strip():
        manifest["icon"] = icon.strip()

    wf_path.write_text(json.dumps(workflow, indent=2), encoding="utf-8")
    meta_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    from server.modules._workflow_shared import register_manifest_by_name
    mod = register_manifest_by_name(mod_id)
    if not mod:
        raise HTTPException(500, "manifest saved but hot-register failed — restart to pick it up")
    MODULES[mod.id] = mod
    return {"module": _module_dict(mod), "manifest": manifest}


@app.post("/api/workflows/rename")
async def workflows_rename(request: Request):
    """Rename a workflow module's user-facing label — updates the `label`
    field in the module's meta.json in place and re-registers the module so
    the change picks up without a server restart. The `id` stays untouched
    (renaming that would require moving the JSON files + updating every
    saved cell that references it, out of scope for a quick rename)."""
    body = await request.json()
    mod_id = str(body.get("id", "")).strip()
    label = str(body.get("label", "")).strip()
    if not mod_id or not _SAFE_MODULE_ID.match(mod_id):
        raise HTTPException(400, "id must be snake_case, [a-zA-Z0-9_-] up to 64 chars")
    if not label:
        raise HTTPException(400, "label cannot be empty")
    if len(label) > 120:
        raise HTTPException(400, "label too long (max 120 chars)")
    meta_path = _WORKFLOWS_DIR / f"{mod_id}.meta.json"
    if not meta_path.exists():
        raise HTTPException(404, f"no manifest at {meta_path}")
    try:
        manifest = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"manifest is not valid JSON: {e}") from e
    manifest["label"] = label
    meta_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    from server.modules._workflow_shared import register_manifest_by_name
    mod = register_manifest_by_name(mod_id)
    if not mod:
        raise HTTPException(500, "manifest saved but hot-register failed — restart to pick it up")
    MODULES[mod.id] = mod
    return {"module": _module_dict(mod), "label": label}


# System prompt for the manifest-writer agent. The tool schema constrains the
# shape; this text explains WHICH node holds a user prompt vs a machine seed vs
# a load-image slot, so the model doesn't misroute.
_MANIFEST_ANALYZER_SYSTEM = """You analyze a ComfyUI workflow graph and emit a manifest that lets ComfyBlockout expose it as a generator.

The user drops a workflow JSON. You look at its nodes and decide:
1. Which nodes hold USER-FACING inputs — the ones the user should type/upload each time. Typically:
   - `CLIPTextEncode` (widget 0 = prompt text)
   - `PrimitiveStringMultiline` titled "Prompt" or similar (widget 0 = prompt)
   - `LoadImage` where the file would come from the user (widget 0 = image filename → use type "scene-image" so ComfyBlockout uploads the editor snapshot at run time)
   - `KSampler` seed (widget 0) IF it's obviously meant to be user-facing — usually leave seeds alone (workflow randomizer handles them).
2. What the OUTPUT kind is — look at the terminal save nodes (SaveImage → image; VHS_VideoCombine / SaveWEBM → video; SaveGLB → 3d; SaveAudio → audio). Pick the primary output kind.
3. A short `label` (title-cased human name) and `id` (snake_case). The client already picked an id from the filename; you can override if the filename was ugly.

Rules:
- SKIP nodes with `mode: 4` (bypassed) or `mode: 2` (muted) — they don't fire.
- If a node has a `title` (custom name the workflow author gave it), that's usually a strong hint about intent.
- If two nodes look like prompt inputs (e.g. a positive + negative CLIPTextEncode), map the POSITIVE one to `prompt` and leave the negative alone — the workflow's default negative is usually fine.
- Match `widget_index` to the widget position in `widgets_values` (0-based).
- For `patch.node_id`, use the numeric `id` field of the node.
- Prefer FEWER inputs over more. Only expose things the user must set for a useful generation.
- ALWAYS expose the KSampler `seed` widget via `type: "seed"` — the UI renders a 🎲/🔒 toggle so the user gets random-per-run by default, with the ability to lock a value they liked. This is not a "hide it" input; users want reproducibility.
- If the workflow has a ControlNet / IPAdapter / Loras strength that materially changes the output (typical range 0-1), expose it via `type: "number"` with `default`, `min: 0`, `max: 1`, `step: 0.05`. Same for guidance/CFG when the workflow author left it as a widget rather than baking it in.
- Comfy COMBO widgets (dropdowns of enumerated strings — AIO Aux Preprocessor's `preprocessor`, KSampler's `sampler_name`/`scheduler`, checkpoint pickers, etc.) MUST be exposed via `type: "dropdown"` with the full `options` array copied verbatim from the node's schema. Never fall back to a plain text field for these — users can't remember every valid string, and typos silently fail at runtime. Always set `default` to the workflow's current value so the picker opens on the same option the author picked.

4. Preprocessor previews (INTERMEDIATES). If the workflow has a preprocessor step that produces a visualisable image the user would want to see (Depth-Anything / Zoe / Marigold / MiDaS depth, Canny / HED / Lineart / Scribble edges, OpenPose, Normal, Seg, etc.), add an entry to `intermediates`:
   `{ name, label, source_node_id, source_slot }`
   The runner splices in a SaveImage for each, and the editor shows a preview tab (e.g. DEPTH) between BLOCKOUT and RENDER. `source_node_id` is the preprocessor node's numeric id and `source_slot` is which output socket (0 for its main image output). Skip this for workflows without a visual preprocessor pass.

Latent sizing note: for workflows with a `scene-image` input, DON'T worry about the EmptyLatentImage width/height — the runner auto-patches it at run time to match the source image's aspect ratio (snapped to /64) while preserving the authored long side. Whatever square (e.g. 1024×1024) the workflow ships with is fine; just don't hand-code a specific AR expecting it to survive.

When you're done, call the `write_manifest` tool with the final manifest. Do not narrate — the tool call is your entire response."""


_MANIFEST_TOOL = {
    "name": "write_manifest",
    "description": "Emit the finished manifest for the workflow.",
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "snake_case identifier (unique per workflow)"},
            "label": {"type": "string", "description": "Human-readable title for the UI"},
            "kind": {"type": "string", "enum": ["image", "video", "3d", "audio"]},
            "output_ext": {"type": "string", "description": "e.g. png, mp4, glb, wav"},
            "inputs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "kwarg name, snake_case"},
                        "type": {"type": "string", "enum": ["textarea", "text", "scene-image", "scene-video", "number", "seed", "dropdown"]},
                        "required": {"type": "boolean"},
                        "placeholder": {"type": "string"},
                        "label": {"type": "string"},
                        "default": {"description": "Optional default value for number/seed/dropdown fields."},
                        "min": {"type": "number"},
                        "max": {"type": "number"},
                        "step": {"type": "number"},
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "For type='dropdown' — list of allowed values shown as a <select> in the editor. Forwarded as a plain string to the widget.",
                        },
                        "patch": {
                            "type": "object",
                            "properties": {
                                "node_id": {"type": "integer"},
                                "widget_index": {"type": "integer"},
                            },
                            "required": ["node_id", "widget_index"],
                        },
                    },
                    "required": ["name", "type", "patch"],
                },
            },
            "intermediates": {
                "type": "array",
                "description": (
                    "Optional preprocessor previews (depth, canny, pose, normal, seg, etc.). "
                    "Each entry causes the runner to splice a SaveImage onto the named node's "
                    "output slot and the editor to render a preview tab between BLOCKOUT and "
                    "RENDER. Omit when the workflow has no visual preprocessor stage."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "snake_case identifier (e.g. 'depth')"},
                        "label": {"type": "string", "description": "Short tab label — 1-2 words (e.g. 'Depth')"},
                        "source_node_id": {"type": "integer", "description": "Numeric id of the preprocessor node whose output you want to save"},
                        "source_slot": {"type": "integer", "description": "Output socket index on that node — 0 for the primary image output"},
                        "filename_prefix": {"type": "string", "description": "Optional. Defaults to intermediate_<name>."},
                    },
                    "required": ["name", "label", "source_node_id"],
                },
            },
        },
        "required": ["id", "label", "kind", "output_ext", "inputs"],
    },
}


def _sse(event: str, **fields) -> bytes:
    """Serialize one Server-Sent Event line. `event` becomes the SSE event type,
    fields become the JSON `data` payload. Trailing blank line delimits."""
    payload = json.dumps({"type": event, **fields})
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


@app.post("/api/workflows/analyze")
async def workflows_analyze(request: Request):
    """SSE stream. Body: {"stem": "..."}. Loads the workflow, sends the slim
    view to Claude with the `write_manifest` tool, saves the returned manifest,
    hot-registers the module. Emits progress events so the UI can show the
    agent's steps in real time."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(400, "Anthropic API key not set — add it in Settings.")
    body = await request.json()
    stem = str(body.get("stem", "")).strip()
    if not stem or not _SAFE_WF_STEM.match(stem):
        raise HTTPException(400, "bad stem")
    wf_path = _WORKFLOWS_DIR / f"{stem}.json"
    if not wf_path.exists():
        raise HTTPException(404, f"workflow not found: {stem}")

    async def gen():
        from server.modules._workflow_shared import slim_workflow_for_analysis, register_manifest_by_name
        import anthropic

        try:
            yield _sse("progress", message=f"Reading {stem}.json…")
            workflow = json.loads(wf_path.read_text(encoding="utf-8"))
            node_count = len(workflow.get("nodes", []))
            active = sum(1 for n in workflow.get("nodes", []) if n.get("mode", 0) not in (2, 4))
            yield _sse("progress", message=f"Workflow has {node_count} nodes ({active} active)")

            slim = slim_workflow_for_analysis(workflow)
            slim_json = json.dumps(slim, indent=2)
            yield _sse("progress", message="Handing graph to the agent…")

            client = anthropic.Anthropic()
            user_msg = (
                f"Client-suggested id: `{stem}` (change it if the filename was garbage).\n\n"
                f"Workflow graph:\n```json\n{slim_json}\n```\n\n"
                "Emit the manifest via the `write_manifest` tool."
            )
            # Sync SDK call in a thread so we don't block the event loop; the
            # streaming vibe here is us emitting steps around the call, not
            # token streaming from Claude (which would need beta streaming).
            resp = await asyncio.to_thread(
                client.messages.create,
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=_MANIFEST_ANALYZER_SYSTEM,
                tools=[_MANIFEST_TOOL],
                tool_choice={"type": "tool", "name": "write_manifest"},
                messages=[{"role": "user", "content": user_msg}],
            )

            manifest = None
            for block in resp.content:
                b = block.model_dump() if hasattr(block, "model_dump") else block
                if b.get("type") == "tool_use" and b.get("name") == "write_manifest":
                    manifest = b.get("input") or {}
                    break
            if not manifest:
                yield _sse("error", message="Agent didn't call write_manifest — analysis failed")
                return

            # Enforce id = filename stem so runtime lookup stays predictable.
            # The agent's proposed id/label are advisory; we keep the label, force the id.
            manifest["id"] = stem
            for spec in manifest.get("inputs", []):
                nid = (spec.get("patch") or {}).get("node_id")
                if nid is not None:
                    yield _sse("found", message=f"Input `{spec['name']}` ({spec['type']}) → node {nid}")

            meta_path = _WORKFLOWS_DIR / f"{stem}.meta.json"
            meta_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            yield _sse("progress", message=f"Saved {meta_path.name}")

            mod = register_manifest_by_name(stem)
            if not mod:
                yield _sse("error", message="Manifest registered on disk but hot-load failed — restart to pick it up")
                return
            MODULES[mod.id] = mod
            yield _sse("progress", message=f"Registered module `{mod.id}`")
            yield _sse(
                "done",
                module=_module_dict(mod),
                manifest=manifest,
            )
        except Exception as e:
            import traceback
            print(f"[cb-app] workflow analyze error: {e}\n{traceback.format_exc()}")
            yield _sse("error", message=f"{type(e).__name__}: {e}")

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/run/status/{node_id}")
async def run_status(node_id: str):
    """Return the current per-node run phase, if any. Frontend polls this
    while a generation is in-flight so it can show 'Generated in Cloud ·
    fetching…' as soon as the Cloud job completes, without waiting for the
    /api/run response body (which only lands after the download loop
    finishes — many minutes later for partner-3D nodes)."""
    st = _run_status.get(node_id)
    if not st:
        return JSONResponse({"phase": None})
    return JSONResponse(st)


def _make_status_cb(node_id: str):
    """Return a closure the module can pass into shared helpers to write
    intermediate phase updates. Keeps the module code shape simple — a
    one-arg callable, no direct import of `_run_status`."""
    def _cb(phase: str, **extra):
        _run_status[node_id] = {"phase": phase, **extra}
    return _cb


@app.post("/api/run/{module_id}")
async def run_module(module_id: str, request: Request):
    m = MODULES.get(module_id)
    if not m or not m.run:
        raise HTTPException(404, f"unknown module: {module_id}")
    body = await request.json()
    node_id = str(body.get("node_id", "")).strip() or "default"
    inputs = dict(body.get("inputs") or {})
    # Seed the per-node status early so a polling client sees "submitting"
    # rather than a null phase during the (usually brief) upload + submit
    # window. Modules that care about progress will overwrite via status_cb.
    _run_status[node_id] = {"phase": "submitting"}

    # Client flag — user hit × on the viewport-blockout row (text-to-image mode).
    # Skips scene-image resolution below so no image_path gets injected, and
    # the module runs with image_path=None. Pop early so it doesn't leak into
    # the module kwargs. Nano Banana has no aspect_ratio CLI parameter, so we
    # synthesize a blank canvas at the requested aspect further down and pass
    # THAT as image_path — the base prompt already tells the model to match
    # image 1's dimensions. Without this, Nano defaults to 1:1 square.
    skip_source_image = bool(inputs.pop("skip_source_image", False))

    # Image/Video mode — utils like Preprocessors offer both. When the client
    # sends mode="image" on an input the manifest declared as scene-video, we
    # treat it as scene-image for path resolution (grab _image_store, populate
    # image_path). The downstream module also gets mode= so it can pick the
    # preset's image_workflow (see _workflow_shared.run_local).
    ui_mode = (inputs.get("mode") or "").strip().lower() or None

    # Blockout Strength (0.0..1.0) — how strictly the model should adhere to the
    # blockout's composition. Injected into the prompt as a tier-appropriate
    # instruction. Default 1.0 (strict) preserves prior behavior when the client
    # doesn't send it.
    try:
        blockout_strength = float(inputs.pop("blockout_strength", 1.0))
    except (TypeError, ValueError):
        blockout_strength = 1.0
    blockout_strength = max(0.0, min(1.0, blockout_strength))

    # Resolve scene-image / scene-video into concrete file paths from the editor's saved state.
    for spec in m.inputs:
        # In image mode, treat scene-video slots as scene-image — the frontend
        # already staged the source via /image_url (autoSnapshot or upload),
        # and the preset-picked workflow uses LoadImage instead of VHS_LoadVideo.
        effective_type = spec.get("type")
        if ui_mode == "image" and effective_type == "scene-video":
            effective_type = "scene-image"
        if effective_type == "scene-image":
            if skip_source_image:
                # Synthesize a plain black canvas at the scene aspect so the
                # model has an aspect anchor even in text-to-image mode. Nano
                # Banana has no aspect CLI param — the model reads image 1's
                # dimensions and (per BASE_PROMPT) matches them in the output.
                cam_meta = inputs.get("camera") or {}
                aspect_str = str(cam_meta.get("aspect") or "16:9")
                try:
                    a, b = aspect_str.split(":", 1)
                    ar_w, ar_h = int(a), int(b)
                except Exception:
                    ar_w, ar_h = 16, 9
                # Long side 1280 → 1280x720 for 16:9, 720x1280 for 9:16, etc.
                LONG = 1280
                if ar_w >= ar_h:
                    W, H = LONG, max(1, round(LONG * ar_h / ar_w))
                else:
                    W, H = max(1, round(LONG * ar_w / ar_h)), LONG
                import tempfile as _tempfile
                from PIL import Image as _Image
                tmp = _tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                tmp.close()
                _Image.new("RGB", (W, H), (0, 0, 0)).save(tmp.name)
                inputs["image_path"] = Path(tmp.name)
                continue
            info = _image_store.get(node_id)
            if info and Path(info["path"]).exists():
                inputs["image_path"] = Path(info["path"])
            elif spec.get("required", True):
                raise HTTPException(400, "no scene image saved — snapshot in the editor first")
        elif effective_type == "scene-video":
            # Preference order: transport-recorded clip FIRST (its duration
            # matches the scene trim range exactly, so feeding it to a video
            # workflow processes exactly what the user animated). Falls back
            # to the Scene Properties BG video if no recording exists — useful
            # for utils that operate on external reference footage.
            info = _video_store.get(node_id)
            if info and Path(info["path"]).exists():
                inputs["video_path"] = Path(info["path"])
                continue
            scene = _scene_store.get(node_id) or {}
            bg_video = ((scene.get("viewport") or {}).get("bgVideo")) or {}
            asset_id = bg_video.get("assetId")
            asset_ext = (bg_video.get("ext") or "").lower().lstrip(".")
            if asset_id and asset_ext and _SAFE_ASSET_ID.match(asset_id) and _SAFE_EXT.match(asset_ext):
                asset_path = _asset_dir(node_id) / f"{asset_id}.{asset_ext}"
                if asset_path.exists():
                    inputs["video_path"] = asset_path
                    continue
            # Only error when the spec insists on a video. Optional scene-video
            # inputs (e.g. Seedance's reference video — the model runs fine
            # without one) just fall through with no video_path set.
            if spec.get("required", True):
                raise HTTPException(400, "no scene video — record from the transport (● button on the timeline) or load a Background video in Scene Properties")

    # Optional image_url override — the frontend's gen-cell source-image slot passes
    # this when the user picked/dragged a specific image instead of using the auto
    # viewport snapshot. Resolve /output/... to a local file; anything else gets
    # fetched to a temp file for `comfy upload` to consume.
    image_url = inputs.pop("image_url", None)
    if image_url:
        import tempfile as _tempfile, urllib.request as _urlreq
        try:
            if image_url.startswith("/output/"):
                # Strip any cache-busting query string (e.g. ?t=1234) — the
                # viewport result-overlay adds one on render and the URL rides
                # along with the drag payload straight into the workflow slot.
                rel = image_url[len("/output/"):].split("?", 1)[0]
                candidate = DATA_DIR / rel
                if not candidate.exists():
                    raise HTTPException(400, f"source image not found: {image_url}")
                inputs["image_path"] = candidate
            elif image_url.startswith("/comfyblockout/image/"):
                # Drag from the Blockout overlay (or any node-scoped snapshot).
                # `/comfyblockout/image/<node_id>[?t=...]` — resolve via the
                # same _image_store the GET endpoint uses. Lets a user drag the
                # Blockout view thumbnail into a generator's Input image slot.
                tail = image_url[len("/comfyblockout/image/"):].split("?", 1)[0]
                nid = tail.strip("/") or "preview"
                info = _image_store.get(nid)
                if not info or not Path(info["path"]).exists():
                    raise HTTPException(400, f"no snapshot for node {nid!r}")
                inputs["image_path"] = Path(info["path"])
            elif image_url.startswith(("http://", "https://")):
                suffix = Path(image_url.split("?", 1)[0]).suffix or ".png"
                tmp = _tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
                tmp.close()
                _urlreq.urlretrieve(image_url, tmp.name)
                inputs["image_path"] = Path(tmp.name)
            else:
                raise HTTPException(400, f"unrecognized image_url scheme: {image_url}")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"couldn't fetch image_url: {e}")

    # Auto-inject user-uploaded reference URLs (signed URLs from comfy generate upload)
    # so modules that opt-in (like nano-banana) get them without the client re-sending each id.
    refs = _refs_store.get(node_id) or []
    if refs and "references" not in inputs:
        inputs["references"] = [r["signed_url"] for r in refs if r.get("signed_url")]

    # Pop client-only fields out of inputs so they don't get forwarded to the
    # module (Nano/Seedance don't accept them as kwargs). We consume them here
    # to build the SCENE INVENTORY block injected into the prompt.
    scene_objects = inputs.pop("scene_objects", None) or []
    camera_meta = inputs.pop("camera", None) or {}

    def _format_inventory(objs: list[dict], cam: dict) -> str:
        if not objs:
            return ""
        lines = ["SCENE INVENTORY (objects shown in image 1):"]
        fov = cam.get("fov_deg")
        aspect = cam.get("aspect")
        cam_bits = []
        if fov:    cam_bits.append(f"camera FOV {fov}° (use this as the lens — a wider FOV exaggerates near-vs-far size differences, a narrower FOV flattens them)")
        if aspect: cam_bits.append(f"aspect ratio {aspect}")
        if cam_bits:
            lines.append("Camera: " + "; ".join(cam_bits) + ".")
        checker_objs = []
        mannequin_objs = []
        annotation_objs = []
        for o in objs:
            name = o.get("name") or "?"
            kind = o.get("kind") or "object"
            color = o.get("color") or "untinted"
            x = o.get("screen_x_pct", 0)
            y = o.get("screen_y_pct", 0)
            w = o.get("screen_w_pct", 0)
            h = o.get("screen_h_pct", 0)
            ref_idx = o.get("ref_image_index")
            ref_note = f", surface/material drawn from swatch image {ref_idx}" if ref_idx else ""
            notes = o.get("notes")
            notes_note = f" — notes: {notes}" if notes else ""
            checker_note = " — the checkerboard on this surface is a reference GRID (perspective / vanishing points only); completely disregard the black-and-white texture in the output" if o.get("checker") else ""
            mannequin_note = " — mannequin: loose placement/scale reference for a human figure; treat the pose as approximate guidance, not a strict pose lock" if kind == "mannequin" else ""
            annotation_note = " — grease-pencil annotation: user's drawn guidance/notes, NOT content to render" if kind == "annotation" else ""
            lines.append(
                f"- {name} ({color} {kind}): centered at x={x}% y={y}% "
                f"of frame, occupies ~{w}%×{h}% of frame{ref_note}{notes_note}{checker_note}{mannequin_note}{annotation_note}"
            )
            if o.get("checker"):
                checker_objs.append(name)
            if kind == "mannequin":
                mannequin_objs.append(name)
            if kind == "annotation":
                annotation_objs.append(name)
        if checker_objs:
            names = ", ".join(checker_objs)
            lines.append(
                f"CHECKERBOARD ON {names}: Use the uploaded reference grid "
                "strictly as a structural framework for perspective, camera "
                "angle, and vanishing points. Completely disregard the "
                "black-and-white checkered texture. Instead of the grid, "
                "render the scene described in the user prompt exactly "
                "adhering to these perspective lines, proportions, and "
                "spatial arrangements."
            )
        if mannequin_objs:
            names = ", ".join(mannequin_objs)
            lines.append(
                f"MANNEQUIN(S) — {names}: Any mannequin figures in image 1 are "
                "loose STRUCTURAL and PLACEMENT references only. Use them to "
                "understand where a human character sits in the scene, at what "
                "scale, and their approximate orientation — but treat the pose "
                "as APPROXIMATE guidance, not a strict pose lock. The final "
                "character can move naturally into a more expressive, realistic "
                "pose that fits the user prompt and scene context, as long as "
                "the general placement and size in the frame are respected. "
                "Do NOT render the mannequin's flat grey blocky body, joint "
                "spheres, or stiff articulated look — replace it with the "
                "human subject described in the user prompt."
            )
        if annotation_objs:
            names = ", ".join(annotation_objs)
            lines.append(
                f"ANNOTATION STROKES — {names}: Any hand-drawn colored line strokes "
                "in image 1 are the user's grease-pencil ANNOTATIONS — notes, doodles, "
                "arrows, and structural guidance about what should appear, where "
                "boundaries lie, or emphasis on particular areas. Treat them as "
                "GUIDANCE ONLY. Do NOT render the stroke lines themselves in the "
                "output image. Interpret their intent (composition hints, area "
                "highlights, directional cues, callouts) and let that shape the "
                "generated scene, but the final image must not contain any of the "
                "raw drawn lines."
            )
        lines.append(
            "Use the screen-space %s above as the exact pixel footprint for each "
            "object. The tint listed is metadata to identify the colored shape "
            "in image 1 — not the final color of the rendered object."
        )
        if len(objs) > 1:
            lines.append(
                "RELATIVE SIZES: When two or more objects appear in image 1, their "
                "size ratio is significant. If two objects look nearly the same size "
                "in the blockout, they MUST look nearly the same size in the output. "
                "Do not exaggerate perspective; respect the perspective, orientation, "
                "and scale shown by the blockout."
            )
        return "\n".join(lines)

    # Prepend BASE_PROMPT (immutable spatial-ControlNet directive) + SCENE INVENTORY
    # (per-object screen-space metadata + reference-image map) + any user-saved
    # tweaks + the actual user request.
    #
    # Nano Banana ONLY — the BASE_PROMPT talks in terms of "image 1 is the
    # blockout" and "images 2, 3 are per-object swatches", which is behavior
    # unique to Nano's multi-image prompting. Custom ControlNet workflows,
    # partner 3D/video/audio APIs, and other image modules do their own
    # conditioning and should receive the user's prompt verbatim.
    user_prompt = (inputs.get("prompt") or "").strip()
    if user_prompt and module_id == "nano-banana":
        # _prompt_store now holds the user's EDITED base prompt (full replace
        # of BASE_PROMPT), not appended tweaks. Empty / unset → server default.
        base_override = (_prompt_store.get(node_id) or "").strip()
        inventory = _format_inventory(scene_objects, camera_meta)
        parts = [base_override or BASE_PROMPT.strip()]
        # Blockout Strength language — tiered instruction for how tightly to
        # hew to image 1's OBJECT-LEVEL composition. Camera angle + aspect are
        # locked by BASE_PROMPT and NOT modulated by this dial. 0 = loose
        # object placement, 1 = exact footprint replacement.
        if not skip_source_image:
            pct = int(blockout_strength * 100)
            if blockout_strength <= 0.25:
                strength_note = (
                    f"BLOCKOUT STRENGTH: {pct}% (LOOSE). Object placement in "
                    "image 1 is a rough suggestion — reinterpret sizes, "
                    "silhouettes, and exact positions as needed. Camera stays "
                    "locked (see camera rule)."
                )
            elif blockout_strength <= 0.6:
                strength_note = (
                    f"BLOCKOUT STRENGTH: {pct}% (MODERATE). Follow image 1's "
                    "object placement and scale as guidance, but the primitive "
                    "shapes can be interpreted loosely — final objects can "
                    "have organic proportions that differ from the blockout "
                    "stencils. Camera stays locked."
                )
            elif blockout_strength < 1.0:
                strength_note = (
                    f"BLOCKOUT STRENGTH: {pct}% (STRICT). Image 1's object "
                    "placement, scale, and silhouette closely match in the "
                    "output. Camera stays locked."
                )
            else:
                strength_note = (
                    "BLOCKOUT STRENGTH: 100% (LOCKED). Each colored shape's "
                    "exact screen-space footprint (position + size) is "
                    "preserved. Camera stays locked."
                )
            parts_prefix_strength = strength_note
        else:
            parts_prefix_strength = None
        if skip_source_image:
            # Text-to-image mode: image 1 is a synthesized blank canvas that
            # exists ONLY to lock the output aspect ratio. Prevent the model
            # from interpreting the black pixels as scene content (dark
            # background, night, silhouette, etc.).
            parts.append(
                "TEXT-TO-IMAGE MODE: image 1 is a BLANK BLACK CANVAS supplied "
                "purely as an aspect-ratio anchor. It contains NO scene "
                "content, NO objects, NO lighting cues, and NO subject. Do "
                "NOT let its blackness bias the output toward dark/night/"
                "silhouette imagery. Ignore its pixel content entirely; use "
                "it ONLY to set the output width and height."
            )
        if parts_prefix_strength:
            parts.append(parts_prefix_strength)
        if inventory:
            parts.append(inventory)
        parts.append(f"USER REQUEST: {user_prompt}")
        inputs["prompt"] = "\n\n".join(parts)

    # Give the module a per-node status callback it can pass down into
    # helpers so the frontend can poll intermediate progress. Modules that
    # don't care about progress just ignore the kwarg (**_ absorbs it).
    inputs["status_cb"] = _make_status_cb(node_id)
    try:
        result = await m.run(data_dir=DATA_DIR, **inputs)
    except ValueError as e:
        _run_status[node_id] = {"phase": "failed", "detail": str(e)[:200]}
        raise HTTPException(400, str(e) or "bad input")
    except RuntimeError as e:
        msg = str(e) or "comfy generate failed (no stderr/stdout captured)"
        print(f"[cb-app] run_module {module_id} RuntimeError: {msg}")
        # Preserve any richer phase that the module already wrote (e.g.
        # "cloud_done_no_download") — only overwrite the generic in-flight
        # phases. This lets the frontend surface Cloud-completed-but-not-
        # downloadable as distinct from an early submit-time failure.
        prev = _run_status.get(node_id, {}).get("phase")
        if prev in (None, "submitting", "generating", "fetching"):
            _run_status[node_id] = {"phase": "failed", "detail": msg[:200]}
        raise HTTPException(500, msg)
    except Exception as e:
        print(f"[cb-app] run_module {module_id} {type(e).__name__}: {e}")
        _run_status[node_id] = {"phase": "failed", "detail": f"{type(e).__name__}: {e}"[:200]}
        raise HTTPException(500, f"{type(e).__name__}: {e}")

    out_path = Path(result["path"])
    try:
        rel = out_path.relative_to(DATA_DIR).as_posix()
        result["url"] = f"/output/{rel}"
    except ValueError:
        result["url"] = None

    # Convert intermediate file paths to /output/ URLs so the frontend can render
    # them as preview tabs alongside the final render. Runners that don't
    # produce intermediates just leave the list empty.
    intermediates = result.get("intermediates")
    if isinstance(intermediates, list):
        for inter in intermediates:
            p = inter.get("path")
            if not p:
                continue
            try:
                rel_i = Path(p).relative_to(DATA_DIR).as_posix()
                inter["url"] = f"/output/{rel_i}"
            except ValueError:
                inter["url"] = None
    return result


# ---------- LLM assistant (Claude + Comfy Cloud MCP) ----------
#
# Per-node conversation memory. ComfyBlockout is single-user-per-process so a
# plain dict is fine; if this ever runs multi-tenant move to a TTL store.
_chat_history: dict[str, list[dict]] = {}


def _chat_history_path(node_id: str) -> Path:
    """Where a node's chat history persists on disk. Mirrors the scene-file
    layout so users can find/back-up conversations alongside their scenes."""
    return DATA_DIR / f"node_{node_id}.chatlog.json"


def _sanitize_history(messages: list[dict]) -> list[dict]:
    """Anthropic requires: any `tool_result` block on a user turn must have a
    matching `tool_use` block in the immediately-preceding assistant turn.
    History-trimming or a mid-conversation crash can leave orphans on either
    end (or MIXED user turns with a stray tool_result alongside text) and
    break the next call with a 400. We walk the log message-by-message and
    keep only well-formed blocks — orphan tool_results get dropped, and if
    stripping them leaves a user turn empty, the turn itself is dropped."""
    out: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "user" and isinstance(content, list):
            # Valid tool_use_ids = whatever the LAST kept message (assistant)
            # advertised. Empty set if the previous turn wasn't an assistant.
            valid_ids: set[str] = set()
            prev = out[-1] if out else None
            if isinstance(prev, dict) and prev.get("role") == "assistant" and isinstance(prev.get("content"), list):
                for b in prev["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tid = b.get("id")
                        if isinstance(tid, str):
                            valid_ids.add(tid)
            new_content = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    if b.get("tool_use_id") in valid_ids:
                        new_content.append(b)
                    # else: drop orphan silently
                else:
                    new_content.append(b)
            if not new_content:
                # Whole turn was orphaned tool_results — drop the turn too.
                continue
            out.append({**msg, "content": new_content})
        else:
            out.append(msg)
    # Trailing assistant with unresolved tool_use blocks would break the next
    # user turn's API call — drop it so the next user turn goes through cleanly.
    if out:
        last = out[-1]
        if isinstance(last, dict) and last.get("role") == "assistant" and isinstance(last.get("content"), list):
            has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in last["content"])
            if has_tool_use:
                out = out[:-1]
    return out


def _load_chat_history(node_id: str) -> list[dict]:
    """Hydrate in-memory history from disk on first request after a restart.
    Returns [] if there's nothing saved OR if the file is corrupt (we don't
    want a bad file to brick the assistant — a fresh conversation is fine).
    Applies the same sanitization as trim so a mid-tool-call crash doesn't
    resurrect a broken history."""
    if node_id in _chat_history:
        return _chat_history[node_id]
    p = _chat_history_path(node_id)
    if not p.exists():
        _chat_history[node_id] = []
        return _chat_history[node_id]
    try:
        loaded = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            _chat_history[node_id] = _sanitize_history(loaded)
            return _chat_history[node_id]
    except Exception:
        pass
    _chat_history[node_id] = []
    return _chat_history[node_id]


def _save_chat_history(node_id: str) -> None:
    """Flush current in-memory history to disk. Called after each turn so a
    server crash mid-tool-loop still leaves the next request with a
    reconstructable state."""
    history = _chat_history.get(node_id)
    if history is None:
        return
    try:
        _chat_history_path(node_id).write_text(json.dumps(history), encoding="utf-8")
    except Exception:
        # Persistence is best-effort — never let a disk hiccup take down the
        # live chat. In-memory history still works this session.
        pass

ASSISTANT_SYSTEM = (
    "You are the in-editor agent for ComfyBlockout. Users are non-technical "
    "filmmakers/artists building blockouts (coarse geometry + lights + camera) "
    "that get restyled into finished frames by AI generators (Nano Banana, "
    "Seedance, Tripo, Flux, etc.). The blockout IS the scaffold — not just a "
    "reference. Your job: shape the scene via natural language, write prompts, "
    "configure workflow cells, and DIRECTLY manipulate the scene when the user "
    "describes intent instead of steps.\n\n"

    "CORE LOOP: user describes shot → you spawn/arrange/light/frame it → user "
    "picks a workflow → hits Generate → viewport snapshot + prompt runs through "
    "the model → result lands in the Assets pane + viewport overlay.\n\n"

    "SPATIAL SYSTEM (read before aiming anything).\n"
    "Right-handed Y-up: +X right, +Y up, +Z toward viewer. Origin (0,0,0) is on "
    "the ground; primitives spawn at y≈0.5 (resting on floor). Default viewport "
    "camera ≈ (2.5, 1.4, 2.5) looking at origin — front-right 3/4 view.\n"
    "NEVER hand-compute Euler angles to aim a light/camera/spot cone. Use "
    "`aim_object_at` — it lookAt's under the hood so the math is correct. "
    "Reserve `set_object_rotation` for explicit rotations the user asked for "
    "(\"tilt 45° on X\", \"turn 90° to face left\") or pure spin-in-place.\n\n"

    "TOOL PREFERENCES (call these, don't describe them).\n"
    "- Spawn/edit: `add_primitive` (cube/sphere/capsule/cylinder/cone/plane/text/"
    "particles/clouds), `batch_add_primitives` for many at once, `spawn_light`, "
    "`spawn_terrain`, `spawn_mannequin`, `spawn_skybox`. Move: `set_object_position`. "
    "Aim: `aim_object_at`. Recolor: `set_object_color`. Rename: `rename_object`. "
    "Delete: `delete_object`.\n"
    "- Environments — always prefer the one-call `generate_*` variants over "
    "spawn+manual: `generate_skybox({prompt})` for 360° backdrops (a plain "
    "spawn_skybox leaves an empty amber sphere; a workflow-cell generator "
    "produces a flat plane image, NOT a sphere texture — those are wrong "
    "answers), `generate_terrain({prompt})` for landscape ground (heightmap "
    "displacement; prompt describes SHAPE from above, not aesthetic).\n"
    "- Camera: `set_camera_target({name})` locks aim to an object every frame; "
    "`clear_camera_target()`; `set_camera_handheld({speed,noise})` adds shake "
    "(0..1 each, only visible during camera-view playback/record).\n"
    "- Turntable: `start_turntable({mode, duration, direction, object_name?})`. "
    "mode='subject' bakes 9 Y-rot keyframes on the object; mode='camera' runs a "
    "procedural camera orbit around it. `stop_turntable()` clears both.\n"
    "- Generate: prefer `trigger_generate` over raw Comfy MCP tools — it routes "
    "through the editor pipeline so the result lands in Assets + viewport overlay. "
    "Raw MCP is for inspection or flows the editor doesn't expose.\n\n"

    "LIGHTS quick spec (`spawn_light({type, position?, intensity?, color?, "
    "cast_shadows?, softbox_width?, softbox_height?})`):\n"
    "- `directional`: parallel-ray sun. KEY shadow-caster for outdoor/establishing.\n"
    "- `spot`: cone with angle + penumbra. Focused pools; clean VSM shadows.\n"
    "- `point`: omnidirectional bulb. USE AS FILL. NEVER enable shadows on point "
    "(cube-map seams). Force cast_shadows=false.\n"
    "- `softbox`: RectAreaLight. Broad soft directional fill; CAN'T cast shadows "
    "(three limitation, exactly the intended use). Size via softbox_width/height.\n"
    "Adding any user light auto-kills the built-in scene fill (hemi + directional "
    "+ PMREM) — feature, not bug. Recommended: one directional/spot as shadow-"
    "caster + one softbox/point as fill from the opposite side.\n\n"

    "CONTACT SHADOW (Scene properties toggle) layers a top-down soft ambient "
    "shadow beneath every object. Suggest turning ON when the user wants extra "
    "grounding or has no shadow-casting light. Off by default. Doesn't respond "
    "to light direction — pair it with a real Directional/Spot for directional "
    "cues.\n\n"

    "TERMINOLOGY: the WORKFLOWS panel mixes partner-API workflows (Nano Banana, "
    "Seedance, Tripo, routed through Comfy Cloud) and local ComfyUI workflows "
    "(manifest-driven modules from `create_workflow_module`). Scene context "
    "distinguishes them via `source: api` vs `source: local`. Legacy tool names "
    "still say \"generator\" (e.g. `set_generator_prompt`) — they work for both.\n\n"

    "SCENE STATE — DON'T PRELOAD, QUERY ON DEMAND.\n"
    "The <scene_pointer> block prepended to each turn contains ONLY minimal "
    "pointers: currently selected object, selected asset, active workflow. NO "
    "object list, no camera pose, no workflow catalog. Preloading the full "
    "scene every turn was burning fresh tokens the user wasn't paying for.\n"
    "- Small-talk / greetings (\"hi\", \"help\", \"what can you do?\") → reply "
    "conversationally, DO NOT call any tools, DO NOT summarize the scene. Ask "
    "the user what they want to build.\n"
    "- Underspecified \"this / it / that\" command → the <scene_pointer> "
    "selection line is usually enough. If not, call `get_selected_object`.\n"
    "- Direct scene questions (\"what's in my scene?\", \"where is the camera?\") "
    "→ call `list_objects` / `get_camera_state` / `get_scene_summary` first.\n"
    "- Commands referencing an object name you don't have — call `list_objects` "
    "before acting so you don't mis-target.\n\n"

    "EXTENDED TOPICS available via `read_docs({topic})` — call this BEFORE acting "
    "when the user's request touches one of these:\n"
    "- `workflows` — creating, importing, or repairing a workflow module "
    "(create_workflow_module, template lookup, scene-image wiring, cloud shape:7 "
    "shift, 3D catalog gap, diagnosing runtime errors). Load when the user says "
    "\"add/build/import/fix a workflow\", references node IDs, asks about "
    "widget/socket wiring, OR when a workflow returns any of: `shape_mismatch`, "
    "`expected INT got STRING`, `expected FLOAT got str`, `'str' object has no "
    "attribute 'shape'`. Non-negotiable: DIAGNOSING a workflow failure without "
    "loading this doc first produces wrong root-cause explanations and worse "
    "'fixes' (stripping user-facing inputs to hide the bug is common — DO NOT).\n"
    "- `animoflow` — text-to-motion via `run_animoflow({prompt, max_frames?, "
    "seed?})`. Load when the user asks for character animation or motion synthesis.\n"
    "Guessing without loading these when the topic applies produces broken output.\n\n"

    "WORKFLOW-REPAIR HARD RULES (apply even when read_docs hasn't been loaded):\n"
    "- Never hand-build a graph for a partner-API model that has an official "
    "Comfy Cloud template. Fetch via MCP `get_template` (or the raw GitHub URL "
    "`https://raw.githubusercontent.com/Comfy-Org/workflow_templates/main/"
    "templates/api_<partner>_<model>_<mode>.json`) and use verbatim. A hand-"
    "constructed graph passes local validation but fails cloud validation with "
    "opaque 'Failed to validate images' errors that get misdiagnosed as "
    "widget-index bugs or partner-API limitations. Precedent: seedream_5_pro_"
    "image_edit was hand-built and produced ~4 sessions of wrong root-cause "
    "theories; swapping in the canonical template fixed it in one edit.\n"
    "- Never 'fix' a shape/type error by removing the user-facing input from the "
    "manifest and hardcoding a value in the workflow JSON. That's not a fix — it "
    "hides the bug, removes user control, and misleads the next agent. The one-"
    "line manifest `type` change is the correct fix; nothing else is.\n"
    "- Never claim partner-API nodes have a 'known limitation' with patched inputs "
    "unless you've read `_workflow_shared.py:run_cloud` and `_workflow_local.py:"
    "_apply_single_patch` and can point to the specific line that would cause it. "
    "There is one patch function per runner and both coerce numeric types "
    "identically.\n"
    "- Before claiming 'node X is a LoadImage with no scene-image patch, so cloud "
    "rejects the literal scene_snapshot.png', OPEN the module's .meta.json and "
    "grep for the node id. If the meta already declares an input with "
    "patch.node_id == X and type: 'scene-image', that node IS patched at runtime "
    "— scene_snapshot.png is the placeholder the runner replaces with the "
    "uploaded viewport/custom image. Adding a duplicate input is a fake fix. "
    "Every scene-image input reads the same kwargs['image_path'], so one "
    "uploaded snapshot fans out to every LoadImage that has a patch — this is "
    "how chained-style-ref workflows (e.g. Krea 2's dual Krea2StyleReferenceNode) "
    "get both LoadImage nodes fed from one UI slot.\n\n"

    "OUTPUT STYLE: keep responses tight. Quote object names with brackets like "
    "[Cube.001] when referring to scene objects — the editor renders those tokens "
    "in the object's color and uses them to attach per-object reference images at "
    "generate time."
)


# Editor tools — Claude calls these via tool_use; the frontend executes them
# and posts the result back via /api/llm/chat with tool_results. Schemas mirror
# what the JS dispatcher in editor.html knows how to run.
EDITOR_TOOLS = [
    {
        "name": "add_primitive",
        "description": "Add ONE primitive. For multiple use batch_add_primitives (~10x faster). For mannequin/skybox use spawn_mannequin/spawn_skybox.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["cube", "sphere", "capsule", "cylinder", "cone", "plane", "text", "particles", "clouds"],
                    "description": "text spawns a 3D 'Text' mesh (rename via rename_object to change the string). particles/clouds = stylized FX.",
                },
                "color": {"type": "string", "description": "Optional hex (e.g. #ff5fbf)"},
                "position": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                "preset": {
                    "type": "string",
                    "enum": ["snow", "rain", "sparks", "fireflies"],
                    "description": "kind='particles' only. Canned weather/effect configs. Use when user names one; skip for generic emitters.",
                },
            },
            "required": ["kind"],
        },
    },
    {
        "name": "batch_add_primitives",
        "description": "Add many primitives in one call. Use for arrays/grids/patterns instead of looping add_primitive.",
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["cube", "sphere", "capsule", "cylinder", "cone", "plane", "text", "particles", "clouds"],
                            },
                            "color": {"type": "string", "description": "Optional hex like #ff5fbf"},
                            "position": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 3, "maxItems": 3,
                            },
                            "scale": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 1, "maxItems": 3,
                                "description": "Optional [x,y,z] or [uniform] scale",
                            },
                            "preset": {
                                "type": "string",
                                "enum": ["snow", "rain", "sparks", "fireflies"],
                                "description": "Only for kind='particles'. Same preset catalog as add_primitive.",
                            },
                        },
                        "required": ["kind"],
                    },
                },
            },
            "required": ["items"],
        },
    },
    {
        "name": "list_objects",
        "description": "List all objects in the scene with their names, kinds, positions, and colors.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_selected_object",
        "description": "Selected object's name/kind/transform/color, or 'nothing selected'. Cheaper than list_objects when the user is pointing at something.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_camera_state",
        "description": "Render camera world position, aim target, FOV, aspect. Call before camera edits or framing questions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_scene_summary",
        "description": "Object counts by kind + active workflows + keyframe count + scene duration. Cheaper than list_objects.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_scene_duration",
        "description": "Set timeline duration in seconds. Keyframes are stored 0..1 relative, so they stay at their relative position.",
        "input_schema": {
            "type": "object",
            "properties": {"seconds": {"type": "number", "minimum": 0.5, "maximum": 300}},
            "required": ["seconds"],
        },
    },
    {
        "name": "set_playhead",
        "description": "Move the timeline playhead. Call before add_keyframe to place a key at that beat.",
        "input_schema": {
            "type": "object",
            "properties": {"time_s": {"type": "number", "minimum": 0}},
            "required": ["time_s"],
        },
    },
    {
        "name": "add_keyframe",
        "description": "Add a keyframe capturing CURRENT pose. Set transform first (set_object_position etc.), then call this. Omit time_s to use playhead. target='camera' or object name. ease defaults to easeInOut (or 'through' when inserted between existing keys).",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "'camera' or object name"},
                "time_s": {"type": "number", "minimum": 0},
                "ease": {"type": "string", "enum": ["linear", "easeIn", "easeOut", "easeInOut", "through"]},
            },
            "required": ["target"],
        },
    },
    {
        "name": "remove_keyframe",
        "description": "Remove a keyframe at/near time_s (within 0.5% of duration).",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "time_s": {"type": "number", "minimum": 0},
            },
            "required": ["target", "time_s"],
        },
    },
    {
        "name": "list_keyframes",
        "description": "Return [{time_s, ease}] for the track. Inspect before editing.",
        "input_schema": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
    },
    {
        "name": "play_preview",
        "description": "Start/stop timeline playback from the current playhead.",
        "input_schema": {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["play", "stop"]}},
            "required": ["action"],
        },
    },
    {
        "name": "set_skybox_image",
        "description": "Apply an equirectangular URL as the skybox texture. Pair with list_recent_outputs to reuse the last render. Spawn a sphere first if none exists. Non-equirectangular sources stretch.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "list_recent_outputs",
        "description": "List recently generated assets. Use to reference the user's last renders.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Default 20"},
                "kind": {"type": "string", "enum": ["image", "video", "3d"]},
            },
        },
    },
    {
        "name": "delete_object",
        "description": "Delete an object by name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "rename_object",
        "description": "Rename an object.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Current name"},
                "new_name": {"type": "string"},
            },
            "required": ["name", "new_name"],
        },
    },
    {
        "name": "set_object_color",
        "description": "Set an object's color (hex string like #ff5fbf).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "color": {"type": "string"},
            },
            "required": ["name", "color"],
        },
    },
    {
        "name": "set_object_position",
        "description": "Set an object's world position.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
            },
            "required": ["name", "x", "y", "z"],
        },
    },
    {
        "name": "set_object_rotation",
        "description": "Set explicit Euler rotation (degrees, XYZ). For aiming at a target use `aim_object_at` instead — hand-computed Euler angles miss.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
            },
            "required": ["name", "x", "y", "z"],
        },
    },
    {
        "name": "read_docs",
        "description": "Load extended-topic markdown before acting on it. Call this BEFORE any workflow-module task (create/import/repair) or AnimoFlow motion synthesis — the base prompt only summarizes them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": ["workflows", "animoflow"],
                    "description": "`workflows` = create_workflow_module recipe, scene-image wiring, cloud gotchas. `animoflow` = text-to-motion.",
                },
            },
            "required": ["topic"],
        },
    },
    {
        "name": "aim_object_at",
        "description": "Point an object's forward at a target using three.js lookAt (guaranteed correct). Use for ALL aiming — lights, cameras, spot cones. Pass `target` (another object's name) OR (target_x, target_y, target_z). Point lights are omnidirectional so aiming them is a no-op.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "target": {"type": "string", "description": "Object name to aim at. Mutually exclusive with target_x/y/z."},
                "target_x": {"type": "number"},
                "target_y": {"type": "number"},
                "target_z": {"type": "number"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "set_object_scale",
        "description": "Set an object's scale (uniform if only x given, or per-axis).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "z": {"type": "number"},
            },
            "required": ["name", "x"],
        },
    },
    {
        "name": "set_generator_prompt",
        "description": "Update the 'prompt' input on a generator cell (nano-banana / seedance / any WORKFLOW module id). Use `set_workflow_inputs` when multiple fields need to change.",
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "'nano-banana', 'seedance', or WORKFLOW module id"},
                "prompt": {"type": "string"},
            },
            "required": ["model", "prompt"],
        },
    },
    {
        "name": "set_workflow_inputs",
        "description": "Batch-set multiple named inputs on a WORKFLOW module. Input names match the module manifest (see activeWorkflowInputs in scene context). Unknown names are ignored + reported back.",
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "WORKFLOW module id"},
                "inputs": {
                    "type": "object",
                    "description": "Map {input_name: value}. Values coerced to strings.",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["model", "inputs"],
        },
    },
    {
        "name": "trigger_generate",
        "description": "Run a generator cell through the editor's pipeline. Result lands in the viewport overlay + Assets pane. Prefer over raw Comfy MCP (that output isn't saved to Assets).",
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "enum": ["nano-banana", "seedance"]},
                "prompt": {"type": "string", "description": "Optional — sets the prompt first, then runs."},
            },
            "required": ["model"],
        },
    },
    {
        "name": "create_workflow_module",
        "description": "Register a ComfyUI workflow as an editor generator/util. Pass the same `id` to overwrite an existing module. Call `read_docs({topic:'workflows'})` first for the full recipe (input types, scene-image wiring, cloud gotchas).",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "snake_case id (e.g. 'flux2_klein_t2i_local')"},
                "label": {"type": "string", "description": "Cell title, ≤24 chars, `·` separator, T2I/I2I/etc. abbreviations."},
                "kind": {"type": "string", "enum": ["image", "video", "3d", "audio"]},
                "output_ext": {"type": "string", "description": "Extension without dot (png, mp4, glb, wav)"},
                "runner": {"type": "string", "enum": ["cloud", "local"], "description": "Default 'cloud'"},
                "workflow": {
                    "type": "object",
                    "description": "Workflow JSON. API format (flat dict keyed by node id) preferred for both runners. Legacy graph format (nodes[]+links[]) also accepted.",
                },
                "intermediates": {
                    "type": "array",
                    "description": "Optional preprocessor preview taps. See workflows doc.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "label": {"type": "string", "description": "Short tab label (e.g. 'Depth')"},
                            "source_node_id": {"type": "integer"},
                            "source_slot": {"type": "integer", "description": "Default 0"},
                            "filename_prefix": {"type": "string"},
                        },
                        "required": ["name", "label", "source_node_id"],
                    },
                },
                "inputs": {
                    "type": "array",
                    "description": "User-facing inputs. Expose ONLY per-run values (prompt, seed, source image). Everything else stays baked in.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "snake_case kwarg"},
                            "type": {
                                "type": "string",
                                "enum": ["textarea", "text", "scene-image", "scene-video", "number", "seed", "dropdown"],
                                "description": "textarea/text = prompt fields. scene-image = LoadImage-patched viewport snapshot or upload. scene-video = LoadVideo-patched Scene→Background→Video. number = numeric (default/min/max/step). seed = numeric with 🎲/🔒 toggle (patch onto KSampler.seed). dropdown = <select> from `options` (any Comfy COMBO widget).",
                            },
                            "required": {"type": "boolean"},
                            "placeholder": {"type": "string"},
                            "label": {"type": "string"},
                            "default": {"description": "Default for number/seed/dropdown."},
                            "min": {"type": "number"},
                            "max": {"type": "number"},
                            "step": {"type": "number"},
                            "options": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "type='dropdown' only. Full ordered list of allowed values.",
                            },
                            "patch": {
                                "type": "object",
                                "description": "Where to write. Include BOTH widget_index (cloud, position) AND widget_name (local, input key like 'text' for CLIPTextEncode).",
                                "properties": {
                                    "node_id": {"type": "integer"},
                                    "widget_index": {"type": "integer"},
                                    "widget_name": {"type": "string"},
                                },
                                "required": ["node_id"],
                            },
                        },
                        "required": ["name", "type", "patch"],
                    },
                },
                "util": {"type": "boolean", "description": "True → Tools grid button (media transforms: pose extract, bg removal, etc). Filename: `util_<name>.json`."},
                "icon": {"type": "string", "description": "Optional 24x24 inline SVG for util buttons."},
            },
            "required": ["id", "label", "kind", "output_ext", "workflow", "inputs"],
        },
    },
    {
        "name": "download_model_to_comfy",
        "description": "Download a model into the local ComfyUI models/<folder>/. Use ONLY after check_local_models flags a miss AND the user confirmed. Prefer HuggingFace resolve/main URLs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Direct file URL (e.g. huggingface.co/.../resolve/main/...)"},
                "folder": {"type": "string", "description": "ComfyUI models sub-folder (diffusion_models, vae, text_encoders, checkpoints, loras, controlnet, ...)"},
                "filename": {"type": "string", "description": "Must match what the workflow references."},
            },
            "required": ["url", "folder", "filename"],
        },
    },
    {
        "name": "get_workflow_module",
        "description": "Read the manifest + workflow JSON saved for a workflow module. Call before proposing a fix. Refuses built-in Python modules.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "check_custom_nodes",
        "description": "Check which third-party node class_types are installed locally. Call after registering a runner='local' workflow BEFORE downloading models. Returns {missing, present}. Missing ones → install_custom_node.",
        "input_schema": {
            "type": "object",
            "properties": {
                "class_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Third-party class_types only. Skip built-ins (KSampler, CLIPTextEncode, etc.).",
                    "minItems": 1,
                },
            },
            "required": ["class_types"],
        },
    },
    {
        "name": "install_custom_node",
        "description": "git clone a ComfyUI custom-node repo into custom_nodes/ AND run pip on its requirements.txt against ComfyUI's Python. Returns {restart_required, pip_ran, pip_ok, pip_error}. If restart_required → call restart_comfy. `force:true` deletes an existing folder + re-clones (use when prior install landed in the wrong ComfyUI). If pip_ran=false, ask the user to run pip manually — otherwise handle it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "git_url": {"type": "string", "description": "Public https URL; trailing .git optional."},
                "name": {"type": "string", "description": "Directory name under custom_nodes/ (default: repo basename)"},
                "force": {"type": "boolean", "description": "Delete existing folder + re-clone."},
            },
            "required": ["git_url"],
        },
    },
    {
        "name": "restart_comfy",
        "description": "Restart local ComfyUI via ComfyUI-Manager so newly-installed custom nodes register. Call IMMEDIATELY after install_custom_node returns restart_required. If restarted=false ('not installed'), ask the user to restart manually.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_local_models",
        "description": "Check which model files exist in the local ComfyUI (respects extra_model_paths.yaml). Call after registering a runner='local' module. Returns {missing:[{filename, folder}], present}.",
        "input_schema": {
            "type": "object",
            "properties": {
                "models": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "filename": {"type": "string", "description": "The .safetensors/.ckpt/etc. filename ComfyUI sees in its dropdown."},
                            "folder": {"type": "string", "description": "checkpoints / vae / loras / controlnet / text_encoders / diffusion_models / ..."},
                        },
                        "required": ["filename"],
                    },
                    "minItems": 1,
                },
            },
            "required": ["models"],
        },
    },
    # Compound-object spawns — assemblies (multi-mesh + userData + skinning)
    # that don't fit add_primitive's single-geometry model.
    {
        "name": "spawn_mannequin",
        "description": "Spawn a ~1.72m Xbot-rigged mannequin (Mixamo skinned GLB, poseable joints). Becomes current selection.",
        "input_schema": {
            "type": "object",
            "properties": {
                "position": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3, "description": "Default [0,0,0]"},
            },
        },
    },
    {
        "name": "spawn_skybox",
        "description": "Spawn an empty 360° backdrop sphere. User drops/generates an equirectangular texture onto it. Prefer `generate_skybox` for a one-call environment.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "spawn_terrain",
        "description": "Spawn a 20x20m fBM-noise terrain. presets: hills / mountains / canyon. For aesthetic-driven landscapes prefer `generate_terrain`.",
        "input_schema": {
            "type": "object",
            "properties": {
                "preset": {"type": "string", "enum": ["hills", "mountains", "canyon"], "description": "Default hills"},
                "seed": {"type": "integer", "description": "Default 42"},
                "position": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
            },
        },
    },
    {
        "name": "spawn_light",
        "description": "Spawn a light (see base prompt for type roles). Adding any user light auto-kills built-in scene fill.",
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["directional", "point", "spot", "softbox"], "description": "Default point."},
                "position": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3, "description": "Default (-3, 3, -3)"},
                "intensity": {"type": "number", "minimum": 0, "maximum": 500, "description": "Default 75. Softbox/point often need more than directional/spot."},
                "color": {"type": "string", "description": "Hex, default #ffffff. Warm #ffe4b0, cool #b0d4ff."},
                "cast_shadows": {"type": "boolean", "description": "TRUE for directional/spot. FALSE for point (cube-map seams) + softbox (unsupported)."},
                "softbox_width": {"type": "number", "minimum": 0.1, "maximum": 20, "description": "Meters, softbox only. Default 2."},
                "softbox_height": {"type": "number", "minimum": 0.1, "maximum": 20, "description": "Meters, softbox only. Default 2."},
            },
        },
    },
    {
        "name": "generate_terrain",
        "description": "One-call: generate a grayscale heightmap on Comfy Cloud + apply it as terrain displacement. Reuses existing terrain if any. Prompt describes SHAPE from above (\"mountain range with river valley\") — server appends the grayscale/orthographic guardrails. Prefer over workflow cells for terrain (those produce planes).",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Landscape SHAPE (top-down)"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "generate_skybox",
        "description": "One-call: generate a 360° equirectangular panorama on Comfy Cloud + apply it as the skybox texture. Reuses existing sphere if any. Prompt = aesthetic (\"misty pine forest at dawn\") — server appends the panorama guardrails. Prefer over workflow cells for backdrops (those produce planes, not sphere textures). On error, surface it — do NOT fall back to workflow cells.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Environment/location/lighting."},
            },
            "required": ["prompt"],
        },
    },
    # Camera control — applied to the render camera (the shot), not the
    # free-orbit scene view. Persists via state.renderCamera.
    {
        "name": "set_camera_target",
        "description": "Lock the render camera's aim to an object every frame. Overrides orbit + keyframed rotation (position keyframes still apply). Also the pivot for start_turntable(mode='camera').",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "clear_camera_target",
        "description": "Release the camera aim lock. Returns to raw pose from keyframes / orbit.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_camera_handheld",
        "description": "Multi-freq shake on the render camera. Both args 0..1 (0=off, 1=~5cm+1.5°). Only visible during camera-view playback/record.",
        "input_schema": {
            "type": "object",
            "properties": {
                "speed": {"type": "number", "minimum": 0, "maximum": 1, "description": "Shake frequency"},
                "noise": {"type": "number", "minimum": 0, "maximum": 1, "description": "Shake amplitude"},
            },
            "required": ["speed", "noise"],
        },
    },
    {
        "name": "start_turntable",
        "description": "Turntable motion. mode='subject' bakes 9 Y-rot keyframes on object_name. mode='camera' activates procedural orbit ring around it. Sets scene duration to one revolution. object_name required for subject mode.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["subject", "camera"]},
                "duration": {"type": "number", "minimum": 0.5, "maximum": 60, "description": "Seconds per rev (typical 3-10)"},
                "direction": {"type": "string", "enum": ["cw", "ccw"], "description": "Default cw"},
                "object_name": {"type": "string"},
            },
            "required": ["mode", "duration"],
        },
    },
    {
        "name": "stop_turntable",
        "description": "Clear procedural orbit + camera keyframes. Also wipes object keyframes if object_name given (undo subject-mode).",
        "input_schema": {
            "type": "object",
            "properties": {"object_name": {"type": "string"}},
        },
    },
    # AnimoFlow — text-to-motion via local MoMask container onto AF_Mannequin.
    # Requires Docker + AnimoFlow containers up (setup in Motion util pane).
    {
        "name": "run_animoflow",
        "description": "Text-to-motion animation retargeted onto an AF_Mannequin (spawns one if needed). Docker + AnimoFlow containers required. First run of the day: 30-90s. Prompt = short verb phrase (\"person walking forward\"). See read_docs({topic:'animoflow'}) for details.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "max_frames": {"type": "integer", "minimum": 30, "maximum": 240, "description": "20fps. Default 120 (6s)."},
                "seed": {"type": "integer", "description": "Default 42"},
            },
            "required": ["prompt"],
        },
    },
]


def _format_scene_context(ctx: dict) -> str:
    """Render a MINIMAL scene-state pointer prepended to every turn. Only the
    'what is the user currently pointing at' bits — selection + active
    workflow + selected asset. Everything else (full object list, camera
    pose, workflow catalog, keyframes) the agent must query via tools
    (list_objects, get_selected_object, get_camera_state, get_scene_summary)
    on demand. Preloading the full scene analysis every turn was the fresh-
    input token hog; this cuts ~90% of that cost.

    The user's rule of thumb: don't analyze the scene unless asked to."""
    if not ctx:
        return ""
    parts = []
    # Scene selection — enables "make it red", "delete this", etc. without a
    # round-trip. This is the single bit worth preloading; it's ~1 line.
    sel = ctx.get("selected")
    if sel and sel.get("name"):
        parts.append(f"selected: [{sel['name']}] ({sel.get('kind') or 'object'})")
    # Assets selection — parallel affordance for "apply this as a skybox".
    sel_assets = ctx.get("selectedAssets") or []
    if sel_assets:
        first = sel_assets[0]
        extra = f" (+{len(sel_assets)-1} more)" if len(sel_assets) > 1 else ""
        parts.append(
            f"selected asset: {first.get('filename') or '?'} "
            f"({first.get('kind') or 'asset'}, url={first.get('url') or '?'}){extra}"
        )
    # Active workflow pointer — disambiguates "run it" without asking the user
    # which workflow they meant. Just the id/label, not the full manifest.
    at = ctx.get("activeTarget")
    if at:
        source = at.get("source") or ("local" if ctx.get("activeWorkflowModuleId") else "api")
        parts.append(f"active workflow: {at.get('id')} ({at.get('label')}, {source})")
    if not parts:
        return ""
    return "<scene_pointer>\n" + "\n".join(parts) + "\n</scene_pointer>"


_EDITOR_TOOL_NAMES = {t["name"] for t in EDITOR_TOOLS}


def _serialize_content_blocks(content) -> list[dict]:
    """Serialize Anthropic SDK ContentBlock objects to dicts so they survive a
    JSON round-trip through the API response and the next request body."""
    out = []
    for block in content:
        d = block.model_dump() if hasattr(block, "model_dump") else block
        # Strip cache_control on round-trip — not valid on assistant blocks anyway.
        if isinstance(d, dict):
            d.pop("cache_control", None)
        out.append(d)
    return out


def _call_claude(history, ctx_block_for_caching):
    """Single Claude turn with full Comfy MCP + editor tool surface. Returns
    the raw response. History is mutated in-place by the caller."""
    comfy_key = os.environ.get("COMFY_API_KEY")
    mcp_servers = []
    tools = list(EDITOR_TOOLS)  # editor tools always available
    if comfy_key:
        mcp_servers.append({
            "type": "url",
            "url": "https://cloud.comfy.org/mcp",
            "name": "comfy-cloud",
            "authorization_token": comfy_key,
        })
        tools.append({"type": "mcp_toolset", "mcp_server_name": "comfy-cloud"})
    # User-registered MCP servers (data/mcp_servers.json). HTTP-transport only.
    # Anthropic handles the connection; we just declare them. Skip disabled
    # entries and anything missing a URL. Names have already been validated at
    # write time (letters/digits/_/-, no 'comfy-cloud' collision).
    for s in _load_mcp_servers():
        if not s.get("enabled", True): continue
        url = s.get("url")
        name = s.get("name")
        if not url or not name: continue
        entry = {"type": "url", "url": url, "name": name}
        tok = s.get("authorization_token")
        if tok: entry["authorization_token"] = tok
        mcp_servers.append(entry)
        tools.append({"type": "mcp_toolset", "mcp_server_name": name})

    import anthropic
    client = anthropic.Anthropic()
    # Anthropic caps requests at 4 cache_control blocks. The MCP beta and the
    # scene-context blocks stack up fast, and even "keep only the latest" hits
    # the ceiling on longer conversations. Strip cache_control from every user
    # message — keep it only on the system prompt (which is what actually
    # matters for cost, since the system prompt is huge and stable across all
    # turns). Scene-block caching wasn't earning much anyway because the scene
    # mutates almost every turn.
    trimmed_history = []
    for msg in history:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            trimmed_history.append(msg)
            continue
        new_content = []
        for block in content:
            if isinstance(block, dict) and "cache_control" in block:
                b = dict(block)
                b.pop("cache_control", None)
                new_content.append(b)
            else:
                new_content.append(block)
        trimmed_history.append({**msg, "content": new_content})
    kwargs = dict(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": ASSISTANT_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=trimmed_history,
        tools=tools,
    )
    if mcp_servers:
        kwargs["mcp_servers"] = mcp_servers
        kwargs["betas"] = ["mcp-client-2025-11-20"]
        return client.beta.messages.create(**kwargs)
    return client.messages.create(**kwargs)


@app.post("/api/llm/chat")
async def llm_chat(request: Request):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(400, "Anthropic API key not set — add it in Settings.")

    body = await request.json()
    node_id = str(body.get("node_id", "")).strip() or "default"
    ctx = body.get("context") or {}
    message = str(body.get("message", "")).strip()
    tool_results = body.get("tool_results")  # list of {tool_use_id, content, is_error?}

    if not message and not tool_results:
        raise HTTPException(400, "empty message")

    # Hydrate from disk on first request after a restart so mid-conversation
    # server crashes don't lose "where we are." Subsequent requests read
    # from memory directly.
    history = _load_chat_history(node_id)

    if tool_results:
        # Continuation turn — frontend ran the editor tools we asked for and is
        # now sending back the results. Guard against the "server restarted
        # mid-loop" case: `_chat_history` lives in memory, so `uvicorn --reload`
        # or a crash wipes it. If tool_results land after that, we'd end up
        # posting a tool_result as message 0 and Anthropic rejects it hard
        # ("tool_use_id has no matching tool_use in previous message").
        # Return a clean 409 so the frontend can reset instead of crashing.
        last = history[-1] if history else None
        expected_ids: set[str] = set()
        if isinstance(last, dict) and last.get("role") == "assistant":
            for block in last.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tuid = block.get("id")
                    if isinstance(tuid, str):
                        expected_ids.add(tuid)
        provided_ids = {r.get("tool_use_id") for r in tool_results if isinstance(r, dict)}
        if not expected_ids or not (provided_ids & expected_ids):
            raise HTTPException(
                409,
                "conversation state was lost (server restarted mid-turn). "
                "Clearing the chat and asking again will fix this.",
            )

        content_blocks = [{
            "type": "tool_result",
            "tool_use_id": r["tool_use_id"],
            "content": r.get("content", ""),
            **({"is_error": True} if r.get("is_error") else {}),
        } for r in tool_results if r.get("tool_use_id") in expected_ids]
        history.append({"role": "user", "content": content_blocks})
    else:
        # New user turn — prepend scene context (cached separately).
        scene_block = _format_scene_context(ctx)
        user_content = [
            {"type": "text", "text": scene_block, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": message},
        ] if scene_block else message
        history.append({"role": "user", "content": user_content})

    try:
        response = _call_claude(history, ctx)
    except Exception as e:
        history.pop()
        msg = str(e)[:500]
        print(f"[cb-app] llm_chat error: {msg}")
        raise HTTPException(502, f"Claude API call failed: {msg}")

    # Persist assistant turn (with tool_use blocks so we can continue the loop).
    assistant_blocks = _serialize_content_blocks(response.content)
    history.append({"role": "assistant", "content": assistant_blocks})

    # Extract text + editor tool_uses (anything Claude wants the frontend to run).
    reply_parts, pending_tools = [], []
    for block in assistant_blocks:
        t = block.get("type")
        if t == "text":
            reply_parts.append(block.get("text", ""))
        elif t == "tool_use" and block.get("name") in _EDITOR_TOOL_NAMES:
            pending_tools.append({
                "id": block.get("id"),
                "name": block.get("name"),
                "input": block.get("input") or {},
            })
    reply_text = "\n".join(p for p in reply_parts if p).strip()
    if not reply_text and not pending_tools:
        reply_text = "(no reply)"

    if len(history) > 40:
        # Sanitize the boundary — a bare tool_result at the new start would
        # violate Anthropic's invariant on the next call.
        _chat_history[node_id] = _sanitize_history(history[-20:])

    # Persist the turn so a server restart doesn't drop the conversation.
    _save_chat_history(node_id)

    usage = {
        "input_tokens": getattr(response.usage, "input_tokens", 0),
        "output_tokens": getattr(response.usage, "output_tokens", 0),
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
        "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
    }
    # Persist against today's bucket + lifetime total so the Settings surface
    # can show cumulative spend without needing the frontend to keep count.
    try:
        _record_turn_usage(usage)
    except Exception:
        pass  # never fail a chat turn because usage logging hiccuped
    # Per-turn detail log — powers the Debug page. Records both the token
    # breakdown AND the tool names Claude asked to run this turn, so we can
    # see which turns burn what, and whether the agent is actually reaching
    # for lazy-loaded docs (read_docs) vs guessing.
    try:
        from datetime import datetime
        user_preview = ""
        if message:
            user_preview = message[:200]
        elif tool_results:
            user_preview = f"(tool_results × {len(tool_results)})"
        # Roll up EVERY tool_use in this turn — editor tools, MCP tools, all of
        # them. Editor tools show up in `pending_tools` (already resolved above);
        # MCP tools live in `assistant_blocks` but weren't queued for the
        # frontend. Union both so the debug view captures the full picture.
        tool_uses = []
        seen_ids = set()
        for pt in pending_tools:
            if pt["id"] in seen_ids:
                continue
            seen_ids.add(pt["id"])
            tool_uses.append({"name": pt["name"], "kind": "editor"})
        for block in assistant_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                bid = block.get("id")
                if bid in seen_ids:
                    continue
                seen_ids.add(bid)
                tool_uses.append({"name": block.get("name") or "?", "kind": "mcp"})
        _record_turn_detail({
            "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "node_id": node_id,
            "kind": "tool_results" if tool_results else "user",
            "user_preview": user_preview,
            "reply_preview": reply_text[:200] if reply_text else "",
            "tool_uses": tool_uses,
            "usage": usage,
            "estimated_usd": round(_estimate_cost_usd(usage), 6),
        })
    except Exception:
        pass
    return {
        "reply": reply_text,
        "pending_tools": pending_tools,
        "stop_reason": getattr(response, "stop_reason", None),
        "usage": usage,
    }


@app.get("/api/llm/history")
async def llm_history(node_id: str = ""):
    """Return the persisted chat history for a node so the client can render
    the past conversation on page load. Only visible text is surfaced — we
    strip tool_use / tool_result blocks (they're noise in a rehydrated log)
    and keep just the user's text and the assistant's replies.
    Returns {messages: [{role, text}, ...]} — trailing pending tool_use turns
    are dropped since they'd be orphaned without a matching tool_result."""
    node_id = node_id.strip() or "default"
    history = _load_chat_history(node_id)
    out = []
    for msg in history:
        role = msg.get("role") if isinstance(msg, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if role not in ("user", "assistant"):
            continue
        # Flatten multi-block content down to plain text. Skip tool blocks —
        # they were part of the round-trip but shouldn't clutter the rehydrated
        # chat log.
        text_parts = []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text") or "")
                # Skip tool_use / tool_result / image blocks — they're not
                # user-legible on their own.
        text = "\n".join(p for p in text_parts if p).strip()
        # Strip the scene_context block that every user turn carries — it's a
        # verbose <scene_context>…</scene_context> payload the user never sees
        # in the live chat, so it shouldn't appear in the rehydrated log either.
        if text.startswith("<scene_context>"):
            end = text.find("</scene_context>")
            if end >= 0:
                text = text[end + len("</scene_context>"):].strip()
        if text:
            out.append({"role": role, "text": text})
    return JSONResponse({"messages": out}, headers=_NO_CACHE)


@app.post("/api/llm/reset")
async def llm_reset(request: Request):
    body = await request.json()
    node_id = str(body.get("node_id", "")).strip() or "default"
    _chat_history.pop(node_id, None)
    # Also remove the on-disk log so /clear is a true reset, not "clear this
    # session but rehydrate the old convo on next restart."
    try:
        p = _chat_history_path(node_id)
        if p.exists():
            p.unlink()
    except Exception:
        pass
    return {"cleared": True}


@app.post("/api/local/restart-comfy")
async def local_restart_comfy():
    """Ask ComfyUI-Manager to reboot the local ComfyUI. Only works when the
    Manager custom node is installed (which is standard on the common easy-
    install builds). Fires and returns — the Manager kills the current
    process, and the launcher relaunches it on its own. If Manager isn't
    present, ComfyUI returns 404 and we surface the error so the agent can
    fall back to telling the user to restart manually."""
    import httpx as _httpx
    # ComfyUI-Manager uses GET for /manager/reboot (POST returns 405). Some
    # forks expose /api/manager/reboot too — try the canonical path first,
    # fall back to the /api/ variant if the GET returns 404.
    async def _try_reboot(url):
        try:
            async with _httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(url)
                return r.status_code, r.text
        except _httpx.RemoteProtocolError:
            # Server dropped the connection while shutting down — the reboot
            # is in progress. Treat as success.
            return 200, "connection dropped mid-restart"
        except Exception as e:
            return None, str(e)

    urls = [f"{_LOCAL_COMFY_URL}/manager/reboot", f"{_LOCAL_COMFY_URL}/api/manager/reboot"]
    last_err = "unknown"
    for u in urls:
        status, body = await _try_reboot(u)
        if status is None:
            last_err = body
            continue
        if 200 <= status < 400 or status == 205:
            return {"restarted": True, "target": "local-comfyui"}
        if status == 404:
            last_err = "endpoint not found"
            continue
        last_err = f"HTTP {status}: {(body or '')[:200]}"
    if "not found" in last_err.lower():
        return {
            "restarted": False,
            "error": "ComfyUI-Manager not installed or doesn't expose /manager/reboot. Install https://github.com/Comfy-Org/ComfyUI-Manager and try again, or restart ComfyUI manually.",
        }
    return {"restarted": False, "error": last_err}


@app.post("/api/cancel-local")
async def cancel_local_comfy():
    """Interrupt whatever's currently running on the local ComfyUI at
    127.0.0.1:8188. Used by the cancel button next to the viewport Generate
    action so the user isn't stuck watching a stalled TripoSplat run. Best-effort
    — returns 200 even if ComfyUI isn't reachable so the frontend can still
    clear its own busy state."""
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post("http://127.0.0.1:8188/interrupt")
            r.raise_for_status()
        return {"cancelled": True, "target": "local-comfyui"}
    except Exception as e:
        return {"cancelled": False, "target": "local-comfyui", "error": str(e)}


# ---------- assets browser (generated outputs) ----------
#
# Lists everything in DATA_DIR whose filename starts with `out_` (the prefix
# the gen modules use). Returns newest-first with the URL the static /data
# mount serves, plus filename, kind (image|video), size, and mtime. The UI
# pulls this for the Assets modal so the user can drag previously-generated
# results back into the editor.
@app.get("/api/assets/list")
async def assets_list(limit: int = 200):
    items = []
    # Two-pass so we can pair 3D outputs with their `<stem>.thumb.<ext>`
    # companion image before returning. Skip .thumb.* files from the listing
    # itself — they're an implementation detail, not standalone assets.
    thumbs: dict[str, Path] = {}
    all_files: list[Path] = []
    # rglob so we pick up outputs routed into type-partitioned subfolders
    # (images/, videos/, 3d/) as well as legacy flat files in DATA_DIR root.
    for p in DATA_DIR.rglob("out_*.*"):
        if not p.is_file():
            continue
        # Skip the on-disk thumbnail cache (`.thumbs/<size>/…`). Its cached
        # tiles preserve the source `out_*` filename, so a naive rglob would
        # surface them as if they were standalone assets — the listing then
        # returns tiny 256px JPGs where the full-res original should be.
        if any(part == ".thumbs" for part in p.parts):
            continue
        # Match `out_<stem>.thumb.<ext>` — .stem strips the last extension
        # only, so we still see `.thumb` in the remaining name.
        if p.stem.endswith(".thumb"):
            base = p.stem[: -len(".thumb")]  # drop `.thumb`
            thumbs[base] = p
            continue
        all_files.append(p)

    for p in all_files:
        ext = p.suffix.lower().lstrip(".")
        kind = None
        # Blockout snapshots get their own kind so the Output pane's
        # Blockout tab can filter them from AI-generated images. Prefix
        # `out_blockout_` is set by /save_blockout.
        if p.name.startswith("out_blockout_") and ext in {"png", "jpg", "jpeg", "webp"}:
            kind = "blockout"
        elif ext in {"png", "jpg", "jpeg", "webp"}:
            kind = "image"
        elif ext in {"mp4", "webm", "mov"}:
            kind = "video"
        elif ext in {"glb", "gltf", "obj", "fbx", "ply", "spz", "splat", "ksplat"}:
            kind = "3d"
        if not kind:
            continue
        try:
            rel = p.relative_to(DATA_DIR).as_posix()
        except ValueError:
            continue
        st = p.stat()
        entry = {
            "url": f"/output/{rel}",
            "filename": p.name,
            "kind": kind,
            "ext": ext,
            "size": st.st_size,
            "mtime": st.st_mtime,
        }
        # Attach paired thumb if we saved one (currently only 3D outputs do).
        thumb = thumbs.get(p.stem)
        if thumb:
            try:
                trel = thumb.relative_to(DATA_DIR).as_posix()
                entry["thumbUrl"] = f"/output/{trel}"
            except ValueError:
                pass
        items.append(entry)
    items.sort(key=lambda x: x["mtime"], reverse=True)
    # Populate a thumbUrl for image + video items so the grid renders lightweight
    # JPEG posters instead of decoding full-res PNGs / video headers. 3D outputs
    # already have their own paired .thumb.<ext> attached above.
    for entry in items:
        if entry["kind"] in ("image", "video", "blockout") and "thumbUrl" not in entry:
            entry["thumbUrl"] = f"/api/assets/thumb?path={entry['url'].split('/output/', 1)[1]}&size=256"
    total = len(items)
    n = max(1, min(500, int(limit)))
    return {"assets": items[:n], "total": total}


# ---------- lazy asset thumbnails ----------
#
# The assets grid used to load full-res PNGs (multi-MB each) + force <video>
# elements to fetch metadata just to poster the tile. This endpoint generates
# a small JPEG once per source and caches it under output/.thumbs/<size>/,
# so subsequent grid loads are ~50–100 kB per tile instead of megabytes.

_THUMB_ROOT = DATA_DIR / ".thumbs"

def _thumb_cache_path(rel: str, size: int) -> Path:
    # Same basename as the source, .jpg extension — reader can trace tiles
    # back to originals easily when spelunking on disk.
    stem = Path(rel).stem
    # Include the immediate parent (images/videos/3d) to avoid same-stem
    # collisions across the type-partitioned subfolders.
    parent = Path(rel).parent.as_posix() if Path(rel).parent.as_posix() != "." else "root"
    d = _THUMB_ROOT / str(int(size)) / parent
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{stem}.jpg"

def _make_image_thumb(src: Path, dst: Path, size: int) -> None:
    from PIL import Image
    with Image.open(src) as im:
        im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
        im.thumbnail((size, size), Image.LANCZOS)
        im.save(dst, "JPEG", quality=82, optimize=True)

def _make_video_thumb(src: Path, dst: Path, size: int) -> None:
    # cv2 is already a dep (opencv-python in requirements.txt). Grab a frame
    # ~0.5s in to skip black lead-ins that a lot of our recordings ship with.
    import cv2
    cap = cv2.VideoCapture(str(src))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(fps * 0.5))
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError("no frames")
    finally:
        cap.release()
    # OpenCV reads BGR — convert to RGB for PIL. Also resize proportionally
    # so the thumb never exceeds `size` on its longest edge.
    from PIL import Image
    import numpy as np
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    im = Image.fromarray(rgb)
    im.thumbnail((size, size), Image.LANCZOS)
    im.save(dst, "JPEG", quality=82, optimize=True)

@app.get("/api/assets/thumb")
async def assets_thumb(path: str, size: int = 256):
    from fastapi.responses import FileResponse
    # Sanity-clamp size so a bad query can't ask for a 4K thumb.
    size = max(64, min(1024, int(size)))
    # Reject anything trying to escape DATA_DIR — path traversal guard.
    src = (DATA_DIR / path).resolve()
    try:
        src.relative_to(DATA_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "path outside output directory")
    if not src.exists() or not src.is_file():
        raise HTTPException(404, "source not found")
    dst = _thumb_cache_path(path, size)
    # Regenerate if the source was modified after the cached thumb, or if
    # there's no cache yet.
    needs_rebuild = (not dst.exists()) or (dst.stat().st_mtime < src.stat().st_mtime)
    if needs_rebuild:
        try:
            ext = src.suffix.lower().lstrip(".")
            if ext in {"png", "jpg", "jpeg", "webp"}:
                _make_image_thumb(src, dst, size)
            elif ext in {"mp4", "webm", "mov"}:
                _make_video_thumb(src, dst, size)
            else:
                raise HTTPException(415, f"cannot thumbnail .{ext}")
        except HTTPException:
            raise
        except Exception as e:
            # Best-effort — if the source is a partial file or codec is
            # unsupported, we surface a 500 and let the frontend fall back
            # to asset.url via its onerror handler.
            raise HTTPException(500, f"thumb failed: {e}")
    return FileResponse(dst, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/assets/upload")
async def assets_upload(file: UploadFile = File(...), prefix: str = "upload"):
    """Drop a client-side image directly into the Assets library (Output pane).
    Used by MediaPipe still capture and any other in-app image producer that
    wants its output to appear alongside generated renders instead of in the
    per-object refs store."""
    from server.modules._base import new_output_path
    ext = (Path(file.filename or "").suffix.lstrip(".").lower()) or "png"
    if ext not in {"png", "jpg", "jpeg", "webp"}:
        raise HTTPException(400, f"unsupported image ext: {ext}")
    safe_prefix = "".join(c if c.isalnum() or c in "-_" else "_" for c in prefix)[:32] or "upload"
    dst = new_output_path(DATA_DIR, safe_prefix, ext)
    data = await file.read()
    dst.write_bytes(data)
    rel = dst.relative_to(DATA_DIR).as_posix()
    return {"url": f"/output/{rel}", "filename": dst.name, "kind": "image"}


@app.delete("/api/assets/{filename}")
async def assets_delete(filename: str):
    # Safety: only allow deleting files matching the `out_*.{ext}` shape we serve.
    safe = Path(filename).name  # strip any path components
    if not safe.startswith("out_"):
        raise HTTPException(400, "filename outside the assets namespace")
    # Files now live in type-partitioned subfolders (images/videos/3d) — search
    # them plus DATA_DIR root for legacy flat files. Take the first hit.
    p = None
    for candidate in (DATA_DIR / "images" / safe,
                      DATA_DIR / "videos" / safe,
                      DATA_DIR / "3d" / safe,
                      DATA_DIR / safe):
        if candidate.exists() and candidate.is_file():
            p = candidate
            break
    if p:
        # Windows: static file handler / thumb generator may still hold a
        # handle for a few ms after the last tile paint. Short retry loop
        # turns transient PermissionError into eventual success instead of
        # a silent failure that resurrects the tile on next refresh.
        import time as _t
        last_err = None
        for _ in range(5):
            try:
                p.unlink()
                last_err = None
                break
            except PermissionError as e:
                last_err = e
                _t.sleep(0.05)
            except Exception as e:
                last_err = e
                break
        if last_err:
            raise HTTPException(500, f"delete failed: {last_err}")
        # Nuke every cached thumb for this source (all sizes, all subfolder
        # variants). Without this the .thumbs cache balloons over time and
        # a re-uploaded file with the same stem serves the stale thumb.
        try:
            for thumb in _THUMB_ROOT.rglob(f"{p.stem}.jpg"):
                try: thumb.unlink()
                except Exception: pass
        except Exception:
            pass
    return {"deleted": True, "filename": safe}


# ---------- data static mount (must be defined after all explicit routes) ----------

app.mount("/output", StaticFiles(directory=str(DATA_DIR)), name="data")

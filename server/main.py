"""ComfyBlockout-app sidecar — serves the editor, persists scenes/assets/recordings,
and runs cloud generations via `comfy generate <model> ...` modules.

No ComfyUI in the loop. Auth lives in comfy-cli (OAuth via `comfy cloud login`,
or COMFY_API_KEY env var as a fallback)."""

from __future__ import annotations

import asyncio
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
DATA_DIR = APP_DIR / "data"
WEB_DIR = APP_DIR / "web"
ENV_PATH = APP_DIR / ".env"
DATA_DIR.mkdir(parents=True, exist_ok=True)


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
    "STRICT SPATIAL REPLACEMENT TASK.\n\n"
    "IMAGE 1 (always the first attached image) is a 3D blockout — a low-fidelity "
    "scene render with simple colored shapes that act as stencils for the final "
    "objects. ANY ADDITIONAL IMAGES (image 2, 3, …) are per-object MATERIAL "
    "SWATCHES — they show the desired surface/finish/texture for a specific "
    "colored shape in image 1; the SCENE INVENTORY below tells you which image "
    "number maps to which named object.\n\n"
    "Your job: produce a single new image that matches IMAGE 1 spatially "
    "(camera, perspective, aspect ratio, position and SCALE of every colored "
    "shape), but renders each colored shape as the subject described in the "
    "user prompt + scene inventory, with its surface drawn from the matching "
    "material swatch image.\n\n"
    "HARD RULES — do not violate:\n"
    "1. Each colored shape in image 1 has a screen-space footprint (X%, Y%, "
    "size%). The replacement object occupies that EXACT footprint. Never scale "
    "up to match what would 'normally' fit the environment. If the blockout "
    "shows a 15%-of-frame cube on a city street, the cube stays 15% of the "
    "frame in the output — it does NOT become a building. The blockout WINS "
    "over semantic expectations.\n"
    "2. The replacement object's center is at the SAME pixel coordinates as the "
    "colored shape's center in image 1.\n"
    "3. Preserve image 1's camera angle, perspective, aspect ratio, and the "
    "ground plane implied by the perspective grid.\n"
    "4. The perspective grid lines themselves are scaffolding — do NOT draw "
    "them in the output.\n"
    "5. One colored shape → one object. Do not add extra instances.\n"
    "6. The colored shape's tint is metadata identifying the object — it is NOT "
    "the final object's color. Pull color/material from the matching material "
    "swatch image (if any), otherwise from the user prompt.\n\n"
    "REFERENCE IMAGES (image 2+) ARE MATERIAL SWATCHES, NOT COMPOSITION:\n"
    "- Pull ONLY surface qualities from them: color, texture, finish, "
    "micro-detail, weathering, sheen, pattern, motif.\n"
    "- IGNORE everything else from those images: their framing, scale, camera "
    "angle, lighting direction, background, any other objects, depth-of-field. "
    "Treat each reference image as if it were a flat material chip swatched "
    "from a sample book.\n"
    "- Do NOT copy the reference image's subject as a whole. If reference image "
    "2 shows a galaxy nebula scene with planets and stars, only the cosmic "
    "swirl / color palette / surface texture gets applied to the object — the "
    "planets, stars, and overall composition stay OUT of the output.\n\n"
    "WHAT TO INVENT vs PRESERVE:\n"
    "- Invent from user prompt: lighting, mood, background environment, weather, "
    "time of day, secondary scene elements around the object.\n"
    "- Invent from material swatch images (surface only): the object's texture, "
    "finish, color palette, micro-detail.\n"
    "- Preserve from image 1: object position, object SCALE (most important), "
    "object silhouette, camera framing, perspective."
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

    # Also drop a timestamped copy into the assets namespace so the recording shows
    # up in the Assets modal (glob "out_*") and can be dragged into a Seedance Ref
    # video slot. Distinct filename per recording preserves history — the primary
    # node_<UID>.mp4 keeps getting overwritten as before for /comfyblockout/video/<id>.
    out_url = None
    out_filename = None
    try:
        import shutil, time
        out_ext = final_path.suffix.lower() or ".mp4"
        out_filename = f"out_rec_{node_id}_{int(time.time() * 1000)}{out_ext}"
        out_path = DATA_DIR / out_filename
        shutil.copyfile(final_path, out_path)
        out_url = f"/data/{out_filename}"
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
    names = sorted([p.name for p in root.iterdir() if p.is_dir() and (p / "scene.json").exists()])
    return JSONResponse({"projects": names})


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
        "local_url": f"/data/refs/{local_path.name}",
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
        "util": bool(getattr(m, "util", False)),
        "icon": getattr(m, "icon", "") or "",
    }


@app.get("/api/modules")
async def list_modules():
    return {"modules": [_module_dict(m) for m in MODULES.values()]}


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


@app.post("/api/run/{module_id}")
async def run_module(module_id: str, request: Request):
    m = MODULES.get(module_id)
    if not m or not m.run:
        raise HTTPException(404, f"unknown module: {module_id}")
    body = await request.json()
    node_id = str(body.get("node_id", "")).strip() or "default"
    inputs = dict(body.get("inputs") or {})

    # Resolve scene-image / scene-video into concrete file paths from the editor's saved state.
    for spec in m.inputs:
        if spec.get("type") == "scene-image":
            info = _image_store.get(node_id)
            if not info or not Path(info["path"]).exists():
                raise HTTPException(400, "no scene image saved — snapshot in the editor first")
            inputs["image_path"] = Path(info["path"])
        elif spec.get("type") == "scene-video":
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
            raise HTTPException(400, "no scene video — record from the transport (● button on the timeline) or load a Background video in Scene Properties")

    # Optional image_url override — the frontend's gen-cell source-image slot passes
    # this when the user picked/dragged a specific image instead of using the auto
    # viewport snapshot. Resolve /data/... to a local file; anything else gets
    # fetched to a temp file for `comfy upload` to consume.
    image_url = inputs.pop("image_url", None)
    if image_url:
        import tempfile as _tempfile, urllib.request as _urlreq
        try:
            if image_url.startswith("/data/"):
                candidate = DATA_DIR / image_url[len("/data/"):]
                if not candidate.exists():
                    raise HTTPException(400, f"source image not found: {image_url}")
                inputs["image_path"] = candidate
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
            lines.append(
                f"- {name} ({color} {kind}): centered at x={x}% y={y}% "
                f"of frame, occupies ~{w}%×{h}% of frame{ref_note}{notes_note}"
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
    user_prompt = (inputs.get("prompt") or "").strip()
    if user_prompt:
        tweaks = (_prompt_store.get(node_id) or "").strip()
        inventory = _format_inventory(scene_objects, camera_meta)
        parts = [BASE_PROMPT.strip()]
        if inventory:
            parts.append(inventory)
        if tweaks:
            parts.append(f"ADDITIONAL TWEAKS:\n{tweaks}")
        parts.append(f"USER REQUEST: {user_prompt}")
        inputs["prompt"] = "\n\n".join(parts)

    try:
        result = await m.run(data_dir=DATA_DIR, **inputs)
    except ValueError as e:
        raise HTTPException(400, str(e) or "bad input")
    except RuntimeError as e:
        msg = str(e) or "comfy generate failed (no stderr/stdout captured)"
        print(f"[cb-app] run_module {module_id} RuntimeError: {msg}")
        raise HTTPException(500, msg)
    except Exception as e:
        print(f"[cb-app] run_module {module_id} {type(e).__name__}: {e}")
        raise HTTPException(500, f"{type(e).__name__}: {e}")

    out_path = Path(result["path"])
    try:
        rel = out_path.relative_to(DATA_DIR).as_posix()
        result["url"] = f"/data/{rel}"
    except ValueError:
        result["url"] = None

    # Convert intermediate file paths to /data/ URLs so the frontend can render
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
                inter["url"] = f"/data/{rel_i}"
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
    "You are an in-editor agent for ComfyBlockout, a 3D blockout tool that "
    "feeds scenes to generative image/video models via Comfy Cloud. You help "
    "the user refine prompts, build generation JSONs, reason about their "
    "scene, AND directly manipulate the scene via editor tools (add_primitive, "
    "delete_object, set_object_color, set_object_position, set_object_rotation, "
    "set_object_scale, rename_object, list_objects, set_generator_prompt). "
    "When the user asks to add/move/recolor/delete something, call the tool — "
    "don't just describe how they could do it manually.\n\n"
    "TO ACTUALLY GENERATE: prefer the `trigger_generate` editor tool over the "
    "raw Comfy MCP tools. trigger_generate uses the editor's own pipeline, so the "
    "result lands in the viewport overlay AND in the user's Assets pane "
    "(double-clickable, draggable, persistent). The raw MCP tools should only be "
    "used for inspection or for advanced flows the editor doesn't expose.\n\n"
    "TERMINOLOGY: the editor's left panel has a single WORKFLOWS section that "
    "mixes two flavors — partner-API workflows (Nano Banana, Seedance, Tripo, "
    "etc., which route through Comfy Cloud) and local ComfyUI workflows (the "
    "manifest-driven modules registered via `create_workflow_module`). Both "
    "are called 'workflows' in the UI. The scene_context block distinguishes "
    "them via `source: api` vs `source: local` so you know which runtime "
    "path each takes. Some legacy tool names still say \"generator\" (e.g. "
    "`set_generator_prompt`) — they work the same for either kind.\n\n"
    "CREATING NEW WORKFLOWS: if the user asks for a workflow that doesn't "
    "exist yet (e.g. \"make me a local Flux 2 Klein T2I workflow\", \"add an "
    "SDXL text-to-image workflow\", \"build a Wan video workflow\"), use the "
    "`create_workflow_module` editor tool:\n"
    "1. Get the workflow — either fetch a matching template via the Comfy Cloud "
    "MCP `get_template` tool, or construct it yourself from ComfyUI nodes if you "
    "know the shape. Ask `search_templates` first to find a match.\n"
    "2. Decide user-facing inputs. Expose ONLY what changes per run (prompt, seed "
    "if the user cares, source image for image-edit workflows). Everything else "
    "stays baked into the workflow.\n"
    "3. For each input, work out the node id + widget slot to patch. Include both "
    "widget_index (0-based) AND widget_name (e.g. \"text\" for CLIPTextEncode) — "
    "the cloud runner uses index, the local runner uses name.\n"
    "3a. LABEL FORMAT: keep the module `label` short enough to fit on ONE line in "
    "a ~200px cell — roughly 24 chars. Use `·` (middle dot) as a separator and "
    "prefer abbreviations: T2I / I2I / T2V / I2V / T2M / I2M / T2S / Depth / "
    "Upscale / Remove BG / Extend / etc. Include \"Local\" or \"Cloud\" only when "
    "both variants might exist. Good: `Flux 2 Klein · T2I · Local`, `SDXL · I2I`, "
    "`Tripo · I2M`. Bad: `Flux 2 Klein — Text to Image (Local)`.\n"
    "3b. LATENT ASPECT RATIO: for workflows with a `scene-image` input, the local "
    "runner auto-patches the EmptyLatentImage / EmptySDXLLatentImage / "
    "EmptySD3LatentImage width and height at run time to match the viewport "
    "snapshot's aspect ratio (long side preserved, snapped to /64). Don't hand-"
    "code a square 1024×1024 assuming the user's viewport is square — leave the "
    "workflow's authored resolution and the runner will reshape it. Only override "
    "if you specifically want to lock a resolution.\n"
    "3d. SEED + STRENGTH: for any KSampler in the workflow, always expose its "
    "seed as `type: \"seed\"` (the UI adds a 🎲/🔒 random-vs-fixed toggle — "
    "random by default, user can lock a seed they liked). For ControlNet, "
    "IPAdapter, or LoRA strength widgets that materially affect the output "
    "(typical 0-1 range), expose as `type: \"number\"` with `default`, `min: 0`, "
    "`max: 1`, `step: 0.05`. Same treatment for CFG when it's not baked in. "
    "These are the two most-common per-run knobs; skipping them forces the "
    "user back into the raw workflow JSON.\n"
    "3c. PREPROCESSOR PREVIEWS: if the workflow has a visual preprocessor stage "
    "(depth, canny, pose, normal, seg, lineart, HED, MiDaS, Zoe, Marigold, "
    "OpenPose, etc.), declare it under `intermediates`: "
    "`[{name, label, source_node_id, source_slot}]`. The runner splices a "
    "SaveImage onto that node and the editor shows a preview tab (e.g. DEPTH) "
    "between BLOCKOUT and RENDER, so the user can compare the preprocessor "
    "output against the final image. `source_node_id` is the preprocessor's "
    "numeric id; `source_slot` is 0 for its main image output.\n"
    "4. If the user asked for a LOCAL generator, set runner=\"local\" and pass the "
    "workflow in ComfyUI's API/prompt format (dict keyed by node id, each entry "
    "has class_type + inputs). If the workflow you have is in graph/save format "
    "(top-level nodes[] array), convert it first — you know the node schemas.\n"
    "5. Call `create_workflow_module`. The workflow cell appears in the WORKFLOWS "
    "section immediately — no reload needed. To FIX or REPLACE an existing workflow "
    "module (e.g. wrong text encoder, missing node, agent mistake in the first pass), "
    "call `get_workflow_module` with its id to see the current JSON, work out what "
    "needs to change, then re-call `create_workflow_module` with the same id and the "
    "corrected workflow — the register endpoint overwrites atomically and hot-"
    "registers the updated module in place.\n"
    "6. If runner=\"local\", IMMEDIATELY call `check_custom_nodes` with every "
    "third-party node class_type the workflow references (skip built-ins like "
    "KSampler / CLIPTextEncode / VAEDecode / etc.). For every missing repo, "
    "IMMEDIATELY call `install_custom_node` yourself — the tool clones AND "
    "auto-pips the requirements against ComfyUI's own Python. Do NOT hand the "
    "user manual `git clone` or `pip install` commands; the tool does both in "
    "one round-trip. If `pip_ok: true` (deps installed), IMMEDIATELY call "
    "`restart_comfy` — do NOT ask the user to Ctrl+C. Only fall back to asking "
    "the user manually when the tool reports `pip_ran: false` (ComfyUI's Python "
    "interpreter wasn't detected) or `restart_comfy` returns restarted=false "
    "with a manager-missing error.\n"
    "7. Also call `check_local_models` with every model file (checkpoints, VAEs, "
    "text encoders, LoRAs, etc.). Format the result as a compact bulleted list "
    "(the chat panel is narrow — NO wide tables). For any missing model, group by "
    "folder and give a short HuggingFace slug, then ask \"want me to download "
    "it for you?\" before doing anything.\n"
    "8. If the user confirms downloads, call `download_model_to_comfy` with the "
    "direct HF URL (https://huggingface.co/<repo>/resolve/main/<path>), the "
    "ComfyUI folder, and the exact filename. Multiple missing models = call the "
    "tool sequentially, one per file — don't parallelize; the server writes "
    ".part files that could collide.\n\n"
    "Keep responses tight. Quote object names with brackets like [Cube.001] when "
    "referring to scene objects — the editor renders those tokens in the object's "
    "color and uses them to attach the per-object reference image at generate time."
)


# Editor tools — Claude calls these via tool_use; the frontend executes them
# and posts the result back via /api/llm/chat with tool_results. Schemas mirror
# what the JS dispatcher in editor.html knows how to run.
EDITOR_TOOLS = [
    {
        "name": "add_primitive",
        "description": "Add a SINGLE primitive object to the scene. For multiple at once, use batch_add_primitives instead — it's ~10x faster than calling this in a loop.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["cube", "sphere", "capsule", "cylinder", "cone", "plane", "particles"],
                    "description": "Primitive type to add",
                },
                "color": {"type": "string", "description": "Optional hex color like #ff5fbf"},
                "position": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 3, "maxItems": 3,
                    "description": "Optional [x,y,z] world position",
                },
            },
            "required": ["kind"],
        },
    },
    {
        "name": "batch_add_primitives",
        "description": "Add MANY primitives in one call — use this for arrays, grids, patterns, or any multi-object placement. Massively faster than looping add_primitive.",
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
                                "enum": ["cube", "sphere", "capsule", "cylinder", "cone", "plane", "particles"],
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
        "description": "Set an object's rotation in degrees (Euler XYZ).",
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
        "description": (
            "Set the prompt on a generator cell. Works on the built-in cells "
            "(model: 'nano-banana' or 'seedance') AND on any agent-created WORKFLOW "
            "module (pass its module id, e.g. 'flux2_klein_t2i_local'). For workflow "
            "modules the tool writes to whichever input is named 'prompt' in the "
            "manifest. For setting multiple named inputs on a workflow (positive + "
            "negative prompt, strength, etc.) prefer `set_workflow_inputs` — this "
            "single-field variant is kept for the common 'just update the prompt' case."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "'nano-banana', 'seedance', or a WORKFLOW module id"},
                "prompt": {"type": "string"},
            },
            "required": ["model", "prompt"],
        },
    },
    {
        "name": "set_workflow_inputs",
        "description": (
            "Batch-set multiple named inputs on a WORKFLOW module in one call — "
            "e.g. positive prompt + negative prompt + strength together. Pass an "
            "'inputs' object keyed by the input names declared in the module's manifest "
            "(check activeWorkflowInputs in the scene context to see what's available). "
            "Values are coerced to strings. Unknown input names are ignored and reported "
            "back so you can correct spelling. Use this instead of calling "
            "set_generator_prompt repeatedly when multiple fields need to change."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "WORKFLOW module id (matches activeWorkflowModuleId in scene context)"},
                "inputs": {
                    "type": "object",
                    "description": "Map of {input_name: value}. Input names must match those declared in the module's manifest.",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["model", "inputs"],
        },
    },
    {
        "name": "trigger_generate",
        "description": (
            "Run the named generator cell through the editor's own pipeline (same as "
            "the user clicking the viewport Generate button). The result lands in the "
            "viewport overlay AND in the Assets pane (persisted to disk). Prefer this "
            "over raw Comfy MCP tools when the user asks to generate something — the "
            "raw MCP tools' output won't be saved to the Assets pane."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "enum": ["nano-banana", "seedance"]},
                "prompt": {
                    "type": "string",
                    "description": "Optional. If provided, sets the cell prompt first, then runs. Omit to use the cell's existing prompt.",
                },
            },
            "required": ["model"],
        },
    },
    {
        "name": "create_workflow_module",
        "description": (
            "Register a new generator based on a ComfyUI workflow you constructed or "
            "fetched. The new module appears as a cell in the editor's WORKFLOW section "
            "and runs through the same pipeline as the built-in generators (output "
            "lands in Assets, cell is clickable/removable, etc.). Use when the user "
            "asks for a generator that doesn't already exist. Also use to FIX or "
            "REPLACE an existing workflow module — passing the same `id` overwrites "
            "the workflow JSON and manifest atomically, then re-registers. Call "
            "`get_workflow_module` first if you need to see what's currently saved "
            "before rewriting. See the system prompt for the full construction "
            "protocol."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "snake_case identifier, unique per workflow (e.g. 'flux2_klein_t2i_local')",
                },
                "label": {
                    "type": "string",
                    "description": "Human-readable title for the cell (e.g. 'Flux.2 Klein — Text to Image (Local)')",
                },
                "kind": {
                    "type": "string",
                    "enum": ["image", "video", "3d", "audio"],
                    "description": "What the workflow produces. Determines the icon and how the result is imported.",
                },
                "output_ext": {
                    "type": "string",
                    "description": "Expected output extension without the dot (e.g. 'png', 'mp4', 'glb', 'wav')",
                },
                "runner": {
                    "type": "string",
                    "enum": ["cloud", "local"],
                    "description": "Where the workflow will run. Defaults to 'cloud' if omitted.",
                },
                "workflow": {
                    "type": "object",
                    "description": (
                        "The full workflow JSON. For runner='cloud' use ComfyUI graph/save "
                        "format (top-level nodes[] + links[]). For runner='local' use API/"
                        "prompt format (flat dict keyed by node id string; each value has "
                        "class_type + inputs + optional _meta.title). Convert format yourself "
                        "if needed."
                    ),
                },
                "intermediates": {
                    "type": "array",
                    "description": (
                        "Optional preprocessor previews (depth, canny, pose, normal, seg, "
                        "lineart, etc.). Each entry causes the runner to splice a SaveImage "
                        "onto the named node's output slot; the editor renders it as a preview "
                        "tab (e.g. DEPTH) between BLOCKOUT and RENDER so the user can compare "
                        "the preprocessor output to the final image. Skip when the workflow "
                        "has no visual preprocessor stage (pure T2I, etc.)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "snake_case identifier (e.g. 'depth')"},
                            "label": {"type": "string", "description": "Short tab label — 1-2 words (e.g. 'Depth')"},
                            "source_node_id": {"type": "integer", "description": "Numeric id of the preprocessor node whose output should be saved"},
                            "source_slot": {"type": "integer", "description": "Output socket index on that node — 0 for the primary image output"},
                            "filename_prefix": {"type": "string", "description": "Optional. Defaults to intermediate_<name>."},
                        },
                        "required": ["name", "label", "source_node_id"],
                    },
                },
                "inputs": {
                    "type": "array",
                    "description": (
                        "User-facing inputs the editor should render on the cell. Each entry "
                        "specifies which node's widget the input patches at run time. "
                        "Expose only inputs that change per run — everything else stays "
                        "baked into the workflow."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "snake_case kwarg name (e.g. 'prompt', 'seed', 'image')",
                            },
                            "type": {
                                "type": "string",
                                "enum": ["textarea", "text", "scene-image", "scene-video", "number", "seed", "dropdown"],
                                "description": (
                                    "UI control type. 'textarea' = multi-line prompt, 'text' = "
                                    "single-line, 'scene-image' = uses the editor's viewport "
                                    "snapshot (or a picked source image) — the runner uploads it "
                                    "and patches the LoadImage node with the returned filename, "
                                    "'scene-video' = uses the video the user set as the Scene "
                                    "Properties → Background → Video (falls back to a recorded "
                                    "clip) — the runner uploads the file to ComfyUI's input dir "
                                    "and patches the LoadVideo / VHS_LoadVideo node's `video` "
                                    "widget with the resulting filename. No UI drop slot; the "
                                    "user configures the video in Scene Properties. "
                                    "'number' = numeric field (supports optional `default`, `min`, "
                                    "`max`, `step`), 'seed' = numeric field with a 🎲/🔒 random-vs-"
                                    "fixed toggle. Patch 'seed' onto KSampler.seed (or an int "
                                    "primitive feeding it) so a fresh int is minted per run. "
                                    "'dropdown' = <select> populated from `options` — use this for "
                                    "any Comfy COMBO widget (preprocessor pickers, sampler names, "
                                    "checkpoint names, etc.). The selected string is forwarded "
                                    "verbatim to the widget."
                                ),
                            },
                            "required": {"type": "boolean"},
                            "placeholder": {
                                "type": "string",
                                "description": "Helper text shown in an empty input.",
                            },
                            "label": {
                                "type": "string",
                                "description": "Optional custom UI label (falls back to `name`).",
                            },
                            "default": {"description": "Optional default value for number/seed/dropdown fields (e.g. 0.8 for a ControlNet strength, 'DepthAnythingV2Preprocessor' for an AIO Aux preprocessor picker)."},
                            "min": {"type": "number", "description": "Optional numeric lower bound (number type)."},
                            "max": {"type": "number", "description": "Optional numeric upper bound (number type)."},
                            "step": {"type": "number", "description": "Optional numeric step (e.g. 0.05 for strengths, 1 for counts)."},
                            "options": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "For type='dropdown' — full list of allowed values shown as a <select>. Ordered exactly as they should appear in the menu. Include every valid option from the Comfy COMBO widget so the user isn't guessing.",
                            },
                            "patch": {
                                "type": "object",
                                "description": (
                                    "How to write this input into the workflow at run time. "
                                    "Provide widget_index for the cloud runner (0-based position "
                                    "in the node's widgets_values array) AND widget_name for the "
                                    "local runner (the input key name in ComfyUI API format, e.g. "
                                    "'text' for CLIPTextEncode, 'image' for LoadImage, 'seed' for "
                                    "KSampler)."
                                ),
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
                "util": {
                    "type": "boolean",
                    "description": (
                        "True → register as a UTILITY (button in the Tools grid, "
                        "output feeds Assets for downstream workflows to consume) "
                        "instead of a generator (cell in the WORKFLOWS section, "
                        "output is a final render). Use for workflows whose "
                        "purpose is media transformation (pose extraction from a "
                        "video, background removal, edge/depth preview, etc). "
                        "Filename prefix convention: `util_<name>.json`."
                    ),
                },
                "icon": {
                    "type": "string",
                    "description": (
                        "Optional inline SVG markup for the Tools button when "
                        "util=True (e.g. `<svg viewBox='0 0 24 24' ...>...</svg>`). "
                        "Small, 24x24, stroke-based to match the other tool "
                        "icons. Falls back to a generic utility glyph."
                    ),
                },
            },
            "required": ["id", "label", "kind", "output_ext", "workflow", "inputs"],
        },
    },
    {
        "name": "download_model_to_comfy",
        "description": (
            "Download a model file straight into the user's local ComfyUI models "
            "directory. Use ONLY after check_local_models flags something as "
            "missing AND the user confirmed they want you to fetch it. The "
            "backend detects the ComfyUI install path and drops the file into "
            "models/<folder>/. Progress streams into the chat automatically; "
            "the tool_result reports the final on-disk path or an error. Prefer "
            "direct HuggingFace URLs (https://huggingface.co/<repo>/resolve/main/<path>) "
            "since those don't need auth for public models."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Direct download URL to the raw file (e.g. huggingface.co/…/resolve/main/…)",
                },
                "folder": {
                    "type": "string",
                    "description": "ComfyUI models sub-folder (diffusion_models, vae, text_encoders, checkpoints, loras, controlnet, etc.)",
                },
                "filename": {
                    "type": "string",
                    "description": "Filename to save as — must match what the workflow references",
                },
            },
            "required": ["url", "folder", "filename"],
        },
    },
    {
        "name": "get_workflow_module",
        "description": (
            "Read back the manifest AND workflow JSON currently saved for a "
            "workflow module (source='workflow'). Use before proposing a fix so "
            "you can see what's actually there instead of guessing. Returns "
            "{id, manifest, workflow}. Refuses to return built-in Python modules."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The workflow module id (e.g. 'flux2_klein_t2i_local')"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "check_custom_nodes",
        "description": (
            "Verify which ComfyUI custom-node class_types are installed locally. "
            "Call this AFTER create_workflow_module with runner='local', BEFORE "
            "downloading models — a workflow that references TripoSplat, Nunchaku, "
            "or another third-party node type will crash at run time with a "
            "cryptic KeyError if the nodes aren't installed. Returns "
            "{reachable, missing: [class_type, ...], present: [class_type, ...]}. "
            "Missing ones can be resolved via install_custom_node."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "class_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Distinct node class_types the workflow uses (not the built-in ones like KSampler / CLIPTextEncode — only third-party ones).",
                    "minItems": 1,
                },
            },
            "required": ["class_types"],
        },
    },
    {
        "name": "install_custom_node",
        "description": (
            "Clone a ComfyUI custom-node repo into the user's local install AND "
            "auto-install its Python dependencies. Backend runs "
            "`git clone --depth=1 <git_url> <ComfyUI>/custom_nodes/<name>` and, "
            "if the repo ships a requirements.txt, ALSO runs "
            "`<comfyui-python> -m pip install -r requirements.txt` against the "
            "detected ComfyUI interpreter. Streams git AND pip output into the "
            "chat. The tool_result reports `restart_required: true` (call "
            "`restart_comfy` next), plus `pip_ran`, `pip_ok`, and `pip_error`. "
            "If the target dir already exists, we skip the clone but still re-"
            "run pip — that's the common recovery path when a node was cloned "
            "on an earlier install but its deps never got installed. Pass "
            "`force: true` to nuke an existing folder and re-clone from scratch "
            "— use this when the previous install landed in the WRONG ComfyUI "
            "(e.g. Easy Install vs Desktop) and you want a clean retry. The "
            "only manual fallback is when `pip_ran: false` (couldn't find "
            "ComfyUI's Python) — then tell the user to run pip themselves."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "git_url": {
                    "type": "string",
                    "description": "Public https git URL (github, gitlab, etc.). Trailing .git is optional.",
                },
                "name": {
                    "type": "string",
                    "description": "Directory name under custom_nodes/ (defaults to the repo's basename)",
                },
                "force": {
                    "type": "boolean",
                    "description": "Delete any existing folder at the target path and re-clone from scratch. Use this when a prior install went to the wrong ComfyUI or left the pack in a broken state.",
                },
            },
            "required": ["git_url"],
        },
    },
    {
        "name": "restart_comfy",
        "description": (
            "Restart the user's local ComfyUI so any newly-installed custom "
            "nodes register on next boot. Requires ComfyUI-Manager (installed "
            "by default on most easy-install builds). Call this IMMEDIATELY "
            "after `install_custom_node` returns `restart_required: true` — "
            "the user should not have to hit Ctrl+C themselves. Returns "
            "{restarted: bool, error?: str}. If restarted=false with a 'not "
            "installed' error, fall back to asking the user to restart "
            "manually (Easy Install: close and reopen; CLI: Ctrl+C then re-run)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "check_local_models",
        "description": (
            "Verify which model files are already installed in the user's local "
            "ComfyUI. Call this right after registering a `runner: local` workflow "
            "module so you can tell the user up-front what they're missing (and "
            "for shared model dirs configured via extra_model_paths.yaml, whatever "
            "ComfyUI can see counts as installed — no path config needed here). "
            "Returns {reachable, missing: [{filename, folder}], present: [filename, ...]}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "models": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "filename": {"type": "string", "description": "The .safetensors / .ckpt / etc. filename ComfyUI would see in its dropdown"},
                            "folder": {"type": "string", "description": "ComfyUI folder category — checkpoints, vae, loras, controlnet, text_encoders, diffusion_models, etc. Optional but helps the user route the download."},
                        },
                        "required": ["filename"],
                    },
                    "minItems": 1,
                },
            },
            "required": ["models"],
        },
    },
]


def _format_scene_context(ctx: dict) -> str:
    """Render the scene state as a compact block we prepend to the conversation
    on every turn. Objects[] is the SINGLE source of truth for what exists —
    names mentioned in generator prompts may reference deleted objects."""
    if not ctx:
        return ""
    parts = ["<scene_context>"]
    objs = ctx.get("objects") or []
    if objs:
        parts.append(f"Objects in scene ({len(objs)}):")
        for o in objs:
            name = o.get("name") or "?"
            kind = o.get("kind") or "object"
            ref = " [has refImage]" if o.get("hasRef") else ""
            notes = f" — notes: {o['notes']}" if o.get("notes") else ""
            parts.append(f"  - {name} ({kind}){ref}{notes}")
    else:
        parts.append("Objects in scene (0): NONE — the scene is empty.")
    # The UI presents partner-API generators (Nano Banana, Seedance, Tripo...)
    # and manifest-driven local ComfyUI modules together under one "Workflows"
    # panel. Both are workflows from the user's POV; here we still list them
    # in two blocks so the agent knows which runtime path they take (partner
    # API call vs local ComfyUI job).
    cells = ctx.get("genCells") or []
    if cells:
        parts.append(f"API workflows ({len(cells)}):")
        for c in cells:
            model = c.get("model") or "?"
            prompt = (c.get("prompt") or "").strip()
            prompt_preview = (prompt[:120] + "…") if len(prompt) > 120 else prompt
            parts.append(f"  - {model}: {prompt_preview or '(empty)'}")
    wfs = ctx.get("workflowModules") or []
    if wfs:
        parts.append(f"Local workflows ({len(wfs)}):")
        for w in wfs:
            parts.append(f"  - {w.get('id')}: {w.get('label')} ({w.get('kind')})")
    # active_target is the disambiguator for underspecified commands like "run
    # one" — always prefer this over guessing from history. kind is always
    # "workflow" now; source ("api" vs "local") tells the agent which path.
    at = ctx.get("activeTarget")
    if at:
        source = at.get("source") or ("local" if ctx.get("activeWorkflowModuleId") else "api")
        parts.append(
            f"Active workflow: '{at.get('id')}' (label: {at.get('label')}, source: {source})."
        )
        awi = ctx.get("activeWorkflowInputs")
        if source == "local" and awi:
            names = ", ".join(str(i.get("name")) for i in awi if isinstance(i, dict))
            parts.append(f"  Its inputs: {names}")
    else:
        parts.append("Active workflow: (none) — ask the user which workflow to use if a command is ambiguous.")
    parts.append(
        "NOTE: Objects above is authoritative. [Name] tokens inside generator "
        "prompts are saved text and may reference objects that were deleted — "
        "don't assume those exist unless the name also appears in Objects."
    )
    parts.append("</scene_context>")
    return "\n".join(parts)


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

    return {
        "reply": reply_text,
        "pending_tools": pending_tools,
        "stop_reason": getattr(response, "stop_reason", None),
        "usage": {
            "input_tokens": getattr(response.usage, "input_tokens", 0),
            "output_tokens": getattr(response.usage, "output_tokens", 0),
            "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
            "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
        },
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
async def assets_list():
    items = []
    # Two-pass so we can pair 3D outputs with their `<stem>.thumb.<ext>`
    # companion image before returning. Skip .thumb.* files from the listing
    # itself — they're an implementation detail, not standalone assets.
    thumbs: dict[str, Path] = {}
    all_files: list[Path] = []
    for p in DATA_DIR.glob("out_*.*"):
        if not p.is_file():
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
        if ext in {"png", "jpg", "jpeg", "webp"}:
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
            "url": f"/data/{rel}",
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
                entry["thumbUrl"] = f"/data/{trel}"
            except ValueError:
                pass
        items.append(entry)
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"assets": items[:200]}


@app.delete("/api/assets/{filename}")
async def assets_delete(filename: str):
    # Safety: only allow deleting files matching the `out_*.{ext}` shape we serve.
    safe = Path(filename).name  # strip any path components
    if not safe.startswith("out_"):
        raise HTTPException(400, "filename outside the assets namespace")
    p = DATA_DIR / safe
    if p.exists() and p.is_file():
        try:
            p.unlink()
        except Exception as e:
            raise HTTPException(500, f"delete failed: {e}")
    return {"deleted": True, "filename": safe}


# ---------- data static mount (must be defined after all explicit routes) ----------

app.mount("/data", StaticFiles(directory=str(DATA_DIR)), name="data")

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
    "COMPOSITION-GUIDED IMAGE GENERATION TASK.\n\n"
    "**OUTPUT ASPECT RATIO — HIGHEST PRIORITY RULE.** The output image's "
    "width and height MUST match image 1's width and height ratio EXACTLY. "
    "Format: match image 1. Measure image 1: if width > height, output "
    "landscape. If height > width, output portrait. If width == height, "
    "output square. Do NOT default to 1:1 or 1024×1024. Do NOT crop, pad, "
    "or letterbox to reshape. Aspect ratio compliance is non-negotiable.\n\n"
    "**IMAGE 1 IS A STRUCTURAL LAYOUT REFERENCE — NOT A LOOK REFERENCE.** "
    "Image 1 is a 3D blockout: a low-fidelity scene with primitive shapes, "
    "flat colors, and reference geometry. Treat it exactly like a hand-drawn "
    "sketch or wireframe used to plan a final polished image. Follow the "
    "structure of the attached reference image exactly for:\n"
    "  • Camera angle, perspective, and lens compression.\n"
    "  • Composition, framing, and where the horizon sits.\n"
    "  • Placement, position, and screen-space size of each element.\n"
    "  • Ground plane orientation and vanishing points.\n"
    "Keep the exact layout of every element as shown in image 1, but render "
    "it in the style described in the user prompt. Strictly adhere to the "
    "placement and scale from the blockout while replacing the primitive "
    "shapes with the intended subjects.\n\n"
    "Do NOT copy from image 1: its flat colors, primitive silhouettes, "
    "material simplicity, low-detail surfaces, tinted shapes, checker "
    "patterns, grid overlays, or overall 'blockout' aesthetic. The output "
    "must look like a fully realized real image (or the style described in "
    "the user prompt), NOT like a stylized version of the blockout.\n\n"
    "ANY ADDITIONAL IMAGES (image 2, 3, …) are per-object REFERENCE IMAGES — "
    "they show what each object should look like as a subject. The SCENE "
    "INVENTORY below tells you which image number maps to which named "
    "object in image 1's composition.\n\n"
    "**SEAMLESS COHESION.** The output must read as a single, unified, "
    "seamless image — one photograph or one painting, not a composite. "
    "Where two objects meet (object touching ground, object against sky, "
    "shadow across a surface), the transition must be physically plausible: "
    "consistent lighting direction, matching color temperature, correct "
    "contact shadows, and continuous surrounding materials. Do NOT leave "
    "visible seams, cut-out edges, hard color breaks, or 'pasted-on' looks "
    "at boundaries between elements. Every object shares the same scene "
    "lighting, atmosphere, and depth-of-field as the environment around it. "
    "The whole image should look like it was captured or painted in one "
    "pass.\n\n"
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


def _animoflow_installed() -> bool:
    """True if the AnimoFlow repo has been cloned into tools/animoflow.
    Checks for the .git subfolder rather than the parent — a bare mkdir
    shouldn't register as installed."""
    return (_ANIMOFLOW_DIR / ".git").exists()


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
    return {"filename": out.name, "path": str(out), "ext": "png"}


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
    return {"filename": out.name, "path": str(out), "ext": "png"}


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

    # Client flag — user hit × on the viewport-blockout row (text-to-image mode).
    # Skips scene-image resolution below so no image_path gets injected, and
    # the module runs with image_path=None. Pop early so it doesn't leak into
    # the module kwargs. Nano Banana has no aspect_ratio CLI parameter, so we
    # synthesize a blank canvas at the requested aspect further down and pass
    # THAT as image_path — the base prompt already tells the model to match
    # image 1's dimensions. Without this, Nano defaults to 1:1 square.
    skip_source_image = bool(inputs.pop("skip_source_image", False))

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
        if spec.get("type") == "scene-image":
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
    # viewport snapshot. Resolve /output/... to a local file; anything else gets
    # fetched to a temp file for `comfy upload` to consume.
    image_url = inputs.pop("image_url", None)
    if image_url:
        import tempfile as _tempfile, urllib.request as _urlreq
        try:
            if image_url.startswith("/output/"):
                candidate = DATA_DIR / image_url[len("/output/"):]
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
        checker_objs = []
        mannequin_objs = []
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
            lines.append(
                f"- {name} ({color} {kind}): centered at x={x}% y={y}% "
                f"of frame, occupies ~{w}%×{h}% of frame{ref_note}{notes_note}{checker_note}{mannequin_note}"
            )
            if o.get("checker"):
                checker_objs.append(name)
            if kind == "mannequin":
                mannequin_objs.append(name)
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
        # _prompt_store now holds the user's EDITED base prompt (full replace
        # of BASE_PROMPT), not appended tweaks. Empty / unset → server default.
        base_override = (_prompt_store.get(node_id) or "").strip()
        inventory = _format_inventory(scene_objects, camera_meta)
        parts = [base_override or BASE_PROMPT.strip()]
        # Blockout Strength language — tiered instruction that tells the model
        # how tightly to hew to image 1's composition. 0 = pure creative
        # freedom (image 1 is a hint), 1 = strict spatial replacement (default).
        # Only added when we ARE using the blockout (not text-to-image mode).
        if not skip_source_image:
            if blockout_strength <= 0.25:
                strength_note = (
                    f"BLOCKOUT STRENGTH: {int(blockout_strength * 100)}% (LOOSE). "
                    "Image 1's composition is a loose suggestion only. Use it "
                    "for rough spatial placement of subjects in the frame, but "
                    "feel free to reinterpret sizes, silhouettes, and exact "
                    "positions. Prioritize the user's prompt and creative "
                    "vision over strict adherence to the blockout shapes."
                )
            elif blockout_strength <= 0.6:
                strength_note = (
                    f"BLOCKOUT STRENGTH: {int(blockout_strength * 100)}% (MODERATE). "
                    "Follow image 1's general placement and scale as guidance, "
                    "but interpret the primitive shapes loosely — the final "
                    "objects can have organic proportions that differ somewhat "
                    "from the blockout stencils, as long as their approximate "
                    "position and screen footprint match."
                )
            elif blockout_strength < 1.0:
                strength_note = (
                    f"BLOCKOUT STRENGTH: {int(blockout_strength * 100)}% (STRICT). "
                    "Image 1's placement, scale, and silhouette should closely "
                    "match in the output. Minor artistic reinterpretation of "
                    "exact shape is allowed, but each object's screen-space "
                    "footprint (position + size) must be very close to what "
                    "the blockout shows."
                )
            else:
                strength_note = (
                    "BLOCKOUT STRENGTH: 100% (LOCKED). Each colored shape in "
                    "image 1 has an EXACT screen-space footprint (position, "
                    "size). The replacement object must occupy that same "
                    "footprint precisely. Do not scale up or down; do not "
                    "reposition; do not reinterpret the silhouette."
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
    "You are an in-editor agent for ComfyBlockout, a 3D blockout tool that "
    "feeds scenes to generative image/video models via Comfy Cloud. "
    "\"Blockout\" here means the classic film/game workflow: coarse geometry "
    "+ lighting + camera to lock composition BEFORE any final rendering. In "
    "ComfyBlockout the twist is that the coarse scene doesn't stay coarse — "
    "it becomes the input to AI generators (image models like Nano Banana / "
    "Flux, video models like Seedance / Wan / Veo, 3D models like Tripo) that "
    "restyle the blockout into a finished frame or clip. So the primitives + "
    "lights + camera aren't just references — they're the scaffold the model "
    "hallucinates the final image onto. Users are typically non-technical "
    "filmmakers / artists building shots, not devs. Your job is to be the "
    "brain that (a) helps them shape the blockout via natural language, (b) "
    "writes the prompts + configures the generator cells, and (c) directly "
    "manipulates the scene when they describe intent instead of steps.\n\n"
    "The core loop: user describes a shot → you spawn / arrange objects, "
    "place lights + camera, set aspect ratio + duration → user picks a "
    "generator workflow → they hit Generate → the workflow snapshots the "
    "viewport (image or short video render) + the user's prompt and runs it "
    "through the chosen model → the result lands in the Assets pane + the "
    "viewport overlay for approval. \n\n"
    "You help the user refine prompts, build generation JSONs, reason about "
    "their scene, AND directly manipulate the scene via editor tools "
    "(add_primitive, delete_object, set_object_color, set_object_position, "
    "set_object_rotation, set_object_scale, rename_object, list_objects, "
    "set_generator_prompt, spawn_light, spawn_terrain, spawn_mannequin, "
    "spawn_skybox + generate_ variants). When the user asks to "
    "add/move/recolor/delete something, call the tool — don't just describe "
    "how they could do it manually.\n\n"
    "SCENE OBJECT PALETTE: add_primitive covers plain geometry + FX (cube, "
    "sphere, capsule, cylinder, cone, plane, text, particles, clouds). For "
    "landscape-scale ground, use `spawn_terrain` (procedural fBM noise, "
    "picks between hills/mountains/canyon presets) or the one-call "
    "`generate_terrain({prompt})` which spawns a terrain, generates a "
    "grayscale heightmap on Comfy Cloud with backend-owned guardrails "
    "(grayscale, top-down orthographic, white=high, no text), and applies "
    "the heightmap as displacement in a single call. Prompt should describe "
    "the landscape SHAPE from above (\"mountain range with a river valley\") "
    "not the aesthetic — think heightmap, not photograph. For "
    "scene-scale figures + backdrops, use the dedicated spawn tools: "
    "`spawn_mannequin` for a ~1.72m Xbot-rigged human reference (Mixamo bones, "
    "poseable per-joint), `spawn_skybox` for an empty inverted 360° panorama "
    "sphere (user drops or generates an equirectangular image onto it later), "
    "OR — the one-call version — `generate_skybox({prompt})` which spawns the "
    "sphere (or reuses an existing one) AND generates the panorama with "
    "backend-owned equirectangular guardrails, applied DIRECTLY as the "
    "sphere's texture in a single tool call. ALWAYS prefer generate_skybox "
    "when the user asks for an environment (\"put me in a misty pine forest\", "
    "\"add a warehouse backdrop\") — a plain spawn_skybox leaves them staring "
    "at an empty amber sphere, and running a workflow-cell generator (Nano "
    "Banana / Seedance / etc.) for a skybox produces a flat image that lands "
    "as a rendered plane in the scene, NOT applied to the sphere. If "
    "generate_skybox fails, surface the exact backend error to the user "
    "rather than falling back to a workflow-cell workaround — the workaround "
    "produces the wrong result (a plane) and confuses the intent. These live "
    "outside add_primitive because they build compound objects, not a single "
    "mesh from a geometry factory.\n\n"
    "LIGHTS: use `spawn_light({type, position?, intensity?, color?, "
    "cast_shadows?, softbox_width?, softbox_height?})` to place a light in "
    "the scene. Four types with distinct use cases:\n"
    "- `directional`: parallel-ray sun-style light with uniform intensity + "
    "direction across the whole scene. Best for the KEY shadow-caster in an "
    "outdoor / establishing shot; VSM soft shadows blur cleanly on the flat "
    "shadow map. Default aim points at world origin — move + rotate via the "
    "wrapper Group.\n"
    "- `spot`: cone-shaped light with adjustable angle + penumbra. Best for "
    "focused pools of light (stage spot, flashlight, dramatic key). Also "
    "produces clean VSM soft shadows. Wider angle = wider pool.\n"
    "- `point`: omnidirectional bulb. USE AS FILL / AMBIENT LIGHT — do NOT "
    "enable shadows on point lights, they use a 6-face cube shadow map that "
    "produces hard rectangular seams in three.js regardless of Softness "
    "settings. The user landed on \"point = softbox-like fill\" as the "
    "mental model. Force `cast_shadows: false` (which is the default anyway).\n"
    "- `softbox`: a rectangular area light (RectAreaLight under the hood). "
    "Fills a scene with soft directional light from a broad emitter surface "
    "— matches photography softbox lighting. CAN'T cast shadows (three "
    "limitation) which is exactly the intended use. Size via "
    "`softbox_width` + `softbox_height` in meters (defaults 2×2).\n"
    "Default light spawn: type=point, intensity=50, color=#ffffff, "
    "castShadow=off. Adding any user light AUTOMATICALLY kills the built-in "
    "scene fill (hemi + directional + PMREM environment intensity) so the "
    "user's lighting dominates — this is a feature, not a bug. Removing the "
    "last user light restores the fill. Recommended shot lighting: one "
    "directional (or spot) as the shadow-caster + one softbox (or point) as "
    "fill from the opposite side.\n\n"
    "TERRAIN + PLANE CHECKERBOARD: for landscape ground, prefer "
    "`generate_terrain` over `spawn_terrain` when the user has an aesthetic "
    "in mind (\"desert canyons\", \"mountain valley\") — the heightmap gives "
    "richer relief than fBM presets. For a flat blockout floor + scale "
    "reference, spawn a `plane` primitive via add_primitive; the user can "
    "toggle its Checker button in the Appearance section for a Blender-style "
    "gray checkerboard (classic scale-reference floor). Planes render as "
    "single-sided (top-visible only) by default now — flip Double-Sided if "
    "the user needs to see a plane from below.\n\n"
    "CONTACT SHADOW: an optional Scene-properties toggle that layers a soft "
    "ambient shadow beneath every scene object independent of any Light — "
    "renders a top-down depth capture blurred with a 2-pass gaussian, "
    "textured onto a 40×40m plane just above the grid. Suggest turning it "
    "ON when the user wants extra grounding (objects reading as \"sitting "
    "on\" the ground) or when their scene has no shadow-casting light but "
    "still needs contact darkening. Off by default. Doesn't respond to light "
    "direction (it's a top-down projection, always beneath objects), so pair "
    "it with a real Directional/Spot for the directional shadow cue.\n\n"
    "CAMERA CONTROL: three dedicated tools shape how the render camera moves "
    "and aims. `set_camera_target({name})` locks the render camera's aim to a "
    "specific object every frame — useful for \"focus on [Sphere.001]\" or as "
    "the pivot for orbit shots (also referenced by start_turntable's camera "
    "mode). `clear_camera_target()` releases the aim lock. "
    "`set_camera_handheld({speed, noise})` adds subtle position + rotation "
    "shake to the render camera (both 0..1; noise = amplitude, speed = shake "
    "frequency; 0 = off). Shake is applied only during camera-view playback "
    "and recording, so a paused shot stays still for composition.\n\n"
    "TURNTABLE / ORBIT: `start_turntable({mode, duration, direction, "
    "object_name?})` builds a one-revolution motion. mode=\"subject\" bakes 9 "
    "linear-ease Y-rotation keyframes on object_name (or the current "
    "selection) so the object spins in place across the timeline. "
    "mode=\"camera\" activates a PROCEDURAL orbit ring — the render camera "
    "orbits object_name at its current radius/height, position sampled per "
    "frame from a circle (no keyframes clutter the timeline, radius/height "
    "are live-scalable). duration seconds sets the scene duration so one "
    "loop = one revolution. `stop_turntable()` clears an active orbit AND "
    "wipes camera keyframes / any subject spin currently owning the timeline.\n\n"
    "ANIMOFLOW (text-to-motion): `run_animoflow({prompt, max_frames?, seed?})` "
    "prompts the local AnimoFlow MoMask container to synthesize a motion "
    "clip from a text description, then retargets it onto an AF_Mannequin in "
    "the scene (spawns one if none exist). Requires Docker Desktop running + "
    "the AnimoFlow containers up (setup lives in the Motion tool pane — "
    "point the user there if the call fails with an env error). Good prompts "
    "read like short verb phrases: \"person walking forward\", \"a character "
    "waving\", \"kick with the right leg then step back\". Frame count 30–240 "
    "typical (20fps, so 120 = 6s). Warn the user the first run of the day "
    "can take 30–90s of CPU inference.\n\n"
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
    "4. WORKFLOW FORMAT — both API and graph/save are now supported by the "
    "cloud + local runners. STRONGLY PREFER API FORMAT for new modules (flat "
    "dict keyed by node id string, each entry has `class_type` + `inputs` dict, "
    "no widget positional counting, no shape:7 shift bugs). The Comfy Cloud "
    "web UI's `Save (API Format)` export IS this format. It's also what "
    "Cloud's templates ship as, so MCP `get_template` returns API format "
    "directly — no conversion step. The cloud runner's manifest patcher "
    "(`_find_node` in `server/modules/_workflow_shared.py`) auto-detects the "
    "format and patches by `widget_name` (dict key) for API and `widget_index` "
    "(list position) for graph. FASTEST PATH TO A NEW WORKFLOW MODULE: use "
    "`mcp__plugin_comfy-cloud_comfy-cloud__search_templates` to find a "
    "canonical Comfy Cloud template for the model the user asked for, then "
    "`get_template` to download the API-format JSON, then write a matching "
    "meta.json where each manifest patch has `node_id` + `widget_name` (the "
    "input key from the template — e.g. `\"image\"` for LoadImage), then call "
    "`create_workflow_module` — done, no widget-counting, no format arguments. "
    "Legacy graph/save format (`last_node_id` + `nodes[]` array + `links[]` at "
    "root) still runs — see `server/workflows/tripo_p1_i2m_cloud.json` for a "
    "reference graph-format cloud workflow. Local runner accepts both formats "
    "too, same way.\n"
    "   - USER-FACING IMPORT UX: the AI Agent chat input row has an Import "
    "Workflow button (tray icon, top-right of the textarea) that opens a "
    "file picker for `.json`, reads it, and auto-injects a directive prompt "
    "asking you to inspect + register the workflow. If a user says \"I have "
    "a workflow to import\" or \"can you add this workflow for me\", POINT "
    "THEM AT THAT BUTTON first — it's faster and cleaner than asking them "
    "to paste a big JSON blob into chat. When the button fires, you'll "
    "receive the file's contents in a fenced ```json``` block with the "
    "directive already spelled out (identify format, pick label, identify "
    "inputs, patch by widget_name for API, ask before creating if anything "
    "is ambiguous). Confirm ambiguous decisions with the user in one round "
    "before calling `create_workflow_module`.\n"
    "   - SCENE-IMAGE INPUT UX: when a workflow has a `scene-image` input, "
    "the user cell shows an empty slot with the hint \"Empty = uses current "
    "viewport\". If the slot is empty, the runner captures the current 3D "
    "viewport as the input image. If the user uploads (or drags) an image "
    "into that slot, that reference image is used instead. When a user "
    "asks something like \"why is my workflow using the scene instead of "
    "the image I picked?\" — check that the cell.values for the image key "
    "is set (they may have clicked Generate before the upload finished, or "
    "the upload failed silently). When a user asks \"how do I feed a "
    "reference image?\" — tell them to click / drop onto the workflow "
    "cell's image slot.\n"
    "   - WHEN YOU BUILD A WORKFLOW (not just import), WIRE IT CORRECTLY "
    "for scene-image to work: (a) INCLUDE a LoadImage node in the graph — "
    "even if the source model has its own image input, always route through "
    "LoadImage so the runner's viewport-snapshot / upload-reference flow "
    "resolves cleanly. (b) The manifest's scene-image patch MUST target the "
    "LoadImage node's `image` widget (API format: `widget_name: \"image\"`, "
    "node_id = LoadImage's id). NEVER target the compute node's image "
    "socket directly — that expects a decoded IMAGE tensor, and the runner "
    "only knows how to upload a filename to LoadImage's widget. (c) In API "
    "format the compute node's `image` input should be a link reference "
    "like `[\"<loadimage_id>\", 0]` — output index 0 of LoadImage is IMAGE, "
    "index 1 is MASK. (d) In graph format the same wiring goes through the "
    "top-level `links[]` array as `[<link_id>, <loadimage_id>, 0, "
    "<compute_id>, <input_slot>, \"IMAGE\"]`. If any part of this wiring "
    "is off, the compute node receives a filename string instead of a "
    "tensor and errors with `'str' object has no attribute 'shape'` at "
    "runtime.\n"
    "   - Both formats: for image inputs from the viewport (scene-image), keep a "
    "LoadImage node in the graph and target ITS widget_index=0 in the manifest "
    "patch — the runner uploads the snapshot to Comfy Cloud and writes the returned "
    "filename into that widget. LoadImage decodes it into an IMAGE tensor that "
    "flows to the downstream node via a link. DO NOT try to patch scene-image "
    "directly onto a compute node's `image` input — those are socket inputs, not "
    "widgets, and the runner has no upload-then-tensor path for them.\n"
    "   - CLOUD widget-position gotcha: when a graph converts a widget into a "
    "socket (input entry has `shape: 7` in the node's `inputs[]` array), the cloud "
    "validator STILL COUNTS THAT WIDGET SLOT when reading `widgets_values`, but "
    "the graph JSON no longer stores a value for it. Result: every widget AFTER "
    "the converted one gets shifted -1 relative to what cloud expects, so cloud "
    "reads pose_mode where you wrote seed, reads seed where you wrote "
    "seed_control, etc. Fix: INSERT an empty-string entry (`\"\"`) in "
    "`widgets_values` at the position where the converted widget would have "
    "lived. Diagnose from the error — if cloud reports `field: pose_mode, code: "
    "unknown_enum_value` with a numeric value that matches a seed-shaped INT one "
    "slot later in your list, you've hit this shift. Look at the node's "
    "`inputs[]` for any `shape: 7` entries and count how many slots to insert "
    "(one per converted widget). Meshy 6 · I2M in the repo hit this — the "
    "`should_texture.texture_image` socket needed an inserted `\"\"` at index 8. "
    "Rodin 3D similarly has multiple `shape: 7` inputs that would each need an "
    "empty slot if converted.\n"
    "   - CLOUD 3D CATALOG GAP: `comfy generate list` shows only image + video "
    "partners (bfl, kling, vertexai/nano-banana, seedance, etc.). Meshy, Rodin, "
    "Tripo are NOT in the partner-generate catalog — they're custom nodes only. "
    "So for 3D generation you MUST use the graph-workflow submission path (this "
    "system) — you cannot bypass to a `comfy generate <model>` CLI call the way "
    "/api/skybox/generate does for Flux 2. If a user asks \"can we just hit an "
    "endpoint for 3D like skybox does?\", explain this catalog gap: 3D partners "
    "aren't wired to `comfy generate`, so workflow submission is the only route.\n"
    "   - DIAGNOSING RUNTIME ERRORS (validation passes but execution fails): use "
    "the Comfy Cloud MCP `get_node` tool (`mcp__plugin_comfy-cloud_comfy-cloud__"
    "get_node`) to inspect the failing node's REQUIRED input types + widget "
    "specs. Especially useful for API nodes (Meshy, Rodin, Kling, Ideogram, Flux "
    "Pro etc.) — some of them declare `IMAGE` as their input type but internally "
    "accept a filename string (from Cloud's LoadImage upload) or a URL, then "
    "call their own `upload_images_to_comfyapi` helper that requires an actual "
    "tensor with `.shape`. If you see `'str' object has no attribute 'shape'` "
    "in the traceback, this is the class of bug: the API node's execute path "
    "isn't happy with what LoadImage handed it. Compare to a WORKING sibling "
    "(e.g. Tripo I2M uses the same LoadImage → node pattern — check "
    "server/workflows/tripo_p1_i2m_cloud.json). If the graph structure matches "
    "but one fails at runtime, it's a bug in that specific partner's cloud "
    "implementation, not a workflow shape issue. Options: (a) file the bug + "
    "wait for a fix, (b) try an alternate cloud API node for the same task if "
    "one exists (`search_nodes` MCP tool), (c) preprocess the image differently "
    "(some nodes want it fed via a PreviewImage or explicit VAEDecode step).\n"
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
        "description": "Add a SINGLE primitive object to the scene. For multiple at once, use batch_add_primitives instead — it's ~10x faster than calling this in a loop. Text/particles/clouds have their own defaults; for a mannequin figure use spawn_mannequin, for a skybox backdrop use spawn_skybox.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["cube", "sphere", "capsule", "cylinder", "cone", "plane", "text", "particles", "clouds"],
                    "description": "Primitive type to add. text spawns a 3D 'Text' mesh (rename via rename_object to change the string). particles/clouds spawn stylized FX systems.",
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
        "description": "Return the currently selected object's name, kind, transform, and color. Returns 'nothing selected' when the user has nothing highlighted. Use this to answer questions like 'what is this?' or before editing 'the selected thing' — much cheaper than list_objects when the user is pointing at something specific.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_camera_state",
        "description": "Return the render camera's world position, aim target (if any), FOV, and aspect ratio. Use before answering camera framing questions or before proposing camera edits so you know where the shot currently is.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_scene_summary",
        "description": "High-level scene digest: object count grouped by kind, active workflow modules, total keyframe count, and scene duration in seconds. Cheaper than list_objects for a broad 'what's in this scene' answer.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_skybox_image",
        "description": "Apply an equirectangular panorama image to the scene's skybox. Pair with list_recent_outputs to pull the user's last render — e.g. 'apply my last generated image as a skybox' becomes list_recent_outputs(kind='image', limit=1) → set_skybox_image(url=<returned url>). If no skybox exists, spawn one first via spawn_skybox. Non-equirectangular images will stretch on the sphere — best used with generated 360° panos.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Image URL (from list_recent_outputs) or a full http(s) URL"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "list_recent_outputs",
        "description": "Return recently generated outputs (images, videos, 3D meshes) with their filenames, kinds, and paths. Use this to reference the user's recent renders — e.g. 'apply my last image as a skybox' or 'what did I generate today?'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Max results to return (default 20)"},
                "kind": {"type": "string", "enum": ["image", "video", "3d"], "description": "Optional filter — only return this asset kind"},
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
    # ── Compound-object spawns ───────────────────────────────────────
    # Skybox + Mannequin build assemblies (multiple meshes + userData +
    # skinning) so they live outside add_primitive's single-geometry model.
    {
        "name": "spawn_mannequin",
        "description": "Spawn a ~1.72m Xbot-rigged mannequin figure — a Mixamo skinned GLB with pose-able joints. Result becomes the current selection so a follow-up rename_object / set_object_position works on it. Only one spawn per call.",
        "input_schema": {
            "type": "object",
            "properties": {
                "position": {
                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                    "description": "Optional [x,y,z] world position (default [0,0,0]).",
                },
            },
        },
    },
    {
        "name": "spawn_skybox",
        "description": "Spawn a giant inverted sphere as a scene backdrop. The user drops or generates a 360° equirectangular image onto it via the object inspector — this tool just adds the sphere. Idempotent-ish: re-clicking the Skybox tile in the UI reuses an existing skybox, but this tool always adds a new one; check list_objects first if you want to avoid duplicates.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "spawn_terrain",
        "description": "Spawn a procedural terrain — a 20x20m displaced plane with fBM noise. Preset picks the silhouette style: \"hills\" (rolling), \"mountains\" (jagged high amplitude), \"canyon\" (medium with plateaus). seed randomizes the specific terrain within the preset.",
        "input_schema": {
            "type": "object",
            "properties": {
                "preset": {"type": "string", "enum": ["hills", "mountains", "canyon"], "description": "Silhouette style (default hills)"},
                "seed": {"type": "integer", "description": "Random seed for the specific terrain (default 42)"},
                "position": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
            },
        },
    },
    {
        "name": "spawn_light",
        "description": "Spawn a light in the scene. Four types with distinct roles: directional (parallel-ray sun, best shadow-caster for outdoor shots), spot (cone for focused pools), point (omnidirectional bulb — USE AS FILL, do NOT enable shadows), softbox (rectangular area light for soft fill — can't cast shadows by design). Adding any user light auto-kills the built-in scene fill so the user's lighting dominates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["directional", "point", "spot", "softbox"], "description": "Light type (default point). Directional + Spot are the shadow-casters. Point + Softbox are fill/ambient."},
                "position": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3, "description": "Optional [x,y,z] world position — default (2, 4, 2)"},
                "intensity": {"type": "number", "minimum": 0, "maximum": 500, "description": "Brightness (default 50). Softbox and point are area/omnidirectional and often need higher values than directional/spot."},
                "color": {"type": "string", "description": "Hex color like #ffe4b0 for warm, #b0d4ff for cool. Default #ffffff."},
                "cast_shadows": {"type": "boolean", "description": "Enable VSM shadow casting. Recommended TRUE for directional/spot, FALSE for point (produces artifacts) and softbox (unsupported)."},
                "softbox_width": {"type": "number", "minimum": 0.1, "maximum": 20, "description": "Softbox emitter rectangle width in meters (softbox type only, default 2)."},
                "softbox_height": {"type": "number", "minimum": 0.1, "maximum": 20, "description": "Softbox emitter rectangle height in meters (softbox type only, default 2)."},
            },
        },
    },
    {
        "name": "generate_terrain",
        "description": "Generate a grayscale heightmap via Comfy Cloud and apply it DIRECTLY as a terrain object's displacement. Reuses an existing terrain if there is one, spawns a fresh one if not — so a single call fully wires the ground. The heightmap guardrails (grayscale, top-down orthographic, white=high, no text) are appended to the prompt by the server. Prompt should describe the SHAPE of the landscape from above — e.g. \"mountain range with a wide river valley\", \"eroded desert canyons\", \"gentle rolling hills with a lake in the center\". PREFER THIS over a workflow-cell path for terrain shaping — those output flat images that land as a rendered plane, NOT applied to the terrain mesh.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Landscape shape description (top-down)"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "generate_skybox",
        "description": "Generate a 360° equirectangular panorama via Comfy Cloud and apply it DIRECTLY as the scene's skybox texture. Reuses an existing skybox if there is one, spawns a fresh sphere if not — so a single call fully wires the backdrop with no follow-up drag-and-drop. The panorama guardrails (equirectangular, 2:1 aspect, seamless wrap, no text, no figures) are appended to the prompt by the server, so you just describe the environment aesthetic — e.g. \"misty pine forest at dawn\", \"warehouse interior with skylights\", \"neon-lit tokyo street at night\". PREFER THIS over any workflow-cell path (Nano Banana / Seedance etc.) for skybox creation — those output flat images that land as a rendered plane, NOT applied to the sphere. If this tool errors, DO NOT fall back to a workflow-cell as a workaround (that produces a plane, wrong result); surface the backend error to the user so they can fix the CLI setup.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Environment description — describe the SCENE / location / lighting, not the panorama format (the server adds those guardrails)."},
            },
            "required": ["prompt"],
        },
    },
    # ── Camera control ──────────────────────────────────────────────
    # Aim lock + hand-held shake + turntable. Applied to the render camera
    # (the one whose shot the user is composing), NOT the perspective/scene
    # view. Persist to state.renderCamera so save/load round-trips.
    {
        "name": "set_camera_target",
        "description": "Lock the render camera's aim to a specific object every frame. Overrides orbit tumble AND any keyframed rotation — position keyframes still apply, but the camera keeps facing this object. Also used as the pivot for start_turntable's camera mode. Pass a scene object name (case-insensitive match against list_objects).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the scene object to lock aim onto"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "clear_camera_target",
        "description": "Release the camera's aim lock (from set_camera_target). Camera returns to its raw pose from keyframes / user orbit.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_camera_handheld",
        "description": "Add subtle multi-freq shake to the render camera — reads as handheld filming. Speed drives the shake frequency, Noise the amplitude (both 0..1). Set both to 0 to disable. Shake is applied only during camera-view playback / recording so a paused shot stays still.",
        "input_schema": {
            "type": "object",
            "properties": {
                "speed": {"type": "number", "minimum": 0, "maximum": 1, "description": "Shake frequency (0 = still, 1 = fast)"},
                "noise": {"type": "number", "minimum": 0, "maximum": 1, "description": "Shake amplitude (0 = off, 1 = ~5cm position + ~1.5° rotation)"},
            },
            "required": ["speed", "noise"],
        },
    },
    {
        "name": "start_turntable",
        "description": "Bake a turntable / orbit motion. mode=\"subject\": rotates the named object 360° around its Y axis via 9 linear keyframes over duration seconds. mode=\"camera\": activates a procedural orbit ring — the render camera orbits object_name (or the current Target Object) at its current radius/height. Sets scene duration so one timeline loop = one revolution. object_name is required for subject mode; optional for camera mode (falls back to Target Object then current lookAt).",
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["subject", "camera"], "description": "\"subject\" spins the object in place; \"camera\" orbits the render camera around it"},
                "duration": {"type": "number", "minimum": 0.5, "maximum": 60, "description": "Seconds per revolution (typical 3-10)"},
                "direction": {"type": "string", "enum": ["cw", "ccw"], "description": "Rotation direction (default cw)"},
                "object_name": {"type": "string", "description": "Which object to spin/orbit around. Required for mode=subject."},
            },
            "required": ["mode", "duration"],
        },
    },
    {
        "name": "stop_turntable",
        "description": "Clear any active procedural camera orbit AND wipe all camera keyframes. Also wipes object keyframes on the named object if given (use to undo a subject-mode turntable). No-op if nothing's active.",
        "input_schema": {
            "type": "object",
            "properties": {
                "object_name": {"type": "string", "description": "Optional — clears keyframes on this object too (undoes subject-mode spin)."},
            },
        },
    },
    # ── AnimoFlow (text-to-motion) ──────────────────────────────────
    # Prompts the local MoMask container to synthesize a HumanML3D motion
    # clip and retargets it onto an AF_Mannequin in the scene. Requires
    # Docker + AnimoFlow containers up (setup UI lives in Motion util pane).
    {
        "name": "run_animoflow",
        "description": "Generate a text-to-motion animation and apply it to an AF_Mannequin in the scene. Requires Docker Desktop running + the AnimoFlow MoMask container up (setup lives in the Motion util pane). Spawns a mannequin if none exists. First run of the day takes 30-90s of CPU inference. Prompt style: short verb phrases like \"person walking forward\" or \"a character waving\".",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Motion description — short verb phrase"},
                "max_frames": {"type": "integer", "minimum": 30, "maximum": 240, "description": "Frame count (20fps, so 120 = 6s). Default 120."},
                "seed": {"type": "integer", "description": "Optional seed for reproducibility. Default 42."},
            },
            "required": ["prompt"],
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
    # Camera state — target lock, hand-held shake, procedural orbit. Emitted
    # so the agent doesn't ask "is a turntable running?" or clobber an active
    # target with clear_camera_target it didn't know was needed.
    cam = ctx.get("camera") or {}
    cam_lines = []
    if cam.get("targetName"):
        cam_lines.append(f"  aim locked on [{cam['targetName']}]")
    hh = cam.get("handheld") or {}
    if hh.get("noise") or hh.get("speed"):
        cam_lines.append(f"  hand-held shake: speed={hh.get('speed', 0):.2f}, noise={hh.get('noise', 0):.2f}")
    orb = cam.get("orbit") or {}
    if orb.get("active"):
        pivot = orb.get("pivotName") or "(frozen point)"
        cam_lines.append(
            f"  procedural orbit ACTIVE around [{pivot}] "
            f"(radius={orb.get('radius', 0):.2f}, height={orb.get('height', 0):.2f}, "
            f"direction={'ccw' if orb.get('direction') == -1 else 'cw'})"
        )
    if cam_lines:
        parts.append("Render camera state:")
        parts.extend(cam_lines)
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
    total = len(items)
    n = max(1, min(500, int(limit)))
    return {"assets": items[:n], "total": total}


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
        try:
            p.unlink()
        except Exception as e:
            raise HTTPException(500, f"delete failed: {e}")
    return {"deleted": True, "filename": safe}


# ---------- data static mount (must be defined after all explicit routes) ----------

app.mount("/output", StaticFiles(directory=str(DATA_DIR)), name="data")

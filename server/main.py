"""ComfyBlockout-app sidecar — serves the editor, persists scenes/assets/recordings,
and runs cloud generations via `comfy generate <model> ...` modules.

No ComfyUI in the loop. Auth lives in comfy-cli (OAuth via `comfy cloud login`,
or COMFY_API_KEY env var as a fallback)."""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

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

DEFAULT_PROMPT = (
    "Render this blockout scene. Match the exact camera angle, composition, scale, "
    "aspect ratio and object orientations — but do not use the blockout to define "
    "the style, colors, or creative direction. Only use the grid as reference for "
    "the scene perspective and not as an element to include in the generated image or video"
)

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
    return JSONResponse({"success": True, "path": str(final_path), "bytes": len(file_bytes), "mp4": converted})


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
    node_id = node_id.strip()
    if not node_id:
        return JSONResponse({"prompt": DEFAULT_PROMPT})
    if node_id in _prompt_store:
        return JSONResponse({"prompt": _prompt_store[node_id]})
    p = DATA_DIR / f"node_{node_id}.prompt.txt"
    if p.exists():
        text = p.read_text(encoding="utf-8")
        _prompt_store[node_id] = text
        return JSONResponse({"prompt": text})
    return JSONResponse({"prompt": DEFAULT_PROMPT})


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
    so subsequent `comfy generate` calls authenticate as the user."""
    body = await request.json()
    key = str(body.get("key", "")).strip()
    if not key:
        raise HTTPException(400, "key is required")
    if not key.startswith("comfyui-"):
        raise HTTPException(400, "key should start with 'comfyui-'")
    os.environ["COMFY_API_KEY"] = key
    kv = _read_env_kv()
    kv["COMFY_API_KEY"] = key
    _write_env_kv(kv)
    return {"saved": True}


@app.delete("/api/auth/key")
async def auth_key_clear():
    os.environ.pop("COMFY_API_KEY", None)
    kv = _read_env_kv()
    kv.pop("COMFY_API_KEY", None)
    _write_env_kv(kv)
    return {"cleared": True}


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
    }


@app.get("/api/modules")
async def list_modules():
    return {"modules": [_module_dict(m) for m in MODULES.values()]}


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
            info = _video_store.get(node_id)
            if not info or not Path(info["path"]).exists():
                raise HTTPException(400, "no scene video recorded — record in the editor first")
            inputs["video_path"] = Path(info["path"])

    # Auto-inject user-uploaded reference URLs (signed URLs from comfy generate upload)
    # so modules that opt-in (like nano-banana) get them without the client re-sending each id.
    refs = _refs_store.get(node_id) or []
    if refs and "references" not in inputs:
        inputs["references"] = [r["signed_url"] for r in refs if r.get("signed_url")]

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
    return result


# ---------- LLM assistant (Claude + Comfy Cloud MCP) ----------
#
# Per-node conversation memory. ComfyBlockout is single-user-per-process so a
# plain dict is fine; if this ever runs multi-tenant move to a TTL store.
_chat_history: dict[str, list[dict]] = {}

ASSISTANT_SYSTEM = (
    "You are an in-editor agent for ComfyBlockout, a 3D blockout tool that "
    "feeds scenes to generative image/video models via Comfy Cloud. You help "
    "the user refine prompts, build generation JSONs, reason about their "
    "scene, AND directly manipulate the scene via editor tools (add_primitive, "
    "delete_object, set_object_color, set_object_position, set_object_rotation, "
    "set_object_scale, rename_object, list_objects, set_generator_prompt). "
    "When the user asks to add/move/recolor/delete something, call the tool — "
    "don't just describe how they could do it manually. You can also call "
    "Comfy Cloud MCP tools to inspect or run workflows.\n\n"
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
        "description": "Add a primitive object to the scene. Returns the new object's name.",
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
        "description": "Set the prompt text on a generator cell (nano-banana or seedance).",
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "enum": ["nano-banana", "seedance"]},
                "prompt": {"type": "string"},
            },
            "required": ["model", "prompt"],
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
    cells = ctx.get("genCells") or []
    if cells:
        parts.append(f"Generator cells ({len(cells)}):")
        for c in cells:
            model = c.get("model") or "?"
            prompt = (c.get("prompt") or "").strip()
            prompt_preview = (prompt[:120] + "…") if len(prompt) > 120 else prompt
            parts.append(f"  - {model}: {prompt_preview or '(empty)'}")
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
    kwargs = dict(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": ASSISTANT_SYSTEM,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=history,
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

    history = _chat_history.setdefault(node_id, [])

    if tool_results:
        # Continuation turn — frontend ran the editor tools we asked for and is
        # now sending back the results. Append as a user message containing
        # tool_result content blocks (one per pending tool_use), then re-invoke
        # Claude so it can react to the results.
        content_blocks = [{
            "type": "tool_result",
            "tool_use_id": r["tool_use_id"],
            "content": r.get("content", ""),
            **({"is_error": True} if r.get("is_error") else {}),
        } for r in tool_results]
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
        _chat_history[node_id] = history[-20:]

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


@app.post("/api/llm/reset")
async def llm_reset(request: Request):
    body = await request.json()
    node_id = str(body.get("node_id", "")).strip() or "default"
    _chat_history.pop(node_id, None)
    return {"cleared": True}


# ---------- data static mount (must be defined after all explicit routes) ----------

app.mount("/data", StaticFiles(directory=str(DATA_DIR)), name="data")

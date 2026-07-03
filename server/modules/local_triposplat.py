"""Local ComfyUI TripoSplat — image → gaussian splat (.ply).

Talks directly to a running local ComfyUI at http://127.0.0.1:8188 via its HTTP
API (upload → prompt → poll history → download output). No comfy-cli, no cloud
auth, no credits — the user's own GPU runs the workflow, so we sidestep both
Comfy Cloud's subgraph converter bug on TripoSplat AND account credit gating.

Workflow lives at server/workflows/3d_triposplat.json (API format). The
SplatToFile3D node's format field is patched to 'ply' at runtime so we get a
standard gaussian-splat PLY that sparkjs can load directly on the frontend.

Output-node discovery:
1. Any node whose _meta.title == "BLOCKOUT_OUTPUT" wins (workflow-author
   convention — tag your final output node with that title and the module
   grabs it regardless of node id or class_type).
2. Otherwise scan every node's outputs dict for a splat-shaped filename.
3. If ComfyUI's history endpoint didn't register the output (SplatToFile3D
   sometimes doesn't populate `ui`), fall back to scanning the local ComfyUI
   output directory for a splat file created after the run started.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import httpx

from ._base import ModuleDef, new_output_path


WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / "workflows"
COMFY_URL = "http://127.0.0.1:8188"
WORKFLOW_NAME = "3d_triposplat.json"
LOAD_IMAGE_NODE = "99"
SPLAT_OUTPUT_NODE = "92"
OUTPUT_TITLE_MARKER = "BLOCKOUT_OUTPUT"
POLL_INTERVAL = 2.0
POLL_TIMEOUT = 1500
SPLAT_EXTS = (".ply", ".spz", ".splat")

# Known ComfyUI output directories on this machine. First existing dir wins.
# Override with the COMFY_OUTPUT_DIR env var if the install is elsewhere.
_env_out = os.environ.get("COMFY_OUTPUT_DIR", "").strip()
COMFY_OUTPUT_CANDIDATES: list[Path] = [Path(_env_out)] if _env_out else []
COMFY_OUTPUT_CANDIDATES += [
    Path(r"H:\ComfyUI-Easy-Install\ComfyUI\output"),
    Path(r"H:\Comfy-Desktop\ComfyUI-Installs\ComfyDesktop\ComfyUI\output"),
    Path(r"H:\ComfyUI_windows_portable\ComfyUI\output"),
    Path(r"H:\Krita\ComfyUI\ComfyUI\output"),
]


def _find_local_output_dir() -> Path | None:
    for p in COMFY_OUTPUT_CANDIDATES:
        try:
            if p.exists() and p.is_dir():
                return p
        except OSError:
            continue
    return None


async def _check_alive(client: httpx.AsyncClient) -> None:
    try:
        r = await client.get(f"{COMFY_URL}/system_stats", timeout=3.0)
        r.raise_for_status()
    except Exception as e:
        raise RuntimeError(
            f"local ComfyUI not reachable at {COMFY_URL} — is it running? ({e})"
        ) from e


async def _upload_image(client: httpx.AsyncClient, image_path: Path) -> str:
    with image_path.open("rb") as f:
        files = {"image": (image_path.name, f, "application/octet-stream")}
        data = {"overwrite": "true"}
        r = await client.post(f"{COMFY_URL}/upload/image", files=files, data=data, timeout=60.0)
    r.raise_for_status()
    j = r.json()
    name = j.get("name") or j.get("filename")
    if not name:
        raise RuntimeError(f"upload response missing name: {j}")
    return name


async def _submit(client: httpx.AsyncClient, workflow: dict) -> str:
    r = await client.post(f"{COMFY_URL}/prompt", json={"prompt": workflow}, timeout=60.0)
    if r.status_code != 200:
        raise RuntimeError(f"/prompt rejected (rc={r.status_code}): {r.text[:1000]}")
    j = r.json()
    pid = j.get("prompt_id")
    if not pid:
        raise RuntimeError(f"/prompt response missing prompt_id: {j}")
    return pid


async def _wait_and_get_outputs(client: httpx.AsyncClient, prompt_id: str) -> dict:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + POLL_TIMEOUT
    while loop.time() < deadline:
        r = await client.get(f"{COMFY_URL}/history/{prompt_id}", timeout=15.0)
        if r.status_code == 200:
            j = r.json()
            entry = j.get(prompt_id)
            if entry:
                status = entry.get("status") or {}
                if status.get("completed"):
                    if status.get("status_str") == "error":
                        raise RuntimeError(
                            f"workflow errored: {json.dumps(status)[:800]}"
                        )
                    return entry.get("outputs") or {}
        await asyncio.sleep(POLL_INTERVAL)
    raise RuntimeError(f"timed out after {POLL_TIMEOUT}s waiting for prompt {prompt_id}")


async def _download(client: httpx.AsyncClient, filename: str, subfolder: str, out_type: str, out_path: Path) -> None:
    params = {"filename": filename, "subfolder": subfolder, "type": out_type}
    async with client.stream("GET", f"{COMFY_URL}/view", params=params, timeout=120.0) as r:
        r.raise_for_status()
        with out_path.open("wb") as f:
            async for chunk in r.aiter_bytes(chunk_size=64 * 1024):
                f.write(chunk)


def _find_output_node_ids_by_title(workflow: dict) -> list[str]:
    """Return node ids whose _meta.title matches the BLOCKOUT_OUTPUT marker."""
    hits = []
    for nid, node in workflow.items():
        title = ((node or {}).get("_meta") or {}).get("title")
        if isinstance(title, str) and title.strip() == OUTPUT_TITLE_MARKER:
            hits.append(nid)
    return hits


def _pick_splat_from_node_outs(node_outs: dict) -> dict | None:
    for key, val in node_outs.items():
        if not isinstance(val, list):
            continue
        for item in val:
            if not isinstance(item, dict):
                continue
            fn = item.get("filename")
            if isinstance(fn, str) and fn.lower().endswith(SPLAT_EXTS):
                return item
    return None


def _save_thumb(source_image: Path, output_ply: Path) -> None:
    """Save a copy of the source image next to the .ply so the Assets pane can
    render it as the tile thumbnail. Named `<stem>.thumb.<ext>` so the
    /api/assets/list endpoint pairs them by prefix. Best-effort — a failed
    copy shouldn't crash the module."""
    try:
        if not source_image or not source_image.exists():
            return
        src_ext = source_image.suffix.lstrip(".").lower() or "png"
        thumb = output_ply.with_suffix(f".thumb.{src_ext}")
        shutil.copy(str(source_image), str(thumb))
    except Exception as e:
        print(f"[cb-app] triposplat: thumb save skipped: {e}")


def _scan_local_for_new_splat(start_ts: float) -> Path | None:
    """After a run, look at the local ComfyUI output dir for any splat file
    modified after the run started. Newest wins."""
    outdir = _find_local_output_dir()
    if not outdir:
        return None
    matches: list[Path] = []
    for ext in SPLAT_EXTS:
        for f in outdir.rglob(f"*{ext}"):
            try:
                if f.stat().st_mtime >= start_ts:
                    matches.append(f)
            except OSError:
                continue
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


async def run(*, image_path: Path, data_dir: Path, **_):
    if not image_path:
        raise ValueError("image is required")
    image_path = Path(image_path)
    if not image_path.exists():
        raise ValueError(f"image not found: {image_path}")

    wf_path = WORKFLOWS_DIR / WORKFLOW_NAME
    if not wf_path.exists():
        raise RuntimeError(f"workflow file missing: {wf_path}")
    wf = json.loads(wf_path.read_text(encoding="utf-8"))

    # Force PLY on SplatToFile3D — the frontend's sniffer routes .ply to sparkjs.
    if SPLAT_OUTPUT_NODE in wf and "inputs" in wf[SPLAT_OUTPUT_NODE]:
        wf[SPLAT_OUTPUT_NODE]["inputs"]["format"] = "ply"

    # Title-based output routing — any node the user tagged with
    # `_meta.title = "BLOCKOUT_OUTPUT"` is the preferred output source.
    title_output_nodes = _find_output_node_ids_by_title(wf)

    async with httpx.AsyncClient() as client:
        await _check_alive(client)
        uploaded_name = await _upload_image(client, image_path)
        wf[LOAD_IMAGE_NODE]["inputs"]["image"] = uploaded_name
        # 5s clock-drift slack so an early-firing SplatToFile3D isn't missed.
        run_started_at = time.time() - 5
        prompt_id = await _submit(client, wf)
        outputs = await _wait_and_get_outputs(client, prompt_id)

        # --- Attempt 1: preferred output node(s) by BLOCKOUT_OUTPUT title.
        item = None
        for nid in title_output_nodes:
            node_outs = outputs.get(nid) or {}
            item = _pick_splat_from_node_outs(node_outs)
            if item:
                print(f"[cb-app] triposplat: splat via BLOCKOUT_OUTPUT node {nid}: {item}")
                break

        # --- Attempt 2: scan every node's outputs for a splat filename.
        if not item:
            for nid, node_outs in outputs.items():
                if not isinstance(node_outs, dict):
                    continue
                item = _pick_splat_from_node_outs(node_outs)
                if item:
                    print(f"[cb-app] triposplat: splat via generic scan on node {nid}: {item}")
                    break

        if item:
            src_filename = item["filename"]
            subfolder = item.get("subfolder", "") or ""
            out_type = item.get("type", "output") or "output"
            ext = Path(src_filename).suffix.lstrip(".").lower() or "ply"
            dst = new_output_path(data_dir, "triposplat-local", ext)
            await _download(client, src_filename, subfolder, out_type, dst)
            _save_thumb(image_path, dst)
            return {"path": str(dst), "filename": dst.name, "ext": ext}

        # --- Attempt 3: filesystem fallback. SplatToFile3D on the current
        # community node builds doesn't return a UI dict, so its file is written
        # to disk but never shows up in /history. Walk the local ComfyUI output
        # dir for anything splat-shaped modified since the run started.
        local_hit = _scan_local_for_new_splat(run_started_at)
        if local_hit:
            ext = local_hit.suffix.lstrip(".").lower() or "ply"
            dst = new_output_path(data_dir, "triposplat-local", ext)
            shutil.copy(str(local_hit), str(dst))
            _save_thumb(image_path, dst)
            print(f"[cb-app] triposplat: splat via filesystem fallback: {local_hit} -> {dst}")
            return {"path": str(dst), "filename": dst.name, "ext": ext}

        # Give up — dump everything so we can debug.
        outdir = _find_local_output_dir()
        raise RuntimeError(
            f"no splat file found. outputs keys: {list(outputs.keys())}; "
            f"local scan dir: {outdir}; "
            f"full outputs: {json.dumps(outputs)[:1000]}"
        )


MODULE = ModuleDef(
    id="triposplat-local",
    label="Local TripoSplat — Image → Splat",
    kind="3d",
    inputs=[
        {"name": "image", "type": "scene-image", "required": True,
         "label": "Source image",
         "help": "Runs the TripoSplat workflow on your local ComfyUI at 127.0.0.1:8188; result imports as a gaussian splat."},
    ],
    output_ext="ply",
    run=run,
)

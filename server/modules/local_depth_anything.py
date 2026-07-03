"""Local ComfyUI Depth Anything 3 — image → depth-map image.

Runs the utility_depth_anything3_image_depth_estimation workflow on the user's
local ComfyUI at 127.0.0.1:8188. Takes an input image (typically a viewport
snapshot or a picked asset), returns a depth-map PNG that lands in the Assets
pane and can be dragged into other cells' reference slots.

Same shape as local_triposplat.py, just image-in / image-out. The workflow's
PreviewImage node registers with ComfyUI's history dict, so we don't need the
filesystem-scan fallback that SplatToFile3D required.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from ._base import ModuleDef, new_output_path


WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / "workflows"
COMFY_URL = "http://127.0.0.1:8188"
WORKFLOW_NAME = "utility_depth_anything3_image_depth_estimation.json"
LOAD_IMAGE_NODE = "85"
OUTPUT_TITLE_MARKER = "BLOCKOUT_OUTPUT"
POLL_INTERVAL = 2.0
POLL_TIMEOUT = 1200
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


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


def _pick_image_from_node_outs(node_outs: dict) -> dict | None:
    for val in node_outs.values():
        if not isinstance(val, list):
            continue
        for item in val:
            if not isinstance(item, dict):
                continue
            fn = item.get("filename")
            if isinstance(fn, str) and fn.lower().endswith(IMAGE_EXTS):
                return item
    return None


def _find_output_node_ids_by_title(workflow: dict) -> list[str]:
    hits = []
    for nid, node in workflow.items():
        title = ((node or {}).get("_meta") or {}).get("title")
        if isinstance(title, str) and title.strip() == OUTPUT_TITLE_MARKER:
            hits.append(nid)
    return hits


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

    title_output_nodes = _find_output_node_ids_by_title(wf)

    async with httpx.AsyncClient() as client:
        await _check_alive(client)
        uploaded_name = await _upload_image(client, image_path)
        wf[LOAD_IMAGE_NODE]["inputs"]["image"] = uploaded_name
        prompt_id = await _submit(client, wf)
        outputs = await _wait_and_get_outputs(client, prompt_id)

        # Prefer any node tagged BLOCKOUT_OUTPUT; otherwise scan.
        item = None
        for nid in title_output_nodes:
            item = _pick_image_from_node_outs(outputs.get(nid) or {})
            if item:
                print(f"[cb-app] depth-anything: image via BLOCKOUT_OUTPUT node {nid}: {item}")
                break
        if not item:
            for nid, node_outs in outputs.items():
                if not isinstance(node_outs, dict):
                    continue
                item = _pick_image_from_node_outs(node_outs)
                if item:
                    print(f"[cb-app] depth-anything: image via generic scan on node {nid}: {item}")
                    break

        if not item:
            raise RuntimeError(
                f"no image output found. outputs keys: {list(outputs.keys())}. "
                f"full outputs: {json.dumps(outputs)[:1000]}"
            )

        src_filename = item["filename"]
        subfolder = item.get("subfolder", "") or ""
        # PreviewImage writes to `temp` type; SaveImage writes to `output`. Both
        # are servable from /view — pass through whatever the node reported.
        out_type = item.get("type", "output") or "output"
        ext = Path(src_filename).suffix.lstrip(".").lower() or "png"
        dst = new_output_path(data_dir, "depth-anything-local", ext)
        await _download(client, src_filename, subfolder, out_type, dst)
        return {"path": str(dst), "filename": dst.name, "ext": ext}


MODULE = ModuleDef(
    id="depth-anything-local",
    label="Local Depth Anything 3 — Image → Depth Map",
    kind="image",
    inputs=[
        {"name": "image", "type": "scene-image", "required": True,
         "label": "Source image",
         "help": "Runs Depth Anything 3 on your local ComfyUI; returns a depth-map PNG that lands in the Assets pane."},
    ],
    output_ext="png",
    run=run,
)

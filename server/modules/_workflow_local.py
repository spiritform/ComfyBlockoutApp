"""Local ComfyUI runner for manifest-driven workflow modules.

Mirrors `local_triposplat.py`'s HTTP pattern (POST /prompt → poll /history →
GET /view) but generalized to run any API-format workflow according to a
manifest's input mapping. Called by `_workflow_shared._make_run` when the
manifest sets `runner: "local"`.

WORKFLOW FORMAT ASSUMPTION: this runner expects the workflow JSON to be in
ComfyUI's API/prompt format — a flat dict keyed by node id (strings), each
value having `class_type`, `inputs`, and optional `_meta`. That's what
"Save (API Format)" produces from the ComfyUI GUI. Cloud-imported workflows
from templates come in the graph/save format instead (top-level `nodes` array
+ `links`); we detect that and surface a clear error rather than attempting
the (non-trivial) conversion.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

from ._base import new_output_path


COMFY_URL = "http://127.0.0.1:8188"
POLL_INTERVAL = 2.0
POLL_TIMEOUT = 1500

# Default per-kind output ext lists — used to filter the outputs that come back
# from /history when the manifest doesn't lock this down. Kept aligned with
# `_workflow_shared._KIND_EXTS` so cloud and local runners agree on what
# "an image output" means.
_KIND_EXTS: dict[str, list[str]] = {
    "image": ["png", "jpg", "jpeg", "webp"],
    "video": ["mp4", "webm", "mov"],
    "3d":    ["glb", "gltf", "ply", "obj"],
    "audio": ["wav", "mp3", "flac", "ogg"],
}


def is_api_format(workflow: dict) -> bool:
    """API/prompt format is a dict keyed by node ID strings. Graph/save format
    has a top-level `nodes` array and `links` array. This lets the caller
    decide up-front whether to run it or bail with a helpful error."""
    if not isinstance(workflow, dict):
        return False
    if "nodes" in workflow and isinstance(workflow["nodes"], list):
        return False
    # Every top-level value should be a dict with class_type — that's the
    # API-format contract.
    for v in workflow.values():
        if not isinstance(v, dict) or "class_type" not in v:
            return False
    return len(workflow) > 0


async def check_alive(client: httpx.AsyncClient) -> tuple[bool, str | None]:
    """Ping /system_stats to make sure the local ComfyUI is up. Timeout is
    generous because /system_stats can block for 5–30s if ComfyUI is mid-
    checkpoint-load — first request after a fresh restart often lands during
    the initial safetensors mmap and 3s isn't enough. The exception message
    gets returned so the caller can surface the actual cause (DNS failure,
    connection refused, timeout, etc.) instead of a generic 'not reachable'."""
    try:
        r = await client.get(f"{COMFY_URL}/system_stats", timeout=30.0)
        r.raise_for_status()
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def fetch_object_info(client: httpx.AsyncClient, class_types: list[str] | None = None) -> dict:
    """Grab /object_info for the class_types actually used by the workflow.
    Passing a filter avoids downloading the full node catalog (which can be
    several MB on a heavily-installed ComfyUI). If the filter fails, fall
    back to the full endpoint."""
    if class_types:
        merged: dict = {}
        try:
            for ct in class_types:
                r = await client.get(f"{COMFY_URL}/object_info/{ct}", timeout=15.0)
                if r.status_code == 200:
                    merged.update(r.json() or {})
            if merged:
                return merged
        except Exception:
            pass
    r = await client.get(f"{COMFY_URL}/object_info", timeout=30.0)
    r.raise_for_status()
    return r.json()


def preflight_models(workflow: dict, object_info: dict) -> list[dict]:
    """Walk every node and check its combo-widget values against the local
    ComfyUI's known choices. Returns a list of {node_id, class_type, input,
    value, choices_sample} for anything missing so the caller can surface it.

    Combo widgets are how ComfyUI models the "pick a model file" dropdown —
    they show up in /object_info as `input.required[<name>] = [ [choice1, ...], {tooltip: ...} ]`.
    Anything else (STRING, INT, IMAGE, etc.) doesn't get preflighted here."""
    missing: list[dict] = []
    for nid, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        if not class_type:
            continue
        node_info = object_info.get(class_type)
        if not node_info:
            # Unknown class_type — the /prompt endpoint will error clearly at
            # submit time; don't flag here or we'd flood on every custom node.
            continue
        input_spec = node_info.get("input", {}) or {}
        inputs_val = node.get("inputs") or {}
        for section in ("required", "optional"):
            for name, spec in (input_spec.get(section) or {}).items():
                # Combo shape: [ [choices...], {opts} ] or just [ [choices...] ]
                if not isinstance(spec, list) or not spec:
                    continue
                choices = spec[0]
                if not isinstance(choices, list) or not choices:
                    continue
                value = inputs_val.get(name)
                # Skip inputs wired from another node (they're [node_id, slot]).
                if not isinstance(value, (str, int, float, bool)):
                    continue
                if value in choices:
                    continue
                missing.append({
                    "node_id": nid,
                    "class_type": class_type,
                    "input": name,
                    "value": value,
                    "choices_sample": choices[:8],
                    "choices_count": len(choices),
                })
    return missing


def apply_manifest_inputs(workflow: dict, manifest: dict, kwargs: dict) -> None:
    """Patch the workflow's node inputs using the manifest's patch mappings.
    Same semantics as the cloud runner: manifest specs identify a node_id +
    widget_index; the local API format keys widgets by NAME instead of index,
    so we need to resolve widget names.

    For MVP we take a shortcut: local runners use widget NAMES (e.g. "text",
    "seed", "image") rather than integer indexes. If the manifest was written
    for the cloud runner (which uses `widget_index`), we assume position 0 =
    the primary widget and grab its name from the node's `inputs` dict — the
    common case for CLIPTextEncode/PrimitiveString/LoadImage. Fancier
    mappings should upgrade the manifest to declare `widget_name` explicitly.
    """
    for spec in manifest.get("inputs", []):
        patch = spec.get("patch") or {}
        node_id = str(patch.get("node_id"))
        widget_name = patch.get("widget_name")  # local-format hint, optional
        input_type = spec.get("type", "text")

        node = workflow.get(node_id)
        if not isinstance(node, dict):
            raise RuntimeError(f"manifest patch: node {node_id} not present in workflow")
        inputs = node.setdefault("inputs", {})

        # Resolve which input key to write to. Prefer explicit widget_name; else
        # fall back to educated guesses per input type based on class_type.
        if not widget_name:
            class_type = node.get("class_type", "")
            widget_name = _guess_widget_name(class_type, input_type)
        if not widget_name:
            raise RuntimeError(
                f"couldn't resolve widget name on node {node_id} ({node.get('class_type')}). "
                f"Add `widget_name` to the manifest patch."
            )

        if input_type == "scene-image":
            image_path = kwargs.get("image_path")
            if not image_path:
                if spec.get("required", True):
                    raise ValueError(f"{spec['name']} is required")
                continue
            # image_path is used as-is here; the caller uploaded it to local
            # ComfyUI first and passed the resulting filename via kwargs.
            inputs[widget_name] = str(image_path)
        else:
            value = kwargs.get(spec["name"])
            if value is None or (isinstance(value, str) and not value.strip()):
                if spec.get("required"):
                    raise ValueError(f"{spec['name']} is required")
                continue
            inputs[widget_name] = value


def _guess_widget_name(class_type: str, input_type: str) -> str | None:
    """Best-effort mapping from (class_type, manifest input type) → the ComfyUI
    input key. Keeps common cases working without forcing the manifest author
    (or the AI agent) to know ComfyUI's exact widget names."""
    ct = class_type or ""
    if input_type == "scene-image":
        # LoadImage's widget is `image`.
        return "image"
    if input_type in ("textarea", "text"):
        if ct == "CLIPTextEncode":
            return "text"
        if "String" in ct or "Prompt" in ct:
            return "value" if ct == "PrimitiveString" else "text"
        return "text"
    if input_type == "number":
        if ct == "KSampler":
            return "seed"
        return "value"
    return None


async def upload_image_to_local(client: httpx.AsyncClient, image_path: Path) -> str:
    """Push a local image into ComfyUI's input/ dir. Returns the server-side
    filename LoadImage should reference."""
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


async def submit_and_wait(client: httpx.AsyncClient, workflow: dict) -> tuple[str, dict]:
    r = await client.post(f"{COMFY_URL}/prompt", json={"prompt": workflow}, timeout=60.0)
    if r.status_code != 200:
        raise RuntimeError(f"/prompt rejected (rc={r.status_code}): {r.text[:1000]}")
    pid = (r.json() or {}).get("prompt_id")
    if not pid:
        raise RuntimeError(f"/prompt response missing prompt_id: {r.text[:400]}")

    loop = asyncio.get_event_loop()
    deadline = loop.time() + POLL_TIMEOUT
    while loop.time() < deadline:
        h = await client.get(f"{COMFY_URL}/history/{pid}", timeout=15.0)
        if h.status_code == 200:
            j = h.json()
            entry = j.get(pid)
            if entry:
                status = entry.get("status") or {}
                if status.get("completed"):
                    if status.get("status_str") == "error":
                        raise RuntimeError(f"workflow errored: {json.dumps(status)[:800]}")
                    return pid, (entry.get("outputs") or {})
        await asyncio.sleep(POLL_INTERVAL)
    raise RuntimeError(f"timed out after {POLL_TIMEOUT}s waiting for prompt {pid}")


async def download_output(client: httpx.AsyncClient, item: dict, dst: Path) -> None:
    params = {
        "filename": item["filename"],
        "subfolder": item.get("subfolder", "") or "",
        "type": item.get("type", "output") or "output",
    }
    async with client.stream("GET", f"{COMFY_URL}/view", params=params, timeout=120.0) as r:
        r.raise_for_status()
        with dst.open("wb") as f:
            async for chunk in r.aiter_bytes(chunk_size=64 * 1024):
                f.write(chunk)


def pick_output(outputs: dict, allowed_exts: list[str]) -> dict | None:
    """Walk every completed node's outputs and return the first file matching
    the allowed extensions. Preference given to nodes titled BLOCKOUT_OUTPUT
    (mirrors the convention used by local_triposplat.py)."""
    prioritized: list[str] = []
    others: list[str] = []
    for nid, node_outs in (outputs or {}).items():
        if not isinstance(node_outs, dict):
            continue
        # Rather than parse titles per output, we just visit prioritized nodes
        # first if present. In the current MVP that requires the manifest to
        # denote them; keeping the code path here so it's easy to extend.
        prioritized.append(nid) if False else others.append(nid)

    for nid in prioritized + others:
        node_outs = outputs[nid]
        for key, val in node_outs.items():
            if not isinstance(val, list):
                continue
            for item in val:
                if not isinstance(item, dict):
                    continue
                fn = item.get("filename")
                if not isinstance(fn, str):
                    continue
                ext = Path(fn).suffix.lstrip(".").lower()
                if ext in allowed_exts:
                    return item
    return None


async def run_local_workflow(workflow_path: Path, manifest: dict, kwargs: dict, data_dir: Path) -> dict:
    """Main entrypoint the dispatch layer calls. Loads the workflow, checks
    format, patches inputs, submits, downloads. Raises RuntimeError with a
    helpful message on any failure."""
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    if not is_api_format(workflow):
        raise RuntimeError(
            "Workflow is in graph/save format — the local runner needs API/prompt "
            "format. Load the JSON in ComfyUI, right-click the canvas → "
            "Save (API Format), and re-import."
        )

    module_id = manifest["id"]
    kind = manifest.get("kind", "image")
    allowed_exts = _KIND_EXTS.get(kind, ["png"])
    # Manifest can lock output_ext to a specific value/list.
    raw = manifest.get("output_ext")
    if isinstance(raw, list):
        allowed_exts = [str(e).lower().lstrip(".") for e in raw if e]
    elif isinstance(raw, str) and raw:
        allowed_exts = [raw.lower().lstrip(".")]

    async with httpx.AsyncClient() as client:
        alive, err = await check_alive(client)
        if not alive:
            raise RuntimeError(f"Local ComfyUI not reachable at {COMFY_URL} — is it running? ({err})")

        # scene-image inputs get uploaded first, and the manifest patch gets
        # rewritten with the server-side filename. We mutate kwargs so
        # apply_manifest_inputs writes the uploaded name.
        for spec in manifest.get("inputs", []):
            if spec.get("type") == "scene-image":
                image_path = kwargs.get("image_path")
                if image_path:
                    uploaded_name = await upload_image_to_local(client, Path(image_path))
                    kwargs["image_path"] = uploaded_name

        apply_manifest_inputs(workflow, manifest, kwargs)

        run_started_at = time.time() - 5
        _pid, outputs = await submit_and_wait(client, workflow)
        item = pick_output(outputs, allowed_exts)
        if not item:
            raise RuntimeError(
                f"no output matching {allowed_exts} in /history. Outputs keys: {list(outputs.keys())}"
            )
        ext = Path(item["filename"]).suffix.lstrip(".").lower() or allowed_exts[0]
        dst = new_output_path(data_dir, module_id, ext)
        await download_output(client, item, dst)
        return {"path": str(dst), "filename": dst.name, "ext": ext}

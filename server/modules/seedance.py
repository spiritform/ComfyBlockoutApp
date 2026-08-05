"""Seedance 2.0 (Reference-to-Video) — three variants:

- **standard / lite** — Comfy Cloud partner-node workflow (`ByteDance2ReferenceNode`).
  The recording's video motion is NOT available (Cloud's LoadVideo enum only
  shows videos catalogued by their proprietary asset service, which the OSS
  CLI/HTTP surface can't reach — see docs/issue-cloud-video-upload.md).
  Fallback: extract frame 0 → use as image_1 reference.

- **local** — submit the workflow to the user's local Comfy Desktop instance
  (127.0.0.1:8188). Local `LoadVideo` scans the local input dir with no
  cloud-catalog restriction, so the recording flows into `video_1` and
  Seedance sees actual motion. The partner-API call still hits ByteDance's
  servers via Comfy's proxy — local only affects graph orchestration, not
  the model itself. Requires local Comfy Desktop to be up + signed in with
  a Comfy API key (same auth as `comfy generate seedance`).

Two supported variants (Standard + Lite/Mini) mirror the Cloud template
options. The API-format workflow is built inline for the local runner —
it's only four nodes so manual assembly is cleaner than converting the
graph-format template.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

import httpx

from ._base import ModuleDef, new_output_path
from ._workflow_shared import _submit_wait_download
from ._tripo_shared import upload_image_to_cloud
from ._workflow_local import (
    COMFY_URL, check_alive, upload_image_to_local, upload_video_to_local,
    submit_and_wait, download_output,
)


WF_DIR = Path(__file__).parent.parent / "workflows"
CLOUD_VARIANTS = {
    "standard": WF_DIR / "api_seedance2_0_r2v.json",
    "lite":     WF_DIR / "api_seedance2_0_mini_r2v.json",
}
# Map our variant id → Cloud's ByteDance2ReferenceNode `model` widget value.
_LOCAL_MODEL_NAMES = {
    "local":      "Seedance 2.0",       # standard model, local orchestration
    "local-lite": "Seedance 2.0 Mini",  # cheaper/faster, local orchestration
}

# ByteDance2ReferenceNode.widgets_values positional layout (graph format).
_W = {
    "model": 0, "prompt": 1, "resolution": 2, "ratio": 3, "duration": 4,
    "generate_audio": 5, "auto_downscale": 6, "auto_upscale": 7,
    "seed": 8, "control_after_generate": 9, "watermark": 10,
}
SEEDANCE_NODE = "ByteDance2ReferenceNode"
LOAD_IMAGE = "LoadImage"


def _set_widget(node: dict, index: int, value) -> None:
    widgets = node.setdefault("widgets_values", [])
    while len(widgets) <= index:
        widgets.append("")
    widgets[index] = value


async def _run_cloud(*, prompt: str, resolution: str, ratio: str, duration: int,
                     variant: str, image_path: Path | None, video_path: Path | None,
                     data_dir: Path) -> dict:
    wf_path = CLOUD_VARIANTS.get(variant)
    if not wf_path or not wf_path.exists():
        raise RuntimeError(f"Seedance cloud template missing for variant '{variant}'")
    workflow = json.loads(wf_path.read_text(encoding="utf-8"))
    seedance_node = next(
        (n for n in workflow["nodes"] if n.get("type") == SEEDANCE_NODE), None
    )
    if not seedance_node:
        raise RuntimeError(f"{SEEDANCE_NODE} not found in {wf_path.name}")

    _set_widget(seedance_node, _W["prompt"], prompt)
    _set_widget(seedance_node, _W["resolution"], resolution)
    _set_widget(seedance_node, _W["ratio"], ratio)
    _set_widget(seedance_node, _W["duration"], int(duration))

    if image_path and Path(image_path).exists():
        load_image = next(
            (n for n in workflow["nodes"] if n.get("type") == LOAD_IMAGE), None
        )
        if load_image:
            cloud_name = await upload_image_to_cloud(Path(image_path))
            _set_widget(load_image, 0, cloud_name)

    # Cloud can't accept custom video refs (see module docstring). Fall back to
    # first-frame extraction so a recording still contributes SOMETHING.
    if video_path and Path(video_path).exists() and not (image_path and Path(image_path).exists()):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            frame_path = Path(tmp.name)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "0", "-i", str(video_path),
                 "-vframes", "1", "-q:v", "2", str(frame_path)],
                capture_output=True, timeout=30,
            )
            if frame_path.exists() and frame_path.stat().st_size > 0:
                load_image = next(
                    (n for n in workflow["nodes"] if n.get("type") == LOAD_IMAGE), None
                )
                if load_image:
                    cloud_name = await upload_image_to_cloud(frame_path)
                    _set_widget(load_image, 0, cloud_name)
                    image_path = frame_path
        except Exception:
            pass

    if not image_path:
        raise ValueError(
            "Seedance 2.0 R2V (Cloud) needs a reference image or a recording "
            "(first frame will be used). Record via the transport ● button, "
            "or drop an image into the start-frame slot."
        )
    return await _submit_wait_download(workflow, "seedance", ["mp4"], data_dir)


def _build_local_api(*, image_name: str | None, video_name: str | None,
                     prompt: str, model_variant: str, resolution: str,
                     ratio: str, duration: int) -> dict:
    """Hand-rolled API-format JSON for Seedance 2.0 R2V running against local
    ComfyUI. Four nodes: LoadImage (optional), LoadVideo (optional),
    ByteDance2ReferenceNode, SaveVideo. Widget keys use the DynamicCombo's
    nested `model.*` naming (see nodes_bytedance.py:2098) because every
    parameter after the model selector lives inside that combo's option
    group."""
    seed_inputs: dict = {
        "model": model_variant,
        "model.prompt": prompt,
        "model.resolution": resolution,
        "model.ratio": ratio,
        "model.duration": int(duration),
        "model.generate_audio": False,
        "model.auto_downscale": True,
        "model.auto_upscale": False,
        "seed": 0,
        "watermark": False,
    }
    workflow: dict = {}
    next_id = 1

    if image_name:
        image_id = str(next_id); next_id += 1
        workflow[image_id] = {
            "class_type": "LoadImage",
            "inputs": {"image": image_name, "upload": "image"},
            "_meta": {"title": "Load Image"},
        }
        seed_inputs["model.reference_images.image_1"] = [image_id, 0]

    if video_name:
        video_id = str(next_id); next_id += 1
        workflow[video_id] = {
            "class_type": "LoadVideo",
            "inputs": {"file": video_name, "upload": "image"},
            "_meta": {"title": "Load Video"},
        }
        seed_inputs["model.reference_videos.video_1"] = [video_id, 0]

    seedance_id = str(next_id); next_id += 1
    workflow[seedance_id] = {
        "class_type": SEEDANCE_NODE,
        "inputs": seed_inputs,
        "_meta": {"title": "ByteDance Seedance 2.0 Reference to Video"},
    }

    save_id = str(next_id); next_id += 1
    workflow[save_id] = {
        "class_type": "SaveVideo",
        "inputs": {
            "video": [seedance_id, 0],
            "filename_prefix": "seedance",
            "format": "auto",
            "codec": "auto",
        },
        "_meta": {"title": "Save Video"},
    }
    return workflow


async def _run_local(*, prompt: str, resolution: str, ratio: str, duration: int,
                     variant: str, image_path: Path | None, video_path: Path | None,
                     data_dir: Path) -> dict:
    model_variant = _LOCAL_MODEL_NAMES.get(variant, "Seedance 2.0")
    if not image_path and not video_path:
        raise ValueError(
            "Seedance 2.0 R2V (Local) needs a reference image or video. "
            "Record via the transport ● button, or drop an image into the "
            "start-frame slot."
        )

    async with httpx.AsyncClient() as client:
        alive, err = await check_alive(client)
        if not alive:
            raise RuntimeError(
                f"Local Comfy Desktop not reachable at {COMFY_URL} — start it "
                f"before running Seedance Local. ({err})"
            )
        image_name = None
        video_name = None
        if image_path and Path(image_path).exists():
            image_name = await upload_image_to_local(client, Path(image_path))
        if video_path and Path(video_path).exists():
            video_name = await upload_video_to_local(client, Path(video_path))

        workflow = _build_local_api(
            image_name=image_name,
            video_name=video_name,
            prompt=prompt,
            model_variant=model_variant,
            resolution=resolution,
            ratio=ratio,
            duration=int(duration),
        )
        # Partner-node auth — Seedance calls Comfy's /customers/storage during
        # execution, which needs our comfy.org API key. Local ComfyUI doesn't
        # auto-inject this on /prompt calls (Desktop UI does, but that's the
        # frontend; direct submitters like us have to include it explicitly).
        # Same env var main.py sets from /api/auth/key.
        api_key = os.environ.get("COMFY_API_KEY") or os.environ.get("COMFY_CLOUD_API_KEY")
        extra_data = {"api_key_comfy_org": api_key} if api_key else None
        if not extra_data:
            raise RuntimeError(
                "No Comfy API key set. Seedance's ByteDance node calls out to "
                "comfy.org and needs your API key. Sign in via the ComfyBlockout "
                "Settings pane (or set COMFY_API_KEY before launching)."
            )
        _, outputs = await submit_and_wait(client, workflow, extra_data=extra_data)
        # SaveVideo emits {"video": [{filename, subfolder, type}]}. Grab the
        # first mp4-ish output and download it into our data_dir.
        picked = None
        for node_out in outputs.values():
            for key in ("video", "videos", "images"):
                items = (node_out or {}).get(key) or []
                for item in items:
                    fn = item.get("filename", "").lower()
                    if fn.endswith((".mp4", ".webm", ".mov")):
                        picked = item
                        break
                if picked:
                    break
            if picked:
                break
        if not picked:
            raise RuntimeError(f"local Seedance produced no video output; outputs={outputs}")
        ext = Path(picked["filename"]).suffix.lstrip(".") or "mp4"
        dst = new_output_path(data_dir, "seedance", ext)
        await download_output(client, picked, dst)
        return {"path": str(dst), "filename": dst.name, "ext": ext}


async def run(*, prompt: str,
              resolution: str = "720p", ratio: str = "adaptive",
              duration: int = 5, variant: str = "standard",
              image_path: Path | None = None,
              video_path: Path | None = None,
              data_dir: Path, **_):
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")
    prompt = prompt.strip()
    kwargs = dict(
        prompt=prompt, resolution=resolution, ratio=ratio, duration=duration,
        variant=variant, image_path=image_path, video_path=video_path,
        data_dir=data_dir,
    )
    if variant in _LOCAL_MODEL_NAMES:
        return await _run_local(**kwargs)
    return await _run_cloud(**kwargs)


MODULE = ModuleDef(
    id="seedance",
    label="Seedance 2.0 — reference to video",
    kind="video",
    inputs=[
        {"name": "prompt", "type": "textarea", "required": True,
         "placeholder": "Describe the video"},
        # Optional scene-video / scene-image — server auto-populates video_path
        # from _video_store (transport recording) and image_path from
        # _image_store when the user hasn't dragged specific media in. Both
        # `required: False` — Seedance 2.0 accepts either as the reference.
        {"name": "video", "type": "scene-video", "required": False,
         "label": "Reference video (recording)"},
        {"name": "image", "type": "scene-image", "required": False,
         "label": "Reference image (start frame)"},
        {"name": "resolution", "type": "select", "default": "720p",
         "options": ["480p", "720p", "1080p"]},
        {"name": "ratio", "type": "select", "default": "adaptive",
         "options": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "9:21", "adaptive"]},
        {"name": "duration", "type": "number", "default": 5, "min": 5, "max": 12,
         "label": "Duration (seconds)"},
        # Standard/Lite → Comfy Cloud (no video-ref); Local/Local Lite → local
        # ComfyUI at 127.0.0.1:8188 (accepts video-ref via LoadVideo).
        {"name": "variant", "type": "select", "default": "standard",
         "options": ["standard", "lite", "local", "local-lite"]},
    ],
    output_ext="mp4",
    run=run,
)

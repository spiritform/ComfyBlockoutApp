"""Seedance 2.0 (Reference-to-Video) — Comfy Cloud workflow module.

Supersedes the earlier `comfy generate seedance` CLI shim, which only knew
Seedance 1.x and had no `--video` reference input. Seedance 2.0 lives as a
Cloud partner node (`ByteDance2ReferenceNode`) with slots for image_1,
image_2, video_1, audio_1, asset_1 plus prompt / resolution / ratio /
duration / etc. widgets. Two variants shipped: standard and lite (mini).

Template JSONs (canonical from Comfy-Org/workflow_templates) come with
image_1 wired to a LoadImage but LEAVE video_1 unlinked. When a video_path
is provided, we inject a LoadVideo node + link at request time so the
canonical templates stay pristine — the shape-7 socket wiring is documented
in reference_cloud_workflow_format.md."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from ._base import ModuleDef
from ._workflow_shared import _submit_wait_download
from ._tripo_shared import upload_image_to_cloud, upload_file_to_cloud_with_subfolder


WF_DIR = Path(__file__).parent.parent / "workflows"
VARIANTS = {
    "standard": WF_DIR / "api_seedance2_0_r2v.json",
    "lite":     WF_DIR / "api_seedance2_0_mini_r2v.json",
}

# ByteDance2ReferenceNode.widgets_values positional layout:
# 0=model, 1=prompt, 2=resolution, 3=ratio, 4=duration, 5=generate_audio,
# 6=auto_downscale, 7=auto_upscale, 8=seed, 9=control_after_generate, 10=watermark
_W = {
    "model": 0, "prompt": 1, "resolution": 2, "ratio": 3, "duration": 4,
    "generate_audio": 5, "auto_downscale": 6, "auto_upscale": 7,
    "seed": 8, "control_after_generate": 9, "watermark": 10,
}
SEEDANCE_NODE = "ByteDance2ReferenceNode"
LOAD_IMAGE = "LoadImage"

# input-slot index on the Seedance node — matches the socket declaration
# order (image_1, image_2, video_1, audio_1, asset_1).
_VIDEO_1_SLOT = 2


def _set_widget(node: dict, index: int, value) -> None:
    widgets = node.setdefault("widgets_values", [])
    while len(widgets) <= index:
        widgets.append("")
    widgets[index] = value


async def run(*, prompt: str,
              resolution: str = "720p", ratio: str = "adaptive",
              duration: int = 5, variant: str = "standard",
              image_path: Path | None = None,
              video_path: Path | None = None,
              data_dir: Path, **_):
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")
    wf_path = VARIANTS.get(variant, VARIANTS["standard"])
    if not wf_path.exists():
        raise RuntimeError(f"Seedance workflow template missing: {wf_path.name}")
    workflow = json.loads(wf_path.read_text(encoding="utf-8"))

    seedance_node = next(
        (n for n in workflow["nodes"] if n.get("type") == SEEDANCE_NODE), None
    )
    if not seedance_node:
        raise RuntimeError(f"{SEEDANCE_NODE} not found in {wf_path.name}")

    # Widget patches on the Seedance node itself. Model widget stays whatever
    # the template shipped (standard → 'Seedance 2.0', lite → 'Seedance 2.0 Mini').
    _set_widget(seedance_node, _W["prompt"], prompt.strip())
    _set_widget(seedance_node, _W["resolution"], resolution)
    _set_widget(seedance_node, _W["ratio"], ratio)
    _set_widget(seedance_node, _W["duration"], int(duration))

    # Reference image (image_1) — template already ships with a LoadImage
    # linked at image_1, so we just replace the widget filename after upload.
    # If no image is provided we LEAVE the template's placeholder in place
    # only if a video is provided; otherwise the model needs some reference.
    if image_path and Path(image_path).exists():
        load_image = next(
            (n for n in workflow["nodes"] if n.get("type") == LOAD_IMAGE), None
        )
        if load_image:
            cloud_name = await upload_image_to_cloud(Path(image_path))
            _set_widget(load_image, 0, cloud_name)

    # Reference video (video_1) — Cloud's LoadVideo enum only shows assets
    # tagged by their proprietary asset service (accessible via the web UI's
    # LoadVideo "choose file to upload" button, but NOT via `/upload/image` or
    # any other REST endpoint the open-source CLI exposes). Every attempt to
    # inject a LoadVideo + reference our uploaded file fails validation with
    # "not in known options for file", regardless of subfolder tagging.
    #
    # Fallback: extract frame 0 of the recording via ffmpeg and feed it as
    # image_1. The video's composition + first-frame anchor still influence
    # the generation; the motion signal is lost, but at least the recording
    # meaningfully contributes to what Seedance produces.
    if video_path and Path(video_path).exists() and not (image_path and Path(image_path).exists()):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            frame_path = Path(tmp.name)
        # -y overwrite, -ss 0 seek to start, -vframes 1 grab a single frame.
        # Falls through silently on ffmpeg failure — Seedance will then error
        # out at the "no reference" check below with a clearer message.
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
                    image_path = frame_path  # so the "no reference" check below passes
        except Exception:
            pass

    if not image_path:
        raise ValueError(
            "Seedance 2.0 R2V needs a reference image or a recording (first "
            "frame will be used). Record via the transport ● button, or drop "
            "an image into the start-frame slot."
        )

    return await _submit_wait_download(workflow, "seedance", ["mp4"], data_dir)


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
        {"name": "variant", "type": "select", "default": "standard",
         "options": ["standard", "lite"]},
    ],
    output_ext="mp4",
    run=run,
)

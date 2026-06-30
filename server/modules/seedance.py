"""Seedance (text-to-video) — `comfy generate seedance --prompt ... --resolution 1080p --duration 5 --download out.mp4`"""

from __future__ import annotations

from pathlib import Path

from ._base import ModuleDef, comfy_bin, new_output_path, run_cli


async def run(*, prompt: str, resolution: str = "1080p", duration: int = 5, data_dir: Path, **_):
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")
    try:
        duration_i = int(duration)
    except (TypeError, ValueError):
        raise ValueError("duration must be an integer (seconds)")

    out = new_output_path(data_dir, "seedance", "mp4")
    code, stdout, stderr = await run_cli([
        comfy_bin(), "generate", "seedance",
        "--prompt", prompt.strip(),
        "--resolution", str(resolution),
        "--duration", str(duration_i),
        "--download", str(out),
    ], timeout=1800)
    if code != 0 or not out.exists():
        raise RuntimeError(stderr.strip() or stdout.strip() or f"comfy generate failed (rc={code})")

    return {"path": str(out), "filename": out.name, "ext": "mp4"}


MODULE = ModuleDef(
    id="seedance",
    label="Seedance — text-to-video",
    kind="video",
    inputs=[
        {"name": "prompt", "type": "textarea", "required": True,
         "placeholder": "Describe the video"},
        {"name": "resolution", "type": "select", "default": "1080p",
         "options": ["480p", "720p", "1080p"]},
        {"name": "duration", "type": "number", "default": 5, "min": 1, "max": 12,
         "label": "Duration (seconds)"},
    ],
    output_ext="mp4",
    run=run,
)

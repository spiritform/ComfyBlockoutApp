"""Seedance (text-to-video AND image-to-video) — shells out to
`comfy generate seedance --prompt ... [--image <url|path>] --resolution 1080p --duration 5 --download out.mp4`.

The optional `image` input maps to seedance's `--image` first-frame parameter (see
`comfy generate seedance --help`: "Optional first-frame image (URL, local path, or
data URI). Local paths are auto-uploaded via /customers/storage."). When present
the CLI runs in image-to-video mode, anchoring the clip to the supplied still.
Without it, seedance falls back to pure text-to-video.
"""

from __future__ import annotations

from pathlib import Path

from ._base import ModuleDef, comfy_bin, new_output_path, run_cli


async def run(*, prompt: str, resolution: str = "1080p", duration: int = 5,
              image: str | None = None, data_dir: Path, **_):
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")
    try:
        duration_i = int(duration)
    except (TypeError, ValueError):
        raise ValueError("duration must be an integer (seconds)")

    out = new_output_path(data_dir, "seedance", "mp4")
    cmd = [
        comfy_bin(), "generate", "seedance",
        "--yes",  # non-TTY subprocess: skip credit-spend confirmation prompt
        "--prompt", prompt.strip(),
        "--resolution", str(resolution),
        "--duration", str(duration_i),
        "--download", str(out),
    ]
    # First-frame image locks the clip's opening to the supplied still. Local
    # /output/ URLs from the frontend need to be resolved to on-disk paths so
    # the CLI's auto-upload path picks them up; anything else (http:// or a
    # data URI) is handed through as-is.
    if image:
        img_arg = image.strip()
        if img_arg.startswith("/output/"):
            # Frontend URL — map back to the file under our DATA_DIR.
            rel = img_arg[len("/output/"):]
            local = data_dir / rel
            if local.exists():
                img_arg = str(local)
        cmd.extend(["--image", img_arg])

    code, stdout, stderr = await run_cli(cmd, timeout=1800)
    if code != 0 or not out.exists():
        raise RuntimeError(stderr.strip() or stdout.strip() or f"comfy generate failed (rc={code})")

    return {"path": str(out), "filename": out.name, "ext": "mp4"}


MODULE = ModuleDef(
    id="seedance",
    label="Seedance — text/image-to-video",
    kind="video",
    inputs=[
        {"name": "prompt", "type": "textarea", "required": True,
         "placeholder": "Describe the video"},
        {"name": "image", "type": "image", "required": False,
         "label": "Start frame (optional)"},
        {"name": "resolution", "type": "select", "default": "1080p",
         "options": ["480p", "720p", "1080p"]},
        {"name": "duration", "type": "number", "default": 5, "min": 1, "max": 12,
         "label": "Duration (seconds)"},
    ],
    output_ext="mp4",
    run=run,
)

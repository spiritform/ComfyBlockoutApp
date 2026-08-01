"""Nano Banana (image edit) — `comfy generate nano-banana --prompt ... --image '["path"]' --download ...`

Note: nano-banana's --image takes a JSON array (one or more reference images),
not a bare path. Seedance and most other partners take a plain string."""

from __future__ import annotations

import json
from pathlib import Path

from ._base import ModuleDef, comfy_bin, new_output_path, run_cli


async def run(*, prompt: str, image_path: Path | None = None, data_dir: Path, references: list[str] | None = None, **_):
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")

    # Build the --image array: source image first (if any), then user-uploaded
    # refs. When image_path is None (user × the viewport-blockout row) AND no
    # refs, nano runs as pure text-to-image — `--image` is dropped entirely.
    images: list[str] = []
    if image_path and Path(image_path).exists():
        images.append(str(image_path))
    if references:
        images.extend([r for r in references if r])

    out = new_output_path(data_dir, "nano-banana", "png")
    argv = [
        comfy_bin(), "generate", "nano-banana",
        "--yes",  # non-TTY subprocess: skip credit-spend confirmation prompt
        "--model", "gemini-3-pro-image-preview",
        "--prompt", prompt.strip(),
        "--download", str(out),
    ]
    if images:
        argv.extend(["--image", json.dumps(images)])
    code, stdout, stderr = await run_cli(argv)
    if code != 0 or not out.exists():
        raise RuntimeError(stderr.strip() or stdout.strip() or f"comfy generate failed (rc={code})")

    return {"path": str(out), "filename": out.name, "ext": "png"}


MODULE = ModuleDef(
    id="nano-banana",
    label="Nano Banana — image edit",
    kind="image",
    inputs=[
        {"name": "prompt", "type": "textarea", "required": True,
         "placeholder": "Describe the edit (e.g. 'add a top hat')"},
        {"name": "image", "type": "scene-image", "required": False,
         "label": "Source image", "help": "Uses the editor's current snapshot. Skippable — set skip_source_image=true for text-to-image mode."},
    ],
    output_ext="png",
    run=run,
)

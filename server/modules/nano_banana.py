"""Nano Banana (image edit) — `comfy generate nano-banana --prompt ... --image '["path"]' --download ...`

Note: nano-banana's --image takes a JSON array (one or more reference images),
not a bare path. Seedance and most other partners take a plain string."""

from __future__ import annotations

import json
from pathlib import Path

from ._base import ModuleDef, comfy_bin, new_output_path, run_cli


async def run(*, prompt: str, image_path: Path, data_dir: Path, references: list[str] | None = None, **_):
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")
    if not image_path or not Path(image_path).exists():
        raise ValueError(f"image not found: {image_path}")

    # Build the --image array: scene snapshot first, then any user-uploaded refs
    # (signed URLs from `comfy generate upload`).
    images: list[str] = [str(image_path)]
    if references:
        images.extend([r for r in references if r])

    out = new_output_path(data_dir, "nano-banana", "png")
    images_arg = json.dumps(images)
    code, stdout, stderr = await run_cli([
        comfy_bin(), "generate", "nano-banana",
        "--prompt", prompt.strip(),
        "--image", images_arg,
        "--download", str(out),
    ])
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
        {"name": "image", "type": "scene-image", "required": True,
         "label": "Source image", "help": "Uses the editor's current snapshot."},
    ],
    output_ext="png",
    run=run,
)

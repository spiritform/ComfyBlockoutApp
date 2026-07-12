"""Tripo H3.1 — unified Text-or-Image → 3D Model (Comfy Cloud).

Front-end dispatcher for the Tools-grid "Tripo 3D" tile. A single button
spawns a placeholder in the scene; its properties pane exposes a mode toggle
(Text / Image), an input for the chosen mode, and a Generate button. This
module receives `mode` in the run inputs and delegates to the appropriate
underlying module — `tripo_image_to_model` or `tripo_text_to_model` — both
of which are already wired to Comfy Cloud and return .glb.

Declares an `image` input of type `scene-image` so image mode auto-resolves
the current viewport snapshot into `image_path` (the standard runner path).
For text mode the client sends `skip_source_image: true` so no snapshot is
required — the runner synthesizes a placeholder image that we ignore.
"""

from __future__ import annotations

from pathlib import Path

from ._base import ModuleDef
from . import tripo_image_to_model, tripo_text_to_model


async def run(*, mode: str = "image", prompt: str = "", image_path: Path | None = None,
              data_dir: Path, pbr: bool = False, texture_quality: str = "standard",
              quad: bool = False, status_cb=None, **kw):
    mode = (mode or "image").strip().lower()
    # Shared options — forwarded to whichever underlying module runs. Both
    # modules accept the same three kwargs and patch them onto the Tripo
    # widget set of their respective workflow.
    opts = dict(pbr=bool(pbr), texture_quality=texture_quality, quad=bool(quad),
                status_cb=status_cb)
    if mode == "text":
        return await tripo_text_to_model.run(prompt=prompt, data_dir=data_dir, **opts, **kw)
    if not image_path:
        raise ValueError("image mode requires a source image — snapshot the viewport first")
    return await tripo_image_to_model.run(image_path=Path(image_path), data_dir=data_dir, **opts, **kw)


MODULE = ModuleDef(
    id="tripo-h31",
    label="Tripo 3D",
    kind="3d",
    inputs=[
        {"name": "mode", "type": "text", "default": "image",
         "help": "text | image — chooses which underlying Tripo H3.1 endpoint runs."},
        {"name": "prompt", "type": "textarea",
         "placeholder": "Describe the 3D model (text mode only)"},
        {"name": "image", "type": "scene-image",
         "label": "Source image",
         "help": "Used in image mode. Auto-resolves to the current viewport snapshot; ignored in text mode."},
    ],
    output_ext="glb",
    # Off the Tools grid — the Comfy Cloud partner-3D download path is
    # unreliable (see project_tripo_h31_cloud_broken memory). Kept in the
    # module registry so /api/run/tripo-h31 still works if the user opens
    # it via the Workflows list to test whether Cloud has been patched.
    util=False,
    # Lucide "box" — reads as a 3D mesh output. Distinct from the astroid
    # glyph the old TripoSplat button carried; splats now live in the
    # Workflows list, not the Tools grid.
    icon=(
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>'
        '<path d="m3.3 7 8.7 5 8.7-5"/>'
        '<path d="M12 22V12"/>'
        '</svg>'
    ),
    run=run,
)

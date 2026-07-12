"""Tripo H3.1 Text → 3D Model (Comfy Cloud template `api_tripo3_1_text_to_model`).

No image input — just a text prompt patched into the TripoTextToModelNode (id 1)
of the shipped workflow. Result comes back as a .glb mesh, imported into the
scene through the standard GLB loader path."""

from __future__ import annotations

import random
from pathlib import Path

from ._base import ModuleDef
from ._tripo_shared import (
    load_workflow,
    find_node,
    run_workflow_and_fetch_glb,
)


WORKFLOW_NAME = "api_tripo3_1_text_to_model.json"
# Node 1 is the TripoTextToModelNode. Workflow is still in graph format so
# widgets_values is positional. Positions determined by the shipped JSON:
#   [0]  prompt
#   [1]  negative_prompt
#   [2]  model_version
#   [3]  style
#   [4]  texture
#   [5]  pbr
#   [6]  model_seed
#   [7]  seed (unnamed; randomized alongside model_seed for full determinism)
#   [8]  texture_seed
#   [9]  texture_quality
#   [10] face_limit
#   [11] quad
#   [12] geometry_quality
TRIPO_T2M_NODE_ID = 1
_PROMPT_IDX = 0
_NEG_PROMPT_IDX = 1
_PBR_IDX = 5
_MODEL_SEED_IDX = 6
_MID_SEED_IDX = 7
_TEXTURE_SEED_IDX = 8
_TEXTURE_QUALITY_IDX = 9
_QUAD_IDX = 11


async def run(*, prompt: str, data_dir: Path, negative_prompt: str = "",
              pbr: bool = False, texture_quality: str = "standard",
              quad: bool = False, status_cb=None, **_):
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")

    workflow = load_workflow(WORKFLOW_NAME)
    t2m = find_node(workflow, TRIPO_T2M_NODE_ID)
    if not t2m:
        raise RuntimeError(f"TripoTextToModelNode {TRIPO_T2M_NODE_ID} missing — workflow drift?")
    widgets = t2m.setdefault("widgets_values", [])
    # Grow the array to cover the highest index we're about to touch — a
    # freshly-authored workflow could ship shorter than 13 if a field was
    # removed upstream.
    while len(widgets) <= _QUAD_IDX:
        widgets.append("")
    widgets[_PROMPT_IDX] = prompt.strip()
    widgets[_NEG_PROMPT_IDX] = (negative_prompt or "").strip()
    widgets[_PBR_IDX] = bool(pbr)
    widgets[_TEXTURE_QUALITY_IDX] = texture_quality if texture_quality in {"standard", "detailed"} else "standard"
    widgets[_QUAD_IDX] = bool(quad)
    # Randomize every seed slot so a re-run genuinely resamples.
    widgets[_MODEL_SEED_IDX] = random.randint(0, 2**31 - 1)
    widgets[_MID_SEED_IDX] = random.randint(0, 2**31 - 1)
    widgets[_TEXTURE_SEED_IDX] = random.randint(0, 2**31 - 1)

    return await run_workflow_and_fetch_glb(workflow, "tripo-t2m", data_dir,
                                             status_cb=status_cb)


MODULE = ModuleDef(
    id="tripo-t2m",
    label="Tripo H3.1 — Text to Model",
    kind="3d",
    inputs=[
        {"name": "prompt", "type": "textarea", "required": True,
         "placeholder": "Describe the 3D model you want (e.g. 'armored knight kneeling with sword')"},
    ],
    output_ext="glb",
    run=run,
)

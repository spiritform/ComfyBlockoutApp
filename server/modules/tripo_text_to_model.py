"""Tripo H3.1 Text → 3D Model (Comfy Cloud template `api_tripo3_1_text_to_model`).

No image input — just a text prompt patched into the TripoTextToModelNode (id 1)
of the shipped workflow. Result comes back as a .glb mesh, imported into the
scene through the standard GLB loader path."""

from __future__ import annotations

from pathlib import Path

from ._base import ModuleDef
from ._tripo_shared import (
    load_workflow,
    find_node,
    run_workflow_and_fetch_glb,
)


WORKFLOW_NAME = "api_tripo3_1_text_to_model.json"
# Node 1 is the TripoTextToModelNode; widgets_values[0] is the positive prompt,
# widgets_values[1] is the negative prompt.
TRIPO_T2M_NODE_ID = 1


async def run(*, prompt: str, data_dir: Path, negative_prompt: str = "", **_):
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")

    workflow = load_workflow(WORKFLOW_NAME)
    t2m = find_node(workflow, TRIPO_T2M_NODE_ID)
    if not t2m:
        raise RuntimeError(f"TripoTextToModelNode {TRIPO_T2M_NODE_ID} missing — workflow drift?")
    widgets = t2m.setdefault("widgets_values", [])
    while len(widgets) < 2:
        widgets.append("")
    widgets[0] = prompt.strip()
    widgets[1] = (negative_prompt or "").strip()

    return await run_workflow_and_fetch_glb(workflow, "tripo-t2m", data_dir)


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

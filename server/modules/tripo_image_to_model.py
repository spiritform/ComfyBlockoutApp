"""Tripo H3.1 Image → 3D Model (Comfy Cloud template `api_tripo3_1_image_to_model`).

Uploads the source image, patches the workflow's LoadImage node (id 10) with the
uploaded filename, submits the workflow to Cloud, downloads the resulting .glb,
and drops it in data/ where the frontend picks it up and imports it into the
scene as a mesh."""

from __future__ import annotations

from pathlib import Path

from ._base import ModuleDef
from ._tripo_shared import (
    load_workflow,
    find_node,
    upload_image_to_cloud,
    run_workflow_and_fetch_glb,
)


WORKFLOW_NAME = "api_tripo3_1_image_to_model.json"
LOAD_IMAGE_NODE_ID = 10


async def run(*, image_path: Path, data_dir: Path, **_):
    if not image_path:
        raise ValueError("image is required")
    image_path = Path(image_path)

    uploaded_name = await upload_image_to_cloud(image_path)

    workflow = load_workflow(WORKFLOW_NAME)
    li = find_node(workflow, LOAD_IMAGE_NODE_ID)
    if not li:
        raise RuntimeError(f"LoadImage node {LOAD_IMAGE_NODE_ID} missing — workflow drift?")
    li.setdefault("widgets_values", ["", "image"])
    li["widgets_values"][0] = uploaded_name

    return await run_workflow_and_fetch_glb(workflow, "tripo-i2m", data_dir)


MODULE = ModuleDef(
    id="tripo-i2m",
    label="Tripo H3.1 — Image to Model",
    kind="3d",
    inputs=[
        {"name": "image", "type": "scene-image", "required": True,
         "label": "Source image", "help": "Runs the Tripo H3.1 image-to-model API on Comfy Cloud; result imports as a .glb mesh."},
    ],
    output_ext="glb",
    run=run,
)

"""Tripo H3.1 Image → 3D Model (Comfy Cloud template `api_tripo3_1_image_to_model`).

Uploads the source image, patches the LoadImage node's `image` input with the
uploaded filename, submits the workflow to Cloud, downloads the resulting .glb,
and drops it in data/ where the frontend picks it up and imports it into the
scene as a mesh.

Workflow is authored in API format (top-level dict keyed by node id string,
each entry has `class_type` + `inputs`). Graph-format submissions cause Cloud
to complete the job successfully but leave `job.outputs` empty forever, which
makes `comfy download` hang on `download_no_outputs` even though the mesh is
visible in Cloud's Media Assets. API format sidesteps that whole class of bug —
see `reference_cloud_workflow_format.md`."""

from __future__ import annotations

from pathlib import Path

from ._base import ModuleDef
from ._tripo_shared import (
    load_workflow,
    upload_image_to_cloud,
    run_workflow_and_fetch_glb,
)


WORKFLOW_NAME = "api_tripo3_1_image_to_model.json"
# LoadImage lives at API-format key "10" — the value's `inputs.image` is the
# widget the Cloud runner reads for the source filename.
LOAD_IMAGE_NODE_KEY = "10"


async def run(*, image_path: Path, data_dir: Path, **_):
    if not image_path:
        raise ValueError("image is required")
    image_path = Path(image_path)

    uploaded_name = await upload_image_to_cloud(image_path)

    workflow = load_workflow(WORKFLOW_NAME)
    li = workflow.get(LOAD_IMAGE_NODE_KEY)
    if not li or not isinstance(li, dict):
        raise RuntimeError(f"LoadImage node '{LOAD_IMAGE_NODE_KEY}' missing — workflow drift?")
    li.setdefault("inputs", {})["image"] = uploaded_name

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

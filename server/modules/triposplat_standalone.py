"""TripoSplat (standalone) — image → gaussian splat via a local Docker
container.

Talks to the FastAPI server at `tools/triposplat/server.py` running inside
the `triposplat` Docker service (default port 8004). Zero dependency on
Comfy Desktop or ComfyUI — the container hosts the VAST-AI TripoSplat
pipeline directly, so users on machines without a running ComfyUI can
still generate splats.

Requirements-pane flow: `/api/requirements/triposplat` in main.py probes
Docker + container health + weight-download state, and the frontend
surfaces those states as amber-pulse rows until they're all green (see
feedback_requirements_pane_pattern memory for the visual template).
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx

from ._base import ModuleDef, new_output_path


TRIPOSPLAT_HOST = os.environ.get("TRIPOSPLAT_HOST", "127.0.0.1")
TRIPOSPLAT_PORT = int(os.environ.get("TRIPOSPLAT_PORT", "8004"))
TRIPOSPLAT_URL = f"http://{TRIPOSPLAT_HOST}:{TRIPOSPLAT_PORT}"
# 15 min — first request warms the pipeline (~30s ckpt load) AND runs
# inference (~1-2 min on a decent GPU). Set high to survive a slower box.
GENERATE_TIMEOUT = 900


async def _check_alive(client: httpx.AsyncClient) -> dict:
    """Fail fast with a clear message if the container isn't up yet.
    Returns the /health payload so callers can gate on weights_ready
    (before hitting /generate the pipeline still needs its weights on
    disk, or the request will block for the whole HF snapshot download)."""
    try:
        r = await client.get(f"{TRIPOSPLAT_URL}/health", timeout=5.0)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        raise RuntimeError(
            f"TripoSplat container not reachable at {TRIPOSPLAT_URL} — "
            f"start it with `docker compose up -d` from tools/triposplat/. ({e})"
        ) from e
    if not j.get("weights_ready"):
        raise RuntimeError(
            "TripoSplat weights are still downloading. Check "
            "`docker compose logs triposplat` and retry in a few minutes."
        )
    return j


async def run(*, image_path: Path, data_dir: Path, num_gaussians: int = 262144,
              status_cb=None, **_):
    if not image_path:
        raise ValueError("image is required")
    image_path = Path(image_path)
    if not image_path.exists():
        raise ValueError(f"image not found: {image_path}")

    def _emit(phase: str, **extra):
        if status_cb:
            try:
                status_cb(phase, **extra)
            except Exception:
                pass

    _emit("generating")

    async with httpx.AsyncClient() as client:
        await _check_alive(client)

        with image_path.open("rb") as f:
            files = {"image": (image_path.name, f, "application/octet-stream")}
            data = {"num_gaussians": str(num_gaussians)}
            r = await client.post(
                f"{TRIPOSPLAT_URL}/generate",
                files=files,
                data=data,
                timeout=GENERATE_TIMEOUT,
            )
        if r.status_code != 200:
            raise RuntimeError(
                f"TripoSplat /generate failed (rc={r.status_code}): {r.text[:800]}"
            )

        _emit("fetching")
        dst = new_output_path(data_dir, "triposplat-standalone", "ply")
        dst.write_bytes(r.content)

    # Save a thumbnail alongside the .ply so the Assets pane shows the
    # source image on the tile (mirrors local_triposplat's behaviour).
    try:
        src_ext = image_path.suffix.lstrip(".").lower() or "png"
        thumb = dst.with_suffix(f".thumb.{src_ext}")
        thumb.write_bytes(image_path.read_bytes())
    except Exception as e:
        print(f"[cb-app] triposplat-standalone: thumb save skipped: {e}")

    return {"path": str(dst), "filename": dst.name, "ext": "ply"}


MODULE = ModuleDef(
    id="triposplat-standalone",
    label="TripoSplat",
    kind="3d",
    inputs=[
        {"name": "image", "type": "scene-image", "required": True,
         "label": "Source image",
         "help": ("Local TripoSplat runs in a Docker container (no Comfy "
                  "Desktop required). Generates a gaussian splat (.ply) "
                  "from the source image.")},
    ],
    output_ext="ply",
    # Hidden from the Tools grid — the local ComfyUI variant (triposplat-local)
    # is the only user-facing TripoSplat tile. Backend module stays available
    # for old scenes that still reference the -standalone id.
    util=False,
    # Reuse the astroid glyph that the old local_triposplat carried — same
    # tool identity, different runtime.
    icon=(
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M12.983 21.186a1 1 0 0 1-1.966 0 10 10 0 0 0-8.203-8.203 1 1 0 0 1 0-1.966 10 10 0 0 0 8.203-8.203 1 1 0 0 1 1.966 0 10 10 0 0 0 8.203 8.203 1 1 0 0 1 0 1.966 10 10 0 0 0-8.203 8.203"/>'
        '</svg>'
    ),
    run=run,
)

"""FastAPI wrapper around VAST-AI-Research/TripoSplat's TripoSplatPipeline.

Loads the pipeline lazily on the first `/generate` request (torch imports
+ ckpt loads take ~30s cold), keeps it warm across subsequent requests, and
exposes:

  GET  /health   → readiness probe used by docker-compose + the frontend
                   requirements pane.
  POST /generate → multipart upload of a source image; returns a .ply
                   gaussian-splat file. Optional `num_gaussians` form
                   field overrides the default splat count.

Weights: on first startup, if `ckpts/` is empty, we snapshot_download the
entire VAST-AI/TripoSplat HuggingFace repo into the ckpts volume. Subsequent
starts reuse the cached weights (few seconds instead of a multi-GB pull).
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("triposplat")

CKPTS_DIR = Path(os.environ.get("TRIPOSPLAT_CKPTS", "/app/ckpts"))
HF_REPO = "VAST-AI/TripoSplat"
# Filenames the pipeline expects, per README + example. We check the ckpts
# dir for these on startup; any missing → we resnapshot the HF repo.
EXPECTED_CKPT_FILES = [
    "model.safetensors",
    "decoder.safetensors",
    "dinov3.safetensors",
    "flux2_vae_encoder.safetensors",
    "rmbg.safetensors",
]

# One-time pipeline handle guarded by a lock so concurrent requests during
# cold-start don't try to load twice. Once `_pipeline` is non-None it can be
# read lock-free.
_pipeline = None
_pipeline_lock = threading.Lock()


def _weights_present() -> bool:
    if not CKPTS_DIR.exists():
        return False
    have = {p.name for p in CKPTS_DIR.rglob("*") if p.is_file()}
    return all(name in have for name in EXPECTED_CKPT_FILES)


def _download_weights() -> None:
    """Pull the VAST-AI/TripoSplat repo into CKPTS_DIR. Idempotent — a
    partial prior download resumes from cache."""
    from huggingface_hub import snapshot_download
    token = os.environ.get("HF_TOKEN") or None
    log.info(f"downloading TripoSplat weights → {CKPTS_DIR} (this can take a while on first boot)")
    snapshot_download(
        repo_id=HF_REPO,
        local_dir=str(CKPTS_DIR),
        token=token,
        # No `local_dir_use_symlinks` — we want flat files so torch's
        # loader doesn't chase Windows-hostile symlinks when someone
        # mounts a Windows-hosted volume.
    )
    log.info("weights ready")


def _init_pipeline():
    """Load the pipeline. Called under `_pipeline_lock`."""
    if not _weights_present():
        _download_weights()

    # Deferred imports — torch pulls in CUDA and adds ~10s just to check
    # for GPU. Keeping it here means `/health` stays snappy while ckpts
    # are still downloading.
    import torch
    from triposplat import TripoSplatPipeline  # type: ignore

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu":
        log.warning("no CUDA detected — TripoSplat will run on CPU and will be extremely slow")

    log.info("instantiating TripoSplatPipeline")
    pipe = TripoSplatPipeline(
        ckpt_path=str(CKPTS_DIR / "model.safetensors"),
        decoder_path=str(CKPTS_DIR / "decoder.safetensors"),
        dinov3_path=str(CKPTS_DIR / "dinov3.safetensors"),
        flux2_vae_encoder_path=str(CKPTS_DIR / "flux2_vae_encoder.safetensors"),
        rmbg_path=str(CKPTS_DIR / "rmbg.safetensors"),
        device=device,
    )
    log.info("pipeline ready")
    return pipe


def _get_pipeline():
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    with _pipeline_lock:
        if _pipeline is None:
            _pipeline = _init_pipeline()
    return _pipeline


app = FastAPI(title="TripoSplat", version="1.0")


@app.get("/health")
def health():
    """Cheap readiness probe. `weights_ready` flips true once the HF
    snapshot has finished; `pipeline_ready` once TripoSplatPipeline is
    instantiated. Frontend requirements pane surfaces both."""
    return {
        "ok": True,
        "weights_ready": _weights_present(),
        "pipeline_ready": _pipeline is not None,
        "ckpts_dir": str(CKPTS_DIR),
    }


@app.post("/generate")
async def generate(
    image: UploadFile = File(...),
    num_gaussians: int = Form(262144),
):
    """Run one TripoSplat inference. Returns the PLY as an application/
    octet-stream download. Client is responsible for saving with an
    appropriate filename."""
    if num_gaussians <= 0 or num_gaussians > 1_048_576:
        raise HTTPException(400, "num_gaussians out of range (1..1048576)")

    # Save the upload to a temp file — TripoSplatPipeline.run() takes a
    # path, not a buffer, and we don't want to reimplement its PIL
    # loading here.
    suffix = Path(image.filename or "").suffix.lower() or ".png"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
        tf.write(await image.read())
        in_path = Path(tf.name)

    out_path = in_path.with_suffix(".ply")
    try:
        pipe = _get_pipeline()
        log.info(f"generating splat: input={in_path.name} num_gaussians={num_gaussians}")
        gaussian, _prepared = pipe.run(
            input=str(in_path),
            num_gaussians=num_gaussians,
            show_progress=False,
        )
        gaussian.save_ply(str(out_path))
        log.info(f"generated {out_path.name} ({out_path.stat().st_size} bytes)")
        return FileResponse(
            str(out_path),
            media_type="application/octet-stream",
            filename=f"triposplat_{num_gaussians}.ply",
        )
    except Exception as e:
        log.exception("generate failed")
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    finally:
        # Leave out_path alone — FileResponse streams it, then FastAPI
        # cleans up after the response body has been sent.
        try:
            in_path.unlink()
        except OSError:
            pass

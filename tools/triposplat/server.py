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

import asyncio
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
# Actual on-disk layout of the VAST-AI/TripoSplat repo — checkpoints live
# in subfolders, not flat under ckpts/. Paths mirror `run_example.py` in
# the upstream repo. Any missing → we resnapshot.
CKPT_MAP = {
    "ckpt":         "diffusion_models/triposplat_fp16.safetensors",
    "decoder":      "vae/triposplat_vae_decoder_fp16.safetensors",
    "dinov3":       "clip_vision/dino_v3_vit_h.safetensors",
    "flux2_vae":    "vae/flux2-vae.safetensors",
    "rmbg":         "background_removal/birefnet.safetensors",
}

# One-time pipeline handle guarded by a lock so concurrent requests during
# cold-start don't try to load twice. Once `_pipeline` is non-None it can be
# read lock-free.
_pipeline = None
_pipeline_lock = threading.Lock()


def _weights_present() -> bool:
    if not CKPTS_DIR.exists():
        return False
    return all((CKPTS_DIR / rel).exists() for rel in CKPT_MAP.values())


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
        ckpt_path=str(CKPTS_DIR / CKPT_MAP["ckpt"]),
        decoder_path=str(CKPTS_DIR / CKPT_MAP["decoder"]),
        dinov3_path=str(CKPTS_DIR / CKPT_MAP["dinov3"]),
        flux2_vae_encoder_path=str(CKPTS_DIR / CKPT_MAP["flux2_vae"]),
        rmbg_path=str(CKPTS_DIR / CKPT_MAP["rmbg"]),
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


# Kick off the HF snapshot as soon as the container boots so the frontend
# requirements pane can see progress instead of a silent /health probe
# loop. Runs in a background thread so /health stays responsive during
# the multi-GB download. Idempotent — snapshot_download resumes cached
# files without re-pulling.
_weights_download_thread: threading.Thread | None = None


def _ensure_weights_download_started() -> None:
    global _weights_download_thread
    if _weights_present():
        return
    if _weights_download_thread and _weights_download_thread.is_alive():
        return

    def _worker():
        try:
            _download_weights()
        except Exception:
            log.exception("background weight download failed — will retry on next /generate")

    t = threading.Thread(target=_worker, name="triposplat-weights", daemon=True)
    t.start()
    _weights_download_thread = t


@app.on_event("startup")
def _on_startup():
    _ensure_weights_download_started()


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

        # Offload the blocking pipe.run() to a worker thread so the event
        # loop stays free — otherwise /health probes queue behind the
        # multi-minute inference and the frontend requirements pane goes
        # red mid-run with "container up but /health unreachable".
        def _run_and_save() -> None:
            # `input` is positional in TripoSplatPipeline.run — passing it
            # as a kwarg raises TypeError. Matches upstream run_example.py.
            gaussian, _prepared = pipe.run(
                str(in_path),
                num_gaussians=num_gaussians,
                show_progress=False,
            )
            gaussian.save_ply(str(out_path))

        await asyncio.to_thread(_run_and_save)
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

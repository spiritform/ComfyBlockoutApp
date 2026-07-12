# TripoSplat standalone inference container.
#
# Wraps VAST-AI-Research/TripoSplat's `TripoSplatPipeline` behind a small
# FastAPI server on port 8000 (mapped to the host by docker-compose). The
# container clones the upstream repo at build time and installs its own
# deps + FastAPI/uvicorn. Model weights are NOT baked in — they'd be
# multiple GB and slow every rebuild; instead the container downloads them
# from HuggingFace on first startup and caches them in a named volume
# (see docker-compose.yml).
#
# GPU is required. Base image ships CUDA 12.4 runtime + cuDNN so the
# torch install can find the right libraries without a separate layer.

FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/root/.cache/huggingface \
    TRIPOSPLAT_CKPTS=/app/ckpts

# Base system: Python 3.11 + git + build tools (torch install needs a
# compiler for a couple of small C extensions on some torch builds).
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-dev python3-pip python3.11-venv \
    git build-essential curl ca-certificates \
    && ln -sf /usr/bin/python3.11 /usr/bin/python \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Clone TripoSplat + install its Python deps in one layer so a repo bump
# invalidates the cache exactly at the point where deps might have changed.
# `--depth 1` to keep the image small; pinning to a specific commit later
# if we hit drift is straightforward (add `git checkout <sha>`).
RUN git clone --depth 1 https://github.com/VAST-AI-Research/TripoSplat.git /app/triposplat

# TripoSplat's README emphasises minimal deps (numpy, safetensors, pillow,
# tqdm, plus torch/torchvision). Install torch matched to the base CUDA
# stream first so the resolver doesn't try to pick a CPU wheel.
RUN pip install --upgrade pip setuptools wheel \
    && pip install torch==2.4.1 torchvision==0.19.1 --index-url https://download.pytorch.org/whl/cu124 \
    && pip install numpy safetensors pillow tqdm einops \
                   huggingface_hub \
                   fastapi uvicorn python-multipart

# Copy our HTTP wrapper. Kept as a single-file server so a maintainer
# tweaking the endpoint doesn't have to hunt through the tree.
COPY server.py /app/server.py

# TripoSplat expects `ckpts/` as its checkpoint dir. We mkdir now and
# server.py will hf_download into it on startup if empty. Bind-mount /
# named volume in docker-compose points at this path so weights survive
# container rebuilds.
RUN mkdir -p ${TRIPOSPLAT_CKPTS}

# Make the cloned repo importable ("from triposplat import
# TripoSplatPipeline" works out of the box).
ENV PYTHONPATH=/app/triposplat:${PYTHONPATH}

# Optional HF token — public repo download works anonymously, but a token
# raises the anonymous rate limit and future-proofs against the repo
# going gated. Pass at run time via .env / -e HF_TOKEN=…
ENV HF_TOKEN=

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]

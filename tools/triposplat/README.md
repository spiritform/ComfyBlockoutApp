# TripoSplat (standalone)

Local gaussian-splat generation via [VAST-AI-Research/TripoSplat](https://github.com/VAST-AI-Research/TripoSplat),
wrapped in a Docker container so ComfyBlockout can call it without needing
Comfy Desktop or ComfyUI running.

## What you need

- Docker Desktop with GPU support enabled (Settings → Resources → WSL
  Integration + Enable Nvidia GPU support on Windows).
- An NVIDIA GPU with ≥ 8 GB VRAM (12+ recommended).
- ~15 GB free disk for the image + weights.

## First run

```bash
cd tools/triposplat
docker compose up --build -d
docker compose logs -f
```

First boot pulls the TripoSplat weights from HuggingFace (~5 GB). Watch
the logs — you'll see `downloading TripoSplat weights → /app/ckpts` and
then `weights ready` once the snapshot finishes. Subsequent starts reuse
the cached weights (few seconds).

Health check:

```bash
curl http://localhost:8004/health
# → {"ok": true, "weights_ready": true, "pipeline_ready": false, ...}
```

`pipeline_ready` only flips true after the first `/generate` request has
warmed the model — that's a ~30s one-time cost.

## HuggingFace token (optional)

The `VAST-AI/TripoSplat` repo is public; anonymous downloads work.
Anonymous rate limits can bite if you're rebuilding often — drop an
`HF_TOKEN` in `.env` next to `docker-compose.yml`:

```
HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## Ports

Default host port is `8004`. Override via `.env`:

```
TRIPOSPLAT_PORT=9004
```

## Endpoints

- `GET  /health`  — readiness (weights + pipeline state)
- `POST /generate` — multipart form; fields:
    - `image` (file, required) — source image
    - `num_gaussians` (int, default 262144) — splat count

Returns the PLY file as `application/octet-stream`.

## Troubleshooting

**Container never becomes healthy** — check `docker compose logs
triposplat`. Most common cause is the HF snapshot still running; give
it 5-10 minutes on a fresh install. `weights_ready: true` in `/health`
means that step's done.

**CUDA out of memory** — reduce `num_gaussians` (try 65536), or close
other GPU workloads. Default 262144 assumes ~10 GB VRAM headroom.

**Wants to reset weights** — `docker volume rm triposplat_ckpts
triposplat_hf_cache`, then rebuild.

# comfyblockout-app

Standalone version of [ComfyBlockout](../ComfyBlockout) — the 3D blockout editor
talks to Comfy Cloud directly via `comfy-cli` instead of running inside ComfyUI.

## Run

```
run.bat
```

First launch creates a `.venv`, installs deps (FastAPI + comfy-cli + ffmpeg shim),
then opens [http://127.0.0.1:8765](http://127.0.0.1:8765).

## Auth

Two paths, pick one:

- **OAuth** (recommended) — in the editor, click *Sign in with Comfy*. Browser opens,
  you sign in at platform.comfy.org, token is stored locally by `comfy-cli`.
- **API key** — set `COMFY_API_KEY=comfyui-…` in the environment before `run.bat`.

## How it talks to Comfy Cloud

No ComfyUI, no workflow JSONs. Each generator is a Python module under
`server/modules/` that shells out to `comfy generate <model> ...`:

| Module       | CLI                                                                 |
|--------------|---------------------------------------------------------------------|
| nano-banana  | `comfy generate nano-banana --prompt … --image … --download …`      |
| seedance     | `comfy generate seedance --prompt … --resolution … --duration …`    |

Drop a new file in `server/modules/` to add a model. The editor auto-renders
a section per module.

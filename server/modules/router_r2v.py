"""Reference-to-video via Comfy Router — the blockout drives the generation.

This is the pass-2 half of the blockout pipeline: record a camera move in the
editor, then hand that clip to a video model as a *motion reference* so the
output matches its timing and camera work.

Why this doesn't go through the workflow runners
------------------------------------------------
The cloud-workflow path can't do reference-video at all:

  * `comfy upload` posts everything to `/upload/image` with no media-type
    routing, so uploads never show up in `LoadVideo`'s enum (comfy-cli #645).
  * More fundamentally, the WAN 3.0 R2V and MiniMax H3 *API templates* ship
    with no `LoadVideo` node, so there'd be nothing to bind an upload to even
    once #645 lands.

Comfy Router sidesteps both by taking the reference as a plain URL in a JSON
body — no graph, no node patching, no comfy-cli subprocess. That's why this is
a hand-written module rather than a manifest: there is no workflow to patch.

Shape of the thing
------------------
1. Trim the recording to a few seconds (providers reject long references).
2. Upload it to Comfy's asset store and mint a signed URL.
3. POST to Router with that URL as the reference.
4. Download the result into output/videos/.

Two request shapes exist and the model picks which one — see MODELS below.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from ._base import ModuleDef, new_output_path

# Assets live on cloud.comfy.org; the model routes live on api.comfy.org.
# This split is load-bearing — api.comfy.org/v2/assets and /api/assets both
# 404, which is an easy hour to lose.
ASSETS_BASE = "https://cloud.comfy.org/api/assets"
ROUTER_BASE = "https://api.comfy.org/v2/models"

# Router holds the connection open for the whole generation rather than
# returning a job id, so the read timeout has to cover a full run. Observed:
# WAN ~190s, H3 ~263s, Seedance ~304s.
GEN_TIMEOUT = httpx.Timeout(900.0, connect=15.0)

# Providers reject long references. 5s is verified working on all three
# models; wan2.6-r2v documents a 5s cap for a single clip, so don't raise
# this default without testing the model you care about.
DEFAULT_REF_SECONDS = 5
MAX_REF_SECONDS = 10


# Each model takes one of two request shapes:
#
#   "media"   — WAN: a flat media[] array alongside a prompt.
#   "content" — MiniMax / Seedance: a typed content[] array where each part
#               carries a `role` saying what the media is *for*.
#
# `resolutions` maps our shared UI values onto whatever string the model
# actually wants, because they disagree about casing ("720P" vs "720p") and
# about which tiers exist at all.
MODELS: dict[str, dict[str, Any]] = {
    "wan3": {
        "id": "wan/wan3.0-video",
        "label": "WAN 3.0",
        "shape": "media",
        "resolutions": {"720p": "720P", "1080p": "1080P"},
        # WAN reports how much reference it consumed, which is the cleanest
        # confirmation available that the clip wasn't silently dropped.
        "ingest_key": "input_video_duration",
    },
    "h3": {
        "id": "minimax/minimax-h3",
        "label": "MiniMax H3",
        "shape": "content",
        "resolutions": {"720p": "768P", "1080p": "2K"},
        "ingest_key": "input_seconds",
    },
    "seedance": {
        "id": "byteplus/dreamina-seedance-2-0-260128",
        "label": "Seedance 2.0",
        "shape": "content",
        "resolutions": {"720p": "720p", "1080p": "1080p"},
        # Seedance's usage block carries only token counts — there's no field
        # that proves the reference was read. It IS fetched (a bad URL fails
        # with 400 "resource download failed"), we just can't assert it from
        # a successful response.
        "ingest_key": None,
    },
}


def _api_key() -> str:
    """Same key the rest of the app uses — don't invent a new env name.

    main.py backfills COMFY_CLOUD_API_KEY from COMFY_API_KEY at startup, so
    either may be the populated one depending on how Settings was used.
    """
    for name in ("COMFY_API_KEY", "COMFY_CLOUD_API_KEY"):
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    raise ValueError(
        "No Comfy API key. Open Settings and sign in with Comfy Cloud first."
    )


def _ffmpeg() -> str:
    """Resolve ffmpeg the same way main.py does: system first, bundled second.

    Kept local rather than imported from main to avoid a circular import —
    modules are loaded *by* main.
    """
    for cand in ("ffmpeg",):
        try:
            if subprocess.run([cand, "-version"], capture_output=True,
                              timeout=5).returncode == 0:
                return cand
        except Exception:
            pass
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        raise RuntimeError(f"ffmpeg not available for trimming the reference: {e}")


def _trim(src: Path, seconds: int, scratch: Path) -> Path:
    """Cut the head off the recording and downscale to 720p.

    Both halves matter: providers reject references over their duration cap,
    and our raw recordings can be 4K/70s — a 68MB upload for a 5s reference
    is pure latency.
    """
    dst = scratch / f"ref_{int(time.time())}.mp4"
    cmd = [
        _ffmpeg(), "-y", "-i", str(src),
        "-t", str(seconds),
        "-vf", "scale=-2:720",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-an",  # audio would only count against the upload size
        str(dst),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if p.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg failed to trim the reference clip: {p.stderr[-600:]}")
    return dst


def _upload_asset(client: httpx.Client, key: str, clip: Path) -> str:
    """Upload the clip and return a signed URL the provider can fetch.

    Two steps, and the second is the subtle one: the asset's /content route
    answers with a 302 whose Location is a storage.googleapis.com signed URL.
    That redirect target is readable *without* credentials, which is exactly
    what we need — the upstream provider fetches it knowing nothing about us.
    Following the redirect here would just download the bytes back to us.
    """
    with clip.open("rb") as fh:
        r = client.post(
            ASSETS_BASE,
            headers={"X-API-Key": key},
            files={"file": (clip.name, fh, "video/mp4")},
            data={"name": clip.name, "mime_type": "video/mp4", "tags": "input"},
        )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"asset upload failed ({r.status_code}): {r.text[:400]}")
    asset_id = r.json().get("id")
    if not asset_id:
        raise RuntimeError(f"asset upload returned no id: {r.text[:400]}")

    r2 = client.get(
        f"{ASSETS_BASE}/{asset_id}/content",
        headers={"X-API-Key": key},
        follow_redirects=False,
    )
    loc = r2.headers.get("location")
    if r2.status_code not in (301, 302, 303, 307, 308) or not loc:
        raise RuntimeError(
            f"couldn't mint a signed URL for the reference "
            f"(HTTP {r2.status_code}): {r2.text[:300]}"
        )
    return loc


def _build_payload(spec: dict, prompt: str, ref_url: str,
                   resolution: str, duration: int) -> dict:
    """Translate one set of user intentions into whichever shape the model wants."""
    res = spec["resolutions"].get(resolution) or next(iter(spec["resolutions"].values()))
    if spec["shape"] == "media":
        return {
            "input": {"prompt": prompt,
                      "media": [{"type": "reference_video", "url": ref_url}]},
            "parameters": {"resolution": res, "duration": duration},
        }
    return {
        "content": [
            {"type": "text", "text": prompt},
            {"type": "video_url", "video_url": {"url": ref_url},
             "role": "reference_video"},
        ],
        "duration": duration,
        "ratio": "16:9",
        "resolution": res,
    }


def _usage(body: dict) -> dict:
    """Find the usage block, which sits at different depths per model.

    WAN and Seedance put it at the top level; H3 nests it inside `task`
    alongside the result. Checking only the top level made H3 look like it
    was ignoring the reference when it wasn't.
    """
    top = body.get("usage")
    if isinstance(top, dict) and top:
        return top
    task = body.get("task")
    if isinstance(task, dict):
        nested = task.get("usage")
        if isinstance(nested, dict):
            return nested
    return {}


def _result_url(body: dict) -> str | None:
    """Dig the output video URL out of whichever envelope came back.

    WAN     -> output.video_url
    Seedance-> content.video_url
    H3      -> task.content.url
    """
    out = body.get("output") or {}
    if isinstance(out, dict) and out.get("video_url"):
        return out["video_url"]
    content = body.get("content") or {}
    if isinstance(content, dict) and content.get("video_url"):
        return content["video_url"]
    task = body.get("task") or {}
    if isinstance(task, dict):
        tc = task.get("content") or {}
        if isinstance(tc, dict) and tc.get("url"):
            return tc["url"]
    return None


async def run(
    *,
    prompt: str,
    model: str = "wan3",
    resolution: str = "720p",
    duration: int = 5,
    reference_seconds: int = DEFAULT_REF_SECONDS,
    video_path: Path | None = None,
    data_dir: Path,
    status_cb: Callable[..., None] | None = None,
    **_: Any,
) -> dict:
    import asyncio

    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("A prompt is required — describe what the reference should become.")
    spec = MODELS.get(model)
    if not spec:
        raise ValueError(f"Unknown model '{model}'. Pick one of: {', '.join(MODELS)}")
    if not video_path or not Path(video_path).exists():
        # Plain ASCII on purpose: this string can reach a Windows console via
        # the run-status detail, and cp1252 can't encode glyphs like the
        # record dot or an arrow.
        raise ValueError(
            "No reference video. Record a camera move with the transport's "
            "record button first, or drop a clip into the Scene background."
        )

    duration = int(duration or 5)
    reference_seconds = max(2, min(int(reference_seconds or DEFAULT_REF_SECONDS),
                                   MAX_REF_SECONDS))

    def emit(phase: str, **extra: Any) -> None:
        if status_cb:
            try:
                status_cb(phase, **extra)
            except Exception:
                pass  # progress reporting must never sink a run

    # Everything up to here is cheap validation; the rest blocks on network
    # and ffmpeg, so push it off the event loop.
    def _work() -> dict:
        key = _api_key()
        scratch = Path(data_dir) / ".scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        clip = _trim(Path(video_path), reference_seconds, scratch)
        try:
            with httpx.Client(timeout=GEN_TIMEOUT) as client:
                ref_url = _upload_asset(client, key, clip)
                emit("generating", model=spec["id"])

                r = client.post(
                    f"{ROUTER_BASE}/{spec['id']}",
                    headers={"X-API-Key": key, "Content-Type": "application/json"},
                    json=_build_payload(spec, prompt, ref_url, resolution, duration),
                )
                if r.status_code == 400:
                    # Router surfaces the provider's own complaint here, which
                    # is usually specific and worth showing verbatim.
                    detail = ""
                    try:
                        j = r.json()
                        detail = j.get("upstream_detail") or j.get("detail") or ""
                    except Exception:
                        detail = r.text[:400]
                    raise ValueError(f"{spec['label']} rejected the request: {detail}")
                if r.status_code != 200:
                    raise RuntimeError(
                        f"{spec['label']} failed (HTTP {r.status_code}): {r.text[:400]}"
                    )

                body = r.json()
                video_url = _result_url(body)
                if not video_url:
                    raise RuntimeError(
                        f"{spec['label']} returned no video URL: {str(body)[:400]}"
                    )

                # Sanity-check that the reference was actually consumed, where
                # the model tells us. Seedance doesn't, so this stays advisory.
                ikey = spec.get("ingest_key")
                if ikey:
                    used = _usage(body).get(ikey)
                    if not used:
                        print(f"[router-r2v] warning: {spec['id']} reported no "
                              f"{ikey} — the reference may have been ignored")

                emit("fetching")
                # The result URL is short-lived and signed; fetch it now.
                dl = client.get(video_url, follow_redirects=True)
                if dl.status_code != 200:
                    raise RuntimeError(
                        f"couldn't download the result (HTTP {dl.status_code}) — "
                        "the signed URL may have expired"
                    )
                out = new_output_path(Path(data_dir), "router_r2v", "mp4")
                out.write_bytes(dl.content)
                return {"path": str(out), "filename": out.name, "ext": "mp4"}
        finally:
            try:
                clip.unlink(missing_ok=True)
            except Exception:
                pass

    return await asyncio.to_thread(_work)


MODULE = ModuleDef(
    id="router-r2v",
    # ASCII-only: labels get printed to the console on Windows, where cp1252
    # can't encode an arrow glyph.
    label="Reference to Video (Router)",
    kind="video",
    output_ext="mp4",
    run=run,
    inputs=[
        # Declaring this as scene-video is what makes main.py resolve the
        # transport recording (or the Scene BG video) into video_path for us.
        {"name": "video", "type": "scene-video", "label": "Reference Video",
         "required": True},
        {"name": "prompt", "type": "textarea", "label": "Prompt", "required": True,
         "placeholder": "What the reference should become - e.g. 'cinematic "
                        "live-action, photoreal, match the motion and camera exactly'."},
        {"name": "model", "type": "dropdown", "label": "Model", "default": "wan3",
         "options": list(MODELS.keys())},
        {"name": "resolution", "type": "dropdown", "label": "Resolution",
         "default": "720p", "options": ["720p", "1080p"], "row": "a"},
        {"name": "duration", "type": "number", "label": "Duration (s)",
         "default": 5, "min": 2, "max": 10, "step": 1, "row": "a"},
        {"name": "reference_seconds", "type": "number", "label": "Reference (s)",
         "default": DEFAULT_REF_SECONDS, "min": 2, "max": MAX_REF_SECONDS, "step": 1,
         "row": "a"},
    ],
)

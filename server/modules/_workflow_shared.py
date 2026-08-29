"""Manifest-driven workflow modules.

Drop `<name>.json` (a ComfyUI workflow) plus `<name>.meta.json` (a manifest) into
`server/workflows/` and the loader synthesizes a ModuleDef with `source="workflow"`.
The manifest tells the runner which node widget each UI input maps to; the runner
handles submit → poll → download → move-into-data/ the same way `_tripo_shared`
does for the hand-written Tripo modules.

Manifest shape:
    {
      "id": "flux2_klein_t2i",
      "label": "Flux.2 Klein — Text to Image",
      "kind": "image",              // image | video | 3d | audio
      "output_ext": "png",          // string, or list like ["png","jpg"]
      "inputs": [
        { "name": "prompt", "type": "textarea", "required": true,
          "patch": {"node_id": 76, "widget_index": 0} },
        { "name": "image",  "type": "scene-image", "required": true,
          "patch": {"node_id": 10, "widget_index": 0} }
      ]
    }

The `patch` field never reaches the frontend — it's stripped from the ModuleDef's
inputs list before the /api/modules response, so the UI only sees the shape it
already knows (name/type/required/placeholder/label/help).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from ._base import ModuleDef, comfy_bin, new_output_path, run_cli
from ._tripo_shared import _parse_envelope, _extract_prompt_id, upload_image_to_cloud


WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / "workflows"
WHERE_CLOUD = ["--where", "cloud"]

# Fallback extensions per kind if the manifest doesn't spell them out.
_KIND_EXTS: dict[str, list[str]] = {
    "image": ["png", "jpg", "jpeg", "webp"],
    "video": ["mp4", "webm", "mov"],
    "3d":    ["glb", "gltf"],
    "audio": ["wav", "mp3", "flac", "ogg"],
}


def _is_api_format(workflow: dict) -> bool:
    """Detect Comfy Cloud's API/prompt format (flat dict keyed by node id string,
    each entry has class_type + inputs dict) vs the graph/save format (top-level
    nodes[] array with numeric ids + widgets_values list). API format is what
    Cloud's `Save (API Format)` toggle produces and what the runner canonicalizes
    to internally — simpler to author against because inputs are named, not
    positional. Graph format still supported for legacy workflows."""
    if "nodes" in workflow and isinstance(workflow["nodes"], list):
        return False
    # API format: every top-level key looks like a numeric node id + its value
    # is a dict with class_type.
    for k, v in workflow.items():
        if not isinstance(v, dict):
            continue
        if "class_type" in v:
            return True
    return False


def _find_node(workflow: dict, node_id: int) -> dict | None:
    # API format: node ids are string keys at the top level.
    if _is_api_format(workflow):
        return workflow.get(str(node_id))
    # Graph/save format: walk nodes[] matching numeric id.
    for n in workflow.get("nodes", []):
        if n.get("id") == node_id:
            return n
    return None


def _patch_widget(workflow: dict, node_id: int, widget_index: int, value: Any, widget_name: str | None = None) -> None:
    node = _find_node(workflow, node_id)
    if node is None:
        raise RuntimeError(f"manifest patch: node {node_id} not found in workflow")
    # API format: patch by named key in the node's `inputs` dict. widget_name
    # from the manifest is authoritative here (widget_index is meaningless for
    # a dict). Falls back to the raw widget_index string only if widget_name
    # wasn't provided — that's a legacy manifest and the caller should really
    # add widget_name.
    if _is_api_format(workflow):
        inputs = node.setdefault("inputs", {})
        key = widget_name if widget_name else str(widget_index)
        inputs[key] = value
        return
    # Graph/save format: widgets_values is positional.
    widgets = node.setdefault("widgets_values", [])
    while len(widgets) <= widget_index:
        widgets.append("")
    widgets[widget_index] = value


def _resolve_output_exts(manifest: dict) -> list[str]:
    raw = manifest.get("output_ext")
    if isinstance(raw, list):
        return [str(e).lower().lstrip(".") for e in raw if e]
    if isinstance(raw, str) and raw:
        return [raw.lower().lstrip(".")]
    return _KIND_EXTS.get(manifest.get("kind", "image"), ["png"])


def _resolve_extra_exts(manifest: dict) -> list[str]:
    # Secondary artifacts a workflow saves alongside its primary — e.g. SAM3D
    # Body writes a rendered .mp4 AND an animated .glb. Runner picks primary
    # by `output_ext`, but also copies each `extra_output_ext` into output/
    # so the Assets panel picks it up.
    raw = manifest.get("extra_output_ext")
    if isinstance(raw, list):
        return [str(e).lower().lstrip(".") for e in raw if e]
    if isinstance(raw, str) and raw:
        return [raw.lower().lstrip(".")]
    return []


async def _submit_wait_download(
    workflow: dict, module_id: str, output_exts: list[str], data_dir: Path,
    status_cb=None, extra_exts: list[str] | None = None,
) -> dict:
    """Submit → poll → download → move newest matching output into data_dir.

    Same three-stage pattern as `_tripo_shared.run_workflow_and_fetch_glb` — see
    the notes there for why we split submit from `jobs wait` (Cloud drops the
    long-lived HTTP stream mid-run on quiet workflows).

    `status_cb(phase, **extra)` is optional; when provided we emit
    "generating" (right after submit) and "fetching" (once jobs_wait returns)
    so the frontend can update its running-state hint mid-run instead of
    silently waiting for the whole flow to complete."""
    def _emit(phase: str, **extra):
        if status_cb:
            try:
                status_cb(phase, **extra)
            except Exception:
                pass  # never let a status hook take down a real run
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json.dump(workflow, tf)
        patched_path = Path(tf.name)

    try:
        code, out, err = await run_cli([
            comfy_bin(), "--json", "run",
            "--workflow", str(patched_path),
            *WHERE_CLOUD,
        ], timeout=300)
        env = _parse_envelope(out)
        if code != 0 or not env or not env.get("ok"):
            detail = (env or {}).get("error") if env else None
            raise RuntimeError(f"comfy run failed (rc={code}): {detail or err.strip() or out.strip()[:800]}")

        prompt_id = ((env.get("data") or {}).get("prompt_id")) or _extract_prompt_id(out)
        if not prompt_id:
            raise RuntimeError(f"couldn't parse prompt_id from `comfy run`. First 500 chars: {out[:500]}")
        _emit("generating", prompt_id=prompt_id)

        # Job started NOW — anything freshly landing in output/ from this point
        # forward is our artifact. 30s pad for clock drift. Kept here so the
        # parallel output-poller below has a floor to compare mtimes against.
        job_start = time.time() - 30
        scratch = Path(tempfile.mkdtemp(prefix=f"{module_id}_"))

        # Race `comfy jobs wait` against a local output/ poller. The Comfy CLI
        # auto-syncs cloud artifacts to output/ DURING jobs wait — but a hung
        # or misbehaving jobs-wait process (seen with some cloud partner nodes
        # when the API's job-status endpoint stalls) would otherwise keep the
        # coroutine blocked indefinitely, even after the file is already sitting
        # in output/. This gives whichever finishes first the win.
        # User-input + already-consumed filenames that live in data_dir
        # alongside real outputs. Excluded from output scans — otherwise:
        #   - `node_<id>_image.png` (viewport snapshot from /save_image)
        #     gets picked up when a job fails right after autoSnapshot ran,
        #     shutil.move'd into `out_<module>_<ts>.png`, and returned as
        #     the "AI render" — user sees their blockout, no error raised.
        #   - `out_blockout_<id>_<ts>.png` (versioned blockout archive from
        #     the Blockout tab click) hits the same failure mode.
        #   - Any prior `out_<module>_<ts>.png` from a recent successful run
        #     could be re-harvested if the current job fails within the mtime
        #     window (typically 30s).
        # CLI-synced real outputs use the workflow's SaveImage filename_prefix
        # (e.g. `Seedream5.0_Pro_image_edit_00001.png`), never `out_*` — our
        # own naming convention only appears on files we've already claimed.
        def _is_output_candidate(p: Path) -> bool:
            n = p.name
            if n.startswith("node_"): return False       # viewport snapshots + raw uploads
            if n.startswith("chat_paste_"): return False # chat-pasted refs
            if n.startswith("out_"): return False        # already-consumed outputs / blockout archives
            return True

        # Widen scans to include secondary artifacts (SAM3D: .glb next to .mp4).
        # Only affects file discovery — primary selection still uses `output_exts`.
        _extra_exts_norm = [e for e in (extra_exts or []) if e and e not in output_exts]
        scan_exts = list(output_exts) + _extra_exts_norm

        async def _wait_for_output_file() -> str:
            while True:
                for ext in scan_exts:
                    for p in data_dir.rglob(f"*.{ext}"):
                        if not _is_output_candidate(p):
                            continue
                        try:
                            if p.stat().st_mtime >= job_start:
                                return "output_synced"
                        except OSError:
                            continue
                await asyncio.sleep(5)

        async def _run_jobs_wait() -> str:
            code_, out_, err_ = await run_cli([
                comfy_bin(), "--json", "jobs", "wait", prompt_id,
                "--poll-interval", "5",
                "--timeout", "1500",
                *WHERE_CLOUD,
            ], timeout=1600)
            env_ = _parse_envelope(out_)
            if code_ != 0 or not env_ or not env_.get("ok"):
                # `jobs wait` returns only a job-count summary; the actual
                # per-node execution traceback lives in `jobs status`. Fetch
                # it so the raised error surfaces the real cause (missing
                # custom node, API 4xx, bad widget value, etc.) instead of
                # the useless "0/1 completed — 1 failed" summary.
                status_detail = ""
                try:
                    s_code, s_out, s_err = await run_cli([
                        comfy_bin(), "--json", "jobs", "status", prompt_id,
                        *WHERE_CLOUD,
                    ], timeout=30)
                    s_env = _parse_envelope(s_out)
                    if s_env and isinstance(s_env.get("data"), dict):
                        # Pull whatever error fields the status endpoint returns
                        # — schema varies across CLI versions so cast a wide net.
                        d = s_env["data"]
                        for key in ("error", "exception", "node_errors",
                                    "traceback", "message", "details"):
                            if d.get(key):
                                status_detail = f"{key}: {d[key]}"
                                break
                        if not status_detail:
                            # Fall back to the whole data blob if nothing named.
                            import json as _json
                            status_detail = _json.dumps(d)[:2000]
                    elif s_env and s_env.get("error"):
                        status_detail = str(s_env["error"])[:2000]
                    else:
                        status_detail = (s_err.strip() or s_out.strip())[:2000]
                except Exception as _e:
                    status_detail = f"(status-fetch failed: {_e})"
                summary = (env_ or {}).get("error") if env_ else None
                raise RuntimeError(
                    f"comfy jobs wait failed (rc={code_}): {summary}\n"
                    f"— job details: {status_detail}"
                )
            return "jobs_wait_ok"

        wait_task = asyncio.create_task(_run_jobs_wait())
        poll_task = asyncio.create_task(_wait_for_output_file())
        done, pending = await asyncio.wait(
            {wait_task, poll_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        # If jobs_wait raised, surface it — bad workflow / auth / etc.
        for t in done:
            exc = t.exception()
            if exc is not None:
                raise exc
        # Cloud has completed the job (or the file already appeared on disk).
        # Flip the frontend hint from "generating" to "fetching" so the user
        # sees Cloud finished even if the download loop drags on for minutes.
        _emit("fetching", prompt_id=prompt_id)
        # Either the CLI finished waiting OR the poller spotted a fresh file
        # on disk — both proceed the same way, running the pre-scan and (if
        # needed) the download retry loop. The parallel race is just a hedge
        # against a hung jobs-wait; we never skip the download attempt.

        # Helper: scan local output/ for anything freshly landed by
        # `jobs wait`'s auto-sync — Comfy CLI often drops the file there
        # before `comfy download` acknowledges any output exists. Short-
        # circuits the retry loop entirely when it hits, which is common
        # for cloud partner nodes that complete before their asset store
        # finalizes.
        # Cloud CLI has been observed syncing partner-3D outputs to the
        # local Comfy Desktop shared output folder (H:\Comfy-Desktop\
        # ComfyUI-Shared\output\3d) rather than the app's data_dir. Include
        # those known locations in the scan so a Rodin / Tripo / Meshy run
        # can be picked up wherever the CLI decides to drop it. First
        # existing candidate wins; missing dirs are silently skipped.
        _EXTRA_SCAN_ROOTS: list[Path] = [
            Path(r"H:\Comfy-Desktop\ComfyUI-Shared\output"),
            Path(r"H:\ComfyUI-Easy-Install\ComfyUI\output"),
            Path(r"H:\Comfy-Desktop\ComfyUI-Installs\ComfyDesktop\ComfyUI\output"),
        ]

        def _scan_output_dir() -> list[Path]:
            found: list[Path] = []
            roots = [data_dir] + [p for p in _EXTRA_SCAN_ROOTS if p.exists()]
            for root in roots:
                for ext in scan_exts:
                    try:
                        for p in root.rglob(f"*.{ext}"):
                            if not _is_output_candidate(p):
                                continue
                            try:
                                if p.stat().st_mtime >= job_start:
                                    found.append(p)
                            except OSError:
                                continue
                    except OSError:
                        continue
            return found

        # Meshy + other partner-API 3D nodes fire the job-success signal from
        # the Cloud side BEFORE their asset store finishes indexing the file —
        # `comfy download` hits `download_no_outputs` for 30-60s after the job
        # completes. Pre-sleep 15s to skip the first wave of doomed retries,
        # then a longer patience budget (20 attempts × up to 30s = ~5min max).
        candidates: list[Path] = _scan_output_dir()
        last_env = None
        if not candidates:
            await asyncio.sleep(15)
            candidates = _scan_output_dir()
        if not candidates:
            for attempt in range(20):
                code, out, err = await run_cli([
                    comfy_bin(), "--json", "download", prompt_id,
                    "-o", str(scratch),
                    *WHERE_CLOUD,
                ], timeout=300)
                last_env = _parse_envelope(out)
                if code == 0 and last_env and last_env.get("ok"):
                    break
                # Even when download says "no_outputs", the fallback might
                # already have the file — re-check between retries so a
                # late-arriving `jobs wait` sync short-circuits us out.
                fresh = _scan_output_dir()
                if fresh:
                    candidates = fresh
                    break
                err_code = ((last_env or {}).get("error") or {}).get("code")
                if err_code != "download_no_outputs":
                    detail = (last_env or {}).get("error") if last_env else None
                    raise RuntimeError(f"comfy download failed (rc={code}): {detail or err.strip() or out.strip()[:800]}")
                await asyncio.sleep(min(5 + attempt * 3, 30))

        # Look under scratch first (what `comfy download -o` was told to use).
        if not candidates:
            for ext in scan_exts:
                candidates.extend(scratch.rglob(f"*.{ext}"))
        # Final fallback: scan output/ once more in case the download call
        # succeeded but wrote directly to output/ instead of scratch.
        if not candidates:
            candidates = _scan_output_dir()
        if not candidates:
            # Cloud finished the job (jobs_wait returned ok) but no artifact
            # ever landed. Common with partner-3D nodes whose files stay in
            # Cloud's Media Assets and never populate job.outputs. Emit a
            # distinct phase so the frontend can show "Generated in Cloud —
            # not retrievable" rather than a generic failure.
            _emit("cloud_done_no_download", prompt_id=prompt_id)
            # Diagnostic — what DID land in scratch? If comfy download said
            # ok=true, the file may just have a different extension than the
            # manifest declared (Rodin/Tripo return signed URLs the CLI may
            # save as .bin/.tmp, or the partner ships gltf instead of glb).
            scratch_contents = []
            try:
                for p in scratch.rglob("*"):
                    if p.is_file():
                        scratch_contents.append(f"{p.relative_to(scratch)} ({p.stat().st_size}B)")
            except OSError:
                pass
            listing = ", ".join(scratch_contents[:20]) if scratch_contents else "(empty)"
            print(f"[cb-app] {module_id}: scratch contents = {listing}")
            raise RuntimeError(
                f"no {output_exts} output found. scratch had: {listing}. "
                f"data_dir scan since {job_start:.0f} also empty."
            )
        # Bias primary pick to the manifest-declared output_exts — with extras
        # in scope, the newest file could be a .glb when we want the .mp4.
        primary_candidates = [
            p for p in candidates
            if p.suffix.lstrip(".").lower() in output_exts
        ] or candidates  # empty primary → fall back so we still return SOMETHING
        src = max(primary_candidates, key=lambda p: p.stat().st_mtime)
        ext = src.suffix.lstrip(".").lower()
        dst = new_output_path(data_dir, module_id, ext)
        # If the source lives inside our own scratch or data_dir, move it
        # (it's ours to consume). If it came from an external shared output
        # (Comfy Desktop), copy so we don't rip the file out from under
        # another install that might want to keep its history.
        def _is_ours(p: Path) -> bool:
            for own in (scratch, data_dir):
                try:
                    p.relative_to(own)
                    return True
                except ValueError:
                    continue
            return False
        if _is_ours(src):
            shutil.move(str(src), str(dst))
        else:
            shutil.copy(str(src), str(dst))
        # Copy any secondary artifacts into output/ so the Assets panel surfaces
        # them. Best-effort: a failure here shouldn't sink the whole run.
        for extra in _extra_exts_norm:
            extra_pool = [
                p for p in candidates
                if p.suffix.lstrip(".").lower() == extra and p.exists()
            ]
            if not extra_pool:
                continue
            extra_src = max(extra_pool, key=lambda p: p.stat().st_mtime)
            try:
                extra_dst = new_output_path(data_dir, module_id, extra)
                if _is_ours(extra_src):
                    shutil.move(str(extra_src), str(extra_dst))
                else:
                    shutil.copy(str(extra_src), str(extra_dst))
            except Exception as _e:
                print(f"[cb-app] {module_id}: extra output copy failed ({extra}): {_e}")
        return {"path": str(dst), "filename": dst.name, "ext": ext}
    finally:
        try:
            patched_path.unlink()
        except OSError:
            pass


def _make_run(workflow_path: Path, manifest: dict):
    """Build the per-module `run` coroutine. Reloads the workflow JSON on every
    call so workflow authors can iterate on the graph without restarting the
    server (manifest changes still need a restart, since it's captured here).

    Dispatches on `manifest.runner` — but a preset can override with its own
    `runner` field, letting one tool tile mix local + cloud presets (e.g.
    Utility hosts Lotus Depth locally alongside Depth Anything 3 in the cloud).

    Runner semantics:
      - "cloud": submit via `comfy run --where cloud`, poll, download
      - "local": POST to 127.0.0.1:8188 /prompt, poll /history, download /view
        via `_workflow_local.run_local_workflow`. Requires the API-format
        workflow; local runner raises a clear error otherwise."""
    module_id = manifest["id"]
    runner_kind = (manifest.get("runner") or "cloud").lower()
    output_exts = _resolve_output_exts(manifest)

    def _resolve_preset_context(kwargs: dict) -> tuple[Path, dict, dict | None]:
        """Peel `preset` + `mode` off kwargs and translate them into an
        effective (workflow_path, manifest, active_preset) triple. Used by
        BOTH run_cloud and run_local so a single tile can host presets that
        target different workflow JSONs (with different node ids) under the
        same input schema.

        Preset override rules:
          - `preset.workflow` (or `image_workflow` when ui_mode == "image")
            picks a different JSON on disk
          - `preset.patches` / `image_patches` remaps top-level input `name`s
            to per-preset patch targets. Value `null` skips that input
            entirely (target workflow doesn't have the widget).
          - `image_output_ext` on the manifest pins image-mode output format
            so a video-oriented default (mp4) doesn't misname a PNG output.
        """
        preset_id = kwargs.pop("preset", None)
        ui_mode = (kwargs.pop("mode", None) or "").strip().lower() or None
        base_path = workflow_path
        active_preset = None
        presets = manifest.get("presets") or []
        if presets and preset_id:
            active_preset = next((p for p in presets if p.get("id") == preset_id), None)
            if active_preset is None:
                raise ValueError(f"unknown preset '{preset_id}' for {manifest['id']}")
            if active_preset.get("coming_soon"):
                raise ValueError(f"preset '{active_preset.get('label') or preset_id}' isn't wired up yet")
            preset_stem = (
                active_preset.get("image_workflow")
                if ui_mode == "image" and active_preset.get("image_workflow")
                else active_preset.get("workflow")
            )
            if preset_stem and preset_stem != workflow_path.stem:
                candidate = workflow_path.parent / f"{preset_stem}.json"
                if not candidate.exists():
                    raise RuntimeError(f"preset workflow missing on disk: {candidate.name}")
                base_path = candidate
        overrides = None
        if active_preset:
            if ui_mode == "image" and isinstance(active_preset.get("image_patches"), dict):
                overrides = active_preset["image_patches"]
            elif isinstance(active_preset.get("patches"), dict):
                overrides = active_preset["patches"]
        effective_manifest = manifest
        if overrides or ui_mode == "image":
            new_inputs = []
            for spec in manifest.get("inputs", []):
                new_spec = dict(spec)
                if ui_mode == "image" and new_spec.get("type") == "scene-video":
                    new_spec["type"] = "scene-image"
                if overrides and new_spec.get("name") in overrides:
                    ov = overrides[new_spec["name"]]
                    new_spec["patch"] = ov  # None → runner sees no patch → skip
                new_inputs.append(new_spec)
            effective_manifest = {**manifest, "inputs": new_inputs}
        if ui_mode == "image":
            image_ext = manifest.get("image_output_ext", "png")
            effective_manifest = {**effective_manifest, "output_ext": image_ext, "kind": "image"}
        return base_path, effective_manifest, active_preset

    async def run_cloud(**kwargs):
        data_dir = kwargs.pop("data_dir")
        base_path, effective_manifest, _preset = _resolve_preset_context(kwargs)
        workflow = json.loads(base_path.read_text(encoding="utf-8"))
        module_id_actual = effective_manifest.get("id", module_id)
        output_exts_actual = _resolve_output_exts(effective_manifest)
        for spec in effective_manifest.get("inputs", []):
            patch = spec.get("patch")
            if not patch:
                continue
            node_id = int(patch["node_id"])
            widget_index = int(patch.get("widget_index", 0))
            # widget_name is the API-format patch key (Cloud names its inputs)
            # — required for API-format workflows, harmless for graph format.
            widget_name = patch.get("widget_name")
            input_type = spec.get("type", "text")

            if input_type == "scene-image":
                image_path = kwargs.get("image_path")
                if not image_path:
                    if spec.get("required", True):
                        raise ValueError(f"{spec['name']} is required")
                    continue
                cloud_name = await upload_image_to_cloud(Path(image_path))
                _patch_widget(workflow, node_id, widget_index, cloud_name, widget_name)
            elif input_type == "scene-video":
                # `comfy upload` is not image-specific — same call handles video
                # files for LoadVideo widgets. Cloud returns a hashed filename
                # the LoadVideo node references from Cloud's input/ dir.
                video_path = kwargs.get("video_path")
                if not video_path:
                    if spec.get("required", True):
                        raise ValueError(f"{spec['name']} is required")
                    continue
                cloud_name = await upload_image_to_cloud(Path(video_path))
                _patch_widget(workflow, node_id, widget_index, cloud_name, widget_name)
            else:
                value = kwargs.get(spec["name"])
                if value is None or (isinstance(value, str) and not value.strip()):
                    if spec.get("required"):
                        raise ValueError(f"{spec['name']} is required")
                    continue
                # Per-patch value translation — same shape as local runner.
                # Lets one UI knob write different values into different
                # widgets (e.g. a "Lightning" dropdown mapping "Off" to
                # strength=0 on one patch while picking a lora on another).
                value_map = patch.get("value_map") if isinstance(patch, dict) else None
                if isinstance(value_map, dict) and str(value) in value_map:
                    value = value_map[str(value)]
                # Coerce numeric widget types — the frontend serializes them
                # as strings ("42", "0.8"). Cloud partner nodes schema-check
                # KSampler.seed as INT and ControlNet.strength as FLOAT, so
                # a string sail-through triggers shape_mismatch on the API
                # side. Mirrors the same coercion in _workflow_local.py.
                if input_type in ("seed", "number") and isinstance(value, str):
                    s = value.strip()
                    try:
                        value = int(s) if input_type == "seed" else float(s)
                    except ValueError:
                        try:
                            value = float(s)
                        except ValueError:
                            pass  # leave as string; the widget may actually want a string
                _patch_widget(workflow, node_id, widget_index, value, widget_name)

        extra_exts_actual = _resolve_extra_exts(effective_manifest)
        return await _submit_wait_download(
            workflow, module_id_actual, output_exts_actual, data_dir,
            extra_exts=extra_exts_actual,
        )

    async def run_local(**kwargs):
        from ._workflow_local import run_local_workflow
        data_dir = kwargs.pop("data_dir")
        base_path, effective_manifest, _preset = _resolve_preset_context(kwargs)
        # Prefer an API-format sidecar over the raw imported JSON. Two naming
        # conventions:
        #   - `<stem>.local.json`  — written by the "prepare for local" agent flow
        #   - `<stem>_api.json`    — ComfyUI's default "Save (API Format)" naming,
        #                            so the user doesn't have to rename after export
        # First existing one wins. Falls back to the raw base_path (which
        # then errors gracefully in run_local_workflow if it's still GUI-format).
        parent = base_path.parent
        candidates = [
            parent / (base_path.stem + ".local.json"),
            parent / (base_path.stem + "_api.json"),
        ]
        target = next((p for p in candidates if p.exists()), base_path)
        return await run_local_workflow(target, effective_manifest, kwargs, data_dir)

    async def run(*, data_dir: Path, **kwargs):
        kwargs["data_dir"] = data_dir
        # Per-preset runner override — lets one tool tile mix local and cloud
        # presets (e.g. Utility hosts local Lotus + cloud Depth Anything 3).
        # We peek at the preset here WITHOUT consuming kwargs — the actual
        # preset handler downstream still needs to see them.
        preset_id = kwargs.get("preset")
        effective_runner = runner_kind
        if preset_id:
            preset = next((p for p in manifest.get("presets") or [] if p.get("id") == preset_id), None)
            if preset and preset.get("runner"):
                effective_runner = str(preset["runner"]).lower()
        if effective_runner == "local":
            return await run_local(**kwargs)
        return await run_cloud(**kwargs)

    return run


def _manifest_to_module_def(workflow_path: Path, manifest: dict) -> ModuleDef:
    # `patch` is server-only wiring — never sent to the frontend, which just
    # needs to render form inputs.
    ui_inputs = [{k: v for k, v in spec.items() if k != "patch"} for spec in manifest.get("inputs", [])]
    exts = _resolve_output_exts(manifest)
    presets = manifest.get("presets") or []
    if not isinstance(presets, list):
        presets = []
    return ModuleDef(
        id=manifest["id"],
        label=manifest.get("label", manifest["id"]),
        kind=manifest.get("kind", "image"),
        inputs=ui_inputs,
        output_ext=exts[0] if exts else "png",
        run=_make_run(workflow_path, manifest),
        source="workflow",
        util=bool(manifest.get("util", False)),
        icon=manifest.get("icon", "") or "",
        presets=presets,
    )


def register_manifest_by_name(workflow_stem: str) -> ModuleDef | None:
    """Load `<stem>.json` + `<stem>.meta.json` and return a synthetic ModuleDef.
    Used by the analyze endpoint to hot-register a module after saving a new
    manifest, without waiting for a server restart. Returns None if either file
    is missing or the manifest is invalid."""
    wf_path = WORKFLOWS_DIR / f"{workflow_stem}.json"
    meta_path = WORKFLOWS_DIR / f"{workflow_stem}.meta.json"
    if not wf_path.exists() or not meta_path.exists():
        return None
    manifest = json.loads(meta_path.read_text(encoding="utf-8"))
    if "id" not in manifest:
        return None
    return _manifest_to_module_def(wf_path, manifest)


def slim_workflow_for_analysis(workflow: dict) -> dict:
    """Strip presentation-only fields from a workflow JSON so Claude focuses on
    the structural bits (node type, mode, title, widget values). Removes pos/
    size/flags/order/properties/color and per-input link IDs — none of which
    matter for figuring out which widget holds the prompt."""
    slim_nodes = []
    for n in workflow.get("nodes", []):
        keep = {
            "id": n.get("id"),
            "type": n.get("type"),
            "mode": n.get("mode", 0),
        }
        if n.get("title"):
            keep["title"] = n["title"]
        wv = n.get("widgets_values")
        if wv is not None:
            keep["widgets_values"] = wv
        # Keep input names + type — helps identify what a subgraph node consumes
        # (Prompt, Image, Latent, etc.) without the noisy link_id/localized_name.
        ins = n.get("inputs")
        if ins:
            keep["inputs"] = [
                {"name": i.get("name"), "type": i.get("type")}
                for i in ins
            ]
        outs = n.get("outputs")
        if outs:
            keep["outputs"] = [
                {"name": o.get("name"), "type": o.get("type")}
                for o in outs
            ]
        slim_nodes.append(keep)
    return {"nodes": slim_nodes}


def discover_manifest_modules() -> list[ModuleDef]:
    """Scan server/workflows/ for `<name>.json` + `<name>.meta.json` pairs.
    Called after the Python-module load in main.py so a hand-written module
    wins on id collision (we skip conflicting manifests there, not here)."""
    if not WORKFLOWS_DIR.exists():
        return []
    out: list[ModuleDef] = []
    for meta_path in sorted(WORKFLOWS_DIR.glob("*.meta.json")):
        # `foo.meta.json` → workflow file is `foo.json` in the same dir.
        wf_name = meta_path.name[: -len(".meta.json")] + ".json"
        wf_path = meta_path.parent / wf_name
        if not wf_path.exists():
            print(f"[cb-app] manifest skipped ({meta_path.name}): matching {wf_name} not found")
            continue
        try:
            manifest = json.loads(meta_path.read_text(encoding="utf-8"))
            if "id" not in manifest:
                print(f"[cb-app] manifest skipped ({meta_path.name}): missing 'id'")
                continue
            out.append(_manifest_to_module_def(wf_path, manifest))
            print(f"[cb-app] loaded workflow module: {manifest['id']} <- {wf_name}")
        except Exception as e:
            print(f"[cb-app] FAILED to load manifest {meta_path.name}: {e}")
    return out

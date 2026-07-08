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


def _find_node(workflow: dict, node_id: int) -> dict | None:
    for n in workflow.get("nodes", []):
        if n.get("id") == node_id:
            return n
    return None


def _patch_widget(workflow: dict, node_id: int, widget_index: int, value: Any) -> None:
    node = _find_node(workflow, node_id)
    if node is None:
        raise RuntimeError(f"manifest patch: node {node_id} not found in workflow")
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


async def _submit_wait_download(
    workflow: dict, module_id: str, output_exts: list[str], data_dir: Path,
) -> dict:
    """Submit → poll → download → move newest matching output into data_dir.

    Same three-stage pattern as `_tripo_shared.run_workflow_and_fetch_glb` — see
    the notes there for why we split submit from `jobs wait` (Cloud drops the
    long-lived HTTP stream mid-run on quiet workflows)."""
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

        code, out, err = await run_cli([
            comfy_bin(), "--json", "jobs", "wait", prompt_id,
            "--poll-interval", "5",
            "--timeout", "1500",
            *WHERE_CLOUD,
        ], timeout=1600)
        env = _parse_envelope(out)
        if code != 0 or not env or not env.get("ok"):
            detail = (env or {}).get("error") if env else None
            raise RuntimeError(f"comfy jobs wait failed (rc={code}): {detail or err.strip() or out.strip()[:800]}")

        scratch = Path(tempfile.mkdtemp(prefix=f"{module_id}_"))
        last_env = None
        for attempt in range(8):
            code, out, err = await run_cli([
                comfy_bin(), "--json", "download", prompt_id,
                "-o", str(scratch),
                *WHERE_CLOUD,
            ], timeout=300)
            last_env = _parse_envelope(out)
            if code == 0 and last_env and last_env.get("ok"):
                break
            err_code = ((last_env or {}).get("error") or {}).get("code")
            if err_code != "download_no_outputs":
                detail = (last_env or {}).get("error") if last_env else None
                raise RuntimeError(f"comfy download failed (rc={code}): {detail or err.strip() or out.strip()[:800]}")
            await asyncio.sleep(min(2 + attempt * 2, 12))
        else:
            detail = (last_env or {}).get("error") if last_env else None
            raise RuntimeError(f"comfy download kept reporting no outputs after 8 tries: {detail}")

        candidates: list[Path] = []
        for ext in output_exts:
            candidates.extend(scratch.rglob(f"*.{ext}"))
        if not candidates:
            raise RuntimeError(f"no output matching {output_exts} found under {scratch}")
        src = max(candidates, key=lambda p: p.stat().st_mtime)
        ext = src.suffix.lstrip(".").lower()
        dst = new_output_path(data_dir, module_id, ext)
        shutil.move(str(src), str(dst))
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

    Dispatches on `manifest.runner`:
      - "cloud" (default): submit via `comfy run --where cloud`, poll, download
      - "local": POST to 127.0.0.1:8188 /prompt, poll /history, download /view
        via `_workflow_local.run_local_workflow`. Requires the API-format
        workflow; local runner raises a clear error otherwise."""
    module_id = manifest["id"]
    runner_kind = (manifest.get("runner") or "cloud").lower()
    output_exts = _resolve_output_exts(manifest)

    async def run_cloud(**kwargs):
        data_dir = kwargs.pop("data_dir")
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
        for spec in manifest.get("inputs", []):
            patch = spec.get("patch")
            if not patch:
                continue
            node_id = int(patch["node_id"])
            widget_index = int(patch.get("widget_index", 0))
            input_type = spec.get("type", "text")

            if input_type == "scene-image":
                image_path = kwargs.get("image_path")
                if not image_path:
                    if spec.get("required", True):
                        raise ValueError(f"{spec['name']} is required")
                    continue
                cloud_name = await upload_image_to_cloud(Path(image_path))
                _patch_widget(workflow, node_id, widget_index, cloud_name)
            else:
                value = kwargs.get(spec["name"])
                if value is None or (isinstance(value, str) and not value.strip()):
                    if spec.get("required"):
                        raise ValueError(f"{spec['name']} is required")
                    continue
                _patch_widget(workflow, node_id, widget_index, value)

        return await _submit_wait_download(workflow, module_id, output_exts, data_dir)

    async def run_local(**kwargs):
        from ._workflow_local import run_local_workflow
        data_dir = kwargs.pop("data_dir")
        # Preset routing — if the manifest declares a `presets` array and the
        # client passed `preset` in kwargs, look up the entry and use its
        # `workflow` stem to load a different JSON. Falls back to the manifest's
        # own workflow if the preset doesn't override. Coming-soon presets fail
        # fast with a clear message so the frontend can surface it.
        preset_id = kwargs.pop("preset", None)
        base_path = workflow_path
        active_preset = None
        presets = manifest.get("presets") or []
        if presets and preset_id:
            active_preset = next((p for p in presets if p.get("id") == preset_id), None)
            if active_preset is None:
                raise ValueError(f"unknown preset '{preset_id}' for {manifest['id']}")
            if active_preset.get("coming_soon"):
                raise ValueError(f"preset '{active_preset.get('label') or preset_id}' isn't wired up yet")
            preset_stem = active_preset.get("workflow")
            if preset_stem and preset_stem != workflow_path.stem:
                candidate = workflow_path.parent / f"{preset_stem}.json"
                if not candidate.exists():
                    raise RuntimeError(f"preset workflow missing on disk: {candidate.name}")
                base_path = candidate
        # Per-preset patch overrides — when the picked preset points at a
        # different workflow file its node ids will diverge from the default
        # workflow's, so the manifest's shared `inputs[i].patch` targets won't
        # apply cleanly. A preset's optional `patches` dict remaps by input
        # name → patch (or list of patches). We build an effective manifest
        # here so the downstream runner sees the correct targets without
        # having to know about presets at all.
        effective_manifest = manifest
        if active_preset and isinstance(active_preset.get("patches"), dict):
            overrides = active_preset["patches"]
            new_inputs = []
            for spec in manifest.get("inputs", []):
                if spec.get("name") in overrides:
                    new_inputs.append({**spec, "patch": overrides[spec["name"]]})
                else:
                    new_inputs.append(spec)
            effective_manifest = {**manifest, "inputs": new_inputs}
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
        if runner_kind == "local":
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

"""Shared plumbing for Tripo template modules.

Both Tripo H3.1 templates (image_to_model, text_to_model) submit a workflow JSON
to Comfy Cloud via `comfy run --where cloud`. They differ in what they patch
(LoadImage for image; TripoTextToModelNode for text) but share the run+download
half of the pipeline, which lives here.

Auth comes from COMFY_CLOUD_API_KEY (set by /api/auth/key in main.py, which mirrors
COMFY_API_KEY into both env vars).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from pathlib import Path

from ._base import comfy_bin, new_output_path, run_cli


WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / "workflows"
WHERE_CLOUD = ["--where", "cloud"]


def load_workflow(name: str) -> dict:
    """Read a shipped workflow JSON from server/workflows/."""
    path = WORKFLOWS_DIR / name
    if not path.exists():
        raise RuntimeError(f"workflow file missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def find_node(workflow: dict, node_id: int) -> dict | None:
    for n in workflow.get("nodes", []):
        if n.get("id") == node_id:
            return n
    return None


def _parse_envelope(text: str) -> dict | None:
    """`comfy --json <cmd>` prints one envelope object on stdout. Some commands
    (`run --json`) stream NDJSON with the envelope as the LAST line, so scan
    from the bottom for the first parseable JSON object."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("schema", "").startswith("envelope/"):
            return obj
    return None


def _extract_prompt_id(text: str) -> str | None:
    """`comfy run --json` streams NDJSON; the final envelope has data.prompt_id.
    We parse line-by-line + regex fallback for schema drift."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        data = ev.get("data") if isinstance(ev, dict) else None
        if isinstance(data, dict):
            pid = data.get("prompt_id") or data.get("id")
            if isinstance(pid, str):
                return pid
        pid = ev.get("prompt_id") if isinstance(ev, dict) else None
        if isinstance(pid, str):
            return pid
    m = re.search(r'"prompt_id"\s*:\s*"([^"]+)"', text)
    return m.group(1) if m else None


async def upload_image_to_cloud(image_path: Path) -> str:
    """Upload an image to Comfy Cloud's ComfyUI input/ dir. Returns the server-
    side filename that LoadImage widgets must reference (parsed from the
    envelope's `uploads[0].cloud_name` — Cloud hashes/renames uploads and does
    not use the client-side SHA256)."""
    if not image_path.exists():
        raise ValueError(f"image not found: {image_path}")
    code, out, err = await run_cli([
        comfy_bin(), "--json", "upload", str(image_path), *WHERE_CLOUD,
    ], timeout=180)
    env = _parse_envelope(out)
    if code != 0 or not env or not env.get("ok"):
        detail = (env or {}).get("error") if env else None
        raise RuntimeError(f"comfy upload failed (rc={code}): {detail or err.strip() or out.strip()}")
    uploads = ((env.get("data") or {}).get("uploads")) or []
    if not uploads or not uploads[0].get("cloud_name"):
        raise RuntimeError(f"upload envelope missing uploads[0].cloud_name: {env}")
    return uploads[0]["cloud_name"]


async def upload_file_to_cloud_with_subfolder(file_path: Path, subfolder: str = "") -> str:
    """Direct multipart POST to Cloud's /api/upload/image endpoint with an
    explicit `subfolder` form field. The `comfy upload` CLI shim hardcodes
    the endpoint but never sends subfolder, so any upload lands untagged in
    root input/ — LoadImage sees it, but LoadVideo's enum (which we believe
    filters by `subfolder=="video"` or an equivalent asset tag) does not.

    Auth reuses comfy-cli's session machinery: `resolve_target(where="cloud")`
    reads the persisted OAuth token / API key the same way `comfy upload` does,
    so if the CLI can talk to Cloud, so can this function. Returns the
    server-side filename the LoadVideo widget must reference.
    """
    if not file_path.exists():
        raise ValueError(f"file not found: {file_path}")

    def _post() -> str:
        # Imports live inside the sync body so the module import chain doesn't
        # fail if comfy-cli's internals shift between versions.
        import mimetypes
        import os as _os
        import urllib.error
        import urllib.request
        import uuid as _uuid
        from comfy_cli.target import resolve_target

        target = resolve_target(where="cloud")
        # Auth precedence mirrors comfy-cli's own target_auth_headers helper:
        # OAuth Bearer > X-API-Key > env-var fallback for the app's server
        # process (which sets COMFY_API_KEY / COMFY_CLOUD_API_KEY via
        # /api/auth/key at startup — see server main.py).
        auth_headers: dict[str, str] = {}
        auth_token = getattr(target, "auth_token", None)
        api_key = getattr(target, "api_key", None) or _os.environ.get("COMFY_API_KEY") or _os.environ.get("COMFY_CLOUD_API_KEY")
        if auth_token:
            auth_headers["Authorization"] = f"Bearer {auth_token}"
        elif api_key:
            auth_headers["X-API-Key"] = api_key
        else:
            raise RuntimeError(
                "Cloud auth not available — sign in with `comfy cloud login` "
                "or set COMFY_API_KEY. Same requirement as `comfy upload`."
            )
        url = target.url("upload/image")
        filename = file_path.name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        boundary = _uuid.uuid4().hex
        parts: list[bytes] = []
        def _field(name: str, value: str) -> None:
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            parts.append(f"{value}\r\n".encode())
        _field("overwrite", "true")
        _field("type", "input")
        if subfolder:
            _field("subfolder", subfolder)
        # File field — server code inspects the `image` form key regardless of
        # actual media type (see ComfyUI server.py `image_upload`).
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'.encode()
        )
        parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
        parts.append(file_path.read_bytes())
        parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        for hdr, val in auth_headers.items():
            req.add_header(hdr, val)
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            body_bytes = b""
            try:
                body_bytes = e.read()
            except Exception:
                pass
            raise RuntimeError(
                f"POST {url} failed: HTTP {e.code} — {body_bytes.decode('utf-8','replace')[:500]}"
            )
        name = data.get("name")
        if not name:
            raise RuntimeError(f"upload response missing 'name': {data}")
        # LoadVideo widget value is `{subfolder}/{filename}` when subfolder is set;
        # matches ComfyUI's folder_paths.get_annotated_filepath format. Bare
        # filename when we uploaded into root input/.
        sf = data.get("subfolder") or subfolder or ""
        return f"{sf}/{name}" if sf else name

    return await asyncio.to_thread(_post)


async def run_workflow_and_fetch_glb(workflow: dict, module_id: str, data_dir: Path,
                                      status_cb=None) -> dict:
    """Submit a patched workflow to Cloud, wait for it, download the .glb, and
    stash it under data_dir with the standard out_<module>_<ts>_<uuid>.glb name
    so it lands in the Assets pane. Returns the {path, filename, ext} envelope
    the module contract expects.

    Delegates to `_workflow_shared._submit_wait_download` — a straight
    sequential `run → jobs wait → download` used to work here but hangs on
    Tripo H3.1 partner-node jobs when `jobs wait` stalls at 99% (the CLI's
    urllib call drops mid-poll even though Cloud has finalized the job). The
    shared helper races `jobs wait` against a filesystem poller and short-
    circuits as soon as the file lands, matching the pattern already used by
    Rodin / Meshy / other partner nodes."""
    # Inline import — `_workflow_shared` imports names from this module at
    # module load, so a top-level import here would create a cycle.
    from ._workflow_shared import _submit_wait_download
    return await _submit_wait_download(workflow, module_id, ["glb", "gltf"], data_dir,
                                        status_cb=status_cb)

"""Shared module helpers. A module exports MODULE = ModuleDef(...) plus a `run` coroutine."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable


def comfy_bin() -> str:
    """Resolve the `comfy` console script — prefers the active venv's Scripts dir
    so subprocess calls work even when PATH doesn't include the venv."""
    venv_scripts = Path(sys.executable).parent
    for name in ("comfy.exe", "comfy.cmd", "comfy.bat", "comfy"):
        p = venv_scripts / name
        if p.exists():
            return str(p)
    return shutil.which("comfy") or "comfy"


@dataclass
class ModuleDef:
    id: str
    label: str
    kind: str
    inputs: list[dict[str, Any]] = field(default_factory=list)
    output_ext: str = "png"
    run: Callable[..., Awaitable[dict]] | None = None
    # "python" = hand-written module in server/modules/*.py.
    # "workflow" = synthesized at load time from a workflow JSON + .meta.json
    # manifest in server/workflows/. Frontend groups these into a separate
    # WORKFLOW section (parallel to GENERATE) so the two paths stay isolated
    # while the workflow-import flow is being tested.
    source: str = "python"


def _run_cli_sync(cmd: list[str], cwd: Path | None, timeout: int) -> tuple[int, str, str]:
    """Blocking subprocess runner. Use via run_cli() from async code; we route
    through a thread so this works on Windows where the asyncio selector loop
    doesn't support create_subprocess_exec (uvicorn --reload hits this)."""
    import os
    import subprocess
    env = os.environ.copy()
    # Force UTF-8 on the child's stdio. Comfy CLI's Rich renderer emits glyphs
    # (arrows, dashes, box-drawing) that crash Python's default cp1252 encoding
    # on Windows when stdout is piped instead of a TTY.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=False,
            cwd=str(cwd) if cwd else None,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out after {timeout}s: {shlex.join(cmd)}")
    out = r.stdout.decode(errors="replace") if r.stdout else ""
    err = r.stderr.decode(errors="replace") if r.stderr else ""
    rc = r.returncode if r.returncode is not None else 0
    if rc != 0:
        print(f"[cb-app] CLI failed (rc={rc}): {shlex.join(cmd)}")
        if err:
            print(f"[cb-app] stderr: {err[:1000]}")
        if out:
            print(f"[cb-app] stdout: {out[:1000]}")
    return rc, out, err


async def run_cli(cmd: list[str], cwd: Path | None = None, timeout: int = 600) -> tuple[int, str, str]:
    return await asyncio.to_thread(_run_cli_sync, cmd, cwd, timeout)


def new_output_path(data_dir: Path, module_id: str, ext: str) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return data_dir / f"out_{module_id}_{stamp}_{uuid.uuid4().hex[:6]}.{ext}"

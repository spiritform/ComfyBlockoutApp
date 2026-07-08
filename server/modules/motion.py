"""Motion — text-to-motion animation for the mannequin (AnimoFlow / MoMask).

Scaffold pass — surfaces a Tools-grid tile that opens a Motion properties pane.
The pane's job is to spell out exactly what the user needs installed to run the
tool, with live status dots + one-click "fix this" affordances (open Docker's
install page, pull the container, spawn a mannequin if none in scene).

Actual generation isn't wired yet — the pattern here is "requirements-first
disclosure": tools with heavy external deps say what they need up front instead
of throwing errors mid-run. Once the user's environment is green, the Generate
button will POST the prompt through to AnimoFlow.

Related endpoint: /api/motion/requirements in main.py checks Docker socket +
container image presence.
"""

from __future__ import annotations

from ._base import ModuleDef


async def run(**kwargs) -> dict:
    """Placeholder — real generation lands once we've picked a first container
    (MoMask is the current target — fast, modest VRAM footprint). For now the
    frontend's Generate button just refreshes the requirements card so the user
    can see the flow shape."""
    raise RuntimeError(
        "Motion generation isn't wired up yet — this scaffold surfaces the "
        "requirements panel so the flow is testable. Wire AnimoFlow's HTTP "
        "endpoint into this run() to enable real generation."
    )


MODULE = ModuleDef(
    id="motion",
    label="AnimoFlow",
    kind="3d",
    inputs=[],
    output_ext="glb",
    util=True,
    # Lucide `footprints` — reads as "moves" / motion capture. Distinct from
    # the mannequin's person-standing so the two tools don't collide visually.
    icon=(
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        '<path d="M4 16v-2.38C4 11.5 2.97 10.5 3 8c.03-2.72 1.49-6 4.5-6C9.37 2 10 3.8 10 5.5c0 3.11-2 5.66-2 8.68V16a2 2 0 1 1-4 0Z"/>'
        '<path d="M20 20v-2.38c0-2.12 1.03-3.12 1-5.62-.03-2.72-1.49-6-4.5-6C14.63 6 14 7.8 14 9.5c0 3.11 2 5.66 2 8.68V20a2 2 0 1 0 4 0Z"/>'
        '<path d="M16 17h4"/>'
        '<path d="M4 13h4"/>'
        '</svg>'
    ),
    run=run,
)

#!/usr/bin/env python3
"""Verify the source-controlled cluster integration; no in-place patching."""

from __future__ import annotations

from pathlib import Path
import sys


repo = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).parent.parent
checks = {
    repo / "roboaixrf" / "main.py": "cluster_nodes: int | str | Sequence[str]",
    repo / "python_code" / "roboai_xrf_frontend" / "components" / "run_page.py": (
        'options=("Single node", "Distributed")'
    ),
    repo / "cluster_jobs" / "ui_cluster_run.py": "def run_cluster(",
}

missing = []
for path, marker in checks.items():
    if not path.is_file() or marker not in path.read_text(encoding="utf-8"):
        missing.append(f"{path}: missing {marker!r}")

if missing:
    raise SystemExit(
        "Cluster integration is incomplete. Deploy the source-controlled files; "
        "the old five-node patch is intentionally no longer applied.\n"
        + "\n".join(missing)
    )

print("Cluster integration is present; no patching is required.")

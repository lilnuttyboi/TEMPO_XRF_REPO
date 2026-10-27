#!/usr/bin/env python3
"""Verify the source-controlled cluster integration; no in-place patching."""

from __future__ import annotations

from pathlib import Path
import sys


repo = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).parent.parent
checks = {
    repo / "roboaixrf" / "main.py": (
        "cluster_nodes: int | str | Sequence[str]",
        "cluster_chunks_per_node: int = 1",
    ),
    repo / "python_code" / "roboai_xrf_frontend" / "components" / "run_page.py": (
        'options=("Single node", "Distributed")',
        '"Queued chunks per node"',
    ),
    repo / "cluster_jobs" / "ui_cluster_run.py": (
        "def run_cluster(",
        '"max_concurrent_chunks"',
    ),
    repo / "cluster_jobs" / "cluster_config.py": (
        "def calculate_queue_shape(",
        'f"--array=0-{shard_count - 1}%{max_concurrent}"',
    ),
}

missing = []
for path, markers in checks.items():
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    for marker in markers:
        if marker not in text:
            missing.append(f"{path}: missing {marker!r}")

if missing:
    raise SystemExit(
        "Cluster integration is incomplete. Deploy the source-controlled files; "
        "the old five-node patch is intentionally no longer applied.\n"
        + "\n".join(missing)
    )

print("Cluster integration is present; no patching is required.")

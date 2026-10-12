#!/usr/bin/env python3
"""Exercise every selected Slurm node and verify distinct successful shards."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from cluster_jobs.cluster_config import discover_nodes, parse_node_list  # noqa: E402
from cluster_jobs.ui_cluster_run import run_cluster  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config_dir", type=Path)
    parser.add_argument("--nodes", help="Comma-separated nodes; defaults to discovered nodes.")
    parser.add_argument("--partition")
    parser.add_argument("--events-per-node", type=int, default=100)
    parser.add_argument("--workers", type=int, choices=(40, 80), default=40)
    parser.add_argument("--seed-base", type=int, default=246_810)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.events_per_node < 1:
        raise ValueError("--events-per-node must be at least 1.")
    nodes = (
        parse_node_list(args.nodes)
        if args.nodes
        else discover_nodes(args.partition).nodes
    )
    manifest = run_cluster(
        config_dir=args.config_dir,
        total_events=args.events_per_node * len(nodes),
        workers=args.workers,
        nodes=nodes,
        seed_base=args.seed_base,
        partition=args.partition,
        publish=False,
        label="cluster-smoke",
    )

    actual = tuple(manifest["actual_nodes"])
    if len(actual) != len(nodes) or set(actual) != set(nodes):
        raise RuntimeError(f"Expected nodes {nodes}, got {actual}")
    print(f"PASS: {len(nodes)} distinct nodes completed: {','.join(actual)}")
    print(f"Smoke-test artifacts: {manifest['run_root']}")


if __name__ == "__main__":
    main()

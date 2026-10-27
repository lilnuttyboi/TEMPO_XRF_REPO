#!/usr/bin/env python3
"""Exercise the dynamic Slurm queue and verify every queued chunk."""

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
    parser.add_argument("--chunks-per-node", type=int, default=2)
    parser.add_argument("--workers", type=int, choices=(40, 80), default=40)
    parser.add_argument("--seed-base", type=int, default=246_810)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.events_per_node < 1:
        raise ValueError("--events-per-node must be at least 1.")
    if args.chunks_per_node < 1:
        raise ValueError("--chunks-per-node must be at least 1.")
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
        chunks_per_node=args.chunks_per_node,
        partition=args.partition,
        publish=False,
        label="cluster-smoke",
    )

    actual = tuple(manifest["actual_nodes"])
    unexpected = set(actual) - set(nodes)
    if unexpected:
        raise RuntimeError(f"Unexpected execution nodes: {sorted(unexpected)}")
    node_summary = manifest["node_summary"]
    total_completed = sum(item["events"] for item in node_summary.values())
    if total_completed != manifest["requested_events"]:
        raise RuntimeError(
            f"Expected {manifest['requested_events']} events, got {total_completed}"
        )
    print(
        f"PASS: {manifest['chunk_count']} queued chunks completed across "
        f"{len(node_summary)} node(s): {','.join(node_summary)}"
    )
    for node, summary in node_summary.items():
        print(f"  {node}: {summary['chunks']} chunks, {summary['events']} events")
    print(f"Smoke-test artifacts: {manifest['run_root']}")


if __name__ == "__main__":
    main()

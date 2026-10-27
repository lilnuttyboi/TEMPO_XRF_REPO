#!/usr/bin/env python3
"""Benchmark end-to-end throughput for several queue chunk counts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time


REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from cluster_jobs.cluster_config import (  # noqa: E402
    discover_nodes,
    parse_node_list,
    validate_chunks_per_node,
    validate_workers,
)
from cluster_jobs.ui_cluster_run import run_cluster  # noqa: E402


def parse_chunk_counts(value: str) -> tuple[int, ...]:
    counts = tuple(
        dict.fromkeys(
            validate_chunks_per_node(item.strip())
            for item in value.split(",")
            if item.strip()
        )
    )
    if not counts:
        raise ValueError("--chunks must contain at least one positive integer.")
    return counts


def parse_worker_counts(value: str) -> tuple[int, ...]:
    counts = tuple(
        dict.fromkeys(
            validate_workers(item.strip())
            for item in value.split(",")
            if item.strip()
        )
    )
    if not counts:
        raise ValueError("--workers must contain at least one integer from 1 to 80.")
    return counts


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config_dir", type=Path)
    parser.add_argument("--events", type=int, required=True)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--nodes", help="Comma-separated expanded node names.")
    selection.add_argument("--node-count", type=int)
    parser.add_argument("--partition")
    parser.add_argument(
        "--workers",
        default="80",
        help="Comma-separated worker counts from 1 to 80, for example 70,80.",
    )
    parser.add_argument("--chunks", default="1,2,4,8")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed-base", type=int, default=731_000)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.events < 1:
        raise ValueError("--events must be at least 1.")
    if args.repetitions < 1:
        raise ValueError("--repetitions must be at least 1.")
    chunk_counts = parse_chunk_counts(args.chunks)
    worker_counts = parse_worker_counts(args.workers)

    if args.nodes:
        nodes = parse_node_list(args.nodes)
    else:
        discovered = discover_nodes(args.partition).nodes
        if args.node_count is None:
            nodes = discovered
        else:
            if not 1 <= args.node_count <= len(discovered):
                raise ValueError(
                    f"--node-count must be between 1 and {len(discovered)}."
                )
            nodes = discovered[: args.node_count]

    measurements = []
    combinations = tuple(
        (workers, chunks_per_node)
        for chunks_per_node in chunk_counts
        for workers in worker_counts
    )
    for repetition in range(args.repetitions):
        order = combinations if repetition % 2 == 0 else tuple(reversed(combinations))
        repetition_seed = args.seed_base + repetition * 10_000
        for workers, chunks_per_node in order:
            started = time.perf_counter()
            manifest = run_cluster(
                config_dir=args.config_dir,
                total_events=args.events,
                workers=workers,
                nodes=nodes,
                seed_base=repetition_seed,
                chunks_per_node=chunks_per_node,
                partition=args.partition,
                publish=False,
                label=(
                    f"benchmark-r{repetition + 1}-w{workers}-c{chunks_per_node}"
                ),
            )
            elapsed_seconds = time.perf_counter() - started
            node_seconds = sum(
                item["elapsed_ns"] / 1_000_000_000
                for item in manifest["node_summary"].values()
            )
            measurements.append(
                {
                    "repetition": repetition + 1,
                    "workers_per_node": workers,
                    "chunks_per_node": chunks_per_node,
                    "chunk_count": manifest["chunk_count"],
                    "selected_node_count": len(nodes),
                    "actual_node_count": len(manifest["node_summary"]),
                    "events": args.events,
                    "seed_base": repetition_seed,
                    "elapsed_seconds": elapsed_seconds,
                    "end_to_end_events_per_second": args.events / elapsed_seconds,
                    "geant4_node_seconds": node_seconds,
                    "average_node_events_per_second": args.events / node_seconds,
                    "node_summary": manifest["node_summary"],
                    "run_root": manifest["run_root"],
                    "job_id": manifest["job_id"],
                }
            )
            chunk_label = "chunk" if chunks_per_node == 1 else "chunks"
            print(
                f"{workers} workers, {chunks_per_node} {chunk_label}/node: "
                f"{elapsed_seconds:.3f} s, "
                f"{args.events / elapsed_seconds:,.2f} events/s end to end"
            )

    medians = {
        (workers, chunks_per_node): statistics.median(
            item["end_to_end_events_per_second"]
            for item in measurements
            if item["workers_per_node"] == workers
            if item["chunks_per_node"] == chunks_per_node
        )
        for workers in worker_counts
        for chunks_per_node in chunk_counts
    }
    winner = max(medians, key=medians.get)
    recommended_by_worker = {
        workers: max(
            chunk_counts,
            key=lambda chunks_per_node: medians[(workers, chunks_per_node)],
        )
        for workers in worker_counts
    }
    result = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config_dir": str(args.config_dir.resolve()),
        "config_sha256": hashlib.sha256(
            (args.config_dir.resolve() / "config.json").read_bytes()
        ).hexdigest(),
        "selected_nodes": list(nodes),
        "worker_counts": list(worker_counts),
        "events_per_run": args.events,
        "repetitions": args.repetitions,
        "paired_seed_base": args.seed_base,
        "chunk_counts_per_node": list(chunk_counts),
        "order_alternates": True,
        "measurements": measurements,
        "median_end_to_end_events_per_second": {
            f"w{workers}-c{chunks_per_node}": value
            for (workers, chunks_per_node), value in medians.items()
        },
        "recommended_chunks_per_node_by_worker": {
            str(workers): chunks_per_node
            for workers, chunks_per_node in recommended_by_worker.items()
        },
        "recommended_workers": winner[0],
        "recommended_chunks_per_node": winner[1],
    }

    if args.output:
        output = args.output.resolve()
    else:
        output_dir = args.config_dir.resolve() / "benchmarks"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / (time.strftime("%Y%m%d_%H%M%S") + "_chunks.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print("Median end-to-end throughput:")
    for workers in worker_counts:
        for chunks_per_node in chunk_counts:
            chunk_label = "chunk" if chunks_per_node == 1 else "chunks"
            print(
                f"  {workers} workers, {chunks_per_node} {chunk_label}/node: "
                f"{medians[(workers, chunks_per_node)]:,.2f} events/s"
            )
    print(
        f"Measured recommendation: {winner[0]} workers and "
        f"{winner[1]} {'chunk' if winner[1] == 1 else 'chunks'} per selected node"
    )
    print(f"Benchmark JSON: {output}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run a paired, repeatable 40-versus-80 Geant4 worker benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import time


REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from cluster_jobs.cluster_config import choose_nodes  # noqa: E402
from cluster_jobs.ui_cluster_run import run_cluster  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config_dir", type=Path)
    parser.add_argument("--node", help="Expanded Slurm node name.")
    parser.add_argument("--partition")
    parser.add_argument("--events", type=int, default=100_000)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--seed-base", type=int, default=424_242)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.events < 1:
        raise ValueError("--events must be at least 1.")
    if args.repetitions < 1:
        raise ValueError("--repetitions must be at least 1.")

    node = choose_nodes(
        explicit=args.node if args.node else None,
        count=None if args.node else 1,
        partition=args.partition,
    )[0]
    measurements = []

    # Alternate order to reduce bias from cache, temperature, or cluster drift.
    for repetition in range(args.repetitions):
        order = (40, 80) if repetition % 2 == 0 else (80, 40)
        pair_seed = args.seed_base + repetition * 100
        for workers in order:
            manifest = run_cluster(
                config_dir=args.config_dir,
                total_events=args.events,
                workers=workers,
                nodes=(node,),
                seed_base=pair_seed,
                partition=args.partition,
                publish=False,
                label=f"benchmark-r{repetition + 1}-w{workers}",
            )
            run_root = Path(manifest["run_root"])
            status = json.loads(
                (run_root / "shard-0" / "success.json").read_text(encoding="utf-8")
            )
            elapsed_seconds = status["elapsed_ns"] / 1_000_000_000
            measurements.append(
                {
                    "repetition": repetition + 1,
                    "workers": workers,
                    "node": status["node"],
                    "events": status["events"],
                    "seed_base": pair_seed,
                    "elapsed_seconds": elapsed_seconds,
                    "events_per_second": status["events"] / elapsed_seconds,
                    "affinity": status.get("affinity"),
                    "run_root": str(run_root),
                    "job_id": manifest["job_id"],
                }
            )

    medians = {
        workers: statistics.median(
            item["events_per_second"]
            for item in measurements
            if item["workers"] == workers
        )
        for workers in (40, 80)
    }
    winner = max(medians, key=medians.get)
    speedup_80_over_40 = medians[80] / medians[40]
    result = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config_dir": str(args.config_dir.resolve()),
        "node": node,
        "events_per_run": args.events,
        "repetitions": args.repetitions,
        "paired_seed_base": args.seed_base,
        "order_alternates": True,
        "measurements": measurements,
        "median_events_per_second": {str(key): value for key, value in medians.items()},
        "speedup_80_over_40": speedup_80_over_40,
        "recommended_workers": winner,
    }

    if args.output:
        output = args.output.resolve()
    else:
        output_dir = args.config_dir.resolve() / "benchmarks"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / (time.strftime("%Y%m%d_%H%M%S") + "_workers.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print(f"40 workers median: {medians[40]:.2f} events/s")
    print(f"80 workers median: {medians[80]:.2f} events/s")
    print(f"80/40 throughput ratio: {speedup_80_over_40:.3f}x")
    print(f"Measured recommendation: {winner} workers per node")
    print(f"Benchmark JSON: {output}")


if __name__ == "__main__":
    main()

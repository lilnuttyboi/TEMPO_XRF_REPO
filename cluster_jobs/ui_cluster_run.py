#!/usr/bin/env python3
"""Submit, monitor, validate, and combine a configurable Geant4 cluster run."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import uuid
from typing import Sequence


REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from cluster_jobs.cluster_config import (  # noqa: E402
    build_sbatch_command,
    calculate_queue_shape,
    choose_nodes,
    parse_node_list,
    seed_pairs,
    split_events,
    validate_chunks_per_node,
    validate_workers,
)


EVENT = re.compile(r"Event\s+(\d+)")


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _tail(path: Path, size: int = 4_000) -> str:
    if not path.is_file():
        return f"missing log: {path}"
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - size))
        return stream.read().decode(errors="replace")


def _new_run_root(config_dir: Path, label: str | None) -> Path:
    suffix = ""
    if label:
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-.")
        if safe_label:
            suffix = f"_{safe_label}"
    identifier = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    return config_dir / "cluster_runs" / f"{identifier}{suffix}"


def run_cluster(
    *,
    config_dir: Path,
    total_events: int,
    workers: int,
    nodes: Sequence[str],
    seed_base: int | None = None,
    chunks_per_node: int = 1,
    partition: str | None = None,
    publish: bool = True,
    label: str | None = None,
) -> dict:
    """Run a throttled queue of chunks and return the completed run manifest."""
    config_dir = config_dir.resolve()
    total_events = int(total_events)
    workers = validate_workers(workers)
    chunks_per_node = validate_chunks_per_node(chunks_per_node)
    selected_nodes = parse_node_list(nodes)
    if total_events < 1:
        raise ValueError("Total events must be at least 1.")

    shard_count, max_concurrent = calculate_queue_shape(
        total_events,
        len(selected_nodes),
        chunks_per_node,
    )
    counts = split_events(total_events, shard_count)
    if seed_base is None:
        seed_base = secrets.randbelow(1_000_000_000) + 1
    seeds = seed_pairs(seed_base, shard_count)

    for name in ("config.json", "run.mac"):
        if not (config_dir / name).is_file():
            raise FileNotFoundError(f"Compiled run input is missing: {config_dir / name}")

    run_root = _new_run_root(config_dir, label)
    (run_root / "logs").mkdir(parents=True)
    for name in ("config.json", "run.mac"):
        shutil.copy2(config_dir / name, run_root / name)

    command = build_sbatch_command(
        repo=REPO,
        run_root=run_root,
        total_events=total_events,
        workers=workers,
        nodes=selected_nodes,
        seed_base=seed_base,
        chunks_per_node=chunks_per_node,
        partition=partition,
    )
    manifest = {
        "version": 3,
        "status": "submitting",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config_dir": str(config_dir),
        "run_root": str(run_root),
        "requested_events": total_events,
        "workers_per_node": workers,
        "selected_nodes": list(selected_nodes),
        "chunks_per_node": chunks_per_node,
        "chunk_count": shard_count,
        "max_concurrent_chunks": max_concurrent,
        # Retained for compatibility with the existing validated merger.
        "shard_count": shard_count,
        "event_counts": list(counts),
        "seed_base": seed_base,
        "seed_pairs": [list(pair) for pair in seeds],
        "publish_requested": publish,
        "sbatch_command": command,
    }
    _write_json(run_root / "run_manifest.json", manifest)

    job_id = None
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def cancel(_signum, _frame):
        if job_id:
            subprocess.run(["scancel", job_id], check=False)
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, cancel)
    try:
        submission = subprocess.run(command, check=True, capture_output=True, text=True)
        job_id = submission.stdout.strip().split(";")[0]
        if not job_id.isdecimal():
            raise RuntimeError(f"Unexpected sbatch response: {submission.stdout!r}")
        manifest["job_id"] = job_id
        manifest["status"] = "running"
        _write_json(run_root / "run_manifest.json", manifest)

        print(f"Submitted Slurm job {job_id}", flush=True)
        print(f"Selected nodes: {','.join(selected_nodes)}", flush=True)
        print(
            f"Queued chunks: {shard_count}; maximum concurrent: {max_concurrent}",
            flush=True,
        )
        print(f"Chunk results: {run_root}", flush=True)

        positions = [0] * shard_count
        completed = [0] * shard_count
        last_progress = 0

        while True:
            for shard_id in range(shard_count):
                log = run_root / "logs" / f"slurm-{job_id}_{shard_id}.log"
                if not log.exists():
                    continue
                with log.open("r", encoding="utf-8", errors="replace") as stream:
                    stream.seek(positions[shard_id])
                    new_text = stream.read()
                    positions[shard_id] = stream.tell()
                for match in EVENT.finditer(new_text):
                    completed[shard_id] = max(
                        completed[shard_id],
                        min(counts[shard_id], int(match.group(1)) + 1),
                    )

            progress = sum(completed)
            if progress > last_progress:
                print(f"Event {progress}", flush=True)
                last_progress = progress

            queue = subprocess.run(
                ["squeue", "-h", "-j", job_id, "-o", "%i"],
                capture_output=True,
                text=True,
            )
            if queue.returncode != 0 and "Invalid job id" not in queue.stderr:
                raise RuntimeError(f"squeue failed: {queue.stderr.strip()}")
            if not queue.stdout.strip():
                break
            time.sleep(1)

        # Give close-to-open NFS consistency a short window after Slurm releases
        # the allocation. A failed setup may legitimately produce no marker.
        marker_deadline = time.monotonic() + 5
        while time.monotonic() < marker_deadline:
            if all(
                (run_root / f"shard-{shard_id}" / "success.json").is_file()
                or (run_root / f"shard-{shard_id}" / "failure.json").is_file()
                for shard_id in range(shard_count)
            ):
                break
            time.sleep(0.2)

        successful_ids = []
        failures = []
        statuses = []
        for shard_id in range(shard_count):
            folder = run_root / f"shard-{shard_id}"
            marker = folder / "success.json"
            log = run_root / "logs" / f"slurm-{job_id}_{shard_id}.log"
            try:
                status = json.loads(marker.read_text(encoding="utf-8"))
                expected = {
                    "id": shard_id,
                    "events": counts[shard_id],
                    "workers": workers,
                    "seeds": list(seeds[shard_id]),
                    "exit_code": 0,
                }
                mismatches = {
                    key: (expected_value, status.get(key))
                    for key, expected_value in expected.items()
                    if status.get(key) != expected_value
                }
                if mismatches:
                    raise ValueError(f"success-marker mismatch: {mismatches}")
                if status.get("node") not in selected_nodes:
                    raise ValueError(f"unexpected node: {status.get('node')}")
                if not (folder / "simulation.root").is_file():
                    raise FileNotFoundError("simulation.root is missing")
                successful_ids.append(shard_id)
                statuses.append(status)
            except Exception as exc:
                failures.append(
                    {
                        "id": shard_id,
                        "reason": str(exc),
                        "log_tail": _tail(log),
                    }
                )

        actual_nodes = [status["node"] for status in statuses]
        node_summary = {}
        for status in statuses:
            summary = node_summary.setdefault(
                status["node"],
                {"chunks": 0, "events": 0, "elapsed_ns": 0},
            )
            summary["chunks"] += 1
            summary["events"] += int(status["events"])
            summary["elapsed_ns"] += int(status.get("elapsed_ns", 0))

        if successful_ids:
            subprocess.run(
                [
                    sys.executable,
                    str(REPO / "cluster_jobs" / "combine_shards.py"),
                    str(run_root),
                    "--shards",
                    ",".join(map(str, successful_ids)),
                ],
                check=True,
            )

        manifest["successful_shards"] = successful_ids
        manifest["failed_shards"] = failures
        manifest["actual_nodes"] = actual_nodes
        manifest["node_summary"] = node_summary
        manifest["status"] = "failed" if failures else "shards_complete"
        _write_json(run_root / "run_manifest.json", manifest)

        if failures:
            details = "\n\n".join(
                f"Shard {failure['id']}: {failure['reason']}\n{failure['log_tail']}"
                for failure in failures
            )
            raise RuntimeError(
                f"{len(failures)} shard validation failure(s). "
                f"Validated successful shards were merged only as a non-published "
                f"artifact in {run_root}.\n"
                f"{details}"
            )

        source = run_root / "combined_response.root"
        destination = config_dir / "simulation.root"
        if publish:
            if destination.exists():
                raise FileExistsError(f"Result appeared during run: {destination}")
            try:
                os.link(source, destination)
            except OSError:
                shutil.copy2(source, destination)
            manifest["published_result"] = str(destination)

        manifest["status"] = "complete"
        _write_json(run_root / "run_manifest.json", manifest)

        print(f"Event {total_events}", flush=True)
        print(f"Combined result: {source}", flush=True)
        if publish:
            print(f"Published result: {destination}", flush=True)
        return manifest
    except BaseException as exc:
        if manifest.get("status") != "complete":
            manifest["status"] = (
                "cancelled"
                if isinstance(exc, SystemExit) and exc.code == 143
                else "failed"
            )
            manifest["error"] = str(exc)
            _write_json(run_root / "run_manifest.json", manifest)
        if job_id:
            subprocess.run(
                ["scancel", job_id],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        raise
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config_dir", type=Path)
    parser.add_argument("total_events", type=int)
    parser.add_argument("workers", type=int)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--nodes", help="Comma-separated expanded node names.")
    selection.add_argument("--node-count", type=int, default=None)
    parser.add_argument("--partition")
    parser.add_argument("--seed-base", type=int)
    parser.add_argument("--chunks-per-node", type=int, default=1)
    parser.add_argument("--label")
    parser.add_argument("--no-publish", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    nodes = choose_nodes(
        explicit=args.nodes,
        count=args.node_count,
        partition=args.partition,
    )
    run_cluster(
        config_dir=args.config_dir,
        total_events=args.total_events,
        workers=args.workers,
        nodes=nodes,
        seed_base=args.seed_base,
        chunks_per_node=args.chunks_per_node,
        partition=args.partition,
        publish=not args.no_publish,
        label=args.label,
    )


if __name__ == "__main__":
    main()

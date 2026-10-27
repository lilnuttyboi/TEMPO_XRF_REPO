"""Shared cluster discovery, validation, and Slurm resource settings."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
from typing import Iterable, Sequence


DEFAULT_NODES = tuple(
    f"node-{group:02d}-{index:03d}"
    for group in (1, 2)
    for index in range(1, 5)
)
DEFAULT_PARTITION = "debug"
MEMORY_MIB_PER_NODE = 185_000
PHYSICAL_CORES_PER_NODE = 40
LOGICAL_CPUS_PER_NODE = 80
# Conservative fallback for a production-sized full-XRF run. Automatic mode
# replaces this with timing from matching completed runs whenever possible.
# The earlier 100,000-event benchmark is deliberately not used here because
# its per-node rate was dominated by Geant4 startup time.
FALLBACK_EVENTS_PER_SECOND_40 = 217_000.0
FALLBACK_EVENTS_PER_SECOND_80 = 274_000.0
RECOMMENDED_CHUNK_SECONDS = 60.0
MAX_AUTOMATIC_CHUNKS_PER_NODE = 8
MAX_CHUNKS_PER_NODE = 32
MIN_CALIBRATION_CHUNK_SECONDS = 15.0
NODE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class NodeInventory:
    nodes: tuple[str, ...]
    source: str
    warning: str | None = None


@dataclass(frozen=True)
class ThroughputCalibration:
    events_per_second: float
    run_count: int
    chunk_count: int
    median_chunk_seconds: float
    source: str


@dataclass(frozen=True)
class QueueBenchmarkRecommendation:
    workers: int
    chunks_per_node: int
    selected_nodes: tuple[str, ...]
    events_per_run: int
    repetitions: int
    end_to_end_events_per_second: float
    source: str


def _deduplicate(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def parse_node_list(value: str | Iterable[str]) -> tuple[str, ...]:
    """Parse and validate an explicit list of expanded Slurm host names."""
    if isinstance(value, str):
        candidates = re.split(r"[\s,]+", value.strip())
    else:
        candidates = [str(item).strip() for item in value]

    nodes = _deduplicate(item for item in candidates if item)
    if not nodes:
        raise ValueError("Select at least one compute node.")

    invalid = [node for node in nodes if not NODE_NAME.fullmatch(node)]
    if invalid:
        raise ValueError(
            "Node names must be expanded host names containing only letters, "
            f"digits, '.', '_' or '-'; invalid: {', '.join(invalid)}"
        )
    return nodes


def discover_nodes(partition: str | None = None) -> NodeInventory:
    """Return nodes from an override, Slurm, or the documented fallback list."""
    override = os.environ.get("TEMPO_XRF_CLUSTER_NODES", "").strip()
    if override:
        return NodeInventory(parse_node_list(override), "TEMPO_XRF_CLUSTER_NODES")

    selected_partition = partition or os.environ.get(
        "TEMPO_XRF_SLURM_PARTITION", DEFAULT_PARTITION
    )
    try:
        result = subprocess.run(
            ["sinfo", "-h", "-N", "-p", selected_partition, "-o", "%N"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        nodes = parse_node_list(result.stdout)
        return NodeInventory(nodes, f"Slurm partition {selected_partition}")
    except (FileNotFoundError, subprocess.SubprocessError, ValueError) as exc:
        return NodeInventory(
            DEFAULT_NODES,
            "documented fallback",
            f"Could not discover Slurm nodes: {exc}",
        )


def choose_nodes(
    *,
    explicit: str | Sequence[str] | None = None,
    count: int | None = None,
    partition: str | None = None,
) -> tuple[str, ...]:
    if explicit is not None:
        nodes = parse_node_list(explicit)
        if count is not None and count != len(nodes):
            raise ValueError("--node-count conflicts with the explicit node list.")
        return nodes

    inventory = discover_nodes(partition)
    requested = 1 if count is None else int(count)
    if requested < 1:
        raise ValueError("Node count must be at least 1.")
    if requested > len(inventory.nodes):
        raise ValueError(
            f"Requested {requested} nodes, but only {len(inventory.nodes)} were "
            f"discovered from {inventory.source}: {', '.join(inventory.nodes)}"
        )
    return inventory.nodes[:requested]


def validate_workers(workers: int) -> int:
    workers = int(workers)
    if not 1 <= workers <= LOGICAL_CPUS_PER_NODE:
        raise ValueError(
            f"Workers per node must be between 1 and {LOGICAL_CPUS_PER_NODE}."
        )
    return workers


def validate_chunks_per_node(chunks_per_node: int) -> int:
    chunks_per_node = int(chunks_per_node)
    if chunks_per_node < 1:
        raise ValueError("Chunks per node must be at least 1.")
    return chunks_per_node


def estimate_node_throughput(workers: int) -> float:
    """Return a conservative per-node fallback for queue-size previews."""
    workers = validate_workers(workers)
    if workers <= PHYSICAL_CORES_PER_NODE:
        return FALLBACK_EVENTS_PER_SECOND_40 * (
            workers / PHYSICAL_CORES_PER_NODE
        )

    logical_fraction = (
        workers - PHYSICAL_CORES_PER_NODE
    ) / PHYSICAL_CORES_PER_NODE
    return FALLBACK_EVENTS_PER_SECOND_40 + logical_fraction * (
        FALLBACK_EVENTS_PER_SECOND_80 - FALLBACK_EVENTS_PER_SECOND_40
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_throughput_calibration(
    config_dir: Path,
    workers: int,
    *,
    minimum_chunk_seconds: float = MIN_CALIBRATION_CHUNK_SECONDS,
    max_runs: int = 3,
) -> ThroughputCalibration | None:
    """Measure per-node throughput from matching, representative completed runs.

    The copied ``config.json`` must match byte-for-byte, every queued chunk must
    have succeeded, and the median chunk must be long enough that startup does
    not dominate. Up to the three newest qualifying runs are combined using
    event-weighted Geant4 time.
    """
    config_dir = Path(config_dir).resolve()
    workers = validate_workers(workers)
    if not math.isfinite(minimum_chunk_seconds) or minimum_chunk_seconds <= 0:
        raise ValueError("minimum_chunk_seconds must be finite and greater than 0.")
    if max_runs < 1:
        raise ValueError("max_runs must be at least 1.")

    current_config = config_dir / "config.json"
    cluster_runs = config_dir / "cluster_runs"
    if not current_config.is_file() or not cluster_runs.is_dir():
        return None
    current_hash = _sha256(current_config)

    manifests = sorted(
        cluster_runs.glob("*/run_manifest.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    total_events = 0
    total_elapsed_seconds = 0.0
    durations: list[float] = []
    qualifying_runs = 0
    qualifying_chunks = 0

    for manifest_path in manifests:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("status") != "complete":
                continue
            if int(manifest.get("workers_per_node", 0)) != workers:
                continue
            chunk_count = int(manifest["chunk_count"])
            if chunk_count < 1:
                raise ValueError("invalid chunk count")
            copied_config = manifest_path.parent / "config.json"
            if not copied_config.is_file() or _sha256(copied_config) != current_hash:
                continue

            statuses = []
            for chunk_id in range(chunk_count):
                marker = manifest_path.parent / f"shard-{chunk_id}" / "success.json"
                status = json.loads(marker.read_text(encoding="utf-8"))
                if (
                    int(status.get("exit_code", -1)) != 0
                    or int(status.get("workers", 0)) != workers
                    or int(status.get("events", 0)) < 1
                    or int(status.get("elapsed_ns", 0)) < 1
                ):
                    raise ValueError("invalid success marker")
                statuses.append(status)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue

        run_durations = [
            int(item["elapsed_ns"]) / 1_000_000_000 for item in statuses
        ]
        if statistics.median(run_durations) < minimum_chunk_seconds:
            continue

        total_events += sum(int(item["events"]) for item in statuses)
        total_elapsed_seconds += sum(run_durations)
        durations.extend(run_durations)
        qualifying_runs += 1
        qualifying_chunks += len(statuses)
        if qualifying_runs >= max_runs:
            break

    if not qualifying_runs or total_elapsed_seconds <= 0:
        return None

    return ThroughputCalibration(
        events_per_second=total_events / total_elapsed_seconds,
        run_count=qualifying_runs,
        chunk_count=qualifying_chunks,
        median_chunk_seconds=statistics.median(durations),
        source=f"{qualifying_runs} matching completed run(s)",
    )


def load_queue_benchmark_recommendation(
    config_dir: Path,
    workers: int,
    selected_nodes: Sequence[str],
    total_events: int,
) -> QueueBenchmarkRecommendation | None:
    """Return a measured queue winner for a comparable workload and nodes."""
    config_dir = Path(config_dir).resolve()
    workers = validate_workers(workers)
    nodes = parse_node_list(selected_nodes)
    total_events = int(total_events)
    if total_events < 1:
        raise ValueError("Total events must be at least 1.")
    current_config = config_dir / "config.json"
    benchmark_dir = config_dir / "benchmarks"
    if not current_config.is_file() or not benchmark_dir.is_dir():
        return None
    current_hash = _sha256(current_config)

    paths = sorted(
        benchmark_dir.glob("*_chunks.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in paths:
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            if result.get("config_sha256") != current_hash:
                continue
            measured_nodes = parse_node_list(result["selected_nodes"])
            if set(measured_nodes) != set(nodes):
                continue
            recommendation = result["recommended_chunks_per_node_by_worker"]
            chunks_per_node = validate_chunks_per_node(recommendation[str(workers)])
            if chunks_per_node > MAX_CHUNKS_PER_NODE:
                raise ValueError("benchmark recommendation exceeds manual limit")
            rates = result["median_end_to_end_events_per_second"]
            rate = float(rates[f"w{workers}-c{chunks_per_node}"])
            if not math.isfinite(rate) or rate <= 0:
                raise ValueError("invalid benchmark throughput")
            events_per_run = int(result["events_per_run"])
            repetitions = int(result["repetitions"])
            if events_per_run < 1 or repetitions < 1:
                raise ValueError("invalid benchmark sample size")
            event_scale_ratio = max(
                total_events / events_per_run,
                events_per_run / total_events,
            )
            if event_scale_ratio > 2:
                continue
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue

        return QueueBenchmarkRecommendation(
            workers=workers,
            chunks_per_node=chunks_per_node,
            selected_nodes=measured_nodes,
            events_per_run=events_per_run,
            repetitions=repetitions,
            end_to_end_events_per_second=rate,
            source=str(path),
        )
    return None


def recommend_chunks_per_node(
    total_events: int,
    node_count: int,
    workers: int,
    *,
    events_per_second: float | None = None,
    target_chunk_seconds: float = RECOMMENDED_CHUNK_SECONDS,
    max_chunks_per_node: int = MAX_AUTOMATIC_CHUNKS_PER_NODE,
) -> int:
    """Recommend queue depth while keeping each chunk long enough to be useful.

    A matching long-run measurement should be supplied when available. The
    conservative fallback avoids exploding a production run into hundreds of
    startup-dominated chunks. The UI keeps a larger manual override.
    """
    total_events = int(total_events)
    node_count = int(node_count)
    max_chunks_per_node = validate_chunks_per_node(max_chunks_per_node)
    if total_events < 1:
        raise ValueError("Total events must be at least 1.")
    if node_count < 1:
        raise ValueError("Node count must be at least 1.")
    if not math.isfinite(target_chunk_seconds) or target_chunk_seconds <= 0:
        raise ValueError("Target chunk duration must be finite and greater than 0.")

    throughput = (
        estimate_node_throughput(workers)
        if events_per_second is None
        else float(events_per_second)
    )
    if not math.isfinite(throughput) or throughput <= 0:
        raise ValueError("events_per_second must be finite and greater than 0.")

    target_events = max(
        1,
        int(throughput * target_chunk_seconds),
    )
    required_chunks = max(1, math.ceil(total_events / target_events))
    chunks_per_node = max(1, math.ceil(required_chunks / node_count))
    return min(chunks_per_node, max_chunks_per_node)


def calculate_queue_shape(
    total_events: int,
    node_count: int,
    chunks_per_node: int,
) -> tuple[int, int]:
    """Return total array chunks and the maximum concurrent array tasks."""
    total_events = int(total_events)
    node_count = int(node_count)
    chunks_per_node = validate_chunks_per_node(chunks_per_node)
    if total_events < 1:
        raise ValueError("Total events must be at least 1.")
    if node_count < 1:
        raise ValueError("Node count must be at least 1.")

    chunk_count = min(total_events, node_count * chunks_per_node)
    return chunk_count, min(node_count, chunk_count)


def split_events(total_events: int, shard_count: int) -> tuple[int, ...]:
    total_events = int(total_events)
    shard_count = int(shard_count)
    if total_events < 1:
        raise ValueError("Total events must be at least 1.")
    if shard_count < 1:
        raise ValueError("Shard count must be at least 1.")
    if shard_count > total_events:
        raise ValueError("Shard count cannot exceed the number of events.")
    quotient, remainder = divmod(total_events, shard_count)
    return tuple(quotient + (index < remainder) for index in range(shard_count))


def seed_pairs(seed_base: int, shard_count: int) -> tuple[tuple[int, int], ...]:
    seed_base = int(seed_base)
    final_seed = seed_base + shard_count * 2 - 1
    if seed_base < 1 or final_seed >= 2_147_483_647:
        raise ValueError("Seed range must stay between 1 and 2147483646.")
    return tuple(
        (seed_base + index * 2, seed_base + index * 2 + 1)
        for index in range(shard_count)
    )


def worker_resource_args(workers: int) -> list[str]:
    """Return allocation flags matching Geant4 worker count to logical CPUs."""
    workers = validate_workers(workers)
    if workers <= PHYSICAL_CORES_PER_NODE:
        topology = ["--hint=nomultithread"]
    else:
        topology = ["--hint=multithread"]
    return [
        f"--cpus-per-task={workers}",
        *topology,
        f"--mem={MEMORY_MIB_PER_NODE}",
    ]


def build_sbatch_command(
    *,
    repo: Path,
    run_root: Path,
    total_events: int,
    workers: int,
    nodes: Sequence[str],
    seed_base: int,
    chunks_per_node: int = 1,
    partition: str | None = None,
) -> list[str]:
    selected_nodes = parse_node_list(nodes)
    shard_count, max_concurrent = calculate_queue_shape(
        total_events,
        len(selected_nodes),
        chunks_per_node,
    )
    split_events(total_events, shard_count)
    seed_pairs(seed_base, shard_count)
    selected_partition = partition or os.environ.get(
        "TEMPO_XRF_SLURM_PARTITION", DEFAULT_PARTITION
    )

    return [
        "sbatch",
        "--parsable",
        f"--partition={selected_partition}",
        "--job-name=tempo-xrf",
        "--nodes=1",
        "--ntasks=1",
        "--ntasks-per-node=1",
        "--exclusive",
        f"--array=0-{shard_count - 1}%{max_concurrent}",
        f"--nodelist={','.join(selected_nodes)}",
        *worker_resource_args(workers),
        "--export=ALL",
        "--chdir=/tmp",
        f"--output={run_root / 'logs' / 'slurm-%A_%a.log'}",
        str(repo / "cluster_jobs" / "run_cluster.sbatch"),
        str(run_root),
        str(int(total_events)),
        str(int(workers)),
        str(shard_count),
        str(int(seed_base)),
        str(repo),
    ]

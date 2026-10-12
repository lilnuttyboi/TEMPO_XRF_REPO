"""Shared cluster discovery, validation, and Slurm resource settings."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
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
NODE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class NodeInventory:
    nodes: tuple[str, ...]
    source: str
    warning: str | None = None


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
    partition: str | None = None,
) -> list[str]:
    selected_nodes = parse_node_list(nodes)
    shard_count = min(int(total_events), len(selected_nodes))
    selected_nodes = selected_nodes[:shard_count]
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
        f"--nodes={shard_count}",
        f"--ntasks={shard_count}",
        "--ntasks-per-node=1",
        "--exclusive",
        f"--nodelist={','.join(selected_nodes)}",
        *worker_resource_args(workers),
        "--export=ALL",
        "--chdir=/tmp",
        f"--output={run_root / 'logs' / 'slurm-%j-controller.log'}",
        str(repo / "cluster_jobs" / "run_cluster.sbatch"),
        str(run_root),
        str(int(total_events)),
        str(int(workers)),
        str(shard_count),
        str(int(seed_base)),
        str(repo),
    ]

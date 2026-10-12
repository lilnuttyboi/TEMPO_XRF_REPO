#!/usr/bin/env python3
"""Merge only shard outputs carrying a valid success marker."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Iterable


BEAM_ON = re.compile(r"(?m)^/run/beamOn\s+(\d+)\s*$")
SEEDS = re.compile(r"(?m)^/random/setSeeds\s+(\d+)\s+(\d+)\s*$")


def parse_macro(folder: Path) -> tuple[int, tuple[int, int]]:
    macro = (folder / "run.mac").read_text(encoding="utf-8")
    counts = BEAM_ON.findall(macro)
    seeds = SEEDS.findall(macro)
    if len(counts) != 1 or len(seeds) != 1:
        raise ValueError(f"Invalid shard macro in {folder}")
    return int(counts[0]), (int(seeds[0][0]), int(seeds[0][1]))


def successful_shard_ids(root: Path) -> tuple[int, ...]:
    ids = []
    for marker in root.glob("shard-*/success.json"):
        match = re.fullmatch(r"shard-(\d+)", marker.parent.name)
        if match:
            ids.append(int(match.group(1)))
    return tuple(sorted(set(ids)))


def _load_run_manifest(root: Path) -> dict:
    path = root / "run_manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_success(folder: Path, shard_id: int) -> dict:
    marker = folder / "success.json"
    if not marker.is_file():
        raise ValueError(f"Shard {shard_id} has no success marker: {marker}")
    status = json.loads(marker.read_text(encoding="utf-8"))
    if status.get("id") != shard_id or status.get("exit_code") != 0:
        raise ValueError(f"Invalid success marker for shard {shard_id}: {status}")
    return status


def merge_shards(root: Path, shard_ids: Iterable[int] | None = None) -> dict:
    """Merge successful shards and return the written response metadata."""
    import numpy as np
    import uproot

    root = root.resolve()
    output = root / "combined_response.root"
    temporary = root / "combined_response.tmp.root"
    metadata_path = root / "combined_response.json"
    metadata_temporary = root / "combined_response.tmp.json"
    if any(path.exists() for path in (output, temporary, metadata_path, metadata_temporary)):
        raise FileExistsError("Combined output already exists; refusing to overwrite it.")

    selected_ids = tuple(
        sorted(successful_shard_ids(root) if shard_ids is None else set(shard_ids))
    )
    if not selected_ids:
        raise ValueError("No successful shards are available to merge.")

    run_manifest = _load_run_manifest(root)
    expected_shards = int(run_manifest.get("shard_count", len(selected_ids)))
    requested_events = run_manifest.get("requested_events")
    schema = {
        "EventId": np.int64,
        "rootId": np.int32,
        "Energy": np.float64,
        "Weight": np.float64,
    }
    offset = 0
    rows = 0
    shards = []

    try:
        with uproot.recreate(temporary) as destination:
            merged = destination.mktree("MyTree", schema)

            for shard_id in selected_ids:
                folder = root / f"shard-{shard_id}"
                events, seeds = parse_macro(folder)
                status = _load_success(folder, shard_id)
                if status.get("events") != events:
                    raise ValueError(
                        f"Shard {shard_id} event count differs between macro and marker."
                    )
                if tuple(status.get("seeds", ())) != seeds:
                    raise ValueError(
                        f"Shard {shard_id} seeds differ between macro and marker."
                    )

                source = folder / "simulation.root"
                if not source.is_file():
                    raise FileNotFoundError(f"Missing successful shard output: {source}")

                with uproot.open(source) as file:
                    tree = file["MyTree"]
                    for batch in tree.iterate(
                        list(schema), step_size="64 MB", library="np", how=dict
                    ):
                        ids = batch["EventId"].astype(np.int64)
                        if np.any((ids < 0) | (ids >= events)):
                            raise ValueError(f"EventId outside range in {source}")

                        merged.extend(
                            {
                                "EventId": ids + offset,
                                "rootId": batch["rootId"].astype(np.int32),
                                "Energy": batch["Energy"].astype(np.float64),
                                "Weight": batch["Weight"].astype(np.float64),
                            }
                        )
                        rows += len(ids)

                shards.append(
                    {
                        "id": shard_id,
                        "node": status.get("node"),
                        "events": events,
                        "event_offset": offset,
                        "workers": status.get("workers"),
                        "seeds": list(seeds),
                        "elapsed_ns": status.get("elapsed_ns"),
                        "source": str(source),
                    }
                )
                offset += events

        temporary.replace(output)
        failed_ids = sorted(set(range(expected_shards)) - set(selected_ids))
        complete = not failed_ids and (
            requested_events is None or int(requested_events) == offset
        )
        metadata = {
            "complete": complete,
            "requested_events": requested_events,
            "total_events": offset,
            "tree_rows": rows,
            "expected_shards": expected_shards,
            "successful_shards": list(selected_ids),
            "failed_shards": failed_ids,
            "shards": shards,
        }
        metadata_temporary.write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        metadata_temporary.replace(metadata_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        metadata_temporary.unlink(missing_ok=True)
        raise

    with uproot.open(output) as file:
        print(f"Combined: {output}")
        print(f"Simulated events: {offset}")
        print(f"MyTree rows: {file['MyTree'].num_entries}")
    return metadata


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path)
    parser.add_argument(
        "--shards",
        help="Comma-separated successful shard IDs; defaults to success markers.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    shard_ids = None
    if args.shards:
        shard_ids = tuple(int(item) for item in args.shards.split(","))
    merge_shards(args.run_root, shard_ids)


if __name__ == "__main__":
    main()

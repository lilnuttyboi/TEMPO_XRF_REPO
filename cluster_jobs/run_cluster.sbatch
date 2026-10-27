#!/usr/bin/env bash
set -euo pipefail

root=$1
total=$2
workers=$3
shards=$4
seed_base=$5
repo=$(cd "${6:?Shared repository path is required}" && pwd)
id=${SLURM_ARRAY_TASK_ID:?SLURM_ARRAY_TASK_ID is required}

if [[ ! -r $repo/cluster_jobs/run_shard.sbatch ]]; then
    echo "Shard launcher is not readable: $repo/cluster_jobs/run_shard.sbatch" >&2
    exit 2
fi

if (( id < 0 || id >= shards )); then
    echo "Array task $id is outside the expected chunk range 0-$((shards - 1))." >&2
    exit 2
fi

echo "Launching queued chunk $id/$((shards - 1)) on $SLURM_JOB_NODELIST: total_events=$total workers_per_node=$workers"

set +e
srun \
    --nodes=1 \
    --ntasks=1 \
    --ntasks-per-node=1 \
    --cpus-per-task="$workers" \
    --cpu-bind=verbose,threads \
    --kill-on-bad-exit=1 \
    bash "$repo/cluster_jobs/run_shard.sbatch" \
        "$root" "$total" "$workers" "$shards" "$seed_base" "$id"
status=$?
set -e

echo "Queued chunk $id exited; srun status=$status"
exit "$status"

#!/usr/bin/env bash
set -euo pipefail

root=$1
total=$2
workers=$3
shards=$4
seed_base=$5
repo=$(cd "${6:?Shared repository path is required}" && pwd)

if [[ ! -r $repo/cluster_jobs/run_shard.sbatch ]]; then
    echo "Shard launcher is not readable: $repo/cluster_jobs/run_shard.sbatch" >&2
    exit 2
fi

if [[ ${SLURM_NTASKS:-0} -ne $shards ]]; then
    echo "Expected $shards Slurm tasks; allocation has ${SLURM_NTASKS:-unset}." >&2
    exit 2
fi

echo "Launching $shards shards on $SLURM_JOB_NODELIST: total_events=$total workers_per_node=$workers"

set +e
srun \
    --nodes="$shards" \
    --ntasks="$shards" \
    --ntasks-per-node=1 \
    --cpus-per-task="$workers" \
    --cpu-bind=verbose,threads \
    --kill-on-bad-exit=0 \
    --output="$root/logs/slurm-${SLURM_JOB_ID}_%t.log" \
    bash "$repo/cluster_jobs/run_shard.sbatch" \
        "$root" "$total" "$workers" "$shards" "$seed_base"
status=$?
set -e

echo "All shard tasks exited; srun status=$status"
exit "$status"

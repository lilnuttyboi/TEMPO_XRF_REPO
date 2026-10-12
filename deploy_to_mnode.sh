#!/usr/bin/env bash
# Selectively deploy the cluster/frontend changes from the laptop to management.

set -Eeuo pipefail

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
REPO_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
TARGET="${TEMPO_XRF_DEPLOY_TARGET:-mnode@192.168.10.3:/srv/cluster/TEMPO_XRF_REPO/}"

[[ "$(hostname -s)" != "mnode-01" ]] || fail "Run this script on the laptop, not on mnode-01."
[[ -f "$REPO_DIR/python_code/roboai_xrf_frontend/main.py" ]] || fail "Laptop frontend source not found in $REPO_DIR."
command -v rsync >/dev/null 2>&1 || fail "rsync is not installed on this machine."

RSYNC_OPTIONS=(
    --archive
    --verbose
    --itemize-changes
    --relative
    --backup
    --backup-dir=.deployment_backups/before_dynamic_cluster
)

if [[ "${1:-}" == "--dry-run" ]]; then
    RSYNC_OPTIONS+=(--dry-run)
elif [[ $# -ne 0 ]]; then
    fail "Usage: $0 [--dry-run]"
fi

cd "$REPO_DIR"

rsync "${RSYNC_OPTIONS[@]}" -e 'ssh -F /dev/null' \
    ./AGENTS.md \
    ./docs/CLUSTER_CONTEXT.md \
    ./docs/CLUSTER_IMPLEMENTATION.md \
    ./start_tempo_xrf.sh \
    ./deploy_to_mnode.sh \
    ./cluster_jobs/__init__.py \
    ./cluster_jobs/benchmark_workers.py \
    ./cluster_jobs/cluster_config.py \
    ./cluster_jobs/cluster_smoke_test.py \
    ./cluster_jobs/combine_shards.py \
    ./cluster_jobs/patch_ui.py \
    ./cluster_jobs/ui_cluster_run.py \
    ./cluster_jobs/run_cluster.sbatch \
    ./cluster_jobs/run_shard.sbatch \
    ./roboaixrf/main.py \
    ./python_code/roboai_xrf_frontend/components/run_page.py \
    ./python_code/roboai_xrf_frontend/requirements.txt \
    ./tests/test_cluster_jobs.py \
    "$TARGET"

echo "Deployment transfer completed: $TARGET"

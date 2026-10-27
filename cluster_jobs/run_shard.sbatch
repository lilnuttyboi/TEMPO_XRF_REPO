#!/usr/bin/env bash
set -euo pipefail

root=$1
total=$2
workers=$3
shards=$4
seed_base=$5
id=${6:-${SLURM_ARRAY_TASK_ID:-${SLURM_PROCID:?A shard ID is required}}}
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

if [[ ${SLURM_CPUS_PER_TASK:-0} -ne $workers ]]; then
    echo "Requested $workers workers but SLURM_CPUS_PER_TASK=${SLURM_CPUS_PER_TASK:-unset}." >&2
    exit 2
fi

events=$((total / shards + (id < total % shards ? 1 : 0)))
out="$root/shard-$id"
success="$out/success.json"
failure="$out/failure.json"

# Geant4 also writes viewer files relative to the process working directory.
mkdir -p "$out/work/launch" "$out/roboaixrf/vis/public"
rm -f "$success" "$failure" "$out/simulation.root"
cp "$root/config.json" "$out/config.json"

seed1=$((seed_base + id * 2))
seed2=$((seed1 + 1))
progress=$((events / 100))
((progress > 0)) || progress=1

awk -v n="$events" -v t="$workers" -v s1="$seed1" \
    -v s2="$seed2" -v p="$progress" '
  $1 == "/random/setSeeds"     { next }
  $1 == "/run/numberOfThreads" { print "/run/numberOfThreads " t; next }
  $1 == "/run/initialize"      { print "/random/setSeeds " s1 " " s2; print; next }
  $1 == "/run/printProgress"   { print "/run/printProgress " p; next }
  $1 == "/run/beamOn"          { print "/run/beamOn " n; next }
  { print }
' "$root/run.mac" > "$out/run.mac"

read -r macro_threads macro_seeds macro_events < <(
    awk -v t="$workers" -v s1="$seed1" -v s2="$seed2" -v n="$events" '
      $1 == "/run/numberOfThreads" && $2 == t && NF == 2 { threads++ }
      $1 == "/random/setSeeds" && $2 == s1 && $3 == s2 && NF == 3 { seeds++ }
      $1 == "/run/beamOn" && $2 == n && NF == 2 { events++ }
      END { print threads + 0, seeds + 0, events + 0 }
    ' "$out/run.mac"
)
if [[ $macro_threads -ne 1 || $macro_seeds -ne 1 || $macro_events -ne 1 ]]; then
    echo "Shard $id macro validation failed: threads=$macro_threads seeds=$macro_seeds beamOn=$macro_events" >&2
    exit 2
fi

geant4_setup=${TEMPO_XRF_GEANT4_SETUP:-/srv/cluster/software/geant4-11.4.2/bin/geant4.sh}
if [[ ! -r $geant4_setup ]]; then
    echo "Geant4 setup script is not readable: $geant4_setup" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$geant4_setup"

affinity=$(awk '/^Cpus_allowed_list:/ {print $2}' /proc/self/status)
node=${SLURMD_NODENAME:-$(hostname -s)}
echo "Shard $id: node=$node, events=$events, workers=$workers, cpus=$SLURM_CPUS_PER_TASK, affinity=$affinity, seeds=$seed1,$seed2"

cd "$out/work/launch"
start_ns=$(date +%s%N)
set +e
"$repo/geant4_code/build/sim" "$out/config.json" "$out/run.mac"
status=$?
set -e
end_ns=$(date +%s%N)
elapsed_ns=$((end_ns - start_ns))

if [[ $status -ne 0 || ! -f $out/simulation.root ]]; then
    printf '{"id":%d,"node":"%s","events":%d,"workers":%d,"affinity":"%s","exit_code":%d,"elapsed_ns":%d}\n' \
        "$id" "$node" "$events" "$workers" "$affinity" "$status" "$elapsed_ns" > "$failure.tmp"
    mv "$failure.tmp" "$failure"
    echo "Shard $id failed with status $status; simulation.root present: $([[ -f $out/simulation.root ]] && echo yes || echo no)" >&2
    [[ $status -ne 0 ]] && exit "$status"
    exit 3
fi

printf '{"id":%d,"node":"%s","events":%d,"workers":%d,"affinity":"%s","seeds":[%d,%d],"exit_code":0,"elapsed_ns":%d}\n' \
    "$id" "$node" "$events" "$workers" "$affinity" "$seed1" "$seed2" "$elapsed_ns" > "$success.tmp"
mv "$success.tmp" "$success"
echo "Shard $id completed successfully in $elapsed_ns ns"

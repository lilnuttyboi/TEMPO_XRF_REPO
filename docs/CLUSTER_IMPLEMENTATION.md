# TEMPO XRF cluster implementation and operations

Last verified on the real cluster: 2026-10-02.
Dynamic queue implementation validated locally: 2026-10-27.

This document explains the cluster work added to TEMPO XRF, why each change was
made, how a simulation now travels from the Streamlit UI through Slurm and
Geant4, where results are stored, how the deployment is maintained, and what was
verified on the real eight-node cluster.

For short, current facts and unresolved questions, also read
[`CLUSTER_CONTEXT.md`](CLUSTER_CONTEXT.md). `AGENTS.md` requires that context to
be read and updated before future cluster, Slurm, or deployment changes.

## Verified production layout

The authoritative production checkout is:

```text
/srv/cluster/TEMPO_XRF_REPO
```

It is exported by management over NFS and is visible at the same path on all
eight compute nodes. There is no need to copy the repository separately to each
node.

The old or incomplete copy at `/home/mnode/TEMPO_XRF_REPO` is not the production
checkout. In particular, its frontend and copied virtual environment were
incomplete. Do not run, deploy, or install packages into that copy.

The verified cluster is:

- Management host: `mnode-01`, `192.168.10.3`.
- Partition: `debug`.
- Nodes: `node-01-001` through `node-01-004` and `node-02-001` through
  `node-02-004`.
- Per node: 40 physical cores, 80 logical CPUs, and 191544 MiB reported memory.
- Requested job memory: `--mem=185000` per selected node.
- Partition memory policy: `DefMemPerCPU=4500` and `MaxMemPerNode=185000`.
  TEMPO XRF's explicit per-node request overrides the partition default.
- Rocky Linux 9.8, Slurm 26.05.4, Geant4 11.4.2 with multithreading.
- Shared Geant4: `/srv/cluster/software/geant4-11.4.2`.
- Simulation executable: `/srv/cluster/TEMPO_XRF_REPO/geant4_code/build/sim`.
- Management Python environment:
  `/srv/cluster/TEMPO_XRF_REPO/.venv` using Python 3.11.
- Frontend: `python_code/roboai_xrf_frontend`.

## Why it was not working and how we got it working

Several independent problems appeared at the same time. This made the cluster
look completely broken, but NFS and the basic Slurm allocation mechanism were
still working. The investigation separated repository/deployment mistakes, one
real launcher bug, a management DNS problem, and inconsistent packages on three
compute nodes.

| Symptom | Actual cause | Repair | Proof |
|---|---|---|---|
| Starting from `/home/mnode/TEMPO_XRF_REPO` reported that frontend `main.py` was missing. | A recursive `scp` had created a separate, incomplete, partly root-owned copy in the home directory. It was not the NFS production checkout. | Kept `/srv/cluster/TEMPO_XRF_REPO` as the only production checkout and stopped using the home copy. The home launcher now delegates to the shared launcher. | `/srv/cluster/TEMPO_XRF_REPO/python_code/roboai_xrf_frontend/main.py` existed and the offline launcher preflight passed there. |
| Copying the laptop folder produced `.venv/lib64` permission errors and an unusable environment. | A Python virtual environment was recursively copied between different machines/Python installations. Virtual environments are not portable deployment artifacts. | Created a clean Python 3.11 environment at `/srv/cluster/TEMPO_XRF_REPO/.venv`, installed the required packages, added `uproot>=5` to requirements, and verified all imports. | Streamlit, NumPy, Pandas, Plotly, Pydantic, SpekPy, uproot, Awkward, xraylib, Matplotlib, h5py, and tqdm imported successfully. |
| Port 8502 still displayed the old five-node UI. | The running `/srv` checkout had not yet received all laptop source changes; an attempted deployment command was also run from the wrong machine/path. | Added `deploy_to_mnode.sh` and selectively synchronized only the intended source files from the laptop to `/srv/cluster/TEMPO_XRF_REPO`, with backups. | The UI changed from the fixed “five nodes” checkbox to Single node/Distributed modes and a live node selector. |
| The new UI fell back to its documented node list because `sinfo`, `squeue`, and state-reading commands timed out. | Slurm successfully answered the RPC, but the client then stalled resolving `mnode-01.ad.local`. `/etc/hosts` had no management-host entry, and lookup fell through to slow external DNS. | Backed up `/etc/hosts` and added `192.168.10.3 mnode-01 mnode-01.ad.local`. | Five consecutive `sinfo` checks changed from eight-second timeouts to successful 0.00–0.01 second responses listing all eight nodes. |
| Jobs 147 and 148 allocated `node-01-001` but immediately exited 127, looking for `/var/spool/slurmd/cluster_jobs/run_shard.sbatch`. | This was the real cluster-launcher code bug. Slurm copies the controller batch script into its spool, so deriving the repository from the controller's `BASH_SOURCE` returned `/var/spool/slurmd`, not the NFS checkout. | Passed `/srv/cluster/TEMPO_XRF_REPO` as an explicit controller argument and added a regression test that executes the controller from a simulated spool path. | All 14 tests passed on management, including the spool-path regression. Later jobs launched the shared shard script correctly. |
| The first eight-node smoke run completed five shards but failed on `node-02-002`, `node-02-003`, and `node-02-004` with `libxerces-c-3.2.so` missing. | Those three nodes lacked the EPEL `xerces-c` runtime package. The shared executable and Geant4 environment were the same on every node. | Enabled `epel-release` and installed `xerces-c-3.2.5-1.el9` on the missing nodes. | `ldd` resolved Xerces from `/lib64/libxerces-c-3.2.so` on all nodes, and the next smoke test passed on all eight. |
| The failed smoke run produced a five-shard merge, raising concern that incomplete data might appear as a result. | This was expected diagnostic behavior, not another fault. Successful shards are retained and may be merged for inspection. | No repair was required; the publication gate was checked. | The partial merge stayed inside its timestamped cluster folder and was not published as the run's top-level `simulation.root`. |

The important distinction is that only the Slurm spool-path problem was a new
launcher defect. The other failures were caused by using the wrong checkout,
copying an incompatible virtual environment, delayed source deployment,
management hostname resolution, or inconsistent compute-node packages.

The working sequence was therefore:

1. Establish `/srv/cluster/TEMPO_XRF_REPO` as the single NFS-shared checkout.
2. Rebuild a clean management Python 3.11 environment and verify imports.
3. Selectively deploy the source-controlled cluster integration.
4. Trace the frontend through the Python launcher, Slurm controller, shard
   script, Geant4 executable, and merge rather than assuming where it failed.
5. Fix management hostname resolution so Slurm inventory queries return
   immediately.
6. Pass the repository explicitly through Slurm's spooled controller script.
7. Normalize the Xerces runtime package across all compute nodes.
8. Run the 14 automated tests, the full eight-node smoke test, and the paired
   40-versus-80 benchmark.

Final confirmation came from Slurm job 154: all eight distinct nodes completed
100 events each, all 800 events were merged, and the smoke tool printed `PASS`.
Jobs 155 through 160 then completed the performance benchmark and measured 80
workers per node as 1.265x the throughput of 40 workers for the tested workload.

## End-to-end execution path

```text
Streamlit Build & run page
        |
        v
RoboAiXrfSimulation.start_run()
        |
        v
cluster_jobs/ui_cluster_run.py
        |
        v
sbatch: one-node job array, throttled to selected-node count
        |
        v
run_cluster.sbatch: one array chunk on whichever selected node is free
        |
        v
run_shard.sbatch: private macro, seeds, folder, and Geant4 process
        |
        v
success.json + shard simulation.root
        |
        v
combine_shards.py validates and merges successful shards
        |
        v
complete merge is published as the run's simulation.root
```

The distributed implementation uses a Slurm job array instead of reserving every
node in one gang allocation. Automatic queue sizing targets approximately 60
seconds per chunk using representative timing learned from matching completed
runs, and is capped at eight chunks per selected node. A measured queue benchmark
for the same configuration, worker count, exact selected-node set, and event
scale within a factor of two overrides that heuristic with its winning chunk
count. Until matching evidence exists it uses a conservative production-scale
fallback. Manual mode allows 1--32 chunks per selected node. Concurrent array
elements are limited to the
number of selected nodes. Each array element requests one exclusive node. When a
fast node completes, Slurm can place another pending chunk there; a slower node
naturally completes fewer chunks.

### Node selection

`cluster_jobs/cluster_config.py` provides the shared node-selection rules.

Discovery order is:

1. `TEMPO_XRF_CLUSTER_NODES`, if explicitly set.
2. Live `sinfo` discovery from the selected Slurm partition.
3. The documented eight-node fallback inventory if live discovery fails.

The partition defaults to `debug` and can be overridden with
`TEMPO_XRF_SLURM_PARTITION`. Live discovery means future nodes added to Slurm
appear in the UI without another five-node or eight-node code change.

Discovery is cached for 30 seconds. A node appears only after Slurm has
registered it and included it in the selected partition; physically connecting
a server does not register or provision it. The current discovery command reads
node names but does not filter on `IDLE`, `DOWN`, or `DRAIN`, so appearing in the
selector is not a readiness guarantee. In an existing browser session the new
option may also need to be selected manually; a fresh Distributed widget selects
all discovered options by default.

Before production use, a new node must have the same private-network
reachability, Munge/Slurm configuration, `/srv/cluster` NFS mount, Geant4
runtime dependencies (including Xerces), compatible executable/runtime, CPU
topology, and sufficient memory. It should be `IDLE` in `debug` and pass a
targeted smoke test.

Node lists are expanded and validated. Values such as
`node-01-001,node-01-002` are accepted. A bracket expression such as
`node-01-[001-004]` is not accepted by the Python API because the launcher
records and validates each exact selected host.

The UI provides:

- **Single node**: choose one compute node; the same safe shard-and-merge path is
  used with one chunk by default.
- **Distributed**: choose any number of nodes; all discovered nodes are selected
  by default. **Queue sizing** defaults to Automatic. Manual mode accepts 1
  through 32 chunks per selected node. The UI shows the saved event total,
  resulting chunk count, approximate events and estimated duration per chunk,
  and maximum concurrency before submission.

Automatic compares the active run's `config.json` with copied configurations in
its immutable `cluster_runs` history. It uses up to three recent complete runs
with the same worker count only when every marker validates and the median chunk
lasted at least 15 seconds. The event-weighted per-node rate is shown in the UI.
This prevents startup-dominated smoke tests and old 100,000-event benchmarks from
driving production queue depth.

### Event splitting

The UI's **Beam-on events** value is the total for the complete run, not the
number per node.

For `E` events and `N` chunks, the launcher calculates `q, r = divmod(E, N)`.
The first `r` chunks receive `q + 1` events and the remaining chunks receive
`q`. This guarantees that:

- every event is assigned exactly once;
- the sum of shard events equals the requested total;
- shard sizes differ by at most one event.

The chunk count is `min(events, selected_nodes * chunks_per_node)`, so no empty
chunk is created. More chunks improve load balancing, but chunks that finish in
only a few seconds can lose throughput to Slurm and Geant4 startup overhead.

### Independent random seeds and folders

Each run receives a seed base. Shard `i` receives:

```text
seed1 = seed_base + 2*i
seed2 = seed1 + 1
```

The allowed range is checked before submission, and every seed in a distributed
run is unique.

Every shard receives its own directory:

```text
cluster_runs/<run-id>/shard-0/
cluster_runs/<run-id>/shard-1/
...
```

Each directory contains its own copied `config.json`, rewritten `run.mac`, work
directory, `simulation.root`, and success or failure marker. This prevents nodes
from overwriting each other's outputs.

### Worker count, CPUs, affinity, and memory

The frontend now labels this setting **Workers per node** and accepts 1 through
80 workers.

- Up to 40 workers requests the corresponding CPUs with
  `--hint=nomultithread`. Forty means one Geant4 worker per physical core.
- Above 40 workers requests the corresponding logical CPUs with
  `--hint=multithread`. Eighty uses both hardware threads on all 40 cores.
- `--cpus-per-task` always equals the Geant4 worker count.
- The shard step applies `--cpu-bind=verbose,threads` and records the actual
  allowed CPU list in its marker.
- Every selected node is explicitly requested with `--mem=185000`, replacing
  the old 16 GiB request.
- Every array element is exclusive, uses one task on one selected node, and uses
  the complete selected-node list as its placement allowlist.

The eight-node smoke test observed 40 CPUs per shard with affinity `0-39` on the
captured task logs. The 40-versus-80 benchmark successfully ran both allocation
modes.

### Per-shard macro rewriting

`cluster_jobs/run_shard.sbatch` rewrites the copied macro for that shard. It
ensures exactly one occurrence of each of the following:

```text
/run/numberOfThreads <workers-per-node>
/random/setSeeds <seed1> <seed2>
/run/beamOn <events-for-this-shard>
```

It also adjusts `/run/printProgress`, validates the rewritten macro, sources the
shared Geant4 setup, records the node and CPU affinity, runs the shared
executable, and measures elapsed nanoseconds.

A shard creates `success.json` only when Geant4 exits with status zero and its
`simulation.root` exists. Otherwise it creates `failure.json` and exits with an
error.

### Validation, merging, and publication

`cluster_jobs/ui_cluster_run.py` waits for the Slurm job, follows shard progress,
and validates every success marker against the original plan:

- shard ID;
- node name;
- assigned event count;
- worker count;
- seed pair;
- zero exit status;
- existence of `simulation.root`;
- execution on a selected node.

Repeated node use is expected: it proves that completed nodes can pick up more
queued work. The run manifest records a per-node summary of completed chunks,
events, and cumulative Geant4 elapsed time.

`cluster_jobs/combine_shards.py` reads only success-marked shard outputs. It
merges the `MyTree` fields `EventId`, `rootId`, `Energy`, and `Weight` with
`uproot`. Shard-local event IDs are offset so the merged event space is
contiguous.

The merge is first written to temporary files and then renamed. Existing merge
outputs are not silently overwritten.

If every shard succeeds, `combined_response.root` is published as the run's
top-level `simulation.root`. If any shard fails, successful shards may be merged
for diagnosis, but that partial artifact remains inside its timestamped
`cluster_runs` folder and is never published as the completed UI result.

## Source changes by file

### `cluster_jobs/cluster_config.py`

New shared configuration module containing:

- live node discovery and the eight-node fallback;
- explicit node parsing, validation, and deduplication;
- future-node environment overrides;
- exact event splitting;
- configurable queue sizing and concurrency calculation;
- independent seed generation;
- worker validation;
- 40/80 topology flags;
- the fixed 185000 memory request;
- construction of the throttled one-node Slurm array command.

### `cluster_jobs/ui_cluster_run.py`

Replaced the fixed five-node launcher with the main distributed controller. It:

- creates immutable timestamped run folders;
- snapshots `config.json` and `run.mac`;
- writes and updates `run_manifest.json` atomically;
- submits and monitors Slurm;
- aggregates progress from per-task logs;
- validates every planned chunk and allows selected nodes to be reused;
- records per-node chunk, event, and elapsed-time totals;
- invokes the safe merge;
- publishes only complete results;
- cancels the active Slurm job if the frontend process receives `SIGTERM`.

### `cluster_jobs/run_cluster.sbatch`

This is the array-element controller executed inside each one-node allocation.
It checks `SLURM_ARRAY_TASK_ID` and starts one CPU-bound `srun` chunk. The array
throttle, not this script, limits the number running concurrently.

The shared repository path is passed as an explicit argument. This is important
because Slurm executes an `sbatch` script from `/var/spool/slurmd`; deriving the
repository from the controller script's `BASH_SOURCE` incorrectly points at the
spool directory.

### `cluster_jobs/run_shard.sbatch`

This is the node-local shard launcher. It provides private inputs and folders,
event and seed rewriting, macro validation, CPU/allocation checks, Geant4 setup,
timing, diagnostics, and atomic success/failure markers.

### `cluster_jobs/combine_shards.py`

New validated ROOT merger. It refuses unmarked shards, checks macros against
markers, offsets event IDs, writes merge metadata, and records whether the merge
is complete.

### `cluster_jobs/cluster_smoke_test.py`

Real-cluster test command. It discovers or accepts nodes, submits two queued
chunks per selected node by default, runs through the production launcher
without publishing over a user result, verifies all requested events, and prints
the per-node chunk distribution. It does not require every selected node to run
work because selected nodes may be unavailable and array chunks may reuse faster
eligible nodes.

### `cluster_jobs/benchmark_workers.py`

New repeatable benchmark. It runs 40 and 80 workers on the same exclusive node,
uses paired seeds, alternates execution order to reduce cache/temperature bias,
calculates median events per second, and saves every measurement to JSON.

### `cluster_jobs/benchmark_chunks.py`

Production queue-depth benchmark. It runs the same total workload with a
configurable matrix such as 70/80 workers and 1/2/4 chunks per selected node. It
alternates order across repetitions, does not publish over the current result,
measures end-to-end controller throughput including merge overhead, retains each
run's job ID and node distribution, and saves the per-worker and overall measured
winners to JSON. The UI accepts a per-worker winner only when the configuration
hash, exact selected-node set, and event total within a factor of two also match.

### `cluster_jobs/patch_ui.py`

The old helper formerly encoded five-node assumptions. It no longer modifies
source files. It now verifies that the source-controlled frontend, launcher, and
cluster integration are present and explains that the obsolete patch must not be
reapplied.

### `roboaixrf/main.py`

`RoboAiXrfSimulation.start_run()` now:

- accepts an integer node count or explicit node sequence;
- accepts and forwards the requested chunks per node;
- validates 1 through 80 workers;
- locates the repository safely;
- launches `cluster_jobs/ui_cluster_run.py` instead of directly executing one
  local `./sim` process;
- forwards total events, workers, and selected nodes;
- removes the previous top-level `simulation.root` before a new quantitative
  run so a stale result cannot be mistaken for the new result;
- continues to expose progress and cancellation through the existing background
  process interface.

### `python_code/roboai_xrf_frontend/components/run_page.py`

The Build & run page now:

- calls the shared node-discovery code;
- caches inventory for 30 seconds;
- displays Single node and Distributed modes;
- allows a configurable node selection instead of a fixed five-node checkbox;
- defaults to **Automatic (recommended)** queue sizing, uses an exact matching
  queue benchmark when available, otherwise learns from matching representative
  completed runs, caps heuristic Automatic at eight chunks per node, and retains
  a 1--32 chunks-per-selected-node Manual override;
- shows the saved event total, calculated chunks, event size, estimated chunk
  duration, and maximum concurrency;
- defaults distributed selection to every discovered node;
- displays the inventory source and any discovery warning;
- labels the thread setting Workers per node and caps it at 80;
- explains the 40 physical-core and 80 hardware-thread choices;
- passes the exact selected nodes to `start_run()`;
- rejects an empty distributed selection.

### `python_code/roboai_xrf_frontend/requirements.txt`

Added `uproot>=5`, which is required for reading and merging ROOT files without
using the ROOT C++ runtime from the management Python process.

### `start_tempo_xrf.sh`

New offline-safe frontend launcher. It:

- never runs Git, Pip, DNF, NPM, Curl, or another downloader;
- finds the authoritative repository or accepts `TEMPO_XRF_REPO_DIR`;
- uses the shared Geant4 setup or `TEMPO_XRF_GEANT4_SETUP`;
- uses an existing virtual environment or `TEMPO_XRF_VENV`;
- exports the repository on `PYTHONPATH`;
- disables Streamlit usage telemetry;
- validates required imports;
- supports `--check` without starting a server;
- defaults to `0.0.0.0:8502`, with an optional port argument.

### `deploy_to_mnode.sh`

New guarded, selective deployment helper. It:

- must run on the laptop and refuses to run on `mnode-01`;
- supports `--dry-run`;
- uses `rsync --relative` so paths remain correct;
- transfers only the cluster integration files;
- preserves replaced management files under
  `.deployment_backups/before_dynamic_cluster`;
- avoids recursively copying a virtual environment, build tree, or run output.

### `tests/test_cluster_jobs.py`

Seventeen automated tests cover:

- the complete eight-node fallback;
- future nodes supplied through the environment;
- node validation and deduplication;
- exact event splitting including remainders;
- independent seeds;
- 40-worker physical-core allocation;
- 80-worker hardware-thread allocation;
- removal of the old 16 GiB request;
- throttled job-array submission with one node per chunk;
- queue sizing when chunks outnumber nodes or events are scarce;
- explicit shared repository forwarding;
- success-marker-only discovery;
- macro validation;
- shard macro rewriting and marker creation;
- publication only after all shards validate;
- repeated-node queue completion and per-node accounting;
- retention but non-publication of partial merges;
- the Slurm spool-path regression.

`tests/__init__.py` makes the repository test directory a regular package. This
prevents an unrelated `tests` package installed in the management virtualenv from
shadowing it when the suite is invoked as `python -m unittest
tests.test_cluster_jobs`.

### `AGENTS.md` and `docs/CLUSTER_CONTEXT.md`

`AGENTS.md` now directs future cluster work to the live context document.
`CLUSTER_CONTEXT.md` contains the concise current cluster facts, verified paths,
operating commands, measurements, and remaining uncertainties.

### Changes deliberately not included in selective deployment

The working tree also contains pre-existing or generated changes that are not
blindly deployed by `deploy_to_mnode.sh`:

- `python_code/roboai_xrf_frontend/builder.py` has a local change that resolves
  and creates the run directory. It was treated as an existing user edit because
  management could have its own active builder/package version.
- `build/sim` is a generated Geant4 executable.
- files under `python_code/roboai_xrf_frontend/runs/` are generated inputs,
  results, plots, benchmarks, and test artifacts.
- `*.before_cluster` files are preservation copies, not active source.

These files must be reconciled or regenerated intentionally rather than being
overwritten by a broad recursive copy.

## Cluster and operating-system repairs

Some failures were outside the Python/Bash implementation.

### Management hostname resolution

Slurm commands initially appeared to hang. A syscall trace proved that `sinfo`
successfully contacted `192.168.10.3:6817`, authenticated through Munge, sent its
request, and received the controller response. It then stalled resolving
`mnode-01.ad.local` through external DNS.

Management had no `mnode-01` entry in `/etc/hosts`, NSS used
`files dns myhostname`, and NetworkManager supplied the `ad.local` search domain.
The hosts file was backed up to:

```text
/etc/hosts.before-tempo-slurm-20261002
```

This entry was added:

```text
192.168.10.3 mnode-01 mnode-01.ad.local
```

Afterward, five consecutive `sinfo` calls completed in 0.00 to 0.01 seconds and
listed all eight nodes.

### Xerces runtime consistency

The first real smoke test reached all eight nodes, but
`node-02-002`, `node-02-003`, and `node-02-004` could not load
`libxerces-c-3.2.so`. The other five nodes had the EPEL 9 Xerces runtime.

The missing nodes were repaired with:

```bash
sudo dnf install -y epel-release
sudo dnf install -y xerces-c
```

All now have `xerces-c-3.2.5-1.el9.x86_64`. `xerces-c-devel` is not required to
execute the already-built simulation binary. Geant4 libraries resolve after
sourcing `/srv/cluster/software/geant4-11.4.2/bin/geant4.sh`.

This dependency belongs in repeatable node provisioning. Compute-node Internet
access is not required for normal simulation execution once packages are
installed; cluster traffic and NFS use the private `eno1` network.

## Bugs found and fixed during deployment

### Recursive copy and wrong checkout

A recursive `scp` into `/home/mnode/TEMPO_XRF_REPO` produced permission errors,
an incomplete frontend, and an unusable copied virtual environment. The fix was
to retain `/srv/cluster/TEMPO_XRF_REPO` as the single NFS-shared production
checkout and deploy only selected source files with `rsync`.

Virtual environments are not portable deployment artifacts and must not be
copied between laptop and management.

### Slurm spool path

Jobs 147 and 148 allocated a compute node but exited 127 because the controller
derived `/var/spool/slurmd` as the repository and looked for:

```text
/var/spool/slurmd/cluster_jobs/run_shard.sbatch
```

Slurm had copied the controller script into its spool, so `BASH_SOURCE` could not
identify the source checkout. The repository is now passed as an explicit final
controller argument. A regression test runs the controller from a simulated
spool directory and verifies that it launches the shard from the shared checkout.

### Partial-result safety

Smoke job 150 completed five shards and failed three because Xerces was missing.
The five successful shards were merged only into an unpublished diagnostic
artifact. No incomplete top-level `simulation.root` was created. This real
failure verified the publication safety gate.

## Verification results

### Automated tests

The original 14 cluster tests passed on the laptop and in the management virtual
environment. The dynamic queue raised this to 17 tests, which passed on
management before and after selective production deployment. Adaptive queue
sizing and the queue-depth benchmark bring the current local suite to 23; rerun
those 23 on management after selectively deploying this update:

```bash
cd /srv/cluster/TEMPO_XRF_REPO
source .venv/bin/activate
export PYTHONPATH=/srv/cluster/TEMPO_XRF_REPO
python -m unittest -v tests.test_cluster_jobs
```

### Eight-node smoke test

Slurm job 154 passed using all eight nodes, 40 workers and 100 events per node:

- 800 requested and simulated events;
- eight distinct successful nodes;
- one concurrent shard per node;
- 49 merged `MyTree` rows;
- successful validated merge;
- smoke artifacts:

```text
python_code/roboai_xrf_frontend/runs/full_xrf_example/cluster_runs/
20261002_161157_c47e02be_cluster-smoke/
```

The smoke test intentionally uses `publish=False`, so it cannot overwrite a
user's top-level result. This was the pre-queue, one-shard-per-node smoke test.
After deploying the array implementation, rerun the command in **Repeating
validation**; it now submits two chunks per selected node and reports the actual
per-node distribution.

### 40-versus-80 benchmark

Jobs 155 through 160 ran three paired repetitions of 100000 events on
`node-01-001`. The order alternated and each pair used the same seed base.

Measured medians:

| Workers | Median throughput |
|---:|---:|
| 40 | 22568.57 events/s |
| 80 | 28557.48 events/s |

The measured 80/40 ratio was **1.265x**, so 80 workers were approximately 26.5%
faster. The production recommendation is therefore **80 workers per node** for
maximum measured throughput. Forty remains available when physical-core-only
operation is desired.

The complete benchmark data is stored at:

```text
python_code/roboai_xrf_frontend/runs/full_xrf_example/benchmarks/
20261002_161346_workers.json
```

The 100000-event runs lasted only a few seconds. For a workload-independent
long-run characterization, repeat the same benchmark with enough events for each
timed simulation to run for several minutes. The result supports the relative
80-versus-40 worker choice, but is no longer used as the absolute queue-sizing
rate because startup dominated those short runs.

### Production-scale queue observation

Slurm job 805 completed one billion events on all 20 nodes with 70 workers and
four chunks per node. All nodes processed exactly four chunks, and their total
Geant4 times had only 1.15% coefficient of variation. Median chunk duration was
47.523 seconds and event-weighted per-node throughput was 262,831.62 events/s.
The dynamic queue therefore did not redistribute work in this run; reducing
repeated initialization and merge inputs is a stronger optimization hypothesis
than adding more chunks. A controlled 70/80-worker, 1/2/4-chunk matrix is the
next measurement. The UI progress meter's reported peak or smoothed event rate
is not final wall throughput; use benchmark elapsed time and manifest timings.

## Starting the frontend

Start only from the shared production checkout:

```bash
cd /srv/cluster/TEMPO_XRF_REPO
./start_tempo_xrf.sh --check
./start_tempo_xrf.sh
```

The checked-in launcher starts Streamlit on port 8502. Keep that terminal open;
`Ctrl+C` stops the frontend.

A convenient home wrapper can contain:

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

exec /srv/cluster/TEMPO_XRF_REPO/start_tempo_xrf.sh "$@"
```

Save it as `/home/mnode/start_tempo_xrf.sh`, make it executable, and run it from
any directory:

```bash
chmod +x /home/mnode/start_tempo_xrf.sh
~/start_tempo_xrf.sh --check
~/start_tempo_xrf.sh
```

From a laptop, create the SSH tunnel:

```bash
ssh -L 8502:127.0.0.1:8502 mnode@192.168.10.3
```

Then open `http://localhost:8502`.

## Using the UI

1. Configure **Setup**, **X-ray tube**, **Detector**, and **Physics**.
2. Open **Build & run**.
3. Enter a unique descriptive run name.
4. Set Workers per node to 80 for maximum measured throughput, or 40 for one
   worker per physical core.
5. Set Beam-on events to the total desired events across all selected nodes.
6. Click **Save run settings**.
7. Choose **Single node** for a test or **Distributed** and the desired nodes for
   production.
8. For Distributed mode, leave **Queue sizing** on **Automatic (recommended)**
   initially. Confirm that the preview shows the intended saved event total. Use
   Manual mode only when measurements for the current physics configuration show
   that the estimate is unsuitable.
9. Click **1. Build & compile**.
10. Optionally use **2. Open visualization** to check geometry. A visualization
   is not a quantitative run.
11. Click **3. Run Geant4** and leave the frontend process running.
12. After success, open **Results**, enter acquisition and detector-response
    settings, and click **Apply detector response**.
13. Inspect the spectrum, upload a comparison spectrum if desired, and download
    the CSV.

Use a unique run name for each important experiment. Reusing a name preserves
timestamped `cluster_runs`, but a new quantitative run removes and replaces the
top-level `simulation.root` for that name.

## Result storage

Each run is stored under:

```text
/srv/cluster/TEMPO_XRF_REPO/python_code/roboai_xrf_frontend/runs/<RUN_NAME>/
```

The run directory contains:

| Path | Purpose |
|---|---|
| `config.json` | Compiled simulation configuration |
| `run.mac` | Current Geant4 macro |
| `simulation.root` | Latest completely validated and published merge |
| `simulation_spectrum.html` | Detector-response spectrum written by Results |
| `cluster_runs/` | Timestamped history of cluster attempts |
| `benchmarks/` | Benchmark JSON files, when benchmarks are run |

Each `cluster_runs/<run-id>/` can contain:

| Path | Purpose |
|---|---|
| `run_manifest.json` | Requested nodes, queue shape, events, workers, seeds, per-node summary, Slurm command and final status |
| `config.json`, `run.mac` | Immutable run input snapshot |
| `combined_response.root` | Merge of validated successful shards |
| `combined_response.json` | Completeness, row counts and per-shard metadata |
| `logs/slurm-<array>_<chunk>.log` | Per-chunk array/controller/task log |
| `shard-N/simulation.root` | Independent queued-chunk ROOT output |
| `shard-N/success.json` | Valid success marker, node, seeds, affinity and timing |
| `shard-N/failure.json` | Failure marker and timing when execution failed |

Failed attempts are retained for diagnosis. They do not become the run's
top-level `simulation.root`.

List saved run names with:

```bash
find /srv/cluster/TEMPO_XRF_REPO/python_code/roboai_xrf_frontend/runs \
  -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort
```

Inspect a run with:

```bash
RUN_DIR=/srv/cluster/TEMPO_XRF_REPO/python_code/roboai_xrf_frontend/runs/<RUN_NAME>
ls -lh "$RUN_DIR"
ls -lht "$RUN_DIR/cluster_runs"
```

Inspect JSON metadata with:

```bash
python -m json.tool "$RUN_DIR/cluster_runs/<RUN-ID>/run_manifest.json" | less
python -m json.tool "$RUN_DIR/cluster_runs/<RUN-ID>/combined_response.json" | less
```

The Results page currently knows the completed simulation held in the active
Streamlit session. Restarting Streamlit does not delete stored data, but the UI
does not yet provide a historical run browser that reloads an old run folder.
Historical results remain accessible on disk.

## Selective deployment

Run deployment from the laptop checkout, never from management:

```bash
cd /home/nutty/Documents/TEMPO_XRF_REPO
./deploy_to_mnode.sh --dry-run
./deploy_to_mnode.sh
```

This deploys the source-controlled integration to:

```text
mnode@192.168.10.3:/srv/cluster/TEMPO_XRF_REPO/
```

Do not replace this with `scp -r` of the complete repository. A broad copy risks
overwriting management-only edits, the virtual environment, the built Geant4
binary, results, and large generated artifacts.

## Repeating validation

### Offline preflight

```bash
cd /srv/cluster/TEMPO_XRF_REPO
./start_tempo_xrf.sh --check
```

### Unit and integration tests

```bash
cd /srv/cluster/TEMPO_XRF_REPO
source .venv/bin/activate
export PYTHONPATH=/srv/cluster/TEMPO_XRF_REPO
python -m unittest -v tests.test_cluster_jobs
```

### Eight-node smoke test

```bash
python cluster_jobs/cluster_smoke_test.py \
  python_code/roboai_xrf_frontend/runs/full_xrf_example \
  --events-per-node 100 \
  --chunks-per-node 2 \
  --workers 40
```

### Repeatable performance benchmark

```bash
python cluster_jobs/benchmark_workers.py \
  python_code/roboai_xrf_frontend/runs/full_xrf_example \
  --node node-01-001 \
  --events 100000 \
  --repetitions 3 \
  --seed-base 424242
```

### Repeatable queue-depth benchmark

```bash
python cluster_jobs/benchmark_chunks.py \
  python_code/roboai_xrf_frontend/runs/full_xrf_example \
  --events 500000000 \
  --workers 70,80 \
  --chunks 1,2,4 \
  --repetitions 1
```

## Current limitations and maintenance notes

- There is no UI page for browsing and reopening historical run folders.
- Run artifacts are retained indefinitely; there is no automatic retention or
  cleanup policy for `cluster_runs`.
- A reused run name replaces its top-level latest result, although timestamped
  cluster artifacts remain.
- The `debug` partition now defaults to 4500 MiB per CPU and limits jobs to
  185000 MiB per node. TEMPO XRF does not currently scale memory from that
  default: every job explicitly requests 185000 MiB per selected node. Removing
  the explicit request would make 40 CPUs imply 180000 MiB, while 80 logical
  CPUs at the same rate would imply 360000 MiB and conflict with the per-node
  maximum. Keep the explicit full-node request unless a capped dynamic policy is
  deliberately implemented and tested.
- The active/generated Geant4 binary and management builder must not be replaced
  by an unreviewed laptop copy.
- `patch_ui.py` is a verifier only; do not restore its old five-node patching
  behavior.
- The current benchmark clearly favors 80 workers for the tested workload, but
  a longer benchmark is advisable if the event physics or output density changes
  substantially.
- Automatic chunk sizing learns from matching long completed runs, but a changed
  geometry or physics configuration has no representative history at first.
  Very small chunks reduce throughput because every chunk initializes Geant4 and
  creates a ROOT file. Measure representative 1, 2, 4, and 8 chunk-per-node runs
  with `benchmark_chunks.py` when the workload changes materially.
- The dynamic job-array path passed clean-checkout and production-checkout Slurm
  smoke tests, including all 20 nodes in job 597 and the production merger in job
  637. A UI-submitted run remains the final frontend-path check.
- Keep all nodes provisioned consistently, including the EPEL Xerces runtime.
- Keep `mnode-01` resolvable locally through `/etc/hosts`; Slurm client commands
  should not depend on external DNS.

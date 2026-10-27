# TEMPO XRF — RoboAI XRF on a Slurm cluster

This branch adapts the existing RoboAI XRF / Geant4 application to a shared
Linux compute cluster. Users configure X-ray fluorescence simulations in
Streamlit; Slurm runs event batches on compute nodes; Python merges accepted
detector results for the existing Results page.

We started with a custom five-node management installation, expanded it to eight
nodes, and now have recorded successful runs on **20 nodes**. The work includes
configurable selection, matching Geant4/Slurm resources, a dynamic job queue,
isolated outputs and seeds, completion checks, offline startup, selective
deployment, and measured automatic chunk sizing.

**Status recorded on 2026-10-27:** 23 automated tests passed on the laptop and
management; real queue tests passed; the production benchmark found **80 workers
and one chunk per selected node** fastest among the tested combinations on
20 nodes. This is a workload-specific measurement, not a universal optimum.

The underlying simulation comes from
[TEMPO_XRF_REPO](https://github.com/Minmyatngwe/TEMPO_XRF_REPO/tree/front).
Cluster work is maintained on `cluster-job-queue-2026-10-27` in the
[fork](https://github.com/lilnuttyboi/TEMPO_XRF_REPO/tree/cluster-job-queue-2026-10-27).
Management also contained independent application changes that we preserved.
Not every C++ or frontend change originated in this cluster adaptation.

## Contents

- [What changed](#what-changed)
- [Cluster and paths](#cluster-and-paths)
- [How a simulation runs](#how-a-simulation-runs)
- [Slurm queue, workers and memory](#slurm-queue-workers-and-memory)
- [Automatic chunk sizing](#automatic-chunk-sizing)
- [Starting and using the UI](#starting-and-using-the-ui)
- [Results and detailed measurements](#results-and-detailed-measurements)
- [Everything required to get it working](#everything-required-to-get-it-working)
- [Source changes and Git history](#source-changes-and-git-history)
- [Validation and repeatable benchmarks](#validation-and-repeatable-benchmarks)
- [Deployment and adding nodes](#deployment-and-adding-nodes)
- [Bottlenecks and future work](#bottlenecks-and-future-work)
- [Accuracy and current limits](#accuracy-and-current-limits)

This README is the branch's main operations and history guide.
[CLUSTER_IMPLEMENTATION.md](docs/CLUSTER_IMPLEMENTATION.md) adds file-by-file
detail; [CLUSTER_CONTEXT.md](docs/CLUSTER_CONTEXT.md) retains dated evidence and
uncertainties for future work. Existing [simulation documentation](docs/index.md)
covers the scientific application. `AGENTS.md` requires future cluster changes
to consult and update the context.

## What changed

The original application prepares a Geant4 configuration and macro and launches
an executable. Management already had custom distribution across five nodes;
a fresh upstream clone alone would have lost part of that working installation.

| Before | This branch |
|---|---|
| Fixed five-node checkbox | Single node or explicit selection from Slurm inventory |
| One part per node in the initial distributed version | Configurable queue of chunks, with one exclusive chunk per allocated node |
| Physical-core-only Slurm hint | Geant4 workers, Slurm CPUs and affinity agree for 40 or 80 workers |
| `--mem=16G` | Explicit `--mem=185000` per one-node job |
| Ad hoc copying and patches | Versioned integration, comparison snapshots, selective transfer and backups |
| Fragile home/environment paths | Shared production checkout and offline launcher |
| Guessed worker/chunk settings | Paired tests and end-to-end benchmarks |
| Long diagnostic UI paragraphs | One concise automatic recommendation; detail in this guide |

No MPI conversion or shared cross-node Geant4 memory was needed. Each node runs
a separate simulator process over a subset of events. Inside a node Geant4 uses
its multithreaded master/worker model, described in
[Geant4's threading documentation](https://geant4.web.cern.ch/documentation/pipelines/master/bftd_html/ForToolkitDeveloper/OOAnalysisDesign/Multithreading/mt.html).

## Cluster and paths

These are recorded installation facts, not a live inventory query:

| Item | Recorded value |
|---|---|
| Management/login host | `mnode@mnode-01`, private address `192.168.10.3` |
| Laptop development checkout | `/home/nutty/Documents/TEMPO_XRF_REPO` |
| Production checkout shared through NFS | `/srv/cluster/TEMPO_XRF_REPO` |
| Clean queue-validation checkout | `/srv/cluster/TEMPO_XRF_QUEUE_TEST` |
| Compute nodes | `node-01-[001-004]` through `node-05-[001-004]`: 20 nodes |
| CPU topology/node | 2 sockets × 20 physical cores × 2 hardware threads: 40 cores, 80 logical CPUs |
| Total across 20 nodes | 800 physical cores, 1,600 logical CPUs |
| Reported memory | 191544 MiB on 19 nodes; 257064 MiB on `node-03-003` |
| Slurm partition | `debug`; all 20 reported IDLE in the October 27 check |
| XRF memory request | 185000 MiB per allocated node, regardless of worker count |
| Recorded software | Rocky Linux 9.8, Slurm 26.05.4, Geant4 11.4.2 with multithreading |
| Shared Geant4 | `/srv/cluster/software/geant4-11.4.2` |
| Simulation executable | `/srv/cluster/TEMPO_XRF_REPO/geant4_code/build/sim` |
| Management Python | `/srv/cluster/TEMPO_XRF_REPO/.venv`, Python 3.11 |
| Frontend | `python_code/roboai_xrf_frontend/main.py` |
| Checked-in launcher's default port | 8502 |

**NFS shares files; Slurm assigns jobs; Geant4 calculates.** Management runs the
UI and controller/merger, while compute nodes run simulator processes. All see
the same `/srv/cluster` path, so the application is not copied to each node.

The stale `/home/mnode/TEMPO_XRF_REPO` copy is not production. A home start script
is only a wrapper around the shared checkout. Management was also reached from
the laptop at `10.103.8.120` during deployment; use the address reachable from
your current network, rather than assuming that external address is permanent.

## How a simulation runs

```text
Laptop browser -- SSH tunnel --> Streamlit on management
                                      |
                            Build & run settings
                                      |
                       RoboAiXrfSimulation.start_run()
                                      |
                         ui_cluster_run.py controller
                                      |
                          Slurm array of event chunks
                                      |
                 +--------------------+--------------------+
                 |                    |                    |
             compute node         compute node         compute node
          run_cluster.sbatch    run_cluster.sbatch    run_cluster.sbatch
                 |                    |                    |
            run_shard.sbatch -> one Geant4 process per allocated node
                               with the requested workers
                 |                    |                    |
                 +---------- private ROOT outputs --------+
                                      |
                        marker and ROOT consistency checks
                                      |
                            combine_shards.py on management
                                      |
                      complete result -> simulation.root -> Results UI
```

1. The builder prepares the geometry/source/physics configuration and spectrum.
   **Build & compile** prepares simulation inputs; it does not rebuild the C++
   executable on each run.
2. `roboaixrf/main.py` prepares `run.mac` and starts the controller with the saved
   total events, workers, selected nodes and chunks setting.
3. The controller creates a timestamp-and-UUID directory, copies `config.json`
   and `run.mac`, and writes a manifest containing the planned run.
4. It submits an array. Each array task index identifies one chunk. The batch
   script receives the real shared repository explicitly.
5. `run_cluster.sbatch` starts a bound `srun` task. `run_shard.sbatch` isolates
   chunk/viewer paths and rewrites macro workers, seeds and events.
6. The shard sources Geant4 and times the simulator process. Zero exit status
   plus an existing output permits an atomic `success.json`. Execution failures
   can write `failure.json`; earlier setup failures may leave only a log.
7. The controller follows logs/Slurm, validates each planned chunk and records
   node use and timings. The merger reads accepted `MyTree` data, offsets local
   event IDs, and writes combined ROOT/JSON through temporary files.
8. Only a complete accepted run becomes the top-level `simulation.root` used by
   Results. Detector-response processing happens afterwards in the application.

Snapshots retain JSON and macros, but do not freeze the executable, shared
libraries, hardware or every external file referenced by those inputs.

## Slurm queue, workers and memory

### Events and chunks

An **event** is one requested Geant4 event, not one detected ROOT row. A **chunk**
(called a shard in filenames) is a group of events. A **worker** is a Geant4
thread within one simulator process on one node.

For events `E`, selected-node count `P`, and chunks-per-selected-node setting `C`:

```text
chunk_count = min(E, P * C)
maximum_concurrent_chunks = min(P, chunk_count)
q, r = divmod(E, chunk_count)
chunk i gets q + 1 events when i < r, otherwise q
```

For example, 103 events in eight chunks become seven chunks of 13 and one of
12; the total stays 103. A billion events on 20 selected nodes at `C=1` gives
20 chunks of 50 million, with up to 20 running concurrently. At `C=4`, it gives
80 chunks of 12.5 million, still at most 20 at a time.

The latter uses an array shaped as `--array=0-79%20`. The `%20` limits concurrent
array elements, as described in [Slurm job-array documentation](https://slurm.schedmd.com/job_array.html).
Each element requests an exclusive node and starts a new Geant4 process.

When a node finishes, Slurm can assign it a pending chunk. `C` controls total
queue size; **it is not a per-node quota**. Faster nodes may do more work, but
the scheduler does not promise that distribution or participation by every
selected node. Resource availability and scheduling limits can reduce concurrency.
Once the queue is empty, a running chunk cannot be split/moved by this code.

### CPU allocation and affinity

| Workers/node | Slurm request | Intended use |
|---:|---|---|
| 40 | `--cpus-per-task=40 --hint=nomultithread` | One worker per physical core |
| 80 | `--cpus-per-task=80 --hint=multithread` | Both hardware threads per core |

Counts 1–80 are accepted: through 40 use `nomultithread`, larger counts use
`multithread`. The step applies `--cpu-bind=verbose,threads`. The shard verifies
`SLURM_CPUS_PER_TASK`, records its observed Linux affinity, and writes the same
worker count to `/run/numberOfThreads`. See the [Slurm allocation reference](https://slurm.schedmd.com/sbatch.html).
Eighty logical CPUs still share 40 physical cores; doubling workers does not
promise twice the throughput.

### Memory

Every chunk explicitly requests `--mem=185000`. Recorded partition settings are:

```text
MaxNodeCount=20
PartitionName=debug Nodes=ALL Default=YES MaxTime=INFINITE State=UP DefMemPerCPU=4500 MaxMemPerNode=185000
```

These are recorded lines, not a complete configuration installer. XRF does
**not** currently scale requested memory with workers: both 40 and 80 request
185000 MiB. Without that override, 40 × 4500 implies 180000 MiB while 80 × 4500
implies 360000 MiB, exceeding the per-node maximum. A ceiling is not an automatic
capped-memory calculation. Reserved memory is also not actual use; inspect
Slurm accounting's `MaxRSS`, when available, for observed consumption.

### Seeds and failure handling

Chunk `i` receives `(seed_base + 2*i, seed_base + 2*i + 1)`, with checked limits
and recorded values. This avoids duplicated seed values in a run. Changing
chunk count changes random-stream assignment and need not produce identical
events or files. Distinct seeds alone do not prove statistical independence.

The controller checks IDs, assigned event counts, workers, seeds, selected node,
exit status and output presence; the merger checks readable data and event-ID
bounds. Accepted chunks from an incomplete run may form a **diagnostic partial
merge**, but it is not published as the final UI result. There is no automatic
failed-chunk retry or resume in the application.

## Automatic chunk sizing

Automatic selects chunks for the **worker count you choose**. It does not
silently change 70 workers into 80.

1. A qualifying `benchmarks/*_chunks.json` takes precedence: same configuration
   hash, same selected-node set, the chosen worker count, and event total within
   a factor of two. It supplies the measured winning chunk count for that worker.
2. Otherwise, up to three recent complete runs with matching configuration and
   worker count supply a rate. Chunk markers must qualify and median chunk time
   must be at least 15 seconds. Rate is events divided by summed process-seconds.
   This history fallback does not require the same node set.
3. Otherwise, the conservative production fallback is 217,000 events/s per node
   at 40 workers and 274,000 at 80, interpolated for other counts.
4. The duration heuristic targets about 60 seconds/chunk and caps at eight chunks
   per selected node. Manual UI sizing allows 1–32; a qualifying measured winner
   takes precedence over the heuristic cap.

The page now retains only the short caption, for example:

```text
Automatic recommendation: 1 chunk per selected node.
```

Genuine discovery warnings remain visible. Verbose inventory, benchmark and
duration paragraphs were removed. **Save run settings** is still required after
editing events/workers because those form values drive the recommendation.

Hardware changes are not detected by calibration: matching does not fingerprint
the binary, BIOS, network, CPU model or libraries. Preserve old evidence and
create fresh matching benchmarks after upgrades. An old JSON can still match
even when its recommendation is no longer the best one.

## Starting and using the UI

### On management

With the existing installed environment:

```bash
cd /srv/cluster/TEMPO_XRF_REPO || exit 1
export TEMPO_XRF_REPO=/srv/cluster/TEMPO_XRF_REPO
./start_tempo_xrf.sh --check
./start_tempo_xrf.sh
```

The launcher sources Geant4 and the existing venv, sets `PYTHONPATH`, checks
imports and starts Streamlit. It never installs packages or pulls Git updates.
`--check` does not start a server or submit work; it is not a full cluster test.

The Python launcher currently requires `TEMPO_XRF_REPO`. The shell launcher's
separate `TEMPO_XRF_REPO_DIR` override chooses the frontend checkout; one is not
a substitute for the other. Exporting the former avoids relying on shell setup.

For a home-directory command, preserve any existing wrapper, then edit
`/home/mnode/start_tempo_xrf.sh` with `nano` to contain:

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
export TEMPO_XRF_REPO=/srv/cluster/TEMPO_XRF_REPO
exec /srv/cluster/TEMPO_XRF_REPO/start_tempo_xrf.sh "$@"
```

Make it executable and start it on management:

```bash
chmod +x /home/mnode/start_tempo_xrf.sh
/home/mnode/start_tempo_xrf.sh
```

An optional port argument such as `./start_tempo_xrf.sh 8503` overrides 8502.
Other overrides include `TEMPO_XRF_PORT`, `TEMPO_XRF_ADDRESS`, `TEMPO_XRF_VENV`
and `TEMPO_XRF_GEANT4_SETUP`. Keep the frontend/controller running during work;
this branch has not established an independent persistent web service.

### From the laptop

Create a tunnel in a laptop terminal:

```bash
ssh -N -L 8502:127.0.0.1:8502 mnode@192.168.10.3
```

Use the reachable management address and match the remote port to the launcher.
Open `http://localhost:8502`. The SSH tunnel and management frontend are separate
processes and both must stay running.

### A normal simulation

1. Configure **Setup**, **X-ray tube**, **Detector** and **Physics** for the
   intended experiment; this guide does not prescribe physics changes.
2. Choose a descriptive run name, total Beam-on events and Workers per node.
   The benchmarked production choice is 80 workers.
3. Click **Save run settings**. Beam-on events is the total across the cluster.
4. Choose **Single node** or **Distributed** with qualified nodes. Leave sizing
   on Automatic unless deliberately testing a manual value.
5. Click **Build & compile** to prepare inputs. Visualization is optional and
   separate from the quantitative run.
6. Click **Run Geant4** and wait for completion and merge. Event progress at
   100% alone is not the final completion check.
7. Open **Results**, apply the intended detector response, compare spectra and
   download `xrf_spectrum.csv`.

Use separate names for important experiments. Reusing a name removes the old
top-level `simulation.root` when a new run starts; timestamped attempts remain.
Do not launch simultaneous runs into the same run name. Results uses the active
Streamlit session, not a historical-run browser. Restarting the UI does not
delete the stored files.

## Results and detailed measurements

The standard launcher saves data under:

```text
/srv/cluster/TEMPO_XRF_REPO/python_code/roboai_xrf_frontend/runs/
└── <run-name>/
    ├── config.json, run.mac              current prepared inputs
    ├── simulation.root                 latest published complete result
    ├── simulation_spectrum.html        detector-response plot when generated
    ├── benchmarks/
    │   ├── <timestamp>_workers.json     one-node 40/80 results
    │   └── <timestamp>_chunks.json      whole-cluster comparisons
    └── cluster_runs/<timestamp>_<uuid>[_label]/
        ├── config.json, run.mac         inputs for this attempt
        ├── run_manifest.json           request, job ID, status and timings
        ├── combined_response.root      accepted-shard merge
        ├── combined_response.json      completeness and row/event metadata
        ├── logs/slurm-<array>_<id>.log  launch, affinity and Geant4 output
        └── shard-<id>/
            ├── config.json, run.mac
            ├── simulation.root
            ├── success.json or failure.json
            └── isolated work/viewer directories
```

`MyTree` rows describe detector scoring. Many events do not deposit recorded
detector energy, while one event can produce multiple entries. A billion events
need not produce a billion rows.

On management, list attempts:

```bash
cd /srv/cluster/TEMPO_XRF_REPO || exit 1
source .venv/bin/activate
ls -lht python_code/roboai_xrf_frontend/runs/full_xrf_example/cluster_runs
```

This read-only report finds the latest completed attempt for that run name.
Change the name as needed; completed benchmark attempts are also included.

```bash
python - <<'PY'
from pathlib import Path
import json

root = Path("python_code/roboai_xrf_frontend/runs/full_xrf_example/cluster_runs")
for path in sorted(root.glob("*/run_manifest.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True):
    manifest = json.loads(path.read_text())
    if manifest.get("status") == "complete":
        break
else:
    raise SystemExit("No completed run found.")

print("Manifest:", path.resolve())
for key in ("job_id", "requested_events", "workers_per_node", "chunk_count",
            "slurm_queue_and_execution_seconds", "merge_seconds",
            "controller_elapsed_seconds"):
    print(f"{key}: {manifest.get(key, 'not recorded in this older run')}")
wall = manifest.get("controller_elapsed_seconds")
if wall and wall > 0:
    print(f"Controller throughput: {manifest['requested_events'] / wall:,.2f} events/s")
for node, item in sorted(manifest.get("node_summary", {}).items()):
    seconds = item["elapsed_ns"] / 1e9
    rate = item["events"] / seconds if seconds > 0 else 0
    print(f"{node}: {item['chunks']} chunks, {item['events']:,} events, "
          f"{seconds:.3f} process-seconds, {rate:,.2f} events/s")
PY
```

Useful read-only Slurm checks:

```bash
sinfo -h -N -p debug -o '%N %T %c %m'
squeue -u "$USER" -n tempo-xrf
scontrol ping
```

For accounting, replace `JOB_ID` with the actual numeric ID:

```bash
sacct -j JOB_ID --units=M \
  --format=JobIDRaw,State,NodeList,Elapsed,AllocCPUS,ReqMem,MaxRSS,TotalCPU
```

Accounting depends on the installed Slurm configuration. Inspect step rows as
well as array parents; one task's elapsed time is not whole-run throughput.

Progress is sampled roughly once a second from chunk logs that each print about
100 updates. Arrivals are batched, so 8-million highs and 4-million lows are not
reliable instantaneous cluster measurements. The later reported one-billion
`03:03` / `5,449,541.41 Event/s` line is a progress observation; its completed
manifest has not been reviewed. For comparisons use:

```text
end-to-end events/s = requested total / controller_elapsed_seconds
```

Slurm queue-and-execution time includes waiting and running. `merge_seconds`
times the merger. A node's `elapsed_ns` sums timed simulator processes, including
initialization and ROOT writing. Summing node durations does not give wall time.
Controller timing excludes later detector-response analysis in Results.

## Everything required to get it working

This records the migration and repairs, including failed approaches that explain
the design. It is not an instruction to repeat repairs on a healthy cluster.

### 1. Preserve the actual working application

We traced frontend → Python → Slurm → Geant4 → merge first. Custom cluster files
were verified locally. Existing builder changes, C++ edits, binaries, runs and
environment-local package changes were preserved rather than assuming upstream
alone represented the installed application.

`CODEX.txt` captured the original requirements and cluster facts. On October 27,
the upstream `front` branch was checked at `ebabe36`: its eight new commits
concerned installer/helper files, rather than a runtime cluster change that
needed porting. Its online rebuild path targeted a different installation and
branch. It was not blindly merged over the working NFS environment. That is a
dated comparison, not a claim about future upstream changes.

### 2. Remove the five-node limit

The checkbox passed either five or one. It became explicit selection, live
inventory, environment override and eight-node fallback. Single-node operation
uses the same controller. This later discovered 20 nodes without another fixed
count patch. `patch_ui.py` became verification-only so it could not reapply the
obsolete five-node/16-GiB patch.

### 3. Match resources and isolate every chunk

CPU hints, allocated CPUs and macro workers were aligned; affinity was recorded;
16 GiB became explicit 185000 MiB. Exact event splitting handles remainders.
Distinct seed pairs, private output/viewer paths, timestamped attempts and
completion checks prevent chunks from overwriting one another or publishing
an incomplete run. Event-ID offsets make the detector rows mergeable.

### 4. Repair the home copy and Python environment

A recursive `scp` including a laptop venv failed with permissions and `lib64`
errors. Local `sudo` did not fix remote ownership. The home checkout lacked the
frontend entry point, while the NFS checkout still had it. A setup script also
could not identify a Git root in the copied directory.

We retained `/srv/cluster/TEMPO_XRF_REPO` and created a working Python 3.11 venv
there. The copied laptop Python 3.14 environment was unusable; management's
default Python 3.9 was incompatible with syntax such as `float | None`.
Imports were checked for Streamlit, NumPy, pandas, Plotly, Pydantic, SpekPy,
uproot, Awkward, xraylib, Matplotlib, h5py and tqdm. Missing `uproot>=5` was added.
Installation used Internet when available; runtime startup needs no downloads.

### 5. Make startup and deployment unambiguous

The old UI stayed visible until the shared source was updated. One laptop
deployment command had been run on management: its `cd` failed, leaving later
commands in the wrong directory. The helper now anchors its own source path,
refuses management, previews its allowlist, and backs up replacements.
The launcher prefers shared Geant4 and the repository venv, supports offline
preflight, and can be called by a home wrapper. SSH forwarding exposes the UI
to the laptop without relocating the simulation.

### 6. Fix the Slurm spool-path defect

Jobs **147 and 148** allocated a node but exited 127 while looking for
`/var/spool/slurmd/cluster_jobs/run_shard.sbatch`. Slurm executes a spooled batch
script; inferring the repository from that script's location was wrong.
Passing the shared repository explicitly fixed it. A regression test now
executes the controller from a simulated spool directory.

### 7. Diagnose the apparent Slurm hangs

Munge and `slurmctld` were active, port 6817 listened and `scontrol ping` said UP,
but `sinfo`/`squeue` timed out. A controlled restart did not repair it. Connection
checks, logs and a syscall trace showed the RPC reply arrived; then the client
stalled resolving `mnode-01.ad.local` externally. That evidence superseded the
initial suspicion of a stuck controller.

Management lacked its own hosts entry. After backing up `/etc/hosts` as
`/etc/hosts.before-tempo-slurm-20261002`, this was added:

```text
192.168.10.3 mnode-01 mnode-01.ad.local
```

Five repeated queries changed from eight-second timeouts to 0.00–0.01-second
successes. NFS was not the cause of those hangs.

### 8. Normalize compute-node libraries

Job **150** reached eight nodes but `node-02-002`, `node-02-003` and
`node-02-004` lacked `libxerces-c-3.2.so`. A common loader probe showed identical
library environments but missing `xerces-c` packages on those three.
Installation first failed because EPEL was disabled. Enabling `epel-release`
and installing `xerces-c` repaired it; the recorded version was `3.2.5-1.el9`.
The development package was not needed to execute the existing binary.

One node also had an external `eno2` DHCP problem while private `eno1`/NFS stayed
healthy. Shared offline RPM staging was discussed; the recorded successful
repairs used EPEL after connectivity was restored. Source `geant4.sh` before
interpreting `ldd` results for Geant4 libraries.

Job 150's five successful parts remained an unpublished partial merge. Job
**154** then passed all eight nodes: 800 events and 49 detector rows. Both
failure handling and successful completion were exercised on real hardware.

### 9. Measure 40 versus 80 workers

Jobs **155–160** used paired seeds, alternating order and three repetitions.
Eighty won. The 100,000-event tests were too short for absolute production-rate
calibration; their timing definition and results appear below.

### 10. Recover and reconcile management-specific changes

Accidental replacement/restoration reintroduced a stale home Python choice and
missing cluster integration. Source restoration and shared-venv selection were
checked before proceeding. Later comparison found large independent
detector-noise, seed, progress and timing changes in the live application.

We retrieved `management_snapshot_20261027` and built a separate
`management_candidate_20261027`, merging the queue into production versions.
The production merger's multithreaded uproot file handler was preserved. These
retrieved controller/UI/merger changes were reconciled into the branch; complete
checkout/venv byte parity or a whole-machine archive was not established.

### 11. Save a fork and introduce the dynamic queue

Upstream rejected the push with HTTP 403 because this account lacked permission.
A fork and `myfork` remote were created and the branch was pushed there. Git
stores source history; NFS distributes the deployed files inside the cluster.

The static per-node split became a throttled array of exclusive one-node chunks
so freed nodes could take pending work. A clean NFS-visible
`TEMPO_XRF_QUEUE_TEST` checkout used a symlink to production's executable,
allowing queue validation without replacing the modified live checkout.

### 12. Repair test packaging and validate selective deployment

An installed `tests` package shadowed our directory; adding/deploying
`tests/__init__.py` fixed the import. A launcher test incorrectly expected one
Python preflight invocation; the restored launcher made several. The assertion
was corrected to require that every call use the intended repository venv.

Job **589** passed eight chunks on four nodes in the clean checkout; job **597**
passed 40 chunks across all 20. The reviewed candidate was selectively deployed
with backups under `.deployment_backups/before_dynamic_queue_job597`.
Job **637** passed production's controller and preserved merger. The suite grew
from 14 to 17 to 23 tests, all 23 later passing on management.

### 13. Replace guessed chunk sizes with production measurements

Job **805** ran a billion events at 70 workers and four chunks/node. Every node
did four chunks; node-total times varied by only 1.15% coefficient of variation.
Extra queue depth did not meaningfully redistribute this uniform workload.

An early automatic estimate used the tiny worker benchmark and would have
chosen as many as 640 chunks for a billion events. It was replaced with matching
representative history, a 15-second threshold, roughly 60-second target and
eight-chunk cap. The production matrix then provided its measured winner.

The confusing preview of 80 × 1,250 events reflected a saved 100,000-event value,
not the user's intended edited 10 million. Saving form values remains required.
The user later requested a cleaner UI: retain the one-line recommendation and
put explanatory material in documentation.

## Source changes and Git history

| File/component | Responsibility |
|---|---|
| `cluster_jobs/cluster_config.py` | Inventory, splitting, seeds, resources, queue shape, calibration and matching |
| `cluster_jobs/ui_cluster_run.py` | Snapshot, manifest, submit/poll, validation, timing, merge/publication |
| `cluster_jobs/run_cluster.sbatch` | Array index, explicit repository and bound `srun` task |
| `cluster_jobs/run_shard.sbatch` | Private files, macro rewrite, runtime, affinity and markers |
| `cluster_jobs/combine_shards.py` | Checked `MyTree` merge, event offsets and metadata |
| `cluster_jobs/cluster_smoke_test.py` | Real small cluster test without replacing a published result |
| `cluster_jobs/benchmark_workers.py` | Paired one-node 40/80 process benchmark |
| `cluster_jobs/benchmark_chunks.py` | Whole-controller worker/chunk matrix |
| `cluster_jobs/patch_ui.py` | Integration verifier instead of obsolete source patching |
| `cluster_jobs/__init__.py`, `tests/__init__.py` | Importable project packages |
| `roboaixrf/main.py` | Existing API/progress/cancellation integrated with the controller |
| Frontend `components/run_page.py` | Nodes, workers, Automatic/Manual and concise caption |
| Frontend `requirements.txt` | Explicit `uproot>=5` dependency |
| `start_tempo_xrf.sh` | Offline start, path/venv selection, preflight and port overrides |
| `deploy_to_mnode.sh` | Guarded backed-up source transfer |
| `tests/test_cluster_jobs.py` | Resource, splitting, marker, publication, launcher and calibration tests |
| `.gitignore`, `AGENTS.md`, docs | Generated-file exclusions and recorded operating knowledge |

Builder edits, generated binaries, run data and historical backup copies were
not swept into integration commits or deployed indiscriminately.

| Commit | Milestone |
|---|---|
| `f5217f5` | Configurable eight-node Slurm execution |
| `550f820` | Dynamic Slurm queue |
| `2d52644` | Automatic sizing and production-source reconciliation |
| `52b2346` | Representative real-run calibration |
| `6abc0ae` | Production benchmark recommendation in the UI |
| `8dfe363` | Recorded measured winner |
| `dbfb3a5` | Compact recommendation text |

The compact caption is saved in the fork; its last management deployment has
not been independently checked. Git source backup does not archive venvs,
executables, `/etc` settings or simulation results.

## Validation and repeatable benchmarks

### Automated checks and smoke tests

On management:

```bash
cd /srv/cluster/TEMPO_XRF_REPO || exit 1
source .venv/bin/activate
export PYTHONPATH=/srv/cluster/TEMPO_XRF_REPO
export TEMPO_XRF_REPO=/srv/cluster/TEMPO_XRF_REPO
python -m unittest -v tests.test_cluster_jobs
bash -n cluster_jobs/run_cluster.sbatch
bash -n cluster_jobs/run_shard.sbatch
python cluster_jobs/patch_ui.py .
```

The 23 tests cover event conservation, sizing, seeds, CPU/memory flags, repeated
nodes, completion/publication, spool paths, test packaging, venv preference and
calibration matching. They use mocked/fake scheduler and simulator components;
they do not themselves prove real allocation or scientific accuracy.

After checking node readiness, use a prepared directory containing `config.json`
and `run.mac` for this real cluster smoke test:

```bash
python cluster_jobs/cluster_smoke_test.py \
  python_code/roboai_xrf_frontend/runs/full_xrf_example \
  --events-per-node 100 --chunks-per-node 2 --workers 40
```

This submits jobs and discovers nodes unless `--nodes` is supplied. Explicit
comma-separated names target one node or subset. For 20 selected nodes it
requests 2,000 events in 40 chunks. Inspect actual distribution: the test does
not require every eligible node to run a chunk. It uses `publish=False`.

| Job(s) | Recorded result |
|---|---|
| 147, 148 | Failed spool path; later reproduced and repaired |
| 150 | Five successes/three missing-library failures; partial not published |
| 154 | Eight-node path: 800 events, eight nodes, 49 rows |
| 155–160 | Six successful paired worker tests |
| 589 | Clean queue: 8 chunks, 4 nodes, 400 events |
| 597 | Clean queue: 40 chunks, 20 actual nodes, 2,000 events, 119 rows |
| 637 | Production queue: 8 chunks, 4 nodes, 400 events, 5 rows |
| 805 | Production: 1 billion events, 20 nodes, 70 workers, 80 chunks |
| 925, 945, 965, 1005, 1045, 1125 | Production benchmark matrix below |

Different row counts across tests do not alone show event loss or accuracy;
the configuration and random samples also matter.

### Initial 40/80 comparison

Historical command:

```bash
python cluster_jobs/benchmark_workers.py \
  python_code/roboai_xrf_frontend/runs/full_xrf_example \
  --node node-01-001 --events 100000 --repetitions 3 --seed-base 424242
```

Both modes use the same exclusive node and paired seeds, with alternating order
over repetitions. Timing includes Geant4 initialization, transport and ROOT
writing but excludes queue time and the later Python merge. JSON is retained
under `benchmarks/*_workers.json`.

| Workers | Median events/s |
|---:|---:|
| 40 | 22,568.57 |
| 80 | 28,557.48 |

The ratio was 1.265× (about 26.5% higher). These short runs support the relative
choice but not absolute production calibration. A longer confirmation should
use enough events for timed simulation to run for minutes on the intended input.

### Production worker/chunk matrix

The October 27 comparison used 500 million events per combination, all 20 nodes,
70/80 workers, 1/2/4 chunks per selected node, and one repetition:

```bash
python cluster_jobs/benchmark_chunks.py \
  python_code/roboai_xrf_frontend/runs/full_xrf_example \
  --events 500000000 --workers 70,80 --chunks 1,2,4 \
  --repetitions 1 --seed-base 731000
```

Default selection uses current discovery. Supply `--nodes` with exact expanded
comma-separated names to reproduce a historical set. Do not change prepared
inputs during the matrix. Seed bases are paired within a repetition; successive
repetitions reverse order. Different chunk counts still partition randomness
differently.

This benchmark times the complete controller call, including queueing,
initialization, execution, validation and merge. `publish=False` retains
artifacts without replacing the published result. JSON includes every job,
node distribution, elapsed time, run path, per-worker winner and overall winner.

| Workers/node | Chunks/selected node | Job | Wall seconds | Events/s |
|---:|---:|---:|---:|---:|
| 70 | 1 | 925 | 106.664 | 4,687,623.83 |
| 80 | 1 | 945 | **97.658** | **5,119,920.84** |
| 70 | 2 | 965 | 111.262 | 4,493,892.23 |
| 80 | 2 | 1005 | 106.205 | 4,707,859.15 |
| 70 | 4 | 1045 | 124.804 | 4,006,266.53 |
| 80 | 4 | 1125 | 114.991 | 4,348,161.68 |

Every run used 20 actual nodes. The winner was 9.22% faster than 70/1 and 27.80%
faster than 70/4 in the same matrix. At 80 workers, two/four chunks were about
8.0%/15.1% slower than one chunk.

The winning node process durations were 82.799–88.656 seconds with about 1.99%
coefficient of variation. Similar nodes left little imbalance to correct;
more chunks repeated startup, scheduling and output work. The dynamic queue
remains available for other workloads/hardware. A single wave can be optimal
among the tested settings.

The approximately nine-second difference between controller wall time and the
longest node process is combined overhead, **not measured merge time alone**.
Inspect per-run manifest phases before assigning it to storage or scheduling.

The retained JSON is at the following management path (shown wrapped):

```text
/srv/cluster/TEMPO_XRF_REPO/python_code/roboai_xrf_frontend/runs/
full_xrf_example/benchmarks/20261027_140740_chunks.json
```

Automatic reads it there. Downloading a copy is optional review, not installation.
Raw benchmark JSON is run data and is not currently committed source.
With one repetition, a reported “median” is a single measurement. After hardware
changes, prefer three alternating repetitions under comparable load. Benchmarks
reserve nodes and retain output: check space first. The dated job805 directory
used 326 MiB; management then had 46 GiB free on a 70-GiB filesystem.

## Deployment and adding nodes

### Source and environment are separate

Edit and version source on the laptop, review and deploy it to the shared
production checkout. Git supplies history; NFS supplies the deployed files.

Production has valuable local changes. Do not blindly pull, switch/reset branches
or copy laptop venvs/builds into it. Compare source, preserve management edits
and test a separate checkout if needed. The snapshot/candidate process above
was used for this purpose.

From the **laptop**, preview the explicit integration file list:

```bash
cd /home/nutty/Documents/TEMPO_XRF_REPO || exit 1
./deploy_to_mnode.sh --dry-run
```

After reviewing it, `./deploy_to_mnode.sh` performs the transfer. The helper
backs up replaced files under `.deployment_backups/before_dynamic_cluster`.
It has no `--delete` and excludes venvs, builds, results and the unverified local
builder. Use `TEMPO_XRF_DEPLOY_TARGET` to override the reachable SSH destination
for both preview and transfer. Repeated deployments reuse the backup directory;
keep a separately named snapshot for an important release.

Run preflight/tests/smoke on management afterwards. Restart the frontend when
no active run needs its controller. A Git push does not deploy management;
rsync success does not certify that a running UI loaded new source. The helper's
current allowlist does not include this root README; Git carries it on the branch.

### Installation and future nodes

Startup assumes an existing installed environment. Historical setup scripts are
not blind repair commands for the live cluster. Each node needs a compatible
runtime, Geant4/data access, the shared executable and system libraries such as
Xerces; management additionally needs Python packages. Installation can use
repositories or staged compatible packages. Normal execution requires no Internet.

For a new node:

1. Configure private networking, hostname resolution and compatible Munge/Slurm;
   register the node in the intended partition.
2. Mount `/srv/cluster` at the same path and verify access as the simulation user.
3. Verify Geant4/data and loader dependencies after sourcing `geant4.sh`.
4. Check topology/memory against the current 40-core/80-thread assumptions and
   fixed 185000-MiB request.
5. Run a targeted smoke test, inspect affinity/markers/logs, and qualify both
   worker modes if needed.
6. Refresh/select it in the UI and benchmark the changed node set.

Discovery uses `TEMPO_XRF_CLUSTER_NODES`, then Slurm, then the historical eight
nodes; the UI caches it for 30 seconds. It does not filter DOWN/DRAIN nodes.
Appearance in the selector is not readiness. Before expansion beyond 20, review
`MaxNodeCount=20`, node definitions, partition policy and array limits too.

## Bottlenecks and future work

Hardware is the user's next priority. No physics, numerical-model or transport
changes are included. Distinguish measured limits from suspected bottlenecks:

| Item | Evidence now | Upcoming task |
|---|---|---|
| Excess chunks | Measured: 80-worker throughput fell from 5.12M to 4.35M events/s at 1 versus 4 chunks/node | Keep measured winner; repeat after changes |
| Node imbalance | Small, about 1–2% timing variation | Do not assume deeper queues help this uniform workload |
| Network | User reports “100mb/s”; actual units, negotiated rates and saturation unmeasured | Confirm node links/management uplink and measure NFS traffic before/after upgrade |
| Shared storage | Outputs go through NFS; HDDs reported, backing devices/I/O wait unmeasured | Identify management's shared-volume disks and latency before prioritizing storage |
| Compute HDDs | Present by report; not established as result-write bottleneck | Check paging, loading and local temporary I/O; local SSDs may not improve NFS writes |
| RAM speed | Reported 2400; units, DIMM population and supported rate unverified | Check supported/configured speed and bandwidth; use supported settings and stability tests |
| BIOS power/thermal policy | Suspected saving modes; no measured clocks/power-limit evidence | Record settings, compare supported performance profiles one change at a time, retain thermal safeguards |
| Other overhead | Around nine seconds combined in winning 500M run; shares unknown | Inspect manifest phases/utilization before blaming merge or I/O |
| Capacity/retention | Run/benchmark artifacts accumulate | Back up data and plan retention/free-space monitoring |
| Historic results | Files exist but UI uses current session | Optional later run browser and readable timing report |
| Reproducibility | Config/seeds recorded; hardware/binary identity absent | Optional later checksums/version inventory and upgrade-aware calibration |
| Provisioning drift | Real hostname, Python and library failures | Maintain tested node/environment checklist |

If the link is **100 megabits/s**, its theoretical data rate is 12.5 MB/s before
overhead; **100 MB/s** is eight times that. Confirm units and the shared uplink.
Faster networking will not multiply throughput when event transport is CPU-bound.
Likewise, 2400 may be the CPU/DIMM population's supported rate rather than an
accidental underclock. These hardware conclusions have not yet been measured.

For each hardware comparison, preserve configuration, events, node selection,
workers, chunks and seed plan. Record before/after settings, controller/merge
times and node timings. Repeat measurements, archive old calibration separately,
and produce fresh matching benchmark evidence.

Node-local scratch, parallel merging, C++ hot-path edits, compiler changes and
NUMA experiments were discussed only. They have not been implemented or scheduled
here; hardware work comes first. Physics-list simplification, raised cuts,
reduced events, changed biasing and fast-math shortcuts are not proposed.

## Accuracy and current limits

This integration partitions execution and combines scoring; it does not intend
to change the scientific configuration. Execution success is still distinct
from scientific validation:

- Markers and merge checks validate process/output consistency. Event totals
  come from requested macros/markers, not an independent completed-event counter.
  The current C++ entry point does not propagate every macro-command error into
  its exit code. Stronger validation is a possible later task, not implemented here.
- The merger handles `MyTree` branches `EventId`, `rootId`, `Energy`, `Weight`;
  it does not combine every ROOT object/histogram, such as `Edep`. Raw shard
  outputs remain available.
- Seeds/chunk counts can produce different Monte Carlo realizations. Future
  scientific comparisons should account for statistical uncertainty, rather
  than expect identical ROOT bytes across different partitionings.
- Automatic retry, durable controller recovery, an environment lockfile,
  automatic retention and a historical-results browser are not implemented.
- Selected management sources were reconciled. The whole checkout, venv, binary
  and machine configuration have not been archived as one reproducible image.

The verified result is a working 20-node execution path and a measured queue
choice for the recorded workload. Hardware gains and broader scientific
validation require their own measurements.

# TEMPO XRF cluster context

Last updated: 2026-10-02

Read this file before changing the frontend-to-cluster execution path. Keep it
current when cluster behavior, deployment paths, or verified assumptions change.
For the complete implementation history, file-by-file change explanation,
operations guide, output layout, and validation record, see
[`CLUSTER_IMPLEMENTATION.md`](CLUSTER_IMPLEMENTATION.md).

## Execution path

The quantitative-run path is:

1. `python_code/roboai_xrf_frontend/components/run_page.py` collects run and
   cluster settings and calls `RoboAiXrfSimulation.start_run()`.
2. `roboaixrf/main.py` writes `run.mac`, removes an older published
   `simulation.root`, and starts `cluster_jobs/ui_cluster_run.py`.
3. The cluster controller creates an immutable run directory below the compiled
   frontend run and submits `cluster_jobs/run_cluster.sbatch` as one gang-scheduled
   allocation covering every selected node. The controller passes the shared
   repository path explicitly because Slurm executes a spool copy of the batch
   script; deriving the repository from the batch script's `BASH_SOURCE` is invalid.
4. `run_cluster.sbatch` starts one concurrent `run_shard.sbatch` task per node.
   Each shard has its own folder, event count, random seeds, macro,
   viewer-output path, and `simulation.root`.
5. `geant4_code/build/sim` reads the shard config/macro. The macro controls
   `/run/numberOfThreads`, `/random/setSeeds`, and `/run/beamOn`.
6. A shard writes `success.json` only after Geant4 exits zero and its ROOT output
   exists. `cluster_jobs/combine_shards.py` accepts only success-marked shards,
   copies `MyTree` rows, and offsets shard-local `EventId` values into a single
   contiguous event space.
7. A complete merge is published as the frontend run's `simulation.root`. If any
   shard fails, successful shards may be retained in a non-published combined
   artifact, but a partial result is never exposed as the completed UI result.

## Reported cluster inventory

- Management/login host: `mnode@mnode-01` (`192.168.10.3`).
- Shared repository: `/srv/cluster/TEMPO_XRF_REPO`, previously branch `front`.
- Debug-partition compute nodes: `node-01-[001-004]` and
  `node-02-[001-004]` (eight total).
- Each node: 40 physical cores, 80 logical CPUs with Hyper-Threading, and
  191544 MiB reported to Slurm. Reported topology is two sockets, 20 physical
  cores per socket, and two hardware threads per core.
- Per-node job memory request: `--mem=185000`.
- The `debug` partition is now configured with `DefMemPerCPU=4500` and
  `MaxMemPerNode=185000`. TEMPO XRF's explicit `--mem=185000` takes precedence
  over that default, so both 40- and 80-worker runs request 185000 MiB per node.
- Rocky Linux 9.8; Slurm 26.05.4; Geant4 11.4.2 with multithreading.
- Compute nodes reportedly use configless Slurm through
  `--conf-server=192.168.10.3:6817`, with `slurmd -Z` in the latest setup.
- Shared NFS root: `/srv/cluster`.
- Shared Geant4: `/srv/cluster/software/geant4-11.4.2`.
- Simulation executable: `geant4_code/build/sim`.
- Frontend: `python_code/roboai_xrf_frontend`.
- Verified management virtualenv: `/srv/cluster/TEMPO_XRF_REPO/.venv`
  (Python 3.11).

## Laptop verification on 2026-10-02

The laptop is on `front` at `a623fc9`, matching `origin/front`, with pre-existing
modified and untracked cluster work. The custom files are present locally:

- `cluster_jobs/ui_cluster_run.py`
- `cluster_jobs/run_shard.sbatch`
- `cluster_jobs/combine_shards.py`
- `cluster_jobs/patch_ui.py`
- patched `roboaixrf/main.py`
- patched `python_code/roboai_xrf_frontend/components/run_page.py`
- patched `python_code/roboai_xrf_frontend/builder.py`

Historical five-shard run directories and logs are also present locally and show
successful runs on the shared `/srv/cluster/TEMPO_XRF_REPO` path.

Direct comparison with management is **not yet verified**. The host was reachable,
but both laptop SSH keys were rejected for `mnode`; non-interactive SSH ended with
`Permission denied`. Do not claim the checkout is byte-identical to the active
management repository or virtualenv until the comparison in the deployment
section has been run with working credentials.

Management startup on 2026-10-02 confirmed that its shared frontend source is
initially the older implementation: the Build & run page displayed the fixed
"Distribute this run across five nodes" control. After selective deployment, the
new Single node/Distributed controls and configurable node selector appeared.
However, live discovery reported that `sinfo -h -N -p debug -o %N` exceeded its
five-second timeout, so the UI showed the documented fallback inventory. Slurm
controller/configuration health must be diagnosed before treating a UI-started
run as valid.

Initial Slurm diagnostics on management found `/etc/slurm/slurm.conf` present,
both `munge` and `slurmctld` active, and TCP port 6817 listening on all addresses.
`scontrol ping` immediately reported the primary controller UP, but both ordinary
and `SLURM_CONF_SERVER=192.168.10.3:6817` `sinfo` queries timed out after 15
seconds with status 124. This rules out missing configuration, a stopped or
unreachable controller, and merely increasing the frontend's five-second timeout.
All Slurm client and controller packages are version 26.05.4. `squeue`,
`scontrol show partition`, `scontrol show nodes`, and `scontrol show config` also
timed out, while the controller remained almost idle and still accepted batch
submissions. This isolates the fault to a wedged state-reading RPC path rather
than version skew or total controller failure. UI submissions 147 and 148 were
allocated to `node-01-001` but exited 127. Their task logs proved that
`run_cluster.sbatch` derived `/var/spool/slurmd` as the repository from Slurm's
spooled script copy, then could not find `cluster_jobs/run_shard.sbatch`. The
controller now receives the shared repository as an explicit argument, covered by
a regression test that executes it from a simulated spool path. The fix was
deployed and all 14 tests passed on management. Address-order checks subsequently
showed IPv4 `192.168.10.3` first, and controller log/state directory permissions
were correct. A live `SIGPROF` capture taken while `sinfo` was hung showed
`requests:0`, no queued controller work, and only the listening socket; `ss` also
showed no connection at that instant. A subsequent syscall trace supplied the
missing evidence: `sinfo` connected to `192.168.10.3:6817`, authenticated through
`/run/munge/munge.socket.2`, sent its request, and received the controller's
partition reply immediately. It then stalled in a DNS poll while resolving
`mnode-01.ad.local` through `193.166.149.122`; the outer 12-second timeout killed
the client. An ordinary `sinfo` immediately after the trace succeeded and listed
all eight nodes idle with 80 CPUs and 191544 MiB each. The repository, NFS,
Munge, controller RPC, and node inventory are therefore functioning; the
observed discovery failure is an intermittent/local hostname-resolution problem.
Confirm `/etc/hosts`, the `hosts:` NSS order, `/etc/resolv.conf`, and repeated
query latency before a real cluster smoke test.

Follow-up checks confirmed that management had no `mnode-01` or
`192.168.10.3` entry in `/etc/hosts`. Its NSS order was
`files dns myhostname`, while NetworkManager configured the `ad.local` search
domain and DNS servers `193.166.149.122` and `193.166.149.115`. Five consecutive
eight-second `sinfo` checks all timed out. This matches the trace: lookup falls
through the empty hosts file to external DNS and stalls on
`mnode-01.ad.local` before the later `myhostname` fallback. The entry
`192.168.10.3 mnode-01 mnode-01.ad.local` was added to `/etc/hosts`, after
backing it up as `/etc/hosts.before-tempo-slurm-20261002`. `getent` then resolved
the short name directly to `192.168.10.3`, and five consecutive `sinfo` checks
all returned the eight idle nodes in 0.00--0.01 seconds. Hostname resolution is
repaired; proceed to the real cluster smoke test. This was not a Slurm, NFS, or
repository deployment failure.

The first real eight-node smoke test was Slurm job 150, using 40 workers and 100
events per node. All eight distinct nodes were allocated concurrently with
40 CPUs per task; observed binding on the failed node logs was CPUs `0-39` with
mask `0xffffffffff`. Five shards completed, while shards 5--7 on
`node-02-002`, `node-02-003`, and `node-02-004` exited 127 because the shared
simulation executable could not load `libxerces-c-3.2.so`. The controller
correctly rejected the incomplete run, retained the five-shard merge only as an
unpublished artifact (500 simulated events, 34 `MyTree` rows), and reported all
three failures. Diagnose package/loader-path consistency across the nodes before
repeating the smoke test; do not treat this partial artifact as a result.

A one-task-per-node loader probe confirmed the cause. `node-01-001` through
`node-01-004` and `node-02-001` have `xerces-c-3.2.5-1.el9.x86_64`, and `ldd`
resolves `libxerces-c-3.2.so` from `/usr/lib64`. `node-02-002`, `node-02-003`,
and `node-02-004` report that `xerces-c` is not installed and `ldd` reports the
library as not found. All eight tasks showed the same `LD_LIBRARY_PATH`, ruling
out a per-node Geant4 environment difference. Install the `xerces-c` runtime
package (the `-devel` package is not needed to execute the already-built binary)
on those three nodes, verify with `ldd`, and rerun the full smoke test.
An initial `dnf install xerces-c` on `node-02-002` returned no package because
only Rocky BaseOS, AppStream, and Extras were enabled. The installed
`xerces-c-3.2.5-1.el9` build on the working nodes is an EPEL 9 package. Enable
`epel-release` on each of the three missing nodes before installing `xerces-c`.
On `node-02-003`, the external `eno2` connection could not obtain a DHCP lease;
the private cluster `eno1` interface remained healthy at `192.168.10.10`.
Therefore, stage the signed EPEL release and Xerces RPMs once from management
under the shared `/srv/cluster` tree and install the local RPMs with repositories
disabled on the three nodes. This avoids making compute-node Internet access a
runtime or provisioning requirement. Diagnose the `eno2` DHCP failure separately
if external connectivity is required later.
`node-02-002`, `node-02-003`, and `node-02-004` subsequently enabled
`epel-release-9-10.el9` and successfully installed `xerces-c-3.2.5-1.el9`.
Their loaders now resolve Xerces from `/lib64/libxerces-c-3.2.so`. Manual `ldd`
checks also reported Geant4 libraries as missing only because those checks did
not first source the shared `geant4.sh`; the production shard does source it.
The three-node Xerces dependency inconsistency is repaired. Repeat the complete
eight-node smoke test to close the original reproduction loop.

The repeated full smoke test, Slurm job 154, passed on 2026-10-02. It selected
all eight documented nodes, allocated one 40-worker shard to each, completed all
800 requested events (100 per node), and merged them into
`cluster_runs/20261002_161157_c47e02be_cluster-smoke/combined_response.root`.
The merged ROOT file reported 800 simulated events and 49 `MyTree` rows. The
smoke tool verified eight distinct successful nodes and printed `PASS`; this
closes the original eight-node execution and Xerces failure reproduction loop.

## Outstanding uncertainties

- The exact node topology and Slurm CPU-binding result for 40 versus 80 workers
  must be captured from a real allocation.
- The active `roboaixrf` module may resolve inside the management virtualenv and
  may contain edits not present in this checkout. Inspect `roboaixrf.__file__`
  and diff it before deployment; do not overwrite an unverified virtualenv copy.
- Maximum throughput at 40 versus 80 workers is a measurement question. Use the
  checked-in paired benchmark and record its JSON result here after it is run.
- Management's original Python environment is no longer available at its
  reported path. The current `/home/mnode/TEMPO_XRF_REPO/.venv` is an incomplete
  root-owned copy of the laptop Python 3.14 venv: it contains `pip3.14` but no
  `bin/python`. Management's default `python3` is 3.9.25 and has no Streamlit.
  Python 3.11.13 is installed at `/usr/bin/python3.11`; a clean venv was created
  at `/srv/cluster/TEMPO_XRF_REPO/.venv` while management temporarily had Internet
  access. `uproot>=5` was added to management's stale frontend requirements and
  installed as uproot 5.7.6. The complete import check for Streamlit, NumPy,
  pandas, Plotly, Pydantic, SpekPy, uproot, Awkward, xraylib, Matplotlib, h5py,
  and tqdm passed on management. Running the checked-in launcher from
  `/srv/cluster/TEMPO_XRF_REPO` also passed its complete offline preflight using
  the shared Geant4 setup and this venv.

## Implemented cluster behavior

- The UI discovers expanded node names from the `debug` partition with `sinfo`.
  `TEMPO_XRF_CLUSTER_NODES` can override the inventory. The documented eight
  nodes are only a fallback, so future Slurm nodes appear without code changes.
  Discovery is cached for 30 seconds and currently returns names regardless of
  whether a node is `IDLE`, `DOWN`, or `DRAIN`. Visibility therefore does not
  prove readiness. Qualify each new node's Slurm state, NFS mount, Geant4
  dependencies, CPU topology, and memory with a targeted smoke test before
  selecting it for production.
- Single-node and distributed modes both use the same validated controller path.
- A distributed job requests all selected nodes together with one task per node,
  so shards start concurrently on distinct nodes rather than relying on array-job
  placement.
- For `N` shards, shard `i` receives
  `total // N + (i < total % N)` events. No zero-event shards are created.
- Each shard receives a unique adjacent seed pair derived from a recorded seed
  base. Benchmark pairs reuse the same seed base for 40 and 80 workers.
- Every node requests `--mem=185000`. Up to 40 workers uses
  `--hint=nomultithread`; 41–80 uses `--hint=multithread`; the requested Geant4
  workers and Slurm `--cpus-per-task` are identical. The `srun` step applies
  `--cpu-bind=verbose,threads`, and each marker records its observed Linux CPU
  affinity.
- `cluster_jobs/patch_ui.py` is now an idempotent integration verifier. It can no
  longer reapply the obsolete five-node/16-GiB patch.

The allocation choices follow SchedMD's documentation for
[`sbatch --hint`](https://slurm.schedmd.com/sbatch.html),
[`srun --cpus-per-task` and CPU binding](https://slurm.schedmd.com/srun.html), and
[multicore/multithread support](https://slurm.schedmd.com/mc_support.html).
Geant4's documented MT seeding assigns seeds to events independently of worker
count, which supports paired 40/80 comparisons with the same starting seed:
[Geant4 MT random-number seeding](https://geant4.web.cern.ch/documentation/pipelines/master/bftd_html/ForToolkitDeveloper/OOAnalysisDesign/Multithreading/mt.html#random-number-generation-seeding-in-mt).

## Local validation

Validated on the laptop on 2026-10-02:

```bash
python3 -m unittest -v tests.test_cluster_jobs
python3 -m compileall -q cluster_jobs roboaixrf/main.py \
  python_code/roboai_xrf_frontend/components/run_page.py tests
bash -n cluster_jobs/run_cluster.sbatch cluster_jobs/run_shard.sbatch
git diff --check
```

All 14 unit/integration tests passed. The tests cover eight-node fallback and
future-node override discovery, exact event division, independent seeds,
40/80 CPU and memory flags, one-task-per-node submission, success-marker gating,
an end-to-end shard macro rewrite with a fake simulator, and execution of the
controller from a simulated Slurm spool path. This laptop has no Slurm client, so
a real allocation still requires management.

The same 14-test suite passed under the clean Python 3.11 management venv after
the explicit shared-repository argument fix was selectively deployed. This
verifies the fix on management but is not yet a real Slurm allocation test.

## Verify management before deployment

The laptop's system SSH include has unsafe permissions, so use `-F /dev/null`.
Working credentials are still required. Retrieve to a temporary comparison
directory; do not copy management files over this working tree:

```bash
SNAPSHOT=$(mktemp -d /tmp/tempo-management.XXXXXX)
mkdir -p "$SNAPSHOT/cluster_jobs" "$SNAPSHOT/roboaixrf" \
  "$SNAPSHOT/python_code/roboai_xrf_frontend/components"
rsync -a -e 'ssh -F /dev/null' \
  mnode@192.168.10.3:/srv/cluster/TEMPO_XRF_REPO/cluster_jobs/ \
  "$SNAPSHOT/cluster_jobs/"
rsync -a -e 'ssh -F /dev/null' \
  mnode@192.168.10.3:/srv/cluster/TEMPO_XRF_REPO/roboaixrf/main.py \
  "$SNAPSHOT/roboaixrf/main.py"
rsync -a -e 'ssh -F /dev/null' \
  mnode@192.168.10.3:/srv/cluster/TEMPO_XRF_REPO/python_code/roboai_xrf_frontend/components/run_page.py \
  "$SNAPSHOT/python_code/roboai_xrf_frontend/components/run_page.py"
rsync -a -e 'ssh -F /dev/null' \
  mnode@192.168.10.3:/srv/cluster/TEMPO_XRF_REPO/python_code/roboai_xrf_frontend/builder.py \
  "$SNAPSHOT/python_code/roboai_xrf_frontend/builder.py"
diff -ru "$SNAPSHOT/cluster_jobs" cluster_jobs || true
diff -u "$SNAPSHOT/roboaixrf/main.py" roboaixrf/main.py || true
diff -u "$SNAPSHOT/python_code/roboai_xrf_frontend/components/run_page.py" \
  python_code/roboai_xrf_frontend/components/run_page.py || true
diff -u "$SNAPSHOT/python_code/roboai_xrf_frontend/builder.py" \
  python_code/roboai_xrf_frontend/builder.py || true
```

Inspect the package actually used by the management virtualenv before choosing
which `roboaixrf/main.py` to activate:

```bash
ssh -F /dev/null mnode@192.168.10.3 \
  'source /srv/cluster/TEMPO_XRF_REPO/.venv/bin/activate && cd /srv/cluster/TEMPO_XRF_REPO/python_code/roboai_xrf_frontend && python -c "import builder, roboaixrf; print(builder.__file__); print(roboaixrf.__file__)"'
```

If that path is in `site-packages`, retrieve and diff its sibling `main.py` too.
Merge any management-only edits into the repository version. Do not overwrite the
virtualenv copy blindly. The safer steady-state is to start the frontend with
`PYTHONPATH=/srv/cluster/TEMPO_XRF_REPO` after reconciling those differences.

## Selective deployment

The preferred deployment command is the guarded helper below. It refuses to run
on `mnode-01`, transfers only the intended files, and backs up replaced management
files under `.deployment_backups/before_dynamic_cluster`:

```bash
cd /home/nutty/Documents/TEMPO_XRF_REPO
./deploy_to_mnode.sh --dry-run
./deploy_to_mnode.sh
```

Preview exactly these files from the laptop repository root:

```bash
rsync -av --itemize-changes --dry-run --relative -e 'ssh -F /dev/null' \
  ./AGENTS.md ./docs/CLUSTER_CONTEXT.md \
  ./cluster_jobs/*.py ./cluster_jobs/*.sbatch \
  ./start_tempo_xrf.sh \
  ./roboaixrf/main.py \
  ./python_code/roboai_xrf_frontend/components/run_page.py \
  ./python_code/roboai_xrf_frontend/requirements.txt \
  ./tests/test_cluster_jobs.py \
  mnode@192.168.10.3:/srv/cluster/TEMPO_XRF_REPO/
```

After reviewing the itemized diff, repeat without `--dry-run`. This intentionally
does not deploy `python_code/roboai_xrf_frontend/builder.py`: it had pre-existing
laptop edits and the active management/package version is unverified.

Validate on management without submitting work:

```bash
cd /srv/cluster/TEMPO_XRF_REPO
source /srv/cluster/TEMPO_XRF_REPO/.venv/bin/activate
export PYTHONPATH=/srv/cluster/TEMPO_XRF_REPO${PYTHONPATH:+:$PYTHONPATH}
python -c 'import roboaixrf; print(roboaixrf.__file__)'
python -c 'import numpy, uproot; print(numpy.__version__, uproot.__version__)'
python -m unittest -v tests.test_cluster_jobs
python -m compileall -q cluster_jobs roboaixrf/main.py \
  python_code/roboai_xrf_frontend/components/run_page.py tests
bash -n cluster_jobs/run_cluster.sbatch cluster_jobs/run_shard.sbatch
python cluster_jobs/patch_ui.py .
test -r /srv/cluster/software/geant4-11.4.2/bin/geant4.sh
sinfo -h -N -p debug -o '%N %T %c %m'
```

## Offline frontend launcher

Management has no Internet access. `start_tempo_xrf.sh` is an offline-only
launcher: it never runs `git`, a package manager, or a download command. It
prefers the shared Geant4 installation at
`/srv/cluster/software/geant4-11.4.2/bin/geant4.sh`, uses the existing management
virtualenv at `/srv/cluster/TEMPO_XRF_REPO/.venv` when present, and starts the
frontend on `0.0.0.0:8502` by default. It checks all required imports before
starting and reports a missing dependency without trying to install it.

Management verification on 2026-10-02 found the shared source intact at
`/srv/cluster/TEMPO_XRF_REPO`, including the frontend `main.py`. A failed
recursive `scp` created a separate, root-owned, incomplete source and venv under
`/home/mnode/TEMPO_XRF_REPO`. Do not use or extend that copied venv, and do not
copy a virtualenv between machines. Rebuild the management environment offline
in a user-owned location after confirming the available Python ABI.

After selectively transferring the script over the local network, start it from
the shared repository:

```bash
cd /srv/cluster/TEMPO_XRF_REPO
./start_tempo_xrf.sh
```

An optional first argument changes the port, for example
`./start_tempo_xrf.sh 8501`. The paths and listen address can be overridden with
`TEMPO_XRF_REPO_DIR`, `TEMPO_XRF_VENV`, `TEMPO_XRF_GEANT4_SETUP`, and
`TEMPO_XRF_ADDRESS`. If a convenient home-directory command is wanted, create a
symlink once; the launcher resolves it back to the repository:

```bash
ln -s /srv/cluster/TEMPO_XRF_REPO/start_tempo_xrf.sh ~/start_tempo_xrf.sh
~/start_tempo_xrf.sh
```

Before restarting the frontend, an offline preflight can validate Geant4, the
virtualenv, and the required Python imports without opening a server:

```bash
cd /srv/cluster/TEMPO_XRF_REPO
./start_tempo_xrf.sh --check
```

Restart Streamlit only after `squeue -u "$USER" -n tempo-xrf` shows no run that
must be preserved. The service/unit name has not been verified. If the frontend
is run manually, stop that process cleanly and start it with the repository first
on `PYTHONPATH`:

```bash
cd /srv/cluster/TEMPO_XRF_REPO/python_code/roboai_xrf_frontend
source /srv/cluster/TEMPO_XRF_REPO/.venv/bin/activate
export PYTHONPATH=/srv/cluster/TEMPO_XRF_REPO${PYTHONPATH:+:$PYTHONPATH}
exec python -m streamlit run main.py
```

If it is managed by systemd, first identify the existing unit with
`systemctl --user --type=service | rg -i 'streamlit|tempo|xrf'`, add the same
`PYTHONPATH` to that unit's environment, then restart that existing unit. Do not
invent a new unit or overwrite the virtualenv package.

## Eight-node smoke test

Use a frontend run directory that already contains compiled `config.json` and
`run.mac`. The smoke test discovers all current `debug` nodes, requests them in a
single allocation, runs 100 events per node with 40 workers, refuses partial
publication, and verifies that every selected node produced one successful shard:

```bash
cd /srv/cluster/TEMPO_XRF_REPO
source /srv/cluster/TEMPO_XRF_REPO/.venv/bin/activate
export PYTHONPATH=/srv/cluster/TEMPO_XRF_REPO${PYTHONPATH:+:$PYTHONPATH}
python cluster_jobs/cluster_smoke_test.py \
  python_code/roboai_xrf_frontend/runs/full_xrf_example \
  --events-per-node 100 --workers 40
```

The command prints the artifact directory. Inspect `run_manifest.json`,
`combined_response.json`, and each `logs/slurm-<job>_<task>.log`; the first shard
line records node, event count, worker count, allocated CPUs, affinity, and seeds.

## Repeatable 40-versus-80 benchmark

Run both modes on the same exclusive node, with identical paired seeds and event
counts. Three repetitions alternate the order to reduce warm-cache and thermal
bias. The measured interval includes Geant4 initialization, event transport, and
ROOT writing, but excludes Slurm queue time and the later cross-shard merge:

```bash
cd /srv/cluster/TEMPO_XRF_REPO
source /srv/cluster/TEMPO_XRF_REPO/.venv/bin/activate
export PYTHONPATH=/srv/cluster/TEMPO_XRF_REPO${PYTHONPATH:+:$PYTHONPATH}
python cluster_jobs/benchmark_workers.py \
  python_code/roboai_xrf_frontend/runs/full_xrf_example \
  --node node-01-001 --events 100000 --repetitions 3 --seed-base 424242
```

Choose an event count that makes each timed run long enough to dominate startup
cost (at least a few minutes is preferable). The script reports median events/s,
the 80/40 throughput ratio, and the measured winner, and saves the full JSON under
the run's `benchmarks/` directory. Do not select 80 merely because more logical
CPUs are available; set the production worker count to the measured winner.

## Measurement status

A valid paired benchmark completed on `node-01-001` on 2026-10-02 using 100000
events per run, three repetitions per worker count, alternating execution order,
and paired seed bases beginning at 424242. Jobs 155--160 all completed and
produced matching paired event-row counts. Results:

- 40 workers: median **22568.57 events/s**
- 80 workers: median **28557.48 events/s**
- 80/40 throughput ratio: **1.265x**
- measured recommendation: **80 workers per node**

The full measurements are stored at
`python_code/roboai_xrf_frontend/runs/full_xrf_example/benchmarks/20261002_161346_workers.json`.
Use 80 workers for maximum measured throughput on this hardware; retain 40 as the
physical-core-only option. Each 100000-event run lasted only a few seconds, so a
larger-event confirmation is advisable if long-production-run performance must
be characterised independently of initialization and ROOT-output overhead.

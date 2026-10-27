# TEMPO XRF cluster context

Last updated: 2026-10-27

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
   frontend run and submits `cluster_jobs/run_cluster.sbatch` as a throttled Slurm
   job array. Distributed UI runs automatically recommend chunks per selected
   node from the saved event total, selected-node count, worker count, and
   representative timing from matching completed runs. A completed queue-depth
   benchmark for the same configuration, workers, selected nodes, and comparable
   event scale (within 2x) takes precedence and supplies its measured winner
   directly. A conservative fallback
   is used until such evidence exists; heuristic Automatic is capped at eight
   chunks per node, while a 1--32 manual override remains available. The array
   concurrency limit equals the selected-node count. Each array element requests
   one exclusive node, so a node that finishes can receive the next pending chunk
   instead of waiting for the slowest original assignment.
4. Each array element starts one `run_shard.sbatch` process. Every chunk has its
   own folder, event count, random seeds, macro, viewer-output path, and
   `simulation.root`. The shared repository path is passed explicitly because
   Slurm executes a spool copy of the batch script.
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
- The original verified compute set was `node-01-[001-004]` and
  `node-02-[001-004]` (eight total). On 2026-10-27 Slurm reported all 20
  registered nodes, through `node-05-004`, as `IDLE`. Live discovery includes
  registered nodes regardless of state, so every new session still requires a
  readiness check with `sinfo`.
- Each node: 40 physical cores and 80 logical CPUs with Hyper-Threading. Nineteen
  nodes reported 191544 MiB to Slurm on 2026-10-27; `node-03-003` reported
  257064 MiB. Reported topology is two sockets, 20 physical cores per socket,
  and two hardware threads per core.
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

## Laptop verification

The cluster implementation was saved locally as commit `f5217f5` on branch
`cluster-eight-node-2026-10-12`, based on upstream `front` commit `a623fc9`.
The custom files are present locally:

- `cluster_jobs/ui_cluster_run.py`
- `cluster_jobs/run_shard.sbatch`
- `cluster_jobs/combine_shards.py`
- `cluster_jobs/patch_ui.py`
- patched `roboaixrf/main.py`
- patched `python_code/roboai_xrf_frontend/components/run_page.py`
- patched `python_code/roboai_xrf_frontend/builder.py`

Historical five-shard run directories and logs are also present locally and show
successful runs on the shared `/srv/cluster/TEMPO_XRF_REPO` path.

On 2026-10-27, `origin/front` was fetched and had advanced to `ebabe36`. Its eight
new commits add only `final_fcking_setup_istg.sh`, `python_code/script.py`, and
`requirement.txt`; they do not overlap the saved cluster commit. The new installer
targets an online, full-machine rebuild under `$HOME`, defaults to branch
`new_geant4`, and includes a Geant4 null-secondary source patch. It must not be
blindly merged into the working NFS cluster installation. No upstream runtime or
cluster change needs to be ported for the queue work.

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

- The active `roboaixrf` module may resolve inside the management virtualenv and
  may contain edits not present in this checkout. Inspect `roboaixrf.__file__`
  and diff it before deployment; do not overwrite an unverified virtualenv copy.
- The management checkout was reported as being restored after an accidental
  replacement, but its final commit/file parity with this laptop branch has not
  been re-verified.
- A 2026-10-27 comparison between the live production checkout and the clean
  queue-test checkout found substantial overlap: `roboaixrf/main.py` produced a
  927-line diff and the frontend `run_page.py` a 229-line diff. Production has
  detector-noise, simulation-time, seed, and progress changes that are unrelated
  to the queue and must be preserved. All eight compared `cluster_jobs` runtime
  files also differ, including `combine_shards.py`. Do not copy these files over
  production wholesale; review the complete cluster diff and port only the
  dynamic-queue changes into the restored production implementation.
- The exact production versions of `roboaixrf/main.py`, frontend `run_page.py`,
  and `combine_shards.py` were retrieved into `management_snapshot_20261027` on
  the laptop. A separate `management_candidate_20261027` preserves the production
  controller/UI behavior, adds only the queue argument, selector, preview, and
  launcher flags, and uses the seven queue runtime files. Production's
  `combine_shards.py` is retained byte-for-byte, including its explicit uproot
  multithreaded file handler. The candidate passed all 17 cluster tests, Python
  compilation, both batch-script syntax checks, and the integration verifier.
  It was then used for the guarded selective production transfer.
- The guarded transfer to `/srv/cluster/TEMPO_XRF_REPO` completed with backups
  under `.deployment_backups/before_dynamic_queue_job597`. Initial production
  validation passed the 15 queue/runtime tests, the integration verifier, both
  batch syntax checks, and the offline launcher preflight. Two ancillary tests
  failed: production still had an older `deploy_to_mnode.sh` that omitted
  `tests/__init__.py`, and the restored launcher invokes the selected Python three
  times during preflight while the test required exactly one invocation. Every
  recorded invocation was the repository venv (`repo`), so no stale home venv was
  selected. The regression assertion now accepts one or more invocations only if
  all use the repository venv, and the current deployment helper includes the
  package marker. Both the laptop and merged-candidate suites then passed all 17
  tests. After deploying those two corrected files, the production suite also
  passed all 17 tests.
- The Slurm job-array queue passed live management smoke tests from both the
  clean side-by-side checkout and the selectively merged production checkout.
  Restarting the production UI and submitting one small UI-driven queue run are
  the remaining operational checks.
- Production Slurm job 805 completed one billion events using all 20 nodes,
  **70** workers per node, four chunks per node, 80 total chunks, and concurrency
  20. Every node processed exactly four chunks and 50 million events; faster
  nodes did not take a fifth chunk. Per-chunk time was 45.970--49.659 seconds
  (median 47.523), with median effective per-node throughput 263,029.97 events/s.
  Summed node time was 3,804.717 seconds, or 262,831.62 events/s per node. Node
  four-chunk totals were 186.889--195.948 seconds with only 1.15% coefficient of
  variation. This cluster was too uniform for four-chunk dynamic redistribution
  to help this run; repeated initialization and merging are now the leading
  explanation for the small regression. The UI/tqdm rate of 5,477,872.86
  events/s was a smoothed progress rate, not final wall throughput: even before
  queue and merge overhead, the slowest node bounds aggregate completion at
  about 5,103,395 events/s. This motivated a controlled production-scale matrix
  instead of using the progress-rate peak as the result.
- The production matrix subsequently completed with 500 million events per run
  on the same 20 nodes. It measured 70 and 80 workers with 1, 2, and 4 chunks per
  node. **80 workers with one chunk per node won at 5,119,920.84 events/s end to
  end.** At 80 workers, two chunks reached 4,707,859.15 events/s and four reached
  4,348,161.68; one chunk was therefore 8.75% faster than two and 17.75% faster
  than four. At one chunk, 80 workers was 9.22% faster than 70. Relative to the
  old 70-worker/four-chunk setting measured in the same matrix, the winning
  combination increased throughput by 27.80%. The production recommendation is
  now 80 workers and one chunk per selected node for this configuration and node
  set. The retained benchmark JSON is
  `benchmarks/20261027_140740_chunks.json`; Automatic mode consumes it.
- The first Automatic implementation used the short 100,000-event 40/80 worker
  benchmark as an absolute production throughput calibration. That benchmark
  was startup-dominated and would have capped a one-billion-event run at 32
  chunks per node (640 chunks on 20 nodes), repeating too much Geant4/ROOT/NFS
  overhead. That calibration must not be deployed. The corrected implementation
  learns only from matching completed runs whose median chunk lasts at least 15
  seconds, targets about 60 seconds per chunk, and caps Automatic at eight chunks
  per node. Manual still permits 1--32 for controlled benchmarks.
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

The first post-queue validation attempt on management could run the integration
verifier but `python -m unittest -v tests.test_cluster_jobs` imported a different
top-level `tests` package from the virtualenv and reported that the repository
test module did not exist. The repository's `tests/` directory had no
`__init__.py`, so it was only a namespace-package candidate and could be shadowed
by a regular installed package. `tests/__init__.py` is now source controlled and
included in selective deployment. Until that one file is deployed, run the suite
directly with `PYTHONPATH=/srv/cluster/TEMPO_XRF_REPO python
tests/test_cluster_jobs.py -v`.

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
- A distributed run creates `selected_nodes * chunks_per_node` chunks, limited by
  the event count. Automatic mode scans this compiled run's history for up to
  three recent complete runs with the same `config.json` and worker count. It
  accepts them only when every chunk has a valid marker and the median chunk is
  at least 15 seconds, then calculates event-weighted per-node throughput. Until
  a matching long run exists, it uses a conservative production-scale fallback
  of 217,000 events/s at 40 workers and 274,000 at 80. It targets approximately
  60 seconds per chunk and returns 1--8 chunks per selected node. Manual mode can
  override that value from 1--32. Slurm receives a one-node job array with
  `%selected_nodes` throttling, so at most one full-node chunk runs per selected
  node at once and newly free nodes take pending chunks. Single-node UI runs use
  one chunk by default.
- A `benchmarks/*_chunks.json` result with the same `config.json` hash, exact
  selected-node set, worker count, and event total within a factor of two
  overrides the duration heuristic with its measured queue winner. Changing the
  configuration, nodes, workers, or workload scale safely falls back to learned
  representative timing or the conservative default.
- Queue calculations use the saved Beam-on events value. The UI names and formats
  that total explicitly and instructs the user to click **Save run settings**
  after editing events or workers. The confusing preview of 80 chunks at 1,250
  events came from the previously saved 100,000-event value; 10,000,000 events
  with that same explicit queue shape would have produced 125,000-event chunks.
- For `N` chunks, chunk `i` receives
  `total // N + (i < total % N)` events. No zero-event shards are created.
- Each shard receives a unique adjacent seed pair derived from a recorded seed
  base. Benchmark pairs reuse the same seed base for 40 and 80 workers.
- Every node requests `--mem=185000`. Up to 40 workers uses
  `--hint=nomultithread`; 41–80 uses `--hint=multithread`; the requested Geant4
  workers and Slurm `--cpus-per-task` are identical. The `srun` step applies
  `--cpu-bind=verbose,threads`, and each marker records its observed Linux CPU
  affinity.
- Repeated node names in successful markers are expected. `run_manifest.json`
  records `chunk_count`, `max_concurrent_chunks`, the execution node for every
  chunk, a `node_summary` containing per-node chunk/event/timing totals, the
  copied-config hash, Slurm queue/execution time, merge time, and complete
  controller wall time.
- Every chunk must validate before the merged result is published. A failed or
  missing chunk still permits a diagnostic merge of successful outputs inside
  the timestamped run directory, never publication as the UI result.
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

Validated on the laptop on 2026-10-27:

```bash
python3 -m unittest -v tests.test_cluster_jobs
python3 -m compileall -q cluster_jobs roboaixrf/main.py \
  python_code/roboai_xrf_frontend/components/run_page.py tests
bash -n cluster_jobs/run_cluster.sbatch cluster_jobs/run_shard.sbatch
git diff --check
```

All 23 unit/integration tests passed. The tests cover eight-node fallback and
future-node override discovery, exact event division, adaptive-throughput chunk
recommendations and bounds, representative-history filtering, measured-winner
matching by configuration/nodes/workers, benchmark-list validation, independent
seeds, 40/80 CPU and memory flags, throttled job-array
submission, repeated-node queue completion, per-node accounting, success-marker
gating, an end-to-end shard macro rewrite with a fake simulator, and execution
of the controller from a simulated Slurm spool path.
This laptop has no Slurm client, so the real array allocation was validated on
management as recorded below.

After deploying commit `6abc0ae` to `/srv/cluster/TEMPO_XRF_REPO`, all 23 tests
also passed in the production management virtualenv on 2026-10-27. The job 805
artifact directory occupied 326 MiB, and `/srv/cluster` had 46 GiB free on its
70-GiB filesystem. The planned six-run, 500-million-event benchmark matrix is
therefore expected to require roughly 1 GiB and has adequate free space.

The current 17-test suite also passed under the Python 3.11 management venv from
the clean NFS-visible checkout `/srv/cluster/TEMPO_XRF_QUEUE_TEST`. Both batch
scripts passed `bash -n`, and `patch_ui.py` confirmed that cluster integration is
present.

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
  ./tests/__init__.py \
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

## Dynamic-queue smoke test

Use a frontend run directory that already contains compiled `config.json` and
`run.mac`. The smoke test discovers all current `debug` nodes, creates two queued
chunks per selected node, runs 100 total events per selected node with 40 workers,
refuses partial publication, verifies the complete event count, and prints how
many chunks each eligible node actually processed:

```bash
cd /srv/cluster/TEMPO_XRF_REPO
source /srv/cluster/TEMPO_XRF_REPO/.venv/bin/activate
export PYTHONPATH=/srv/cluster/TEMPO_XRF_REPO${PYTHONPATH:+:$PYTHONPATH}
python cluster_jobs/cluster_smoke_test.py \
  python_code/roboai_xrf_frontend/runs/full_xrf_example \
  --events-per-node 100 --chunks-per-node 2 --workers 40
```

The command prints the artifact directory and `node_summary`. Inspect
`run_manifest.json`, `combined_response.json`, and each
`logs/slurm-<array>_<chunk>.log`; the first chunk line records node, event count,
worker count, allocated CPUs, affinity, and seeds.

The first live dynamic-queue smoke test passed on management on 2026-10-27 as
Slurm job 589. It ran commit `550f820` from the clean side-by-side checkout
`/srv/cluster/TEMPO_XRF_QUEUE_TEST`, using `node-01-001` through
`node-01-004`, 40 workers, two chunks per node, and 100 total events per selected
node. Slurm completed all eight queued chunks with a maximum concurrency of four.
Each node processed two chunks and 100 events; all 400 requested events validated
and merged successfully. The artifacts are under
`cluster_runs/20261027_125541_62cba83c_cluster-smoke` in that checkout's
`full_xrf_example` run directory. This validates array scheduling, repeated-node
work pickup, shared-NFS markers, complete-result gating, and merging. It does not
yet replace the dirty production checkout or prove the best production chunk
duration.

A subsequent all-node qualification passed as Slurm job 597. It discovered and
selected all 20 registered nodes from `node-01-001` through `node-05-004`, queued
40 chunks with maximum concurrency 20, and completed two 50-event chunks on every
node. All 2,000 requested events validated and merged; the combined ROOT file had
119 `MyTree` rows. Its artifacts are under
`cluster_runs/20261027_125803_da9009d6_cluster-smoke` in the clean checkout's
`full_xrf_example` run directory. This verifies the NFS path, executable runtime
dependencies, 40-CPU allocations, result markers, and queue execution on all 20
nodes.

After the selective merge into `/srv/cluster/TEMPO_XRF_REPO`, the production
path passed its smoke test as Slurm job 637. It used `node-01-001` through
`node-01-004`, queued eight chunks with concurrency four, completed two 50-event
chunks on every node, and validated and merged all 400 requested events. The
preserved production `combine_shards.py` completed successfully and reported
five `MyTree` rows. Artifacts are under
`cluster_runs/20261027_131259_55dae82c_cluster-smoke` in the production
`full_xrf_example` run directory. This closes the production-path deployment and
merger compatibility check; only a frontend restart and UI-submitted run remain.

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

## Repeatable queue-depth benchmark

`cluster_jobs/benchmark_chunks.py` compares a matrix of worker counts and chunk
counts on the same compiled configuration, nodes, event count, and paired seed
base. It measures end-to-end controller time, including Slurm waiting,
Geant4, validation, and merging, because that is the throughput visible to the
user. With two repetitions it reverses the order in the second pass to reduce
temperature, cache, or cluster-drift bias. It never publishes over the current
top-level result.

For the production workload, first compare the observed 70-worker setting with
80 workers and 1/2/4 chunks using 500 million events. This keeps every tested
chunk representative while reducing the initial six-run matrix cost:

```bash
cd /srv/cluster/TEMPO_XRF_REPO
source .venv/bin/activate
export PYTHONPATH=/srv/cluster/TEMPO_XRF_REPO
python cluster_jobs/benchmark_chunks.py \
  python_code/roboai_xrf_frontend/runs/full_xrf_example \
  --events 500000000 --workers 70,80 --chunks 1,2,4 --repetitions 1
```

Use `--nodes` to reproduce an exact UI selection. The script writes every job ID,
artifact path, node distribution, Geant4 node-seconds, end-to-end rate, medians,
the winner for each worker count, and the overall winning combination under
`<config_dir>/benchmarks/*_chunks.json`. Automatic mode consumes the per-worker
winner only when configuration, selected-node set, and event scale match. Run a second
alternating repetition only if the first-place margin is small or results are
noisy. Check NFS free space first because every non-published benchmark still
retains its shard and combined ROOT artifacts.

Job 805 confirmed representative 47.5-second chunks and about 262,832 events/s
per node at 70 workers. Its node totals varied by only 1.15%, and all nodes ran
exactly four chunks, so extra queue depth did not rebalance work. This evidence
led to the production-scale worker/chunk matrix below.

That comparison is now complete. Results in end-to-end events/s were:

| Workers | 1 chunk/node | 2 chunks/node | 4 chunks/node |
|---:|---:|---:|---:|
| 70 | 4,687,623.83 | 4,493,892.23 | 4,006,266.53 |
| 80 | **5,119,920.84** | 4,707,859.15 | 4,348,161.68 |

Use **80 workers and one chunk per selected node** for this full-XRF workload on
the current 20-node set. Retain additional chunks as a manual option for a future
heterogeneous cluster, transiently unreliable nodes, or a materially different
configuration whose own benchmark demonstrates a benefit.

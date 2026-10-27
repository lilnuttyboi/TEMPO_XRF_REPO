from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from cluster_jobs.cluster_config import (
    DEFAULT_NODES,
    MEMORY_MIB_PER_NODE,
    build_sbatch_command,
    calculate_queue_shape,
    discover_nodes,
    parse_node_list,
    seed_pairs,
    split_events,
    worker_resource_args,
)
from cluster_jobs.combine_shards import parse_macro, successful_shard_ids
from cluster_jobs.ui_cluster_run import run_cluster


class ClusterConfigTests(unittest.TestCase):
    def test_documented_fallback_contains_all_eight_nodes(self):
        self.assertEqual(
            DEFAULT_NODES,
            (
                "node-01-001",
                "node-01-002",
                "node-01-003",
                "node-01-004",
                "node-02-001",
                "node-02-002",
                "node-02-003",
                "node-02-004",
            ),
        )

    def test_node_list_is_expanded_validated_and_deduplicated(self):
        self.assertEqual(
            parse_node_list("node-01-001, node-01-002 node-01-001"),
            ("node-01-001", "node-01-002"),
        )
        with self.assertRaises(ValueError):
            parse_node_list("node-01-[001-004]")

    def test_environment_inventory_supports_future_nodes(self):
        with mock.patch.dict(
            "os.environ", {"TEMPO_XRF_CLUSTER_NODES": "node-03-001,node-03-002"}
        ):
            inventory = discover_nodes()
        self.assertEqual(inventory.nodes, ("node-03-001", "node-03-002"))
        self.assertEqual(inventory.source, "TEMPO_XRF_CLUSTER_NODES")

    def test_event_split_preserves_total_and_distributes_remainder(self):
        self.assertEqual(split_events(13, 5), (3, 3, 3, 2, 2))
        self.assertEqual(sum(split_events(1_000_003, 8)), 1_000_003)

    def test_seed_pairs_are_independent(self):
        pairs = seed_pairs(424_242, 8)
        self.assertEqual(pairs[0], (424_242, 424_243))
        self.assertEqual(len({seed for pair in pairs for seed in pair}), 16)

    def test_40_worker_allocation_uses_physical_cores(self):
        args = worker_resource_args(40)
        self.assertIn("--cpus-per-task=40", args)
        self.assertIn("--hint=nomultithread", args)
        self.assertIn(f"--mem={MEMORY_MIB_PER_NODE}", args)

    def test_80_worker_allocation_uses_both_hardware_threads(self):
        args = worker_resource_args(80)
        self.assertIn("--cpus-per-task=80", args)
        self.assertIn("--hint=multithread", args)
        self.assertIn("--mem=185000", args)
        self.assertNotIn("--mem=16G", args)
        self.assertFalse(any(item.startswith("--threads-per-core") for item in args))

    def test_queue_shape_creates_multiple_chunks_per_node(self):
        self.assertEqual(calculate_queue_shape(803, 8, 4), (32, 8))
        self.assertEqual(calculate_queue_shape(3, 8, 4), (3, 3))

    def test_sbatch_uses_a_throttled_one_node_job_array(self):
        command = build_sbatch_command(
            repo=Path("/srv/cluster/TEMPO_XRF_REPO"),
            run_root=Path("/srv/cluster/run"),
            total_events=803,
            workers=80,
            nodes=DEFAULT_NODES,
            seed_base=123_456,
            chunks_per_node=4,
        )
        self.assertIn("--nodes=1", command)
        self.assertIn("--ntasks=1", command)
        self.assertIn("--ntasks-per-node=1", command)
        self.assertIn("--exclusive", command)
        self.assertIn("--array=0-31%8", command)
        self.assertIn(f"--nodelist={','.join(DEFAULT_NODES)}", command)
        self.assertIn("--output=/srv/cluster/run/logs/slurm-%A_%a.log", command)
        self.assertEqual(command[-1], "/srv/cluster/TEMPO_XRF_REPO")


class LauncherTests(unittest.TestCase):
    def test_deployment_includes_a_real_tests_package(self):
        repo = Path(__file__).parents[1]
        self.assertTrue((repo / "tests" / "__init__.py").is_file())
        deployment = (repo / "deploy_to_mnode.sh").read_text(encoding="utf-8")
        self.assertIn("./tests/__init__.py", deployment)

    def test_shared_repo_venv_is_preferred_over_stale_home_copy(self):
        source_script = Path(__file__).parents[1] / "start_tempo_xrf.sh"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            launcher = repo / "start_tempo_xrf.sh"
            shutil.copy2(source_script, launcher)
            (repo / "python_code" / "roboai_xrf_frontend").mkdir(parents=True)
            (repo / "python_code" / "roboai_xrf_frontend" / "main.py").touch()

            home = root / "home"
            marker = root / "selected-python.txt"
            for label, venv in (
                ("home", home / "TEMPO_XRF_REPO" / ".venv"),
                ("repo", repo / ".venv"),
            ):
                bindir = venv / "bin"
                bindir.mkdir(parents=True)
                python = bindir / "python"
                python.write_text(
                    "#!/usr/bin/env bash\n"
                    f"printf '{label}\\n' >> \"$TEMPO_TEST_PYTHON_MARKER\"\n",
                    encoding="utf-8",
                )
                python.chmod(0o755)
                (bindir / "activate").write_text(
                    'export PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd):$PATH"\n',
                    encoding="utf-8",
                )

            geant4_setup = root / "geant4.sh"
            geant4_setup.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "TEMPO_TEST_PYTHON_MARKER": str(marker),
                    "TEMPO_XRF_GEANT4_SETUP": str(geant4_setup),
                }
            )

            result = subprocess.run(
                ["bash", str(launcher), "--check"],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertIn(f"Python:     {repo / '.venv'}/bin/python", result.stdout)
            self.assertEqual(marker.read_text(encoding="utf-8"), "repo\n")


class MergeGateTests(unittest.TestCase):
    def test_only_success_markers_are_discovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for shard_id in (0, 2):
                folder = root / f"shard-{shard_id}"
                folder.mkdir()
                (folder / "success.json").write_text(
                    json.dumps({"id": shard_id, "exit_code": 0}), encoding="utf-8"
                )
            (root / "shard-1").mkdir()
            (root / "shard-1" / "simulation.root").touch()
            self.assertEqual(successful_shard_ids(root), (0, 2))

    def test_macro_parser_requires_one_event_count_and_seed_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "run.mac").write_text(
                "/run/numberOfThreads 80\n"
                "/random/setSeeds 100 101\n"
                "/run/initialize\n"
                "/run/beamOn 123\n",
                encoding="utf-8",
            )
            self.assertEqual(parse_macro(folder), (123, (100, 101)))


class ShardScriptTests(unittest.TestCase):
    def test_shard_rewrites_macro_and_writes_success_marker(self):
        source_script = Path(__file__).parents[1] / "cluster_jobs" / "run_shard.sbatch"
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            script = repo / "cluster_jobs" / "run_shard.sbatch"
            script.parent.mkdir(parents=True)
            shutil.copy2(source_script, script)

            executable = repo / "geant4_code" / "build" / "sim"
            executable.parent.mkdir(parents=True)
            executable.write_text(
                "#!/usr/bin/env bash\n"
                "touch \"$(dirname \"$1\")/simulation.root\"\n"
                "echo 'Done recording'\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)

            geant4_setup = repo / "geant4.sh"
            geant4_setup.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            run_root = repo / "run"
            run_root.mkdir()
            (run_root / "config.json").write_text("{}\n", encoding="utf-8")
            (run_root / "run.mac").write_text(
                "/run/numberOfThreads 6\n"
                "/run/initialize\n"
                "/run/printProgress 10\n"
                "/run/beamOn 11\n",
                encoding="utf-8",
            )

            environment = os.environ.copy()
            environment.update(
                {
                    "SLURM_PROCID": "1",
                    "SLURM_CPUS_PER_TASK": "80",
                    "TEMPO_XRF_GEANT4_SETUP": str(geant4_setup),
                }
            )
            subprocess.run(
                ["bash", str(script), str(run_root), "11", "80", "3", "1000"],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )

            folder = run_root / "shard-1"
            status = json.loads((folder / "success.json").read_text(encoding="utf-8"))
            self.assertEqual(status["events"], 4)
            self.assertEqual(status["workers"], 80)
            self.assertEqual(status["seeds"], [1002, 1003])
            self.assertEqual(parse_macro(folder), (4, (1002, 1003)))
            macro = (folder / "run.mac").read_text(encoding="utf-8")
            self.assertIn("/run/numberOfThreads 80", macro)


class ControllerScriptTests(unittest.TestCase):
    def test_controller_uses_explicit_repo_when_slurm_copies_script_to_spool(self):
        source_script = Path(__file__).parents[1] / "cluster_jobs" / "run_cluster.sbatch"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "shared-repo"
            shard_script = repo / "cluster_jobs" / "run_shard.sbatch"
            shard_script.parent.mkdir(parents=True)
            shard_script.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s|%s\\n' \"${BASH_SOURCE[0]}\" \"${6:-}\" "
                "> \"$TEMPO_TEST_OUTPUT\"\n",
                encoding="utf-8",
            )

            spool_script = root / "var" / "spool" / "slurmd" / "slurm_script"
            spool_script.parent.mkdir(parents=True)
            shutil.copy2(source_script, spool_script)

            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_srun = fake_bin / "srun"
            fake_srun.write_text(
                "#!/usr/bin/env bash\n"
                "while (( $# )); do\n"
                "  if [[ $1 == bash ]]; then exec \"$@\"; fi\n"
                "  shift\n"
                "done\n"
                "exit 99\n",
                encoding="utf-8",
            )
            fake_srun.chmod(0o755)

            run_root = root / "run"
            (run_root / "logs").mkdir(parents=True)
            output = root / "launched-shard.txt"
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
                    "SLURM_NTASKS": "1",
                    "SLURM_JOB_ID": "123",
                    "SLURM_ARRAY_TASK_ID": "0",
                    "SLURM_JOB_NODELIST": "node-01-001",
                    "TEMPO_TEST_OUTPUT": str(output),
                }
            )

            subprocess.run(
                [
                    "bash",
                    str(spool_script),
                    str(run_root),
                    "10",
                    "40",
                    "1",
                    "1000",
                    str(repo),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )

            launched_script, chunk_id = output.read_text(encoding="utf-8").strip().split("|")
            self.assertEqual(Path(launched_script), shard_script)
            self.assertEqual(chunk_id, "0")


class RunnerTests(unittest.TestCase):
    def test_controller_accepts_reused_nodes_and_records_queue_distribution(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary) / "compiled"
            config_dir.mkdir()
            (config_dir / "config.json").write_text("{}\n", encoding="utf-8")
            (config_dir / "run.mac").write_text(
                "/run/numberOfThreads 40\n/run/initialize\n/run/beamOn 5\n",
                encoding="utf-8",
            )
            nodes = ("node-01-001", "node-01-002")
            markers_written = False

            def fake_run(command, **_kwargs):
                nonlocal markers_written
                if command[0] == "sbatch":
                    return subprocess.CompletedProcess(command, 0, "123\n", "")
                if command[0] == "squeue":
                    if not markers_written:
                        run_root = next((config_dir / "cluster_runs").iterdir())
                        for shard_id, (events, seeds, node) in enumerate(
                            (
                                (3, [1000, 1001], nodes[0]),
                                (2, [1002, 1003], nodes[1]),
                                (2, [1004, 1005], nodes[0]),
                                (2, [1006, 1007], nodes[1]),
                            )
                        ):
                            folder = run_root / f"shard-{shard_id}"
                            folder.mkdir()
                            (folder / "simulation.root").write_bytes(b"shard")
                            (folder / "success.json").write_text(
                                json.dumps(
                                    {
                                        "id": shard_id,
                                        "node": node,
                                        "events": events,
                                        "workers": 40,
                                        "affinity": "0-39",
                                        "seeds": seeds,
                                        "exit_code": 0,
                                        "elapsed_ns": 1_000_000,
                                    }
                                ),
                                encoding="utf-8",
                            )
                        markers_written = True
                    return subprocess.CompletedProcess(command, 0, "", "")
                if "combine_shards.py" in " ".join(map(str, command)):
                    run_root = Path(command[2])
                    (run_root / "combined_response.root").write_bytes(b"combined")
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise AssertionError(f"Unexpected command: {command}")

            with mock.patch(
                "cluster_jobs.ui_cluster_run.subprocess.run", side_effect=fake_run
            ):
                manifest = run_cluster(
                    config_dir=config_dir,
                    total_events=9,
                    workers=40,
                    nodes=nodes,
                    seed_base=1000,
                    chunks_per_node=2,
                )

            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["event_counts"], [3, 2, 2, 2])
            self.assertEqual(manifest["actual_nodes"], [nodes[0], nodes[1]] * 2)
            self.assertEqual(manifest["chunk_count"], 4)
            self.assertEqual(manifest["max_concurrent_chunks"], 2)
            self.assertEqual(
                manifest["node_summary"],
                {
                    nodes[0]: {
                        "chunks": 2,
                        "events": 5,
                        "elapsed_ns": 2_000_000,
                    },
                    nodes[1]: {
                        "chunks": 2,
                        "events": 4,
                        "elapsed_ns": 2_000_000,
                    },
                },
            )
            self.assertEqual((config_dir / "simulation.root").read_bytes(), b"combined")
            self.assertIn("--mem=185000", manifest["sbatch_command"])

    def test_failed_shard_keeps_partial_merge_unpublished(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary) / "compiled"
            config_dir.mkdir()
            (config_dir / "config.json").write_text("{}\n", encoding="utf-8")
            (config_dir / "run.mac").write_text(
                "/run/numberOfThreads 40\n/run/initialize\n/run/beamOn 4\n",
                encoding="utf-8",
            )
            markers_written = False

            def fake_run(command, **_kwargs):
                nonlocal markers_written
                if command[0] == "sbatch":
                    return subprocess.CompletedProcess(command, 0, "124\n", "")
                if command[0] == "squeue":
                    if not markers_written:
                        run_root = next((config_dir / "cluster_runs").iterdir())
                        first = run_root / "shard-0"
                        first.mkdir()
                        (first / "simulation.root").write_bytes(b"shard")
                        (first / "success.json").write_text(
                            json.dumps(
                                {
                                    "id": 0,
                                    "node": "node-01-001",
                                    "events": 2,
                                    "workers": 40,
                                    "seeds": [2000, 2001],
                                    "exit_code": 0,
                                    "elapsed_ns": 1_000_000,
                                }
                            ),
                            encoding="utf-8",
                        )
                        second = run_root / "shard-1"
                        second.mkdir()
                        (second / "failure.json").write_text(
                            '{"id":1,"exit_code":1}\n', encoding="utf-8"
                        )
                        markers_written = True
                    return subprocess.CompletedProcess(command, 0, "", "")
                if "combine_shards.py" in " ".join(map(str, command)):
                    self.assertEqual(command[-1], "0")
                    run_root = Path(command[2])
                    (run_root / "combined_response.root").write_bytes(b"partial")
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[0] == "scancel":
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise AssertionError(f"Unexpected command: {command}")

            with mock.patch(
                "cluster_jobs.ui_cluster_run.subprocess.run", side_effect=fake_run
            ):
                with self.assertRaisesRegex(RuntimeError, "validation failure"):
                    run_cluster(
                        config_dir=config_dir,
                        total_events=4,
                        workers=40,
                        nodes=("node-01-001", "node-01-002"),
                        seed_base=2000,
                    )

            run_root = next((config_dir / "cluster_runs").iterdir())
            manifest = json.loads(
                (run_root / "run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "failed")
            self.assertTrue((run_root / "combined_response.root").is_file())
            self.assertFalse((config_dir / "simulation.root").exists())


if __name__ == "__main__":
    unittest.main()

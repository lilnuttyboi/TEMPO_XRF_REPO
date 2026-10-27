from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import streamlit as st
import streamlit.components.v1 as components

from builder import build_simulation

REPO_PATH = Path(__file__).resolve().parents[3]
if str(REPO_PATH) not in sys.path:
    sys.path.append(str(REPO_PATH))

from cluster_jobs.cluster_config import (
    MAX_AUTOMATIC_CHUNKS_PER_NODE,
    MAX_CHUNKS_PER_NODE,
    RECOMMENDED_CHUNK_SECONDS,
    calculate_queue_shape,
    discover_nodes,
    estimate_node_throughput,
    load_queue_benchmark_recommendation,
    load_throughput_calibration,
    recommend_chunks_per_node,
)
from components.common import page_header
from state import (
    configuration_fingerprint,
    stop_geant4_process,
)


def _simulation_is_current() -> bool:
    sim = st.session_state.get("simulation")

    if sim is None:
        return False

    return (
        st.session_state.get("compiled_fingerprint")
        == configuration_fingerprint(
            st.session_state.ui_config
        )
    )


@st.cache_data(ttl=30, show_spinner=False)
def _cluster_inventory() -> tuple[tuple[str, ...], str, str | None]:
    inventory = discover_nodes()
    return inventory.nodes, inventory.source, inventory.warning


@st.cache_data(ttl=30, show_spinner=False)
def _calibrated_throughput(config_dir: str, workers: int):
    return load_throughput_calibration(Path(config_dir), workers)


@st.cache_data(ttl=30, show_spinner=False)
def _queue_benchmark(
    config_dir: str,
    workers: int,
    nodes: tuple[str, ...],
    total_events: int,
):
    return load_queue_benchmark_recommendation(
        Path(config_dir),
        workers,
        nodes,
        total_events,
    )


def _finalize_finished_process() -> None:

    process = st.session_state.get(
        "geant4_process"
    )

    if process is None:
        return

    return_code = process.poll()

    # Still running
    if return_code is None:
        st.session_state.geant4_running = True
        return

    # Process finished
    st.session_state.geant4_running = False
    st.session_state.geant4_process = None

    # FAILED

    if return_code != 0:

        st.session_state.run_complete = False
        st.session_state.last_root_file = None

        st.error(
            f"Geant4 exited with code {return_code}."
        )

        return

    # SUCCESS

    sim = st.session_state.get(
        "simulation"
    )

    if sim is None:

        st.session_state.run_complete = False
        st.session_state.last_root_file = None

        st.error(
            "Geant4 finished, but the simulation object "
            "is no longer available."
        )

        return

    root_file = (
        Path(sim.config_path)
        / "simulation.root"
    )

    if not root_file.is_file():

        st.session_state.run_complete = False
        st.session_state.last_root_file = None

        st.error(
            f"Geant4 finished, but simulation.root "
            f"was not found: {root_file}"
        )

        return

    # COMPLETE

    st.session_state.run_complete = True
    st.session_state.last_root_file = str(
        root_file
    )


@st.fragment(run_every="1s")
def _running_simulation_panel():

    sim = st.session_state.get("simulation")
    process = st.session_state.get(
        "geant4_process"
    )

    if process is None:
        st.info("No simulation process found.")
        return

    if sim is None:
        st.warning("No simulation object.")
        return
    if sim._tqdm_output =="":
        return
    current_percentage=sim._tqdm_output/sim._total_run



    st.progress(current_percentage, text=f"Geant4: {int(current_percentage*100)}%")
    if process.poll() is None:

        if st.button(
            "■ Stop simulation",
            type="primary",
            width="stretch",
            key="stop_geant4_live",
        ):

            stop_geant4_process()
            st.rerun()
        return

    _finalize_finished_process()

    st.rerun()
def render_run_page():
    cfg = st.session_state.ui_config
    run = cfg["run"]

    # Check any background job whenever this page reruns.
    _finalize_finished_process()

    page_header(
        "Build & run",
        "Compile creates config.json and the SpekPy source. "
        "Run executes the Geant4 backend through the roboaixrf package.",
    )

    with st.form("run_settings_form"):

        c1, c2 = st.columns([1.4, 1])

        run_name = c1.text_input(
            "Run name",
            value=run["run_name"],
        )

        threads = c2.number_input(
            "Workers per node",
            min_value=1,
            max_value=80,
            value=int(
                run["number_of_thread"]
            ),
            step=1,
            help=(
                "Use 40 for one worker per physical core or 80 to use both "
                "hardware threads on each core. Benchmark both for throughput."
            ),
        )

        c1, c2 = st.columns(2)

        beam_on = c1.number_input(
            "Beam-on events",
            min_value=2,
            value=int(
                run["beam_on"]
            ),
            step=1000,
            format="%d",
        )

        # print_display = c2.number_input(
        #     "Print progress every",
        #     min_value=1,
        #     value=int(
        #         run["print_display"]
        #     ),
        #     step=1000,
        #     format="%d",
        # )

        c1, c2 = st.columns(2)

        vis_beam = c1.number_input(
            "Visualization events",
            min_value=1,
            value=int(
                run["vis_beam_on"]
            ),
            step=1,
        )

        vis_threads = c2.number_input(
            "Visualization threads",
            min_value=1,
            value=int(
                run["vis_threads"]
            ),
            step=1,
        )

        if st.form_submit_button(
            "Save run settings"
        ):
            run.update(
                {
                    "run_name": run_name.strip(),
                    "beam_on": int(beam_on),
                    "number_of_thread": int(
                        threads
                    ),
                    # "print_display": int(
                    #     print_display
                    # ),
                    "vis_beam_on": int(
                        vis_beam
                    ),
                    "vis_threads": int(
                        vis_threads
                    ),
                }
            )

            st.success(
                "Run settings saved."
            )

    st.divider()

    node_options, inventory_source, inventory_warning = _cluster_inventory()
    execution_mode = st.radio(
        "Cluster execution",
        options=("Single node", "Distributed"),
        horizontal=True,
        key="cluster_execution_mode",
    )
    estimated_throughput = estimate_node_throughput(
        int(run["number_of_thread"])
    )
    if execution_mode == "Single node":
        selected_nodes = [
            st.selectbox(
                "Compute node",
                options=node_options,
                key="single_cluster_node",
            )
        ]
        chunks_per_node = 1
    else:
        selected_nodes = st.multiselect(
            "Compute nodes",
            options=node_options,
            default=list(node_options),
            key="distributed_cluster_nodes",
            help=(
                "Queued chunks can run on any selected node. Faster nodes take "
                "more chunks as they become free. The list is refreshed from Slurm."
            ),
        )
        expected_config_dir = (
            REPO_PATH
            / "python_code"
            / "roboai_xrf_frontend"
            / "runs"
            / str(run["run_name"]).strip()
        ).resolve()
        calibration = _calibrated_throughput(
            str(expected_config_dir),
            int(run["number_of_thread"]),
        )
        estimated_throughput = (
            calibration.events_per_second
            if calibration is not None
            else estimate_node_throughput(int(run["number_of_thread"]))
        )
        benchmark_recommendation = (
            _queue_benchmark(
                str(expected_config_dir),
                int(run["number_of_thread"]),
                tuple(selected_nodes),
                int(run["beam_on"]),
            )
            if selected_nodes
            else None
        )
        recommended_chunks = (
            benchmark_recommendation.chunks_per_node
            if benchmark_recommendation is not None
            else (
                recommend_chunks_per_node(
                    int(run["beam_on"]),
                    len(selected_nodes),
                    int(run["number_of_thread"]),
                    events_per_second=estimated_throughput,
                )
                if selected_nodes
                else 1
            )
        )
        queue_sizing_mode = st.radio(
            "Queue sizing",
            options=("Automatic (recommended)", "Manual"),
            horizontal=True,
            key="cluster_queue_sizing_mode",
        )
        if queue_sizing_mode == "Automatic (recommended)":
            chunks_per_node = recommended_chunks
            if benchmark_recommendation is not None:
                st.caption(
                    f"Automatic recommendation: {recommended_chunks} chunk(s) per "
                    f"selected node. This measured queue winner reached "
                    f"{benchmark_recommendation.end_to_end_events_per_second:,.0f} "
                    f"events/s end to end using the same configuration and "
                    f"node selection."
                )
            else:
                st.caption(
                    f"Automatic recommendation: {chunks_per_node} chunk(s) per "
                    f"selected node. It targets about "
                    f"{RECOMMENDED_CHUNK_SECONDS:g} seconds per chunk and will "
                    f"not automatically exceed "
                    f"{MAX_AUTOMATIC_CHUNKS_PER_NODE}."
                )
            if benchmark_recommendation is None and calibration is not None:
                st.caption(
                    f"Learned rate: {calibration.events_per_second:,.0f} "
                    f"events/s per node from {calibration.run_count} matching "
                    f"completed run(s), {calibration.chunk_count} chunks."
                )
            elif benchmark_recommendation is None:
                st.caption(
                    "No matching long-run timing is available yet; using a "
                    "conservative production-rate fallback. Manual remains "
                    "available for a measured override."
                )
        else:
            chunks_per_node = int(
                st.number_input(
                    "Chunks per selected node",
                    min_value=1,
                    max_value=MAX_CHUNKS_PER_NODE,
                    value=recommended_chunks,
                    step=1,
                    key="manual_cluster_chunks_per_node",
                    help=(
                        "This value is per selected node, not the total. More "
                        "chunks improve load balancing, but short chunks repeat "
                        "Slurm and Geant4 startup overhead."
                    ),
                )
            )
    st.caption(
        f"Node inventory: {inventory_source}. "
        f"Workers per selected node: {int(run['number_of_thread'])}."
    )
    if selected_nodes:
        saved_events = int(run["beam_on"])
        chunk_count, max_concurrent = calculate_queue_shape(
            saved_events,
            len(selected_nodes),
            chunks_per_node,
        )
        smallest_chunk, extra = divmod(saved_events, chunk_count)
        largest_chunk = smallest_chunk + (1 if extra else 0)
        chunk_size = (
            f"{smallest_chunk:,}"
            if smallest_chunk == largest_chunk
            else f"{smallest_chunk:,}-{largest_chunk:,}"
        )
        estimated_seconds = largest_chunk / estimated_throughput
        st.caption(
            f"Queue for the saved {saved_events:,}-event total: {chunk_count} "
            f"chunks of about {chunk_size} events; up to {max_concurrent} run "
            f"at once; estimated {estimated_seconds:.1f} seconds per chunk."
        )
        st.caption(
            "After changing Beam-on events or Workers per node, click "
            "Save run settings to update this recommendation. Actual duration "
            "depends on the geometry and physics configuration."
        )
        if estimated_seconds < 10:
            st.warning(
                "Estimated chunks are under 10 seconds, so startup overhead may "
                "be significant. For maximum efficiency, consider selecting "
                "fewer nodes for this event total."
            )
    if inventory_warning:
        st.caption(inventory_warning)

    st.divider()

    build_disabled = st.session_state.get(
        "geant4_running",
        False,
    )

    vis_disabled = (
        not _simulation_is_current()
        or st.session_state.get(
            "geant4_running",
            False,
        )
    )

    run_disabled = (
        not _simulation_is_current()
        or st.session_state.get(
            "geant4_running",
            False,
        )
    )

    c1, c2, c3 = st.columns(3)

    build_clicked = c1.button(
        "1. Build & compile",
        type="primary",
        disabled=build_disabled,
        width="stretch",
    )

    vis_clicked = c2.button(
        "2. Open visualization",
        disabled=vis_disabled,
        width="stretch",
    )

    run_clicked = c3.button(
        "3. Run Geant4",
        disabled=run_disabled,
        width="stretch",
    )
    if st.session_state.pop(
        "build_success",
        False,
    ):
        st.success(
            "Configuration compiled.",
            width="stretch",
        )

    if build_clicked:

        try:

            with st.spinner(
                "Building package objects and compiling configuration..."
            ):

                simulation = build_simulation(cfg)

                simulation.compile()

                print("config path",simulation.config_path)

            st.session_state.simulation = simulation

            st.session_state.compiled_fingerprint = (
                configuration_fingerprint(cfg)
            )

            st.session_state.run_complete = False
            st.session_state.noise_result = None
            st.session_state.last_root_file = None

            st.session_state.geant4_process = None
            st.session_state.geant4_running = False
            st.session_state.geant4_stopped = False
            st.session_state.geant4_beam_on = None

            # Clear old visualization
            st.session_state.viewer_url = None

            st.session_state.build_success = True

            st.rerun()
        except Exception as exc:
            st.exception(exc)

    if vis_clicked:

        try:

            sim = st.session_state.simulation

            with st.spinner(
                "Running visualization histories..."
            ):

                viewer_url = sim.show_vis(
                    beam_on=int(
                        run["vis_beam_on"]
                    ),
                    number_of_thread=int(
                        run["vis_threads"]
                    ),
                )

            # Visualization is NOT a valid quantitative run.
            st.session_state.run_complete = False
            st.session_state.noise_result = None
            st.session_state.last_root_file = None

            if viewer_url:

                st.session_state.viewer_url = viewer_url

                components.html(
                    f"""
                    <script>
                        window.parent.open(
                            "{viewer_url}",
                            "_blank"
                        );
                    </script>
                    """,
                    height=0,
                )

                st.success(
                    "Visualization generated.",
                    width="stretch",
                )

            else:

                st.session_state.viewer_url = None

                st.warning(
                    "Visualization completed, "
                    "but no viewer URL was returned.",
                    width="stretch",
                )

        except Exception as exc:
            st.exception(exc)
    # PERSISTENT VISUALIZATION LINK

    viewer_url = st.session_state.get(
        "viewer_url"
    )

    if (
        viewer_url
        and not st.session_state.get(
            "geant4_running",
            False,
        )
    ):

        st.link_button(
            "Open 3D viewer",
            viewer_url,
            width="stretch",
        )

    # 3. RUN GEANT4

    if run_clicked:

        try:

            sim = st.session_state.simulation

            selected_beam_on = int(
                run["beam_on"]
            )

            if not selected_nodes:
                raise ValueError("Select at least one compute node.")

            process = sim.start_run(
                beam_on=selected_beam_on,
                number_of_thread=int(
                    run["number_of_thread"]
                ),
                cluster_nodes=selected_nodes,
                cluster_chunks_per_node=chunks_per_node,
            )
            st.session_state.geant4_started_at = time.time()

            st.session_state.geant4_process = (
                process
            )

            # Store the exact beam count belonging
            # to this process.
            st.session_state.geant4_beam_on = (
                selected_beam_on
            )

            st.session_state.geant4_running = True
            st.session_state.geant4_stopped = False

            st.session_state.run_complete = False
            st.session_state.noise_result = None
            st.session_state.last_root_file = None

            st.rerun()

        except Exception as exc:

            st.session_state.geant4_running = False

            st.exception(exc)

    # LIVE / FINISHED SIMULATION STATUS
    # FULL WIDTH because this is NOT inside c3

    if st.session_state.get(
        "geant4_process"
    ) is not None:

        _running_simulation_panel()

    elif st.session_state.get(
        "run_complete",
        False,
    ):

        root_file = st.session_state.get(
            "last_root_file"
        )

        if root_file:

            st.success(
                f"Simulation complete: {root_file}",
                width="stretch",
            )

        else:

            st.success(
                "Simulation complete.",
                width="stretch",
            )

    elif st.session_state.get(
        "geant4_stopped",
        False,
    ):

        st.warning(
            "The previous Geant4 simulation was stopped.",
            width="stretch",
        )

    st.divider()

    # GENERATED CONFIGURATION

    st.subheader(
        "Generated configuration"
    )

    if _simulation_is_current():

        config_path = (
            Path(
                st.session_state
                .simulation
                .config_path
            )
            / "config.json"
        )

        if config_path.is_file():

            text = config_path.read_text(
                encoding="utf-8"
            )

            left, right = st.columns(
                [3, 1]
            )

            with left:

                with st.expander(
                    "Preview config.json",
                    expanded=False,
                ):
                    st.json(
                        json.loads(text)
                    )

            with right:

                st.download_button(
                    "Download config.json",
                    data=text,
                    file_name="config.json",
                    mime="application/json",
                    width="stretch",
                )

        else:

            st.caption(
                "Compile the configuration "
                "to generate config.json."
            )

    else:

        st.caption(
            "No current compiled configuration."
        )

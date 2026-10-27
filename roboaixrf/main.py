
from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Sequence
import numpy as np
import plotly.graph_objects as go
import subprocess
import socket
import time
from pydantic import BaseModel, PrivateAttr
import os
import signal
import threading
from .config.xrfconfig import XRFConfigure
from .config.write_json import write_json
from .detector_noise.xray_tube import get_flu
from .detector_noise.pipeline import apply_detectornoise
from plotly.subplots import make_subplots
from tqdm import tqdm
import re
import sys
class RoboAiXrfSimulation(BaseModel):
    """
    RoboAI XRF simulation controller.

    Workflow
    --------
    1. compile()
       - Build the SpekPy source spectrum at 1 mAs.
       - Write the Geant4 JSON configuration.

    2. run()
       - Run Geant4 with the source spectrum.
       - Estimate detector yield per simulated primary photon.

    3. detector_noise()
       - User chooses current and live time.
       - Re-run SpekPy fluence calculation using:
             mAs = current_mA * live_time_s
       - Convert fluence to physical photons through the tube collimator.
       - Scale the Geant4 response.
       - Apply arrival-time sampling, pile-up, and detector broadening.
    """

    config: XRFConfigure
    config_path: Path

    # Internal package path; this is not part of the user's simulation config.
    ROOTPATH: ClassVar[Path] = Path(__file__).resolve().parent
    _total_run:int=PrivateAttr(default=0)
    _beam_on: int = PrivateAttr(default=0)
    _is_compiled: bool = PrivateAttr(default=False)

    _energy_bin: np.ndarray | None = PrivateAttr(default=None)
    _fluence_list: np.ndarray | None = PrivateAttr(default=None)

    _last_mas: float | None = PrivateAttr(default=None)
    _last_incident_photons: float | None = PrivateAttr(default=None)
    _run_done:threading.Event=PrivateAttr(    default_factory=threading.Event)
    _tqdm_output:str|None=PrivateAttr(default="")




    @property
    def is_compiled(self) -> bool:
        return self._is_compiled

    @property
    def beam_on(self) -> int:
        return self._beam_on

    @property
    def energy_bin(self) -> np.ndarray | None:
        return self._energy_bin

    @property
    def fluence_list(self) -> np.ndarray | None:
        return self._fluence_list

    @property
    def last_mas(self) -> float | None:
        return self._last_mas

    @property
    def last_incident_photons(self) -> float | None:
        return self._last_incident_photons

    @property
    def get_latest_tqdm_output(self)->str|None:
        return self._tqdm_output


    def model_post_init(self, __context) -> None:
        self.config.validate_complete()

        self.config_path = self.config_path.resolve()
        self.config_path.mkdir(
            parents=True,
            exist_ok=True,
        )


    def _get_tube_inputs(self):
        """
        Collect the tube values .

        Returns
        -------
        tube
            X-ray tube configuration.
        filters
            SpekPy filter dictionaries.
        source_to_collimator_mm
            Focal-spot-to-virtual-collimator distance in mm.
        """
        tube = self.config.xray_tube
        if tube is None:
                raise ValueError("X-ray tube configuration is missing.")

        source_to_sample_mm = tube.placement.position.distance_mm

        window_to_sample_mm = tube.tube_window_to_sample_distance_mm

        window_to_collimator_mm = tube.tube_window_to_virtual_collimator_distance_mm

        tube_internal_length_mm = source_to_sample_mm- window_to_sample_mm
        source_to_collimator_mm = tube_internal_length_mm+ window_to_collimator_mm

        if tube_internal_length_mm < 0:
                raise ValueError(
                    "Focal-spot-to-sample distance cannot be smaller "
                    "than tube-window-to-sample distance."
                )


        if source_to_collimator_mm <= 0:
                raise ValueError(
                    "Focal-spot-to-collimator distance must be greater than 0."
                )

        if tube.is_spekpy_tube:


            filters = [
                f.model_dump()
                for f in tube.tube_filter_spekpy
            ]

            return tube.is_spekpy_tube, (tube, filters, source_to_collimator_mm)
        else:
            spectrum_file_path=tube.spectrum_file_path

            if spectrum_file_path is None:
                raise ValueError("Spectrum file path should not be none")

            return tube.is_spekpy_tube,(tube,spectrum_file_path)


    def write_spectrum_plot( self,energy_mev: np.ndarray, count: np.ndarray) -> Path:
        """
        Write the source spectrum used by Geant4 for the browser visualizer.

        Parameters
        ----------
        energy_mev
            Source energy bins in MeV.
        count
            Relative SpekPy fluence weights.
        """
        fig = go.Figure()

        fig.add_trace(
            go.Scatter(
                x=energy_mev * 1000.0,
                y=count,
                mode="lines",
                name="X-ray tube spectrum",
            )
        )

        fig.update_layout(
            title="X-ray Tube Spectrum",
            xaxis_title="Energy (keV)",
            yaxis_title="Relative fluence",
            template="plotly_white",
        )

        output_file = (
            self.ROOTPATH
            / "vis"
            / "public"
            / "runs"
            / "spectrum.json"
        )

        output_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        output_file.write_text(
            fig.to_json(),
            encoding="utf-8",
        )

        return output_file

    def write_macro(self,beam_on: int,number_of_thread: int,print_display: int,) -> tuple[str, Path]:
        if not self.is_compiled:
            raise RuntimeError("Call simulation.compile() first.")

        if self._energy_bin is None or self._fluence_list is None:
            raise RuntimeError(
                "Source spectrum is missing. Call simulation.compile() first."
            )

        if beam_on <= 0:
            raise ValueError("beam_on must be greater than 0.")

        if number_of_thread <= 0:
            raise ValueError(
                "number_of_thread must be greater than 0."
            )

        if print_display <= 0:
            raise ValueError(
                "print_display must be greater than 0."
            )

        macro_lines = [
            f"/run/numberOfThreads {number_of_thread}",
            "/run/initialize",
            "/gps/particle gamma",
            "/gps/ene/type User",
            "/gps/hist/type energy",
        ]

        # GPS uses these values as relative histogram weights.
        for energy, fluence in zip(self._energy_bin,self._fluence_list):
            macro_lines.append(
                f"/gps/hist/point {energy} {fluence}"
            )

        macro_lines.extend(
            [
                f"/run/printProgress {print_display}",
                f"/run/beamOn {beam_on}",
            ]
        )

        macro_text = "\n".join(macro_lines) + "\n"

        macro_file_path = (
            self.config_path / "run.mac"
        )

        macro_file_path.write_text(
            macro_text,
            encoding="utf-8",
        )
        return macro_text, macro_file_path

    def compile(self) -> None:
        """
        Prepare the source spectrum and Geant4 JSON configuration.

        The source spectrum is generated at 1 mAs because mAs changes
        intensity, not the normalized source-energy shape.

        Actual current/time scaling is done later in detector_noise()
        by calling SpekPy again with the user's requested mAs.
        """
        is_spekpy_tube,other = self._get_tube_inputs()
        if is_spekpy_tube:
            tube,filters,source_to_collimator_mm=other

            energy_bin_kev,fluence_list, _= get_flu(
                            mas=1.0,
                            voltage=tube.voltage_kv,
                            anode_degree=tube.anode_angle_deg,
                            anode_target_material=tube.anode_symbol,
                            filters=filters,
                            source_to_tube_collimator_mm=source_to_collimator_mm,
                            tube_type=tube.tube_type,
                            target_thickness_um=tube.target_thickness_um,
                        )
        else:
            tube,spectrum_file_path=other
            energy_bin_kev,fluence_list=tube._read(spectrum_file_path)






        self._energy_bin = np.asarray(
            energy_bin_kev,
            dtype=np.float64,
        ) / 1000.0

        self._fluence_list = np.asarray(
            fluence_list,
            dtype=np.float64,
        )


        if self._energy_bin.size == 0:
            raise ValueError(" Returned no energy bins.")

        if self._fluence_list.size != self._energy_bin.size:
            raise ValueError(
                " Energy and fluence arrays have different lengths."
            )

        if not np.all(np.isfinite(self._fluence_list)):
            raise ValueError(
                " Source spectrum contains non-finite values."
            )

        if np.sum(self._fluence_list) <= 0:
            raise ValueError(
                " Source spectrum has zero total fluence."
            )

        write_json(
            self.config,
            self.config_path / "config.json",
        )

        self._beam_on = 0
        self._last_mas = None
        self._last_incident_photons = None
        self._is_compiled = True
    def port_is_open(self,port_number: int) -> bool:
        with socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
        ) as sock:

            sock.settimeout(0.2)

            return (
                sock.connect_ex(
                    ("127.0.0.1", port_number)
                )
                == 0
            )
    def show_vis(self,beam_on: int = 100,number_of_thread: int = 1,port:int=5173) -> None:
        """
        Run a small visualization simulation and launch the Vite viewer.

        This is NOT registered as the quantitative Geant4 response run.
        Call run() afterwards before detector_noise().
        """
        if not self.is_compiled:
            raise RuntimeError(
                "Call simulation.compile() first."
            )

        _, macro_file_path = self.write_macro(
            beam_on=beam_on,
            number_of_thread=number_of_thread,
            print_display=max(1, beam_on),
        )

        self.write_spectrum_plot(
            self._energy_bin,
            self._fluence_list,
        )
        self._beam_on = 0

        process=subprocess.run(
            [
                "./sim",
                str(self.config_path / "config.json"),
                str(macro_file_path),
            ],
            cwd=(
                self.ROOTPATH.parent
                / "geant4_code"
                / "build"
            ),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,

        )
        with open(self.ROOTPATH/"vis"/"public"/"geant4_debug.log","w") as f:

            f.write(process.stdout)
        viewer_url = f"http://localhost:{port}"

        if not self.port_is_open(port):
            subprocess.Popen(
                [
                    "npx",
                    "vite",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    str(port),
                    "--strictPort"
                ],
                cwd=self.ROOTPATH / "vis",
                start_new_session=True
            )
            start_time=time.time()
            while not self.port_is_open(port):
                if time.time()-start_time>10:
                    raise RuntimeError(
                                        f"Vite viewer failed to start on port {port}."
                                    )
                time.sleep(0.1)

        print(f"Visualization ready at : {viewer_url}")
        return viewer_url
    def start_run(self,beam_on: int,number_of_thread: int,cluster_nodes: int | str | Sequence[str] = 1,cluster_chunks_per_node: int = 1,) -> subprocess.Popen:

        """
        Run the quantitative Geant4 response simulation.

        Returns
        -------
        subprocess.Popen
        """
        self._run_done.clear()
        self._total_run=beam_on
        if not self.is_compiled:
            raise RuntimeError(
                "Call simulation.compile() first."
            )
        workers = int(number_of_thread)
        if not 1 <= workers <= 80:
            raise ValueError("number_of_thread must be between 1 and 80 on the cluster.")
        chunks_per_node = int(cluster_chunks_per_node)
        if chunks_per_node < 1:
            raise ValueError("cluster_chunks_per_node must be at least 1.")



        print_display=max(1,beam_on//1000)



        _, macro_file_path = self.write_macro(
            beam_on=beam_on,
            number_of_thread=number_of_thread,
            print_display=print_display,
        )

        config_file = (
            self.config_path / "config.json"
        )


        root_file = (
            self.config_path / "simulation.root"
        )


        self._beam_on = 0
        try:
            env_path = os.environ["TEMPO_XRF_REPO"]
        except KeyError:
            raise KeyError(
                "TEMPO_XRF_REPO is not set. Set it to your repository path."
            ) from None

        repo = Path(env_path).expanduser().resolve()
        if repo is None:
            raise FileNotFoundError(
                "Could not locate the TEMPO XRF repository. Set TEMPO_XRF_REPO "
                "to the checkout containing cluster_jobs and geant4_code/build/sim."
            )

        build_dir = repo / "geant4_code" / "build"

        launch_command = [
            sys.executable,
            str(repo / "cluster_jobs" / "ui_cluster_run.py"),
            str(self.config_path.resolve()),
            str(int(beam_on)),
            str(workers),
            "--chunks-per-node",
            str(chunks_per_node),
        ]
        if isinstance(cluster_nodes, int):
            if cluster_nodes < 1:
                raise ValueError("Select at least one node.")

            launch_command += ["--node-count", str(cluster_nodes)]

        else:
            if isinstance(cluster_nodes, str):
                nodes = cluster_nodes
            else:
                nodes = ",".join(str(node) for node in cluster_nodes)

            if not nodes.strip():
                raise ValueError("Select at least one node.")

            launch_command += ["--nodes", nodes]

        if root_file.exists():
            root_file.unlink()
        bar=tqdm(
            total=beam_on,
            desc="RoboAI XRF Simulation",
            unit="Event",
            colour="green",
        )
        process=subprocess.Popen(
            launch_command,
            cwd=build_dir,
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,

            text=True

        )


        def _watch_background_run()->None:
            last_number=0

            if process.stdout is not None:
                for line in process.stdout:
                    tqdm.write(line.rstrip())

                    match=re.search(r"Event\s+(\d+)",line)
                    if match:
                        event_number=int(match.group(1))
                        if event_number>last_number:
                            bar.update(
                                event_number-last_number
                            )
                            last_number=event_number
                            self._tqdm_output=last_number

            return_code=process.wait()

            if return_code==0 and root_file.is_file():
                self._beam_on=int(beam_on)
            else:
                self._beam_on=0
            self._run_done.set()

        threading.Thread(target=_watch_background_run,daemon=True).start()
        # _watch_background_run()
        return process

    def run(self, beam_on: int, number_of_thread: int):
        process = None

        try:
            process = self.start_run(
                beam_on=beam_on,
                number_of_thread=number_of_thread,
            )

            self._run_done.wait()

            if process.returncode != 0:
                raise RuntimeError(
                    f"Simulation launcher failed with exit code "
                    f"{process.returncode}. Check the output above."
                )

            root_file = self.config_path.resolve() / "simulation.root"

            if not root_file.is_file():
                raise RuntimeError(
                    f"Simulation output was not created: {root_file}"
                )

            if self._beam_on < 2:
                raise RuntimeError(
                    "Simulation did not register a valid quantitative run."
                )

        except KeyboardInterrupt:
            self.stop_run(process=process)
            raise
    def stop_run(self,process:subprocess.Popen|None)->None:

        """
        Stop a background Geant4 simulation.

        SIGTERM is sent to the entire process group first. If the
        process does not exit within five seconds, SIGKILL is used.

        Any partial simulation.root is deleted because it is not a
        valid quantitative response.
        """

        self._beam_on=0
        if process is not None and process.poll() is None:
            try:
                os.killpg(
                    os.getpgid(process.pid),
                    signal.SIGTERM
                )
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(
                        os.getpgid(process.pid),
                        signal.SIGKILL
                    )
                    process.wait()
            except ProcessLookupError:
                pass

            except Exception:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except Exception:
                    try:
                        process.kill()
                        process.wait()

                    except Exception:
                        pass
        root_file=self.config_path/"simulation.root"
        if root_file.exists():
            try:
                root_file.unlink()
            except OSError:
                pass



    def detector_noise(self,*,fwhm: float,fwhm_energy_kev: float,detector_zero_offset: float,detector_gain_kev: float,
                        live_time: float, pile_up_window_us: float,current: float | None = None,fano_factor: float = 0.115,
                        pair_creation_energy_ev: float = 3.6,mca_channels: int = 2048,chunk_size: int = 3_000_000,number_of_buckets: int = 65,simulation_time_s:int=3600,
                        seed:int|None=None):
        """
        Create the physical detector spectrum for a requested acquisition.

        Important
        ---------
        Current and live time do NOT change the stored Geant4 source shape.
        Instead:

            mAs = current[mA] * simulation_time_s[s]

        SpekPy is called again with that mAs to obtain the physical fluence.
        That fluence is multiplied by the tube-collimator area to obtain
        the physical number of incident photons.

        The current defaults to config.xray_tube.current_ma when current=None.
        If the spectrum is from the reading custom file current will be neglected

        seed: optional nonnegative integer. When set, the pile-up and
        broadening random draws are deterministic (same seed => same spectrum);
        default None keeps the historical non-reproducible behaviour.
        """
        if not self.is_compiled:
            raise RuntimeError(
                "Call simulation.compile() first."
            )

        if self._beam_on < 2:
            raise RuntimeError(
                "No quantitative Geant4 response run is registered. "
                "Call simulation.run() with beam_on >= 2 first."
            )

        if not np.isfinite(simulation_time_s) or simulation_time_s <= 0:            raise RuntimeError(
                "simulation time s cannot be negative or inf value must be finite positive value"
            )
        root_file = self.config_path / "simulation.root"


        if not root_file.is_file():
            raise FileNotFoundError(
                f"simulation.root not found: {root_file}"
            )

        if  not np.isfinite(live_time) or live_time <= 0:
            raise ValueError(
                            "live_time must be greater than 0 seconds. or must be finite positive number"
                        )

        if detector_gain_kev <= 0:
            raise ValueError(
                "detector_gain_kev must be greater than 0."
            )

        if mca_channels <= 0:
            raise ValueError(
                "mca_channels must be greater than 0."
            )

        if seed is not None and (
            isinstance(seed, bool)
            or not isinstance(seed, (int, np.integer))
            or seed < 0
        ):
            raise ValueError(
                "seed must be a nonnegative integer or None."
            )

        is_spekpy_tube, other= (
            self._get_tube_inputs()
        )
        if is_spekpy_tube:
            tube,filters, source_to_collimator_mm=other
            if current is None:
                current = float(tube.current_ma)

            if current <= 0:
                raise ValueError(
                    "current must be greater than 0 mA."
                )

            # mA * s = mAs
            simulation_time_s=min(live_time,simulation_time_s)

            mas = current *simulation_time_s

            _, _, fluence_photons_cm2 = get_flu(
                mas=mas,
                voltage=tube.voltage_kv,
                anode_degree=tube.anode_angle_deg,
                anode_target_material=tube.anode_symbol,
                filters=filters,
                source_to_tube_collimator_mm=source_to_collimator_mm,
                tube_type=tube.tube_type,
                target_thickness_um=tube.target_thickness_um,
            )

            # Tube-collimator radius is configured in mm.
            # SpekPy fluence is photons/cm², so convert radius to cm.
            radius_cm = (
                tube.tube_collimator_radius_mm / 10.0
            )

            area_cm2 = (
                np.pi * radius_cm**2
            )

            # photons/cm² * cm² = photons
            number_of_photon = (
                float(fluence_photons_cm2)
                * area_cm2
            )
            self._last_mas = current*live_time



        else:
            tube,spectrum_file_path=other
            number_of_photon=np.trapezoid(
                self._fluence_list,
                self._energy_bin*1000
            )*simulation_time_s

        self._last_incident_photons = number_of_photon * live_time / simulation_time_s

        (
            final_count,
            final_energy_centers,
            scaled_count,
            average_channel_wise_yield,
            se_channel_wise_yield,
            spectrum_yield_avg,
            spectrum_se,
        ) = apply_detectornoise(
            root_path=root_file,

            beam_on=self._beam_on,

            number_of_photon=number_of_photon,

            live_time=simulation_time_s,

            fwhm=fwhm,
            fwhm_energy=fwhm_energy_kev,

            detector_zero_offset=detector_zero_offset,

            detector_gain=detector_gain_kev,

            pile_up_window=pile_up_window_us,

            fano_factor=fano_factor,
            pair_creation_energy_ev=(
                pair_creation_energy_ev
            ),

            mca_channels=mca_channels,
            chunk_size=chunk_size,
            number_of_buckets=number_of_buckets,
            seed=seed,
        )

        scaling_factor=live_time/simulation_time_s
        final_count=final_count.astype(np.float64)
        scaled_count=scaled_count.astype(np.float64)
        final_count*=scaling_factor
        scaled_count*=scaling_factor

        fig = make_subplots(
            rows=1,
            cols=2,
            subplot_titles=(
                "Before Detector Noise",
                "After Detector Noise",
            ),
        )

        fig.add_trace(
            go.Scatter(
                x=final_energy_centers,
                y=scaled_count,
                mode="lines",
                name="Before detector noise",
            ),
            row=1,
            col=1,
        )

        fig.add_trace(
            go.Scatter(
                x=final_energy_centers,
                y=final_count,
                mode="lines",
                name="After detector noise",
            ),
            row=1,
            col=2,
        )

        fig.update_xaxes(
            title_text="Energy (keV)",
            row=1,
            col=1,
        )

        fig.update_xaxes(
            title_text="Energy (keV)",
            row=1,
            col=2,
        )

        fig.update_yaxes(
            title_text="Counts",
            row=1,
            col=1,
        )

        fig.update_yaxes(
            title_text="Counts",
            row=1,
            col=2,
        )

        fig.update_layout(
            title=(
                "XRF Simulated Spectrum "
                + (f"({current:g} mA, {live_time:g} s)" if is_spekpy_tube
                 else f"(custom source, {live_time:g} s)")
            ),
            template="plotly_white",
            height=600,
            width=1500,
        )

        plot_path = (
            self.config_path
            / "simulation_spectrum.html"
        )

        fig.write_html(
            plot_path,
            include_plotlyjs=True,
        )

        print(
            f"Spectrum saved to: {plot_path}"
        )

        return (
            final_count,
            final_energy_centers,
            scaled_count,
            average_channel_wise_yield,
            se_channel_wise_yield,
            spectrum_yield_avg,
            spectrum_se,
        )

if __name__=="__main__":
    pass

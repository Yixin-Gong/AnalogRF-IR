from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.environment import load_environment, resolve_project_path
from core.compensation import has_miller_capacitive_compensation, has_miller_rc_compensation
from core.validator import Validator
import core.design_rules  # noqa: F401
from diagnostics import build_causal_diagnostics, build_spice_intervention_model
from flow.state_update import apply_optimizer_meta_to_state
from frontends.design_input import StateBuilder, load_design_input
from netlist.generator import generate_netlist
from optimizer.problem import OptimizationProblem
from optimizer.registry import OptimizerConfig, OptimizerRegistry
from outputs.artifacts import ArtifactWriter, RunArtifacts, next_iteration
from postprocess.common import backfill_state_from_ngspice, normalize_phase_margin
from postprocess.registry import PostprocessConfig, PostprocessContext, PostprocessRegistry
from pygmid.plugin import GmIdPlugin
from schemas.design_state import DesignState, Target
from simulator.ngspice import NgspiceSimulator, SimulationResult
from specs.models import SpecRegistry


Emit = Callable[[str], None]


@dataclass
class FlowConfig:
    env: str | Path = "environment.yaml"
    schema: str | Path = "inputs/ota/five_transistor/five_transistor_ota.yaml"
    spice: str | Path | None = None
    spice_yaml_out: str | Path | None = None
    topology: str = "auto"
    optimizer: str = "nsga2"
    pop_size: int = 100
    generations: int = 50
    seed: int | None = None
    skip_dc_repair: bool = False
    skip_comp_tune: bool = False
    ngspice_bin: str | None = None
    tail_current_mirror: bool = False
    run_asir: bool = True
    runs_dir: str | Path = "runs"
    enable_intervention_model: bool = False
    intervention_max_actions: int = 4
    intervention_perturbation_fraction: float = 0.10


@dataclass
class FlowResult:
    state: DesignState
    artifacts: RunArtifacts
    best_meta: dict[str, Any]
    sim_result: SimulationResult
    validation_reports: list[dict[str, Any]]
    flow_meta: dict[str, Any]


class AnalogRFIRFlowRunner:
    """End-to-end orchestration with explicit module boundaries."""

    def __init__(
        self,
        *,
        config: FlowConfig,
        legacy_state_builder: StateBuilder | None = None,
        emit: Emit | None = None,
        artifact_writer: ArtifactWriter | None = None,
        spec_registry: SpecRegistry | None = None,
    ) -> None:
        self.config = config
        self.legacy_state_builder = legacy_state_builder
        self.emit = emit or (lambda _msg: None)
        self.spec_registry = spec_registry or SpecRegistry()
        self.artifact_writer = artifact_writer or ArtifactWriter(config.runs_dir, self.spec_registry)
        self.validation_reports: list[dict[str, Any]] = []

    def run(self) -> FlowResult:
        cfg = self.config
        env_path = resolve_project_path(cfg.env)
        schema_path = resolve_project_path(cfg.schema)
        self._header(env_path, schema_path)

        self.emit("\n[0/8] Loading environment.yaml ...")
        env = load_environment(env_path)
        self.emit(f"       Process:  {env.get('process', {}).get('process_name', 'unknown')}")
        self.emit(f"       Simulator: {env.get('simulation', {}).get('simulator', 'unknown')}")
        self.emit(f"       Vdd:       {env.get('simulation', {}).get('supply', {}).get('vdd', 'unknown')}V")

        self.emit("\n[1/8] Loading input and building DesignState ...")
        design_input = load_design_input(
            env=env,
            schema_path=schema_path,
            topology=cfg.topology,
            spice_path=cfg.spice,
            spice_yaml_out=cfg.spice_yaml_out,
            legacy_builder=self.legacy_state_builder,
            run_asir=cfg.run_asir,
        )
        state = design_input.state
        problem = OptimizationProblem.from_state(state)
        spec_model = self.spec_registry.select(state)
        self._print_state_summary(problem, spec_model.name)

        self.emit("\n[2/8] Validating Schema ...")
        self._validate_or_raise(state, "input")

        self.emit("\n[3/8] Initializing gm/ID plugin ...")
        gmid_plugin = GmIdPlugin.from_environment(env)
        pygmid = gmid_plugin.load(env)
        self.emit(pygmid.summary())

        self.emit("\n[4/8] Creating optimizer/evaluator ...")
        opt_config = OptimizerConfig(
            algorithm=cfg.optimizer,
            pop_size=cfg.pop_size,
            generations=cfg.generations,
            seed=cfg.seed,
            verbose=True,
        )
        optimizer, evaluator = OptimizerRegistry.create(opt_config, problem, pygmid)
        self.emit(f"       Optimizer: {cfg.optimizer}")
        self.emit(f"       Estimator: {problem.estimator_key}")
        self.emit(f"       Variables: {evaluator.n_vars}")
        self.emit(f"       Load cap:  {state.simulation.cload * 1e12:.1f}pF")

        self.emit("\n[5/8] Running optimizer ...")
        t0 = time.time()
        _best_x, best_meta = optimizer.optimize()
        opt_elapsed = time.time() - t0
        flow_options = {
            "skip_dc_repair": bool(cfg.skip_dc_repair),
            "skip_comp_tune": bool(cfg.skip_comp_tune),
            "tail_current_mirror": bool(problem.capabilities.has("tail_current_mirror") or cfg.tail_current_mirror),
            "stage2_current_mirror": bool(problem.capabilities.has("output_bias_mirror")),
            "input_kind": design_input.source_kind,
            "asir_enabled": bool(cfg.run_asir),
            "capabilities": list(problem.capabilities.names),
        }
        best_meta["flow_options"] = flow_options
        self._print_optimizer_summary(state, best_meta, opt_elapsed)

        self.emit("\n[6/8] Updating schema state from optimizer ...")
        apply_optimizer_meta_to_state(state, best_meta)
        if cfg.tail_current_mirror and not formal_tail_current_mirror:
            state.global_parameters["tail_current_mirror_bias"] = 1.0
        self._validate_or_raise(state, "post_optimizer")
        for did, ts in state.transistors.items():
            p = ts.parameters
            if p.W > 0 and p.L > 0:
                self.emit(f"       {did}: W={p.W * 1e6:.3f}um, L={p.L * 1e9:.0f}nm")
        for name, val in state.global_parameters.items():
            self.emit(f"       {name}: {val:.4e}")

        self.emit("\n[7/8] Generating SPICE netlist ...")
        iteration_id = next_iteration(self.artifact_writer.runs_dir)
        output_dir = self.artifact_writer.runs_dir / f"iter_{iteration_id:03d}"
        output_dir.mkdir(parents=True, exist_ok=True)
        netlist_str = generate_netlist(state)
        self.emit(f"       Netlist length: {len(netlist_str)} chars")

        self.emit("\n[8/8] Running ngspice simulation ...")
        ngspice_bin = cfg.ngspice_bin or (env.get("tools", {}) or {}).get("ngspice_bin", "ngspice")
        sim = NgspiceSimulator(timeout_sec=30, ngspice_bin=ngspice_bin)
        flow_meta = {
            "environment": str(env_path),
            "schema": str(design_input.schema_path),
            "source_kind": design_input.source_kind,
            "source_path": str(design_input.source_path) if design_input.source_path else "",
            "generated_yaml": str(design_input.generated_yaml_path) if design_input.generated_yaml_path else "",
            "asir": design_input.asir_summary,
            "problem": problem.to_flow_meta(),
            "options": flow_options,
            "validation": self.validation_reports,
            "postprocess": [],
        }
        if not sim.check_available():
            self.emit("       ngspice not available; writing structured outputs without simulation.")
            sim_result = SimulationResult(
                success=False,
                return_code=-1,
                raw_stderr=f"ngspice binary not available: {ngspice_bin}",
            )
            artifacts = self.artifact_writer.write(
                state=state,
                best_meta=best_meta,
                sim_result=sim_result,
                iteration=iteration_id,
                netlist_str=netlist_str,
                flow_meta=flow_meta,
            )
            return FlowResult(state, artifacts, best_meta, sim_result, self.validation_reports, flow_meta)

        post_context = PostprocessContext(
            state=state,
            sim=sim,
            work_dir=output_dir,
            config=PostprocessConfig(
                skip_dc_repair=cfg.skip_dc_repair,
                skip_comp_tune=cfg.skip_comp_tune,
            ),
            profile=problem.profile,
            capabilities=problem.capabilities,
        )
        post_events = PostprocessRegistry().run(post_context)
        flow_meta["postprocess"] = post_events
        for event in post_events:
            self._print_postprocess_event(event)
        if post_events:
            netlist_str = generate_netlist(state)
            best_meta.setdefault("decoded", {})["__global__"] = dict(state.global_parameters)
            if has_miller_capacitive_compensation(state):
                best_meta.setdefault("performance", {})["Cc"] = state.global_parameters.get("Cc", 0.0)
            if has_miller_rc_compensation(state):
                best_meta.setdefault("performance", {})["Rz"] = state.global_parameters.get("Rz", 0.0)
            self._validate_or_raise(state, "postprocess")

        sim_result = sim.run(netlist_str, work_dir=str(output_dir))
        if "phase_margin" in sim_result.measurements and "phase_margin_from_curve" not in sim_result.measurements:
            sim_result.measurements["phase_margin"] = normalize_phase_margin(sim_result.measurements["phase_margin"])
        backfill_state_from_ngspice(state, sim_result)
        self._validate_or_raise(state, "post_ngspice")
        self._print_simulation_summary(sim_result)
        self._maybe_build_local_intervention_model(
            state=state,
            best_meta=best_meta,
            sim_result=sim_result,
            sim=sim,
            output_dir=output_dir,
            spec_model=spec_model,
            flow_meta=flow_meta,
        )

        artifacts = self.artifact_writer.write(
            state=state,
            best_meta=best_meta,
            sim_result=sim_result,
            iteration=iteration_id,
            netlist_str=netlist_str,
            flow_meta=flow_meta,
        )
        self._print_artifacts(artifacts)
        self._print_comparison(state, best_meta, sim_result, spec_model)
        return FlowResult(state, artifacts, best_meta, sim_result, self.validation_reports, flow_meta)

    def _maybe_build_local_intervention_model(
        self,
        *,
        state: DesignState,
        best_meta: dict[str, Any],
        sim_result: SimulationResult,
        sim: NgspiceSimulator,
        output_dir: Path,
        spec_model,
        flow_meta: dict[str, Any],
    ) -> None:
        cfg = self.config
        if not cfg.enable_intervention_model:
            flow_meta["local_intervention_model"] = {
                "schema_version": "analogrf_ir.local_intervention_model.v0_1",
                "method": "disabled",
                "status": "disabled",
                "reason": "FlowConfig.enable_intervention_model is false.",
            }
            return
        if not sim_result.success:
            flow_meta["local_intervention_model"] = {
                "schema_version": "analogrf_ir.local_intervention_model.v0_1",
                "method": "spice_small_perturbation",
                "status": "skipped",
                "reason": "Base ngspice run did not produce usable measurements.",
            }
            return

        self.emit("\n[8b] Building local intervention model ...")
        perf_est = best_meta.get("performance", {}) or {}
        target_status = {
            name: spec_model.target_status(name, target, sim_result.measurements or {}, perf_est)
            for name, target in state.targets.items()
        }
        provisional = build_causal_diagnostics(
            state=state,
            best_meta=best_meta,
            sim_result=sim_result,
            target_status=target_status,
            spec_model=spec_model,
            flow_meta={**flow_meta, "local_intervention_model": None},
        )
        model = build_spice_intervention_model(
            state=state,
            sim=sim,
            work_dir=output_dir / "interventions",
            spec_model=spec_model,
            target_status=target_status,
            tuning=provisional.get("attribution_guided_tuning", {}),
            max_actions=max(0, int(cfg.intervention_max_actions)),
            perturbation_fraction=float(cfg.intervention_perturbation_fraction),
        )
        flow_meta["local_intervention_model"] = model
        effects = model.get("action_effects", []) or []
        ok_effects = [item for item in effects if item.get("status") == "ok"]
        self.emit(
            "       Local model: "
            f"{model.get('method')} status={model.get('status')} "
            f"actions={len(ok_effects)}/{len(effects)}"
        )
        for effect in ok_effects[:3]:
            self.emit(
                "       intervention: "
                f"{effect.get('knob')} reduction={effect.get('violation_reduction')} "
                f"{effect.get('interpretation', '')}"
            )

    def _validate_or_raise(self, state: DesignState, stage: str) -> None:
        report = Validator().validate(state)
        summary = {
            "stage": stage,
            "schema_valid": bool(report.schema_valid),
            "error_count": len(report.errors()),
            "warning_count": len(report.warnings()),
            "info_count": len(report.info()),
            "check_count": len(report.results),
        }
        self.validation_reports.append(summary)
        self.emit(report.summary(by_layer=True))
        if not report.schema_valid:
            messages = "; ".join(error.message for error in report.errors())
            raise RuntimeError(f"Schema validation failed at {stage}: {messages}")

    def _header(self, env_path: Path, schema_path: Path) -> None:
        self.emit("=" * 70)
        self.emit("  AnalogRF-IR v0.1 - modular schema-driven analog/RF flow")
        self.emit("  Schema is the single source of truth")
        self.emit(f"  env:    {env_path}")
        self.emit(f"  schema: {schema_path}")
        if self.config.spice:
            self.emit(f"  spice:  {resolve_project_path(self.config.spice)}")
        self.emit("=" * 70)

    def _print_state_summary(self, problem: OptimizationProblem, spec_model: str) -> None:
        state = problem.state
        self.emit(f"       Topology: {state.topology.name} ({state.topology.architecture})")
        self.emit(f"       Class/spec model: {state.topology.class_} / {spec_model}")
        self.emit(f"       IR profile: {problem.profile.name}")
        self.emit(f"       IR capabilities: {', '.join(problem.capabilities.names) or 'none'}")
        self.emit(f"       Process:  {state.process.process_name} ({state.process.technology_node}um)")
        self.emit(f"       Vdd:      {state.simulation.supply.get('vdd', 1.2)}V")
        self.emit(f"       Devices:  {len(state.topology.devices)}")
        self.emit(f"       Variables: {len(state.design_variables)}")
        for dv in state.design_variables:
            sym = f" [{dv.symmetry_label}]" if dv.symmetry_label else ""
            label = f"{dv.device}.{dv.variable}" if dv.device else dv.variable
            self.emit(f"         {label}: [{dv.range.min:.1e}, {dv.range.max:.1e}]{sym}")
        self.emit(f"       Loss terms: {len(state.loss_terms)}")
        self.emit(f"       Evaluations: {len(state.evaluations)}")

    def _print_optimizer_summary(self, state: DesignState, best_meta: dict[str, Any], elapsed: float) -> None:
        perf_est = best_meta.get("performance", {}) or {}
        self.emit(f"\n       Optimizer completed in {elapsed:.1f}s")
        self.emit(f"       Best loss: {best_meta.get('total_loss', 0):.6f}")
        self.emit("       Estimated performance:")
        for key, value in perf_est.items():
            unit = state.targets.get(key, Target()).unit
            self.emit(f"         {key:>22s}: {self._format_metric_value(value):>12s} {unit}")

    def _print_postprocess_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "initial_operating_point":
            regions = event.get("region_counts", {}) or {}
            region_text = ", ".join(f"{name}={count}" for name, count in sorted(regions.items()))
            self.emit(
                f"       Initial OP check: devices={event.get('operating_point_count', 0)}, "
                f"regions=({region_text or 'none'})"
            )
        elif etype in {"stage2_balance", "stage2_rebalance"}:
            label = "Stage-2 re-balance" if etype == "stage2_rebalance" else "Stage-2 DC balance"
            self.emit(
                f"       {label}: M6_W scale={event.get('scale', 1.0):.3f}, "
                f"M7_W scale={event.get('sink_scale', 1.0):.3f}, vout~{event.get('vout', 0.0):.3f}V"
            )
        elif etype == "tail_headroom":
            self.emit(
                f"       Tail headroom repair: M1/M2_W scale={event.get('scale', 1.0):.3f}, "
                f"M5 VDS-VDSAT~{event.get('margin', 0.0):.3f}V"
            )
        elif etype == "compensation_tune":
            meas = event.get("measurements", {}) or {}
            stop = (
                f", stop={event.get('early_stop_reason')}"
                if event.get("early_stop")
                else ""
            )
            self.emit(
                f"       Compensation tune: Cc={event.get('Cc', 0.0):.3e}F, "
                f"Rz={event.get('Rz', 0.0):.1f}ohm, "
                f"Rz_target={event.get('Rz_target_1_over_gm2', 0.0):.1f}ohm, "
                f"I_tail={event.get('I_tail', 0.0):.3e}A, "
                f"I_stage2={event.get('I_stage2', 0.0):.3e}A, "
                f"PM~{meas.get('phase_margin', 0.0):.1f}deg, "
                f"UGBW~{meas.get('unity_gain_bandwidth', 0.0):.3e}Hz, "
                f"evals={event.get('evaluated_candidates', 0)}"
                f"{stop}"
            )
        elif etype == "source_follower_op_tune":
            scale = float(event.get("width_scale", 1.0) or 1.0)
            scale_text = f", width_scale={scale:.2f}" if abs(scale - 1.0) > 1e-9 else ""
            self.emit(
                f"       Source-follower OP tune: "
                f"vbias_p={event.get('new_vbias_p', 0.0):.3f}V, "
                f"vbias_reg={event.get('new_vbias_reg', 0.0):.3f}V, "
                f"gain~{event.get('dc_gain_db', 0.0):.2f}dB, "
                f"UGBW~{event.get('unity_gain_bandwidth', 0.0):.3e}Hz, "
                f"PM~{event.get('phase_margin', 0.0):.1f}deg, "
                f"margin~{event.get('op_margin', 0.0):.3f}V, "
                f"req_margin~{event.get('op_required_margin', 0.0):.3f}V, "
                f"evals={event.get('candidate_count', 0)}"
                f"{scale_text}"
            )
        elif etype == "single_stage_ota_op_tune":
            meas = event.get("measurements", {}) or {}
            self.emit(
                f"       Single-stage OTA OP tune: "
                f"phase={event.get('selected_phase', '')}, "
                f"vbias={event.get('new_vbias', 0.0):.4f}V, "
                f"M1/M2_W/L scale={event.get('input_width_scale', 1.0):.2f}/"
                f"{event.get('input_length_scale', 1.0):.2f}, "
                f"M3/M4_L scale={event.get('load_length_scale', 1.0):.2f}, "
                f"M5_W/L scale={event.get('tail_width_scale', 1.0):.2f}/"
                f"{event.get('tail_length_scale', 1.0):.2f}, "
                f"gain~{meas.get('dc_gain_db', 0.0):.2f}dB, "
                f"UGBW~{meas.get('unity_gain_bandwidth', 0.0):.3e}Hz, "
                f"PM~{meas.get('phase_margin', 0.0):.1f}deg, "
                f"power~{meas.get('total_power', 0.0):.3e}W, "
                f"evals={event.get('candidate_count', 0)}"
            )

    def _print_simulation_summary(self, result: SimulationResult) -> None:
        if result.success:
            self.emit(f"       Simulation completed in {result.elapsed_sec:.1f}s")
            self.emit("       Measurements:")
            perf_keys = {
                "dc_gain_db",
                "unity_gain_bandwidth",
                "phase_margin",
                "slew_rate",
                "slew_rate_pos",
                "slew_rate_neg",
                "output_swing",
                "output_swing_low",
                "output_swing_high",
                "icmr",
                "icmr_min",
                "icmr_max",
                "delay",
                "regeneration_time",
                "reset_time",
                "energy",
                "pdp",
                "kickback_noise",
                "input_capacitance",
                "metastability_margin",
                "max_sample_rate",
                "total_power",
                "i_vdd",
            }
            for key, value in result.measurements.items():
                if key in perf_keys or key.startswith("m"):
                    self.emit(f"         {key:>20s}: {value:>12.4e}")
            if result.operating_points:
                self.emit(f"       Operating points: {len(result.operating_points)} devices")
        else:
            self.emit("       Simulation completed with issues")
            self.emit(f"       Return code: {result.return_code}")
            if result.raw_stderr:
                for line in result.raw_stderr.strip().splitlines()[-5:]:
                    self.emit(f"       {line}")

    def _print_artifacts(self, artifacts: RunArtifacts) -> None:
        self.emit(f"\n{'=' * 70}")
        self.emit(f"  Output -> {artifacts.output_dir}/")
        self.emit("     design_state.yaml    Updated schema + transistor state")
        self.emit("     netlist.cir          SPICE netlist emitted from schema")
        self.emit("     sim_log.json         Optimizer + ngspice structured log")
        self.emit("     agent_diagnostics.json Agent diagnostics")
        self.emit("     causal_diagnostics.json Causal graph and attribution view")
        self.emit("     result.json          Compact final JSON result")
        self.emit(f"{'=' * 70}")

    def _print_comparison(self, state: DesignState, best_meta: dict[str, Any], result: SimulationResult, spec_model) -> None:
        if not result.success:
            return
        self.emit("\n  Optimizer vs ngspice comparison:")
        self.emit(f"       {'':>22s} {'Optimizer':>12s} {'ngspice':>12s} {'delta':>10s}")
        perf_est = best_meta.get("performance", {}) or {}
        for key, est_val in perf_est.items():
            ng_key = spec_model.measurement_key(key)
            ng_val = result.measurements.get(ng_key)
            unit = state.targets.get(key, Target()).unit
            est_str = f"{self._format_metric_value(est_val)} {unit}"
            ng_str = (
                f"{self._format_metric_value(ng_val)} {unit}"
                if ng_val is not None
                else "N/A"
            )
            delta_str = f"{ng_val - est_val:+.2e}" if ng_val is not None else ""
            self.emit(f"       {key:>22s}: {est_str:>12s} {ng_str:>12s} {delta_str:>10s}")

    @staticmethod
    def _format_metric_value(value: float) -> str:
        if value == 0:
            return "0"
        if abs(value) < 1e-2 or abs(value) >= 1e4:
            return f"{value:.4e}"
        return f"{value:.4f}"

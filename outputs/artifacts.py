from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from schemas.design_state import DesignState
from simulator.ngspice import SimulationResult
from specs.models import CircuitSpecModel, SpecRegistry


@dataclass
class RunArtifacts:
    output_dir: Path
    design_state: Path
    netlist: Path
    sim_log: Path
    diagnostics: Path
    result_json: Path


def next_iteration(runs_dir: Path) -> int:
    if not runs_dir.exists():
        return 1
    max_n = 0
    for entry in runs_dir.iterdir():
        if entry.is_dir() and entry.name.startswith("iter_"):
            try:
                max_n = max(max_n, int(entry.name.split("_")[-1]))
            except ValueError:
                pass
    return max_n + 1


class ArtifactWriter:
    def __init__(self, runs_dir: str | Path = "runs", spec_registry: SpecRegistry | None = None):
        self.runs_dir = Path(runs_dir)
        self.spec_registry = spec_registry or SpecRegistry()

    def write(
        self,
        *,
        state: DesignState,
        best_meta: dict[str, Any],
        sim_result: SimulationResult,
        iteration: int,
        netlist_str: str,
        flow_meta: dict[str, Any] | None = None,
    ) -> RunArtifacts:
        output_dir = self.runs_dir / f"iter_{iteration:03d}"
        output_dir.mkdir(parents=True, exist_ok=True)
        design_state_path = output_dir / "design_state.yaml"
        netlist_path = output_dir / "netlist.cir"
        sim_log_path = output_dir / "sim_log.json"
        diagnostics_path = output_dir / "agent_diagnostics.json"
        result_path = output_dir / "result.json"

        state.to_yaml(design_state_path)
        netlist_path.write_text(netlist_str, encoding="utf-8")

        spec_model = self.spec_registry.select(state)
        sim_log = build_simulation_log(state, best_meta, sim_result, iteration, spec_model, flow_meta or {})
        sim_log_path.write_text(json.dumps(sim_log, indent=2), encoding="utf-8")

        diagnostics = build_agent_diagnostics(
            state,
            best_meta,
            sim_result,
            iteration,
            sim_log["comparison"],
            spec_model,
            flow_meta or {},
        )
        diagnostics_path.write_text(json.dumps(diagnostics, indent=2), encoding="utf-8")

        result = {
            "schema_version": "analogrf_ir.result.v0_1",
            "iteration": iteration,
            "status": diagnostics["status"],
            "measurements": sim_result.measurements,
            "artifacts": diagnostics["artifacts"],
        }
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

        return RunArtifacts(
            output_dir=output_dir,
            design_state=design_state_path,
            netlist=netlist_path,
            sim_log=sim_log_path,
            diagnostics=diagnostics_path,
            result_json=result_path,
        )


def build_simulation_log(
    state: DesignState,
    best_meta: dict[str, Any],
    sim_result: SimulationResult,
    iteration: int,
    spec_model: CircuitSpecModel,
    flow_meta: dict[str, Any],
) -> dict[str, Any]:
    decoded = best_meta.get("decoded", {}) or {}
    perf_est = best_meta.get("performance", {}) or {}
    proc = state.process
    w_grid = getattr(proc, "W_precision", 10e-9)
    l_grid = getattr(proc, "L_precision", 1e-9)
    w_dec = max(0, int(-math.floor(math.log10(w_grid)))) if w_grid > 0 else 0
    l_dec = max(0, int(-math.floor(math.log10(l_grid)))) if l_grid > 0 else 0

    def _sig(value, digits=8):
        if isinstance(value, float):
            return float(f"{value:.{digits}g}")
        return value

    def _snap(value, grid=1e-9):
        if isinstance(value, float) and grid > 0:
            dec = w_dec if grid == w_grid else l_dec
            return round(int(round(value / grid)) * grid, dec)
        return value

    log = {
        "schema_version": "analogrf_ir.sim_log.v0_1",
        "iteration": iteration,
        "timestamp": datetime.now().isoformat(),
        "design_name": state.design_name,
        "process": state.process.process_name,
        "supply_vdd": state.simulation.supply.get("vdd", 1.2),
        "spec_model": spec_model.name,
        "flow": flow_meta,
        "optimizer": {
            "convergence": True,
            "best_loss": best_meta.get("total_loss", 0),
            "performance_estimated": {k: _sig(v) for k, v in perf_est.items()},
            "loss_breakdown": {
                k: round(v, 6)
                for k, v in (best_meta.get("loss_breakdown", {}) or {}).items()
            },
            "decision_variables": {
                did: {
                    k: _snap(v, l_grid) if k == "L" else round(v, 2) if k == "gm_id" else v
                    for k, v in dv.items()
                }
                for did, dv in decoded.items()
                if did and not str(did).startswith("__")
            },
            "global_variables": {
                k: _sig(v)
                for k, v in (state.global_parameters or decoded.get("__global__", {}) or {}).items()
            },
            "transistor_params_opt": {
                did: {
                    "W": _snap(ts.parameters.W, w_grid),
                    "L": _snap(ts.parameters.L, l_grid),
                    "gm": ts.parameters.gm,
                    "gds": ts.parameters.gds,
                    "vgs": ts.parameters.vgs,
                    "vds": ts.parameters.vds,
                    "vdsat": ts.parameters.vdsat,
                    "region": ts.parameters.region or "unknown",
                    "id": ts.parameters.id,
                    "ft": ts.parameters.ft,
                    "gm_id_realized": ts.parameters.gm_id_realized,
                    "cgs": ts.parameters.cgs,
                    "cgd": ts.parameters.cgd,
                }
                for did, ts in state.transistors.items()
            },
        },
        "ngspice": {
            "success": sim_result.success,
            "return_code": sim_result.return_code,
            "elapsed_sec": round(sim_result.elapsed_sec, 3),
            "measurements": {k: round(v, 8) for k, v in sim_result.measurements.items()},
            "operating_points": {
                did: {
                    k: round(v, 6) if abs(v) < 1e-3 else round(v, 4)
                    for k, v in op.items()
                }
                for did, op in sim_result.operating_points.items()
            },
        },
        "comparison": {},
    }
    for key, est_val in perf_est.items():
        ng_key = spec_model.measurement_key(key)
        ng_val = sim_result.measurements.get(ng_key)
        log["comparison"][key] = {
            "optimizer_estimated": round(est_val, 3),
            "ngspice_measured": round(ng_val, 4) if ng_val is not None else None,
        }
    return log


def build_agent_diagnostics(
    state: DesignState,
    best_meta: dict[str, Any],
    sim_result: SimulationResult,
    iteration: int,
    comparison: dict[str, Any],
    spec_model: CircuitSpecModel,
    flow_meta: dict[str, Any],
) -> dict[str, Any]:
    measurements = sim_result.measurements or {}
    perf_est = best_meta.get("performance", {}) or {}
    loss_breakdown = best_meta.get("loss_breakdown", {}) or {}
    target_status = {
        name: spec_model.target_status(name, target, measurements, perf_est)
        for name, target in state.targets.items()
    }
    failed_targets = [name for name, item in target_status.items() if item["status"] == "fail"]
    unverified_targets = [name for name, item in target_status.items() if item["status"] == "unverified"]
    top_losses = _top_items(loss_breakdown, limit=8)
    mismatches = _comparison_mismatch(comparison)
    devices = _device_status(state)
    return {
        "schema_version": "analogrf_ir.agent_diagnostics.v0_1",
        "run": {
            "iteration": iteration,
            "timestamp": datetime.now().isoformat(),
            "design_name": state.design_name,
            "topology": state.topology.name,
            "architecture": state.topology.architecture,
            "class": state.topology.class_,
            "process": state.process.process_name,
            "technology_node_um": state.process.technology_node,
            "vdd": state.simulation.supply.get("vdd", 1.2),
            "spec_model": spec_model.name,
        },
        "flow": flow_meta,
        "status": {
            "ngspice_success": bool(sim_result.success),
            "return_code": sim_result.return_code,
            "spec_pass": bool(target_status) and not failed_targets and not unverified_targets,
            "failed_targets": failed_targets,
            "unverified_targets": unverified_targets,
            "best_loss": best_meta.get("total_loss", 0.0),
        },
        "targets": target_status,
        "optimizer": {
            "estimated_performance": perf_est,
            "top_loss_terms": top_losses,
            "decoded": best_meta.get("decoded", {}),
            "global_parameters": dict(state.global_parameters or {}),
        },
        "ngspice": {
            "measurements": measurements,
            "elapsed_sec": sim_result.elapsed_sec,
            "operating_point_count": len(sim_result.operating_points or {}),
        },
        "model_mismatch": mismatches,
        "devices": devices,
        "diagnosis": _diagnosis_items(state, target_status, top_losses, mismatches, devices),
        "artifacts": {
            "design_state": "design_state.yaml",
            "netlist": "netlist.cir",
            "sim_log": "sim_log.json",
            "agent_diagnostics": "agent_diagnostics.json",
            "result": "result.json",
        },
    }


def _top_items(values: dict, limit: int = 8) -> list[dict]:
    items = []
    for name, value in values.items():
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        items.append({"name": name, "value": numeric})
    return sorted(items, key=lambda item: abs(item["value"]), reverse=True)[:limit]


def _comparison_mismatch(comparison: dict) -> list[dict]:
    out = []
    for name, item in comparison.items():
        est = item.get("optimizer_estimated")
        meas = item.get("ngspice_measured")
        if est is None or meas is None:
            continue
        delta = meas - est
        rel = delta / max(abs(est), 1e-30)
        out.append(
            {
                "metric": name,
                "optimizer_estimated": est,
                "ngspice_measured": meas,
                "delta": delta,
                "rel_delta": rel,
            }
        )
    return sorted(out, key=lambda item: abs(item["rel_delta"]), reverse=True)


def _device_status(state: DesignState) -> dict:
    out = {}
    for dev in state.topology.devices:
        ts = state.transistors.get(dev.id)
        if ts is None:
            continue
        p = ts.parameters
        vds_margin = None
        if p.vds and p.vdsat:
            vds_margin = p.vds - p.vdsat * state.process.VDSAT_headroom_factor
        out[dev.id] = {
            "role": dev.role,
            "stage": dev.stage,
            "type": dev.type,
            "model": dev.model,
            "W": p.W,
            "L": p.L,
            "gm_id_strategy": ts.gm_id_strategy,
            "gm_id_realized": p.gm_id_realized,
            "region": p.region or "unknown",
            "id": p.id,
            "gm": p.gm,
            "gds": p.gds,
            "vgs": p.vgs,
            "vds": p.vds,
            "vdsat": p.vdsat,
            "vds_margin_vs_required": vds_margin,
        }
    return out


def _diagnosis_items(state: DesignState, targets: dict, losses: list[dict], mismatches: list[dict], devices: dict) -> list[dict]:
    items = []
    is_two_stage = _is_two_stage_ota(state)
    for name, target in targets.items():
        if target["status"] == "fail":
            hint = _target_hint(name, is_two_stage=is_two_stage)
            items.append(
                {
                    "type": "target_failure",
                    "metric": name,
                    "severity": "error",
                    "hint": hint,
                    "target": target,
                }
            )
        elif target["status"] == "unverified":
            items.append(
                {
                    "type": "target_unverified",
                    "metric": name,
                    "severity": "warning",
                    "hint": "Priority-1 targets require ngspice measurements before they can count as passing.",
                    "target": target,
                }
            )
    for loss in losses:
        if loss["value"] <= 0:
            continue
        severity = "warning" if loss["value"] < 10 else "error"
        items.append({"type": "loss_contributor", "name": loss["name"], "severity": severity, "value": loss["value"]})
    for mismatch in mismatches[:4]:
        if abs(mismatch["rel_delta"]) > 0.1:
            items.append({"type": "model_mismatch", "severity": "warning", **mismatch})
    for dev_id, dev in devices.items():
        margin = dev.get("vds_margin_vs_required")
        if margin is not None and margin < 0:
            items.append(
                {
                    "type": "device_headroom",
                    "severity": "warning",
                    "device": dev_id,
                    "margin": margin,
                    "hint": "Device may be outside robust saturation; adjust bias, current, or W/L.",
                }
            )
    return items


def _is_two_stage_ota(state: DesignState) -> bool:
    arch = (state.topology.architecture or "").lower()
    return state.topology.class_.lower() == "ota" and (
        "two" in arch or any(dev.role == "second_stage_gain" for dev in state.topology.devices)
    )


def _target_hint(name: str, *, is_two_stage: bool = False) -> str:
    if name in {"unity_gain_bandwidth", "ugbw", "bandwidth"}:
        if is_two_stage:
            return "After OP is valid, retune gm1/Cc: reduce Cc only if phase margin has enough reserve, otherwise keep the dominant pole low."
        return "Increase speed by raising useful gm, reducing compensation/load capacitance, or shortening high-capacitance devices."
    if name in {"phase_margin"}:
        if is_two_stage:
            return "For a two-stage OTA, first pull the dominant pole to lower frequency by increasing Cc, then set Rz near 1/gm2 and verify p2 separation."
        return "Improve stability by increasing compensation capacitance, moving Rz near the zero target, or reducing second-pole loading."
    if name in {"slew_rate", "slew_rate_pos", "slew_rate_neg"}:
        return "Increase available large-signal charging current or reduce compensation/load capacitance."
    if name in {"output_swing", "swing"}:
        return "Increase output headroom by reducing output-device VDSAT, relaxing current, or raising supply voltage."
    if name in {"icmr", "icmr_min", "icmr_max", "input_common_mode_min", "input_common_mode_max"}:
        return "Improve input common-mode range by reducing input/tail/load headroom or changing input topology."
    if name in {"dc_gain", "gain"}:
        return "Increase intrinsic gain by lengthening output/load devices or reducing output conductance."
    if name in {"delay", "regeneration_time"}:
        return "Increase latch/input gm or reduce capacitive loading."
    if name in {"offset", "input_referred_offset"}:
        return "Increase matching area or rebalance the input/latch devices."
    if name in {"power", "energy"}:
        return "Reduce bias currents or device widths on non-critical paths."
    return "Inspect the target-specific measurement and related loss term."

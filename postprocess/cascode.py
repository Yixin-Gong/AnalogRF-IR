from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from netlist.generator import generate_netlist
from postprocess.common import backfill_state_from_ngspice
from postprocess.ota import tune_single_stage_ota_operating_point
from schemas.design_state import DesignState, Target
from simulator.ngspice import NgspiceSimulator


def is_cascode_ota_state(state: DesignState) -> bool:
    architecture = (state.topology.architecture or "").lower()
    roles = {(dev.role or "").lower() for dev in state.topology.devices}
    return (
        (state.topology.class_ or "").lower() == "ota"
        and any(role == "input_pair" for role in roles)
        and (
            "cascode" in architecture
            or any("cascode" in role or "folded" in role for role in roles)
        )
    )


def tune_current_mirror_ota_operating_point(
    state: DesignState,
    sim: NgspiceSimulator,
    work_dir: Path,
) -> dict[str, Any]:
    event = tune_single_stage_ota_operating_point(
        state,
        sim,
        work_dir,
        max_candidates=120,
        time_budget_sec=150.0,
    )
    if not event:
        return {}
    return {"topology_family": "current_mirror_ota", **event}


def tune_cascode_ota_operating_point(
    state: DesignState,
    sim: NgspiceSimulator,
    work_dir: Path,
    *,
    max_candidates: int = 72,
    time_budget_sec: float = 90.0,
    candidate_timeout_sec: float = 5.0,
) -> dict[str, Any]:
    if not is_cascode_ota_state(state):
        return {}

    bias_ports = _bias_ports(state)
    if not bias_ports:
        return {}

    vdd = float(state.simulation.supply.get("vdd", 1.2) or 1.2)
    original_globals = dict(state.global_parameters or {})
    original_widths = _capture_widths(state)
    original_lengths = _capture_lengths(state)
    original_values = {name: _initial_bias_value(state, name, vdd) for name in bias_ports}
    candidates = _candidate_points(state, original_values, vdd)
    if not candidates:
        return {}

    records: list[dict[str, Any]] = []
    evaluated = 0
    started = time.time()
    tune_dir = work_dir / "cascode_ota_op_tune"
    original_timeout = getattr(sim, "timeout_sec", None)
    if original_timeout is not None and candidate_timeout_sec > 0:
        sim.timeout_sec = min(float(original_timeout), float(candidate_timeout_sec))
    try:
        for candidate in candidates[:max_candidates]:
            if time.time() - started > time_budget_sec:
                break
            _restore_globals(state, original_globals)
            _apply_candidate(state, candidate)
            result = sim.run(generate_netlist(state), work_dir=str(tune_dir), include_transient=False)
            evaluated += 1
            item = _score_candidate(state, result, candidate)
            records.append(item)
            if item["spec_pass"] and item["op_ok"]:
                break
    finally:
        if original_timeout is not None:
            sim.timeout_sec = original_timeout

    _restore_globals(state, original_globals)
    _restore_widths(state, original_widths)
    _restore_lengths(state, original_lengths)
    best = _select_candidate(records)
    if not best:
        return {}
    _apply_candidate(state, best)
    best = _refine_tail_current_for_speed(
        state,
        sim,
        tune_dir,
        best,
        original_globals,
        started=started,
        time_budget_sec=max(time_budget_sec, 180.0),
        candidate_timeout_sec=max(candidate_timeout_sec, 12.0),
    )
    best = _refine_widths_for_speed(
        state,
        sim,
        tune_dir,
        best,
        original_globals,
        original_widths,
        original_lengths,
        started=started,
        time_budget_sec=max(time_budget_sec, 240.0),
        candidate_timeout_sec=max(candidate_timeout_sec, 12.0),
    )
    _restore_globals(state, original_globals)
    _restore_widths(state, original_widths)
    _restore_lengths(state, original_lengths)
    _apply_candidate(state, best)
    final_result = best.get("_result")
    if final_result is not None and getattr(final_result, "operating_points", None):
        backfill_state_from_ngspice(state, final_result)

    return {
        "topology_family": _topology_family(state),
        "old_bias_values": original_values,
        "new_bias_values": {name: float(best[name]) for name in bias_ports if name in best},
        "selected_phase": str(best.get("phase", "")),
        "score": float(best["score"]),
        "spec_pass": bool(best["spec_pass"]),
        "op_ok": bool(best["op_ok"]),
        "op_margin": float(best.get("op_margin", 0.0)),
        "op_required_margin": float(best.get("op_required_margin", 0.0)),
        "candidate_count": evaluated,
        "current_refinement_count": int(best.get("current_refinement_count", 0)),
        "width_refinement_count": int(best.get("width_refinement_count", 0)),
        "new_current_values": {
            name: float(best[name])
            for name in ("I_tail",)
            if name in best
        },
        "width_scales": dict(best.get("width_scales", {}) or {}),
        "measurements": dict(best.get("measurements", {}) or {}),
        "elapsed_sec": time.time() - started,
    }


def _bias_ports(state: DesignState) -> list[str]:
    return [port.id for port in state.topology.ports if port.direction == "bias"]


def _initial_bias_value(state: DesignState, name: str, vdd: float) -> float:
    for key in (name, f"V{name}", f"{name}_voltage"):
        value = (state.global_parameters or {}).get(key)
        if value is not None:
            return _snap_voltage(_clip(float(value), 0.04 * vdd, 0.96 * vdd))
    low = name.lower()
    if "ptail" in low or ("tail" in low and "p" in low):
        return _snap_voltage(0.68 * vdd)
    if "pcas" in low or ("p" in low and "cas" in low):
        return _snap_voltage(0.46 * vdd)
    if "ncas" in low or "fold" in low or "sink" in low:
        return _snap_voltage(0.55 * vdd)
    if "tail" in low:
        return _snap_voltage(0.42 * vdd)
    fallback = getattr(state.simulation, "bias_voltage", None)
    return _snap_voltage(float(fallback or 0.5 * vdd))


def _candidate_points(
    state: DesignState,
    original: dict[str, float],
    vdd: float,
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = [{**original, "phase": "baseline"}]
    ports = list(original)

    heuristic: dict[str, list[float]] = {}
    for name, value in original.items():
        low, high = _bias_range(state, name, vdd)
        candidates = [
            value,
            value - 0.12,
            value - 0.06,
            value + 0.06,
            value + 0.12,
        ]
        name_low = name.lower()
        if "ptail" in name_low or ("tail" in name_low and "p" in name_low):
            candidates.extend([0.62 * vdd, 0.68 * vdd, 0.74 * vdd, 0.82 * vdd])
        elif "pcas" in name_low:
            candidates.extend([0.36 * vdd, 0.42 * vdd, 0.48 * vdd, 0.56 * vdd, 0.64 * vdd])
        elif "ncas" in name_low or "sink" in name_low:
            candidates.extend([0.42 * vdd, 0.48 * vdd, 0.55 * vdd, 0.62 * vdd, 0.70 * vdd])
        elif "tail" in name_low:
            candidates.extend([0.34 * vdd, 0.40 * vdd, 0.46 * vdd, 0.52 * vdd])
        else:
            candidates.extend([0.40 * vdd, 0.50 * vdd, 0.60 * vdd])
        heuristic[name] = _unique_values(candidates, low, high)

    # Named stack-balance presets are evaluated before one-at-a-time moves so
    # telescopic/folded stacks get a physically meaningful OP repair budget.
    for preset in _stack_balance_presets(original, vdd):
        point = dict(original)
        used = False
        for name, value in preset.items():
            if name == "phase":
                continue
            if name not in original:
                continue
            low, high = _bias_range(state, name, vdd)
            point[name] = _snap_voltage(_clip(value, low, high))
            used = True
        if used:
            point["phase"] = str(preset.get("phase", "stack_balance"))
            points.append(point)

    # One-at-a-time moves keep the repair trace interpretable when a preset is
    # not enough or a topology exposes a non-standard bias port.
    for name in ports:
        for value in heuristic[name]:
            point = dict(original)
            point[name] = value
            point["phase"] = f"{name}_sweep"
            points.append(point)

    return _dedupe(points)


def _stack_balance_presets(original: dict[str, float], vdd: float) -> list[dict[str, Any]]:
    presets: list[dict[str, Any]] = []
    if {"vbias_tail", "vbias_ncas", "vbias_pcas"}.issubset(original):
        presets.extend(
            [
                {
                    "phase": "telescopic_stack_balance",
                    "vbias_tail": 0.25 * vdd,
                    "vbias_ncas": 0.78 * vdd,
                    "vbias_pcas": 0.24 * vdd,
                },
                {
                    "phase": "telescopic_stack_balance",
                    "vbias_tail": 0.25 * vdd,
                    "vbias_ncas": 0.74 * vdd,
                    "vbias_pcas": 0.24 * vdd,
                },
                {
                    "phase": "telescopic_stack_balance",
                    "vbias_tail": 0.25 * vdd,
                    "vbias_ncas": 0.74 * vdd,
                    "vbias_pcas": 0.36 * vdd,
                },
                {
                    "phase": "telescopic_stack_balance",
                    "vbias_tail": 0.25 * vdd,
                    "vbias_ncas": 0.74 * vdd,
                    "vbias_pcas": 0.40 * vdd,
                },
                {
                    "phase": "telescopic_stack_balance",
                    "vbias_tail": 0.30 * vdd,
                    "vbias_ncas": 0.78 * vdd,
                    "vbias_pcas": 0.40 * vdd,
                },
                {
                    "phase": "telescopic_stack_balance",
                    "vbias_tail": 0.34 * vdd,
                    "vbias_ncas": 0.82 * vdd,
                    "vbias_pcas": 0.42 * vdd,
                },
                {
                    "phase": "telescopic_stack_balance",
                    "vbias_tail": 0.40 * vdd,
                    "vbias_ncas": 0.70 * vdd,
                    "vbias_pcas": 0.44 * vdd,
                },
                {
                    "phase": "telescopic_stack_balance",
                    "vbias_tail": 0.46 * vdd,
                    "vbias_ncas": 0.62 * vdd,
                    "vbias_pcas": 0.48 * vdd,
                },
                {
                    "phase": "telescopic_pcas_headroom",
                    "vbias_tail": 0.34 * vdd,
                    "vbias_ncas": 0.78 * vdd,
                    "vbias_pcas": 0.54 * vdd,
                },
                {
                    "phase": "telescopic_pcas_headroom",
                    "vbias_tail": 0.38 * vdd,
                    "vbias_ncas": 0.74 * vdd,
                    "vbias_pcas": 0.58 * vdd,
                },
                {
                    "phase": "telescopic_pcas_headroom",
                    "vbias_tail": 0.42 * vdd,
                    "vbias_ncas": 0.70 * vdd,
                    "vbias_pcas": 0.62 * vdd,
                },
                {
                    "phase": "telescopic_pcas_headroom",
                    "vbias_tail": 0.46 * vdd,
                    "vbias_ncas": 0.66 * vdd,
                    "vbias_pcas": 0.66 * vdd,
                },
            ]
        )
    if {"vbias_ptail", "vbias_ncas"}.issubset(original):
        presets.extend(
            [
                {"phase": "folded_stack_balance", "vbias_ptail": 0.68 * vdd, "vbias_ncas": 0.44 * vdd},
                {"phase": "folded_stack_balance", "vbias_ptail": 0.74 * vdd, "vbias_ncas": 0.50 * vdd},
                {"phase": "folded_stack_balance", "vbias_ptail": 0.82 * vdd, "vbias_ncas": 0.56 * vdd},
            ]
        )
    return presets


def _bias_range(state: DesignState, name: str, vdd: float) -> tuple[float, float]:
    for dv in state.design_variables:
        if not dv.device and dv.variable == name:
            return max(float(dv.range.min), 0.02 * vdd), min(float(dv.range.max), 0.98 * vdd)
    rng = state.constraints.global_.get(name) if getattr(state.constraints, "global_", None) else None
    if rng:
        return max(float(rng.min), 0.02 * vdd), min(float(rng.max), 0.98 * vdd)
    return 0.05 * vdd, 0.95 * vdd


def _apply_candidate(state: DesignState, candidate: dict[str, Any]) -> None:
    for name in _bias_ports(state):
        if name in candidate:
            state.global_parameters[name] = _snap_voltage(float(candidate[name]))
    for name in ("I_tail",):
        if name in candidate:
            state.global_parameters[name] = float(candidate[name])
    widths = candidate.get("_widths")
    if isinstance(widths, dict):
        for dev_id, width in widths.items():
            if dev_id in state.transistors:
                state.transistors[dev_id].parameters.W = float(width)
    lengths = candidate.get("_lengths")
    if isinstance(lengths, dict):
        for dev_id, length in lengths.items():
            if dev_id in state.transistors:
                state.transistors[dev_id].parameters.L = float(length)
                state.transistors[dev_id].L_strategy = float(length)


def _restore_globals(state: DesignState, original_globals: dict[str, float]) -> None:
    state.global_parameters.clear()
    state.global_parameters.update(original_globals)


def _score_candidate(state: DesignState, result, candidate: dict[str, Any]) -> dict[str, Any]:
    meas = dict(result.measurements or {})
    gain = float(meas.get("dc_gain_db", -200.0) or -200.0)
    bw = float(meas.get("unity_gain_bandwidth", 0.0) or 0.0)
    pm = float(meas.get("phase_margin", 0.0) or 0.0)
    sr = float(meas.get("slew_rate", 0.0) or 0.0)
    power = float(meas.get("total_power", 0.0) or 0.0)
    swing = float(meas.get("output_swing", 0.0) or 0.0)

    targets = state.targets
    gain_min = float(targets.get("dc_gain", Target()).min or 0.0)
    bw_min = float(targets.get("unity_gain_bandwidth", Target()).min or 0.0)
    pm_min = float(targets.get("phase_margin", Target()).min or 0.0)
    sr_min = float(targets.get("slew_rate", Target()).min or 0.0)
    power_max = float(targets.get("power", Target()).max or float("inf"))
    swing_min = float(targets.get("output_swing", Target()).min or 0.0)

    if not result.success:
        gain -= 200.0
    op_margin, op_required_margin = _minimum_margins(state, result.operating_points or {})
    op_ok = op_required_margin >= 0.0
    spec_pass = (
        gain >= gain_min
        and (bw_min <= 0.0 or bw >= bw_min)
        and (pm_min <= 0.0 or pm >= pm_min)
        and (sr_min <= 0.0 or sr >= sr_min)
        and (power_max == float("inf") or power <= power_max)
        and (swing_min <= 0.0 or swing >= swing_min)
    )
    score = 0.0
    score += 90.0 * max(0.0, gain_min - gain) / max(gain_min, 1.0)
    score += 45.0 * max(0.0, bw_min - bw) / max(bw_min, 1.0)
    score += 45.0 * max(0.0, pm_min - pm) / max(pm_min, 1.0)
    score += 35.0 * max(0.0, sr_min - sr) / max(sr_min, 1.0)
    score += 35.0 * max(0.0, swing_min - swing) / max(swing_min, 1.0)
    if power_max < float("inf"):
        score += 25.0 * max(0.0, power - power_max) / max(power_max, 1e-12)
    if op_required_margin < 0.0:
        score += 80.0 * abs(op_required_margin)
    if op_required_margin < -0.05:
        score += 40.0 * abs(op_required_margin + 0.05)
    if gain <= 0.0:
        score += 300.0
    if bw_min > 0.0 and "unity_gain_bandwidth" not in meas:
        score += 120.0
    if pm_min > 0.0 and "phase_margin" not in meas:
        score += 120.0
    if sr_min > 0.0 and "slew_rate" not in meas:
        score += 80.0
    if spec_pass:
        score -= 250.0
    gain_deficit = max(0.0, gain_min - gain) / max(gain_min, 1.0)
    bw_deficit = max(0.0, bw_min - bw) / max(bw_min, 1.0)
    pm_deficit = max(0.0, pm_min - pm) / max(pm_min, 1.0)
    sr_deficit = max(0.0, sr_min - sr) / max(sr_min, 1.0)
    swing_deficit = max(0.0, swing_min - swing) / max(swing_min, 1.0)
    op_deficit = max(0.0, -op_required_margin) / max(_target_saturation_margin(state), 1e-3)
    measured_deficit = gain_deficit + bw_deficit + pm_deficit + sr_deficit + swing_deficit
    measured_fail_count = sum(
        1
        for value in (gain_deficit, bw_deficit, pm_deficit, sr_deficit, swing_deficit)
        if value > 0.0
    )
    item = dict(candidate)
    item.update(
        {
            "score": score,
            "measured_deficit": measured_deficit,
            "measured_fail_count": measured_fail_count,
            "gain_deficit": gain_deficit,
            "bw_deficit": bw_deficit,
            "pm_deficit": pm_deficit,
            "sr_deficit": sr_deficit,
            "swing_deficit": swing_deficit,
            "measurements": meas,
            "success": bool(result.success),
            "spec_pass": spec_pass,
            "op_ok": op_ok,
            "op_margin": op_margin,
            "op_required_margin": op_required_margin,
            "_result": result,
        }
    )
    return item


def _refine_tail_current_for_speed(
    state: DesignState,
    sim: NgspiceSimulator,
    tune_dir: Path,
    best: dict[str, Any],
    original_globals: dict[str, float],
    *,
    started: float,
    time_budget_sec: float,
    candidate_timeout_sec: float,
) -> dict[str, Any]:
    targets = state.targets
    bw_min = float(targets.get("unity_gain_bandwidth", Target()).min or 0.0)
    sr_min = float(targets.get("slew_rate", Target()).min or 0.0)
    if bw_min <= 0.0 and sr_min <= 0.0:
        return best
    meas = best.get("measurements", {}) or {}
    speed_gap = (
        (bw_min > 0.0 and float(meas.get("unity_gain_bandwidth", 0.0) or 0.0) < bw_min)
        or (sr_min > 0.0 and float(meas.get("slew_rate", 0.0) or 0.0) < sr_min)
    )
    if not speed_gap:
        return best
    current_range = _global_variable_range(state, "I_tail")
    if not current_range:
        return best
    low, high = current_range
    base_current = _global_variable_value(state, original_globals, "I_tail")
    if base_current <= 0.0 or high <= low:
        return best

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    original_timeout = getattr(sim, "timeout_sec", None)
    if original_timeout is not None and candidate_timeout_sec > 0:
        sim.timeout_sec = min(float(original_timeout), float(candidate_timeout_sec))
    try:
        for scale in (1.15, 1.35, 1.6, 2.0, 2.6, 3.4, 4.5, 6.0, 8.0, 11.0, 15.0, 22.0, 32.0):
            if time.time() - started > time_budget_sec:
                break
            current = min(max(base_current * scale, low), high)
            key = f"{current:.12e}"
            if key in seen:
                continue
            seen.add(key)
            _restore_globals(state, original_globals)
            _apply_candidate(state, best)
            state.global_parameters["I_tail"] = current
            trial = sim.run(generate_netlist(state), work_dir=str(tune_dir), include_transient=True)
            candidate = dict(best)
            candidate["phase"] = f"{best.get('phase', '')}+tail_current_speed_refine"
            candidate["I_tail"] = current
            item = _score_candidate(state, trial, candidate)
            item["current_refinement_count"] = len(seen)
            records.append(item)
            if item.get("spec_pass", False):
                break
    finally:
        if original_timeout is not None:
            sim.timeout_sec = original_timeout

    selected = _select_candidate([best, *records])
    if selected is None:
        return best
    selected["current_refinement_count"] = len(records)
    return selected


def _refine_widths_for_speed(
    state: DesignState,
    sim: NgspiceSimulator,
    tune_dir: Path,
    best: dict[str, Any],
    original_globals: dict[str, float],
    base_widths: dict[str, float],
    base_lengths: dict[str, float],
    *,
    started: float,
    time_budget_sec: float,
    candidate_timeout_sec: float,
) -> dict[str, Any]:
    targets = state.targets
    bw_min = float(targets.get("unity_gain_bandwidth", Target()).min or 0.0)
    sr_min = float(targets.get("slew_rate", Target()).min or 0.0)
    meas = best.get("measurements", {}) or {}
    speed_gap = (
        (bw_min > 0.0 and float(meas.get("unity_gain_bandwidth", 0.0) or 0.0) < bw_min)
        or (sr_min > 0.0 and float(meas.get("slew_rate", 0.0) or 0.0) < sr_min)
    )
    if not speed_gap:
        return best

    templates = _width_speed_templates(state)
    if not templates:
        return best
    records: list[dict[str, Any]] = []
    original_timeout = getattr(sim, "timeout_sec", None)
    if original_timeout is not None and candidate_timeout_sec > 0:
        sim.timeout_sec = min(float(original_timeout), float(candidate_timeout_sec))
    try:
        for idx, template in enumerate(templates, start=1):
            if time.time() - started > time_budget_sec:
                break
            _restore_globals(state, original_globals)
            _restore_widths(state, base_widths)
            _restore_lengths(state, base_lengths)
            _apply_candidate(state, best)
            applied = _apply_role_geometry_template(state, base_widths, base_lengths, template)
            if not applied:
                continue
            trial = sim.run(generate_netlist(state), work_dir=str(tune_dir), include_transient=True)
            candidate = dict(best)
            candidate["phase"] = f"{best.get('phase', '')}+role_width_speed_refine"
            candidate["_widths"] = _capture_widths(state)
            candidate["_lengths"] = _capture_lengths(state)
            candidate["width_scales"] = template
            item = _score_candidate(state, trial, candidate)
            item["width_refinement_count"] = idx
            item["width_scales"] = template
            item["_widths"] = candidate["_widths"]
            records.append(item)
            if item.get("spec_pass", False):
                break
    finally:
        if original_timeout is not None:
            sim.timeout_sec = original_timeout

    selected = _select_candidate([best, *records])
    if selected is None:
        return best
    selected["width_refinement_count"] = len(records)
    return selected


def _width_speed_templates(state: DesignState) -> list[dict[str, Any]]:
    family = _topology_family(state)
    if family == "telescopic_cascode_ota":
        return [
            {
                "__role_min_widths__": {
                    "input_pair": 22.0e-6,
                    "tail_current_source": 11.0e-6,
                    "input_cascode": 7.5e-6,
                    "current_mirror_load": 110.0e-6,
                    "load_cascode": 70.0e-6,
                },
                "__role_min_lengths__": {
                    "input_pair": 1.95e-6,
                    "tail_current_source": 0.33e-6,
                    "input_cascode": 1.93e-6,
                    "current_mirror_load": 2.42e-6,
                    "load_cascode": 1.98e-6,
                },
            },
            {
                "input_pair": 2.0,
                "tail_current_source": 3.0,
                "current_mirror_load": 2.0,
                "load_cascode": 5.0,
            },
            {
                "input_pair": 2.0,
                "tail_current_source": 3.0,
                "current_mirror_load": 2.0,
                "load_cascode": 8.0,
            },
            {
                "input_pair": 2.4,
                "tail_current_source": 3.0,
                "input_cascode": 1.1,
                "current_mirror_load": 2.25,
                "load_cascode": 8.2,
            },
            {
                "tail_current_source": 3.0,
                "current_mirror_load": 2.0,
                "load_cascode": 8.0,
            },
        ]
    return [
        {
            "input_pair": 1.5,
            "tail_current_source": 2.0,
            "current_mirror_load": 1.5,
            "load_cascode": 2.0,
            "input_cascode": 1.3,
        },
        {
            "input_pair": 2.0,
            "tail_current_source": 2.5,
            "current_mirror_load": 2.0,
            "load_cascode": 3.0,
            "input_cascode": 1.5,
        },
    ]


def _apply_role_geometry_template(
    state: DesignState,
    base_widths: dict[str, float],
    base_lengths: dict[str, float],
    role_scales: dict[str, Any],
) -> bool:
    proc = state.process
    min_w = float(getattr(proc, "min_W", 150e-9) or 150e-9)
    max_w = float(getattr(proc, "max_W", 200e-6) or 200e-6)
    min_l = float(getattr(proc, "min_L", 130e-9) or 130e-9)
    max_l = float(getattr(proc, "max_L", 3e-6) or 3e-6)
    min_widths = role_scales.get("__role_min_widths__", {})
    min_lengths = role_scales.get("__role_min_lengths__", {})
    any_applied = False
    for dev in state.topology.devices:
        ts = state.transistors.get(dev.id)
        base_w = base_widths.get(dev.id, 0.0)
        if ts is None or base_w <= 0.0:
            continue
        scale = role_scales.get(dev.role)
        min_role_w = min_widths.get(dev.role)
        if scale is not None or min_role_w is not None:
            next_w = base_w * float(scale) if scale is not None else base_w
            if min_role_w is not None:
                next_w = max(next_w, float(min_role_w))
            ts.parameters.W = min(max(next_w, min_w), max_w)
            any_applied = True
        min_role_l = min_lengths.get(dev.role)
        if min_role_l is not None:
            base_l = base_lengths.get(dev.id, ts.parameters.L)
            ts.parameters.L = min(max(max(base_l, float(min_role_l)), min_l), max_l)
            ts.L_strategy = ts.parameters.L
            any_applied = True
    if any_applied:
        state._ensure_wl_on_grid()
    return any_applied


def _capture_widths(state: DesignState) -> dict[str, float]:
    return {
        dev_id: float(ts.parameters.W)
        for dev_id, ts in state.transistors.items()
        if ts.parameters.W > 0.0
    }


def _restore_widths(state: DesignState, widths: dict[str, float]) -> None:
    for dev_id, width in widths.items():
        if dev_id in state.transistors:
            state.transistors[dev_id].parameters.W = float(width)


def _capture_lengths(state: DesignState) -> dict[str, float]:
    return {
        dev_id: float(ts.parameters.L)
        for dev_id, ts in state.transistors.items()
        if ts.parameters.L > 0.0
    }


def _restore_lengths(state: DesignState, lengths: dict[str, float]) -> None:
    for dev_id, length in lengths.items():
        if dev_id in state.transistors:
            state.transistors[dev_id].parameters.L = float(length)
            state.transistors[dev_id].L_strategy = float(length)


def _global_variable_range(state: DesignState, name: str) -> tuple[float, float] | None:
    for dv in state.design_variables:
        if not dv.device and dv.variable == name:
            return float(dv.range.min), float(dv.range.max)
    return None


def _global_variable_value(state: DesignState, original_globals: dict[str, float], name: str) -> float:
    if name in original_globals:
        return float(original_globals[name])
    value = state.global_parameters.get(name)
    if value is not None:
        return float(value)
    for dv in state.design_variables:
        if not dv.device and dv.variable == name and dv.initial is not None:
            return float(dv.initial)
    return 0.0


def _select_candidate(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None
    usable = [item for item in records if item.get("success", False)] or records
    return min(usable, key=_candidate_priority)


def _candidate_priority(item: dict[str, Any]) -> tuple[float, ...]:
    meas = item.get("measurements", {}) or {}
    gain = float(meas.get("dc_gain_db", -200.0) or -200.0)
    bw = float(meas.get("unity_gain_bandwidth", 0.0) or 0.0)
    swing = float(meas.get("output_swing", 0.0) or 0.0)
    op_required_margin = float(item.get("op_required_margin", -1.0) or -1.0)
    op_bucket = 0.0 if op_required_margin >= 0.0 else 1.0 if op_required_margin >= -0.05 else 2.0
    missing_required = 0.0 if {"unity_gain_bandwidth", "phase_margin"}.issubset(meas) else 1.0
    return (
        0.0 if item.get("spec_pass", False) else 1.0,
        float(item.get("measured_fail_count", 99.0)),
        float(item.get("measured_deficit", float("inf"))),
        0.0 if gain > 0.0 else 1.0,
        missing_required,
        float(item.get("gain_deficit", 1.0)),
        float(item.get("swing_deficit", 1.0)),
        float(item.get("pm_deficit", 1.0)),
        float(item.get("sr_deficit", 1.0)),
        float(item.get("bw_deficit", 1.0)),
        op_bucket,
        max(0.0, -op_required_margin),
        float(item.get("score", float("inf"))),
        -gain,
        -bw,
        -swing,
    )


def _minimum_margins(state: DesignState, operating_points: dict[str, dict[str, float]]) -> tuple[float, float]:
    margins: list[float] = []
    required_margins: list[float] = []
    for dev in state.topology.devices:
        op = _lookup_op(operating_points, dev.id)
        if not op:
            continue
        margin = abs(float(op.get("vds", 0.0))) - abs(float(op.get("vdsat", 0.0)))
        margins.append(margin)
        required_margins.append(margin - _required_saturation_margin(state, dev.role))
    return (min(margins), min(required_margins)) if margins else (-1.0, -1.0)


def _required_saturation_margin(state: DesignState, role: str) -> float:
    return _target_saturation_margin(state)


def _target_saturation_margin(state: DesignState) -> float:
    target = state.targets.get("saturation_margin")
    if target is not None and target.min is not None:
        return max(0.0, float(target.min))
    return 0.05


def _lookup_op(operating_points: dict[str, dict[str, float]], device_id: str) -> dict[str, float]:
    for key in (device_id, f"M{device_id}", device_id.upper(), f"M{device_id}".upper()):
        if key in operating_points:
            return operating_points[key]
    return {}


def _topology_family(state: DesignState) -> str:
    architecture = (state.topology.architecture or "").lower()
    if "folded" in architecture:
        return "folded_cascode_ota"
    if "telescopic" in architecture:
        return "telescopic_cascode_ota"
    return "cascode_ota"


def _dedupe(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[tuple[str, float], ...]] = set()
    out: list[dict[str, Any]] = []
    for point in points:
        key = tuple(
            sorted(
                (name, round(float(value), 4))
                for name, value in point.items()
                if name != "phase"
            )
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(point)
    return out


def _unique_values(values: list[float], low: float, high: float) -> list[float]:
    out: list[float] = []
    seen: set[float] = set()
    for value in values:
        snapped = _snap_voltage(_clip(value, low, high))
        if snapped in seen:
            continue
        seen.add(snapped)
        out.append(snapped)
    return out


def _clip(value: float, low: float, high: float) -> float:
    return min(max(float(value), low), high)


def _snap_voltage(value: float) -> float:
    return round(float(value), 4)

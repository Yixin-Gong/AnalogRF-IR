from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

from netlist.generator import generate_netlist
from postprocess.common import backfill_state_from_ngspice, phase_margin_window_penalty
from schemas.design_state import DesignState, Target
from simulator.ngspice import NgspiceSimulator


def is_single_stage_ota_state(state: DesignState) -> bool:
    architecture = (state.topology.architecture or "").lower()
    if "two" in architecture or "source_follower" in architecture or "follower" in architecture:
        return False
    roles = {(dev.role or "").lower() for dev in state.topology.devices}
    return {
        "input_pair",
        "current_mirror_load",
        "tail_current_source",
    }.issubset(roles)


def tune_single_stage_ota_operating_point(
    state: DesignState,
    sim: NgspiceSimulator,
    work_dir: Path,
    *,
    max_candidates: int = 42,
    time_budget_sec: float = 50.0,
    candidate_timeout_sec: float = 5.0,
) -> dict[str, Any]:
    if not is_single_stage_ota_state(state):
        return {}
    input_ids = [dev.id for dev in state.topology.devices if dev.role == "input_pair"]
    load_ids = [dev.id for dev in state.topology.devices if dev.role == "current_mirror_load"]
    tail_id = next((dev.id for dev in state.topology.devices if dev.role == "tail_current_source"), "")
    if len(input_ids) < 2 or len(load_ids) < 2 or not tail_id:
        return {}

    vdd = float(state.simulation.supply.get("vdd", 1.2) or 1.2)
    original_globals = dict(state.global_parameters or {})
    original_dims = {
        dev_id: (
            state.transistors[dev_id].parameters.W,
            state.transistors[dev_id].parameters.L,
            state.transistors[dev_id].L_strategy,
        )
        for dev_id in input_ids + load_ids + [tail_id]
        if dev_id in state.transistors
    }
    if not original_dims:
        return {}

    old_vbias = _initial_vbias(state, tail_id, vdd)
    candidates = _candidate_points(state, old_vbias, vdd)
    if not candidates:
        return {}

    records: list[dict[str, Any]] = []
    evaluated = 0
    started = time.time()
    tune_dir = work_dir / "single_stage_ota_op_tune"
    original_timeout = getattr(sim, "timeout_sec", None)
    if original_timeout is not None and candidate_timeout_sec > 0:
        sim.timeout_sec = min(float(original_timeout), float(candidate_timeout_sec))
    try:
        for candidate in candidates[:max_candidates]:
            if time.time() - started > time_budget_sec:
                break
            _restore_state(state, original_globals, original_dims)
            _apply_candidate(state, candidate, input_ids, load_ids, tail_id)
            result = sim.run(generate_netlist(state), work_dir=str(tune_dir), include_transient=False)
            evaluated += 1
            item = _score_candidate(state, result, candidate)
            records.append(item)
            if item["spec_pass"] and item["op_ok"]:
                break
    finally:
        if original_timeout is not None:
            sim.timeout_sec = original_timeout

    _restore_state(state, original_globals, original_dims)
    best = _select_two_phase_candidate(state, records)
    if not best:
        return {}
    best = _validate_single_stage_dynamic_candidates(
        state,
        sim,
        tune_dir,
        best,
        records,
        original_globals,
        original_dims,
        input_ids,
        load_ids,
        tail_id,
        started=started,
        time_budget_sec=max(time_budget_sec, 90.0),
        candidate_timeout_sec=max(candidate_timeout_sec, 6.0),
    )
    best = _refine_single_stage_gain_headroom_boundary(
        state,
        sim,
        tune_dir,
        best,
        records,
        original_globals,
        original_dims,
        input_ids,
        load_ids,
        tail_id,
        started=started,
        time_budget_sec=max(time_budget_sec, 90.0),
    )
    best = _refine_current_mirror_reference_geometry(
        state,
        sim,
        tune_dir,
        best,
        original_globals,
        original_dims,
        input_ids,
        load_ids,
        tail_id,
        started=started,
        time_budget_sec=max(time_budget_sec, 110.0),
    )
    best = _refine_single_stage_bandwidth_boundary(
        state,
        sim,
        tune_dir,
        best,
        original_globals,
        original_dims,
        input_ids,
        load_ids,
        tail_id,
        started=started,
        time_budget_sec=max(time_budget_sec, 90.0),
    )
    best = _refine_single_stage_headroom_recovery(
        state,
        sim,
        tune_dir,
        best,
        original_globals,
        original_dims,
        input_ids,
        load_ids,
        tail_id,
        started=started,
        time_budget_sec=max(time_budget_sec, 90.0),
    )
    _apply_candidate(state, best, input_ids, load_ids, tail_id)
    final_result = best.get("_result")
    if final_result is not None and getattr(final_result, "operating_points", None):
        backfill_state_from_ngspice(state, final_result)

    measurements = dict(best.get("measurements", {}) or {})
    return {
        "old_vbias": old_vbias,
        "new_vbias": float(best["vbias"]),
        "input_width_scale": float(best["input_width_scale"]),
        "input_length_scale": float(best["input_length_scale"]),
        "load_width_scale": float(best.get("load_width_scale", 1.0)),
        "load_length_scale": float(best["load_length_scale"]),
        "tail_width_scale": float(best["tail_width_scale"]),
        "tail_length_scale": float(best["tail_length_scale"]),
        "selected_phase": str(best.get("selected_phase", best.get("phase", ""))),
        "score": float(best["score"]),
        "gain_anchor": dict(best.get("gain_anchor", {}) or {}),
        "bandwidth_guard_floor": float(best.get("bandwidth_guard_floor", 0.0) or 0.0),
        "spec_pass": bool(best["spec_pass"]),
        "op_ok": bool(best["op_ok"]),
        "op_margin": float(best.get("op_margin", 0.0)),
        "op_required_margin": float(best.get("op_required_margin", 0.0)),
        "candidate_count": evaluated,
        "dynamic_validation_count": int(best.get("dynamic_validation_count", 0)),
        "gain_headroom_refinement_count": int(best.get("gain_headroom_refinement_count", 0)),
        "current_mirror_reference_count": int(best.get("current_mirror_reference_count", 0)),
        "bandwidth_refinement_count": int(best.get("bandwidth_refinement_count", 0)),
        "headroom_recovery_count": int(best.get("headroom_recovery_count", 0)),
        "measurements": measurements,
        "elapsed_sec": time.time() - started,
    }


def _initial_vbias(state: DesignState, tail_id: str, vdd: float) -> float:
    for key in ("vbias", "Vvbias", "vbias_voltage"):
        value = state.global_parameters.get(key)
        if value is not None:
            return _clip(float(value), 0.05 * vdd, 0.95 * vdd)
    tail = state.transistors.get(tail_id)
    if tail and tail.parameters.vgs > 0:
        return _clip(float(tail.parameters.vgs), 0.05 * vdd, 0.95 * vdd)
    fallback = getattr(state.simulation, "bias_voltage", None)
    return _clip(float(fallback or 0.5 * vdd), 0.05 * vdd, 0.95 * vdd)


def _candidate_points(state: DesignState, old_vbias: float, vdd: float) -> list[dict[str, Any]]:
    targets = state.targets
    gain_min = float(targets.get("dc_gain", Target()).min or 0.0)
    bw_min = float(targets.get("unity_gain_bandwidth", Target()).min or 0.0)
    aggressive = gain_min >= 40.0 or bw_min >= 50e6

    vbias_values = _unique_values(
        [
            old_vbias,
            old_vbias - 0.16,
            old_vbias - 0.12,
            old_vbias - 0.08,
            old_vbias - 0.04,
            old_vbias + 0.03,
            old_vbias + 0.06,
            old_vbias + 0.09,
            old_vbias + 0.12,
            old_vbias + 0.16,
            0.34 * vdd,
            0.38 * vdd,
            0.42 * vdd,
            0.46 * vdd,
            0.50 * vdd,
            0.54 * vdd,
            0.58 * vdd,
            0.62 * vdd,
        ],
        0.05 * vdd,
        0.95 * vdd,
    )
    load_scales = [1.0, 1.35, 1.75, 2.3, 3.0, 4.0] if aggressive else [1.0, 1.25, 1.6]
    input_width_scales = [1.0, 1.25, 1.5, 1.75, 2.3] if aggressive else [1.0, 1.25]
    input_length_scales = [1.0, 1.5, 2.0, 3.0, 4.5, 6.0] if aggressive else [1.0, 1.5, 2.0]
    tail_width_scales = [0.65, 0.8, 1.0, 1.4, 2.0]
    tail_length_scales = [1.0, 1.5, 2.0, 3.0]

    points: list[dict[str, float]] = [
        _point(old_vbias, 1.0, 1.0, 1.0, 1.0, 1.0, phase="baseline"),
    ]
    focused = [
        (old_vbias - 0.05, 4.00, 4.50, 1.75, 1.00, 2.50, "gain_speed"),
        (old_vbias - 0.04, 4.00, 4.50, 2.00, 1.00, 2.50, "gain_speed"),
        (old_vbias - 0.03, 4.00, 4.50, 2.00, 1.20, 2.00, "gain_speed"),
        (0.42 * vdd, 4.00, 4.50, 2.00, 1.00, 2.50, "gain_speed"),
        (old_vbias + 0.09, 1.75, 1.50, 1.50, 1.40, 1.50, "bandwidth"),
        (old_vbias + 0.12, 2.30, 2.00, 1.75, 1.40, 2.00, "bandwidth"),
        (old_vbias + 0.16, 3.00, 2.00, 2.30, 2.00, 2.00, "bandwidth"),
        (0.58 * vdd, 2.30, 2.00, 1.75, 1.40, 2.00, "bandwidth"),
        (old_vbias - 0.12, 2.30, 2.00, 1.25, 0.80, 2.00, "gain"),
        (old_vbias - 0.08, 3.00, 3.00, 1.50, 0.80, 2.00, "gain"),
        (old_vbias - 0.04, 4.00, 4.50, 1.75, 0.65, 3.00, "gain"),
        (0.38 * vdd, 3.00, 3.00, 1.50, 0.80, 2.00, "gain"),
        (0.42 * vdd, 4.00, 4.50, 1.75, 0.80, 3.00, "gain"),
        (old_vbias + 0.06, 1.35, 1.00, 1.35, 1.00, 1.50, "bandwidth"),
        (old_vbias + 0.09, 1.75, 1.50, 1.50, 1.40, 1.50, "bandwidth"),
        (old_vbias + 0.12, 2.30, 2.00, 1.75, 1.40, 2.00, "bandwidth"),
        (old_vbias + 0.16, 3.00, 2.00, 2.30, 2.00, 2.00, "bandwidth"),
        (0.54 * vdd, 1.75, 1.50, 1.50, 1.40, 1.50, "bandwidth"),
        (0.58 * vdd, 2.30, 2.00, 1.75, 1.40, 2.00, "bandwidth"),
        (0.62 * vdd, 3.00, 2.00, 2.30, 2.00, 2.00, "bandwidth"),
    ]
    points.extend(_point(v, l, il, iw, tw, tl, phase=phase) for v, l, il, iw, tw, tl, phase in focused)
    if aggressive:
        for vbias in _unique_values([old_vbias + 0.03, old_vbias + 0.06, 0.42 * vdd, 0.46 * vdd], 0.05 * vdd, 0.95 * vdd):
            for load_w in (4.0, 7.0, 9.0):
                points.append(
                    _point(
                        vbias,
                        3.0,
                        3.0,
                        1.75,
                        1.15,
                        1.50,
                        phase="mirror_load_width",
                        load_width_scale=load_w,
                    )
                )
                points.append(
                    _point(
                        vbias,
                        4.0,
                        4.5,
                        2.00,
                        1.40,
                        1.50,
                        phase="mirror_load_width_speed",
                        load_width_scale=load_w,
                    )
                )

    for vbias in vbias_values[:7]:
        for load_scale in load_scales[2:]:
            for input_l in input_length_scales[1:]:
                points.append(_point(vbias, load_scale, input_l, min(input_l, input_width_scales[-1]), 0.8, 2.0, phase="gain"))
            for tail_w in tail_width_scales[:3]:
                points.append(_point(vbias, load_scale, 2.0, 1.25, tail_w, 2.0, phase="gain"))
    for vbias in vbias_values:
        for load_scale in load_scales[:3]:
            points.append(_point(vbias, load_scale, 1.0, 1.0, 1.0, 1.0, phase="balanced"))
    for vbias in vbias_values[-6:]:
        for load_scale in load_scales[1:4]:
            for input_scale in input_width_scales[1:]:
                points.append(_point(vbias, load_scale, 1.5, input_scale, 1.4, 1.5, phase="bandwidth"))
    for vbias in vbias_values[-5:]:
        for tail_w in tail_width_scales[3:]:
            for tail_l in tail_length_scales[1:3]:
                points.append(_point(vbias, load_scales[-2], 1.5, input_width_scales[-1], tail_w, tail_l, phase="bandwidth"))

    return _dedupe_points(points)


def _point(
    vbias: float,
    load_length_scale: float,
    input_length_scale: float,
    input_width_scale: float,
    tail_width_scale: float,
    tail_length_scale: float,
    *,
    phase: str,
    load_width_scale: float = 1.0,
) -> dict[str, Any]:
    return {
        "vbias": float(vbias),
        "load_width_scale": float(load_width_scale),
        "load_length_scale": float(load_length_scale),
        "input_length_scale": float(input_length_scale),
        "input_width_scale": float(input_width_scale),
        "tail_width_scale": float(tail_width_scale),
        "tail_length_scale": float(tail_length_scale),
        "phase": phase,
    }


def _apply_candidate(
    state: DesignState,
    candidate: dict[str, Any],
    input_ids: list[str],
    load_ids: list[str],
    tail_id: str,
) -> None:
    state.global_parameters["vbias"] = _snap_voltage(candidate["vbias"])
    for dev_id in input_ids:
        _scale_device_width(state, dev_id, candidate["input_width_scale"])
        _scale_device_length(state, dev_id, candidate["input_length_scale"])
    for dev_id in load_ids:
        _scale_device_width(state, dev_id, candidate.get("load_width_scale", 1.0))
        _scale_device_length(state, dev_id, candidate["load_length_scale"])
    _scale_device_width(state, tail_id, candidate["tail_width_scale"])
    _scale_device_length(state, tail_id, candidate["tail_length_scale"])
    absolute_widths = candidate.get("_absolute_widths")
    if isinstance(absolute_widths, dict):
        for dev_id, width in absolute_widths.items():
            _set_device_width(state, dev_id, float(width))
    absolute_lengths = candidate.get("_absolute_lengths")
    if isinstance(absolute_lengths, dict):
        for dev_id, length in absolute_lengths.items():
            _set_device_length(state, dev_id, float(length))


def _scale_device_width(state: DesignState, dev_id: str, scale: float) -> None:
    ts = state.transistors.get(dev_id)
    if not ts:
        return
    proc = state.process
    min_w = float(getattr(proc, "min_W", 150e-9) or 150e-9)
    max_w = float(getattr(proc, "max_W", 200e-6) or 200e-6)
    grid = float(getattr(proc, "W_precision", 1e-9) or 1e-9)
    base = max(ts.parameters.W, min_w)
    ts.parameters.W = _clip(_snap_to_grid(base * scale, grid), min_w, max_w)


def _set_device_width(state: DesignState, dev_id: str, width: float) -> None:
    ts = state.transistors.get(dev_id)
    if not ts:
        return
    proc = state.process
    min_w = float(getattr(proc, "min_W", 150e-9) or 150e-9)
    max_w = float(getattr(proc, "max_W", 200e-6) or 200e-6)
    grid = float(getattr(proc, "W_precision", 1e-9) or 1e-9)
    ts.parameters.W = _clip(_snap_to_grid(float(width), grid), min_w, max_w)


def _scale_device_length(state: DesignState, dev_id: str, scale: float) -> None:
    ts = state.transistors.get(dev_id)
    if not ts:
        return
    proc = state.process
    low, high = _device_l_range(state, dev_id)
    low = max(low, float(getattr(proc, "min_L", 130e-9) or 130e-9))
    high = min(high, float(getattr(proc, "max_L", 10e-6) or 10e-6))
    grid = float(getattr(proc, "L_precision", 1e-9) or 1e-9)
    if grid > 0.0:
        low = math.ceil(low / grid) * grid
        high = math.floor(high / grid) * grid
    if high < low:
        high = low
    base = max(ts.parameters.L, low)
    value = _clip(_snap_to_grid(base * scale, grid), low, high)
    ts.parameters.L = value
    ts.L_strategy = value


def _set_device_length(state: DesignState, dev_id: str, length: float) -> None:
    ts = state.transistors.get(dev_id)
    if not ts:
        return
    proc = state.process
    low, high = _device_l_range(state, dev_id)
    low = max(low, float(getattr(proc, "min_L", 130e-9) or 130e-9))
    high = min(high, float(getattr(proc, "max_L", 10e-6) or 10e-6))
    grid = float(getattr(proc, "L_precision", 1e-9) or 1e-9)
    if grid > 0.0:
        low = math.ceil(low / grid) * grid
        high = math.floor(high / grid) * grid
    if high < low:
        high = low
    value = _clip(_snap_to_grid(float(length), grid), low, high)
    ts.parameters.L = value
    ts.L_strategy = value


def _device_l_range(state: DesignState, dev_id: str) -> tuple[float, float]:
    for dv in state.design_variables:
        if dv.device == dev_id and dv.variable == "L":
            return float(dv.range.min), float(dv.range.max)
    return float(getattr(state.process, "min_L", 130e-9)), float(getattr(state.process, "max_L", 10e-6))


def _restore_state(
    state: DesignState,
    original_globals: dict[str, float],
    original_dims: dict[str, tuple[float, float, float]],
) -> None:
    state.global_parameters.clear()
    state.global_parameters.update(original_globals)
    for dev_id, (width, length, l_strategy) in original_dims.items():
        ts = state.transistors.get(dev_id)
        if not ts:
            continue
        ts.parameters.W = width
        ts.parameters.L = length
        ts.L_strategy = l_strategy


def _score_candidate(state: DesignState, result, candidate: dict[str, Any]) -> dict[str, Any]:
    meas = dict(result.measurements or {})
    gain = float(meas.get("dc_gain_db", -200.0))
    bw = float(meas.get("unity_gain_bandwidth", 0.0))
    pm = float(meas.get("phase_margin", 0.0))
    sr = float(meas.get("slew_rate", 0.0))
    power = float(meas.get("total_power", 0.0))
    swing = float(meas.get("output_swing", 0.0))
    targets = state.targets
    gain_min = float(targets.get("dc_gain", Target()).min or 0.0)
    bw_min = float(targets.get("unity_gain_bandwidth", Target()).min or 0.0)
    pm_min = float(targets.get("phase_margin", Target()).min or 0.0)
    sr_min = float(targets.get("slew_rate", Target()).min or 0.0)
    power_max = float(targets.get("power", Target()).max or float("inf"))
    swing_min = float(targets.get("output_swing", Target()).min or 0.0)

    op_margin, op_required_margin = _minimum_margins(state, result.operating_points or {})
    if not result.success and "dc_gain_db" not in meas:
        gain -= 200.0
    op_ok = op_required_margin >= 0.0
    static_spec_pass = (
        gain >= gain_min
        and (bw_min <= 0.0 or bw >= bw_min)
        and (pm_min <= 0.0 or pm >= pm_min)
        and (power_max == float("inf") or power <= power_max)
        and (swing_min <= 0.0 or swing >= swing_min)
    )
    dynamic_spec_pass = static_spec_pass and (sr_min <= 0.0 or ("slew_rate" in meas and sr >= sr_min))
    spec_pass = dynamic_spec_pass and op_ok
    gain_deficit = max(0.0, gain_min - gain) / max(gain_min, 1.0)
    bw_deficit = max(0.0, bw_min - bw) / max(bw_min, 1.0)
    pm_deficit = max(0.0, pm_min - pm) / max(pm_min, 1.0)
    sr_deficit = max(0.0, sr_min - sr) / max(sr_min, 1.0)
    swing_deficit = max(0.0, swing_min - swing) / max(swing_min, 1.0)
    power_deficit = (
        max(0.0, power - power_max) / max(power_max, 1e-12)
        if power_max < float("inf")
        else 0.0
    )
    hard_fail_count = sum(
        1
        for value in (gain_deficit, bw_deficit, pm_deficit, sr_deficit, swing_deficit, power_deficit)
        if value > 0.0
    )
    score = 0.0
    score += 45.0 * hard_fail_count
    score += 95.0 * gain_deficit
    score += 70.0 * bw_deficit
    score += 24.0 * phase_margin_window_penalty(pm, pm_min or 60.0)
    score += 35.0 * sr_deficit
    if power_max < float("inf"):
        score += 40.0 * power_deficit
        score += 0.15 * max(power, 0.0) / max(power_max, 1e-12)
    if swing_min > 0.0:
        score += 35.0 * swing_deficit
    if op_required_margin < 0.0:
        target_sat_margin = max(_required_saturation_margin(state, ""), 1e-3)
        op_deficit = abs(op_required_margin) / target_sat_margin
        score += 90.0 + 90.0 * op_deficit
    if bw_min > 0.0 and "unity_gain_bandwidth" not in meas:
        score += 100.0
    if pm_min > 0.0 and "phase_margin" not in meas:
        score += 100.0
    if sr_min > 0.0 and "slew_rate" not in meas:
        score += 70.0
    if spec_pass:
        score -= 250.0
    score -= 0.8 * min(max(gain - gain_min, 0.0), 20.0)
    if bw_min > 0.0:
        score -= 2.0 * min(max(bw / bw_min - 1.0, 0.0), 1.0)
    item = dict(candidate)
    item.update(
        {
            "score": score,
            "measurements": meas,
            "success": bool(result.success),
            "spec_pass": spec_pass,
            "dynamic_spec_pass": dynamic_spec_pass,
            "static_spec_pass": static_spec_pass,
            "op_ok": op_ok,
            "op_margin": op_margin,
            "op_required_margin": op_required_margin,
            "_result": result,
        }
    )
    return item


def _select_two_phase_candidate(
    state: DesignState,
    records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not records:
        return None

    targets = state.targets
    gain_min = float(targets.get("dc_gain", Target()).min or 0.0)
    bw_min = float(targets.get("unity_gain_bandwidth", Target()).min or 0.0)
    pm_min = float(targets.get("phase_margin", Target()).min or 0.0)
    power_max = float(targets.get("power", Target()).max or float("inf"))

    usable = [
        item
        for item in records
        if item.get("success", False)
        and _metric(item, "phase_margin") >= max(0.0, pm_min - 10.0)
        and (
            power_max == float("inf")
            or _metric(item, "total_power") <= max(power_max * 1.25, power_max + 1e-12)
        )
        and float(item.get("op_required_margin", -1.0)) >= 0.0
    ]
    if not usable:
        usable = [
            item
            for item in records
            if item.get("success", False)
            and float(item.get("op_required_margin", -1.0)) >= 0.0
        ]
    if not usable:
        usable = [item for item in records if item.get("success", False)] or records

    gain_anchor = max(usable, key=lambda item: _gain_merit(item, gain_min, power_max))
    gain_anchor_gain = _metric(gain_anchor, "dc_gain_db")
    best_bw = max((_metric(item, "unity_gain_bandwidth") for item in usable), default=0.0)
    bandwidth_guard_floor = _bandwidth_guard_floor(bw_min, best_bw)

    if gain_anchor_gain >= gain_min:
        gain_floor = gain_min
    elif bw_min > 0.0 and bandwidth_guard_floor > 0.0:
        # When both gain and bandwidth are below target, do not let the local
        # postprocess collapse bandwidth just to chase a few extra dB. Keep
        # enough gain to be useful, but leave room for a high-bandwidth point.
        gain_floor = max(0.0, gain_anchor_gain - 4.0)
    else:
        # When the target is not reachable in this local sweep, preserve the
        # best found gain and let later LLM rounds move the schema around it.
        gain_floor = max(0.0, gain_anchor_gain - 1.25)

    bandwidth_pool = [
        item
        for item in usable
        if _metric(item, "dc_gain_db") >= gain_floor
        and (pm_min <= 0.0 or _metric(item, "phase_margin") >= pm_min)
        and (bandwidth_guard_floor <= 0.0 or _metric(item, "unity_gain_bandwidth") >= bandwidth_guard_floor)
        and float(item.get("op_required_margin", -1.0)) >= 0.0
    ]
    if not bandwidth_pool:
        bandwidth_pool = [
            item
            for item in usable
            if _metric(item, "dc_gain_db") >= gain_floor
            and (bandwidth_guard_floor <= 0.0 or _metric(item, "unity_gain_bandwidth") >= 0.5 * bandwidth_guard_floor)
            and float(item.get("op_required_margin", -1.0)) >= 0.0
        ]
    if not bandwidth_pool:
        bandwidth_pool = [
            item
            for item in usable
            if _metric(item, "unity_gain_bandwidth") >= bandwidth_guard_floor
            and float(item.get("op_required_margin", -1.0)) >= 0.0
        ]
    if not bandwidth_pool:
        bandwidth_pool = [gain_anchor]

    selected_source = max(
        bandwidth_pool,
        key=lambda item: _bandwidth_merit(item, gain_min, bw_min, pm_min, power_max),
    )
    selected = dict(selected_source)
    bandwidth_improved = (
        selected_source is not gain_anchor
        and _metric(selected_source, "unity_gain_bandwidth") > _metric(gain_anchor, "unity_gain_bandwidth")
    )
    selected["selected_phase"] = "bandwidth_guarded_after_gain" if bandwidth_guard_floor > 0.0 and bandwidth_improved else (
        "bandwidth_after_gain" if bandwidth_improved else "gain_anchor"
    )
    selected["gain_anchor"] = _compact_candidate_summary(gain_anchor)
    selected["bandwidth_guard_floor"] = bandwidth_guard_floor
    return selected


def _validate_single_stage_dynamic_candidates(
    state: DesignState,
    sim: NgspiceSimulator,
    tune_dir: Path,
    best: dict[str, Any],
    records: list[dict[str, Any]],
    original_globals: dict[str, float],
    original_dims: dict[str, tuple[float, float, float]],
    input_ids: list[str],
    load_ids: list[str],
    tail_id: str,
    *,
    started: float,
    time_budget_sec: float,
    candidate_timeout_sec: float,
) -> dict[str, Any]:
    targets = state.targets
    gain_min = float(targets.get("dc_gain", Target()).min or 0.0)
    bw_min = float(targets.get("unity_gain_bandwidth", Target()).min or 0.0)
    pm_min = float(targets.get("phase_margin", Target()).min or 0.0)
    sr_min = float(targets.get("slew_rate", Target()).min or 0.0)
    power_max = float(targets.get("power", Target()).max or float("inf"))

    if best.get("spec_pass", False):
        return best

    pool = [
        item
        for item in records
        if _has_static_measurements(item)
        and float(item.get("op_required_margin", -1.0)) >= 0.0
        and (pm_min <= 0.0 or _metric(item, "phase_margin") >= max(55.0, pm_min - 5.0))
        and (
            power_max == float("inf")
            or _metric(item, "total_power") <= max(1.15 * power_max, power_max + 1e-12)
        )
        and (gain_min <= 0.0 or _metric(item, "dc_gain_db") >= gain_min - 1.0)
        and (bw_min <= 0.0 or _metric(item, "unity_gain_bandwidth") >= 0.90 * bw_min)
    ]
    if not pool:
        return best

    def priority(item: dict[str, Any]) -> tuple[float, ...]:
        gain = _metric(item, "dc_gain_db")
        bw = _metric(item, "unity_gain_bandwidth")
        gain_deficit = max(0.0, gain_min - gain) / max(gain_min, 1.0)
        bw_deficit = max(0.0, bw_min - bw) / max(bw_min, 1.0)
        gain_ratio = gain / max(gain_min, 1.0) if gain_min > 0.0 else gain
        bw_ratio = bw / max(bw_min, 1.0) if bw_min > 0.0 else bw
        return (
            0.0 if item.get("static_spec_pass", False) else 1.0,
            gain_deficit + bw_deficit,
            max(gain_deficit, bw_deficit),
            -min(gain_ratio, 1.25),
            -min(bw_ratio, 1.35),
            float(item.get("score", float("inf"))),
        )

    records_with_transient: list[dict[str, Any]] = []
    seen: set[tuple[float, float, float, float, float, float]] = set()
    original_timeout = getattr(sim, "timeout_sec", None)
    try:
        for item in sorted(pool, key=priority)[:8]:
            if time.time() - started > time_budget_sec:
                break
            key = (
                round(float(item.get("vbias", 0.0) or 0.0), 6),
                round(float(item.get("load_width_scale", 0.0) or 0.0), 4),
                round(float(item.get("load_length_scale", 0.0) or 0.0), 4),
                round(float(item.get("input_length_scale", 0.0) or 0.0), 4),
                round(float(item.get("input_width_scale", 0.0) or 0.0), 4),
                round(float(item.get("tail_width_scale", 0.0) or 0.0), 4),
                round(float(item.get("tail_length_scale", 0.0) or 0.0), 4),
            )
            if key in seen:
                continue
            seen.add(key)
            _restore_state(state, original_globals, original_dims)
            _apply_candidate(state, item, input_ids, load_ids, tail_id)
            trial = sim.run(generate_netlist(state), work_dir=str(tune_dir), include_transient=True)
            candidate = dict(item)
            candidate["phase"] = f"{item.get('phase', '')}+dynamic_validate"
            scored = _score_candidate(state, trial, candidate)
            scored["dynamic_validation_count"] = len(records_with_transient) + 1
            records_with_transient.append(scored)
            if scored.get("spec_pass", False):
                break
    finally:
        if original_timeout is not None:
            sim.timeout_sec = original_timeout
        _restore_state(state, original_globals, original_dims)

    if not records_with_transient:
        return best
    selected = _select_two_phase_candidate(state, records_with_transient)
    if selected is None:
        return best
    selected["dynamic_validation_count"] = len(records_with_transient)
    if sr_min > 0.0:
        selected["dynamic_validation_required"] = True
    return selected


def _refine_single_stage_gain_headroom_boundary(
    state: DesignState,
    sim: NgspiceSimulator,
    tune_dir: Path,
    best: dict[str, Any],
    records: list[dict[str, Any]],
    original_globals: dict[str, float],
    original_dims: dict[str, tuple[float, float, float]],
    input_ids: list[str],
    load_ids: list[str],
    tail_id: str,
    *,
    started: float,
    time_budget_sec: float,
) -> dict[str, Any]:
    targets = state.targets
    gain_min = float(targets.get("dc_gain", Target()).min or 0.0)
    bw_min = float(targets.get("unity_gain_bandwidth", Target()).min or 0.0)
    pm_min = float(targets.get("phase_margin", Target()).min or 0.0)
    sr_min = float(targets.get("slew_rate", Target()).min or 0.0)
    if gain_min <= 0.0 or best.get("spec_pass", False):
        return best
    best_gain = _metric(best, "dc_gain_db")
    if best_gain >= gain_min or gain_min - best_gain > 1.5:
        return best
    if bw_min > 0.0 and _metric(best, "unity_gain_bandwidth") < 0.90 * bw_min:
        return best
    if pm_min > 0.0 and _metric(best, "phase_margin") < max(55.0, pm_min - 5.0):
        return best
    if sr_min > 0.0 and _metric(best, "slew_rate") < 0.85 * sr_min:
        return best
    if float(best.get("op_required_margin", -1.0) or -1.0) < 0.0:
        return best

    gain_side = [
        item
        for item in records
        if _has_static_measurements(item)
        and _metric(item, "dc_gain_db") >= gain_min
        and float(item.get("op_required_margin", 0.0) or 0.0) < 0.0
        and (bw_min <= 0.0 or _metric(item, "unity_gain_bandwidth") >= 0.85 * bw_min)
        and (pm_min <= 0.0 or _metric(item, "phase_margin") >= max(55.0, pm_min - 5.0))
    ]
    if not gain_side:
        return best
    anchor = max(gain_side, key=lambda item: (_metric(item, "dc_gain_db"), _metric(item, "unity_gain_bandwidth")))
    anchor_vbias = float(anchor.get("vbias", 0.0) or 0.0)
    best_vbias = float(best.get("vbias", 0.0) or 0.0)
    if abs(anchor_vbias - best_vbias) < 1e-4:
        return best

    records_with_refine: list[dict[str, Any]] = []
    low = min(anchor_vbias, best_vbias)
    high = max(anchor_vbias, best_vbias)
    trial_vbias = _unique_values(
        [
            best_vbias + 0.25 * (anchor_vbias - best_vbias),
            best_vbias + 0.40 * (anchor_vbias - best_vbias),
            best_vbias + 0.55 * (anchor_vbias - best_vbias),
            best_vbias + 0.70 * (anchor_vbias - best_vbias),
            best_vbias + 0.85 * (anchor_vbias - best_vbias),
        ],
        low,
        high,
    )
    original_timeout = getattr(sim, "timeout_sec", None)
    try:
        for vbias in trial_vbias:
            if time.time() - started > time_budget_sec:
                break
            _restore_state(state, original_globals, original_dims)
            candidate = dict(anchor)
            candidate["vbias"] = vbias
            candidate["phase"] = f"{anchor.get('phase', '')}+gain_headroom_boundary"
            _apply_candidate(state, candidate, input_ids, load_ids, tail_id)
            trial = sim.run(generate_netlist(state), work_dir=str(tune_dir), include_transient=True)
            scored = _score_candidate(state, trial, candidate)
            scored["gain_headroom_refinement_count"] = len(records_with_refine) + 1
            records_with_refine.append(scored)
            if scored.get("spec_pass", False):
                break
    finally:
        if original_timeout is not None:
            sim.timeout_sec = original_timeout
        _restore_state(state, original_globals, original_dims)

    if not records_with_refine:
        return best
    selected = _select_two_phase_candidate(state, [best, *records_with_refine])
    if selected is None:
        return best
    selected["gain_headroom_refinement_count"] = len(records_with_refine)
    return selected


def _refine_current_mirror_reference_geometry(
    state: DesignState,
    sim: NgspiceSimulator,
    tune_dir: Path,
    best: dict[str, Any],
    original_globals: dict[str, float],
    original_dims: dict[str, tuple[float, float, float]],
    input_ids: list[str],
    load_ids: list[str],
    tail_id: str,
    *,
    started: float,
    time_budget_sec: float,
) -> dict[str, Any]:
    if best.get("spec_pass", False) or not _is_current_mirror_family(state):
        return best

    targets = state.targets
    gain_min = float(targets.get("dc_gain", Target()).min or 0.0)
    bw_min = float(targets.get("unity_gain_bandwidth", Target()).min or 0.0)
    pm_min = float(targets.get("phase_margin", Target()).min or 0.0)
    sr_min = float(targets.get("slew_rate", Target()).min or 0.0)
    power_max = float(targets.get("power", Target()).max or float("inf"))

    meas = best.get("measurements", {}) or {}
    gain = float(meas.get("dc_gain_db", 0.0) or 0.0)
    bw = float(meas.get("unity_gain_bandwidth", 0.0) or 0.0)
    pm = float(meas.get("phase_margin", 0.0) or 0.0)
    sr = float(meas.get("slew_rate", 0.0) or 0.0)
    op_required = float(best.get("op_required_margin", -1.0) or -1.0)
    needs_reference = (
        (gain_min > 0.0 and gain < gain_min + 0.5)
        or (bw_min > 0.0 and bw < 1.10 * bw_min)
        or (pm_min > 0.0 and pm < max(55.0, pm_min - 2.0))
        or (sr_min > 0.0 and sr < 1.05 * sr_min)
        or op_required < 0.0
    )
    if not needs_reference:
        return best

    candidates = _current_mirror_reference_points(
        state,
        best,
        input_ids,
        load_ids,
        tail_id,
        gain_min=gain_min,
        bw_min=bw_min,
    )
    if not candidates:
        return best

    records: list[dict[str, Any]] = []
    original_timeout = getattr(sim, "timeout_sec", None)
    try:
        for candidate in candidates[:9]:
            if time.time() - started > time_budget_sec:
                break
            _restore_state(state, original_globals, original_dims)
            _apply_candidate(state, candidate, input_ids, load_ids, tail_id)
            result = sim.run(generate_netlist(state), work_dir=str(tune_dir), include_transient=True)
            item = _score_candidate(state, result, candidate)
            item["current_mirror_reference_count"] = len(records) + 1
            records.append(item)
            if item.get("spec_pass", False):
                break
    finally:
        if original_timeout is not None:
            sim.timeout_sec = original_timeout
        _restore_state(state, original_globals, original_dims)

    if not records:
        return best
    selected = _select_two_phase_candidate(state, [best, *records])
    if selected is None:
        return best
    selected["current_mirror_reference_count"] = len(records)
    if power_max < float("inf"):
        selected["current_mirror_reference_power_max"] = power_max
    return selected


def _is_current_mirror_family(state: DesignState) -> bool:
    name = (state.design_name or "").lower()
    architecture = (state.topology.architecture or "").lower()
    return "current_mirror" in name or "current-mirror" in architecture or "current mirror" in architecture


def _current_mirror_reference_points(
    state: DesignState,
    best: dict[str, Any],
    input_ids: list[str],
    load_ids: list[str],
    tail_id: str,
    *,
    gain_min: float,
    bw_min: float,
) -> list[dict[str, Any]]:
    vdd = float(state.simulation.supply.get("vdd", 1.2) or 1.2)
    vbias0 = float(best.get("vbias", state.global_parameters.get("vbias", 0.5 * vdd)))
    vbias_low, vbias_high = _global_range(state, "vbias", 0.25 * vdd, 0.75 * vdd)
    speed_scale = _clip(bw_min / 30.0e6 if bw_min > 0.0 else 1.0, 0.75, 1.35)
    gain_scale = _clip(gain_min / 28.0 if gain_min > 0.0 else 1.0, 0.85, 1.25)
    old_widths = {
        dev_id: max(float(state.transistors[dev_id].parameters.W), float(getattr(state.process, "min_W", 150e-9)))
        for dev_id in [*input_ids, *load_ids, tail_id]
        if dev_id in state.transistors
    }

    def point(
        vbias: float,
        input_w: float,
        input_l: float,
        load_w: float,
        load_l: float,
        tail_w: float,
        tail_l: float,
        phase: str,
    ) -> dict[str, Any]:
        widths = {dev_id: input_w for dev_id in input_ids}
        widths.update({dev_id: load_w for dev_id in load_ids})
        widths[tail_id] = tail_w
        lengths = {dev_id: input_l for dev_id in input_ids}
        lengths.update({dev_id: load_l for dev_id in load_ids})
        lengths[tail_id] = tail_l
        return {
            "vbias": _snap_voltage(_clip(vbias, vbias_low, vbias_high)),
            "load_width_scale": 1.0,
            "load_length_scale": 1.0,
            "input_length_scale": 1.0,
            "input_width_scale": 1.0,
            "tail_width_scale": 1.0,
            "tail_length_scale": 1.0,
            "_absolute_widths": widths,
            "_absolute_lengths": lengths,
            "phase": phase,
        }

    base_input = max(max((old_widths.get(dev_id, 0.0) for dev_id in input_ids), default=0.0), 10.0e-6)
    base_load = max(max((old_widths.get(dev_id, 0.0) for dev_id in load_ids), default=0.0), 12.0e-6)
    base_tail = max(old_widths.get(tail_id, 0.0), 3.5e-6)
    l_long = 2.0e-6

    templates = [
        point(
            0.50,
            max(18.0e-6 * speed_scale, 1.10 * base_input),
            l_long,
            max(48.0e-6 * gain_scale, 2.4 * base_load),
            l_long,
            max(4.0e-6 * speed_scale, base_tail),
            0.95e-6,
            "current_mirror_reference_high_load",
        ),
        point(
            0.456,
            max(18.0e-6 * speed_scale, 1.05 * base_input),
            l_long,
            max(20.0e-6 * gain_scale, 1.6 * base_load),
            l_long,
            max(6.5e-6 * speed_scale, 1.15 * base_tail),
            1.50e-6,
            "current_mirror_reference_balanced_speed",
        ),
        point(
            0.36,
            max(17.0e-6 * speed_scale, base_input),
            l_long,
            max(54.0e-6 * gain_scale, 2.6 * base_load),
            l_long,
            max(8.5e-6 * speed_scale, 1.25 * base_tail),
            0.50e-6,
            "current_mirror_reference_low_bias_high_load",
        ),
        point(
            0.456,
            max(11.0e-6 * speed_scale, base_input),
            l_long,
            max(12.0e-6 * gain_scale, 1.2 * base_load),
            l_long,
            max(4.0e-6 * speed_scale, base_tail),
            0.50e-6,
            "current_mirror_reference_moderate_load",
        ),
        point(
            max(vbias0 - 0.04, vbias_low),
            max(16.0e-6 * speed_scale, base_input),
            l_long,
            max(36.0e-6 * gain_scale, 2.0 * base_load),
            l_long,
            max(5.2e-6 * speed_scale, base_tail),
            0.80e-6,
            "current_mirror_reference_headroom",
        ),
        point(
            min(vbias0 + 0.035, vbias_high),
            max(22.0e-6 * speed_scale, 1.15 * base_input),
            l_long,
            max(30.0e-6 * gain_scale, 1.7 * base_load),
            l_long,
            max(7.0e-6 * speed_scale, 1.20 * base_tail),
            0.90e-6,
            "current_mirror_reference_bandwidth",
        ),
    ]
    return _dedupe_points(templates)


def _global_range(state: DesignState, name: str, fallback_low: float, fallback_high: float) -> tuple[float, float]:
    for dv in state.design_variables:
        if not dv.device and dv.variable == name:
            return float(dv.range.min), float(dv.range.max)
    rng = state.constraints.global_.get(name) if getattr(state.constraints, "global_", None) else None
    if rng:
        return float(rng.min), float(rng.max)
    return fallback_low, fallback_high


def _refine_single_stage_bandwidth_boundary(
    state: DesignState,
    sim: NgspiceSimulator,
    tune_dir: Path,
    best: dict[str, Any],
    original_globals: dict[str, float],
    original_dims: dict[str, tuple[float, float, float]],
    input_ids: list[str],
    load_ids: list[str],
    tail_id: str,
    *,
    started: float,
    time_budget_sec: float,
) -> dict[str, Any]:
    targets = state.targets
    gain_min = float(targets.get("dc_gain", Target()).min or 0.0)
    bw_min = float(targets.get("unity_gain_bandwidth", Target()).min or 0.0)
    pm_min = float(targets.get("phase_margin", Target()).min or 0.0)
    sr_min = float(targets.get("slew_rate", Target()).min or 0.0)
    if bw_min <= 0.0 or best.get("spec_pass", False):
        return best

    meas = best.get("measurements", {}) or {}
    bw = float(meas.get("unity_gain_bandwidth", 0.0) or 0.0)
    gain = float(meas.get("dc_gain_db", 0.0) or 0.0)
    pm = float(meas.get("phase_margin", 0.0) or 0.0)
    sr = float(meas.get("slew_rate", 0.0) or 0.0)
    if bw >= bw_min or bw < 0.65 * bw_min:
        return best
    if gain_min > 0.0 and gain < gain_min - 0.75:
        return best
    if pm_min > 0.0 and pm < max(55.0, pm_min - 4.0):
        return best
    if sr_min > 0.0 and "slew_rate" in meas and sr < 0.75 * sr_min:
        return best
    if float(best.get("op_required_margin", -1.0) or -1.0) < 0.0:
        return best

    vdd = float(state.simulation.supply.get("vdd", 1.2) or 1.2)
    vbias0 = float(best.get("vbias", state.global_parameters.get("vbias", 0.5 * vdd)))
    low = 0.05 * vdd
    high = 0.95 * vdd
    trial_points = [
        {"vbias": _clip(vbias0 + 0.010, low, high)},
        {"vbias": _clip(vbias0 + 0.020, low, high)},
        {"vbias": _clip(vbias0 + 0.035, low, high)},
        {"tail_width_scale": float(best.get("tail_width_scale", 1.0) or 1.0) * 1.08},
        {"tail_width_scale": float(best.get("tail_width_scale", 1.0) or 1.0) * 1.16},
        {"tail_width_scale": float(best.get("tail_width_scale", 1.0) or 1.0) * 1.28},
        {"load_width_scale": float(best.get("load_width_scale", 1.0) or 1.0) * 2.0},
        {"load_width_scale": float(best.get("load_width_scale", 1.0) or 1.0) * 4.0},
        {
            "vbias": _clip(vbias0 + 0.012, low, high),
            "tail_width_scale": float(best.get("tail_width_scale", 1.0) or 1.0) * 1.08,
        },
        {
            "vbias": _clip(vbias0 + 0.020, low, high),
            "tail_width_scale": float(best.get("tail_width_scale", 1.0) or 1.0) * 1.16,
        },
        {
            "input_width_scale": float(best.get("input_width_scale", 1.0) or 1.0) * 1.08,
            "load_width_scale": float(best.get("load_width_scale", 1.0) or 1.0) * 2.0,
            "tail_width_scale": float(best.get("tail_width_scale", 1.0) or 1.0) * 1.08,
        },
    ]

    records: list[dict[str, Any]] = []
    original_timeout = getattr(sim, "timeout_sec", None)
    try:
        for point in _dedupe_points([dict(best, **p) for p in trial_points])[:11]:
            if time.time() - started > time_budget_sec:
                break
            _restore_state(state, original_globals, original_dims)
            candidate = dict(best)
            candidate.update(point)
            candidate["phase"] = f"{best.get('phase', '')}+bandwidth_boundary_refine"
            _apply_candidate(state, candidate, input_ids, load_ids, tail_id)
            trial = sim.run(generate_netlist(state), work_dir=str(tune_dir), include_transient=True)
            item = _score_candidate(state, trial, candidate)
            item["bandwidth_refinement_count"] = len(records) + 1
            records.append(item)
            if item.get("spec_pass", False):
                break
    finally:
        if original_timeout is not None:
            sim.timeout_sec = original_timeout
        _restore_state(state, original_globals, original_dims)

    selected = _select_two_phase_candidate(state, [best, *records])
    if selected is None:
        return best
    selected["bandwidth_refinement_count"] = len(records)
    return selected


def _refine_single_stage_headroom_recovery(
    state: DesignState,
    sim: NgspiceSimulator,
    tune_dir: Path,
    best: dict[str, Any],
    original_globals: dict[str, float],
    original_dims: dict[str, tuple[float, float, float]],
    input_ids: list[str],
    load_ids: list[str],
    tail_id: str,
    *,
    started: float,
    time_budget_sec: float,
) -> dict[str, Any]:
    targets = state.targets
    gain_min = float(targets.get("dc_gain", Target()).min or 0.0)
    bw_min = float(targets.get("unity_gain_bandwidth", Target()).min or 0.0)
    pm_min = float(targets.get("phase_margin", Target()).min or 0.0)
    sr_min = float(targets.get("slew_rate", Target()).min or 0.0)
    if best.get("spec_pass", False):
        return best
    if float(best.get("op_required_margin", 0.0) or 0.0) >= 0.0:
        return best

    meas = best.get("measurements", {}) or {}
    gain = float(meas.get("dc_gain_db", 0.0) or 0.0)
    bw = float(meas.get("unity_gain_bandwidth", 0.0) or 0.0)
    pm = float(meas.get("phase_margin", 0.0) or 0.0)
    sr = float(meas.get("slew_rate", 0.0) or 0.0)
    if gain_min > 0.0 and gain < gain_min - 2.5:
        return best
    if bw_min > 0.0 and bw < 0.85 * bw_min:
        return best
    if pm_min > 0.0 and pm < max(55.0, pm_min - 5.0):
        return best
    if sr_min > 0.0 and sr < 0.85 * sr_min:
        return best

    vdd = float(state.simulation.supply.get("vdd", 1.2) or 1.2)
    vbias0 = float(best.get("vbias", state.global_parameters.get("vbias", 0.5 * vdd)))
    low = 0.05 * vdd
    high = 0.95 * vdd
    tail0 = float(best.get("tail_width_scale", 1.0) or 1.0)
    load_l0 = float(best.get("load_length_scale", 1.0) or 1.0)
    input_l0 = float(best.get("input_length_scale", 1.0) or 1.0)
    trial_points = [
        {"vbias": _clip(vbias0 - 0.010, low, high)},
        {"vbias": _clip(vbias0 - 0.020, low, high)},
        {"vbias": _clip(vbias0 - 0.035, low, high)},
        {"tail_width_scale": tail0 * 0.90},
        {"tail_width_scale": tail0 * 0.80},
        {
            "vbias": _clip(vbias0 - 0.020, low, high),
            "tail_width_scale": tail0 * 0.90,
        },
        {
            "vbias": _clip(vbias0 - 0.020, low, high),
            "load_length_scale": load_l0 * 1.10,
            "input_length_scale": input_l0 * 1.08,
        },
    ]

    records: list[dict[str, Any]] = []
    original_timeout = getattr(sim, "timeout_sec", None)
    try:
        for point in _dedupe_points([dict(best, **p) for p in trial_points])[:7]:
            if time.time() - started > time_budget_sec:
                break
            _restore_state(state, original_globals, original_dims)
            candidate = dict(best)
            candidate.update(point)
            candidate["phase"] = f"{best.get('phase', '')}+headroom_recovery_refine"
            _apply_candidate(state, candidate, input_ids, load_ids, tail_id)
            trial = sim.run(generate_netlist(state), work_dir=str(tune_dir), include_transient=True)
            item = _score_candidate(state, trial, candidate)
            item["headroom_recovery_count"] = len(records) + 1
            records.append(item)
            if item.get("spec_pass", False):
                break
    finally:
        if original_timeout is not None:
            sim.timeout_sec = original_timeout
        _restore_state(state, original_globals, original_dims)

    selected = _select_two_phase_candidate(state, [best, *records])
    if selected is None:
        return best
    selected["headroom_recovery_count"] = len(records)
    return selected


def _has_static_measurements(item: dict[str, Any]) -> bool:
    meas = item.get("measurements", {}) or {}
    return bool(item.get("success", False)) or {"dc_gain_db", "unity_gain_bandwidth", "phase_margin"}.issubset(meas)


def _bandwidth_guard_floor(bw_min: float, best_bw: float) -> float:
    if bw_min <= 0.0 or best_bw <= 0.0:
        return 0.0
    return min(0.35 * bw_min, 0.65 * best_bw)


def _gain_merit(item: dict[str, Any], gain_min: float, power_max: float) -> tuple[float, float, float, float]:
    gain = _metric(item, "dc_gain_db")
    op_margin = float(item.get("op_required_margin", -1.0))
    power = _metric(item, "total_power")
    power_penalty = power / max(power_max, 1e-12) if power_max < float("inf") else 0.0
    # Saturate excess gain credit so the anchor does not choose a fragile,
    # over-biased point when several candidates already satisfy gain.
    gain_credit = min(gain, gain_min + 8.0) if gain_min > 0.0 else gain
    return (gain_credit, min(op_margin, 0.08), -power_penalty, _metric(item, "unity_gain_bandwidth"))


def _bandwidth_merit(
    item: dict[str, Any],
    gain_min: float,
    bw_min: float,
    pm_min: float,
    power_max: float,
) -> tuple[Any, ...]:
    gain = _metric(item, "dc_gain_db")
    bw = _metric(item, "unity_gain_bandwidth")
    pm = _metric(item, "phase_margin")
    power = _metric(item, "total_power")
    power_ratio = power / max(power_max, 1e-12) if power_max < float("inf") else 0.0
    spec_pass = bool(item.get("spec_pass", False))
    static_pass = bool(item.get("static_spec_pass", False))
    gain_margin = gain - gain_min if gain_min > 0.0 else gain
    pm_penalty = phase_margin_window_penalty(pm, pm_min or 60.0)
    pm_ok = pm_min <= 0.0 or pm >= pm_min
    bw_ratio = bw / max(bw_min, 1.0) if bw_min > 0.0 else bw
    gain_ratio = gain / max(gain_min, 1.0) if gain_min > 0.0 else gain
    balanced_core = min(gain_ratio, 1.25) + min(bw_ratio, 1.35)
    return (
        spec_pass,
        static_pass,
        balanced_core,
        min(bw_ratio, 2.0),
        gain_margin,
        pm_ok,
        -pm_penalty,
        -power_ratio,
    )


def _compact_candidate_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": item.get("phase", ""),
        "vbias": item.get("vbias"),
        "dc_gain_db": _metric(item, "dc_gain_db"),
        "unity_gain_bandwidth": _metric(item, "unity_gain_bandwidth"),
        "phase_margin": _metric(item, "phase_margin"),
        "total_power": _metric(item, "total_power"),
        "op_required_margin": float(item.get("op_required_margin", 0.0) or 0.0),
    }


def _metric(item: dict[str, Any], name: str) -> float:
    return float((item.get("measurements", {}) or {}).get(name, 0.0) or 0.0)


def _minimum_margins(state: DesignState, operating_points: dict[str, dict[str, float]]) -> tuple[float, float]:
    margins: list[float] = []
    required_margins: list[float] = []
    factor = _vdsat_headroom_factor(state)
    for dev in state.topology.devices:
        op = _lookup_op(operating_points, dev.id)
        if not op:
            continue
        margin = abs(float(op.get("vds", 0.0))) - factor * abs(float(op.get("vdsat", 0.0)))
        margins.append(margin)
        required_margins.append(margin - _required_saturation_margin(state, dev.role))
    return (min(margins), min(required_margins)) if margins else (-1.0, -1.0)


def _vdsat_headroom_factor(state: DesignState) -> float:
    return float(getattr(state.process, "VDSAT_headroom_factor", 1.0) or 1.0)


def _required_saturation_margin(state: DesignState, role: str) -> float:
    target = state.targets.get("saturation_margin")
    if target is not None and target.min is not None:
        return max(0.0, float(target.min))
    return 0.05


def _lookup_op(operating_points: dict[str, dict[str, float]], device_id: str) -> dict[str, float]:
    for key in (device_id, f"M{device_id}", device_id.upper(), f"M{device_id}".upper()):
        if key in operating_points:
            return operating_points[key]
    return {}


def _unique_values(values: list[float], low: float, high: float) -> list[float]:
    out: list[float] = []
    for value in values:
        value = _clip(float(value), low, high)
        if not any(abs(value - old) < 1e-6 for old in out):
            out.append(value)
    return sorted(out)


def _dedupe_points(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[float, float, float, float, float]] = set()
    out: list[dict[str, Any]] = []
    for point in points:
        key = (
            round(point["vbias"], 5),
            round(point.get("load_width_scale", 1.0), 4),
            round(point["load_length_scale"], 4),
            round(point["input_length_scale"], 4),
            round(point["input_width_scale"], 4),
            round(point["tail_width_scale"], 4),
            round(point["tail_length_scale"], 4),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(point)
    return out


def _snap_voltage(value: float) -> float:
    return round(float(value), 4)


def _snap_to_grid(value: float, precision: float) -> float:
    if precision <= 0.0:
        return value
    decimals = max(0, int(-math.floor(math.log10(precision)))) if precision > 0.0 else 12
    return round(round(value / precision) * precision, decimals)


def _clip(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)

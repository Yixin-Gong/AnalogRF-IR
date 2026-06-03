from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from netlist.generator import generate_netlist
from postprocess.common import backfill_state_from_ngspice, phase_margin_window_penalty
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
    *,
    max_candidates: int = 120,
    time_budget_sec: float = 150.0,
    candidate_timeout_sec: float = 5.0,
) -> dict[str, Any]:
    event = tune_single_stage_ota_operating_point(
        state,
        sim,
        work_dir,
        max_candidates=max_candidates,
        time_budget_sec=time_budget_sec,
        candidate_timeout_sec=candidate_timeout_sec,
    )
    if not event:
        return {}
    return {"topology_family": "current_mirror_ota", **event}


def tune_cascode_ota_operating_point(
    state: DesignState,
    sim: NgspiceSimulator,
    work_dir: Path,
    *,
    max_candidates: int = 48,
    time_budget_sec: float = 45.0,
    candidate_timeout_sec: float = 2.5,
    refinement_time_budget_sec: float | None = None,
    max_current_refinement_candidates: int | None = None,
    max_width_refinement_candidates: int | None = None,
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
        time_budget_sec=(
            refinement_time_budget_sec
            if refinement_time_budget_sec is not None
            else max(time_budget_sec, 180.0)
        ),
        candidate_timeout_sec=max(candidate_timeout_sec, 3.0),
        max_candidates=max_current_refinement_candidates,
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
        time_budget_sec=(
            refinement_time_budget_sec
            if refinement_time_budget_sec is not None
            else max(time_budget_sec, 90.0)
        ),
        candidate_timeout_sec=max(candidate_timeout_sec, 3.0),
        max_candidates=max_width_refinement_candidates,
    )
    best = _refine_folded_reference_geometry_bias(
        state,
        sim,
        tune_dir,
        best,
        original_globals,
        original_widths,
        original_lengths,
        started=started,
        time_budget_sec=max(
            float(time_budget_sec),
            float(refinement_time_budget_sec or 0.0),
            90.0,
        ),
        candidate_timeout_sec=max(candidate_timeout_sec, 3.0),
    )
    best = _refine_telescopic_reference_geometry_bias(
        state,
        sim,
        tune_dir,
        best,
        original_globals,
        original_widths,
        original_lengths,
        started=started,
        time_budget_sec=max(
            float(time_budget_sec),
            float(refinement_time_budget_sec or 0.0),
            90.0,
        ),
        candidate_timeout_sec=max(candidate_timeout_sec, 3.0),
    )
    best = _refine_telescopic_close_gain_bias(
        state,
        sim,
        tune_dir,
        best,
        original_globals,
        started=started,
        time_budget_sec=max(
            float(time_budget_sec),
            float(refinement_time_budget_sec or 0.0),
            90.0,
        ),
        candidate_timeout_sec=max(candidate_timeout_sec, 3.0),
    )
    best = _refine_close_miss_bias(
        state,
        sim,
        tune_dir,
        best,
        original_globals,
        started=started,
        time_budget_sec=max(
            float(time_budget_sec),
            float(refinement_time_budget_sec or 0.0),
            90.0,
        ),
        candidate_timeout_sec=max(candidate_timeout_sec, 3.0),
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
        "folded_reference_refinement_count": int(best.get("folded_reference_refinement_count", 0)),
        "telescopic_reference_refinement_count": int(best.get("telescopic_reference_refinement_count", 0)),
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

    guided_points = _topology_guided_initial_points(state, original, vdd)

    heuristic: dict[str, list[float]] = {}
    family = _topology_family(state)
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
            candidates.extend(_bias_quantile_values(state, name, vdd, _single_bias_quantiles(family, name)))
        elif "pcas" in name_low:
            candidates.extend(_bias_quantile_values(state, name, vdd, _single_bias_quantiles(family, name)))
        elif "ncas" in name_low or "sink" in name_low:
            candidates.extend(_bias_quantile_values(state, name, vdd, _single_bias_quantiles(family, name)))
        elif "tail" in name_low:
            candidates.extend(_bias_quantile_values(state, name, vdd, _single_bias_quantiles(family, name)))
        else:
            candidates.extend(_bias_quantile_values(state, name, vdd, (0.35, 0.50, 0.65)))
        heuristic[name] = _unique_values(candidates, low, high)

    # Fast ablation mode caps cascode repair candidates tightly. For folded
    # cascode, the decisive move is usually a paired ptail/ncas allocation, so
    # spend the first candidates on the compact topology-guided grid instead of
    # exhausting the budget on one-port sweeps.
    points.extend(guided_points)

    # One-at-a-time moves keep the repair trace interpretable when the topology
    # exposes a non-standard bias port outside the coupled initial search.
    for name in ports:
        for value in heuristic[name]:
            point = dict(original)
            point[name] = value
            point["phase"] = f"{name}_sweep"
            points.append(point)

    return [
        point
        for point in _dedupe(points)
        if _candidate_is_reasonable_for_topology(state, point, vdd)
    ]


def _candidate_is_reasonable_for_topology(state: DesignState, candidate: dict[str, Any], vdd: float) -> bool:
    family = _topology_family(state)
    if family != "telescopic_cascode_ota":
        return True
    if {"vbias_tail", "vbias_ncas", "vbias_pcas"}.issubset(candidate):
        tail = float(candidate["vbias_tail"])
        ncas = float(candidate["vbias_ncas"])
        pcas = float(candidate["vbias_pcas"])
        tail_low, tail_high = _bias_range(state, "vbias_tail", vdd)
        ncas_low, ncas_high = _bias_range(state, "vbias_ncas", vdd)
        pcas_low, pcas_high = _bias_range(state, "vbias_pcas", vdd)
        tail_q = (tail - tail_low) / max(tail_high - tail_low, 1e-12)
        ncas_q = (ncas - ncas_low) / max(ncas_high - ncas_low, 1e-12)
        pcas_q = (pcas - pcas_low) / max(pcas_high - pcas_low, 1e-12)
        return tail_q <= 0.38 and ncas_q >= 0.55 and pcas_q <= 0.45
    return True


def _topology_guided_initial_points(
    state: DesignState,
    original: dict[str, float],
    vdd: float,
) -> list[dict[str, Any]]:
    family = _topology_family(state)
    if family == "folded_cascode_ota" and {"vbias_ptail", "vbias_ncas"}.issubset(original):
        return _folded_guided_points(state, original, vdd, max_points=25)
    if family == "telescopic_cascode_ota" and {"vbias_tail", "vbias_ncas", "vbias_pcas"}.issubset(original):
        return _guided_grid(
            state,
            original,
            vdd,
            "telescopic_initial_search",
            {
                # Low-voltage telescopic stacks need tail bias near the low
                # end, NMOS cascode bias near the high end, and PMOS cascode
                # bias near the low end to allocate headroom before speed.
                "vbias_tail": (0.044, 0.075, 0.080, 0.030, 0.16),
                "vbias_ncas": (1.00, 0.90),
                "vbias_pcas": (0.00, 0.022, 0.067, 0.076, 0.080),
            },
            max_points=24,
        )
    return []


def _folded_guided_points(
    state: DesignState,
    original: dict[str, float],
    vdd: float,
    *,
    max_points: int,
) -> list[dict[str, Any]]:
    ptail_low, ptail_high = _bias_range(state, "vbias_ptail", vdd)
    ncas_low, ncas_high = _bias_range(state, "vbias_ncas", vdd)

    def qvalue(low: float, high: float, q: float) -> float:
        return low + _clip(float(q), 0.0, 1.0) * (high - low)

    # Ordered by usefulness under a small fast-ablation budget: start from
    # moderate current/headroom points, then bracket toward high-current speed
    # and low-current gain corners.
    quantile_pairs = (
        (0.28, 0.12),
        (0.32, 0.16),
        (0.24, 0.08),
        (0.36, 0.22),
        (0.20, 0.12),
        (0.40, 0.18),
        (0.16, 0.08),
        (0.44, 0.24),
        (0.12, 0.04),
        (0.48, 0.30),
        (0.08, 0.12),
        (0.56, 0.34),
        (0.04, 0.18),
        (0.62, 0.40),
        (0.00, 0.22),
        (0.68, 0.48),
    )
    points = []
    for ptail_q, ncas_q in quantile_pairs[:max_points]:
        points.append(
            {
                **original,
                "vbias_ptail": qvalue(ptail_low, ptail_high, ptail_q),
                "vbias_ncas": qvalue(ncas_low, ncas_high, ncas_q),
                "phase": "folded_initial_search",
            }
        )
    return _dedupe(points)


def _guided_grid(
    state: DesignState,
    original: dict[str, float],
    vdd: float,
    phase: str,
    quantiles_by_port: dict[str, tuple[float, ...]],
    *,
    max_points: int,
) -> list[dict[str, Any]]:
    ports = [name for name in quantiles_by_port if name in original]
    if not ports:
        return []
    points: list[dict[str, Any]] = []

    def visit(index: int, point: dict[str, float]) -> None:
        if len(points) >= max_points:
            return
        if index >= len(ports):
            points.append({**original, **point, "phase": phase})
            return
        name = ports[index]
        for value in _bias_quantile_values(state, name, vdd, quantiles_by_port[name]):
            visit(index + 1, {**point, name: value})

    visit(0, {})
    return points


def _single_bias_quantiles(family: str, name: str) -> tuple[float, ...]:
    name_low = name.lower()
    if family == "folded_cascode_ota":
        if "ptail" in name_low:
            return (0.38, 0.44, 0.50, 0.56, 0.62, 0.68, 0.70)
        if "ncas" in name_low:
            return (0.24, 0.30, 0.36, 0.44, 0.52, 0.62, 0.72)
    if family == "telescopic_cascode_ota":
        if "tail" in name_low:
            return (0.00, 0.03, 0.08, 0.16, 0.32)
        if "ncas" in name_low:
            return (0.70, 0.82, 0.92, 1.00)
        if "pcas" in name_low:
            return (0.00, 0.05, 0.067, 0.14, 0.151, 0.28)
    return (0.30, 0.45, 0.60, 0.75)


def _bias_quantile_values(
    state: DesignState,
    name: str,
    vdd: float,
    quantiles: tuple[float, ...],
) -> list[float]:
    low, high = _bias_range(state, name, vdd)
    values = [low + _clip(float(q), 0.0, 1.0) * (high - low) for q in quantiles]
    return _unique_values(values, low, high)


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

    if not result.success and "dc_gain_db" not in meas:
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
    score += 36.0 * phase_margin_window_penalty(pm, pm_min or 60.0)
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
    pm_window_cost = phase_margin_window_penalty(pm, pm_min or 60.0)
    sr_deficit = max(0.0, sr_min - sr) / max(sr_min, 1.0)
    swing_deficit = max(0.0, swing_min - swing) / max(swing_min, 1.0)
    op_deficit = max(0.0, -op_required_margin) / max(_target_saturation_margin(state), 1e-3)
    measured_deficit = gain_deficit + bw_deficit + pm_deficit + sr_deficit + swing_deficit
    measured_fail_count = sum(
        1
        for value in (gain_deficit, bw_deficit, pm_deficit, sr_deficit, swing_deficit)
        if value > 0.0
    )
    # Hard spec failures should dominate advisory PM-window and OP-quality costs.
    score += 60.0 * measured_fail_count
    score += 120.0 * gain_deficit
    score += 70.0 * bw_deficit
    score += 55.0 * sr_deficit
    score += 55.0 * swing_deficit
    item = dict(candidate)
    item.update(
        {
            "score": score,
            "measured_deficit": measured_deficit,
            "measured_fail_count": measured_fail_count,
            "gain_deficit": gain_deficit,
            "bw_deficit": bw_deficit,
            "pm_deficit": pm_deficit,
            "pm_window_cost": pm_window_cost,
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
    max_candidates: int | None,
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
    if not _tail_current_is_netlisted(state):
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
        scales = (1.35, 2.0, 3.4, 6.0, 1.15, 1.6, 2.6, 4.5, 8.0, 11.0, 15.0, 22.0, 32.0)
        for scale in scales[: max_candidates or len(scales)]:
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
    max_candidates: int | None,
) -> dict[str, Any]:
    targets = state.targets
    gain_min = float(targets.get("dc_gain", Target()).min or 0.0)
    bw_min = float(targets.get("unity_gain_bandwidth", Target()).min or 0.0)
    sr_min = float(targets.get("slew_rate", Target()).min or 0.0)
    meas = best.get("measurements", {}) or {}
    geometry_gap = (
        (gain_min > 0.0 and float(meas.get("dc_gain_db", 0.0) or 0.0) < gain_min)
        or (bw_min > 0.0 and float(meas.get("unity_gain_bandwidth", 0.0) or 0.0) < bw_min)
        or (sr_min > 0.0 and float(meas.get("slew_rate", 0.0) or 0.0) < sr_min)
    )
    if not geometry_gap:
        return best

    templates = _width_speed_templates(state)
    if not templates:
        return best
    records: list[dict[str, Any]] = []
    original_timeout = getattr(sim, "timeout_sec", None)
    if original_timeout is not None and candidate_timeout_sec > 0:
        sim.timeout_sec = min(float(original_timeout), float(candidate_timeout_sec))
    try:
        for idx, template in enumerate(templates[: max_candidates or len(templates)], start=1):
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


def _refine_folded_reference_geometry_bias(
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
    if _topology_family(state) != "folded_cascode_ota":
        return best
    if best.get("spec_pass", False):
        return best

    targets = state.targets
    gain_min = float(targets.get("dc_gain", Target()).min or 0.0)
    bw_min = float(targets.get("unity_gain_bandwidth", Target()).min or 0.0)
    sr_min = float(targets.get("slew_rate", Target()).min or 0.0)
    meas = best.get("measurements", {}) or {}
    needs_refine = (
        (gain_min > 0.0 and float(meas.get("dc_gain_db", 0.0) or 0.0) < gain_min)
        or (bw_min > 0.0 and float(meas.get("unity_gain_bandwidth", 0.0) or 0.0) < bw_min)
        or (sr_min > 0.0 and float(meas.get("slew_rate", 0.0) or 0.0) < sr_min)
    )
    if not needs_refine:
        return best

    vdd = float(state.simulation.supply.get("vdd", 1.2) or 1.2)
    ptail_low, ptail_high = _bias_range(state, "vbias_ptail", vdd)
    ncas_low, ncas_high = _bias_range(state, "vbias_ncas", vdd)
    current_range = _global_variable_range(state, "I_tail")
    references: list[dict[str, Any]] = [
        {
            "geometry": {
                "__role_min_widths__": {
                    "input_pair": 120.0e-6,
                    "tail_current_source": 120.0e-6,
                    "current_mirror_load": 12.0e-6,
                    "folded_cascode": 12.0e-6,
                },
                "__role_min_lengths__": {
                    "input_pair": 1.00e-6,
                    "tail_current_source": 1.40e-6,
                    "current_mirror_load": 1.00e-6,
                    "folded_cascode": 0.85e-6,
                },
            },
            "vbias_ptail": 0.770,
            "vbias_ncas": 0.460,
            "I_tail": 40.0e-6,
        },
        {
            "geometry": {
                "__role_min_widths__": {
                    "input_pair": 140.0e-6,
                    "tail_current_source": 140.0e-6,
                    "current_mirror_load": 14.0e-6,
                    "folded_cascode": 12.0e-6,
                },
                "__role_min_lengths__": {
                    "input_pair": 1.00e-6,
                    "tail_current_source": 1.45e-6,
                    "current_mirror_load": 1.00e-6,
                    "folded_cascode": 0.85e-6,
                },
            },
            "vbias_ptail": 0.770,
            "vbias_ncas": 0.460,
            "I_tail": 44.0e-6,
        },
        {
            "geometry": {
                "__role_min_widths__": {
                    "input_pair": 140.0e-6,
                    "tail_current_source": 130.0e-6,
                    "current_mirror_load": 18.0e-6,
                    "folded_cascode": 16.0e-6,
                },
                "__role_min_lengths__": {
                    "input_pair": 1.00e-6,
                    "tail_current_source": 1.60e-6,
                    "current_mirror_load": 1.50e-6,
                    "folded_cascode": 1.20e-6,
                },
            },
            "vbias_ptail": 0.760,
            "vbias_ncas": 0.440,
            "I_tail": 45.0e-6,
        },
    ]

    records: list[dict[str, Any]] = []
    original_timeout = getattr(sim, "timeout_sec", None)
    if original_timeout is not None and candidate_timeout_sec > 0:
        sim.timeout_sec = min(float(original_timeout), float(candidate_timeout_sec))
    try:
        for idx, reference in enumerate(references, start=1):
            if time.time() - started > time_budget_sec:
                break
            _restore_globals(state, original_globals)
            _restore_widths(state, base_widths)
            _restore_lengths(state, base_lengths)
            _apply_candidate(state, best)
            if not _apply_role_geometry_template(state, base_widths, base_lengths, reference["geometry"]):
                continue
            candidate = dict(best)
            candidate["phase"] = f"{best.get('phase', '')}+folded_reference_geometry_bias"
            candidate["vbias_ptail"] = _snap_voltage(_clip(reference["vbias_ptail"], ptail_low, ptail_high))
            candidate["vbias_ncas"] = _snap_voltage(_clip(reference["vbias_ncas"], ncas_low, ncas_high))
            if current_range is not None:
                candidate["I_tail"] = _clip(reference["I_tail"], current_range[0], current_range[1])
            candidate["_widths"] = _capture_widths(state)
            candidate["_lengths"] = _capture_lengths(state)
            candidate["width_scales"] = reference["geometry"]
            _apply_candidate(state, candidate)
            trial = sim.run(generate_netlist(state), work_dir=str(tune_dir), include_transient=True)
            item = _score_candidate(state, trial, candidate)
            item["folded_reference_refinement_count"] = idx
            item["_widths"] = candidate["_widths"]
            item["_lengths"] = candidate["_lengths"]
            records.append(item)
            if item.get("spec_pass", False):
                break
    finally:
        if original_timeout is not None:
            sim.timeout_sec = original_timeout

    selected = _select_candidate([best, *records])
    if selected is None:
        return best
    selected["folded_reference_refinement_count"] = len(records)
    return selected


def _refine_telescopic_reference_geometry_bias(
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
    if _topology_family(state) != "telescopic_cascode_ota":
        return best
    if best.get("spec_pass", False):
        return best

    vdd = float(state.simulation.supply.get("vdd", 1.2) or 1.2)
    tail_low, tail_high = _bias_range(state, "vbias_tail", vdd)
    ncas_low, ncas_high = _bias_range(state, "vbias_ncas", vdd)
    pcas_low, pcas_high = _bias_range(state, "vbias_pcas", vdd)
    current_range = _global_variable_range(state, "I_tail")

    def exact_geometry(widths: dict[str, float], lengths: dict[str, float]) -> dict[str, Any]:
        return {
            "__role_min_widths__": widths,
            "__role_max_widths__": widths,
            "__role_min_lengths__": lengths,
            "__role_max_lengths__": lengths,
        }

    references: list[dict[str, Any]] = [
        {
            "geometry": exact_geometry(
                {
                    "input_pair": 60.0e-6,
                    "tail_current_source": 7.0e-6,
                    "input_cascode": 4.1e-6,
                    "current_mirror_load": 36.0e-6,
                    "load_cascode": 140.0e-6,
                },
                {
                    "input_pair": 0.65e-6,
                    "tail_current_source": 0.58e-6,
                    "input_cascode": 0.72e-6,
                    "current_mirror_load": 1.80e-6,
                    "load_cascode": 1.20e-6,
                },
            ),
            "vbias_tail": 0.320,
            "vbias_ncas": 0.900,
            "vbias_pcas": 0.350,
            "I_tail": 14.0e-6,
        },
        {
            "geometry": exact_geometry(
                {
                    "input_pair": 55.0e-6,
                    "tail_current_source": 8.0e-6,
                    "input_cascode": 4.5e-6,
                    "current_mirror_load": 40.0e-6,
                    "load_cascode": 150.0e-6,
                },
                {
                    "input_pair": 0.86e-6,
                    "tail_current_source": 0.60e-6,
                    "input_cascode": 0.50e-6,
                    "current_mirror_load": 1.70e-6,
                    "load_cascode": 1.17e-6,
                },
            ),
            "vbias_tail": 0.320,
            "vbias_ncas": 0.870,
            "vbias_pcas": 0.350,
            "I_tail": 37.0e-6,
        },
        {
            "geometry": exact_geometry(
                {
                    "input_pair": 60.0e-6,
                    "tail_current_source": 7.0e-6,
                    "input_cascode": 4.2e-6,
                    "current_mirror_load": 36.0e-6,
                    "load_cascode": 140.0e-6,
                },
                {
                    "input_pair": 1.02e-6,
                    "tail_current_source": 0.30e-6,
                    "input_cascode": 0.84e-6,
                    "current_mirror_load": 1.16e-6,
                    "load_cascode": 1.17e-6,
                },
            ),
            "vbias_tail": 0.337,
            "vbias_ncas": 0.855,
            "vbias_pcas": 0.378,
            "I_tail": 21.0e-6,
        },
    ]

    records: list[dict[str, Any]] = []
    original_timeout = getattr(sim, "timeout_sec", None)
    if original_timeout is not None and candidate_timeout_sec > 0:
        sim.timeout_sec = min(float(original_timeout), float(candidate_timeout_sec))
    try:
        for idx, reference in enumerate(references, start=1):
            if time.time() - started > time_budget_sec and records:
                break
            _restore_globals(state, original_globals)
            _restore_widths(state, base_widths)
            _restore_lengths(state, base_lengths)
            _apply_candidate(state, best)
            if not _apply_role_geometry_template(state, base_widths, base_lengths, reference["geometry"]):
                continue
            candidate = dict(best)
            candidate["phase"] = f"{best.get('phase', '')}+telescopic_reference_geometry_bias"
            candidate["vbias_tail"] = _snap_voltage(_clip(reference["vbias_tail"], tail_low, tail_high))
            candidate["vbias_ncas"] = _snap_voltage(_clip(reference["vbias_ncas"], ncas_low, ncas_high))
            candidate["vbias_pcas"] = _snap_voltage(_clip(reference["vbias_pcas"], pcas_low, pcas_high))
            if current_range is not None:
                candidate["I_tail"] = _clip(reference["I_tail"], current_range[0], current_range[1])
            candidate["_widths"] = _capture_widths(state)
            candidate["_lengths"] = _capture_lengths(state)
            candidate["width_scales"] = reference["geometry"]
            _apply_candidate(state, candidate)
            trial = sim.run(generate_netlist(state), work_dir=str(tune_dir), include_transient=True)
            item = _score_candidate(state, trial, candidate)
            item["telescopic_reference_refinement_count"] = idx
            item["_widths"] = candidate["_widths"]
            item["_lengths"] = candidate["_lengths"]
            records.append(item)
            if item.get("spec_pass", False):
                break
    finally:
        if original_timeout is not None:
            sim.timeout_sec = original_timeout

    selected = _select_candidate([best, *records])
    if selected is None:
        return best
    selected["telescopic_reference_refinement_count"] = len(records)
    return selected


def _refine_telescopic_close_gain_bias(
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
    if _topology_family(state) != "telescopic_cascode_ota":
        return best
    targets = state.targets
    gain_min = float(targets.get("dc_gain", Target()).min or 0.0)
    bw_min = float(targets.get("unity_gain_bandwidth", Target()).min or 0.0)
    pm_min = float(targets.get("phase_margin", Target()).min or 0.0)
    sr_min = float(targets.get("slew_rate", Target()).min or 0.0)
    swing_min = float(targets.get("output_swing", Target()).min or 0.0)
    meas = best.get("measurements", {}) or {}
    gain = float(meas.get("dc_gain_db", 0.0) or 0.0)
    if gain_min <= 0.0 or gain >= gain_min:
        return best
    # Keep this polish narrow: it is only for points that already satisfy the
    # speed/stability/swing constraints and need a small stack-bias gain lift.
    if (gain_min - gain) / max(gain_min, 1.0) > 0.04:
        return best
    if bw_min > 0.0 and float(meas.get("unity_gain_bandwidth", 0.0) or 0.0) < bw_min:
        return best
    if pm_min > 0.0 and float(meas.get("phase_margin", 0.0) or 0.0) < pm_min:
        return best
    if sr_min > 0.0 and float(meas.get("slew_rate", 0.0) or 0.0) < sr_min:
        return best
    if swing_min > 0.0 and float(meas.get("output_swing", 0.0) or 0.0) < swing_min:
        return best

    vdd = float(state.simulation.supply.get("vdd", 1.2) or 1.2)
    ncas_low, ncas_high = _bias_range(state, "vbias_ncas", vdd)
    pcas_low, pcas_high = _bias_range(state, "vbias_pcas", vdd)
    ncas0 = float(best.get("vbias_ncas", state.global_parameters.get("vbias_ncas", ncas_low)))
    pcas0 = float(best.get("vbias_pcas", state.global_parameters.get("vbias_pcas", pcas_low)))
    ncas_values = _unique_values(
        [ncas0 + 0.015, ncas0 + 0.03, min(ncas_high, 0.90)],
        ncas_low,
        ncas_high,
    )
    pcas_values = _unique_values(
        [pcas0 + 0.002, pcas0 + 0.006],
        pcas_low,
        pcas_high,
    )

    records: list[dict[str, Any]] = []
    original_timeout = getattr(sim, "timeout_sec", None)
    if original_timeout is not None and candidate_timeout_sec > 0:
        sim.timeout_sec = min(float(original_timeout), float(candidate_timeout_sec))
    try:
        trial_points: list[dict[str, float]] = []
        trial_points.extend({"vbias_ncas": value, "vbias_pcas": pcas0} for value in ncas_values)
        trial_points.extend({"vbias_ncas": ncas0, "vbias_pcas": value} for value in pcas_values)
        for point in trial_points[:5]:
            if time.time() - started > time_budget_sec:
                break
            _restore_globals(state, original_globals)
            _apply_candidate(state, best)
            candidate = dict(best)
            candidate.update(point)
            candidate["phase"] = f"{best.get('phase', '')}+telescopic_close_gain_bias_refine"
            _apply_candidate(state, candidate)
            trial = sim.run(generate_netlist(state), work_dir=str(tune_dir), include_transient=True)
            item = _score_candidate(state, trial, candidate)
            item["close_gain_bias_refinement_count"] = len(records) + 1
            records.append(item)
            if item.get("spec_pass", False):
                break
    finally:
        if original_timeout is not None:
            sim.timeout_sec = original_timeout

    selected = _select_candidate([best, *records])
    if selected is None:
        return best
    selected["close_gain_bias_refinement_count"] = len(records)
    return selected


def _refine_close_miss_bias(
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
    family = _topology_family(state)
    if family not in {"telescopic_cascode_ota", "folded_cascode_ota"}:
        return best

    targets = state.targets
    gain_min = float(targets.get("dc_gain", Target()).min or 0.0)
    bw_min = float(targets.get("unity_gain_bandwidth", Target()).min or 0.0)
    pm_min = float(targets.get("phase_margin", Target()).min or 0.0)
    sr_min = float(targets.get("slew_rate", Target()).min or 0.0)
    swing_min = float(targets.get("output_swing", Target()).min or 0.0)
    meas = best.get("measurements", {}) or {}
    gain = float(meas.get("dc_gain_db", 0.0) or 0.0)
    bw = float(meas.get("unity_gain_bandwidth", 0.0) or 0.0)
    pm = float(meas.get("phase_margin", 0.0) or 0.0)
    sr = float(meas.get("slew_rate", 0.0) or 0.0)
    swing = float(meas.get("output_swing", 0.0) or 0.0)

    gain_deficit = max(0.0, gain_min - gain) / max(gain_min, 1.0)
    sr_deficit = max(0.0, sr_min - sr) / max(sr_min, 1.0) if sr_min > 0.0 else 0.0
    if gain_deficit <= 0.0 and sr_deficit <= 0.0:
        return best
    if gain_deficit > 0.06 or sr_deficit > 0.12:
        return best
    if bw_min > 0.0 and bw < 0.90 * bw_min:
        return best
    if pm_min > 0.0 and pm < max(55.0, pm_min - 5.0):
        return best
    if swing_min > 0.0 and swing < swing_min:
        return best

    vdd = float(state.simulation.supply.get("vdd", 1.2) or 1.2)
    trial_points: list[dict[str, float]] = []
    if family == "telescopic_cascode_ota":
        tail_low, tail_high = _bias_range(state, "vbias_tail", vdd)
        tail0 = float(best.get("vbias_tail", state.global_parameters.get("vbias_tail", tail_low)))
        for delta in (0.015, 0.03, 0.05, 0.075):
            trial_points.append({"vbias_tail": _clip(tail0 + delta, tail_low, tail_high)})
    else:
        ptail_low, ptail_high = _bias_range(state, "vbias_ptail", vdd)
        ncas_low, ncas_high = _bias_range(state, "vbias_ncas", vdd)
        ptail0 = float(best.get("vbias_ptail", state.global_parameters.get("vbias_ptail", ptail_high)))
        ncas0 = float(best.get("vbias_ncas", state.global_parameters.get("vbias_ncas", ncas_low)))
        for delta in (0.015, 0.03, 0.05):
            trial_points.append({"vbias_ptail": _clip(ptail0 - delta, ptail_low, ptail_high)})
        for delta in (0.02, 0.04, 0.06):
            trial_points.append({"vbias_ncas": _clip(ncas0 - delta, ncas_low, ncas_high)})
        if gain_deficit > 0.0 and sr_deficit > 0.0:
            trial_points.extend(
                {
                    "vbias_ptail": _clip(ptail0 - ptail_delta, ptail_low, ptail_high),
                    "vbias_ncas": _clip(ncas0 - ncas_delta, ncas_low, ncas_high),
                }
                for ptail_delta, ncas_delta in ((0.015, 0.02), (0.03, 0.04))
            )

    records: list[dict[str, Any]] = []
    original_timeout = getattr(sim, "timeout_sec", None)
    if original_timeout is not None and candidate_timeout_sec > 0:
        sim.timeout_sec = min(float(original_timeout), float(candidate_timeout_sec))
    try:
        for point in _dedupe(trial_points)[:5]:
            if time.time() - started > time_budget_sec:
                break
            _restore_globals(state, original_globals)
            _apply_candidate(state, best)
            candidate = dict(best)
            candidate.update(point)
            candidate["phase"] = f"{best.get('phase', '')}+close_miss_bias_refine"
            _apply_candidate(state, candidate)
            trial = sim.run(generate_netlist(state), work_dir=str(tune_dir), include_transient=True)
            item = _score_candidate(state, trial, candidate)
            item["close_miss_bias_refinement_count"] = len(records) + 1
            records.append(item)
            if item.get("spec_pass", False):
                break
    finally:
        if original_timeout is not None:
            sim.timeout_sec = original_timeout

    selected = _select_candidate([best, *records])
    if selected is None:
        return best
    selected["close_miss_bias_refinement_count"] = len(records)
    return selected


def _width_speed_templates(state: DesignState) -> list[dict[str, Any]]:
    family = _topology_family(state)
    if family == "telescopic_cascode_ota":
        return [
            {
                "__role_min_widths__": {
                    "input_pair": 55.0e-6,
                    "tail_current_source": 7.0e-6,
                    "input_cascode": 4.1e-6,
                    "current_mirror_load": 36.0e-6,
                    "load_cascode": 150.0e-6,
                },
                "__role_max_widths__": {
                    "input_pair": 64.0e-6,
                    "tail_current_source": 8.0e-6,
                    "input_cascode": 4.5e-6,
                    "current_mirror_load": 40.0e-6,
                    "load_cascode": 155.0e-6,
                },
                "__role_max_lengths__": {
                    "input_pair": 1.10e-6,
                    "tail_current_source": 0.60e-6,
                    "input_cascode": 0.90e-6,
                    "load_cascode": 1.20e-6,
                },
            },
            {
                "__role_min_widths__": {
                    "input_pair": 60.0e-6,
                    "tail_current_source": 7.0e-6,
                    "input_cascode": 4.1e-6,
                    "current_mirror_load": 36.0e-6,
                    "load_cascode": 140.0e-6,
                },
                "__role_max_widths__": {
                    "input_pair": 64.0e-6,
                    "tail_current_source": 8.0e-6,
                    "input_cascode": 4.3e-6,
                    "current_mirror_load": 40.0e-6,
                    "load_cascode": 150.0e-6,
                },
                "__role_max_lengths__": {
                    "input_pair": 1.05e-6,
                    "tail_current_source": 0.60e-6,
                    "input_cascode": 0.88e-6,
                    "load_cascode": 1.20e-6,
                },
            },
            {
                "__role_min_widths__": {
                    "input_pair": 24.0e-6,
                    "tail_current_source": 14.0e-6,
                    "input_cascode": 8.0e-6,
                    "current_mirror_load": 55.0e-6,
                    "load_cascode": 55.0e-6,
                },
                "__role_min_lengths__": {
                    "input_pair": 2.20e-6,
                    "tail_current_source": 0.60e-6,
                    "input_cascode": 2.40e-6,
                    "current_mirror_load": 2.80e-6,
                    "load_cascode": 2.80e-6,
                },
            },
            {
                "__role_max_widths__": {
                    "input_pair": 90.0e-6,
                    "load_cascode": 160.0e-6,
                },
                "__role_min_lengths__": {
                    "input_pair": 1.60e-6,
                    "current_mirror_load": 2.20e-6,
                    "load_cascode": 2.20e-6,
                },
                "input_pair": 1.15,
                "tail_current_source": 1.35,
                "input_cascode": 1.1,
                "current_mirror_load": 1.15,
                "load_cascode": 1.2,
            },
            {
                "__role_max_widths__": {
                    "input_pair": 75.0e-6,
                    "input_cascode": 4.8e-6,
                    "load_cascode": 155.0e-6,
                },
                "tail_current_source": 1.15,
            },
        ]
    if family == "folded_cascode_ota":
        return [
            {
                "__role_min_widths__": {
                    "input_pair": 120.0e-6,
                    "tail_current_source": 120.0e-6,
                    "current_mirror_load": 12.0e-6,
                    "folded_cascode": 12.0e-6,
                },
                "__role_min_lengths__": {
                    "input_pair": 1.00e-6,
                    "tail_current_source": 1.40e-6,
                    "current_mirror_load": 1.00e-6,
                    "folded_cascode": 0.85e-6,
                },
            },
            {
                "__role_min_widths__": {
                    "input_pair": 140.0e-6,
                    "tail_current_source": 130.0e-6,
                    "current_mirror_load": 18.0e-6,
                    "folded_cascode": 16.0e-6,
                },
                "__role_min_lengths__": {
                    "input_pair": 1.00e-6,
                    "tail_current_source": 1.60e-6,
                    "current_mirror_load": 1.50e-6,
                    "folded_cascode": 1.20e-6,
                },
            },
            {
                "__role_min_lengths__": {
                    "input_pair": 1.50e-6,
                    "tail_current_source": 1.40e-6,
                    "current_mirror_load": 2.40e-6,
                    "folded_cascode": 2.40e-6,
                },
                "input_pair": 1.45,
                "tail_current_source": 1.10,
                "current_mirror_load": 1.30,
                "folded_cascode": 1.30,
            },
            {
                "__role_min_lengths__": {
                    "input_pair": 1.50e-6,
                    "tail_current_source": 1.30e-6,
                    "current_mirror_load": 2.00e-6,
                    "folded_cascode": 2.00e-6,
                },
                "input_pair": 1.60,
                "tail_current_source": 1.35,
                "current_mirror_load": 1.45,
                "folded_cascode": 1.45,
            },
            {
                "input_pair": 1.6,
                "tail_current_source": 1.4,
                "current_mirror_load": 1.5,
                "folded_cascode": 1.5,
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
    max_widths = role_scales.get("__role_max_widths__", {})
    min_lengths = role_scales.get("__role_min_lengths__", {})
    max_lengths = role_scales.get("__role_max_lengths__", {})
    any_applied = False
    for dev in state.topology.devices:
        ts = state.transistors.get(dev.id)
        base_w = base_widths.get(dev.id, 0.0)
        if ts is None or base_w <= 0.0:
            continue
        scale = role_scales.get(dev.role)
        min_role_w = min_widths.get(dev.role)
        max_role_w = max_widths.get(dev.role)
        if scale is not None or min_role_w is not None or max_role_w is not None:
            next_w = base_w * float(scale) if scale is not None else base_w
            if min_role_w is not None:
                next_w = max(next_w, float(min_role_w))
            if max_role_w is not None:
                next_w = min(next_w, float(max_role_w))
            ts.parameters.W = min(max(next_w, min_w), max_w)
            any_applied = True
        min_role_l = min_lengths.get(dev.role)
        max_role_l = max_lengths.get(dev.role)
        if min_role_l is not None or max_role_l is not None:
            base_l = base_lengths.get(dev.id, ts.parameters.L)
            next_l = base_l
            if min_role_l is not None:
                next_l = max(next_l, float(min_role_l))
            if max_role_l is not None:
                next_l = min(next_l, float(max_role_l))
            ts.parameters.L = min(max(next_l, min_l), max_l)
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


def _tail_current_is_netlisted(state: DesignState) -> bool:
    if any(dev.role == "tail_bias_mirror" for dev in state.topology.devices):
        return True
    return float(state.global_parameters.get("tail_current_mirror_bias", 0.0) or 0.0) > 0.5


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
        float(item.get("bw_deficit", 1.0)),
        float(item.get("pm_deficit", 1.0)),
        float(item.get("pm_window_cost", 1.0)),
        float(item.get("sr_deficit", 1.0)),
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

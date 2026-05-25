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
    event = tune_single_stage_ota_operating_point(state, sim, work_dir)
    if not event:
        return {}
    return {"topology_family": "current_mirror_ota", **event}


def tune_cascode_ota_operating_point(
    state: DesignState,
    sim: NgspiceSimulator,
    work_dir: Path,
    *,
    max_candidates: int = 32,
    time_budget_sec: float = 55.0,
    candidate_timeout_sec: float = 5.0,
) -> dict[str, Any]:
    if not is_cascode_ota_state(state):
        return {}

    bias_ports = _bias_ports(state)
    if not bias_ports:
        return {}

    vdd = float(state.simulation.supply.get("vdd", 1.2) or 1.2)
    original_globals = dict(state.global_parameters or {})
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
    best = _select_candidate(records)
    if not best:
        return {}
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

    # One-at-a-time moves give a small, interpretable OP repair budget.
    for name in ports:
        for value in heuristic[name]:
            point = dict(original)
            point[name] = value
            point["phase"] = f"{name}_sweep"
            points.append(point)

    # A few named stack-balance presets keep telescopic/folded families from
    # depending on a full Cartesian bias grid.
    presets = [
        {"vbias_tail": 0.40 * vdd, "vbias_ncas": 0.58 * vdd, "vbias_pcas": 0.42 * vdd},
        {"vbias_tail": 0.46 * vdd, "vbias_ncas": 0.62 * vdd, "vbias_pcas": 0.48 * vdd},
        {"vbias_tail": 0.52 * vdd, "vbias_ncas": 0.68 * vdd, "vbias_pcas": 0.52 * vdd},
        {"vbias_ptail": 0.68 * vdd, "vbias_ncas": 0.44 * vdd},
        {"vbias_ptail": 0.74 * vdd, "vbias_ncas": 0.50 * vdd},
        {"vbias_ptail": 0.82 * vdd, "vbias_ncas": 0.56 * vdd},
    ]
    for preset in presets:
        point = dict(original)
        used = False
        for name, value in preset.items():
            if name not in original:
                continue
            low, high = _bias_range(state, name, vdd)
            point[name] = _snap_voltage(_clip(value, low, high))
            used = True
        if used:
            point["phase"] = "stack_balance"
            points.append(point)

    return _dedupe(points)


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


def _restore_globals(state: DesignState, original_globals: dict[str, float]) -> None:
    state.global_parameters.clear()
    state.global_parameters.update(original_globals)


def _score_candidate(state: DesignState, result, candidate: dict[str, Any]) -> dict[str, Any]:
    meas = dict(result.measurements or {})
    gain = float(meas.get("dc_gain_db", -200.0) or -200.0)
    bw = float(meas.get("unity_gain_bandwidth", 0.0) or 0.0)
    pm = float(meas.get("phase_margin", 0.0) or 0.0)
    power = float(meas.get("total_power", 0.0) or 0.0)
    swing = float(meas.get("output_swing", 0.0) or 0.0)

    targets = state.targets
    gain_min = float(targets.get("dc_gain", Target()).min or 0.0)
    bw_min = float(targets.get("unity_gain_bandwidth", Target()).min or 0.0)
    pm_min = float(targets.get("phase_margin", Target()).min or 0.0)
    power_max = float(targets.get("power", Target()).max or float("inf"))
    swing_min = float(targets.get("output_swing", Target()).min or 0.0)

    if not result.success:
        gain -= 200.0
    op_margin, op_required_margin = _minimum_margins(state, result.operating_points or {})
    spec_pass = (
        gain >= gain_min
        and (bw_min <= 0.0 or bw >= bw_min)
        and (pm_min <= 0.0 or pm >= pm_min)
        and (power_max == float("inf") or power <= power_max)
        and (swing_min <= 0.0 or swing >= swing_min)
    )
    score = 0.0
    score += 90.0 * max(0.0, gain_min - gain) / max(gain_min, 1.0)
    score += 45.0 * max(0.0, bw_min - bw) / max(bw_min, 1.0)
    score += 45.0 * max(0.0, pm_min - pm) / max(pm_min, 1.0)
    score += 35.0 * max(0.0, swing_min - swing) / max(swing_min, 1.0)
    if power_max < float("inf"):
        score += 25.0 * max(0.0, power - power_max) / max(power_max, 1e-12)
    if op_required_margin < 0.0:
        score += 220.0 * abs(op_required_margin)
    if spec_pass and op_required_margin >= -0.02:
        score -= 250.0
    item = dict(candidate)
    item.update(
        {
            "score": score,
            "measurements": meas,
            "success": bool(result.success),
            "spec_pass": spec_pass,
            "op_ok": op_required_margin >= -0.02,
            "op_margin": op_margin,
            "op_required_margin": op_required_margin,
            "_result": result,
        }
    )
    return item


def _select_candidate(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None
    usable = [item for item in records if item.get("success", False)] or records
    return min(usable, key=lambda item: float(item.get("score", float("inf"))))


def _minimum_margins(state: DesignState, operating_points: dict[str, dict[str, float]]) -> tuple[float, float]:
    margins: list[float] = []
    required_margins: list[float] = []
    for dev in state.topology.devices:
        op = _lookup_op(operating_points, dev.id)
        if not op:
            continue
        margin = abs(float(op.get("vds", 0.0))) - abs(float(op.get("vdsat", 0.0)))
        margins.append(margin)
        required_margins.append(margin - _required_saturation_margin(dev.role))
    return (min(margins), min(required_margins)) if margins else (-1.0, -1.0)


def _required_saturation_margin(role: str) -> float:
    role_l = (role or "").lower()
    if "cascode" in role_l or "folded" in role_l:
        return 0.06
    if role_l == "input_pair":
        return 0.06
    if "tail" in role_l:
        return 0.05
    return 0.04


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

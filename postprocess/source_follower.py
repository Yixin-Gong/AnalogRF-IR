from __future__ import annotations

import time
import math
from pathlib import Path
from typing import Any

from netlist.generator import generate_netlist
from schemas.design_state import DesignState
from simulator.ngspice import NgspiceSimulator


def has_source_follower_regulation(state: DesignState) -> bool:
    return any(
        "source_follower" in dev.role.lower() or "follower" in dev.role.lower()
        for dev in state.topology.devices
    )


def tune_source_follower_operating_point(
    state: DesignState,
    sim: NgspiceSimulator,
    work_dir: Path,
    *,
    time_budget_sec: float = 90.0,
) -> dict[str, Any]:
    if not has_source_follower_regulation(state):
        return {}
    bias_ports = {port.id for port in state.topology.ports if port.direction == "bias"}
    if not {"vbias_p", "vbias_reg"}.issubset(bias_ports):
        return {}

    vdd = float(state.simulation.supply.get("vdd", 1.2))
    old_vbias_p = float(state.global_parameters.get("vbias_p", 0.70 * vdd))
    old_vbias_reg = float(state.global_parameters.get("vbias_reg", 0.45 * vdd))
    p_candidates = _candidate_values(
        [
            0.65 * vdd,
            0.70 * vdd,
            0.72 * vdd,
            0.73 * vdd,
            0.77 * vdd,
            0.80,
            0.81,
            0.82,
            0.86,
            0.88,
            0.90,
            0.83 * vdd,
            0.87 * vdd,
            old_vbias_p,
        ],
        0.0,
        vdd,
    )
    reg_candidates = _candidate_values(
        [
            0.33 * vdd,
            0.38 * vdd,
            0.42 * vdd,
            0.46 * vdd,
            0.47,
            0.49,
            0.50,
            0.50 * vdd,
            0.52,
            0.55,
            0.60,
            old_vbias_reg,
        ],
        0.0,
        vdd,
    )

    started = time.time()
    best: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = []
    tried = 0
    original = dict(state.global_parameters)
    repair_device_ids = _repair_width_device_ids(state)
    original_widths = {
        dev_id: state.transistors[dev_id].parameters.W
        for dev_id in repair_device_ids
        if dev_id in state.transistors
    }
    candidate_points = _candidate_points(p_candidates, reg_candidates, repair_device_ids)
    for vbias_p, vbias_reg, width_scale in candidate_points:
        if time.time() - started > time_budget_sec:
            break
        _apply_width_scale(state, original_widths, width_scale)
        state.global_parameters["vbias_p"] = vbias_p
        state.global_parameters["vbias_reg"] = vbias_reg
        result = sim.run(generate_netlist(state), work_dir=str(work_dir / "source_follower_op_tune"))
        tried += 1
        item = _candidate_result(state, result, vbias_p, vbias_reg, width_scale)
        candidates.append(item)
        if best is None or item["score"] > best["score"]:
            best = item

    _apply_width_scale(state, original_widths, 1.0)
    state.global_parameters.clear()
    state.global_parameters.update(original)
    if not best:
        return {}

    full_pass = [
        item
        for item in candidates
        if item["gain_ok"] and item["bandwidth_ok"] and item["pm_ok"] and item["op_ok"]
    ]
    if full_pass:
        best = max(
            full_pass,
            key=lambda item: (
                min(item["op_required_margin"], 0.05),
                item["unity_gain_bandwidth"],
                item["phase_margin"],
                item["dc_gain_db"],
            ),
        )
    else:
        stable = [item for item in candidates if item["gain_ok"] and item["pm_ok"] and item["op_ok"]]
        if stable:
            best = max(
                stable,
                key=lambda item: (
                    item["unity_gain_bandwidth"],
                    min(item["op_required_margin"], 0.05),
                    item["dc_gain_db"],
                    item["output_swing"],
                ),
            )

    state.global_parameters["vbias_p"] = best["vbias_p"]
    state.global_parameters["vbias_reg"] = best["vbias_reg"]
    _apply_width_scale(state, original_widths, best["width_scale"])
    return {
        "old_vbias_p": old_vbias_p,
        "old_vbias_reg": old_vbias_reg,
        "new_vbias_p": best["vbias_p"],
        "new_vbias_reg": best["vbias_reg"],
        "width_scale": best["width_scale"],
        "repair_devices": sorted(original_widths),
        "dc_gain_db": best["dc_gain_db"],
        "unity_gain_bandwidth": best["unity_gain_bandwidth"],
        "phase_margin": best["phase_margin"],
        "op_margin": best["op_margin"],
        "op_required_margin": best["op_required_margin"],
        "output_swing": best["output_swing"],
        "candidate_count": tried,
    }


def _candidate_values(values: list[float], low: float, high: float) -> list[float]:
    return sorted({round(min(max(value, low), high), 4) for value in values})


def _candidate_points(
    p_candidates: list[float],
    reg_candidates: list[float],
    repair_device_ids: list[str],
) -> list[tuple[float, float, float]]:
    if not repair_device_ids:
        return [(vbias_p, vbias_reg, 1.0) for vbias_p in p_candidates for vbias_reg in reg_candidates]

    seed_points = [
        (0.81, 0.47, 2.50),
        (0.80, 0.49, 2.25),
        (0.80, 0.50, 2.00),
        (0.81, 0.50, 1.75),
        (0.82, 0.50, 1.50),
        (0.82, 0.54, 1.00),
        (0.84, 0.504, 1.00),
        (0.82, 0.55, 1.00),
    ]
    focused = [
        (vbias_p, vbias_reg, width_scale)
        for width_scale in (2.00, 2.25, 2.50)
        for vbias_p in (0.80, 0.81, 0.82)
        for vbias_reg in (0.47, 0.49, 0.50)
    ]
    fallback = [(vbias_p, vbias_reg, 1.0) for vbias_p in p_candidates for vbias_reg in reg_candidates]
    return _dedupe_points(seed_points + focused + fallback)


def _dedupe_points(points: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    seen: set[tuple[float, float, float]] = set()
    out = []
    for vbias_p, vbias_reg, width_scale in points:
        key = (round(vbias_p, 4), round(vbias_reg, 4), round(width_scale, 4))
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _repair_width_device_ids(state: DesignState) -> list[str]:
    return [
        dev.id
        for dev in state.topology.devices
        if dev.role == "regulated_source_current_source" and dev.id in state.transistors
    ]


def _apply_width_scale(state: DesignState, original_widths: dict[str, float], width_scale: float) -> None:
    for dev_id, width in original_widths.items():
        state.transistors[dev_id].parameters.W = _snap_width_to_grid(state, width * width_scale)


def _snap_width_to_grid(state: DesignState, width: float) -> float:
    grid = float(getattr(state.process, "W_precision", 0.0) or 0.0)
    min_width = float(getattr(state.process, "min_W", 0.0) or 0.0)
    width = max(width, min_width)
    if grid <= 0.0:
        return width
    decimals = max(0, int(-math.floor(math.log10(grid)))) if grid > 0.0 else 12
    return round(round(width / grid) * grid, decimals)


def _candidate_result(
    state: DesignState,
    result,
    vbias_p: float,
    vbias_reg: float,
    width_scale: float,
) -> dict[str, Any]:
    measurements = result.measurements or {}
    gain = float(measurements.get("dc_gain_db", -200.0))
    ugbw = float(measurements.get("unity_gain_bandwidth", 0.0))
    pm = float(measurements.get("phase_margin", 0.0))
    swing = float(measurements.get("output_swing", 0.0))
    op_margin, op_required_margin = _minimum_margins(state, result.operating_points or {})
    if not result.success:
        gain -= 200.0
    gain_target = float(state.targets.get("dc_gain").min if state.targets.get("dc_gain") else 50.0)
    ugbw_target = float(
        state.targets.get("unity_gain_bandwidth").min
        if state.targets.get("unity_gain_bandwidth")
        else 0.0
    )
    pm_target = float(state.targets.get("phase_margin").min if state.targets.get("phase_margin") else 0.0)
    gain_ok = gain >= gain_target
    bandwidth_ok = ugbw >= ugbw_target if ugbw_target > 0 else True
    pm_ok = pm >= pm_target
    op_ok = op_required_margin >= 0.0
    gain_score = min(gain, gain_target + 10.0)
    ugbw_ratio = min(ugbw / max(ugbw_target, 1.0), 1.5) if ugbw_target > 0 else 0.0
    pm_score = min(pm / max(pm_target, 1.0), 1.5) if pm_target > 0 else 0.0
    score = (
        gain_score
        + 18.0 * ugbw_ratio
        + 30.0 * pm_score
        + 80.0 * min(max(op_required_margin, -0.2), 0.3)
        + 8.0 * min(max(swing, 0.0), 1.2)
    )
    if gain_ok and bandwidth_ok and pm_ok and op_ok:
        score += 260.0 + 40.0 * min(max(op_required_margin, 0.0), 0.05)
    elif gain_ok and pm_ok and op_ok:
        score += 200.0 + 40.0 * ugbw_ratio
    elif gain_ok and op_ok:
        score += 40.0 * pm_score
    if gain < gain_target:
        score -= 2.0 * (gain_target - gain)
    if pm_target > 0 and pm < pm_target:
        score -= 120.0 * (1.0 - pm / max(pm_target, 1.0))
    if ugbw_target > 0 and ugbw < ugbw_target:
        score -= 8.0 * (1.0 - ugbw / max(ugbw_target, 1.0))
    if op_required_margin < 0.0:
        score -= 140.0 * abs(op_required_margin)
    return {
        "vbias_p": vbias_p,
        "vbias_reg": vbias_reg,
        "width_scale": width_scale,
        "dc_gain_db": gain,
        "unity_gain_bandwidth": ugbw,
        "phase_margin": pm,
        "output_swing": swing,
        "op_margin": op_margin,
        "op_required_margin": op_required_margin,
        "gain_ok": gain_ok,
        "bandwidth_ok": bandwidth_ok,
        "pm_ok": pm_ok,
        "op_ok": op_ok,
        "score": score,
    }


def _minimum_margins(state: DesignState, operating_points: dict[str, dict[str, float]]) -> tuple[float, float]:
    margins = []
    required_margins = []
    for dev in state.topology.devices:
        op = _lookup_op(operating_points, dev.id)
        if not op:
            continue
        margin = float(op.get("vds", 0.0)) - float(op.get("vdsat", 0.0))
        margins.append(margin)
        required_margins.append(margin - _required_saturation_margin(dev.role))
    return (min(margins), min(required_margins)) if margins else (-1.0, -1.0)


def _required_saturation_margin(role: str) -> float:
    return {
        "input_pair": 0.08,
        "cascode": 0.20,
        "tail_current_source": 0.05,
        "tail_bias_mirror": 0.03,
        "current_mirror_load": 0.03,
        "current_mirror_load_regulated_output": 0.03,
        "source_follower_regulator": 0.05,
        "regulated_source_current_source": 0.05,
        "second_stage_gain": 0.05,
        "second_stage_load": 0.05,
        "output_current_source": 0.05,
        "output_bias_mirror": 0.03,
    }.get(role, 0.05)


def _lookup_op(operating_points: dict[str, dict[str, float]], device_id: str) -> dict[str, float]:
    for key in (device_id, f"M{device_id}", device_id.upper(), f"M{device_id}".upper()):
        if key in operating_points:
            return operating_points[key]
    return {}

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from core.compensation import has_miller_rc_compensation
from netlist.generator import generate_netlist
from postprocess.common import backfill_state_from_ngspice, phase_margin_window_penalty
from schemas.design_state import DesignState, Target
from simulator.ngspice import NgspiceSimulator


def is_two_stage_state(state: DesignState) -> bool:
    arch = (state.topology.architecture or "").lower()
    return "two" in arch or any(dev.role == "second_stage_gain" for dev in state.topology.devices)


def get_stage2_device_ids(state: DesignState) -> tuple[str | None, str | None]:
    gain_id = None
    sink_id = None
    for dev in state.topology.devices:
        if dev.role == "second_stage_gain":
            gain_id = dev.id
        elif dev.role in ("second_stage_load", "output_current_source"):
            sink_id = dev.id
    return gain_id, sink_id


def stage2_vout_from_result(state: DesignState, result) -> float | None:
    _, sink_id = get_stage2_device_ids(state)
    if not sink_id:
        return None
    for name in (f"M{sink_id}".upper(), sink_id.upper()):
        op = result.operating_points.get(name)
        if op and "vds" in op:
            return abs(float(op["vds"]))
    return None


def op_for_device(result, device_id: str) -> dict:
    for name in (f"M{device_id}".upper(), device_id.upper()):
        op = result.operating_points.get(name)
        if op:
            return op
    return {}


def symmetric_device_ids(state: DesignState, device_id: str) -> list[str]:
    labels = {
        dv.symmetry_label
        for dv in state.design_variables
        if dv.device == device_id and dv.symmetry_label
    }
    if not labels:
        return [device_id]
    peers: list[str] = []
    for dv in state.design_variables:
        if dv.device and dv.symmetry_label in labels and dv.device not in peers:
            peers.append(dv.device)
    return peers or [device_id]


def set_symmetric_width(state: DesignState, device_id: str, width: float) -> None:
    for peer_id in symmetric_device_ids(state, device_id):
        if peer_id in state.transistors:
            state.transistors[peer_id].parameters.W = width


def hard_reliability_errors(state: DesignState, result) -> list[str]:
    max_vgs = float(getattr(state.process, "max_VGS", 0.0) or 0.0)
    if max_vgs <= 0.0:
        return []
    limit = 0.95 * max_vgs
    errors = []
    for raw_name, op in (result.operating_points or {}).items():
        try:
            vgs = abs(float(op.get("vgs", 0.0) or 0.0))
        except (TypeError, ValueError):
            continue
        if vgs > limit:
            name = str(raw_name).upper()
            if not name.startswith("M"):
                name = f"M{name}"
            errors.append(f"{name}: |VGS|={vgs:.3f}V >= {limit:.3f}V")
    return errors


def confirm_initial_operating_point(
    state: DesignState,
    sim: NgspiceSimulator,
    work_dir: Path,
) -> dict:
    if not is_two_stage_state(state):
        return {}
    result = sim.run(generate_netlist(state), work_dir=str(work_dir), include_transient=False)
    if result.operating_points:
        backfill_state_from_ngspice(state, result)
    region_counts: dict[str, int] = {}
    for ts in state.transistors.values():
        region = ts.parameters.region or "unknown"
        region_counts[region] = region_counts.get(region, 0) + 1
    return {
        "success": bool(result.success),
        "return_code": result.return_code,
        "operating_point_count": len(result.operating_points or {}),
        "region_counts": region_counts,
        "measurements": dict(result.measurements or {}),
    }


def balance_two_stage_output(
    state: DesignState,
    sim: NgspiceSimulator,
    work_dir: Path,
    max_iter: int = 9,
) -> dict:
    if not is_two_stage_state(state):
        return {}
    gain_id, sink_id = get_stage2_device_ids(state)
    if not gain_id or not sink_id:
        return {}
    gain_ts = state.transistors.get(gain_id)
    sink_ts = state.transistors.get(sink_id)
    if not gain_ts or gain_ts.parameters.W <= 0:
        return {}

    vdd = state.simulation.supply.get("vdd", 1.2)
    target = 0.5 * vdd
    base_w = gain_ts.parameters.W
    proc = state.process
    min_w = getattr(proc, "min_W", 150e-9)
    max_w = getattr(proc, "max_W", 200e-6)
    w_grid = getattr(proc, "W_precision", 10e-9)
    base_sink_w = sink_ts.parameters.W if sink_ts and sink_ts.parameters.W > 0 else None
    best = {"scale": 1.0, "sink_scale": 1.0, "vout": None, "error": float("inf")}

    def _clip_width(width: float) -> float:
        return min(max(_snap_to_grid(width, w_grid), min_w), max_w)

    def evaluate(scale: float, sink_scale: float = 1.0) -> float | None:
        gain_ts.parameters.W = _clip_width(base_w * scale)
        if sink_ts and base_sink_w:
            set_symmetric_width(state, sink_id, _clip_width(base_sink_w * sink_scale))
        trial = sim.run(generate_netlist(state), work_dir=str(work_dir), include_transient=False)
        if hard_reliability_errors(state, trial):
            return None
        vout = stage2_vout_from_result(state, trial)
        if vout is None:
            return None
        err = abs(vout - target)
        if err < best["error"]:
            best.update({"scale": scale, "sink_scale": sink_scale, "vout": vout, "error": err})
        return vout

    base_vout = evaluate(1.0, 1.0)
    if base_vout is None:
        gain_ts.parameters.W = base_w
        if sink_ts and base_sink_w:
            set_symmetric_width(state, sink_id, base_sink_w)
        return {}
    if 0.25 * vdd <= base_vout <= 0.75 * vdd:
        gain_ts.parameters.W = base_w
        if sink_ts and base_sink_w:
            set_symmetric_width(state, sink_id, base_sink_w)
        best.update({"scale": 1.0, "sink_scale": 1.0, "vout": base_vout, "error": abs(base_vout - target)})
        return best

    lo = max(0.25, min_w / base_w) if base_w > 0 else 0.25
    hi = min(16.0, max_w / base_w) if base_w > 0 else 16.0
    if hi <= lo:
        gain_ts.parameters.W = _clip_width(base_w)
        return {}
    vlo = evaluate(lo)
    vhi = evaluate(hi)
    if vlo is None or vhi is None:
        gain_ts.parameters.W = base_w
        if sink_ts and base_sink_w:
            set_symmetric_width(state, sink_id, base_sink_w)
        return {}

    if min(vlo, vhi) <= target <= max(vlo, vhi):
        increasing = vhi > vlo
        for _ in range(max_iter):
            mid = math.sqrt(lo * hi)
            vmid = evaluate(mid)
            if vmid is None:
                break
            if increasing:
                if vmid < target:
                    lo = mid
                else:
                    hi = mid
            else:
                if vmid < target:
                    hi = mid
                else:
                    lo = mid
    elif sink_ts and base_sink_w:
        if best["vout"] is not None and best["vout"] < target:
            gain_scale = hi
            sink_candidates = [1.0, 0.75, 0.5, 0.33, 0.2, 0.125, 0.08]
        else:
            gain_scale = lo
            sink_hi = min(16.0, max_w / base_sink_w) if base_sink_w > 0 else 16.0
            sink_candidates = [1.0, 1.5, 2.0, 3.0, 5.0, 8.0, sink_hi]
        for sink_scale in sink_candidates:
            if sink_scale > 0:
                evaluate(gain_scale, sink_scale)

    gain_ts.parameters.W = _clip_width(base_w * best["scale"])
    if sink_ts and base_sink_w:
        set_symmetric_width(state, sink_id, _clip_width(base_sink_w * best["sink_scale"]))
    return best


def improve_tail_headroom(
    state: DesignState,
    sim: NgspiceSimulator,
    work_dir: Path,
    required_margin: float = 0.05,
) -> dict:
    input_ids = [dev.id for dev in state.topology.devices if dev.role == "input_pair"]
    tail_id = next((dev.id for dev in state.topology.devices if dev.role == "tail_current_source"), None)
    if len(input_ids) < 2 or not tail_id:
        return {}

    max_w = getattr(state.process, "max_W", 200e-6)
    min_w = getattr(state.process, "min_W", 150e-9)
    w_grid = getattr(state.process, "W_precision", 10e-9)
    base_widths = {
        dev_id: state.transistors[dev_id].parameters.W
        for dev_id in input_ids
        if dev_id in state.transistors and state.transistors[dev_id].parameters.W > 0
    }
    if len(base_widths) != len(input_ids):
        return {}

    best = {"scale": 1.0, "margin": float("-inf"), "vds": None, "vdsat": None}
    chosen = None
    def clip_width(width: float) -> float:
        return min(max(_snap_to_grid(width, w_grid), min_w), max_w)

    for scale in (1.0, 1.2, 1.5, 2.0, 2.5, 3.0):
        for dev_id, base_w in base_widths.items():
            state.transistors[dev_id].parameters.W = clip_width(base_w * scale)
        trial = sim.run(generate_netlist(state), work_dir=str(work_dir), include_transient=False)
        op = op_for_device(trial, tail_id)
        if not op:
            continue
        vds = abs(float(op.get("vds", 0.0)))
        vdsat = abs(float(op.get("vdsat", 0.0)))
        margin = vds - vdsat
        if margin > best["margin"]:
            best.update({"scale": scale, "margin": margin, "vds": vds, "vdsat": vdsat})
        if margin >= required_margin:
            chosen = {"scale": scale, "margin": margin, "vds": vds, "vdsat": vdsat}
            break
    if chosen is None:
        chosen = best if best["margin"] > float("-inf") else None
    if not chosen or chosen["scale"] <= 1.0:
        for dev_id, base_w in base_widths.items():
            state.transistors[dev_id].parameters.W = base_w
        return {}
    for dev_id, base_w in base_widths.items():
        state.transistors[dev_id].parameters.W = clip_width(base_w * chosen["scale"])
    return chosen


def tune_two_stage_compensation(
    state: DesignState,
    sim: NgspiceSimulator,
    work_dir: Path,
    *,
    max_base_candidates: int = 24,
    max_refine_candidates: int = 8,
    max_load_candidates: int = 8,
    max_current_candidates: int = 16,
    max_gain_candidates: int = 16,
    time_budget_sec: float = 160.0,
    candidate_timeout_sec: float = 12.0,
) -> dict:
    if not is_two_stage_state(state) or not has_miller_rc_compensation(state):
        return {}
    cc_range = _design_var_range(state, "Cc")
    rz_range = _design_var_range(state, "Rz")
    if not cc_range or not rz_range:
        return {}

    cc_low, cc_high = cc_range
    rz_low, rz_high = rz_range
    current_cc = state.global_parameters.get("Cc", 0.5 * (cc_low + cc_high))
    current_rz = state.global_parameters.get("Rz", 0.5 * (rz_low + rz_high))

    gain_id, _ = get_stage2_device_ids(state)
    gm6 = state.transistors.get(gain_id).parameters.gm if gain_id in state.transistors else 0.0
    rz0 = 1.0 / gm6 if gm6 > 1e-12 else current_rz

    targets = state.targets
    gain_min = targets.get("dc_gain", Target()).min or 0.0
    bw_target = targets.get("unity_gain_bandwidth", Target())
    bw_min = bw_target.min or 0.0
    bw_max = bw_target.max or float("inf")
    pm_min = targets.get("phase_margin", Target()).min or 0.0
    sr_min = targets.get("slew_rate", Target()).min or 0.0
    power_max = targets.get("power", Target()).max or float("inf")
    swing_min = targets.get("output_swing", Target()).min or 0.0
    icmr_min_max = targets.get("icmr_min", Target()).max
    icmr_max_min = targets.get("icmr_max", Target()).min

    load_ids = [dev.id for dev in state.topology.devices if dev.role == "current_mirror_load"]
    load_dims = {
        dev_id: (
            state.transistors[dev_id].parameters.W,
            state.transistors[dev_id].parameters.L,
            state.transistors[dev_id].L_strategy,
        )
        for dev_id in load_ids
        if dev_id in state.transistors
    }
    input_pair_ids = [dev.id for dev in state.topology.devices if dev.role == "input_pair"]
    input_pair_dims = {
        dev_id: (
            state.transistors[dev_id].parameters.W,
            state.transistors[dev_id].parameters.L,
            state.transistors[dev_id].L_strategy,
        )
        for dev_id in input_pair_ids
        if dev_id in state.transistors
    }
    gain_refine_ids = [
        dev.id
        for dev in state.topology.devices
        if dev.role in {"current_mirror_load", "second_stage_gain", "output_current_source"}
    ]
    gain_refine_dims = {
        dev_id: (
            state.transistors[dev_id].parameters.W,
            state.transistors[dev_id].parameters.L,
            state.transistors[dev_id].L_strategy,
        )
        for dev_id in gain_refine_ids
        if dev_id in state.transistors
    }
    base_currents = {
        "I_tail": float(state.global_parameters.get("I_tail", 0.0) or 0.0),
        "I_stage2": float(state.global_parameters.get("I_stage2", 0.0) or 0.0),
    }
    current_ranges = {
        name: _design_var_range(state, name)
        for name in base_currents
        if base_currents[name] > 0.0 and _design_var_range(state, name)
    }
    proc = state.process
    min_w = getattr(proc, "min_W", 150e-9)
    max_w = getattr(proc, "max_W", 200e-6)
    min_l = getattr(proc, "min_L", 130e-9)
    max_l = getattr(proc, "max_L", 10e-6)
    w_grid = getattr(proc, "W_precision", 10e-9)
    l_grid = getattr(proc, "L_precision", 1e-9)

    def apply_load_scale(scale: float) -> None:
        for dev_id, (base_w, base_l, base_strategy_l) in load_dims.items():
            ts = state.transistors[dev_id]
            ts.parameters.W = min(max(_snap_to_grid(base_w * scale, w_grid), min_w), max_w)
            scaled_l = min(max(_snap_to_grid(base_l * scale, l_grid), min_l), max_l)
            ts.parameters.L = scaled_l
            ts.L_strategy = scaled_l if base_strategy_l > 0 else base_strategy_l

    def apply_input_pair_geometry(width_scale: float, length_scale: float) -> None:
        for dev_id, (base_w, base_l, base_strategy_l) in input_pair_dims.items():
            ts = state.transistors[dev_id]
            ts.parameters.W = min(max(_snap_to_grid(base_w * width_scale, w_grid), min_w), max_w)
            scaled_l = min(max(_snap_to_grid(base_l * length_scale, l_grid), min_l), max_l)
            ts.parameters.L = scaled_l
            ts.L_strategy = scaled_l if base_strategy_l > 0 else base_strategy_l

    def apply_gain_refine_geometry(width_scale: float, length_scale: float) -> None:
        for dev_id, (base_w, base_l, base_strategy_l) in gain_refine_dims.items():
            ts = state.transistors[dev_id]
            ts.parameters.W = min(max(_snap_to_grid(base_w * width_scale, w_grid), min_w), max_w)
            scaled_l = min(max(_snap_to_grid(base_l * length_scale, l_grid), min_l), max_l)
            ts.parameters.L = scaled_l
            ts.L_strategy = scaled_l if base_strategy_l > 0 else base_strategy_l

    def apply_current_scale(tail_scale: float, stage2_scale: float) -> None:
        scale_by_name = {"I_tail": tail_scale, "I_stage2": stage2_scale}
        for name, base_value in base_currents.items():
            if base_value <= 0.0 or name not in current_ranges:
                continue
            low, high = current_ranges[name]
            state.global_parameters[name] = min(max(base_value * scale_by_name[name], low), high)

    def spec_pass(meas: dict) -> bool:
        if meas.get("dc_gain_db", 0.0) < gain_min:
            return False
        if meas.get("unity_gain_bandwidth", 0.0) < bw_min:
            return False
        if meas.get("unity_gain_bandwidth", 0.0) > bw_max:
            return False
        if meas.get("phase_margin", 0.0) < pm_min:
            return False
        if power_max < float("inf") and "total_power" not in meas:
            return False
        if meas.get("total_power", 0.0) > power_max:
            return False
        if swing_min and ("output_swing" not in meas or meas.get("output_swing", 0.0) < swing_min):
            return False
        if icmr_min_max is not None and ("icmr_min" not in meas or meas.get("icmr_min", float("inf")) > icmr_min_max):
            return False
        if icmr_max_min is not None and ("icmr_max" not in meas or meas.get("icmr_max", 0.0) < icmr_max_min):
            return False
        return True

    def robust_spec_pass(meas: dict) -> bool:
        if not spec_pass(meas):
            return False
        pm = meas.get("phase_margin", 0.0)
        return pm_min <= 0.0 or (pm >= pm_min and pm <= 70.0)

    def score_measurements(meas: dict) -> float:
        gain = meas.get("dc_gain_db", 0.0)
        bw = meas.get("unity_gain_bandwidth", 0.0)
        pm = meas.get("phase_margin", 0.0)
        power = meas.get("total_power", 0.0)
        score = 0.0
        score += 25.0 * max(0.0, gain_min - gain) / max(gain_min, 1.0)
        score += 25.0 * max(0.0, bw_min - bw) / max(bw_min, 1.0)
        score += 48.0 * phase_margin_window_penalty(pm, pm_min or 60.0)
        if pm < 55.0:
            score += 80.0 + max(0.0, 55.0 - pm)
        if bw_max < float("inf"):
            score += 0.7 * max(0.0, bw - bw_max) / max(bw_max, 1.0)
        if power_max < float("inf"):
            score += 0.2 * max(0.0, power) / max(power_max, 1e-12)
        if swing_min:
            score += 6.0 * max(0.0, swing_min - meas.get("output_swing", 0.0)) / max(swing_min, 1.0)
        if icmr_min_max is not None:
            if "icmr_min" in meas:
                score += 4.0 * max(0.0, meas["icmr_min"] - icmr_min_max) / max(icmr_min_max, 1.0)
            else:
                score += 4.0
        if icmr_max_min is not None:
            score += 4.0 * max(0.0, icmr_max_min - meas.get("icmr_max", 0.0)) / max(icmr_max_min, 1.0)
        if pm >= pm_min and gain >= gain_min and bw >= bw_min:
            score -= min(max(0.0, bw - bw_min) / max(bw_min, 1.0), 1.0) / 50.0
        return score

    def should_skip_candidate(cc: float, rz: float) -> bool:
        # Very small Cc with a large nulling resistor is frequently unstable and
        # expensive for PSP/ngspice AC convergence, while rarely fixing PM.
        return cc <= cc_low * 1.35 and rz >= rz_high * 0.70

    def candidate_zero_target(result) -> float:
        if gain_id:
            op = op_for_device(result, gain_id)
            gm = abs(float(op.get("gm", 0.0))) if op else 0.0
            if gm > 1e-12:
                return 1.0 / gm
        return rz0

    def evaluate_candidate(
        best: dict,
        cc: float,
        rz: float,
        load_scale: float,
        tail_scale: float,
        stage2_scale: float,
    ) -> dict:
        apply_load_scale(load_scale)
        apply_current_scale(tail_scale, stage2_scale)
        state.global_parameters["Cc"] = cc
        state.global_parameters["Rz"] = rz
        trial = sim.run(generate_netlist(state), work_dir=str(work_dir), include_transient=False)
        hard_errors = hard_reliability_errors(state, trial)
        if hard_errors:
            stats["rejected_unsafe_candidates"] += 1
            if len(stats["unsafe_examples"]) < 4:
                stats["unsafe_examples"].append(
                    {
                        "Cc": cc,
                        "Rz": rz,
                        "load_scale": load_scale,
                        "tail_current_scale": tail_scale,
                        "stage2_current_scale": stage2_scale,
                        "errors": hard_errors[:3],
                    }
                )
            return best
        meas = dict(trial.measurements)
        zero_target = candidate_zero_target(trial)
        score = score_measurements(meas)
        if score < best["score"]:
            return {
                "score": score,
                "Cc": cc,
                "Rz": rz,
                "Rz_target_1_over_gm2": zero_target,
                "load_scale": load_scale,
                "tail_current_scale": tail_scale,
                "stage2_current_scale": stage2_scale,
                "I_tail": state.global_parameters.get("I_tail", base_currents["I_tail"]),
                "I_stage2": state.global_parameters.get("I_stage2", base_currents["I_stage2"]),
                "measurements": meas,
                "_has_operating_points": bool(trial.operating_points),
            }
        return best

    best = {
        "score": float("inf"),
        "Cc": current_cc,
        "Rz": current_rz,
        "Rz_target_1_over_gm2": rz0,
        "load_scale": 1.0,
        "tail_current_scale": 1.0,
        "stage2_current_scale": 1.0,
        "I_tail": base_currents["I_tail"],
        "I_stage2": base_currents["I_stage2"],
        "measurements": {},
        "_has_operating_points": False,
    }
    stats = {
        "evaluated_candidates": 0,
        "skipped_candidates": 0,
        "candidate_budget": (
            max_base_candidates
            + max_refine_candidates
            + max_load_candidates
            + max_current_candidates
            + max_gain_candidates
        ),
        "early_stop": False,
        "early_stop_reason": "",
        "rejected_unsafe_candidates": 0,
        "unsafe_examples": [],
        "gain_refine_candidates": 0,
    }
    started = time.time()
    evaluated: set[tuple[str, str, str, str, str]] = set()
    records: list[dict] = []

    def candidate_key(
        cc: float,
        rz: float,
        load_scale: float,
        tail_scale: float,
        stage2_scale: float,
    ) -> tuple[str, str, str, str, str]:
        return (
            f"{cc:.12e}",
            f"{rz:.12e}",
            f"{load_scale:.6f}",
            f"{tail_scale:.6f}",
            f"{stage2_scale:.6f}",
        )

    def add_candidate(
        candidates: list[tuple[float, float, float, float, float, str]],
        cc: float,
        rz: float,
        load_scale: float = 1.0,
        tail_scale: float = 1.0,
        stage2_scale: float = 1.0,
        tag: str = "base",
    ) -> None:
        cc = min(max(float(cc), cc_low), cc_high)
        rz = min(max(float(rz), rz_low), rz_high)
        load_scale = float(load_scale)
        tail_scale = float(tail_scale)
        stage2_scale = float(stage2_scale)
        key = candidate_key(cc, rz, load_scale, tail_scale, stage2_scale)
        if not any(
            candidate_key(old_cc, old_rz, old_load, old_tail, old_stage2) == key
            for old_cc, old_rz, old_load, old_tail, old_stage2, _ in candidates
        ):
            candidates.append((cc, rz, load_scale, tail_scale, stage2_scale, tag))

    def candidate_priority(item: tuple[float, float, float, float, float, str]) -> float:
        cc, rz, load_scale, tail_scale, stage2_scale, _tag = item
        cc_ref = max(current_cc, cc_low)
        rz_ref = max(rz0, rz_low)
        return (
            abs(math.log(max(cc, 1e-30) / max(cc_ref, 1e-30)))
            + 0.6 * abs(math.log(max(rz, 1e-30) / max(rz_ref, 1e-30)))
            + 0.3 * abs(load_scale - 1.0)
            + 0.25 * abs(math.log(max(tail_scale, 1e-9)))
            + 0.25 * abs(math.log(max(stage2_scale, 1e-9)))
        )

    def budget_exhausted() -> bool:
        if stats["evaluated_candidates"] >= stats["candidate_budget"]:
            return True
        return (time.time() - started) >= time_budget_sec

    def run_candidates(candidates: list[tuple[float, float, float, float, float, str]], limit: int) -> bool:
        nonlocal best
        for cc, rz, load_scale, tail_scale, stage2_scale, tag in candidates[: max(0, limit)]:
            if budget_exhausted():
                stats["early_stop"] = True
                stats["early_stop_reason"] = "budget_exhausted"
                return True
            key = candidate_key(cc, rz, load_scale, tail_scale, stage2_scale)
            if key in evaluated:
                continue
            if should_skip_candidate(cc, rz):
                stats["skipped_candidates"] += 1
                continue
            evaluated.add(key)
            before_score = best["score"]
            best = evaluate_candidate(best, cc, rz, load_scale, tail_scale, stage2_scale)
            stats["evaluated_candidates"] += 1
            if best["score"] < before_score:
                records.append(
                    {
                        "score": best["score"],
                        "Cc": best["Cc"],
                        "Rz": best["Rz"],
                        "load_scale": best["load_scale"],
                        "tail_current_scale": best.get("tail_current_scale", 1.0),
                        "stage2_current_scale": best.get("stage2_current_scale", 1.0),
                        "tag": tag,
                    }
                )
            if robust_spec_pass(best["measurements"]):
                stats["early_stop"] = True
                stats["early_stop_reason"] = "robust_spec_pass"
                return True
        return False

    base_candidates: list[tuple[float, float, float, float, float, str]] = []
    add_candidate(base_candidates, current_cc, current_rz, tag="current")

    low_rz_rescue = min(max(rz0 * 0.85, rz_low), rz_high)
    mid_rz_rescue = min(max(rz0, rz_low), rz_high)
    high_rz_rescue = min(max(rz0 * 1.15, rz_low), rz_high)
    for cc, rz in (
        (cc_high, high_rz_rescue),
        (cc_high, mid_rz_rescue),
        (cc_high * 0.8, high_rz_rescue),
        (cc_high * 0.8, mid_rz_rescue),
        (max(current_cc * 3.0, cc_low * 10.0), low_rz_rescue),
        (max(current_cc * 3.0, cc_low * 10.0), mid_rz_rescue),
        (max(current_cc * 4.0, cc_low * 15.0), mid_rz_rescue),
    ):
        add_candidate(base_candidates, cc, rz, tag="stability_rescue")

    stability_cc_pool = _unique_preserve(
        [
            current_cc * 1.5,
            current_cc * 2.0,
            current_cc * 3.0,
            cc_low * 10.0,
            cc_low * 15.0,
            cc_low * 20.0,
            cc_high,
        ],
        cc_low,
        cc_high,
    )
    stability_rz_pool = _unique_preserve(
        [
            rz0 * 0.85,
            rz0,
            rz0 * 1.15,
        ],
        rz_low,
        rz_high,
    )
    for cc in stability_cc_pool:
        for rz in stability_rz_pool:
            add_candidate(base_candidates, cc, rz, tag="stability_rescue")

    coarse_candidates: list[tuple[float, float, float, float, float, str]] = []
    cc_pool = _unique_preserve(
        [
            current_cc,
            current_cc * 0.75,
            current_cc * 1.25,
            current_cc * 1.5,
            cc_low * 1.5,
            cc_low * 2.0,
            cc_low * 3.0,
            cc_low * 5.0,
            min(cc_high, math.sqrt(cc_low * cc_high)),
        ],
        cc_low,
        cc_high,
    )
    rz_pool = _unique_preserve(
        [
            current_rz,
            rz0 * 0.8,
            rz0,
            rz0 * 1.2,
        ],
        rz_low,
        rz_high,
    )
    for cc in cc_pool:
        for rz in rz_pool:
            add_candidate(coarse_candidates, cc, rz, tag="coarse")
    coarse_candidates.sort(key=candidate_priority)
    for cc, rz, load_scale, tail_scale, stage2_scale, tag in coarse_candidates:
        add_candidate(base_candidates, cc, rz, load_scale, tail_scale, stage2_scale, tag)

    original_timeout = getattr(sim, "timeout_sec", None)
    if original_timeout is not None and candidate_timeout_sec > 0:
        sim.timeout_sec = min(float(original_timeout), float(candidate_timeout_sec))
    try:
        stopped = run_candidates(base_candidates, max_base_candidates)

        if not stopped and not spec_pass(best["measurements"]) and max_refine_candidates > 0:
            refine_candidates: list[tuple[float, float, float, float, float, str]] = []
            seeds = sorted(records, key=lambda item: item["score"])[:2] or [best]
            for seed in seeds:
                for cc_factor in (0.75, 1.0, 1.25):
                    for rz_factor in (0.75, 1.0, 1.25):
                        add_candidate(
                            refine_candidates,
                            seed["Cc"] * cc_factor,
                            seed["Rz"] * rz_factor,
                            seed.get("load_scale", 1.0),
                            seed.get("tail_current_scale", 1.0),
                            seed.get("stage2_current_scale", 1.0),
                            tag="refine",
                        )
            refine_candidates.sort(key=candidate_priority)
            stopped = run_candidates(refine_candidates, max_refine_candidates)

        if (
            not stopped
            and load_dims
            and not spec_pass(best["measurements"])
            and max_load_candidates > 0
        ):
            load_candidates: list[tuple[float, float, float, float, float, str]] = []
            seeds = sorted(records, key=lambda item: item["score"])[:2] or [best]
            for seed in seeds:
                for load_scale in (1.08, 1.16, 1.24):
                    for cc_factor in (0.9, 1.0, 1.15):
                        add_candidate(
                            load_candidates,
                            seed["Cc"] * cc_factor,
                            seed["Rz"],
                            load_scale,
                            seed.get("tail_current_scale", 1.0),
                            seed.get("stage2_current_scale", 1.0),
                            tag="load_refine",
                        )
            load_candidates.sort(key=candidate_priority)
            stopped = run_candidates(load_candidates, max_load_candidates)

        if (
            not stopped
            and current_ranges
            and not spec_pass(best["measurements"])
            and max_current_candidates > 0
        ):
            current_candidates: list[tuple[float, float, float, float, float, str]] = []
            seeds = sorted(records, key=lambda item: item["score"])[:2] or [best]
            recovery_steps = (
                (1.08, 1.10, 1.00),
                (1.12, 1.15, 1.05),
                (1.12, 1.15, 1.10),
                (1.15, 1.20, 1.05),
                (1.15, 1.20, 1.10),
                (1.20, 1.25, 1.10),
                (1.20, 1.40, 1.15),
                (1.25, 1.60, 1.20),
                (1.30, 1.80, 1.25),
                (1.08, 1.15, 1.05),
                (1.12, 1.25, 1.10),
            )
            for seed in seeds:
                for tail_scale, stage2_scale, cc_factor in recovery_steps:
                    add_candidate(
                        current_candidates,
                        seed["Cc"] * cc_factor,
                        seed["Rz"],
                        seed.get("load_scale", 1.0),
                        seed.get("tail_current_scale", 1.0) * tail_scale,
                        seed.get("stage2_current_scale", 1.0) * stage2_scale,
                        tag="current_recovery",
                    )
            run_candidates(current_candidates, max_current_candidates)
    finally:
        if original_timeout is not None:
            sim.timeout_sec = original_timeout

    def run_gain_refine(best: dict) -> dict:
        if not input_pair_dims or max_gain_candidates <= 0:
            return best
        meas = best.get("measurements", {}) or {}
        gain = float(meas.get("dc_gain_db", 0.0) or 0.0)
        if gain >= gain_min:
            return best
        if gain < max(0.0, gain_min - 8.0):
            return best
        if bw_min > 0.0 and float(meas.get("unity_gain_bandwidth", 0.0) or 0.0) < 0.75 * bw_min:
            return best
        if pm_min > 0.0 and float(meas.get("phase_margin", 0.0) or 0.0) < max(55.0, pm_min - 5.0):
            return best
        if swing_min > 0.0 and float(meas.get("output_swing", 0.0) or 0.0) < swing_min:
            return best
        candidates = (
            (1.20, 1.80, 1.00, 2.00),
            (1.50, 2.30, 1.00, 2.40),
            (2.00, 3.00, 1.00, 3.00),
            (1.20, 2.00, 0.90, 2.80),
            (1.50, 2.60, 0.90, 3.00),
            (2.00, 2.60, 1.10, 2.80),
            (1.00, 2.00, 0.85, 3.00),
            (1.00, 2.60, 0.85, 3.00),
            (1.20, 3.00, 0.75, 3.00),
            (1.60, 3.00, 0.85, 3.00),
            (2.20, 3.00, 1.00, 3.00),
            (1.40, 2.80, 1.20, 2.80),
        )
        selected = best
        original_timeout = getattr(sim, "timeout_sec", None)
        if original_timeout is not None:
            sim.timeout_sec = min(float(original_timeout), max(float(candidate_timeout_sec), 12.0))
        try:
            for width_scale, length_scale, gain_width_scale, gain_length_scale in candidates[:max_gain_candidates]:
                if time.time() - started >= time_budget_sec:
                    break
                apply_load_scale(best.get("load_scale", 1.0))
                apply_current_scale(best.get("tail_current_scale", 1.0), best.get("stage2_current_scale", 1.0))
                apply_input_pair_geometry(width_scale, length_scale)
                apply_gain_refine_geometry(gain_width_scale, gain_length_scale)
                state.global_parameters["Cc"] = best["Cc"]
                state.global_parameters["Rz"] = best["Rz"]
                trial = sim.run(generate_netlist(state), work_dir=str(work_dir), include_transient=True)
                hard_errors = hard_reliability_errors(state, trial)
                stats["gain_refine_candidates"] += 1
                if hard_errors:
                    stats["rejected_unsafe_candidates"] += 1
                    continue
                meas = dict(trial.measurements)
                score = score_measurements(meas)
                if score < selected["score"] or spec_pass(meas):
                    selected = {
                        **best,
                        "score": score,
                        "input_pair_width_scale": width_scale,
                        "input_pair_length_scale": length_scale,
                        "gain_refine_width_scale": gain_width_scale,
                        "gain_refine_length_scale": gain_length_scale,
                        "measurements": meas,
                        "_has_operating_points": bool(trial.operating_points),
                    }
                if spec_pass(meas):
                    break
        finally:
            if original_timeout is not None:
                sim.timeout_sec = original_timeout
        return selected

    best = run_gain_refine(best)
    apply_load_scale(best.get("load_scale", 1.0))
    apply_current_scale(best.get("tail_current_scale", 1.0), best.get("stage2_current_scale", 1.0))
    apply_input_pair_geometry(
        best.get("input_pair_width_scale", 1.0),
        best.get("input_pair_length_scale", 1.0),
    )
    apply_gain_refine_geometry(
        best.get("gain_refine_width_scale", 1.0),
        best.get("gain_refine_length_scale", 1.0),
    )
    state.global_parameters["Cc"] = best["Cc"]
    state.global_parameters["Rz"] = best["Rz"]
    final_hard_errors = []
    if best.get("_has_operating_points"):
        final_trial = sim.run(generate_netlist(state), work_dir=str(work_dir), include_transient=sr_min > 0.0)
        if final_trial.operating_points:
            backfill_state_from_ngspice(state, final_trial)
        final_hard_errors = hard_reliability_errors(state, final_trial)
        if final_trial.measurements:
            best["measurements"] = dict(final_trial.measurements)
    best.update(stats)
    best["final_hard_errors"] = final_hard_errors
    best["Rz_target_1_over_gm2"] = best.get("Rz_target_1_over_gm2", rz0)
    best["elapsed_sec"] = time.time() - started
    best["spec_pass"] = spec_pass(best["measurements"])
    return best


@dataclass
class TwoStagePostProcessor:
    skip_dc_repair: bool = False
    skip_comp_tune: bool = False
    compensation_kwargs: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)

    def run(self, state: DesignState, sim: NgspiceSimulator, work_dir: Path) -> list[dict]:
        self.events = []
        if not is_two_stage_state(state):
            return self.events
        op = confirm_initial_operating_point(state, sim, work_dir)
        if op:
            self.events.append({"type": "initial_operating_point", **op})
        if not self.skip_dc_repair:
            info = balance_two_stage_output(state, sim, work_dir)
            if info:
                self.events.append({"type": "stage2_balance", **info})
            headroom = improve_tail_headroom(state, sim, work_dir)
            if headroom:
                self.events.append({"type": "tail_headroom", **headroom})
                rebalance = balance_two_stage_output(state, sim, work_dir)
                if rebalance:
                    self.events.append({"type": "stage2_rebalance", **rebalance})
        if not self.skip_comp_tune and has_miller_rc_compensation(state):
            compensation_kwargs = {
                "time_budget_sec": 160.0,
                "candidate_timeout_sec": 12.0,
            }
            compensation_kwargs.update(dict(self.compensation_kwargs or {}))
            comp = tune_two_stage_compensation(
                state,
                sim,
                work_dir,
                **compensation_kwargs,
            )
            if comp:
                self.events.append({"type": "compensation_tune", **comp})
        return self.events


def _design_var_range(state: DesignState, name: str) -> tuple[float, float] | None:
    for dv in state.design_variables:
        if not dv.device and dv.variable == name:
            return dv.range.min, dv.range.max
    return None


def _unique_sorted(values: list[float], low: float, high: float) -> list[float]:
    out = []
    for value in values:
        value = min(max(float(value), low), high)
        if not any(abs(value - old) <= max(abs(old), abs(value), 1e-30) * 1e-6 for old in out):
            out.append(value)
    return sorted(out)


def _unique_preserve(values: list[float], low: float, high: float) -> list[float]:
    out = []
    for value in values:
        value = min(max(float(value), low), high)
        if not any(abs(value - old) <= max(abs(old), abs(value), 1e-30) * 1e-6 for old in out):
            out.append(value)
    return out


def _snap_to_grid(value: float, precision: float) -> float:
    if precision <= 0:
        return value
    return round(value / precision) * precision

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from netlist.generator import generate_netlist
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
    base_sink_w = sink_ts.parameters.W if sink_ts and sink_ts.parameters.W > 0 else None
    best = {"scale": 1.0, "sink_scale": 1.0, "vout": None, "error": float("inf")}

    def _clip_width(width: float) -> float:
        return min(max(width, min_w), max_w)

    def evaluate(scale: float, sink_scale: float = 1.0) -> float | None:
        gain_ts.parameters.W = _clip_width(base_w * scale)
        if sink_ts and base_sink_w:
            sink_ts.parameters.W = _clip_width(base_sink_w * sink_scale)
        trial = sim.run(generate_netlist(state), work_dir=str(work_dir))
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
            sink_ts.parameters.W = base_sink_w
        return {}
    if 0.25 * vdd <= base_vout <= 0.75 * vdd:
        gain_ts.parameters.W = base_w
        if sink_ts and base_sink_w:
            sink_ts.parameters.W = base_sink_w
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
            sink_ts.parameters.W = base_sink_w
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
        sink_ts.parameters.W = _clip_width(base_sink_w * best["sink_scale"])
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
    base_widths = {
        dev_id: state.transistors[dev_id].parameters.W
        for dev_id in input_ids
        if dev_id in state.transistors and state.transistors[dev_id].parameters.W > 0
    }
    if len(base_widths) != len(input_ids):
        return {}

    best = {"scale": 1.0, "margin": float("-inf"), "vds": None, "vdsat": None}
    chosen = None
    for scale in (1.0, 1.2, 1.5, 2.0, 2.5, 3.0):
        for dev_id, base_w in base_widths.items():
            state.transistors[dev_id].parameters.W = min(base_w * scale, max_w)
        trial = sim.run(generate_netlist(state), work_dir=str(work_dir))
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
        state.transistors[dev_id].parameters.W = min(base_w * chosen["scale"], max_w)
    return chosen


def tune_two_stage_compensation(
    state: DesignState,
    sim: NgspiceSimulator,
    work_dir: Path,
) -> dict:
    if not is_two_stage_state(state):
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

    cc_candidates = _unique_sorted(
        [
            cc_low,
            cc_low * 1.25,
            cc_low * 1.5,
            cc_low * 2.0,
            cc_low * 2.5,
            cc_low * 3.0,
            cc_low * 4.0,
            cc_low * 5.0,
            cc_low * 7.5,
            cc_low * 10.0,
            current_cc * 0.75,
            current_cc,
            current_cc * 1.5,
            current_cc * 2.25,
            cc_high,
        ],
        cc_low,
        cc_high,
    )
    rz_candidates = _unique_sorted(
        [
            rz_low,
            300.0,
            500.0,
            750.0,
            1000.0,
            1500.0,
            2000.0,
            2500.0,
            3000.0,
            3500.0,
            4000.0,
            4500.0,
            5000.0,
            7500.0,
            10000.0,
            15000.0,
            rz0 * 0.5,
            rz0,
            rz0 * 1.5,
            rz0 * 2.0,
            current_rz,
            rz_high,
        ],
        rz_low,
        rz_high,
    )

    targets = state.targets
    gain_min = targets.get("dc_gain", Target()).min or 0.0
    bw_target = targets.get("unity_gain_bandwidth", Target())
    bw_min = bw_target.min or 0.0
    bw_max = bw_target.max or float("inf")
    pm_min = targets.get("phase_margin", Target()).min or 0.0
    power_max = targets.get("power", Target()).max or float("inf")

    best = {"score": float("inf"), "Cc": current_cc, "Rz": current_rz, "measurements": {}}
    for cc in cc_candidates:
        for rz in rz_candidates:
            state.global_parameters["Cc"] = cc
            state.global_parameters["Rz"] = rz
            trial = sim.run(generate_netlist(state), work_dir=str(work_dir))
            meas = dict(trial.measurements)
            gain = meas.get("dc_gain_db", 0.0)
            bw = meas.get("unity_gain_bandwidth", 0.0)
            pm = meas.get("phase_margin", 0.0)
            power = meas.get("total_power", 0.0)
            score = 0.0
            score += 20.0 * max(0.0, gain_min - gain) / max(gain_min, 1.0)
            score += 15.0 * max(0.0, bw_min - bw) / max(bw_min, 1.0)
            score += 25.0 * max(0.0, pm_min - pm) / max(pm_min, 1.0)
            if bw_max < float("inf"):
                score += 0.7 * max(0.0, bw - bw_max) / max(bw_max, 1.0)
            if power_max < float("inf"):
                score += 0.2 * max(0.0, power) / max(power_max, 1e-12)
            if pm >= pm_min and gain >= gain_min and bw >= bw_min:
                score -= min(pm - pm_min, 40.0) / 200.0
            if score < best["score"]:
                best = {"score": score, "Cc": cc, "Rz": rz, "measurements": meas}

    state.global_parameters["Cc"] = best["Cc"]
    state.global_parameters["Rz"] = best["Rz"]
    return best


@dataclass
class TwoStagePostProcessor:
    skip_dc_repair: bool = False
    skip_comp_tune: bool = False
    events: list[dict] = field(default_factory=list)

    def run(self, state: DesignState, sim: NgspiceSimulator, work_dir: Path) -> list[dict]:
        self.events = []
        if not is_two_stage_state(state):
            return self.events
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
        if not self.skip_comp_tune:
            comp = tune_two_stage_compensation(state, sim, work_dir)
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

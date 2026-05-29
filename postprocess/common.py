from __future__ import annotations

from schemas.design_state import DesignState
from simulator.ngspice import SimulationResult


def normalize_phase_margin(raw_value: float) -> float:
    """Convert wrapped ngspice phase-at-unity values to conventional PM."""
    value = float(raw_value)
    while value < -180.0:
        value += 360.0
    while value > 180.0:
        value -= 360.0
    if value < 0.0:
        return 180.0 + value
    return value


def phase_margin_window_penalty(
    pm: float,
    target_min: float = 60.0,
    *,
    hard_min: float = 55.0,
    target_high: float = 65.0,
    acceptable_high: float = 70.0,
    conservative_high: float = 75.0,
) -> float:
    """Piecewise PM cost: hard below 55, best near 60-65, no reward above 75."""
    pm = float(pm or 0.0)
    target_min = float(target_min or 60.0)
    if pm < hard_min:
        return 2.5 + (hard_min - pm) / max(hard_min, 1.0)
    if pm < target_min:
        return (target_min - pm) / max(target_min - hard_min, 1.0)
    if pm <= target_high:
        return 0.0
    if pm <= acceptable_high:
        return 0.04 * (pm - target_high) / max(acceptable_high - target_high, 1.0)
    if pm <= conservative_high:
        return 0.10 + 0.10 * (pm - acceptable_high) / max(conservative_high - acceptable_high, 1.0)
    return 0.25 + min((pm - conservative_high) / 50.0, 1.5)


def backfill_state_from_ngspice(state: DesignState, result: SimulationResult) -> None:
    for dev_id, ts in state.transistors.items():
        op = _op_for_device(result, dev_id)
        if not op:
            continue
        params = ts.parameters
        if "gm" in op:
            params.gm = float(op["gm"])
        if "gds" in op:
            params.gds = float(op["gds"])
        if "vgs" in op:
            params.vgs = abs(float(op["vgs"]))
        if "vds" in op:
            params.vds = abs(float(op["vds"]))
        if "vdsat" in op:
            params.vdsat = abs(float(op["vdsat"]))
        if "id" in op:
            params.id = abs(float(op["id"]))
        if "cgs" in op:
            params.cgs = abs(float(op["cgs"]))
        if "cgd" in op:
            params.cgd = abs(float(op["cgd"]))
        if "cgg" in op:
            params.cgg = abs(float(op["cgg"]))
        if params.id > 0 and params.gm > 0:
            params.gm_id_realized = params.gm / params.id
        if params.vdsat > 0:
            params.region = "saturation" if params.vds >= params.vdsat else "linear"


def _op_for_device(result: SimulationResult, device_id: str) -> dict:
    for name in (f"M{device_id}".upper(), device_id.upper()):
        op = result.operating_points.get(name)
        if op:
            return op
    return {}

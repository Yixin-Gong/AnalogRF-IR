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

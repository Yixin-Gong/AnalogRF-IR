from __future__ import annotations

from schemas.design_state import DesignState


def has_global_design_knob(state: DesignState, name: str) -> bool:
    """Return true when a global parameter or design variable is explicitly present."""
    if name in (state.global_parameters or {}):
        return True
    return any((not dv.device) and dv.variable == name for dv in state.design_variables)


def has_miller_capacitive_compensation(state: DesignState) -> bool:
    """Return true only for schemas that explicitly expose Miller capacitance."""
    return has_global_design_knob(state, "Cc")


def has_miller_rc_compensation(state: DesignState) -> bool:
    """Return true only for schemas that explicitly expose a series Rz-Cc network."""
    return has_miller_capacitive_compensation(state) and has_global_design_knob(state, "Rz")

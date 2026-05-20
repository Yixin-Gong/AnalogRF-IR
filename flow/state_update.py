from __future__ import annotations

from typing import Any

from core.regions import normalize_spice_region
from optimizer.nsga2 import round_and_update_state
from schemas.design_state import DesignState, TransistorParameters


def apply_optimizer_meta_to_state(state: DesignState, best_meta: dict[str, Any]) -> None:
    decoded = best_meta.get("decoded", {}) or {}
    transistor_params = best_meta.get("transistor_params", {}) or {}
    merged_globals = dict(state.global_parameters or {})
    merged_globals.update({
        str(k): float(v)
        for k, v in (decoded.get("__global__", {}) or {}).items()
    })
    state.global_parameters = merged_globals
    for dev_id, vars_dict in decoded.items():
        if str(dev_id).startswith("__") or dev_id not in state.transistors:
            continue
        ts = state.transistors[dev_id]
        ts.gm_id_strategy = float(vars_dict.get("gm_id", ts.gm_id_strategy))
        ts.L_strategy = float(vars_dict.get("L", ts.L_strategy))
        if dev_id in transistor_params:
            phys = transistor_params[dev_id]
            drain_current = float(phys.get("id", 0.0))
            vds = float(phys.get("vds", 0.0))
            vdsat = float(phys.get("vdsat", 0.0))
            ts.parameters = TransistorParameters(
                W=float(phys.get("W", 0.0)),
                L=float(vars_dict.get("L", phys.get("L", 0.0))),
                gm=float(phys.get("gm", 0.0)),
                gds=float(phys.get("gds", 0.0)),
                vgs=float(phys.get("vgs", 0.0)),
                vds=vds,
                vdsat=vdsat,
                region=normalize_spice_region(phys.get("region"), vds=vds, vdsat=vdsat, current=drain_current),
                id=drain_current,
                ft=float(phys.get("ft", 0.0)),
                gm_id_realized=float(phys.get("gm_id", phys.get("gm_id_realized", 0.0))),
                cgs=float(phys.get("cgs", 0.0)),
                cgd=float(phys.get("cgd", 0.0)),
                ic=float(phys.get("ic", 0.0)),
            )
    round_and_update_state(state, decoded, transistor_params)

"""AnalogRF-IR internal documentation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# Internal implementation note.
UT_300K = 0.02585  # kT/q [V]


@dataclass
class InversionResult:
    """AnalogRF-IR internal documentation."""
    device_id: str
    ic: float                          # Internal implementation note.
    region: str                        # weak | moderate | strong | unknown
    gm_id: float                       # gm/ID [S/A]
    vgs: float                         # Internal implementation note.
    vth: float                         # Internal implementation note.
    vov: float                         # Internal implementation note.
    description: str = ""              # Internal implementation note.


# Internal implementation note.

def compute_I0(device_type: str, L: float, proc) -> float:
    """AnalogRF-IR internal documentation."""
    if device_type in ("nmos", "nch_18"):
        n = proc.n_sub_nmos if hasattr(proc, "n_sub_nmos") else 1.4
        mu = proc.mu_n if hasattr(proc, "mu_n") else 0.04
    else:
        n = proc.n_sub_pmos if hasattr(proc, "n_sub_pmos") else 1.4
        mu = proc.mu_p if hasattr(proc, "mu_p") else 0.01

    cox = proc.COX if hasattr(proc, "COX") else 1.535e-2
    return 2.0 * n * mu * cox * UT_300K * UT_300K


def compute_IC_from_gm_id(gm_id: float, n: float = 1.4,
                          ut: float = UT_300K) -> float:
    """AnalogRF-IR internal documentation."""
    if gm_id <= 0:
        return 0.0

    # Internal implementation note.
    # gm/ID ≈ 2/VOV, VOV = 2*UT*sqrt(IC)
    # Internal implementation note.

    x = n * ut * gm_id

    # Internal implementation note.
    if x >= 0.999:
        # Internal implementation note.
        return 1.0 / (math.exp(1.0 / (1.0 - x)) - 1.0) if x < 0.9999 else 0.01

    # Internal implementation note.
    lo, hi = 1e-4, 1e6
    for _ in range(60):
        mid = (lo + hi) / 2.0
        # EKV: gm/ID = 2 / (n*UT * (1 + sqrt(1 + 4*IC)))
        # Internal implementation note.
        ic_sqrt = math.sqrt(1.0 + 4.0 * mid)
        gm_id_pred = 2.0 / (n * ut * (1.0 + ic_sqrt))

        if gm_id_pred > gm_id:
            lo = mid
        else:
            hi = mid

        if hi - lo < 1e-8 * max(lo, 1e-4):
            break

    return (lo + hi) / 2.0


def compute_IC_from_id_w(id_w: float, device_type: str,
                         proc) -> float:
    """AnalogRF-IR internal documentation."""
    i0 = compute_I0(device_type, 1e-6, proc)  # Internal implementation note.
    return id_w / i0 if i0 > 0 else 0.0


# Internal implementation note.

def ic_to_region(ic: float) -> str:
    """AnalogRF-IR internal documentation."""
    if ic < 0:
        return "unknown"
    if ic < 0.1:
        return "weak"
    if ic <= 10:
        return "moderate"
    return "strong"


def ic_to_description(ic: float, gm_id: Optional[float] = None) -> str:
    """AnalogRF-IR internal documentation."""
    region = ic_to_region(ic)
    parts = [f"IC={ic:.3f}"]

    if region == "weak":
        parts.append("weak inversion / subthreshold")
        parts.append("high gm efficiency, lower bandwidth, higher noise")
    elif region == "moderate":
        parts.append("moderate inversion")
        parts.append("gain-bandwidth tradeoff region")
    elif region == "strong":
        parts.append("strong inversion")
        parts.append("higher bandwidth and linearity, lower gm efficiency")
    else:
        parts.append("unknown inversion level")

    if gm_id is not None:
        parts.append(f"gm/ID={gm_id:.1f}")

    return " — ".join(parts)


# Internal implementation note.

class InversionAnalyzer:
    """AnalogRF-IR internal documentation."""

    def __init__(self, n_default: float = 1.4):
        self.n_default = n_default

    def analyze_transistor(self, ts, proc,
                           device_type: Optional[str] = None,
                           vth: Optional[float] = None) -> InversionResult:
        """AnalogRF-IR internal documentation."""
        p = ts.parameters
        dtype = device_type or ts.type or "nmos"

        # Internal implementation note.
        if vth is None:
            vth = proc.VTH_n if dtype in ("nmos", "nch_18") else proc.VTH_p

        # Internal implementation note.
        gm_id = p.gm_id_realized if p.gm_id_realized > 0 else ts.gm_id_strategy
        vgs = p.vgs if p.vgs > 0 else 0.0

        # Internal implementation note.
        n = (proc.n_sub_nmos if dtype in ("nmos", "nch_18")
             else proc.n_sub_pmos)

        # Internal implementation note.
        ic = compute_IC_from_gm_id(gm_id, n)

        # Internal implementation note.
        if hasattr(p, "id") and p.id > 0 and p.W > 0:
            id_w = p.id / p.W
            ic_from_id_w = compute_IC_from_id_w(id_w, dtype, proc)
        else:
            ic_from_id_w = None

        region = ic_to_region(ic)
        vov = vgs - vth if vgs > 0 else 0.0

        desc = ic_to_description(ic, gm_id)

        return InversionResult(
            device_id=ts.device_id,
            ic=ic,
            region=region,
            gm_id=gm_id,
            vgs=vgs,
            vth=vth,
            vov=vov,
            description=desc,
        )

    def analyze_all(self, state,
                    by_role: bool = True) -> Dict[str, InversionResult]:
        """AnalogRF-IR internal documentation."""
        results = {}
        for dev_id, ts in state.transistors.items():
            dev_def = state.get_device_def(dev_id)
            dtype = dev_def.type if dev_def else ts.type
            vth = None
            if hasattr(state, "process"):
                result = self.analyze_transistor(ts, state.process, dtype, vth)
            else:
                # fallback: use defaults
                result = self.analyze_transistor(ts, None, dtype, vth)
            results[dev_id] = result

        if by_role:
            # Internal implementation note.
            sorted_results = {}
            for dev in state.topology.devices:
                if dev.id in results:
                    sorted_results[dev.id] = results[dev.id]
            return sorted_results

        return results

    def summary_table(self, state) -> str:
        """AnalogRF-IR internal documentation."""
        results = self.analyze_all(state)
        lines = [
            f"{'Device':<6} {'Role':<22} {'Type':<5} "
            f"{'gm/ID':<8} {'IC':<10} {'Region':<12} {'Note'}"
        ]
        lines.append("-" * 80)

        for dev_id, r in results.items():
            dev_def = state.get_device_def(dev_id)
            role = dev_def.role if dev_def else "?"
            dtype = dev_def.type if dev_def else "?"
            note = ""
            if r.ic < 0.05:
                note = "deep subthreshold"
            elif r.ic > 50:
                note = "deep strong inversion"
            elif 0.5 < r.ic < 2:
                note = "best-efficiency region"

            lines.append(
                f"{dev_id:<6} {role:<22} {dtype:<5} "
                f"{r.gm_id:<8.1f} {r.ic:<10.3f} {r.region:<12} {note}"
            )

        return "\n".join(lines)

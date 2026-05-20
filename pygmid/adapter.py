"""AnalogRF-IR internal documentation."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, Optional, Tuple, Any

try:
    from pygmid.lookup import LookupTable
except ImportError:
    from lookup import LookupTable  # Internal implementation note.


# Internal implementation note.

class _AnalyticalFallback:
    """AnalogRF-IR internal documentation."""

    def __init__(self, device_type: str):
        self.device_type = device_type
        if device_type in ("nmos", "nch_18"):
            self.VTH = 0.378
            self.KP = 910e-6
            self.COX = 1.535e-2
        else:  # pmos
            self.VTH = 0.321
            self.KP = 123e-6
            self.COX = 1.470e-2
        self.n_sub = 1.4
        self.VT = 0.02585

    def forward(self, gm_id: float, L: float, id_target: float) -> Dict[str, float]:
        if gm_id > 10:
            vov = 2.0 / gm_id
        elif gm_id > 3:
            vov = 2.0 / gm_id
        else:
            vov = self.n_sub * self.VT
        vov = max(vov, 0.02)

        vgs = self.VTH + vov
        W = (2.0 * id_target * L) / (self.KP * vov ** 2)
        W = max(W, L * 1.0)
        gm = 2.0 * id_target / vov
        vdsat = vov * 0.8
        cgs = (2.0/3.0) * self.COX * W * L
        cgd = 0.1 * cgs
        ft = gm / (2.0 * math.pi * (cgs + cgd)) if (cgs + cgd) > 0 else 0

        # Internal implementation note.
        # VOV ~= 2*UT*sqrt(IC) for strong inversion
        ic = max((vov / (2.0 * self.VT)) ** 2, 0.001)

        return {
            "W": W, "vgs": vgs, "vdsat": vdsat,
            "gm": gm, "ft": ft, "vov": vov,
            "cgs": cgs, "cgd": cgd, "gds": 0.15 * id_target,
            "ic": ic, "gm_id": gm_id,
        }

    def backward(self, gm: float, id_val: float, vgs: float) -> Dict[str, float]:
        gm_id = gm / id_val if id_val > 1e-15 else 0
        region = "subthreshold" if gm_id > 15 else ("moderate" if gm_id > 8 else "saturation")
        return {"gm_id_realized": gm_id, "region": region}

    def lookup(self, gm_id: float, L: float) -> Dict[str, float]:
        if gm_id > 10:
            vov = 2.0 / gm_id
        else:
            vov = self.n_sub * self.VT * (1.0 + gm_id / 15.0)
        vov = max(vov, 0.02)
        id_over_w = 0.5 * self.KP * (1.0 / L) * vov ** 2
        gm_over_w = id_over_w * gm_id
        gm_gds = gm_id / 0.15
        ft = gm_id * id_over_w / (2.0 * math.pi * self.COX * vov)
        return {
            "id_over_w": id_over_w, "gm_over_w": gm_over_w,
            "gm_gds": gm_gds, "vov": vov, "ft_approx": ft,
        }


# Boris Murmann PygmidAdapter

class PygmidAdapter:
    """AnalogRF-IR internal documentation."""

    def __init__(self,
                 nmos_table: Optional[str] = None,
                 pmos_table: Optional[str] = None,
                 tables_dir: Optional[str] = None):
        """AnalogRF-IR internal documentation."""
        self._nmos: Optional[LookupTable] = None
        self._pmos: Optional[LookupTable] = None
        self._fallback_nmos = _AnalyticalFallback("nmos")
        self._fallback_pmos = _AnalyticalFallback("pmos")

        self._nmos_path = nmos_table
        self._pmos_path = pmos_table

        if nmos_table:
            self._load_nmos(nmos_table)
        if pmos_table:
            self._load_pmos(pmos_table)

        if tables_dir and (not self._nmos or not self._pmos):
            self._auto_discover(tables_dir)

    def _load_nmos(self, path: str) -> None:
        try:
            self._nmos = LookupTable(path)
        except (FileNotFoundError, KeyError, OSError) as e:
            print(f"[PygmidAdapter] NMOS table not loadable ({e}), using fallback")

    def _load_pmos(self, path: str) -> None:
        try:
            self._pmos = LookupTable(path)
        except (FileNotFoundError, KeyError, OSError) as e:
            print(f"[PygmidAdapter] PMOS table not loadable ({e}), using fallback")

    def _auto_discover(self, tables_dir: str) -> None:
        """AnalogRF-IR internal documentation."""
        import os
        d = Path(tables_dir)
        if not d.is_dir():
            return
        for f in sorted(d.iterdir()):
            name = f.name.lower()
            if name.endswith("_nmos.npz") and not self._nmos:
                self._load_nmos(str(f))
            elif name.endswith("_pmos.npz") and not self._pmos:
                self._load_pmos(str(f))

    def _get_table(self, device_type: str) -> Optional[LookupTable]:
        if device_type in ("nmos", "nch_18"):
            return self._nmos
        else:
            return self._pmos

    def _get_fallback(self, device_type: str) -> _AnalyticalFallback:
        return self._fallback_nmos if device_type in ("nmos", "nch_18") else self._fallback_pmos

    def _compute_ic(self, id_w: float, device_type: str) -> float:
        """AnalogRF-IR internal documentation."""
        UT = 0.02585
        if device_type in ("nmos", "nch_18"):
            n, mu = 1.4, 0.04
        else:
            n, mu = 1.4, 0.01
        cox = 1.535e-2
        i0 = 2.0 * n * mu * cox * UT * UT
        return id_w / i0 if i0 > 0 else 0.0

    @property
    def has_nmos_table(self) -> bool:
        return self._nmos is not None

    @property
    def has_pmos_table(self) -> bool:
        return self._pmos is not None

    # Internal implementation note.

    def forward(self, gm_id: float, L: float, id_target: float,
                device_type: str = "nmos") -> Dict[str, float]:
        """AnalogRF-IR internal documentation."""
        table = self._get_table(device_type)

        if table is not None:
            try:
                result = table.lookup(GM_ID=gm_id, L=L)
                id_w = max(result["ID_W"], 1e-15)
                W = id_target / id_w
                W = max(W, 1e-9)  # Internal implementation note.

                cgs_w = result.get("CGS_W", 0) or 0
                cgd_w = result.get("CGD_W", 0) or 0
                cgg_w = result.get("CGG_W", 0) or 0
                vth = result.get("VTH", self._get_fallback(device_type).VTH)

                # Internal implementation note.
                ic = self._compute_ic(id_w, device_type)

                return {
                    "W": W,
                    "vgs": result["VGS"],
                    "vdsat": result["VDSAT"],
                    "gm_id": result["GM_ID"],
                    "gm": id_target * result["GM_ID"],
                    "gds": W * result["GDS_W"],
                    "ft": result.get("FT", 0),
                    "cgs": W * cgs_w if cgs_w > 0 else W * cgg_w,
                    "cgd": W * cgd_w if cgd_w > 0 else W * cgg_w * 0.05,
                    "cgg": W * cgg_w,
                    "vov": result["VGS"] - vth,
                    "vth": vth,
                    "id_w": id_w,
                    "ic": ic,
                }
            except Exception as e:
                print(f"[PygmidAdapter] lookup failed for GM_ID={gm_id}, L={L}: {e}")

        # Fallback
        return self._get_fallback(device_type).forward(gm_id, L, id_target)

    def forward_vgs(self, vgs: float, L: float, id_target: float,
                    device_type: str = "nmos") -> Dict[str, float]:
        """Translate a device whose gate bias is imposed by another node."""
        table = self._get_table(device_type)
        if table is not None:
            try:
                result = table.lookup(VGS=vgs, L=L)
                id_w = max(result["ID_W"], 1e-15)
                W = max(id_target / id_w, 1e-9)
                cgs_w = result.get("CGS_W", 0) or 0
                cgd_w = result.get("CGD_W", 0) or 0
                cgg_w = result.get("CGG_W", 0) or 0
                vth = result.get("VTH", self._get_fallback(device_type).VTH)
                ic = self._compute_ic(id_w, device_type)
                return {
                    "W": W,
                    "vgs": result["VGS"],
                    "vdsat": result["VDSAT"],
                    "gm_id": result["GM_ID"],
                    "gm": id_target * result["GM_ID"],
                    "gds": W * result["GDS_W"],
                    "ft": result.get("FT", 0),
                    "cgs": W * cgs_w if cgs_w > 0 else W * cgg_w,
                    "cgd": W * cgd_w if cgd_w > 0 else W * cgg_w * 0.05,
                    "cgg": W * cgg_w,
                    "vov": result["VGS"] - vth,
                    "vth": vth,
                    "id_w": id_w,
                    "ic": ic,
                }
            except Exception as e:
                print(f"[PygmidAdapter] VGS lookup failed for VGS={vgs}, L={L}: {e}")

        fb = self._get_fallback(device_type)
        gm_id_guess = 2.0 / max(vgs - fb.VTH, 0.05)
        return fb.forward(gm_id_guess, L, id_target)

    def backward(self, gm: float, id_val: float, vgs: float,
                 device_type: str = "nmos") -> Dict[str, float]:
        """AnalogRF-IR internal documentation."""
        table = self._get_table(device_type)

        if table is not None and id_val > 1e-15:
            try:
                # Internal implementation note.
                # Internal implementation note.
                # Internal implementation note.
                gm_id = gm / id_val
                region = "saturation"
                if gm_id > 20:
                    region = "subthreshold"
                elif gm_id > 12:
                    region = "moderate"
                return {"gm_id_realized": gm_id, "region": region}
            except Exception:
                pass

        return self._get_fallback(device_type).backward(gm, id_val, vgs)

    def lookup(self, gm_id: float, L: float,
               device_type: str = "nmos") -> Dict[str, float]:
        """AnalogRF-IR internal documentation."""
        table = self._get_table(device_type)

        if table is not None:
            try:
                result = table.lookup(GM_ID=gm_id, L=L)
                return {
                    "id_over_w": result["ID_W"],
                    "gm_over_w": result["GM_W"],
                    "gm_gds": (result["GM_ID"] / max(result["GDS_W"] / result["ID_W"], 1e-15)
                               if result["GDS_W"] > 0 and result["ID_W"] > 0 else 50),
                    "vov": result["VGS"] - result.get("VTH", self._get_fallback(device_type).VTH),
                    "ft_approx": result.get("FT", 0),
                }
            except Exception:
                pass

        return self._get_fallback(device_type).lookup(gm_id, L)

    def get_params(self, device_type: str = "nmos") -> Tuple[float, float, float, float]:
        """AnalogRF-IR internal documentation."""
        fallback = self._get_fallback(device_type)
        table = self._get_table(device_type)

        if table is not None:
            try:
                mid_L = table.L_grid[len(table.L_grid) // 2]
                mid_result = table.lookup(GM_ID=10, L=mid_L)
                vth = mid_result.get("VTH", fallback.VTH)
                # Internal implementation note.
                id_w = mid_result["ID_W"]
                vov = mid_result["VGS"] - vth
                if vov > 0.01 and id_w > 0:
                    kp_est = 2.0 * id_w * mid_L / (vov ** 2)
                else:
                    kp_est = fallback.KP
                # gm_gds to LAMBDA
                gds_w = mid_result.get("GDS_W", 0)
                gm_w = mid_result.get("GM_W", 0)
                if gds_w > 0 and gm_w > 0:
                    lambda_est = gds_w / gm_w * mid_result["GM_ID"]
                else:
                    lambda_est = 0.15
                cox = fallback.COX
                return (vth, kp_est, lambda_est, cox)
            except Exception:
                pass

        return (fallback.VTH, fallback.KP, 0.15, fallback.COX)

    def summary(self) -> str:
        """AnalogRF-IR internal documentation."""
        lines = ["AnalogRF-IR v0.1 PygmidAdapter (Boris Murmann style)"]
        lines.append(f"  NMOS: {'LookupTable' if self._nmos else 'Analytical fallback'}")
        if self._nmos:
            lines.append(f"    {self._nmos.summary()}")
        lines.append(f"  PMOS: {'LookupTable' if self._pmos else 'Analytical fallback'}")
        if self._pmos:
            lines.append(f"    {self._pmos.summary()}")
        return "\n".join(lines)


# Internal implementation note.

def create_pygmid_adapter(nmos_path: Optional[str] = None,
                          pmos_path: Optional[str] = None,
                          tables_dir: Optional[str] = None) -> PygmidAdapter:
    """AnalogRF-IR internal documentation."""
    return PygmidAdapter(nmos_table=nmos_path, pmos_table=pmos_path, tables_dir=tables_dir)

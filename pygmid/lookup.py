"""AnalogRF-IR internal documentation."""

from __future__ import annotations

import numpy as np
from pathlib import Path
from typing import Dict, Optional, Tuple


class LookupTable:
    """AnalogRF-IR internal documentation."""

    def __init__(self, filename: str):
        """AnalogRF-IR internal documentation."""
        data = np.load(filename, allow_pickle=False)

        # Internal implementation note.
        self.L_grid: np.ndarray = data["L_grid"]        # (M,)
        self.VGS_grid: np.ndarray = data["VGS_grid"]    # (N,)
        self.VDS: float = float(data["VDS"])
        self.VSB: float = float(data["VSB"])

        # Internal implementation note.
        self.GM_ID: np.ndarray = data["GM_ID"]           # gm/ID
        self.ID_W: np.ndarray = data["ID_W"]             # ID/W
        self.GM_W: np.ndarray = data["GM_W"]             # gm/W
        self.GDS_W: np.ndarray = data["GDS_W"]           # gds/W
        self.VDSAT: np.ndarray = data["VDSAT"]           # VDSAT
        self.CGG_W: np.ndarray = data.get("CGG_W", np.zeros_like(self.ID_W))
        self.CGS_W: np.ndarray = data.get("CGS_W", np.zeros_like(self.ID_W))
        self.CGD_W: np.ndarray = data.get("CGD_W", np.zeros_like(self.ID_W))
        self.FT: np.ndarray = data.get("FT", np.zeros_like(self.ID_W))
        self.VTH: np.ndarray = data.get("VTH", np.zeros_like(self.ID_W))

        self._M = len(self.L_grid)
        self._N = len(self.VGS_grid)

        # Internal implementation note.
        self._gm_id_min = float(np.nanmin(self.GM_ID))
        self._gm_id_max = float(np.nanmax(self.GM_ID))

    # Internal implementation note.

    def lookup(self, GM_ID: Optional[float] = None,
               L: Optional[float] = None,
               VGS: Optional[float] = None) -> Dict[str, float]:
        """AnalogRF-IR internal documentation."""
        L_val = L or self.L_grid[len(self.L_grid) // 2]

        if GM_ID is not None:
            gm_id_clipped = np.clip(GM_ID, self._gm_id_min * 0.95, self._gm_id_max * 0.95)
            vgs_val = self._invert_gm_id(gm_id_clipped, L_val)
        elif VGS is not None:
            vgs_val = np.clip(VGS, self.VGS_grid[0], self.VGS_grid[-1])
            gm_id_clipped = None
        else:
            raise ValueError("Must provide either GM_ID or VGS")

        return self._interpolate(L_val, vgs_val, gm_id_clipped)

    def lookup_VGS(self, VGS: float, L: Optional[float] = None) -> Dict[str, float]:
        """AnalogRF-IR internal documentation."""
        return self.lookup(VGS=VGS, L=L)

    def forward(self, GM_ID: float, L: float, ID: float) -> Dict[str, float]:
        """AnalogRF-IR internal documentation."""
        result = self.lookup(GM_ID=GM_ID, L=L)
        ID_W = max(result["ID_W"], 1e-15)
        W = ID / ID_W

        return {
            "W": W,
            "VGS": result["VGS"],
            "VDSAT": result["VDSAT"],
            "GM": ID * result["GM_ID"],
            "GDS": W * result["GDS_W"],
            "CGS": W * result.get("CGS_W", 0),
            "CGD": W * result.get("CGD_W", 0),
            "CGG": W * result.get("CGG_W", 0),
            "FT": result.get("FT", 0),
            "VTH": result.get("VTH", 0),
            "GM_ID": result["GM_ID"],
            "ID_W": ID_W,
        }

    @property
    def gm_id_range(self) -> Tuple[float, float]:
        """AnalogRF-IR internal documentation."""
        return (self._gm_id_min, self._gm_id_max)

    @property
    def L_range(self) -> Tuple[float, float]:
        """AnalogRF-IR internal documentation."""
        return (float(self.L_grid[0]), float(self.L_grid[-1]))

    # Internal implementation note.

    def _invert_gm_id(self, GM_ID: float, L: float) -> float:
        """AnalogRF-IR internal documentation."""
        vgs_per_L = np.zeros(self._M)
        for i in range(self._M):
            row = self.GM_ID[i, :]
            # Internal implementation note.
            vgs_per_L[i] = np.interp(GM_ID, row[::-1], self.VGS_grid[::-1])

        vgs = float(np.interp(L, self.L_grid, vgs_per_L))
        return np.clip(vgs, self.VGS_grid[0], self.VGS_grid[-1])

    def _interpolate(self, L: float, VGS: float,
                     gm_id_override: Optional[float] = None) -> Dict[str, float]:
        """AnalogRF-IR internal documentation."""
        i_L = max(0, np.searchsorted(self.L_grid, L) - 1)
        i_L = min(i_L, self._M - 2)

        j_V = max(0, np.searchsorted(self.VGS_grid, VGS) - 1)
        j_V = min(j_V, self._N - 2)

        L0, L1 = self.L_grid[i_L], self.L_grid[i_L + 1]
        V0, V1 = self.VGS_grid[j_V], self.VGS_grid[j_V + 1]

        wL = (L - L0) / (L1 - L0) if L1 > L0 else 0
        wV = (VGS - V0) / (V1 - V0) if V1 > V0 else 0

        def _bilinear(arr: np.ndarray) -> float:
            f00 = arr[i_L, j_V]
            f10 = arr[i_L + 1, j_V]
            f01 = arr[i_L, j_V + 1]
            f11 = arr[i_L + 1, j_V + 1]
            f0 = f00 + wV * (f01 - f00)
            f1 = f10 + wV * (f11 - f10)
            return float(f0 + wL * (f1 - f0))

        gm_id_val = gm_id_override if gm_id_override is not None else _bilinear(self.GM_ID)

        # Internal implementation note.
        id_w = _bilinear(self.ID_W)
        ic_val = 0.0  # Internal implementation note.

        return {
            "GM_ID": gm_id_val,
            "L": L,
            "ID_W": id_w,
            "GM_W": _bilinear(self.GM_W),
            "GDS_W": _bilinear(self.GDS_W),
            "VGS": VGS,
            "VDSAT": _bilinear(self.VDSAT),
            "CGG_W": _bilinear(self.CGG_W) if np.any(self.CGG_W) else 0.0,
            "CGS_W": _bilinear(self.CGS_W) if np.any(self.CGS_W) else 0.0,
            "CGD_W": _bilinear(self.CGD_W) if np.any(self.CGD_W) else 0.0,
            "FT": _bilinear(self.FT) if np.any(self.FT) else 0.0,
            "VTH": _bilinear(self.VTH) if np.any(self.VTH) else 0.0,
            "IC": ic_val,   # Internal implementation note.
        }

    # Internal implementation note.

    def summary(self) -> str:
        """AnalogRF-IR internal documentation."""
        lines = [
            f"LookupTable @ VDS={self.VDS:.2f}V, VSB={self.VSB:.2f}V",
            f"  L:    {self.L_grid[0]*1e9:.0f}nm ... {self.L_grid[-1]*1e9:.0f}nm ({self._M} pts)",
            f"  VGS:  {self.VGS_grid[0]:.3f}V ... {self.VGS_grid[-1]:.3f}V ({self._N} pts)",
            f"  GM_ID range: {self._gm_id_min:.1f} ... {self._gm_id_max:.1f} S/A",
        ]
        return "\n".join(lines)


# Internal implementation note.

def load_lookup_table(filename: str) -> LookupTable:
    """AnalogRF-IR internal documentation."""
    return LookupTable(filename)


def create_lookup_pair(nmos_path: str, pmos_path: str) -> Tuple[LookupTable, LookupTable]:
    """AnalogRF-IR internal documentation."""
    return LookupTable(nmos_path), LookupTable(pmos_path)

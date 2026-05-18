"""
LookupTable V1.0 — Boris Murmann 风格的 gm/ID 查找表。

基于预计算的 ngspice 仿真数据，提供从 (GM_ID, L) → 物理参数的
精确翻译。数据以 numpy .npz 格式存储，无需外部依赖（只用 numpy）。

核心 API：
    table = LookupTable("ptm130_nmos.npz")
    result = table.lookup(GM_ID=15, L=0.2e-6)
    # → {"ID_W": ..., "GM_W": ..., "VGS": ..., "VDSAT": ..., ...}

数据格式 (.npz)：
    L_grid    : (M,)   沟道长度扫描点 [m]
    VGS_grid  : (N,)   栅源电压扫描点 [V]
    VDS       : float  固定漏源电压 [V]
    VSB       : float  固定体源电压 [V]
    GM_ID     : (M,N)  gm/ID [S/A]
    ID_W      : (M,N)  ID/W [A/m]
    GM_W      : (M,N)  gm/W [S/m]
    GDS_W     : (M,N)  gds/W [S/m]
    VDSAT     : (M,N)  VDSAT [V]
    CGG_W     : (M,N)  Cgg/W [F/m]
    CGS_W     : (M,N)  Cgs/W [F/m]
    CGD_W     : (M,N)  Cgd/W [F/m]
    FT        : (M,N)  fT [Hz]
    VTH       : (M,N)  VTH [V] (可选)
"""

from __future__ import annotations

import numpy as np
from pathlib import Path
from typing import Dict, Optional, Tuple


class LookupTable:
    """gm/ID 查找表（与 Boris Murmann 的 pygmid 接口兼容）。"""

    def __init__(self, filename: str):
        """
        加载 .npz 查找表。

        Args:
            filename: .npz 文件路径
        """
        data = np.load(filename, allow_pickle=False)

        # 网格
        self.L_grid: np.ndarray = data["L_grid"]        # (M,)
        self.VGS_grid: np.ndarray = data["VGS_grid"]    # (N,)
        self.VDS: float = float(data["VDS"])
        self.VSB: float = float(data["VSB"])

        # 2D 数据: shape (M, N) — rows=L, cols=VGS
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

        # 预计算 GM_ID 的合法范围
        self._gm_id_min = float(np.nanmin(self.GM_ID))
        self._gm_id_max = float(np.nanmax(self.GM_ID))

    # ── 公共接口 ────────────────────────────────────────────

    def lookup(self, GM_ID: Optional[float] = None,
               L: Optional[float] = None,
               VGS: Optional[float] = None) -> Dict[str, float]:
        """
        查找 (GM_ID, L) 或 (VGS, L) 对应的归一化参数。

        两种模式：
        1. lookup(GM_ID=15, L=0.2e-6)  → 从 GM_ID 反推 VGS
        2. lookup(VGS=0.6, L=0.2e-6)   → 直接用 VGS

        Returns:
            dict with keys: ID_W, GM_W, GDS_W, VGS, VDSAT,
                            CGG_W, CGS_W, CGD_W, FT, VTH,
                            GM_ID, L (回显输入)
        """
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
        """通过 VGS 直接查找（别名）。"""
        return self.lookup(VGS=VGS, L=L)

    def forward(self, GM_ID: float, L: float, ID: float) -> Dict[str, float]:
        """
        正向翻译：(GM_ID, L, ID) → (W, VGS, VDSAT, gm, ...)

        Args:
            GM_ID: 目标 gm/ID [S/A]
            L: 沟道长度 [m]
            ID: 漏极电流 [A]

        Returns:
            dict with W, VGS, VDSAT, GM, GDS, CGS, CGD, CGG, FT, VTH
        """
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
        """返回 GM_ID 的有效范围。"""
        return (self._gm_id_min, self._gm_id_max)

    @property
    def L_range(self) -> Tuple[float, float]:
        """返回 L 的有效范围。"""
        return (float(self.L_grid[0]), float(self.L_grid[-1]))

    # ── 内部插值 ────────────────────────────────────────────

    def _invert_gm_id(self, GM_ID: float, L: float) -> float:
        """
        对于给定 L，在 VGS 轴上反求 GM_ID → VGS。

        GM_ID(VGS) 在饱和区通常单调递减。
        对每行独立反插，再沿 L 插值。
        """
        vgs_per_L = np.zeros(self._M)
        for i in range(self._M):
            row = self.GM_ID[i, :]
            # 反转 VGS 与 GM_ID 顺序确保单调递增
            vgs_per_L[i] = np.interp(GM_ID, row[::-1], self.VGS_grid[::-1])

        vgs = float(np.interp(L, self.L_grid, vgs_per_L))
        return np.clip(vgs, self.VGS_grid[0], self.VGS_grid[-1])

    def _interpolate(self, L: float, VGS: float,
                     gm_id_override: Optional[float] = None) -> Dict[str, float]:
        """
        在 (L, VGS) 点双线性插值所有参数。
        """
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

        # 反型系数 IC ≈ ID_W / I0  (需要工艺参数; 填占位值, adapter 会覆盖)
        id_w = _bilinear(self.ID_W)
        ic_val = 0.0  # 由 adapter 调用方设置，此处先占位

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
            "IC": ic_val,   # 反型系数 — adapter 层负责计算
        }

    # ── 诊断 ────────────────────────────────────────────────

    def summary(self) -> str:
        """返回查找表的可读摘要。"""
        lines = [
            f"LookupTable @ VDS={self.VDS:.2f}V, VSB={self.VSB:.2f}V",
            f"  L:    {self.L_grid[0]*1e9:.0f}nm ... {self.L_grid[-1]*1e9:.0f}nm ({self._M} pts)",
            f"  VGS:  {self.VGS_grid[0]:.3f}V ... {self.VGS_grid[-1]:.3f}V ({self._N} pts)",
            f"  GM_ID range: {self._gm_id_min:.1f} ... {self._gm_id_max:.1f} S/A",
        ]
        return "\n".join(lines)


# ── 工厂函数 ────────────────────────────────────────────────

def load_lookup_table(filename: str) -> LookupTable:
    """便捷工厂：从文件加载查找表。"""
    return LookupTable(filename)


def create_lookup_pair(nmos_path: str, pmos_path: str) -> Tuple[LookupTable, LookupTable]:
    """加载 NMOS/PMOS 查找表对。"""
    return LookupTable(nmos_path), LookupTable(pmos_path)

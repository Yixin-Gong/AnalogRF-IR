"""
pygmid 适配器 V2.0 — Boris Murmann 风格的 gm/ID LookupTable 后端。

职责：
1. 将 (gm_id, L, id) 翻译为 (W, VGS, VDSAT, gm, ...)  — forward
2. 将仿真结果反求为物理 gm_id                            — backward
3. 提供工艺相关的归一化参数查询                           — lookup

架构原则（保持不变）：
  - 这是"物理执行层"的确定性脚本
  - Agent 不直接调用查找表，而是通过 Schema 间接驱动
  - forward/backward/lookup 接口保持不变

V2.0 变更：
  - 删除 MockPygmid（平方律近似）和 RealPygmid（旧导入尝试）
  - 使用 Boris Murmann 风格的 LookupTable + 双线性插值
  - 所有参数来自 BSIM4 预仿真 .npz 查找表
  - 无查找表时自动降级为基础分析模型
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, Optional, Tuple, Any

try:
    from pygmid.lookup import LookupTable
except ImportError:
    from lookup import LookupTable  # 直接执行时的 fallback


# ── 基础分析模型（查找表不可用时的降级）─────────────────

class _AnalyticalFallback:
    """
    基于平方律 + 亚阈值修正的分析模型。
    仅在查找表文件不存在时使用，精度远低于 BSIM4 查找表。
    """

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

        # 反型系数 IC 估算: 从 VOV 反推 (平方律)
        # VOV ≈ 2*UT*sqrt(IC) for strong inversion
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


# ── Boris Murmann PygmidAdapter ────────────────────────────

class PygmidAdapter:
    """
    Boris Murmann 风格的 gm/ID 适配器。

    使用预计算的 .npz 查找表进行精确的 (gm_id, L) → (W, VGS, ...) 翻译。
    查找表不可用时自动降级为分析模型。

    Usage:
        adapter = PygmidAdapter(nmos_table="tables/ptm130_nmos.npz",
                                pmos_table="tables/ptm130_pmos.npz")
        result = adapter.forward(gm_id=15, L=0.2e-6, id_target=10e-6, device_type="nmos")
    """

    def __init__(self,
                 nmos_table: Optional[str] = None,
                 pmos_table: Optional[str] = None,
                 tables_dir: Optional[str] = None):
        """
        Args:
            nmos_table: NMOS .npz 查找表路径
            pmos_table: PMOS .npz 查找表路径
            tables_dir: 查找表目录（若未指定 nmos_table/pmos_table，在此目录下搜索）
        """
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
        """在 tables_dir 下搜索 *_nmos.npz 和 *_pmos.npz。"""
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
        """从 ID_W 计算反型系数 IC = ID_W / I0。

        I0 = 2 * n * μ * Cox * UT² 使用默认 PTM 130nm 参数。
        """
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

    # ── 核心接口（兼容旧 adapter.py）─────────────────────

    def forward(self, gm_id: float, L: float, id_target: float,
                device_type: str = "nmos") -> Dict[str, float]:
        """
        正向翻译：(gm_id, L, id_target) → (W, vgs, vdsat, gm, gds, ft, cgs, cgd, vov, id_w)

        使用查找表时，流程为：
          1. 从 GM_ID 反推 VGS
          2. 双线性插值所有归一化参数
          3. W = id_target / ID_W
        """
        table = self._get_table(device_type)

        if table is not None:
            try:
                result = table.lookup(GM_ID=gm_id, L=L)
                id_w = max(result["ID_W"], 1e-15)
                W = id_target / id_w
                W = max(W, 1e-9)  # 最小 1nm

                cgs_w = result.get("CGS_W", 0) or 0
                cgd_w = result.get("CGD_W", 0) or 0
                cgg_w = result.get("CGG_W", 0) or 0
                vth = result.get("VTH", self._get_fallback(device_type).VTH)

                # 计算反型系数 IC = ID_W / I0
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
        """
        反向计算：从仿真结果反求 gm_id_realized 和工作区。

        使用查找表时：通过 VGS 和 L 查表得出 GM_ID。
        """
        table = self._get_table(device_type)

        if table is not None and id_val > 1e-15:
            try:
                # 从 VGS 反查 — 但我们不知道 L...
                # 实际用法中，backward 通常在仿真后调用，此时已知所有参数
                # 简化：直接用 gm/id_val
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
        """
        纯查表：给定 (gm_id, L) → 归一化参数。

        Returns:
            dict with id_over_w, gm_over_w, gm_gds, vov, ft_approx
        """
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
        """
        获取工艺参数 (VTH, KP, LAMBDA, COX) — 兼容旧接口。

        查找表可用时，从表中取 L 中点的典型值；否则用 fallback。
        """
        fallback = self._get_fallback(device_type)
        table = self._get_table(device_type)

        if table is not None:
            try:
                mid_L = table.L_grid[len(table.L_grid) // 2]
                mid_result = table.lookup(GM_ID=10, L=mid_L)
                vth = mid_result.get("VTH", fallback.VTH)
                # 从 ID_W 估算 KP: ID = 0.5 * KP * (W/L) * vov^2
                id_w = mid_result["ID_W"]
                vov = mid_result["VGS"] - vth
                if vov > 0.01 and id_w > 0:
                    kp_est = 2.0 * id_w * mid_L / (vov ** 2)
                else:
                    kp_est = fallback.KP
                # gm_gds → LAMBDA
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
        """返回适配器状态摘要。"""
        lines = ["PygmidAdapter V2.0 (Boris Murmann style)"]
        lines.append(f"  NMOS: {'LookupTable' if self._nmos else 'Analytical fallback'}")
        if self._nmos:
            lines.append(f"    {self._nmos.summary()}")
        lines.append(f"  PMOS: {'LookupTable' if self._pmos else 'Analytical fallback'}")
        if self._pmos:
            lines.append(f"    {self._pmos.summary()}")
        return "\n".join(lines)


# ── 工厂函数 ────────────────────────────────────────────────

def create_pygmid_adapter(nmos_path: Optional[str] = None,
                          pmos_path: Optional[str] = None,
                          tables_dir: Optional[str] = None) -> PygmidAdapter:
    """
    工厂函数：创建 PygmidAdapter 实例。

    Args:
        nmos_path: NMOS .npz 查找表路径
        pmos_path: PMOS .npz 查找表路径
        tables_dir: 查找表目录（自动发现）

    Returns:
        PygmidAdapter 实例
    """
    return PygmidAdapter(nmos_table=nmos_path, pmos_table=pmos_path, tables_dir=tables_dir)

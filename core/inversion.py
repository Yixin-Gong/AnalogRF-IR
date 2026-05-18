"""
反型系数分析模块 V1.0 — gm/ID 方法学的反型层语义分析。

核心概念:
  I0 = 2 * n * μ * Cox * UT² / L   (工艺特定电流, A/m)
  IC = ID / (I0 * W)               (反型系数, 无量纲)

  弱反型 (WI):   IC < 0.1
  中等反型 (MI): 0.1 ≤ IC ≤ 10
  强反型 (SI):   IC > 10

  gm/ID 与 IC 的近似关系 (EKV 模型):
    gm/ID ≈ 1 / (n * UT * (1 + sqrt(1 + 4*IC)) / (2*IC))
    反向: IC ≈ 1 / ((n*UT * gm/ID - 1)² / 2 - 1)

用法:
    from core.inversion import InversionAnalyzer, ic_to_region
    analyzer = InversionAnalyzer()
    ic = analyzer.compute_IC_from_gm_id(gm_id=15, device_type="nmos", proc=process_info)
    region = ic_to_region(ic)  # "moderate"
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# 热电压 @ 27°C
UT_300K = 0.02585  # kT/q [V]


@dataclass
class InversionResult:
    """单个晶体管的反型层分析结果。"""
    device_id: str
    ic: float                          # 反型系数
    region: str                        # weak | moderate | strong | unknown
    gm_id: float                       # gm/ID [S/A]
    vgs: float                         # 栅源电压 [V]
    vth: float                         # 阈值电压 [V]
    vov: float                         # 过驱动电压 [V]
    description: str = ""              # 人类可读描述


# ── 核心计算 ────────────────────────────────────────────────

def compute_I0(device_type: str, L: float, proc) -> float:
    """
    计算工艺特定电流 I0 [A/m] — 从工艺参数直接计算。

    公式: I0 = 2 * n * μ * Cox * UT²

    Args:
        device_type: "nmos" or "pmos"
        L: 沟道长度 [m] (I0 本身不依赖 L，此处保留接口)
        proc: ProcessInfo dataclass

    Returns:
        I0 [A/m]
    """
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
    """
    从 gm/ID 近似计算反型系数 IC (EKV 模型逆推)。

    EKV 公式:
      gm/ID ≈ 1 / (n*UT * (1 + sqrt(1+4*IC)) / (2*IC))

    反解:
      设 x = n*UT * gm/ID
      IC ≈ 4 * IC . 通过迭代求解:
        IC ≈ 1 / (x * (1 + sqrt(1+4*IC))/ (2*IC) - 1) 的逆形式

    简化逼近 (Murmann 经验公式):
      IC ≈ 1 / (exp((gm/ID_max - gm/ID) / (n*UT)) - 1)  不适合强反型

    数值稳定方案 (二分法求根):
      解 f(IC) = 1/(n*UT * (1+sqrt(1+4*IC))/(2*IC)) - gm/ID = 0

    Args:
        gm_id: gm/ID [S/A]
        n: 亚阈值斜率因子
        ut: 热电压 [V]

    Returns:
        IC (反型系数)
    """
    if gm_id <= 0:
        return 0.0

    # 对于极低 gm/ID (强反型), 用平方律近似:
    # gm/ID ≈ 2/VOV, VOV = 2*UT*sqrt(IC)
    # IC ≈ (n * gm_id * UT)⁻² — 不够精确, 用数值解

    x = n * ut * gm_id

    # 弱反型近似: IC → 0, gm/ID → 1/(n*UT)
    if x >= 0.999:
        # 弱反型/亚阈值区域，用解析近似
        return 1.0 / (math.exp(1.0 / (1.0 - x)) - 1.0) if x < 0.9999 else 0.01

    # 中等-强反型: 二分法求 IC ∈ [1e-4, 1e6]
    lo, hi = 1e-4, 1e6
    for _ in range(60):
        mid = (lo + hi) / 2.0
        # EKV: gm/ID = 2 / (n*UT * (1 + sqrt(1 + 4*IC)))
        # 注意分子是 2, 不是 2*IC
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
    """
    从 ID/W 直接计算 IC。

    公式: IC = ID / (W * I0) = ID_W / I0
    其中 I0 = 2*n*μ*Cox*UT² (与 L 无关，是 per-square 量)

    Args:
        id_w: ID/W [A/m]
        device_type: "nmos" or "pmos"
        proc: ProcessInfo

    Returns:
        IC
    """
    i0 = compute_I0(device_type, 1e-6, proc)  # L 不参与 I0
    return id_w / i0 if i0 > 0 else 0.0


# ── 反型层分类 ──────────────────────────────────────────────

def ic_to_region(ic: float) -> str:
    """
    将反型系数 IC 映射为反型层字符串。

    分类:
      IC < 0.1        → "weak"       (弱反型 / 亚阈值)
      0.1 ≤ IC ≤ 10   → "moderate"   (中等反型)
      IC > 10         → "strong"     (强反型)

    Args:
        ic: 反型系数

    Returns:
        "weak" | "moderate" | "strong" | "unknown"
    """
    if ic < 0:
        return "unknown"
    if ic < 0.1:
        return "weak"
    if ic <= 10:
        return "moderate"
    return "strong"


def ic_to_description(ic: float, gm_id: Optional[float] = None) -> str:
    """
    返回反型层的详细描述。

    Args:
        ic: 反型系数
        gm_id: 可选 gm/ID 用于补充描述

    Returns:
        人类可读描述
    """
    region = ic_to_region(ic)
    parts = [f"IC={ic:.3f}"]

    if region == "weak":
        parts.append("弱反型 (亚阈值)")
        parts.append("高增益效率, 低带宽, 噪声大")
    elif region == "moderate":
        parts.append("中等反型")
        parts.append("增益-带宽平衡区")
    elif region == "strong":
        parts.append("强反型")
        parts.append("高带宽, 高线性度, 低增益效率")
    else:
        parts.append("未知反型层")

    if gm_id is not None:
        parts.append(f"gm/ID={gm_id:.1f}")

    return " — ".join(parts)


# ── 综合分析 ────────────────────────────────────────────────

class InversionAnalyzer:
    """
    反型层分析器 — 对单管或整个设计状态进行反型系数分析。

    Usage:
        analyzer = InversionAnalyzer()
        result = analyzer.analyze_transistor(ts, state.process, device_type="nmos")
        print(result.region, result.ic)
    """

    def __init__(self, n_default: float = 1.4):
        self.n_default = n_default

    def analyze_transistor(self, ts, proc,
                           device_type: Optional[str] = None,
                           vth: Optional[float] = None) -> InversionResult:
        """
        对单个 TransistorState 进行反型层分析。

        优先使用仿真回填的 gm_id_realized 和 vgs/vth，
        若不可用则使用 strategy 值。

        Args:
            ts: TransistorState
            proc: ProcessInfo
            device_type: 覆盖器件类型 (默认从 ts.type 推断)
            vth: 覆盖阈值电压 (默认从 proc 取)

        Returns:
            InversionResult
        """
        p = ts.parameters
        dtype = device_type or ts.type or "nmos"

        # 确定 VTH
        if vth is None:
            vth = proc.VTH_n if dtype in ("nmos", "nch_18") else proc.VTH_p

        # 优先使用仿真值
        gm_id = p.gm_id_realized if p.gm_id_realized > 0 else ts.gm_id_strategy
        vgs = p.vgs if p.vgs > 0 else 0.0

        # 选取 n 因子
        n = (proc.n_sub_nmos if dtype in ("nmos", "nch_18")
             else proc.n_sub_pmos)

        # 计算 IC
        ic = compute_IC_from_gm_id(gm_id, n)

        # 也可从 ID_W 验证
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
        """
        分析设计状态中所有晶体管。

        Args:
            state: DesignState
            by_role: 按 role 排序输出

        Returns:
            Dict[device_id, InversionResult]
        """
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
            # 保持插入顺序但分组
            sorted_results = {}
            for dev in state.topology.devices:
                if dev.id in results:
                    sorted_results[dev.id] = results[dev.id]
            return sorted_results

        return results

    def summary_table(self, state) -> str:
        """
        生成所有晶体管的反型层摘要表。

        Args:
            state: DesignState

        Returns:
            格式化的字符串表格
        """
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
                note = "深亚阈值"
            elif r.ic > 50:
                note = "深强反型"
            elif 0.5 < r.ic < 2:
                note = "最优效率区"

            lines.append(
                f"{dev_id:<6} {role:<22} {dtype:<5} "
                f"{r.gm_id:<8.1f} {r.ic:<10.3f} {r.region:<12} {note}"
            )

        return "\n".join(lines)

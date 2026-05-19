"""
设计规则库 V2.0 — 25 个实用的模拟IC约束函数。

全部用 @register_rule 注册，Agent 可通过 get_rule(name) 调用。
函数分类:
  A. 几何/工艺设计规则 (7)
  B. 面积与版图 (2)
  C. 匹配与对称 (4)
  D. 偏置与工作区 (5)
  E. 性能达标 (2)
  F. 搜索空间 (3)
  G. 可靠性 (2)

每个函数签名: (state: DesignState) -> ValidationReport
"""

from __future__ import annotations

import math
from typing import Optional

from core.regions import SPICE_OPERATING_REGIONS
from schemas.design_state import DesignState, ProcessInfo, TransistorParameters
from core.rule_registry import register_rule, ValidationReport, DiagnosisResult


# ═══════════════════════════════════════════════════════════════
#  A. 几何/工艺设计规则 (DRC)
# ═══════════════════════════════════════════════════════════════

@register_rule("check_min_width", layer=4,
               description="W >= process.min_W 对所有晶体管")
def check_min_width(state: DesignState) -> ValidationReport:
    """检查所有晶体管宽度不小于工艺最小宽度。"""
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        W = ts.parameters.W or 0
        if W <= 0:
            continue
        if W < proc.min_W * 0.999:
            report.add(DiagnosisResult(
                check_name="dr:min_width", passed=False, severity="error",
                message=f"{tid}: W={W:.2e}m < min_W={proc.min_W:.2e}m",
                layer=4, device=tid,
                details={"W": W, "min_W": proc.min_W}
            ))
    return report


@register_rule("check_max_width", layer=4,
               description="W <= process.max_W 对所有晶体管")
def check_max_width(state: DesignState) -> ValidationReport:
    """检查所有晶体管宽度不超过工艺最大单指宽度。"""
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        W = ts.parameters.W or 0
        if W > proc.max_W:
            report.add(DiagnosisResult(
                check_name="dr:max_width", passed=False, severity="warning",
                message=f"{tid}: W={W*1e6:.1f}um > max_W={proc.max_W*1e6:.1f}um — "
                        f"consider finger decomposition",
                layer=4, device=tid
            ))
    return report


@register_rule("check_min_length", layer=4,
               description="L >= process.min_L 对所有晶体管")
def check_min_length(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        L_val = ts.L_strategy or ts.parameters.L or 0
        if L_val <= 0:
            continue
        if L_val < proc.min_L * 0.999:
            report.add(DiagnosisResult(
                check_name="dr:min_length", passed=False, severity="error",
                message=f"{tid}: L={L_val*1e9:.1f}nm < min_L={proc.min_L*1e9:.1f}nm",
                layer=4, device=tid
            ))
    return report


@register_rule("check_max_length", layer=4,
               description="L <= process.max_L 对所有晶体管")
def check_max_length(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        L_val = ts.L_strategy or ts.parameters.L or 0
        if L_val > proc.max_L:
            report.add(DiagnosisResult(
                check_name="dr:max_length", passed=False, severity="warning",
                message=f"{tid}: L={L_val*1e6:.1f}um > max_L={proc.max_L*1e6:.1f}um",
                layer=4, device=tid
            ))
    return report


@register_rule("check_W_precision", layer=4,
               description="W 是 process.W_precision 的整数倍")
def check_W_precision(state: DesignState) -> ValidationReport:
    """检查 W 符合工艺网格精度。"""
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        W = ts.parameters.W or 0
        if W <= 0 or proc.W_precision <= 0:
            continue
        steps = W / proc.W_precision
        if abs(steps - round(steps)) > 0.01:
            report.add(DiagnosisResult(
                check_name="dr:W_precision", passed=False, severity="warning",
                message=f"{tid}: W={W*1e6:.4f}um not on {proc.W_precision*1e9:.1f}nm grid",
                layer=4, device=tid
            ))
    return report


@register_rule("check_L_precision", layer=4,
               description="L 是 process.L_precision 的整数倍")
def check_L_precision(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        L_val = ts.L_strategy or ts.parameters.L or 0
        if L_val <= 0 or proc.L_precision <= 0:
            continue
        steps = L_val / proc.L_precision
        if abs(steps - round(steps)) > 0.01:
            report.add(DiagnosisResult(
                check_name="dr:L_precision", passed=False, severity="warning",
                message=f"{tid}: L={L_val*1e9:.2f}nm not on {proc.L_precision*1e9:.1f}nm grid",
                layer=4, device=tid
            ))
    return report


@register_rule("check_W_L_ratio", layer=4,
               description="W/L 在 [min_W_L_ratio, max_W_L_ratio] 范围内")
def check_W_L_ratio(state: DesignState) -> ValidationReport:
    """检查宽长比在合理范围，防止极端尺寸。"""
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        W = ts.parameters.W or 0
        L_val = ts.L_strategy or ts.parameters.L or 1e-15
        if W <= 0 or L_val <= 0:
            continue
        ratio = W / L_val
        if ratio > proc.max_W_L_ratio:
            report.add(DiagnosisResult(
                check_name="dr:W_L_ratio", passed=False, severity="warning",
                message=f"{tid}: W/L={ratio:.1f} > max={proc.max_W_L_ratio:.0f}",
                layer=4, device=tid
            ))
        elif ratio < proc.min_W_L_ratio:
            report.add(DiagnosisResult(
                check_name="dr:W_L_ratio", passed=False, severity="info",
                message=f"{tid}: W/L={ratio:.3f} < min={proc.min_W_L_ratio}",
                layer=4, device=tid
            ))
    return report


# ═══════════════════════════════════════════════════════════════
#  B. 面积与版图
# ═══════════════════════════════════════════════════════════════

@register_rule("check_min_area", layer=4,
               description="W*L >= process.min_area 对所有晶体管")
def check_min_area(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        W = ts.parameters.W or 0
        L_val = ts.L_strategy or ts.parameters.L or 0
        area = W * L_val
        if area <= 0:
            continue
        if area < proc.min_area * 0.99:
            report.add(DiagnosisResult(
                check_name="dr:min_area", passed=False, severity="error",
                message=f"{tid}: area={area:.2e}m² < min={proc.min_area:.2e}m²",
                layer=4, device=tid
            ))
    return report


@register_rule("check_finger_width", layer=4,
               description="W > max_finger_width 时建议分叉指")
def check_finger_width(state: DesignState) -> ValidationReport:
    """检查是否需要叉指分解（版图建议）。"""
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        W = ts.parameters.W or 0
        if W > proc.max_finger_width:
            nf = math.ceil(W / proc.max_finger_width)
            report.add(DiagnosisResult(
                check_name="dr:finger_width", passed=False, severity="info",
                message=f"{tid}: W={W*1e6:.1f}um > {proc.max_finger_width*1e6:.0f}um/finger "
                        f"→ suggest m={nf} fingers",
                layer=4, device=tid, details={"nf": nf}
            ))
    return report


# ═══════════════════════════════════════════════════════════════
#  C. 匹配与对称
# ═══════════════════════════════════════════════════════════════

PAIR_TOLERANCE = 0.05  # 5% 默认容差


@register_rule("check_pair_W_mismatch", layer=4,
               description="对管 W 偏差 < 5%")
def check_pair_W_mismatch(state: DesignState) -> ValidationReport:
    return _check_pair_param(state, "W", PAIR_TOLERANCE, "W", lambda p: p.W)


@register_rule("check_pair_L_mismatch", layer=4,
               description="对管 L 偏差 < 5%")
def check_pair_L_mismatch(state: DesignState) -> ValidationReport:
    return _check_pair_param(state, "L", PAIR_TOLERANCE, "L",
                              lambda p, ts: ts.L_strategy or p.L)


@register_rule("check_pair_gm_mismatch", layer=4,
               description="对管 gm 偏差 < 5%")
def check_pair_gm_mismatch(state: DesignState) -> ValidationReport:
    return _check_pair_param(state, "gm", PAIR_TOLERANCE, "gm", lambda p: p.gm)


def _check_pair_param(state: DesignState, param_name: str, tolerance: float,
                       label: str, getter) -> ValidationReport:
    """通用对管参数偏差检查。"""
    report = ValidationReport()
    # 按 role 分组
    role_groups = {}
    for dev in state.topology.devices:
        role_groups.setdefault(dev.role, []).append(dev.id)
    for role, dev_ids in role_groups.items():
        if len(dev_ids) < 2:
            continue
        for i in range(len(dev_ids)):
            for j in range(i + 1, len(dev_ids)):
                a, b = dev_ids[i], dev_ids[j]
                if a not in state.transistors or b not in state.transistors:
                    continue
                pa = state.transistors[a].parameters
                pb = state.transistors[b].parameters
                tsa = state.transistors[a]
                tsb = state.transistors[b]
                va = getter(pa, tsa) if "L" in param_name else getter(pa)
                vb = getter(pb, tsb) if "L" in param_name else getter(pb)
                if va <= 0 or vb <= 0:
                    continue
                dev = abs(va - vb) / max(va, vb)
                if dev > tolerance:
                    report.add(DiagnosisResult(
                        check_name=f"dr:pair_{param_name}_mismatch",
                        passed=False, severity="warning",
                        message=f"{a}/{b} ({role}) {label}: {va:.4e} vs {vb:.4e} "
                                f"(deviation={dev*100:.1f}%)",
                        layer=4, device=f"{a}/{b}",
                        details={"deviation": dev}
                    ))
    return report


@register_rule("check_current_mirror_ratio", layer=4,
               description="电流镜 W 比例在约束范围内")
def check_current_mirror_ratio(state: DesignState) -> ValidationReport:
    """检查电流镜管之间的 W 比例是否合理（1:1 ~ 1:10）。"""
    report = ValidationReport()
    mirror_groups = {}
    for dev in state.topology.devices:
        if "current_mirror" in dev.role or "mirror" in dev.role.lower():
            # 按 gate 连接分组
            gate_net = dev.connections.get("gate", "")
            mirror_groups.setdefault(gate_net, []).append(dev.id)
    for gate_net, dev_ids in mirror_groups.items():
        if len(dev_ids) < 2:
            continue
        # 取所有 W 值，检查比例
        widths = []
        for did in dev_ids:
            ts = state.transistors.get(did)
            if ts and ts.parameters.W > 0:
                widths.append((did, ts.parameters.W))
        if len(widths) < 2:
            continue
        _, ref_w = widths[0]
        for did, W in widths[1:]:
            ratio = W / ref_w if ref_w > 0 else 0
            if ratio > 10 or ratio < 0.1:
                report.add(DiagnosisResult(
                    check_name="dr:mirror_ratio", passed=False, severity="info",
                    message=f"Current mirror {did}/{widths[0][0]}: W ratio={ratio:.1f} "
                            f"(extreme, check intentional)",
                    layer=4, device=did
                ))
    return report


# ═══════════════════════════════════════════════════════════════
#  D. 偏置与工作区
# ═══════════════════════════════════════════════════════════════

@register_rule("check_saturation_margin", layer=4,
               description="VDS >= VDSAT * headroom_factor 对有源器件")
def check_saturation_margin(state: DesignState) -> ValidationReport:
    """检查关键器件是否在饱和区，使用工艺定义的 headroom factor。"""
    report = ValidationReport()
    proc = state.process
    factor = proc.VDSAT_headroom_factor
    exempt = {"current_mirror_load"}  # diode-connected 豁免
    for tid, ts in state.transistors.items():
        p = ts.parameters
        if p.region == "unknown" or p.vdsat <= 0:
            continue
        dev_def = state.get_device_def(tid)
        role = dev_def.role if dev_def else ""
        if role in exempt:
            continue
        if p.vds < p.vdsat * factor:
            report.add(DiagnosisResult(
                check_name="dr:saturation_margin", passed=False, severity="warning",
                message=f"{tid} ({role}): vds={p.vds:.3f}V < {factor}*vdsat={p.vdsat*factor:.3f}V — "
                        f"not in saturation",
                layer=4, device=tid,
                details={"vds": p.vds, "vdsat": p.vdsat, "factor": factor}
            ))
    return report


@register_rule("check_region_validity", layer=4,
               description="region 字段是有效 SPICE 工作区值且非豁免器件在饱和区")
def check_region_validity(state: DesignState) -> ValidationReport:
    """检查所有晶体管的 region 字段：
    1. 是有效 SPICE 工作区值
    2. 非豁免器件必须在 saturation 区（或 subthreshold 亚阈值区）
    3. unknown 表示仿真未正确回填
    """
    report = ValidationReport()
    SATURATION_OK = {"saturation", "subthreshold"}
    EXEMPT = {"current_mirror_load"}
    for tid, ts in state.transistors.items():
        p = ts.parameters
        dev_def = state.get_device_def(tid)
        role = dev_def.role if dev_def else ""

        # 0. 有效性
        if p.region not in SPICE_OPERATING_REGIONS:
            report.add(DiagnosisResult(
                check_name="dr:region_valid", passed=False, severity="error",
                message=f"{tid}: region='{p.region}' is not a valid SPICE operating region",
                layer=4, device=tid
            ))
            continue

        # 1. unknown → 仿真回填Bug
        if p.region == "unknown":
            report.add(DiagnosisResult(
                check_name="dr:region_valid", passed=False, severity="warning",
                message=f"{tid}: region='unknown' — simulation may not have properly "
                        f"back-filled operating region",
                layer=4, device=tid
            ))
            continue

        # 2. 豁免器件不检查
        if role in EXEMPT:
            continue

        # 3. 非豁免，必须在饱和区或亚阈值区
        if p.region not in SATURATION_OK:
            report.add(DiagnosisResult(
                check_name="dr:region_saturation", passed=False,
                severity="warning" if p.region == "linear" else "error",
                message=f"{tid} ({role}): region='{p.region}' — expected saturation, "
                        f"vds={p.vds:.3f}V, vdsat={p.vdsat:.3f}V",
                layer=4, device=tid,
                details={"region": p.region, "vds": p.vds, "vdsat": p.vdsat}
            ))

    return report


# ── 按 role 分级的饱和深度 ──────────────────────────────────

# 默认裕度映射: role → 最小 VDS-VDSAT 余量 [V]
_SATURATION_DEPTH_MARGIN: dict = {
    "input_pair":         0.08,   # 差分对: 80mV 裕度
    "cascode":            0.20,   # cascode: 需要深饱和
    "tail_current_source": 0.05,  # 尾电流源: 压摆有限
    "current_mirror_load": 0.0,   # diode-connected 豁免
}
_DEFAULT_DEPTH_MARGIN = 0.05  # 未明确 role 的默认余量


@register_rule("check_saturation_depth", layer=4,
               description="按器件 role 分级检查 VDS-VDSAT 饱和深度")
def check_saturation_depth(state: DesignState) -> ValidationReport:
    """对每个非豁免器件，检查 VDS 超出 VDSAT 的裕度是否达到其 role 的要求。

    diode-connected (current_mirror_load) 器件 VDS = VGS，豁免。
    其他器件按 role 查表，无匹配则用默认 50mV。

    与 check_saturation_margin (乘性因子) 互补 — 本规则使用绝对电压裕度。
    """
    report = ValidationReport()
    for tid, ts in state.transistors.items():
        p = ts.parameters
        if p.vdsat <= 0 or p.region == "unknown":
            continue
        dev_def = state.get_device_def(tid)
        role = dev_def.role if dev_def else ""
        if role in ("current_mirror_load",):
            continue  # diode-connected 豁免

        margin_v = p.vds - p.vdsat
        required = _SATURATION_DEPTH_MARGIN.get(role, _DEFAULT_DEPTH_MARGIN)
        if required <= 0:
            continue

        if margin_v < required:
            report.add(DiagnosisResult(
                check_name="dr:saturation_depth", passed=False,
                severity="warning",
                message=f"{tid} ({role}): VDS-VDSAT={margin_v*1e3:.0f}mV "
                        f"< required {required*1e3:.0f}mV — marginal saturation",
                layer=4, device=tid,
                details={"vds": p.vds, "vdsat": p.vdsat,
                         "margin": margin_v, "required": required}
            ))
    return report


# ── 反型层约束 ──────────────────────────────────────────────

# 按 role 的反型层期望: (IC_min, IC_max, 描述)
# 来自 gm/ID 设计方法学:
#   WI (弱反型):  IC < 0.1, 高增益效率, 低带宽
#   MI (中等反型): 0.1 ≤ IC ≤ 10, 增益-带宽平衡
#   SI (强反型):  IC > 10, 高带宽, 低增益效率
_INVERSION_EXPECTATION: dict = {
    "input_pair":         (0.3, 8.0,   "中等反型中段 — 增益带宽平衡"),
    "current_mirror_load": (2.0, 50.0, "中强反型 — 电流复制精度高"),
    "tail_current_source": (5.0, 100.0, "强反型 — 电流稳定, 高输出阻抗"),
    "cascode":            (5.0, 100.0, "强反型 — 高本征增益"),
}


@register_rule("check_inversion_region", layer=4,
               description="按器件 role 检查反型系数 IC 是否在期望范围内")
def check_inversion_region(state: DesignState) -> ValidationReport:
    """使用 InversionAnalyzer 计算每个晶体管的 IC，检查是否落在 role 期望区间。

    本规则需要 ProcessInfo 中的 n_sub_nmos / n_sub_pmos 参数（P0 已加入）。
    若仿真尚未回填物理参数，使用 strategy gm_id 做近似计算。
    """
    report = ValidationReport()
    try:
        from core.inversion import InversionAnalyzer
    except ImportError:
        report.add(DiagnosisResult(
            check_name="dr:inversion_region", passed=False, severity="error",
            message="core.inversion module not available — cannot compute IC",
            layer=4
        ))
        return report

    proc = state.process
    analyzer = InversionAnalyzer()

    for tid, ts in state.transistors.items():
        dev_def = state.get_device_def(tid)
        role = dev_def.role if dev_def else ""
        expectation = _INVERSION_EXPECTATION.get(role)
        if expectation is None:
            continue  # 无约束的 role 跳过

        ic_min, ic_max, desc = expectation
        result = analyzer.analyze_transistor(ts, proc)
        ic = result.ic

        if ic <= 0:
            report.add(DiagnosisResult(
                check_name="dr:inversion_region", passed=False, severity="warning",
                message=f"{tid} ({role}): IC not computable (no gm_id or id data)",
                layer=4, device=tid
            ))
            continue

        if ic < ic_min:
            report.add(DiagnosisResult(
                check_name="dr:inversion_region", passed=False,
                severity="warning",
                message=f"{tid} ({role}): IC={ic:.3f} < min={ic_min} — "
                        f"too weak (期望: {desc})",
                layer=4, device=tid,
                details={"ic": ic, "ic_min": ic_min, "ic_max": ic_max,
                         "gm_id": result.gm_id, "region": result.region}
            ))
        elif ic > ic_max:
            report.add(DiagnosisResult(
                check_name="dr:inversion_region", passed=False,
                severity="warning",
                message=f"{tid} ({role}): IC={ic:.3f} > max={ic_max} — "
                        f"too strong (期望: {desc})",
                layer=4, device=tid,
                details={"ic": ic, "ic_min": ic_min, "ic_max": ic_max,
                         "gm_id": result.gm_id, "region": result.region}
            ))

    return report


# ── 反型层一致性 ────────────────────────────────────────────

_IC_CONSISTENCY_TOLERANCE = 0.30  # 30% 偏差容忍


# ── 饱和失效诊断 ────────────────────────────────────────────


@register_rule("diagnose_saturation_failure", layer=4,
               description="饱和失效根因诊断 — 分析 VDS 不足的可能原因 (info only)")
def diagnose_saturation_failure(state: DesignState) -> ValidationReport:
    """对每个非豁免的饱和不足器件，推断可能的根因。

    本规则是纯诊断性的 (severity=info)，不做 pass/fail 判断。
    仅在 check_saturation_margin 或 check_saturation_depth 发现问题后才有输出。

    诊断策略 (按 role):
      tail_current_source: VDS_tail = VGS_input_pair，尾电流过大或 VDD 不足
      input_pair:         VDS_input = VDD − VDS_load − VDS_tail，负载压降过大
      cascode:            偏置电压可能不足，或上下管 W 比例失调
      current_mirror_load: VDS = VGS (diode)，本身不应饱和失效；若出问题检查镜像比
    """
    report = ValidationReport()
    proc = state.process

    # 计算 VDD 范围
    vdd = state.simulation.supply.get("vdd", proc.nominal_VDD)
    vss = state.simulation.supply.get("vss", 0.0)
    span = vdd - vss
    if span <= 0:
        return report

    # 先收集所有晶体管的 VDS/VDSAT 信息
    devices_info = {}
    for tid, ts in state.transistors.items():
        p = ts.parameters
        dev_def = state.get_device_def(tid)
        role = dev_def.role if dev_def else ""
        devices_info[tid] = {
            "vds": p.vds, "vdsat": p.vdsat, "region": p.region,
            "role": role, "type": dev_def.type if dev_def else "nmos",
            "margin": p.vds - p.vdsat,
        }

    # 对每个可能饱和不足的器件进行诊断
    for tid, info in devices_info.items():
        if info["region"] == "unknown" or info["vdsat"] <= 0:
            continue
        role = info["role"]
        if role in ("current_mirror_load",):
            continue  # diode 不需要饱和

        margin = info["margin"]

        # 使用 saturation_depth 中的裕度要求
        required = _SATURATION_DEPTH_MARGIN.get(role, _DEFAULT_DEPTH_MARGIN)
        if margin >= required:
            continue  # 裕度充足，无需诊断

        # ── 推断可能原因 ──
        causes = []

        # 通用检查: VDS/VDD 占比
        vds_ratio = info["vds"] / span if span > 0 else 0

        if role == "tail_current_source":
            if info["vdsat"] > 0.25:
                causes.append("VDSAT 偏高 → gm/ID 可能过于激进（偏强反型），试试降低 gm_id")
            if vds_ratio < 0.15:
                causes.append(f"VDS 仅占 VDD 的 {vds_ratio*100:.0f}%，"
                              "输入对管 VGS 占用过大 → 尾电流可能偏大")

        elif role == "input_pair":
            if info["vdsat"] > 0.35:
                causes.append("VDSAT 偏高 → L 太小或 gm/ID 偏强反型，考虑增大 L 或提高 gm_id")
            if vds_ratio < 0.20:
                causes.append(f"VDS 仅占 VDD 的 {vds_ratio*100:.0f}%，"
                              "负载管或尾电流管压降过大 → 检查 VDD 分配")

        elif role == "cascode":
            if info["vdsat"] > 0.3:
                causes.append("VDSAT 偏高 → cascode 器件通常用较大 L，检查 L 是否充足")
            causes.append("cascode VDS 不足 → 检查偏置电压和上下管 W 比例")

        else:
            if info["vdsat"] > 0.3:
                causes.append("VDSAT 偏高 → 考虑调整 gm/ID 或增大 L")
            if vds_ratio < 0.10:
                causes.append(f"VDS 仅占 VDD 的 {vds_ratio*100:.0f}%，"
                              "电压分配极端不均 → 检查堆叠结构")

        if not causes:
            causes.append("VDS 裕度不足 — 检查 VDD、各管 VDS 分配、gm/ID 策略")

        report.add(DiagnosisResult(
            check_name="dr:diagnose_saturation", passed=True, severity="info",
            message=f"{tid} ({role}): VDS-VDSAT={margin*1e3:.0f}mV "
                    f"(required {required*1e3:.0f}mV) → {'; '.join(causes)}",
            layer=4, device=tid,
            details={"vds": info["vds"], "vdsat": info["vdsat"],
                     "margin": margin, "vds_ratio": vds_ratio,
                     "causes": causes}
        ))

    # 全局诊断: VDD 堆叠利用率
    total_vds = sum(info["vds"] for info in devices_info.values()
                    if info["vds"] > 0 and info["role"] != "current_mirror_load")
    active_count = sum(1 for info in devices_info.values()
                       if info["vds"] > 0 and info["role"] != "current_mirror_load")
    if active_count > 0:
        avg_vds = total_vds / active_count
        if avg_vds < 0.15:
            report.add(DiagnosisResult(
                check_name="dr:diagnose_headroom", passed=True, severity="info",
                message=f"平均 VDS/管 = {avg_vds*1e3:.0f}mV (VDD={span:.2f}V) — "
                        "堆叠过于拥挤，考虑降低层数或提高 VDD",
                layer=4
            ))

    return report


@register_rule("check_inversion_consistency", layer=4,
               description="同 role 器件之间的 IC 偏差 < 30%")
def check_inversion_consistency(state: DesignState) -> ValidationReport:
    """验证同 role 器件（如差分对管）的反型系数一致。

    大偏差意味着匹配对管工作在不同的反型层 — 增益、带宽、噪声不匹配。
    """
    report = ValidationReport()
    try:
        from core.inversion import InversionAnalyzer
    except ImportError:
        return report

    proc = state.process
    analyzer = InversionAnalyzer()

    # 按 role 分组
    role_groups: dict = {}
    for dev in state.topology.devices:
        role_groups.setdefault(dev.role, []).append(dev.id)

    for role, dev_ids in role_groups.items():
        if len(dev_ids) < 2:
            continue

        # 计算每管的 IC
        ic_map = {}
        for did in dev_ids:
            ts = state.transistors.get(did)
            if ts is None:
                continue
            result = analyzer.analyze_transistor(ts, proc)
            if result.ic > 0:
                ic_map[did] = result.ic

        if len(ic_map) < 2:
            continue

        # 两两比较
        dev_list = list(ic_map.keys())
        for i in range(len(dev_list)):
            for j in range(i + 1, len(dev_list)):
                a, b = dev_list[i], dev_list[j]
                ica, icb = ic_map[a], ic_map[b]
                if ica <= 0 or icb <= 0:
                    continue
                dev_pct = abs(ica - icb) / max(ica, icb)
                if dev_pct > _IC_CONSISTENCY_TOLERANCE:
                    report.add(DiagnosisResult(
                        check_name="dr:inversion_consistency", passed=False,
                        severity="warning",
                        message=f"{a}/{b} ({role}): IC={ica:.3f} vs {icb:.3f} "
                                f"(deviation={dev_pct*100:.1f}%)",
                        layer=4, device=f"{a}/{b}",
                        details={"ic_a": ica, "ic_b": icb, "deviation": dev_pct}
                    ))

    return report


@register_rule("check_VGS_breakdown", layer=4,
               description="|VGS| <= max_VGS 防止栅氧击穿")
def check_VGS_breakdown(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        vgs = ts.parameters.vgs
        if vgs <= 0:
            continue
        if abs(vgs) > proc.max_VGS * 0.95:
            report.add(DiagnosisResult(
                check_name="dr:VGS_breakdown", passed=False, severity="error",
                message=f"{tid}: |VGS|={abs(vgs):.3f}V >= 0.95*max_VGS={proc.max_VGS*0.95:.3f}V",
                layer=4, device=tid
            ))
    return report


@register_rule("check_VDS_reliability", layer=4,
               description="VDS 在安全范围内 (VDS + safe_margin < max_VDS)")
def check_VDS_reliability(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        vds = ts.parameters.vds
        if vds <= 0:
            continue
        if abs(vds) > proc.max_VDS - proc.VDS_safe_margin:
            report.add(DiagnosisResult(
                check_name="dr:VDS_reliability", passed=False, severity="warning",
                message=f"{tid}: VDS={abs(vds):.3f}V exceeds safe limit "
                        f"({proc.max_VDS - proc.VDS_safe_margin:.3f}V)",
                layer=4, device=tid
            ))
    return report


@register_rule("check_VGS_safe_range", layer=4,
               description="VGS 在 VTH+50mV ~ VTH+800mV 安全范围")
def check_VGS_safe_range(state: DesignState) -> ValidationReport:
    """VGS 太低 → 深亚阈值噪声大；太高 → 过驱动可靠性差。"""
    report = ValidationReport()
    for tid, ts in state.transistors.items():
        vgs = ts.parameters.vgs
        if vgs <= 0:
            continue
        dev_def = state.get_device_def(tid)
        dev_type = dev_def.type if dev_def else "nmos"
        VTH = state.process.VTH_n if dev_type == "nmos" else state.process.VTH_p
        vov = vgs - VTH
        if vov < 0.05:
            report.add(DiagnosisResult(
                check_name="dr:VGS_safe", passed=False, severity="info",
                message=f"{tid}: vov={vov*1e3:.1f}mV — deep subthreshold, noise may be high",
                layer=4, device=tid
            ))
        elif vov > 0.8:
            report.add(DiagnosisResult(
                check_name="dr:VGS_safe", passed=False, severity="info",
                message=f"{tid}: vov={vov:.2f}V — strong overdrive, check reliability",
                layer=4, device=tid
            ))
    return report


@register_rule("check_headroom", layer=4,
               description="VDS 堆叠 ≤ VDD-VSS 余量")
def check_headroom(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    vdd = state.simulation.supply.get("vdd", 1.8)
    vss = state.simulation.supply.get("vss", 0.0)
    span = vdd - vss
    for dev in state.topology.devices:
        ts = state.transistors.get(dev.id)
        if ts and ts.parameters.vds > span * 0.85:
            report.add(DiagnosisResult(
                check_name="dr:headroom", passed=False, severity="warning",
                message=f"{dev.id}: VDS={ts.parameters.vds:.3f}V > 85% span ({span}V)",
                layer=4, device=dev.id
            ))
    return report


# ═══════════════════════════════════════════════════════════════
#  E. 性能达标检查
# ═══════════════════════════════════════════════════════════════

@register_rule("check_hard_targets", layer=4,
               description="所有 priority=1 的 targets 必须满足")
def check_hard_targets(state: DesignState) -> ValidationReport:
    """检查硬性指标（priority=1）是否全部满足。需要仿真后的性能数据。"""
    report = ValidationReport()
    # 从最后一个 history entry 取性能数据（如果有的话）
    perf = {}
    if state.history:
        perf = state.history[-1].final_performance
    if not perf:
        return report  # 没有仿真数据，跳过

    for name, t in state.targets.items():
        if t.priority != 1:
            continue
        realized = perf.get(name)
        if realized is None:
            continue
        if t.min is not None and realized < t.min:
            report.add(DiagnosisResult(
                check_name="dr:hard_target", passed=False, severity="error",
                message=f"Target '{name}': {realized:.3g} < min={t.min}",
                layer=4
            ))
        if t.max is not None and realized > t.max:
            report.add(DiagnosisResult(
                check_name="dr:hard_target", passed=False, severity="error",
                message=f"Target '{name}': {realized:.3g} > max={t.max}",
                layer=4
            ))
    return report


@register_rule("check_all_targets", layer=4,
               description="所有 targets 检查（含低优先级）")
def check_all_targets(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    perf = {}
    if state.history:
        perf = state.history[-1].final_performance
    if not perf:
        return report
    for name, t in state.targets.items():
        realized = perf.get(name)
        if realized is None:
            continue
        if t.min is not None and realized < t.min:
            report.add(DiagnosisResult(
                check_name="dr:target", passed=False,
                severity="warning" if t.priority > 1 else "error",
                message=f"Target '{name}' (P{t.priority}): {realized:.3g} < min={t.min}",
                layer=4
            ))
        if t.max is not None and realized > t.max:
            report.add(DiagnosisResult(
                check_name="dr:target", passed=False,
                severity="warning" if t.priority > 1 else "error",
                message=f"Target '{name}' (P{t.priority}): {realized:.3g} > max={t.max}",
                layer=4
            ))
    return report


# ═══════════════════════════════════════════════════════════════
#  F. 搜索空间
# ═══════════════════════════════════════════════════════════════

@register_rule("check_gm_id_physical_range", layer=4,
               description="gm/ID 在 [gm_id_min, gm_id_max] 物理可行范围内")
def check_gm_id_physical_range(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    for dv in state.design_variables:
        if dv.variable != "gm_id":
            continue
        if dv.range.min < proc.gm_id_min or dv.range.max > proc.gm_id_max:
            report.add(DiagnosisResult(
                check_name="dr:gm_id_range", passed=False, severity="warning",
                message=f"{dv.device}.gm_id range [{dv.range.min:.1f}, {dv.range.max:.1f}] "
                        f"exceeds physical range [{proc.gm_id_min:.1f}, {proc.gm_id_max:.1f}]",
                layer=4, device=dv.device
            ))
    return report


@register_rule("check_L_step_resolution", layer=4,
               description="L 优化步长 >= L_precision")
def check_L_step_resolution(state: DesignState) -> ValidationReport:
    """检查 optimizer 的 L 搜索步长不小于工艺精度。"""
    report = ValidationReport()
    proc = state.process
    for dv in state.design_variables:
        if dv.variable != "L":
            continue
        span = dv.range.max - dv.range.min
        # 粗略估计：如果 L 范围极窄（< 10 * precision），提示可能无法探索
        if span < proc.L_precision * 10:
            report.add(DiagnosisResult(
                check_name="dr:L_resolution", passed=False, severity="info",
                message=f"{dv.device}.L range [{dv.range.min*1e9:.0f}, {dv.range.max*1e9:.0f}]nm "
                        f"span={span*1e9:.1f}nm < 10*L_precision={proc.L_precision*10*1e9:.1f}nm",
                layer=4, device=dv.device
            ))
    return report


@register_rule("check_symmetry_in_design_vars", layer=4,
               description="对称对管的设计变量 range 必须一致")
def check_symmetry_in_design_vars(state: DesignState) -> ValidationReport:
    """验证同 (symmetry_label, variable) 的设计变量有相同的 range。"""
    report = ValidationReport()
    groups: dict = {}
    for dv in state.design_variables:
        if dv.symmetry_label:
            key = (dv.symmetry_label, dv.variable)
            groups.setdefault(key, []).append(dv)
    for (label, var_name), dvs in groups.items():
        if len(dvs) < 2:
            continue
        ref = dvs[0]
        for dv in dvs[1:]:
            if (abs(dv.range.min - ref.range.min) > 1e-15 or
                abs(dv.range.max - ref.range.max) > 1e-15):
                report.add(DiagnosisResult(
                    check_name="dr:design_var_symmetry", passed=False, severity="error",
                    message=f"Symmetry group '{label}' {var_name}: "
                            f"{dv.device}.{dv.variable} range ≠ {ref.device}.{ref.variable} range",
                    layer=4, device=dv.device
                ))
    return report


# ═══════════════════════════════════════════════════════════════
#  G. 可靠性
# ═══════════════════════════════════════════════════════════════

@register_rule("check_temperature_range", layer=4,
               description="仿真温度在 IC 工作范围 (-40~125°C)")
def check_temperature_range(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    t = state.simulation.temperature
    if t < -40 or t > 125:
        report.add(DiagnosisResult(
            check_name="dr:temperature", passed=False, severity="warning",
            message=f"Simulation temperature {t}°C outside standard IC range (-40~125°C)",
            layer=4
        ))
    return report


@register_rule("check_supply_valid", layer=4,
               description="VDD 在工艺电源域内")
def check_supply_valid(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    vdd = state.simulation.supply.get("vdd", proc.nominal_VDD)
    if vdd < proc.VDD_min * 0.95 or vdd > proc.VDD_max * 1.05:
        report.add(DiagnosisResult(
            check_name="dr:supply", passed=False, severity="warning",
            message=f"VDD={vdd:.2f}V outside process domain [{proc.VDD_min:.2f}, {proc.VDD_max:.2f}]V",
            layer=4
        ))
    return report


@register_rule("check_power_density", layer=4,
               description="功耗密度检查 — 总功耗/总面积")
def check_power_density(state: DesignState) -> ValidationReport:
    """粗略检查：总功耗 / 总晶体管面积 是否合理。"""
    report = ValidationReport()
    total_area = 0.0
    for ts in state.transistors.values():
        W = ts.parameters.W or 0
        L_val = ts.L_strategy or ts.parameters.L or 0
        total_area += W * L_val
    # 估算总功耗
    total_power = 0.0
    for ts in state.transistors.values():
        p = ts.parameters
        if p.id > 0 and p.vds > 0:
            total_power += p.id * p.vds
    if total_area > 0 and total_power > 0:
        density = total_power / total_area  # W/m²
        if density > 1e6:  # > 1 W/mm² 是很高的功率密度
            report.add(DiagnosisResult(
                check_name="dr:power_density", passed=False, severity="info",
                message=f"Power density={density/1e4:.1f}W/mm² — high, consider reliability",
                layer=4
            ))
    return report


@register_rule("check_process_config", layer=4,
               description="工艺配置完整性")
def check_process_config(state: DesignState) -> ValidationReport:
    """检查工艺信息是否完整配置。"""
    report = ValidationReport()
    proc = state.process
    if not proc.process_name:
        report.add(DiagnosisResult(
            check_name="dr:process_name", passed=False, severity="warning",
            message="Process name not configured", layer=4
        ))
    if proc.min_W <= 0 or proc.min_L <= 0:
        report.add(DiagnosisResult(
            check_name="dr:process_drc", passed=False, severity="error",
            message="Process min_W or min_L not set", layer=4
        ))
    if proc.max_VGS <= 0:
        report.add(DiagnosisResult(
            check_name="dr:process_reliability", passed=False, severity="error",
            message="Process max_VGS not set", layer=4
        ))
    if proc.VTH_n <= 0:
        report.add(DiagnosisResult(
            check_name="dr:process_physics", passed=False, severity="warning",
            message="VTH_n not configured", layer=4
        ))
    return report


# ═══════════════════════════════════════════════════════════════
#  导出列表（供 Agent 发现）
# ═══════════════════════════════════════════════════════════════

ALL_RULES = [
    # A. 几何/DRC
    check_min_width, check_max_width, check_min_length, check_max_length,
    check_W_precision, check_L_precision, check_W_L_ratio,
    # B. 面积
    check_min_area, check_finger_width,
    # C. 匹配
    check_pair_W_mismatch, check_pair_L_mismatch, check_pair_gm_mismatch,
    check_current_mirror_ratio,
    # D. 偏置
    check_region_validity, check_saturation_margin,
    check_saturation_depth,
    check_VGS_breakdown, check_VDS_reliability,
    check_VGS_safe_range, check_headroom,
    # D2. 反型层
    check_inversion_region, check_inversion_consistency,
    # D3. 诊断
    diagnose_saturation_failure,
    # E. 性能
    check_hard_targets, check_all_targets,
    # F. 搜索空间
    check_gm_id_physical_range, check_L_step_resolution,
    check_symmetry_in_design_vars,
    # G. 可靠性
    check_temperature_range, check_supply_valid, check_power_density,
    check_process_config,
]

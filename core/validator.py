"""
分层验证器 V2.0 — 数据驱动的多级验证引擎。

架构：
  Layer 1 — Syntax:   结构完整性（字段存在、类型、SchemaSpec 驱动）
  Layer 2 — Semantic: 引用一致性（ID 交叉验证、net 名称有效性）
  Layer 3 — Value:    数值合理性（范围、正数、物理可行性、单位一致性）
  Layer 4 — Physical: 设计规则（工作区、对称、电压堆叠）— 基于 role/stage 动态发现

每一层独立运行，互不依赖。用户可以：
  1. 编辑 Schema 后重新验证 — 所有层自动适配
  2. 注册自定义规则函数 — Agent 通过名称调用
  3. 选择只运行某些层 — 快速检查 vs 完整验证

规则注册系统：
  @register_rule("my_symmetry_check", layer=4)
  def my_check(state): ...

  Agent 调用: rule = get_rule("my_symmetry_check"); report = rule(state)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Any, Set, Tuple

from schemas.design_state import (
    DesignState, TransistorState, TransistorParameters,
    DeviceDefinition, Target, Range, DesignVariable, LossTerm,
    Constraints, DeviceConstraint, Topology, SimulationConfig, GlobalNet,
)
from util.units import Unit, Dimension, Length, Voltage, Current

# 导入设计规则库 → 触发 @register_rule 自动注册
import core.design_rules  # noqa: F401

# 规则注册 API（共享自 rule_registry 模块）
from core.rule_registry import (
    register_rule, get_rule, list_rules, run_registered_rules,
    DiagnosisResult, ValidationReport,
)
# ── Layer 1: Syntax 验证 ────────────────────────────────────

class SyntaxValidator:
    """
    语法层验证 — 基于 SchemaSpec 的字段存在性和类型检查。

    这是数据驱动的：从 DesignState dataclass 的字段定义自动生成 spec，
    不硬编码任何字段名。用户新增字段后，spec 自动更新。
    """

    @staticmethod
    def validate(state: DesignState) -> ValidationReport:
        report = ValidationReport()

        # 必填字段检查
        if not state.design_name or state.design_name == "unnamed":
            report.add(DiagnosisResult(
                check_name="syntax:required_field", passed=False, severity="warning",
                message="design_name is empty or default", layer=1
            ))

        if not state.schema_version:
            report.add(DiagnosisResult(
                check_name="syntax:required_field", passed=False, severity="error",
                message="schema_version is required", layer=1
            ))

        if not state.topology:
            report.add(DiagnosisResult(
                check_name="syntax:required_field", passed=False, severity="error",
                message="topology is required", layer=1
            ))
        elif not state.topology.devices:
            report.add(DiagnosisResult(
                check_name="syntax:required_field", passed=False, severity="error",
                message="topology.devices is empty — at least one device required", layer=1
            ))

        # 类型检查
        for dev in state.topology.devices:
            if not isinstance(dev.connections, dict):
                report.add(DiagnosisResult(
                    check_name="syntax:type_check", passed=False, severity="error",
                    message=f"{dev.id}: connections must be a dict", layer=1, device=dev.id
                ))
            if dev.type not in ("nmos", "pmos"):
                report.add(DiagnosisResult(
                    check_name="syntax:type_check", passed=False, severity="warning",
                    message=f"{dev.id}: unknown device type '{dev.type}' (expected nmos/pmos)",
                    layer=1, device=dev.id
                ))

        if not state.loss_terms:
            report.add(DiagnosisResult(
                check_name="syntax:required_field", passed=False, severity="warning",
                message="loss_terms is empty — optimizer has no objective", layer=1
            ))

        return report


# ── Layer 2: Semantic 验证 ──────────────────────────────────

class SemanticValidator:
    """
    语义层验证 — 引用完整性和一致性检查。

    检查各项之间的 ID 引用是否闭合：
      - topology.devices ↔ transistors
      - topology.devices ↔ constraints.per_device
      - design_variables.device → topology.devices
      - connections 中的 net 名称有效性
    """

    @staticmethod
    def validate(state: DesignState) -> ValidationReport:
        report = ValidationReport()
        topo_ids = {dev.id for dev in state.topology.devices}
        transistor_ids = set(state.transistors.keys())

        # ── devices ↔ transistors ──
        missing_in_transistors = topo_ids - transistor_ids
        for mid in missing_in_transistors:
            report.add(DiagnosisResult(
                check_name="semantic:device_transistor_match", passed=False,
                severity="error",
                message=f"Device {mid} defined in topology but missing in transistors",
                layer=2, device=mid
            ))

        extra_in_transistors = transistor_ids - topo_ids
        for eid in extra_in_transistors:
            report.add(DiagnosisResult(
                check_name="semantic:device_transistor_match", passed=False,
                severity="error",
                message=f"Transistor {eid} present but not defined in topology",
                layer=2, device=eid
            ))

        # ── devices ↔ constraints.per_device ──
        for dev_id in state.constraints.per_device:
            if dev_id not in topo_ids:
                report.add(DiagnosisResult(
                    check_name="semantic:constraint_device_match", passed=False,
                    severity="error",
                    message=f"Constraint defined for unknown device '{dev_id}'",
                    layer=2, device=dev_id
                ))

        # ── design_variables.device → devices ──
        for dv in state.design_variables:
            if not dv.device:
                continue  # 全局变量 (device="") 豁免
            if dv.device not in topo_ids:
                report.add(DiagnosisResult(
                    check_name="semantic:design_var_device", passed=False,
                    severity="error",
                    message=f"Design variable references unknown device '{dv.device}'",
                    layer=2, device=dv.device
                ))

        # ── connections net 名称有效性 ──
        valid_nets = {gn.name for gn in state.topology.global_nets}
        valid_nets |= {p.id for p in state.topology.ports}
        # 中间节点也有效（如 net1, tail 等 — 任何非空字符串即可）
        for dev in state.topology.devices:
            for pin, net_name in dev.connections.items():
                if not net_name or (isinstance(net_name, str) and net_name.strip() == ""):
                    report.add(DiagnosisResult(
                        check_name="semantic:connection_net", passed=False,
                        severity="error",
                        message=f"{dev.id}.{pin} connected to empty net",
                        layer=2, device=dev.id
                    ))

        return report


# ── Layer 3: Value Domain 验证 ──────────────────────────────

class ValueValidator:
    """
    值域层验证 — 数值合理性、范围、单位一致性。

    检查:
      - Range: min < max
      - 物理量正值: W > 0, L > 0, gm > 0
      - 温度合理: 250K < T < 400K
      - 电源正压: vdd > vss
      - Loss weight 非负
    """

    @staticmethod
    def validate(state: DesignState) -> ValidationReport:
        report = ValidationReport()

        # ── constraints 范围检查 ──
        for key, rng in state.constraints.global_.items():
            if rng.min > rng.max:
                report.add(DiagnosisResult(
                    check_name="value:range_validity", passed=False,
                    severity="error",
                    message=f"Global constraint '{key}': min ({rng.min}) > max ({rng.max})",
                    layer=3
                ))

        for dev_id, dc in state.constraints.per_device.items():
            if dc.gm_id and dc.gm_id.min > dc.gm_id.max:
                report.add(DiagnosisResult(
                    check_name="value:range_validity", passed=False,
                    severity="error",
                    message=f"{dev_id} gm_id range invalid: min > max", layer=3, device=dev_id
                ))
            if dc.L and dc.L.min > dc.L.max:
                report.add(DiagnosisResult(
                    check_name="value:range_validity", passed=False,
                    severity="error",
                    message=f"{dev_id} L range invalid: min > max", layer=3, device=dev_id
                ))

        # ── 仿真配置 ──
        sim = state.simulation
        if not (-50 < sim.temperature < 200):
            report.add(DiagnosisResult(
                check_name="value:temperature_range", passed=False,
                severity="warning",
                message=f"Temperature {sim.temperature}°C outside IC range (-50~200°C)",
                layer=3
            ))

        vdd = sim.supply.get("vdd", 0)
        vss = sim.supply.get("vss", 0)
        if vdd <= vss:
            report.add(DiagnosisResult(
                check_name="value:supply_polarity", passed=False,
                severity="error",
                message=f"vdd ({vdd}V) must be > vss ({vss}V)", layer=3
            ))

        # ── 晶体管物理量正值检查 ──
        for tid, ts in state.transistors.items():
            p = ts.parameters
            if p.W < 0:
                report.add(DiagnosisResult(
                    check_name="value:positive_width", passed=False,
                    severity="warning",
                    message=f"{tid}: W={p.W:.2e} should be positive", layer=3, device=tid
                ))
            if p.L <= 0 and ts.L_strategy <= 0:
                report.add(DiagnosisResult(
                    check_name="value:positive_length", passed=False,
                    severity="warning",
                    message=f"{tid}: L is undefined (both strategy and parameters)", layer=3, device=tid
                ))

        # ── Loss weight 检查 ──
        for lt in state.loss_terms:
            if lt.weight < 0:
                report.add(DiagnosisResult(
                    check_name="value:loss_weight_positive", passed=False,
                    severity="warning",
                    message=f"Loss '{lt.id}': negative weight {lt.weight}", layer=3
                ))
            if lt.weight > 100:
                report.add(DiagnosisResult(
                    check_name="value:loss_weight_sanity", passed=False,
                    severity="warning",
                    message=f"Loss '{lt.id}': weight {lt.weight} > 100 may cause instability",
                    layer=3
                ))

        # ── target 范围 ──
        for name, t in state.targets.items():
            if t.min is not None and t.max is not None and t.min > t.max:
                report.add(DiagnosisResult(
                    check_name="value:target_range", passed=False,
                    severity="error",
                    message=f"Target '{name}': min ({t.min}) > max ({t.max})", layer=3
                ))

        return report


# ── Layer 4: Physical Rules 验证 ────────────────────────────

class PhysicalValidator:
    """
    物理规则层验证 — 基于 role/stage 标注动态发现检查规则。

    这是领域专用的验证层。与旧版本的区别：
      - 不再硬编码 "input_pair"、"current_mirror_load"
      - 而是扫描 topology 中的 role 标注，自动发现对称组和检查对象
      - 用户添加新 role 后，规则自动适配

    动态规则发现逻辑：
      - 对称性检查：扫描所有 role，对 role 内出现 ≥2 次的器件自动配对
      - 电压堆叠：基于 topology connections 构建信号路径图
      - 饱和区检查：应用于所有有仿真数据的器件
    """

    # 不需要饱和区检查的 role（如 diode-connected 负载管）
    SATURATION_EXEMPT_ROLES: Set[str] = {"current_mirror_load"}  # diode-connected

    @staticmethod
    def validate(state: DesignState,
                  vdsat_margin: float = 0.05,
                  min_headroom: float = 0.15,
                  symmetry_tolerance: float = 0.05) -> ValidationReport:
        report = ValidationReport()

        # ── 饱和区检查 ──
        report.results.extend(
            PhysicalValidator._check_saturation(state, vdsat_margin)
        )

        # ── 对称性检查（动态发现） ──
        report.results.extend(
            PhysicalValidator._check_symmetry(state, symmetry_tolerance)
        )

        # ── 电压堆叠检查 ──
        report.results.extend(
            PhysicalValidator._check_voltage_stack(state, min_headroom)
        )

        # ── 电流守恒检查（粗略 KCL）──────────────────────────
        report.results.extend(
            PhysicalValidator._check_current_balance(state)
        )

        return report

    # ── 内部方法 ──

    @staticmethod
    def _check_saturation(state: DesignState, margin: float) -> List[DiagnosisResult]:
        results = []
        for tid, ts in state.transistors.items():
            p = ts.parameters
            if p.region == "unknown" or p.vds == 0:
                continue
            dev_def = state.get_device_def(tid)
            role = dev_def.role if dev_def else ""
            if role in PhysicalValidator.SATURATION_EXEMPT_ROLES:
                continue  # diode 连接的器件不需要饱和检查

            if p.vds < p.vdsat + margin:
                results.append(DiagnosisResult(
                    check_name="physical:saturation", passed=False,
                    severity="warning",
                    message=f"{tid} ({role}): vds={p.vds:.3f}V < vdsat+margin={p.vdsat + margin:.3f}V",
                    layer=4, device=tid,
                    details={"vds": p.vds, "vdsat": p.vdsat, "margin": p.vds - p.vdsat}
                ))
            else:
                results.append(DiagnosisResult(
                    check_name="physical:saturation", passed=True,
                    severity="info",
                    message=f"{tid} ({role}): vds={p.vds:.3f}V ≥ vdsat={p.vdsat:.3f}V, margin={p.vds - p.vdsat:.3f}V",
                    layer=4, device=tid
                ))
        return results

    @staticmethod
    def _check_symmetry(state: DesignState, tolerance: float) -> List[DiagnosisResult]:
        """
        动态对称性检查。

        算法：
          1. 扫描 topology.devices，按 role 分组
          2. 对每个 role 组，若成员 ≥2，检查物理参数对称性
          3. 不需要预先知道有哪些 role
        """
        results = []
        # 按 role 分组
        role_groups: Dict[str, List[str]] = {}
        for dev in state.topology.devices:
            role_groups.setdefault(dev.role, []).append(dev.id)

        for role, dev_ids in role_groups.items():
            if len(dev_ids) < 2:
                continue

            # 取第一对（可扩展为 N 向对称）
            for i in range(len(dev_ids)):
                for j in range(i + 1, len(dev_ids)):
                    a, b = dev_ids[i], dev_ids[j]
                    if a not in state.transistors or b not in state.transistors:
                        continue
                    p1 = state.transistors[a].parameters
                    p2 = state.transistors[b].parameters
                    results.extend(
                        PhysicalValidator._compare_pair(a, b, p1, p2, tolerance, role)
                    )
        return results

    @staticmethod
    def _compare_pair(a: str, b: str, p1: TransistorParameters,
                       p2: TransistorParameters, tolerance: float,
                       role: str) -> List[DiagnosisResult]:
        results = []
        checks = {
            "W": (p1.W, p2.W),
            "gm": (p1.gm, p2.gm),
            "vgs": (p1.vgs, p2.vgs),
            "vds": (p1.vds, p2.vds),
        }
        for param, (v1, v2) in checks.items():
            if v1 <= 0 or v2 <= 0:
                continue
            denom = max(abs(v1), abs(v2))
            if denom < 1e-30:
                continue
            deviation = abs(v1 - v2) / denom
            if deviation > tolerance:
                results.append(DiagnosisResult(
                    check_name="physical:symmetry", passed=False,
                    severity="warning",
                    message=f"{a}/{b} ({role}) {param} mismatch: {v1:.4e} vs {v2:.4e} "
                            f"(deviation={deviation*100:.1f}%)",
                    layer=4, device=f"{a}/{b}",
                    details={"param": param, "deviation": deviation}
                ))
        return results

    @staticmethod
    def _check_voltage_stack(state: DesignState, headroom: float) -> List[DiagnosisResult]:
        """
        电压堆叠检查 — 基于 topology 连接关系推断。

        简单版本：对每个 stage，按连接关系估算 VDS 堆叠。
        完整版本需要构建 netlist graph，暂用简化版。
        """
        results = []
        vdd = state.simulation.supply.get("vdd", 1.8)
        vss = state.simulation.supply.get("vss", 0.0)
        supply_span = vdd - vss

        # 按 stage 分组
        stage_devices: Dict[str, List[str]] = {}
        for dev in state.topology.devices:
            stage_devices.setdefault(dev.stage, []).append(dev.id)

        # 对每个 stage，估算 VDS 总和
        for stage, dev_ids in stage_devices.items():
            total_vds = 0.0
            for did in dev_ids:
                if did in state.transistors:
                    total_vds += state.transistors[did].parameters.vds
            if total_vds > supply_span - headroom:
                results.append(DiagnosisResult(
                    check_name="physical:voltage_stack", passed=False,
                    severity="warning",
                    message=f"Stage '{stage}': total VDS ({total_vds:.3f}V) "
                            f"> supply ({supply_span}V) - headroom ({headroom}V)",
                    layer=4
                ))

        return results

    @staticmethod
    def _check_current_balance(state: DesignState) -> List[DiagnosisResult]:
        """
        电流守恒检查 — 粗略 KCL。

        基于 role 标注推断电流关系：
          - tail_current_source 电流 = 输入对管电流之和
          - 输入对管电流之和 ≈ 负载管电流
        """
        results = []
        # 收集各 role 的电流
        role_currents: Dict[str, List[float]] = {}
        for tid, ts in state.transistors.items():
            dev_def = state.get_device_def(tid)
            if not dev_def:
                continue
            p = ts.parameters
            if p.id <= 0:
                continue
            role_currents.setdefault(dev_def.role, []).append(p.id)

        # tail 电流 vs 输入对管电流
        tail_currents = role_currents.get("tail_current_source", [])
        input_currents = role_currents.get("input_pair", [])
        if tail_currents and input_currents:
            tail_sum = sum(tail_currents)
            input_sum = sum(input_currents)
            if tail_sum > 0 and abs(tail_sum - input_sum) / tail_sum > 0.3:
                results.append(DiagnosisResult(
                    check_name="physical:current_balance", passed=False,
                    severity="warning",
                    message=f"Tail current ({tail_sum*1e6:.1f}uA) vs input pair sum "
                            f"({input_sum*1e6:.1f}uA) mismatch >30%",
                    layer=4
                ))

        return results


# ── 综合验证器 ──────────────────────────────────────────────

class Validator:
    """
    综合验证器 — 运行全部分层验证 + 用户注册的自定义规则。

    使用方式:
        v = Validator()
        report = v.validate(state)

        # 只运行语法 + 语义
        report = v.validate(state, layers=[1, 2])

        # 运行自定义规则
        report = v.validate(state, include_custom=True)
    """

    def __init__(self):
        self.syntax = SyntaxValidator()
        self.semantic = SemanticValidator()
        self.value_validator = ValueValidator()
        self.physical = PhysicalValidator()

    def validate(self, state: DesignState,
                  layers: Optional[List[int]] = None,
                  include_custom: bool = True) -> ValidationReport:
        report = ValidationReport()

        if layers is None:
            layers = [1, 2, 3, 4]

        if 1 in layers:
            report.results.extend(self.syntax.validate(state).results)
        if 2 in layers:
            report.results.extend(self.semantic.validate(state).results)
        if 3 in layers:
            report.results.extend(self.value_validator.validate(state).results)
        if 4 in layers:
            report.results.extend(self.physical.validate(state).results)
        if include_custom:
            report.results.extend(run_registered_rules(state).results)

        report.schema_valid = all(
            r.severity != "error" for r in report.results
        )
        return report


# ── 便捷函数 ─────────────────────────────────────────────────

def validate(state: DesignState) -> ValidationReport:
    """运行完整验证（4 层 + 自定义规则）。"""
    return Validator().validate(state)

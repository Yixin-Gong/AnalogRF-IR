#!/usr/bin/env python3
"""
objective_ir V2.1 — 模拟电路自动化设计系统
PTM 130nm · NSGA-II · Boris Murmann Pygmid · ngspice 后仿真

四层 Schema 架构 (架构总纲 V1.1):
  L1 (仅人):    topology + targets
  L2 (人+Agent): constraints + loss_terms (formula + weight)
  L3 (人+Agent): evaluations
  L4 (仅脚本):   transistors 物理量

完整流程：
  1. 加载 environment.yaml → 工艺/仿真/工具配置
  2. 构建 DesignState (PTM 130nm five_transistor_ota)
  3. Schema 验证 (4 层)
  4. 初始化 pygmid 适配器（Boris Murmann LookupTable）
  5. 创建 CircuitEvaluator
  6. NSGA-II 优化搜索 → 最优 (gm_id, L) 决策变量
  7. W/L rounding 到工艺网格
  8. 生成 SPICE 网表
  9. 调用 ngspice 批量仿真
 10. 解析仿真结果，生成仿真 log
 11. 保存全部输出到 runs/iter_NNN/

用法:
    python main.py
"""
from __future__ import annotations

import sys
import math
import json
import time
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from schemas.design_state import (
    DesignState, Topology, GlobalNet, Port, DeviceDefinition,
    Target, Range, Constraints, DeviceConstraint,
    DesignVariable, TransistorState, TransistorParameters,
    LossTerm, Evaluation, SimulationConfig, ProcessInfo, HistoryEntry,
    CorrectionFactors,
)
from core.validator import Validator, ValidationReport, validate, list_rules
import core.design_rules  # 触发注册
from pygmid.adapter import PygmidAdapter, create_pygmid_adapter
from optimizer.nsga2 import (
    NSGA2Optimizer, CircuitEvaluator, NSGA2Config,
    round_transistor_params, round_and_update_state
)
from netlist.generator import NetlistGenerator, generate_netlist
from simulator.ngspice import NgspiceSimulator, SimulationResult
from frontends.yaml_loader import build_design_state_from_yaml, load_yaml_mapping, yaml_has_explicit_topology
from frontends.spice_parser import parse_spice_file, write_yaml


# ═══════════════════════════════════════════════════════════════
# 0. 加载 environment.yaml
# ═══════════════════════════════════════════════════════════════

ENV_PATH = Path(__file__).parent / "environment.yaml"


def _resolve_project_path(path_like: str | Path) -> Path:
    path = Path(path_like)
    if not path.is_absolute():
        path = Path(__file__).parent / path
    return path


def _existing_project_path(path_like: str | Path | None) -> str | None:
    if not path_like:
        return None
    path = _resolve_project_path(path_like)
    return str(path) if path.exists() else None


def load_environment(env_path: Path = None) -> dict:
    """加载工具链与工艺配置。

    Returns:
        dict with keys: process, simulation, tools, design_rules
    """
    if env_path is None:
        env_path = ENV_PATH
    if not env_path.exists():
        print(f"[WARN] {env_path} not found, using hardcoded defaults")
        return _default_environment()

    import yaml
    with open(env_path, "r") as f:
        env = yaml.safe_load(f)
    return env


def _default_environment() -> dict:
    """Fallback: 硬编码环境配置 (PTM 130nm)。"""
    return {
        "process": {
            "process_name": "PTM_130nm",
            "technology_node": 0.13,
            "foundry": "PTM",
            "model_lib": "ptm_130.lib",
            "model_corner": "",
            "osdi_libs": [],
            "device_style": "mos",
            "nmos_model": "nmos",
            "pmos_model": "pmos",
            "VTH_n": 0.3782, "VTH_p": 0.321,
            "KP_n": 0.00091, "KP_p": 0.000123,
            "COX": 0.01535,
        },
        "simulation": {
            "simulator": "ngspice",
            "temperature": 27.0,
            "supply": {"vdd": 1.2, "vss": 0.0},
            "analyses": ["op", "ac"],
            "ac_start": 1.0, "ac_stop": 1e9, "ac_points": 50,
            "cload": 1e-12,
            "bias_voltage": 0.6,
        },
        "tools": {
            "ngspice_bin": "ngspice",
            "pygmid_tables_dir": "tables",
            "nmos_table": "tables/ptm130_nmos.npz",
            "pmos_table": "tables/ptm130_pmos.npz",
        },
        "design_rules": {
            "min_L": 1.3e-7, "max_L": 1e-5,
            "min_W": 1.5e-7, "max_W": 2e-4,
            "W_precision": 1e-8, "L_precision": 1e-9,
            "min_area": 2e-14,
            "max_W_L_ratio": 1000.0, "min_W_L_ratio": 0.1,
            "max_finger_width": 1e-5,
            "max_VGS": 1.32, "max_VDS": 1.32,
            "VDS_safe_margin": 0.15,
            "nominal_VDD": 1.2, "VDD_min": 1.08, "VDD_max": 1.32,
            "gm_id_min": 3.0, "gm_id_max": 28.0,
            "VDSAT_headroom_factor": 1.3,
            "n_sub_nmos": 1.4, "n_sub_pmos": 1.4,
            "mu_n": 0.04, "mu_p": 0.01,
        },
    }


def build_process_info(env: dict) -> ProcessInfo:
    """从 environment.yaml 构建 ProcessInfo。"""
    p = env.get("process", {})
    dr = env.get("design_rules", {})
    return ProcessInfo(
        process_name=p.get("process_name", "PTM_130nm"),
        technology_node=p.get("technology_node", 0.13),
        foundry=p.get("foundry", "PTM"),
        model_lib=p.get("model_lib", "ptm_130.lib"),
        model_corner=p.get("model_corner", ""),
        osdi_libs=p.get("osdi_libs", []) or [],
        device_style=p.get("device_style", "mos"),
        nmos_model=p.get("nmos_model", "nmos"),
        pmos_model=p.get("pmos_model", "pmos"),
        VTH_n=p.get("VTH_n", 0.3782), VTH_p=p.get("VTH_p", 0.321),
        KP_n=p.get("KP_n", 910e-6), KP_p=p.get("KP_p", 123e-6),
        COX=p.get("COX", 1.535e-2),
        min_L=dr.get("min_L", 1.3e-7), max_L=dr.get("max_L", 10e-6),
        min_W=dr.get("min_W", 1.5e-7), max_W=dr.get("max_W", 200e-6),
        W_precision=dr.get("W_precision", 10e-9), L_precision=dr.get("L_precision", 1e-9),
        min_area=dr.get("min_area", 2e-14),
        max_W_L_ratio=dr.get("max_W_L_ratio", 1000.0), min_W_L_ratio=dr.get("min_W_L_ratio", 0.1),
        max_finger_width=dr.get("max_finger_width", 10e-6),
        max_VGS=dr.get("max_VGS", 1.32), max_VDS=dr.get("max_VDS", 1.32),
        VDS_safe_margin=dr.get("VDS_safe_margin", 0.15),
        nominal_VDD=dr.get("nominal_VDD", 1.2),
        VDD_min=dr.get("VDD_min", 1.08), VDD_max=dr.get("VDD_max", 1.32),
        gm_id_min=dr.get("gm_id_min", 3.0), gm_id_max=dr.get("gm_id_max", 28.0),
        VDSAT_headroom_factor=dr.get("VDSAT_headroom_factor", 1.3),
        n_sub_nmos=dr.get("n_sub_nmos", 1.4), n_sub_pmos=dr.get("n_sub_pmos", 1.4),
        mu_n=dr.get("mu_n", 0.04), mu_p=dr.get("mu_p", 0.01),
    )


def build_simulation_config(env: dict) -> SimulationConfig:
    """从 environment.yaml 构建 SimulationConfig。"""
    s = env.get("simulation", {})
    return SimulationConfig(
        temperature=s.get("temperature", 27.0),
        supply=s.get("supply", {"vdd": 1.2, "vss": 0.0}),
        model_lib=env.get("process", {}).get("model_lib", "ptm_130.lib"),
        analyses=s.get("analyses", ["op", "ac"]),
        ac_start=s.get("ac_start", 1.0),
        ac_stop=s.get("ac_stop", 1e9),
        ac_points=s.get("ac_points", 50),
        cload=s.get("cload", 1e-12),
        bias_voltage=s.get("bias_voltage", 0.6),
    )


# ═══════════════════════════════════════════════════════════════
# 1. 构建 DesignState (PTM 130nm five_transistor_ota)
#    L1 (topology + targets) 在此硬编码 — 仅人可修改
# ═══════════════════════════════════════════════════════════════

def build_five_transistor_ota_ptm130(
    env: dict = None,
    schema_path: Path = None,
    apply_overrides: bool = True,
) -> DesignState:
    """
    构建 PTM 130nm five_transistor_ota 的完整设计状态。

    L1 (topology + targets) — 硬编码，仅人可修改。
    L2/L3 初始值 — 硬编码默认，随后由 ir/schema.yaml 覆盖。
    L4 — 空初始状态，由优化器+仿真回填。

    五管 OTA 拓扑：
      - M1, M2: NMOS 差分输入对 (input_pair)
      - M3, M4: PMOS 电流镜负载 (current_mirror_load)
      - M5:    NMOS 尾电流源 (tail_current_source)
    """
    if env is None:
        env = _default_environment()

    state = DesignState(
        schema_version="2.0",
        design_name="five_transistor_ota",
    )

    # ── L1: 拓扑 (仅人) ──
    nmos_model = env.get("process", {}).get("nmos_model", "nmos")
    pmos_model = env.get("process", {}).get("pmos_model", "pmos")
    state.topology = Topology(
        name="five_transistor_ota",
        class_="ota",
        architecture="single-stage",
        global_nets=[
            GlobalNet(name="vdd", type="supply"),
            GlobalNet(name="gnd", type="ground"),
        ],
        ports=[
            Port(id="vinp", direction="input"),
            Port(id="vinn", direction="input"),
            Port(id="vout", direction="output"),
            Port(id="vbias", direction="bias"),
        ],
        devices=[
            DeviceDefinition(
                id="M1", role="input_pair", stage="input", type="nmos", model=nmos_model,
                connections={"drain": "net1", "gate": "vinn", "source": "tail", "body": "gnd"}
            ),
            DeviceDefinition(
                id="M2", role="input_pair", stage="input", type="nmos", model=nmos_model,
                connections={"drain": "vout", "gate": "vinp", "source": "tail", "body": "gnd"}
            ),
            DeviceDefinition(
                id="M3", role="current_mirror_load", stage="load", type="pmos", model=pmos_model,
                connections={"drain": "net1", "gate": "net1", "source": "vdd", "body": "vdd"}
            ),
            DeviceDefinition(
                id="M4", role="current_mirror_load", stage="load", type="pmos", model=pmos_model,
                connections={"drain": "vout", "gate": "net1", "source": "vdd", "body": "vdd"}
            ),
            DeviceDefinition(
                id="M5", role="tail_current_source", stage="bias", type="nmos", model=nmos_model,
                connections={"drain": "tail", "gate": "vbias", "source": "gnd", "body": "gnd"}
            ),
        ],
    )

    # ── L1: 目标规格 (仅人) ──
    state.targets = {
        "dc_gain":              Target(min=35, unit="dB", priority=1),
        "unity_gain_bandwidth": Target(min=40e6, unit="Hz", priority=1),
        "phase_margin":         Target(min=45, unit="deg", priority=1),
        "power":                Target(max=0.3e-3, unit="W", priority=2),
    }

    # ── L2: 约束（搜索空间, Agent 可覆盖）──
    state.constraints = Constraints(
        global_={
            "gm_id": Range(min=5, max=25),
            "L":     Range(min=1.3e-7, max=2.0e-6),
        },
        per_device={
            "M1": DeviceConstraint(gm_id=Range(min=12, max=25)),
            "M2": DeviceConstraint(gm_id=Range(min=12, max=25)),
            "M3": DeviceConstraint(gm_id=Range(min=5, max=12)),
            "M4": DeviceConstraint(gm_id=Range(min=5, max=12)),
            "M5": DeviceConstraint(gm_id=Range(min=8, max=20)),
        }
    )

    # ── L4: 晶体管初始状态 ──
    for dev in state.topology.devices:
        role = dev.role
        if role == "input_pair":
            gm_id_strategy = 15
            L_strategy = 200e-9
        elif "current_mirror" in role:
            gm_id_strategy = 8
            L_strategy = 500e-9
        elif role == "tail_current_source":
            gm_id_strategy = 10
            L_strategy = 200e-9
        else:
            gm_id_strategy = 10
            L_strategy = 300e-9

        state.transistors[dev.id] = TransistorState(
            device_id=dev.id,
            role=dev.role,
            type=dev.type,
            model=dev.model,
            connections=dev.connections,
            gm_id_strategy=gm_id_strategy,
            L_strategy=L_strategy,
        )

    # ── L2: Loss 项 (Agent 可覆盖 formula + weight) ──
    state.loss_terms = [
        LossTerm(
            id="gain_deficit",
            formula="relu(targets.dc_gain.min - realized.dc_gain) / max(targets.dc_gain.min, 1)",
            weight=1.0,
            description="DC gain shortfall penalty"
        ),
        LossTerm(
            id="bw_deficit",
            formula="relu(targets.unity_gain_bandwidth.min - realized.unity_gain_bandwidth) / max(targets.unity_gain_bandwidth.min, 1)",
            weight=0.8,
            description="Bandwidth shortfall penalty"
        ),
        LossTerm(
            id="pm_deficit",
            formula="relu(targets.phase_margin.min - realized.phase_margin) / max(targets.phase_margin.min, 1)",
            weight=0.25,
            description="Phase margin soft penalty (low weight, approximate model)"
        ),
        LossTerm(
            id="power_ratio",
            formula="realized.power / max(targets.power.max, 1e-9)",
            weight=0.5,
            description="Power consumption penalty"
        ),
    ]

    # ── L3: 评估声明 (Agent 可覆盖) ──
    state.evaluations = [
        Evaluation(name="dc_gain",         type="ac_gain",       probe="vout", target_ref="dc_gain"),
        Evaluation(name="unity_gain_bandwidth", type="ugbw",     probe="vout", target_ref="unity_gain_bandwidth"),
        Evaluation(name="phase_margin",    type="phase_margin",  probe="vout", target_ref="phase_margin"),
        Evaluation(name="total_power",     type="dc_power",      target_ref="power"),
    ]

    # ── 仿真配置 (从 environment.yaml) ──
    state.simulation = build_simulation_config(env)

    # ── 工艺信息 (从 environment.yaml) ──
    state.process = build_process_info(env)

    # 构建设计变量（基于硬编码默认值）
    state.build_design_variables()

    # 从 ir/schema.yaml 覆盖 L1/L2/L3（Schema 是规格与优化问题入口）
    if apply_overrides:
        schema_path = schema_path or (Path(__file__).parent / "ir" / "schema.yaml")
        apply_schema_overrides(state, schema_path)

    return state


def build_two_stage_ota_ptm130(
    env: dict = None,
    schema_path: Path = None,
    apply_overrides: bool = True,
) -> DesignState:
    """Build a Miller-compensated two-stage CMOS OTA."""
    if env is None:
        env = _default_environment()

    state = DesignState(schema_version="2.1", design_name="two_stage_ota")
    nmos_model = env.get("process", {}).get("nmos_model", "nmos")
    pmos_model = env.get("process", {}).get("pmos_model", "pmos")
    state.topology = Topology(
        name="two_stage_ota",
        class_="ota",
        architecture="two-stage",
        global_nets=[
            GlobalNet(name="vdd", type="supply"),
            GlobalNet(name="gnd", type="ground"),
        ],
        ports=[
            Port(id="vinp", direction="input"),
            Port(id="vinn", direction="input"),
            Port(id="vout", direction="output"),
            Port(id="vbias_tail", direction="bias"),
            Port(id="vbias_stage2", direction="bias"),
        ],
        devices=[
            DeviceDefinition(
                id="M1", role="input_pair", stage="input", type="nmos", model=nmos_model,
                connections={"drain": "net1", "gate": "vinn", "source": "tail", "body": "gnd"}
            ),
            DeviceDefinition(
                id="M2", role="input_pair", stage="input", type="nmos", model=nmos_model,
                connections={"drain": "n1", "gate": "vinp", "source": "tail", "body": "gnd"}
            ),
            DeviceDefinition(
                id="M3", role="current_mirror_load", stage="load", type="pmos", model=pmos_model,
                connections={"drain": "net1", "gate": "net1", "source": "vdd", "body": "vdd"}
            ),
            DeviceDefinition(
                id="M4", role="current_mirror_load", stage="load", type="pmos", model=pmos_model,
                connections={"drain": "n1", "gate": "net1", "source": "vdd", "body": "vdd"}
            ),
            DeviceDefinition(
                id="M5", role="tail_current_source", stage="bias", type="nmos", model=nmos_model,
                connections={"drain": "tail", "gate": "vbias_tail", "source": "gnd", "body": "gnd"}
            ),
            DeviceDefinition(
                id="M6", role="second_stage_gain", stage="output", type="pmos", model=pmos_model,
                connections={"drain": "vout", "gate": "n1", "source": "vdd", "body": "vdd"}
            ),
            DeviceDefinition(
                id="M7", role="output_current_source", stage="output", type="nmos", model=nmos_model,
                connections={"drain": "vout", "gate": "vbias_stage2", "source": "gnd", "body": "gnd"}
            ),
        ],
    )

    state.targets = {
        "dc_gain":              Target(min=60, unit="dB", priority=1),
        "unity_gain_bandwidth": Target(min=80e6, unit="Hz", priority=1),
        "phase_margin":         Target(min=60, unit="deg", priority=1),
        "power":                Target(max=0.5e-3, unit="W", priority=2),
    }

    state.constraints = Constraints(
        global_={"gm_id": Range(5, 25), "L": Range(1.3e-7, 2.0e-6)},
        per_device={
            "M1": DeviceConstraint(gm_id=Range(10, 22), L=Range(3e-7, 2.0e-6)),
            "M2": DeviceConstraint(gm_id=Range(10, 22), L=Range(3e-7, 2.0e-6)),
            "M3": DeviceConstraint(gm_id=Range(5, 12), L=Range(3e-7, 2.0e-6)),
            "M4": DeviceConstraint(gm_id=Range(5, 12), L=Range(3e-7, 2.0e-6)),
            "M5": DeviceConstraint(gm_id=Range(6, 14), L=Range(2e-7, 1.5e-6)),
            "M6": DeviceConstraint(gm_id=Range(6, 18), L=Range(2e-7, 2.0e-6)),
            "M7": DeviceConstraint(gm_id=Range(6, 14), L=Range(2e-7, 1.5e-6)),
        },
    )

    defaults = {
        "input_pair": (16, 1.0e-6),
        "current_mirror_load": (7, 1.0e-6),
        "tail_current_source": (9, 5.0e-7),
        "second_stage_gain": (10, 8.0e-7),
        "output_current_source": (9, 5.0e-7),
    }
    for dev in state.topology.devices:
        gm_id_strategy, L_strategy = defaults.get(dev.role, (10, 5.0e-7))
        state.transistors[dev.id] = TransistorState(
            device_id=dev.id, role=dev.role, type=dev.type, model=dev.model,
            connections=dev.connections, gm_id_strategy=gm_id_strategy,
            L_strategy=L_strategy,
        )

    state.loss_terms = [
        LossTerm("gain_deficit", "relu(targets.dc_gain.min - realized.dc_gain)/max(targets.dc_gain.min, 1)", 4.0, "DC gain shortfall"),
        LossTerm("bw_deficit", "relu(targets.unity_gain_bandwidth.min - realized.unity_gain_bandwidth)/max(targets.unity_gain_bandwidth.min, 1)", 3.0, "UGBW shortfall"),
        LossTerm("pm_deficit", "relu(targets.phase_margin.min - realized.phase_margin)/max(targets.phase_margin.min, 1)", 6.0, "PM shortfall"),
        LossTerm("power_ratio", "realized.power/max(targets.power.max, 1e-9)", 0.4, "Power ratio"),
        LossTerm("zero_alignment", "abs(realized.Rz - realized.zero_target_rz)/max(realized.zero_target_rz, 1)", 0.08, "Rz near 1/gm6"),
    ]

    state.evaluations = [
        Evaluation(name="dc_gain", type="ac_gain", probe="vout", target_ref="dc_gain"),
        Evaluation(name="unity_gain_bandwidth", type="ugbw", probe="vout", target_ref="unity_gain_bandwidth"),
        Evaluation(name="phase_margin", type="phase_margin", probe="vout", target_ref="phase_margin"),
        Evaluation(name="total_power", type="dc_power", target_ref="power"),
    ]

    state.simulation = build_simulation_config(env)
    state.process = build_process_info(env)
    state.build_design_variables()

    if apply_overrides:
        schema_path = schema_path or (Path(__file__).parent / "ir" / "schema_two_stage.yaml")
        apply_schema_overrides(state, schema_path)

    return state


# ═══════════════════════════════════════════════════════════════
# Schema 覆盖函数 — 从 ir/schema.yaml 读取并应用到 DesignState
#
# 四层权限 (架构总纲 V1.1):
#   L1 (targets)              — 读入 (人编辑), Agent 只读绝不修改
#   L2 (constraints, loss_terms) — 覆盖 (formula + weight + 新增 term)
#   L3 (evaluations)          — 覆盖
#   L4 (transistors)          — 不覆盖 (仅脚本可写)
#
# 覆盖自动重建 design_variables。
# 这是 Agent 修改 Schema 后的唯一生效入口。
# ═══════════════════════════════════════════════════════════════

def _read_schema_header(schema_path: Path) -> dict:
    if not schema_path.exists():
        return {}
    import yaml
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {}
    return {
        "design_name": str(data.get("design_name", "")),
        "architecture": str((data.get("topology", {}) or {}).get("architecture", "")),
    }


def build_design_state(env: dict, schema_path: Path, topology: str = "auto") -> DesignState:
    schema_path = _resolve_project_path(schema_path)
    if topology == "yaml" or (topology == "auto" and yaml_has_explicit_topology(schema_path)):
        return build_design_state_from_yaml(load_yaml_mapping(schema_path), env)
    if topology == "two_stage":
        return build_two_stage_ota_ptm130(env, schema_path=schema_path)
    if topology == "five":
        return build_five_transistor_ota_ptm130(env, schema_path=schema_path)

    meta = _read_schema_header(schema_path)
    signature = f"{meta.get('design_name', '')} {meta.get('architecture', '')}".lower()
    if "two" in signature or "2stage" in signature or "two_stage" in signature:
        return build_two_stage_ota_ptm130(env, schema_path=schema_path)
    return build_five_transistor_ota_ptm130(env, schema_path=schema_path)


def apply_schema_overrides(state: DesignState, schema_path: Path) -> DesignState:
    """从 ir/schema.yaml 读取并应用到 DesignState 的所有层。

    覆盖域（按顺序）：
      0. targets — L1 读入 (人编辑, Agent 只读)
      1. constraints — per-device gm_id/L 范围
      2. design_variables — 显式声明则直接使用 (含全局变量)
      3. corrections — 估计模型校正因子
      4. loss_terms — formula + weight + 新增 term
      5. evaluations — 评估声明

    process/simulation 从 environment.yaml 加载，不在此覆盖。
    """
    if not schema_path.exists():
        return state

    import yaml
    try:
        with open(schema_path, "r") as f:
            sd = yaml.safe_load(f)
    except Exception:
        return state

    # ── 0. targets (L1 — 人可编辑，Agent 只读不修改) ──
    if "design_name" in sd and sd["design_name"]:
        state.design_name = str(sd["design_name"])
        state.topology.name = state.design_name

    if "targets" in sd:
        for name, t in sd["targets"].items():
            if name in state.targets:
                if "min" in t:
                    state.targets[name].min = float(t["min"]) if t["min"] is not None else None
                if "max" in t:
                    state.targets[name].max = float(t["max"]) if t["max"] is not None else None

    # ── 1. constraints (L2) ──
    if "constraints" in sd:
        c = sd["constraints"]

        # 检测格式: 新格式 (平铺器件) 或旧格式 (global + devices)
        has_device_keys = any(
            (isinstance(k, str) and (k.startswith("M") or k.startswith("m")))
            for k in c.keys()
        )

        if has_device_keys:
            # 新格式: constraints: {M1: {gm_id: [8,20], L: [500n,2u]}}
            for dev_id, d in c.items():
                if not isinstance(d, dict):
                    continue
                if dev_id not in state.constraints.per_device:
                    state.constraints.per_device[dev_id] = DeviceConstraint()
                dc = state.constraints.per_device[dev_id]
                if "gm_id" in d:
                    gm_val = d["gm_id"]
                    if isinstance(gm_val, list):
                        dc.gm_id = Range(min=gm_val[0], max=gm_val[1])
                    elif isinstance(gm_val, dict):
                        dc.gm_id = Range(min=gm_val["min"], max=gm_val["max"])
                if "L" in d:
                    l_val = d["L"]
                    if isinstance(l_val, list):
                        dc.L = Range(min=l_val[0], max=l_val[1])
                    elif isinstance(l_val, dict):
                        dc.L = Range(min=l_val["min"], max=l_val["max"])
        else:
            # 旧格式: constraints: {gm_id: {min, max}, L: {min, max}, devices: {M1:...}}
            # 全局约束
            for key in ("gm_id", "L"):
                if key in c and isinstance(c[key], dict):
                    r = c[key]
                    if "min" in r and "max" in r:
                        state.constraints.global_[key] = Range(min=r["min"], max=r["max"])
            # 器件级约束
            if "devices" in c:
                for dev_id, d in c["devices"].items():
                    if dev_id not in state.constraints.per_device:
                        state.constraints.per_device[dev_id] = DeviceConstraint()
                    dc = state.constraints.per_device[dev_id]
                    if "gm_id" in d and isinstance(d["gm_id"], dict):
                        dc.gm_id = Range(min=d["gm_id"]["min"], max=d["gm_id"]["max"])
                    if "L" in d and isinstance(d["L"], dict):
                        dc.L = Range(min=d["L"]["min"], max=d["L"]["max"])

    # ── 2. design_variables (L2) — YAML 显式声明则直接使用 ──
    if "design_variables" in sd and sd["design_variables"]:
        state.design_variables = []
        for dv_raw in sd["design_variables"]:
            r = dv_raw.get("range", {})
            r_min = r.get("min", 0)
            r_max = r.get("max", 0)
            if isinstance(r_min, str):
                from util.units import parse_value
                r_min = parse_value(r_min)
            if isinstance(r_max, str):
                from util.units import parse_value
                r_max = parse_value(r_max)
            rng = Range(min=r_min, max=r_max)
            state.design_variables.append(DesignVariable(
                device=dv_raw.get("device", ""),
                variable=dv_raw.get("variable", ""),
                range=rng,
                initial=dv_raw.get("initial"),
                symmetry_label=dv_raw.get("symmetry_label"),
                unit=dv_raw.get("unit", ""),
                description=dv_raw.get("description", ""),
            ))

    # ── 3. corrections (L2) — 估计模型校正因子 ──
    if "corrections" in sd:
        corr = sd["corrections"]
        state.corrections = CorrectionFactors(
            gm_factor=float(corr.get("gm_factor", 1.0)),
            gds_factor=float(corr.get("gds_factor", 1.0)),
            c_factor=float(corr.get("c_factor", 1.0)),
            description=corr.get("description", ""),
        )

    # ── 4. loss_terms (L2) — formula + weight + 动态新增 ──
    if "loss_terms" in sd:
        existing_ids = {lt.id for lt in state.loss_terms}
        for lt_schema in sd["loss_terms"]:
            lt_id = lt_schema.get("id", "")
            if not lt_id:
                continue

            if lt_id in existing_ids:
                # 更新已有 term
                for lt in state.loss_terms:
                    if lt.id == lt_id:
                        if "formula" in lt_schema and lt_schema["formula"]:
                            lt.formula = lt_schema["formula"]
                        if "weight" in lt_schema and lt_schema["weight"] is not None:
                            lt.weight = float(lt_schema["weight"])
                        if "description" in lt_schema:
                            lt.description = lt_schema.get("description", lt.description)
                        break
            else:
                # 动态新增 term
                new_lt = LossTerm(
                    id=lt_id,
                    formula=lt_schema.get("formula", ""),
                    weight=float(lt_schema.get("weight", 1.0)),
                    description=lt_schema.get("description", ""),
                )
                state.loss_terms.append(new_lt)
                existing_ids.add(lt_id)

    # ── 5. evaluations (L3) ──
    if "evaluations" in sd:
        existing_ev_names = {ev.name for ev in state.evaluations}
        for ev_schema in sd["evaluations"]:
            ev_name = ev_schema.get("name", "")
            if not ev_name:
                continue
            if ev_name in existing_ev_names:
                # 更新已有 evaluation
                for ev in state.evaluations:
                    if ev.name == ev_name:
                        for field in ("type", "probe", "device", "target_ref", "meas_formula"):
                            if field in ev_schema:
                                setattr(ev, field, ev_schema[field])
                        break
            else:
                # 新增 evaluation
                state.evaluations.append(Evaluation(
                    name=ev_name,
                    type=ev_schema.get("type", ""),
                    probe=ev_schema.get("probe", ""),
                    device=ev_schema.get("device", ""),
                    target_ref=ev_schema.get("target_ref", ""),
                    meas_formula=ev_schema.get("meas_formula", ""),
                ))

    # ── 6. initial_strategy (legacy — 未被 design_variables 覆盖时使用) ──
    if "constraints" in sd:
        c = sd["constraints"]
        has_device_keys = any(
            (isinstance(k, str) and (k.startswith("M") or k.startswith("m")))
            for k in c.keys()
        )
        if has_device_keys:
            # 新格式: 直接从器件 constraints 读取 initial_gm_id / initial_L
            device_dict = c
        else:
            # 旧格式: 从 constraints.devices 读取
            device_dict = c.get("devices", {})

        for dev_id, d in device_dict.items():
            if not isinstance(d, dict):
                continue
            if dev_id in state.transistors:
                ts = state.transistors[dev_id]
                if "initial_gm_id" in d:
                    ts.gm_id_strategy = float(d["initial_gm_id"])
                if "initial_L" in d:
                    ts.L_strategy = float(d["initial_L"])

    # ── 7. 重建 design_variables (仅当 YAML 未显式声明时) ──
    state.build_design_variables()

    return state


# ═══════════════════════════════════════════════════════════════
# 2. 迭代计数与保存
# ═══════════════════════════════════════════════════════════════

RUNS_DIR = Path("runs")


def _next_iteration(runs_dir: Path) -> int:
    """扫描 runs/ 目录，返回下一个迭代编号。"""
    if not runs_dir.exists():
        return 1
    max_n = 0
    for entry in runs_dir.iterdir():
        if entry.is_dir() and entry.name.startswith("iter_"):
            try:
                n = int(entry.name.split("_")[1])
                if n > max_n:
                    max_n = n
            except ValueError:
                pass
    return max_n + 1


def save_simulation_log(
    state: DesignState,
    best_meta: dict,
    sim_result: SimulationResult,
    iteration: int,
    netlist_str: str = "",
) -> Path:
    """
    保存完整输出：design_state.yaml, netlist.cir, sim_log.json。

    sim_log.json 包含：
      - 优化器结果（损失、决策变量、性能估计）
      - ngspice 仿真结果 (.measure + 工作点)
      - 对比：optimizer_estimated vs ngspice_measured
    """
    output_dir = RUNS_DIR / f"iter_{iteration:03d}"
    output_dir.mkdir(parents=True, exist_ok=True)

    ## ── design_state.yaml ──
    state.to_yaml(output_dir / "design_state.yaml")

    ## ── netlist.cir ──
    with open(output_dir / "netlist.cir", "w", encoding="utf-8") as f:
        f.write(netlist_str)

    ## ── sim_log.json ──
    decoded = best_meta.get("decoded", {})
    perf_est = best_meta.get("performance", {})

    # transistor_params_opt 从已 round 的 state.transistors 读取
    proc = state.process
    W_grid = getattr(proc, "W_precision", 10e-9)
    L_grid = getattr(proc, "L_precision", 1e-9)
    W_dec = max(0, int(-math.floor(math.log10(W_grid)))) if W_grid > 0 else 0
    L_dec = max(0, int(-math.floor(math.log10(L_grid)))) if L_grid > 0 else 0

    def _snap(v, grid=1e-9, dec=9):
        if isinstance(v, float) and grid > 0:
            n = int(round(v / grid))
            return round(n * grid, dec)
        return v

    def _sig(v, digits=8):
        if isinstance(v, float):
            return float(f"{v:.{digits}g}")
        return v

    def _round_wl(tp_in: dict) -> dict:
        out = dict(tp_in)
        if out.get("W", 0) > 0:
            n = int(round(out["W"] / W_grid))
            out["W"] = round(n * W_grid, W_dec)
        if out.get("L", 0) > 0:
            n = int(round(out["L"] / L_grid))
            out["L"] = round(n * L_grid, L_dec)
        return out

    log = {
        "schema_version": "2.0",
        "iteration": iteration,
        "timestamp": datetime.now().isoformat(),
        "design_name": state.design_name,
        "process": state.process.process_name,
        "supply_vdd": state.simulation.supply.get("vdd", 1.2),

        "optimizer": {
            "convergence": True,
            "best_loss": best_meta.get("total_loss", 0),
            "performance_estimated": {
                k: _sig(v) for k, v in perf_est.items()
            },
            "loss_breakdown": {
                k: round(v, 6) for k, v in best_meta.get("loss_breakdown", {}).items()
            },
            "decision_variables": {
                did: {k: _snap(v, 1e-9) if k == "L" else round(v, 2) if k == "gm_id" else v
                      for k, v in dv.items()}
                for did, dv in decoded.items()
                if did and not did.startswith("__")
            },
            "global_variables": (
                {k: _sig(v) for k, v in (state.global_parameters or decoded.get("__global__", {})).items()}
            ),
            "transistor_params_opt": {
                did: _round_wl({
                    "W": ts.parameters.W,
                    "L": ts.parameters.L,
                    "gm": ts.parameters.gm,
                    "gds": ts.parameters.gds,
                    "vgs": ts.parameters.vgs,
                    "vds": ts.parameters.vds,
                    "vdsat": ts.parameters.vdsat,
                    "region": ts.parameters.region or "unknown",
                    "id": ts.parameters.id,
                    "ft": ts.parameters.ft,
                    "gm_id_realized": ts.parameters.gm_id_realized,
                    "cgs": ts.parameters.cgs,
                    "cgd": ts.parameters.cgd,
                })
                for did, ts in state.transistors.items()
            },
        },

        # ── ngspice 仿真结果 ──
        "ngspice": {
            "success": sim_result.success,
            "return_code": sim_result.return_code,
            "elapsed_sec": round(sim_result.elapsed_sec, 3),
            "measurements": {
                k: round(v, 8) for k, v in sim_result.measurements.items()
            },
            "operating_points": {
                did: {k: round(v, 6) if abs(v) < 1e-3 else round(v, 4)
                      for k, v in op.items()}
                for did, op in sim_result.operating_points.items()
            },
        },

        # ── 对比 ──
        "comparison": {},
    }

    # 构建对比表：optimizer estimated vs ngspice measured
    meas = sim_result.measurements
    for key, est_val in perf_est.items():
        meas_key_map = {
            "dc_gain": "dc_gain_db",
            "unity_gain_bandwidth": "unity_gain_bandwidth",
            "phase_margin": "phase_margin",
            "power": "total_power",
        }
        ng_key = meas_key_map.get(key, key)
        ng_val = meas.get(ng_key)
        log["comparison"][key] = {
            "optimizer_estimated": round(est_val, 3),
            "ngspice_measured": round(ng_val, 4) if ng_val is not None else None,
        }

    with open(output_dir / "sim_log.json", "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)

    diagnostics = build_agent_diagnostics(state, best_meta, sim_result, iteration, log["comparison"])
    with open(output_dir / "agent_diagnostics.json", "w", encoding="utf-8") as f:
        json.dump(diagnostics, f, indent=2)

    return output_dir


def build_agent_diagnostics(
    state: DesignState,
    best_meta: dict,
    sim_result: SimulationResult,
    iteration: int,
    comparison: dict,
) -> dict:
    """Build structured, agent-readable run diagnostics.

    This file replaces the old append-only Markdown history log. It is scoped
    to a single run directory and is designed for downstream agents to parse
    without natural-language scraping.
    """
    measurements = sim_result.measurements or {}
    perf_est = best_meta.get("performance", {}) or {}
    loss_breakdown = best_meta.get("loss_breakdown", {}) or {}
    target_status = _target_status(state, measurements, perf_est)
    failed_targets = [name for name, item in target_status.items() if item["status"] == "fail"]
    unverified_targets = [name for name, item in target_status.items() if item["status"] == "unverified"]
    top_losses = _top_items(loss_breakdown, limit=8)
    top_model_mismatch = _comparison_mismatch(comparison)
    device_status = _device_status(state)

    return {
        "schema_version": "agent_diagnostics.v1",
        "run": {
            "iteration": iteration,
            "timestamp": datetime.now().isoformat(),
            "design_name": state.design_name,
            "topology": state.topology.name,
            "architecture": state.topology.architecture,
            "class": state.topology.class_,
            "process": state.process.process_name,
            "technology_node_um": state.process.technology_node,
            "vdd": state.simulation.supply.get("vdd", 1.2),
        },
        "status": {
            "ngspice_success": bool(sim_result.success),
            "return_code": sim_result.return_code,
            "spec_pass": bool(target_status) and not failed_targets and not unverified_targets,
            "failed_targets": failed_targets,
            "unverified_targets": unverified_targets,
            "best_loss": best_meta.get("total_loss", 0.0),
        },
        "targets": target_status,
        "optimizer": {
            "estimated_performance": perf_est,
            "top_loss_terms": top_losses,
            "decoded": best_meta.get("decoded", {}),
            "global_parameters": dict(state.global_parameters or {}),
        },
        "ngspice": {
            "measurements": measurements,
            "elapsed_sec": sim_result.elapsed_sec,
            "operating_point_count": len(sim_result.operating_points or {}),
        },
        "model_mismatch": top_model_mismatch,
        "devices": device_status,
        "diagnosis": _diagnosis_items(target_status, top_losses, top_model_mismatch, device_status),
        "artifacts": {
            "design_state": "design_state.yaml",
            "netlist": "netlist.cir",
            "sim_log": "sim_log.json",
            "agent_diagnostics": "agent_diagnostics.json",
        },
    }


def _target_status(state: DesignState, measurements: dict, perf_est: dict) -> dict:
    metric_map = {
        "dc_gain": "dc_gain_db",
        "unity_gain_bandwidth": "unity_gain_bandwidth",
        "phase_margin": "phase_margin",
        "power": "total_power",
    }
    out = {}
    for name, target in state.targets.items():
        metric = metric_map.get(name, name)
        source = "ngspice" if metric in measurements else "optimizer_estimate"
        value = measurements.get(metric, perf_est.get(name))
        status = "unknown"
        model_status = "unknown"
        margin_abs = None
        margin_rel = None
        if value is not None:
            status = "pass"
            if target.min is not None:
                margin_abs = float(value) - float(target.min)
                margin_rel = margin_abs / max(abs(float(target.min)), 1e-30)
                if margin_abs < 0:
                    status = "fail"
            if target.max is not None:
                max_margin = float(target.max) - float(value)
                max_margin_rel = max_margin / max(abs(float(target.max)), 1e-30)
                if margin_abs is None or max_margin < margin_abs:
                    margin_abs = max_margin
                    margin_rel = max_margin_rel
                if max_margin < 0:
                    status = "fail"
            model_status = status
            if int(target.priority or 1) <= 1 and source != "ngspice":
                status = "unverified"
        out[name] = {
            "status": status,
            "model_status": model_status,
            "source": source,
            "requires_ngspice": int(target.priority or 1) <= 1,
            "measurement_key": metric,
            "value": value,
            "min": target.min,
            "max": target.max,
            "unit": target.unit,
            "priority": target.priority,
            "margin_abs": margin_abs,
            "margin_rel": margin_rel,
        }
    return out


def _top_items(values: dict, limit: int = 8) -> list[dict]:
    items = []
    for name, value in values.items():
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        items.append({"name": name, "value": numeric})
    return sorted(items, key=lambda item: abs(item["value"]), reverse=True)[:limit]


def _comparison_mismatch(comparison: dict) -> list[dict]:
    out = []
    for name, item in comparison.items():
        est = item.get("optimizer_estimated")
        meas = item.get("ngspice_measured")
        if est is None or meas is None:
            continue
        delta = meas - est
        rel = delta / max(abs(est), 1e-30)
        out.append({"metric": name, "optimizer_estimated": est, "ngspice_measured": meas, "delta": delta, "rel_delta": rel})
    return sorted(out, key=lambda item: abs(item["rel_delta"]), reverse=True)


def _device_status(state: DesignState) -> dict:
    out = {}
    for dev in state.topology.devices:
        ts = state.transistors.get(dev.id)
        if ts is None:
            continue
        p = ts.parameters
        vds_margin = None
        if p.vds and p.vdsat:
            vds_margin = p.vds - p.vdsat * state.process.VDSAT_headroom_factor
        out[dev.id] = {
            "role": dev.role,
            "stage": dev.stage,
            "type": dev.type,
            "model": dev.model,
            "W": p.W,
            "L": p.L,
            "gm_id_strategy": ts.gm_id_strategy,
            "gm_id_realized": p.gm_id_realized,
            "region": p.region or "unknown",
            "id": p.id,
            "gm": p.gm,
            "gds": p.gds,
            "vgs": p.vgs,
            "vds": p.vds,
            "vdsat": p.vdsat,
            "vds_margin_vs_required": vds_margin,
        }
    return out


def _diagnosis_items(targets: dict, losses: list[dict], mismatches: list[dict], devices: dict) -> list[dict]:
    items = []
    for name, target in targets.items():
        if target["status"] == "fail":
            if name in {"unity_gain_bandwidth", "ugbw"}:
                hint = "Increase speed by raising bias current, reducing compensation/load capacitance, or shortening high-capacitance devices."
            elif name in {"phase_margin"}:
                hint = "Improve stability by increasing compensation capacitance, moving Rz near the zero target, or reducing second-pole loading."
            elif name in {"dc_gain"}:
                hint = "Increase intrinsic gain by lengthening output/load devices or reducing output conductance."
            elif name in {"power"}:
                hint = "Reduce bias currents or device widths on non-critical paths."
            else:
                hint = "Inspect the target-specific measurement and its related loss term."
            items.append({"type": "target_failure", "metric": name, "severity": "error", "hint": hint, "target": target})
        elif target["status"] == "unverified":
            items.append({
                "type": "target_unverified",
                "metric": name,
                "severity": "warning",
                "hint": "Priority-1 targets require ngspice measurements before they can be counted as passing.",
                "target": target,
            })

    for loss in losses:
        if loss["value"] <= 0:
            continue
        severity = "warning" if loss["value"] < 10 else "error"
        items.append({"type": "loss_contributor", "name": loss["name"], "severity": severity, "value": loss["value"]})

    for mismatch in mismatches[:4]:
        if abs(mismatch["rel_delta"]) > 0.1:
            items.append({"type": "model_mismatch", "severity": "warning", **mismatch})

    for dev_id, dev in devices.items():
        margin = dev.get("vds_margin_vs_required")
        if margin is not None and margin < 0:
            items.append({
                "type": "device_headroom",
                "severity": "warning",
                "device": dev_id,
                "margin": margin,
                "hint": "Device may be outside robust saturation; adjust bias, current, or W/L.",
            })
    return items


def normalize_phase_margin(raw_value: float) -> float:
    """Convert ngspice vp() output to a phase-margin-like value in degrees."""
    phase = float(raw_value)
    if abs(phase) <= 2.0 * math.pi + 0.1:
        phase *= 180.0 / math.pi
    if phase < 0:
        return max(0.0, 180.0 + phase)
    if phase > 180.0:
        return max(0.0, 360.0 - phase)
    return phase


def backfill_state_from_ngspice(state: DesignState, result: SimulationResult) -> None:
    """Update L4 transistor parameters with ngspice operating-point values."""
    if not result.operating_points:
        return
    for dev in state.topology.devices:
        ts = state.transistors.get(dev.id)
        if ts is None:
            continue
        spice_names = [f"M{dev.id}".upper(), dev.id.upper()]
        op = {}
        for name in spice_names:
            if name in result.operating_points:
                op = result.operating_points[name]
                break
        if not op:
            continue

        p = ts.parameters
        for field in ("gm", "gds", "vgs", "vds", "vdsat", "id", "cgs", "cgd", "cgg"):
            if field in op:
                setattr(p, field, abs(float(op[field])))
        if p.id > 1e-18 and p.gm > 0:
            p.gm_id_realized = p.gm / p.id
        if p.vdsat > 0 and p.vds > 0:
            p.region = "saturation" if p.vds >= p.vdsat else "linear"


def _is_two_stage_state(state: DesignState) -> bool:
    arch = (state.topology.architecture or "").lower()
    return "two" in arch or any(dev.role == "second_stage_gain" for dev in state.topology.devices)


def _get_stage2_device_ids(state: DesignState) -> tuple[str | None, str | None]:
    gain_id = None
    sink_id = None
    for dev in state.topology.devices:
        if dev.role == "second_stage_gain":
            gain_id = dev.id
        elif dev.role in ("second_stage_load", "output_current_source"):
            sink_id = dev.id
    return gain_id, sink_id


def _stage2_vout_from_result(state: DesignState, result: SimulationResult) -> float | None:
    _, sink_id = _get_stage2_device_ids(state)
    if not sink_id:
        return None
    for name in (f"M{sink_id}".upper(), sink_id.upper()):
        op = result.operating_points.get(name)
        if op and "vds" in op:
            return abs(float(op["vds"]))
    return None


def _op_for_device(result: SimulationResult, device_id: str) -> dict:
    for name in (f"M{device_id}".upper(), device_id.upper()):
        op = result.operating_points.get(name)
        if op:
            return op
    return {}


def balance_two_stage_output(
    state: DesignState,
    sim: NgspiceSimulator,
    work_dir: Path,
    max_iter: int = 9,
) -> dict:
    """Balance second-stage DC output before final AC verification."""
    if not _is_two_stage_state(state):
        return {}
    gain_id, sink_id = _get_stage2_device_ids(state)
    if not gain_id or not sink_id:
        return {}
    gain_ts = state.transistors.get(gain_id)
    sink_ts = state.transistors.get(sink_id)
    if not gain_ts or gain_ts.parameters.W <= 0:
        return {}

    vdd = state.simulation.supply.get("vdd", 1.2)
    target = 0.5 * vdd
    base_w = gain_ts.parameters.W
    proc = state.process
    min_w = getattr(proc, "min_W", 150e-9)
    max_w = getattr(proc, "max_W", 200e-6)
    base_sink_w = sink_ts.parameters.W if sink_ts and sink_ts.parameters.W > 0 else None
    best = {"scale": 1.0, "sink_scale": 1.0, "vout": None, "error": float("inf")}

    def _clip_width(width: float) -> float:
        return min(max(width, min_w), max_w)

    def evaluate(scale: float, sink_scale: float = 1.0) -> float | None:
        gain_ts.parameters.W = min(max(base_w * scale, min_w), max_w)
        if sink_ts and base_sink_w:
            sink_ts.parameters.W = _clip_width(base_sink_w * sink_scale)
        trial = sim.run(generate_netlist(state), work_dir=str(work_dir))
        vout = _stage2_vout_from_result(state, trial)
        if vout is None:
            return None
        err = abs(vout - target)
        if err < best["error"]:
            best.update({"scale": scale, "sink_scale": sink_scale, "vout": vout, "error": err})
        return vout

    lo = max(0.0625, min_w / base_w) if base_w > 0 else 0.0625
    hi = min(16.0, max_w / base_w) if base_w > 0 else 16.0
    if hi <= lo:
        gain_ts.parameters.W = min(max(base_w, min_w), max_w)
        return {}
    vlo = evaluate(lo)
    vhi = evaluate(hi)
    if vlo is None or vhi is None:
        gain_ts.parameters.W = base_w
        if sink_ts and base_sink_w:
            sink_ts.parameters.W = base_sink_w
        return {}

    if min(vlo, vhi) <= target <= max(vlo, vhi):
        increasing = vhi > vlo
        for _ in range(max_iter):
            mid = math.sqrt(lo * hi)
            vmid = evaluate(mid)
            if vmid is None:
                break
            if increasing:
                if vmid < target:
                    lo = mid
                else:
                    hi = mid
            else:
                if vmid < target:
                    hi = mid
                else:
                    lo = mid
    elif sink_ts and base_sink_w:
        if best["vout"] is not None and best["vout"] < target:
            gain_scale = hi
            sink_candidates = [1.0, 0.75, 0.5, 0.33, 0.2, 0.125, 0.08]
        else:
            gain_scale = lo
            sink_hi = min(16.0, max_w / base_sink_w) if base_sink_w > 0 else 16.0
            sink_candidates = [1.0, 1.5, 2.0, 3.0, 5.0, 8.0, sink_hi]
        for sink_scale in sink_candidates:
            if sink_scale <= 0:
                continue
            evaluate(gain_scale, sink_scale)

    gain_ts.parameters.W = min(max(base_w * best["scale"], min_w), max_w)
    if sink_ts and base_sink_w:
        sink_ts.parameters.W = _clip_width(base_sink_w * best["sink_scale"])
    return best


def improve_tail_headroom(
    state: DesignState,
    sim: NgspiceSimulator,
    work_dir: Path,
    required_margin: float = 0.05,
) -> dict:
    """Increase input-pair width if the tail device lacks VDS headroom."""
    input_ids = [dev.id for dev in state.topology.devices if dev.role == "input_pair"]
    tail_id = next(
        (dev.id for dev in state.topology.devices if dev.role == "tail_current_source"),
        None,
    )
    if len(input_ids) < 2 or not tail_id:
        return {}

    proc = state.process
    max_w = getattr(proc, "max_W", 200e-6)
    base_widths = {
        dev_id: state.transistors[dev_id].parameters.W
        for dev_id in input_ids
        if dev_id in state.transistors and state.transistors[dev_id].parameters.W > 0
    }
    if len(base_widths) != len(input_ids):
        return {}

    best = {"scale": 1.0, "margin": float("-inf"), "vds": None, "vdsat": None}
    chosen = None

    for scale in (1.0, 1.2, 1.5, 2.0, 2.5, 3.0):
        for dev_id, base_w in base_widths.items():
            state.transistors[dev_id].parameters.W = min(base_w * scale, max_w)
        trial = sim.run(generate_netlist(state), work_dir=str(work_dir))
        op = _op_for_device(trial, tail_id)
        if not op:
            continue
        vds = abs(float(op.get("vds", 0.0)))
        vdsat = abs(float(op.get("vdsat", 0.0)))
        margin = vds - vdsat
        if margin > best["margin"]:
            best.update({"scale": scale, "margin": margin, "vds": vds, "vdsat": vdsat})
        if margin >= required_margin:
            chosen = {"scale": scale, "margin": margin, "vds": vds, "vdsat": vdsat}
            break

    if chosen is None:
        chosen = best if best["margin"] > float("-inf") else None
    if not chosen or chosen["scale"] <= 1.0:
        for dev_id, base_w in base_widths.items():
            state.transistors[dev_id].parameters.W = base_w
        return {}

    for dev_id, base_w in base_widths.items():
        state.transistors[dev_id].parameters.W = min(base_w * chosen["scale"], max_w)
    return chosen


def _design_var_range(state: DesignState, name: str) -> tuple[float, float] | None:
    for dv in state.design_variables:
        if not dv.device and dv.variable == name:
            return dv.range.min, dv.range.max
    return None


def _unique_sorted(values: list[float], low: float, high: float) -> list[float]:
    out = []
    for value in values:
        value = min(max(float(value), low), high)
        if not any(
            abs(value - old) <= max(abs(old), abs(value), 1e-30) * 1e-6
            for old in out
        ):
            out.append(value)
    return sorted(out)


def tune_two_stage_compensation(
    state: DesignState,
    sim: NgspiceSimulator,
    work_dir: Path,
) -> dict:
    """Use a small ngspice sweep to choose Cc/Rz after transistor sizing."""
    if not _is_two_stage_state(state):
        return {}
    cc_range = _design_var_range(state, "Cc")
    rz_range = _design_var_range(state, "Rz")
    if not cc_range or not rz_range:
        return {}

    cc_low, cc_high = cc_range
    rz_low, rz_high = rz_range
    current_cc = state.global_parameters.get("Cc", 0.5 * (cc_low + cc_high))
    current_rz = state.global_parameters.get("Rz", 0.5 * (rz_low + rz_high))

    gain_id, _ = _get_stage2_device_ids(state)
    gm6 = state.transistors.get(gain_id).parameters.gm if gain_id in state.transistors else 0.0
    rz0 = 1.0 / gm6 if gm6 > 1e-12 else current_rz

    cc_candidates = _unique_sorted(
        [
            cc_low,
            cc_low * 1.25,
            cc_low * 1.5,
            cc_low * 2.0,
            cc_low * 2.5,
            cc_low * 3.0,
            cc_low * 4.0,
            cc_low * 5.0,
            cc_low * 7.5,
            cc_low * 10.0,
            current_cc * 0.75,
            current_cc,
            current_cc * 1.5,
            current_cc * 2.25,
            cc_high,
        ],
        cc_low, cc_high,
    )
    rz_candidates = _unique_sorted(
        [
            rz_low,
            300.0,
            500.0,
            750.0,
            1000.0,
            1500.0,
            2000.0,
            2500.0,
            3000.0,
            3500.0,
            4000.0,
            4500.0,
            5000.0,
            7500.0,
            10000.0,
            15000.0,
            rz0 * 0.5,
            rz0,
            rz0 * 1.5,
            rz0 * 2.0,
            current_rz,
            rz_high,
        ],
        rz_low, rz_high,
    )

    targets = state.targets
    gain_min = targets.get("dc_gain", Target()).min or 0.0
    bw_target = targets.get("unity_gain_bandwidth", Target())
    bw_min = bw_target.min or 0.0
    bw_max = bw_target.max or float("inf")
    pm_min = targets.get("phase_margin", Target()).min or 0.0
    power_max = targets.get("power", Target()).max or float("inf")

    best = {"score": float("inf"), "Cc": current_cc, "Rz": current_rz, "measurements": {}}
    for cc in cc_candidates:
        for rz in rz_candidates:
            state.global_parameters["Cc"] = cc
            state.global_parameters["Rz"] = rz
            trial = sim.run(generate_netlist(state), work_dir=str(work_dir))
            meas = dict(trial.measurements)
            if "phase_margin" in meas and "phase_margin_from_curve" not in meas:
                meas["phase_margin"] = normalize_phase_margin(meas["phase_margin"])
            gain = meas.get("dc_gain_db", 0.0)
            bw = meas.get("unity_gain_bandwidth", 0.0)
            pm = meas.get("phase_margin", 0.0)
            power = meas.get("total_power", 0.0)

            score = 0.0
            score += 20.0 * max(0.0, gain_min - gain) / max(gain_min, 1.0)
            score += 15.0 * max(0.0, bw_min - bw) / max(bw_min, 1.0)
            score += 25.0 * max(0.0, pm_min - pm) / max(pm_min, 1.0)
            if bw_max < float("inf"):
                score += 0.7 * max(0.0, bw - bw_max) / max(bw_max, 1.0)
            if power_max < float("inf"):
                score += 0.2 * max(0.0, power) / max(power_max, 1e-12)
            if pm >= pm_min and gain >= gain_min and bw >= bw_min:
                score -= min(pm - pm_min, 40.0) / 200.0

            if score < best["score"]:
                best = {"score": score, "Cc": cc, "Rz": rz, "measurements": meas}

    state.global_parameters["Cc"] = best["Cc"]
    state.global_parameters["Rz"] = best["Rz"]
    return best


# ═══════════════════════════════════════════════════════════════
# 3. 主流程
# ═══════════════════════════════════════════════════════════════

def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="objective_ir OTA optimizer")
    parser.add_argument("--env", default="environment.yaml", help="Environment YAML path")
    parser.add_argument("--schema", default="ir/schema.yaml", help="Schema YAML path")
    parser.add_argument("--spice", default="", help="Parse a SPICE netlist into YAML before optimization")
    parser.add_argument("--spice-yaml-out", default="", help="YAML path generated from --spice")
    parser.add_argument("--topology", choices=("auto", "five", "two_stage", "yaml"), default="auto")
    parser.add_argument("--pop-size", type=int, default=80)
    parser.add_argument("--generations", type=int, default=40)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--ngspice-bin", default=None)
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    env_path = _resolve_project_path(args.env)
    schema_path = _resolve_project_path(args.schema)
    if args.spice:
        spice_path = _resolve_project_path(args.spice)
        out_path = _resolve_project_path(args.spice_yaml_out) if args.spice_yaml_out else (
            Path(__file__).parent / "runs" / "tmp_spice_import" / f"{spice_path.stem}.yaml"
        )
        generated = parse_spice_file(spice_path)
        write_yaml(generated, out_path)
        schema_path = out_path
        args.topology = "yaml"

    print("=" * 70)
    print("  objective_ir V2.1 — process-aware · NSGA-II · ngspice")
    print("  四层 Schema 架构 (架构总纲 V1.1)")
    print(f"  env:    {env_path}")
    print(f"  schema: {schema_path}")
    if args.spice:
        print(f"  spice:  {_resolve_project_path(args.spice)}")
    print("=" * 70)

    # ── Step 0: 加载 environment.yaml ──
    print("\n[0/8] Loading environment.yaml ...")
    env = load_environment(env_path)
    print(f"       Process:  {env['process']['process_name']}")
    print(f"       Simulator: {env['simulation']['simulator']}")
    print(f"       Vdd:       {env['simulation']['supply']['vdd']}V")

    # ── Step 1: 构建 Schema ──
    print("\n[1/8] Building DesignState ...")
    state = build_design_state(env, schema_path, args.topology)
    print(f"       Topology: {state.topology.name} ({state.topology.architecture})")
    print(f"       Process:  {state.process.process_name} ({state.process.technology_node}um)")
    print(f"       Vdd:      {state.simulation.supply.get('vdd', 1.2)}V")
    print(f"       Devices:  {len(state.topology.devices)}")
    print(f"       Variables: {len(state.design_variables)}")
    for dv in state.design_variables:
        sym = f" [{dv.symmetry_label}]" if dv.symmetry_label else ""
        label = f"{dv.device}.{dv.variable}" if dv.device else dv.variable
        print(f"         {label}: [{dv.range.min:.1e}, {dv.range.max:.1e}]{sym}")
    print(f"       Loss terms: {len(state.loss_terms)}")
    for lt in state.loss_terms:
        print(f"         {lt.id}: weight={lt.weight}")
    print(f"       Evaluations: {len(state.evaluations)}")
    for ev in state.evaluations:
        print(f"         {ev.name}: type={ev.type}, probe={ev.probe}")

    # ── Step 2: Schema 验证 ──
    print("\n[2/8] Validating Schema ...")
    report = Validator().validate(state)
    print(report.summary(by_layer=True))
    if not report.schema_valid:
        print("\n[FATAL] Schema validation failed.")
        for err in report.errors():
            print(f"  ERROR: {err.message}")
        return 1

    rules_list = list_rules(layer=4)
    print(f"       Registered rules: {len(rules_list)}")

    # ── Step 3: pygmid 适配器 ──
    print("\n[3/8] Initializing pygmid adapter (Boris Murmann LookupTable) ...")
    tools_cfg = env.get("tools", {})
    tables_dir = tools_cfg.get("pygmid_tables_dir", "tables")
    nmos_table = env.get("tools", {}).get("nmos_table")
    pmos_table = env.get("tools", {}).get("pmos_table")
    explicit_tables = bool(nmos_table or pmos_table)
    pygmid = create_pygmid_adapter(
        nmos_path=_existing_project_path(nmos_table),
        pmos_path=_existing_project_path(pmos_table),
        tables_dir=None if explicit_tables else str(_resolve_project_path(tables_dir)),
    )
    print(pygmid.summary())

    # ── Step 4: 电路评估器 ──
    print("\n[4/8] Creating Circuit Evaluator ...")
    evaluator = CircuitEvaluator(state, pygmid)
    print(f"       Variables: {evaluator.n_vars}")
    print(f"       Load cap:  {state.simulation.cload*1e12:.1f}pF")

    # ── Step 5: NSGA-II 优化 ──
    print("\n[5/8] Running NSGA-II optimizer ...")
    config = NSGA2Config(
        pop_size=args.pop_size,
        n_generations=args.generations,
        crossover_prob=0.9,
        mutation_prob=0.15,
        seed=args.seed,
        verbose=True,
    )
    optimizer = NSGA2Optimizer(state, evaluator, config)
    t0 = time.time()
    best_x, best_meta = optimizer.optimize()
    opt_elapsed = time.time() - t0

    perf_est = best_meta.get("performance", {})
    print(f"\n       Optimizer completed in {opt_elapsed:.1f}s")
    print(f"       Best loss: {best_meta.get('total_loss', 0):.6f}")
    print(f"       Estimated performance:")
    for k, v in perf_est.items():
        unit = state.targets.get(k, Target()).unit
        print(f"         {k:>22s}: {v:>10.3f} {unit}")

    # ── Step 6: W/L Rounding ──
    print("\n[6/8] Rounding W/L to process grid ...")
    decoded = best_meta.get("decoded", {})
    tp = best_meta.get("transistor_params", {})
    state.global_parameters = {
        k: float(v) for k, v in decoded.get("__global__", {}).items()
    }

    # 更新 state.transistors 的物理参数
    for dev_id, vars_dict in decoded.items():
        if dev_id.startswith("__"):
            continue
        if dev_id in state.transistors:
            ts = state.transistors[dev_id]
            ts.gm_id_strategy = vars_dict.get("gm_id", 10)
            ts.L_strategy = vars_dict.get("L", 1e-7)
            if dev_id in tp:
                phys = tp[dev_id]
                ts.parameters = TransistorParameters(
                    W=phys.get("W", 0),
                    L=vars_dict.get("L", 1e-7),
                    gm=phys.get("gm", 0),
                    gds=phys.get("gds", 0),
                    vgs=phys.get("vgs", 0),
                    vds=phys.get("vds", 0),
                    vdsat=phys.get("vdsat", 0),
                    region=phys.get("region", "unknown"),
                    id=phys.get("id", 0),
                    ft=phys.get("ft", 0),
                    gm_id_realized=phys.get("gm_id", 0),
                    cgs=phys.get("cgs", 0),
                    cgd=phys.get("cgd", 0),
                )

    # Round W/L 并写回
    rounded = round_and_update_state(state, decoded, tp)
    for did, rp in rounded.items():
        ts = state.transistors.get(did)
        if ts:
            print(f"       {did}: W={ts.parameters.W*1e6:.3f}um, L={ts.parameters.L*1e9:.0f}nm")
    if state.global_parameters:
        for name, val in state.global_parameters.items():
            print(f"       {name}: {val:.4e}")

    # ── Step 7: 生成 SPICE 网表 ──
    print("\n[7/8] Generating SPICE netlist ...")
    iteration_id = _next_iteration(RUNS_DIR)
    netlist_str = generate_netlist(state)
    print(f"       Netlist length: {len(netlist_str)} chars")

    # ── Step 8: ngspice 仿真 ──
    print("\n[8/8] Running ngspice simulation ...")
    ngspice_bin = args.ngspice_bin or env.get("tools", {}).get("ngspice_bin", "ngspice")
    sim = NgspiceSimulator(timeout_sec=30, ngspice_bin=ngspice_bin)
    if not sim.check_available():
        print("       ⚠️  ngspice not available — saving structured outputs without simulation.")
        print("       Install ngspice: sudo apt install ngspice")
        missing_result = SimulationResult(
            success=False,
            return_code=-1,
            raw_stderr=f"ngspice binary not available: {ngspice_bin}",
        )
        output_dir = save_simulation_log(state, best_meta, missing_result, iteration_id, netlist_str)
        print(f"       Output → {output_dir}/")
        print(f"       agent_diagnostics.json records ngspice availability failure.")
        return 0

    # 将网表写入仿真工作目录
    output_dir = RUNS_DIR / f"iter_{iteration_id:03d}"
    output_dir.mkdir(parents=True, exist_ok=True)
    balance_info = balance_two_stage_output(state, sim, output_dir)
    if balance_info:
        netlist_str = generate_netlist(state)
        scale = balance_info.get("scale")
        sink_scale = balance_info.get("sink_scale", 1.0)
        vout = balance_info.get("vout")
        print(
            "       Stage-2 DC balance: "
            f"M6_W scale={scale:.3f}, M7_W scale={sink_scale:.3f}, vout≈{vout:.3f}V"
        )

    headroom_info = improve_tail_headroom(state, sim, output_dir)
    if headroom_info:
        netlist_str = generate_netlist(state)
        print(
            "       Tail headroom repair: "
            f"M1/M2_W scale={headroom_info['scale']:.3f}, "
            f"M5 VDS-VDSAT≈{headroom_info['margin']:.3f}V"
        )
        rebalance_info = balance_two_stage_output(state, sim, output_dir)
        if rebalance_info:
            netlist_str = generate_netlist(state)
            scale = rebalance_info.get("scale")
            sink_scale = rebalance_info.get("sink_scale", 1.0)
            vout = rebalance_info.get("vout")
            print(
                "       Stage-2 re-balance: "
                f"M6_W scale={scale:.3f}, M7_W scale={sink_scale:.3f}, vout≈{vout:.3f}V"
            )

    comp_info = tune_two_stage_compensation(state, sim, output_dir)
    if comp_info:
        netlist_str = generate_netlist(state)
        best_meta.setdefault("decoded", {})["__global__"] = dict(state.global_parameters)
        best_meta.setdefault("performance", {})["Cc"] = state.global_parameters.get("Cc", 0.0)
        best_meta.setdefault("performance", {})["Rz"] = state.global_parameters.get("Rz", 0.0)
        meas = comp_info.get("measurements", {})
        print(
            "       Compensation tune: "
            f"Cc={comp_info['Cc']:.3e}F, Rz={comp_info['Rz']:.1f}Ω, "
            f"PM≈{meas.get('phase_margin', 0):.1f}°, "
            f"UGBW≈{meas.get('unity_gain_bandwidth', 0):.3e}Hz"
        )

    result = sim.run(netlist_str, work_dir=str(output_dir))

    if "phase_margin" in result.measurements and "phase_margin_from_curve" not in result.measurements:
        result.measurements["phase_margin"] = normalize_phase_margin(result.measurements["phase_margin"])

    backfill_state_from_ngspice(state, result)

    if result.success:
        print(f"       ✅ Simulation completed in {result.elapsed_sec:.1f}s")
        print(f"       Measurements:")
        perf_keys = {"dc_gain_db", "unity_gain_bandwidth", "phase_margin", "total_power", "i_vdd"}
        for k, v in result.measurements.items():
            if k in perf_keys or k.startswith("m"):
                print(f"         {k:>20s}: {v:>12.4e}")
        if result.operating_points:
            print(f"       Operating points: {len(result.operating_points)} devices")
            for did, op in sorted(result.operating_points.items()):
                gate_v = op.get("gate", 0)
                _id = op.get("id", 0)
                print(f"         {did}: id={_id:.3e}A, vg={gate_v:.3f}V")
    else:
        print(f"       ⚠️  Simulation completed with issues")
        print(f"       Return code: {result.return_code}")
        if result.raw_stderr:
            # 只打印最后几行错误
            err_lines = result.raw_stderr.strip().split("\n")[-5:]
            for line in err_lines:
                print(f"       {line}")

    # ── 保存完整仿真 log ──
    save_log = save_simulation_log(state, best_meta, result, iteration_id, netlist_str)
    print(f"\n{'='*70}")
    print(f"  ✅ Output → {save_log}/")
    print(f"     design_state.yaml    Schema + transistor state")
    print(f"     netlist.cir          SPICE netlist")
    print(f"     sim_log.json         Optimizer results + ngspice measurements")
    print(f"     agent_diagnostics.json Structured diagnostics for agents")
    print(f"{'='*70}")

    # ── 对比简表 ──
    if result.success:
        print(f"\n  📊 Optimizer vs ngspice comparison:")
        print(f"       {'':>22s} {'Optimizer':>12s} {'ngspice':>12s} {'Δ':>10s}")
        print(f"       {'':>22s} {'─'*12} {'─'*12} {'─'*10}")
        meas = result.measurements
        for key, est_val in perf_est.items():
            ng_key = {
                "dc_gain": "dc_gain_db",
                "unity_gain_bandwidth": "unity_gain_bandwidth",
                "phase_margin": "phase_margin",
                "power": "total_power",
            }.get(key, key)
            ng_val = meas.get(ng_key)
            unit = state.targets.get(key, Target()).unit
            est_str = f"{est_val:.2e} {unit}" if abs(est_val) < 0.01 else f"{est_val:.1f} {unit}"
            ng_str = f"{ng_val:.2e} {unit}" if ng_val is not None and abs(ng_val) < 0.01 else (f"{ng_val:.1f} {unit}" if ng_val is not None else "N/A")
            delta_str = f"{ng_val - est_val:+.2e}" if ng_val is not None else ""
            print(f"       {key:>22s}: {est_str:>12s} {ng_str:>12s} {delta_str:>10s}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

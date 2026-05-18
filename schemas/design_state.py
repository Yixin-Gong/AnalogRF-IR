"""
Schema V2.0 — 设计状态文件 design_state.yaml 的 Python 数据模型。

四层 Schema 架构 (架构总纲 V1.1):
  L1 — 设计宪法:   topology + targets         (仅人可写, Agent 只读)
  L2 — 优化问题:   constraints + loss_terms   (人 + Agent 可写)
  L3 — 评估声明:   evaluations                (人 + Agent 可写)
  L4 — 物理状态:   transistors 物理量          (仅执行脚本可写, 只读)

这是系统的唯一设计状态源 (Single Source of Truth)。
Agent 通过读写此文件与物理执行层交互，遵守"认知-执行分离"原则。
"""

from __future__ import annotations

import copy
import yaml
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any, Union, Tuple
from pathlib import Path

from util.units import parse_value


# ── L1: 拓扑层 ──────────────────────────────────────────────

@dataclass
class GlobalNet:
    """全局网络定义（电源、地等）。"""
    name: str          # e.g. vdd, gnd
    type: str          # supply | ground


@dataclass
class Port:
    """电路端口定义。"""
    id: str            # e.g. vinp, vinn, vout, vbias
    direction: str     # input | output | bias | supply


@dataclass
class DeviceDefinition:
    """
    器件的拓扑定义（静态，不随迭代改变）。
    connections 直接对应 ngspice 网表的节点连接。
    """
    id: str                              # M1, M2, ...
    role: str                            # input_pair | current_mirror_load | tail_current_source | cascode | ...
    stage: str = "core"                  # input | core | output | bias
    type: str = "nmos"                   # nmos | pmos
    model: str = "nch_18"               # SPICE model name
    connections: Dict[str, str] = field(default_factory=dict)


@dataclass
class Topology:
    """电路拓扑的完整语义描述 (L1 — 仅人可写)。"""
    name: str = "unnamed"
    class_: str = "ota"
    architecture: str = "single-stage"
    global_nets: List[GlobalNet] = field(default_factory=list)
    ports: List[Port] = field(default_factory=list)
    devices: List[DeviceDefinition] = field(default_factory=list)


# ── L1: 规格层 ──────────────────────────────────────────────

@dataclass
class Target:
    """单个性能指标规格 (L1 — 仅人可写)。"""
    min: Optional[float] = None
    max: Optional[float] = None
    unit: str = ""
    priority: int = 1


@dataclass
class Range:
    """数值范围。"""
    min: float
    max: float


# ── L2: 优化问题定义 ──────────────────────────────────────────

@dataclass
class DeviceConstraint:
    """单个器件的约束边界 (L2)。"""
    gm_id: Optional[Range] = None
    L: Optional[Range] = None
    VDS_min: Optional[float] = None
    VGS_max: Optional[float] = None


@dataclass
class Constraints:
    """
    优化器的搜索空间限制 (L2 — Agent 可修改)。

    支持两种序列化格式:
      新格式 (架构总纲 V1.1 平铺):
        constraints:
          M1: {gm_id: [8, 20], L: [0.13e-6, 2e-6]}
      旧格式 (嵌套, 向后兼容):
        constraints:
          global: {gm_id: {min: 5, max: 25}}
          per_device: {M1: {gm_id: {min: 12, max: 25}}}
    """
    global_: Dict[str, Range] = field(default_factory=dict)
    per_device: Dict[str, DeviceConstraint] = field(default_factory=dict)

    def get_gm_id_range(self, device_id: str) -> Range:
        if device_id in self.per_device and self.per_device[device_id].gm_id:
            return self.per_device[device_id].gm_id
        return self.global_.get("gm_id", Range(5, 25))

    def get_L_range(self, device_id: str) -> Range:
        if device_id in self.per_device and self.per_device[device_id].L:
            return self.per_device[device_id].L
        return self.global_.get("L", Range(1.8e-7, 2.0e-6))


@dataclass
class DesignVariable:
    """单个优化器决策变量（扁平化，供 NSGA-II 使用）。

    支持两类变量:
      - 器件变量: device="M1", variable="gm_id" | "L"
      - 全局变量: device="", variable="I_tail"  (device 为空)
    """
    device: str = ""
    variable: str = ""        # gm_id | L | I_tail | ...
    range: Range = field(default_factory=lambda: Range(0, 1))
    initial: Optional[float] = None   # 优化器种子值 (None → 取 range 中点)
    symmetry_label: Optional[str] = None
    unit: str = ""
    description: str = ""


@dataclass
class CorrectionFactors:
    """估计模型校正因子 — 从 ngspice 回测更新，不进优化器逻辑。

    Agent 可修改这些值以改善 pygmid 查找表与 ngspice BSIM4 之间
    的系统性偏差。Optimizer 只读。
    """
    gm_factor: float = 1.0          # gm 缩放 (VDS/体效应校正)
    gds_factor: float = 1.0         # gds 缩放
    c_factor: float = 1.0           # 电容缩放
    description: str = ""


# ── L4: 晶体管运行时状态层 ─────────────────────────────────────

@dataclass
class TransistorParameters:
    """仿真后回填的物理参数 (L4 — 仅脚本可写)。"""
    W: float = 0.0
    L: float = 0.0
    gm: float = 0.0
    gds: float = 0.0
    vgs: float = 0.0
    vds: float = 0.0
    vdsat: float = 0.0
    region: str = "unknown"
    id: float = 0.0
    ft: float = 0.0
    gm_id_realized: float = 0.0
    cgg: float = 0.0
    cdd: float = 0.0
    cgs: float = 0.0
    cgd: float = 0.0
    ic: float = 0.0          # 反型系数 (inversion coefficient), L4 脚本回填


@dataclass
class TransistorState:
    """单个晶体管的完整状态 (L4 — 仅脚本可写)。"""
    device_id: str
    role: str
    type: str
    model: str
    connections: Dict[str, str] = field(default_factory=dict)
    gm_id_strategy: float = 10.0
    L_strategy: float = 1.0e-6
    parameters: TransistorParameters = field(default_factory=TransistorParameters)


# ── L2: Loss 定义 ──────────────────────────────────────────────

@dataclass
class LossTerm:
    """
    单项 Loss 定义 (L2 — Agent 可修改 formula 和 weight)。

    formula 白名单变量: realized.*, targets.*, device.Mx.*
    白名单函数: relu, abs, max, min, sqrt, pow, log10, penalty_if
    """
    id: str
    formula: str
    weight: float = 1.0
    description: str = ""


# ── L3: 评估声明 ──────────────────────────────────────────────

@dataclass
class Evaluation:
    """
    评估声明 (L3 — Agent 可修改): 定义要测量什么、如何测量。

    驱动 ngspice .meas 生成和仿真结果解析。
    """
    name: str                    # e.g. dc_gain, phase_margin, total_power, region_M1
    type: str                    # ac_gain | ugbw | phase_margin | dc_power | operating_region | cmrr | psrr
    probe: str = ""             # 探测节点, e.g. vout
    device: str = ""            # 关联器件, e.g. M1
    target_ref: str = ""        # 对应的 target key, e.g. dc_gain
    meas_formula: str = ""      # 可选: 自定义 .meas 公式


# ── 仿真配置 ──────────────────────────────────────────────────

@dataclass
class SimulationConfig:
    """ngspice 仿真配置。"""
    temperature: float = 27.0
    supply: Dict[str, float] = field(default_factory=dict)
    model_lib: str = ""
    analyses: List[str] = field(default_factory=lambda: ["op", "ac", "dc"])
    ac_start: float = 1.0
    ac_stop: float = 1e9
    ac_points: int = 50
    cload: float = 1e-12
    bias_voltage: float = 0.6


# ── 工艺信息 ──────────────────────────────────────────────────

@dataclass
class ProcessInfo:
    """工艺信息 — 设计规则、物理参数、可靠性约束的来源。"""
    process_name: str = ""
    technology_node: float = 0.13
    foundry: str = ""
    model_lib: str = ""
    nmos_model: str = "nmos"
    pmos_model: str = "pmos"
    VTH_n: float = 0.3782
    VTH_p: float = 0.321
    KP_n: float = 910e-6
    KP_p: float = 123e-6
    COX: float = 1.535e-2
    min_L: float = 1.3e-7
    max_L: float = 10e-6
    min_W: float = 1.5e-7
    max_W: float = 200e-6
    W_precision: float = 1e-9
    L_precision: float = 1e-9
    min_area: float = 2e-14
    max_W_L_ratio: float = 1000.0
    min_W_L_ratio: float = 0.1
    max_finger_width: float = 10e-6
    max_VGS: float = 1.32
    max_VDS: float = 1.32
    VDS_safe_margin: float = 0.15
    nominal_VDD: float = 1.2
    VDD_min: float = 1.08
    VDD_max: float = 1.32
    gm_id_min: float = 3.0
    gm_id_max: float = 28.0
    VDSAT_headroom_factor: float = 1.3
    n_sub_nmos: float = 1.4       # NMOS 亚阈值斜率因子
    n_sub_pmos: float = 1.4       # PMOS 亚阈值斜率因子
    mu_n: float = 0.04            # NMOS 电子迁移率 [m²/V·s] (PTM 130nm 典型)
    mu_p: float = 0.01            # PMOS 空穴迁移率 [m²/V·s] (PTM 130nm 典型)


# ── 历史记录层 ─────────────────────────────────────────────

@dataclass
class HistoryEntry:
    """单次外层迭代的完整记录 (Agent 写入, 确保变更可追溯)。"""
    iteration: int
    timestamp: str = ""
    strategy: str = ""
    diagnosis: str = ""                  # 物理诊断依据 (强制: Agent 必须填写)
    constraint_changes: List[str] = field(default_factory=list)
    loss_weight_changes: List[str] = field(default_factory=list)
    loss_formula_changes: List[str] = field(default_factory=list)
    evaluation_changes: List[str] = field(default_factory=list)
    final_loss: float = 0.0
    final_performance: Dict[str, float] = field(default_factory=dict)
    convergence: bool = False
    transistor_snapshot: Dict[str, TransistorParameters] = field(default_factory=dict)


# ── 顶层设计状态 ───────────────────────────────────────────

@dataclass
class DesignState:
    """
    设计状态 V2.0 — 系统的唯一真相源。

    四层架构:
      L1 (仅人):    topology, targets, constraints
      L2 (人+Agent): design_variables, loss_terms
      L3 (人+Agent): evaluations
      L4 (仅脚本):   transistors
    """
    schema_version: str = "2.0"
    design_name: str = "unnamed"

    # L1
    topology: Topology = field(default_factory=Topology)
    targets: Dict[str, Target] = field(default_factory=dict)
    constraints: Constraints = field(default_factory=Constraints)

    # L2
    design_variables: List[DesignVariable] = field(default_factory=list)
    loss_terms: List[LossTerm] = field(default_factory=list)
    corrections: CorrectionFactors = field(default_factory=CorrectionFactors)

    # L3
    evaluations: List[Evaluation] = field(default_factory=list)

    # L4
    transistors: Dict[str, TransistorState] = field(default_factory=dict)
    global_parameters: Dict[str, float] = field(default_factory=dict)

    # 工具链配置
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    process: ProcessInfo = field(default_factory=ProcessInfo)

    # 历史
    history: List[HistoryEntry] = field(default_factory=list)

    # ── 序列化 ──

    def to_dict(self) -> dict:
        return _dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DesignState":
        return _dict_to_design_state(d)

    def to_yaml(self, path: Union[str, Path]) -> None:
        self._ensure_wl_on_grid()
        path = Path(path)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(self.to_dict(), f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def _ensure_wl_on_grid(self) -> None:
        import math
        proc = self.process
        W_grid = getattr(proc, "W_precision", 10e-9)
        L_grid = getattr(proc, "L_precision", 1e-9)
        W_min  = getattr(proc, "min_W", 150e-9)
        L_min  = getattr(proc, "min_L", 130e-9)
        W_dec = max(0, int(-math.floor(math.log10(W_grid)))) if W_grid > 0 else 0
        L_dec = max(0, int(-math.floor(math.log10(L_grid)))) if L_grid > 0 else 0
        for dev_id, ts in self.transistors.items():
            W = ts.parameters.W
            L = ts.parameters.L
            if W > 0:
                W = max(W, W_min)
                n = int(round(W / W_grid))
                ts.parameters.W = round(n * W_grid, W_dec)
            if L > 0:
                L = max(L, L_min)
                n = int(round(L / L_grid))
                ts.parameters.L = round(n * L_grid, L_dec)

    @classmethod
    def from_yaml(cls, path: Union[str, Path]) -> "DesignState":
        path = Path(path)
        with open(path, "r", encoding="utf-8") as f:
            d = yaml.safe_load(f)
        return cls.from_dict(d)

    def clone(self) -> "DesignState":
        return copy.deepcopy(self)

    def get_device_def(self, device_id: str) -> Optional[DeviceDefinition]:
        for dev in self.topology.devices:
            if dev.id == device_id:
                return dev
        return None

    def build_design_variables(self) -> None:
        """从 constraints 构建 design_variables（兜底：YAML 已声明时跳过）。"""
        # 如果 design_variables 已由 YAML/Agent 填充，仅标记对称
        if self.design_variables:
            self.design_variables = self._mark_pair_symmetry(self.design_variables)
            return

        dv = []
        for dev in self.topology.devices:
            ts = self.transistors.get(dev.id)
            gm_id_range = self.constraints.get_gm_id_range(dev.id)
            dv.append(DesignVariable(
                device=dev.id, variable="gm_id", range=gm_id_range,
                initial=ts.gm_id_strategy if ts else None,
            ))
            L_range = self.constraints.get_L_range(dev.id)
            dv.append(DesignVariable(
                device=dev.id, variable="L", range=L_range,
                initial=ts.L_strategy if ts else None,
            ))
        dv = self._mark_pair_symmetry(dv)
        self.design_variables = dv

    def _mark_pair_symmetry(self, dv: List[DesignVariable]) -> List[DesignVariable]:
        input_pair = [dev.id for dev in self.topology.devices if dev.role == "input_pair"]
        if len(input_pair) == 2:
            label = f"sym_{input_pair[0]}_{input_pair[1]}"
            for var in dv:
                if var.device in input_pair:
                    var.symmetry_label = label
        load_pair = [dev.id for dev in self.topology.devices if "current_mirror" in dev.role]
        if len(load_pair) >= 2:
            label = f"sym_load_{load_pair[0]}_{load_pair[1]}"
            for var in dv:
                if var.device in load_pair[:2]:
                    var.symmetry_label = label
        return dv

    def get_evaluation(self, name: str) -> Optional[Evaluation]:
        for ev in self.evaluations:
            if ev.name == name:
                return ev
        return None


# ── 序列化辅助函数 ──────────────────────────────────────────

def _dataclass_to_dict(obj: Any) -> Any:
    if isinstance(obj, list):
        return [_dataclass_to_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in obj.items()}
    elif hasattr(obj, "__dataclass_fields__"):
        result = {}
        for f_name in obj.__dataclass_fields__:
            value = getattr(obj, f_name)
            try:
                is_empty = (value is None or value == "" or value == [] or value == {})
            except (ValueError, TypeError):
                is_empty = False
            if is_empty:
                if f_name not in ("id", "device_id", "name", "schema_version", "design_name",
                                  "formula", "description", "target_ref", "meas_formula",
                                  "probe", "device"):
                    continue
            key = "class" if f_name == "class_" else f_name
            result[key] = _dataclass_to_dict(value)
        return result
    elif isinstance(obj, float):
        return float(obj)
    elif isinstance(obj, int) and not isinstance(obj, bool):
        return int(obj)
    else:
        try:
            import numpy as np
            if isinstance(obj, (np.floating,)):
                return float(obj)
            if isinstance(obj, (np.integer,)):
                return int(obj)
        except ImportError:
            pass
        return obj


def _parse_range(v: dict) -> Range:
    min_raw = v.get("min", 0)
    max_raw = v.get("max", 0)
    if isinstance(min_raw, str):
        min_raw = parse_value(min_raw)
    if isinstance(max_raw, str):
        max_raw = parse_value(max_raw)
    return Range(min=min_raw, max=max_raw)


def _dict_to_design_state(d: dict) -> DesignState:
    # topology
    topo_d = d.get("topology", {})
    global_nets = [GlobalNet(**gn) for gn in topo_d.get("global_nets", [])]
    ports = [Port(**p) for p in topo_d.get("ports", [])]
    devices = [DeviceDefinition(
        id=dev["id"], role=dev.get("role", ""), stage=dev.get("stage", "core"),
        type=dev.get("type", "nmos"), model=dev.get("model", "nch_18"),
        connections=dev.get("connections", {})
    ) for dev in topo_d.get("devices", [])]
    topology = Topology(
        name=topo_d.get("name", "unnamed"), class_=topo_d.get("class", "ota"),
        architecture=topo_d.get("architecture", "single-stage"),
        global_nets=global_nets, ports=ports, devices=devices
    )

    # targets
    targets = {}
    for k, v in d.get("targets", {}).items():
        min_raw = v.get("min"); max_raw = v.get("max"); unit = v.get("unit", "")
        if isinstance(min_raw, str): min_raw = parse_value(min_raw)
        if isinstance(max_raw, str): max_raw = parse_value(max_raw)
        targets[k] = Target(min=min_raw, max=max_raw, unit=unit, priority=v.get("priority", 1))

    # constraints (支持新旧两种格式)
    cons_d = d.get("constraints", {})
    global_cons = {}
    per_device_cons = {}
    has_device_keys = any(
        (isinstance(k, str) and (k.startswith("M") or k.startswith("m")))
        for k in cons_d.keys()
    )
    if has_device_keys:
        for dev_id, dev_c in cons_d.items():
            if not isinstance(dev_c, dict): continue
            pd = DeviceConstraint()
            if "gm_id" in dev_c:
                gm_val = dev_c["gm_id"]
                if isinstance(gm_val, list): pd.gm_id = Range(min=gm_val[0], max=gm_val[1])
                elif isinstance(gm_val, dict): pd.gm_id = _parse_range(gm_val)
            if "L" in dev_c:
                l_val = dev_c["L"]
                if isinstance(l_val, list): pd.L = Range(min=l_val[0], max=l_val[1])
                elif isinstance(l_val, dict): pd.L = _parse_range(l_val)
            if "VDS_min" in dev_c: pd.VDS_min = dev_c["VDS_min"]
            if "VGS_max" in dev_c: pd.VGS_max = dev_c["VGS_max"]
            per_device_cons[dev_id] = pd
    else:
        for k, v in cons_d.get("global", {}).items():
            global_cons[k] = _parse_range(v)
        for dev_id, dev_c in cons_d.get("per_device", {}).items():
            pd = DeviceConstraint()
            if "gm_id" in dev_c: pd.gm_id = _parse_range(dev_c["gm_id"])
            if "L" in dev_c: pd.L = _parse_range(dev_c["L"])
            if "VDS_min" in dev_c: pd.VDS_min = dev_c["VDS_min"]
            if "VGS_max" in dev_c: pd.VGS_max = dev_c["VGS_max"]
            per_device_cons[dev_id] = pd
    constraints = Constraints(global_=global_cons, per_device=per_device_cons)

    # design_variables
    design_vars = []
    for dv in d.get("design_variables", []):
        r = dv.get("range", {})
        if isinstance(r, dict):
            r = Range(**r)
        design_vars.append(DesignVariable(
            device=dv.get("device", ""), variable=dv.get("variable", ""),
            range=r, initial=dv.get("initial"),
            symmetry_label=dv.get("symmetry_label"),
            unit=dv.get("unit", ""), description=dv.get("description", ""),
        ))

    # corrections
    corr_d = d.get("corrections", {})
    corrections = CorrectionFactors(
        gm_factor=corr_d.get("gm_factor", 1.0),
        gds_factor=corr_d.get("gds_factor", 1.0),
        c_factor=corr_d.get("c_factor", 1.0),
        description=corr_d.get("description", ""),
    )

    # transistors
    transistors = {}
    for tid, ts in d.get("transistors", {}).items():
        params = TransistorParameters()
        if "parameters" in ts: params = TransistorParameters(**ts["parameters"])
        transistors[tid] = TransistorState(
            device_id=tid, role=ts.get("role", ""), type=ts.get("type", "nmos"),
            model=ts.get("model", "nch_18"), connections=ts.get("connections", {}),
            gm_id_strategy=ts.get("gm_id_strategy", 10.0),
            L_strategy=ts.get("L_strategy", 1.0e-6), parameters=params
        )

    # loss_terms
    loss_terms = [LossTerm(**lt) for lt in d.get("loss_terms", [])]

    # evaluations (L3)
    evaluations = []
    for ev in d.get("evaluations", []):
        evaluations.append(Evaluation(
            name=ev.get("name", ""), type=ev.get("type", ""),
            probe=ev.get("probe", ""), device=ev.get("device", ""),
            target_ref=ev.get("target_ref", ""), meas_formula=ev.get("meas_formula", ""),
        ))

    # simulation
    sim_d = d.get("simulation", {})
    simulation = SimulationConfig(
        temperature=sim_d.get("temperature", 27.0), supply=sim_d.get("supply", {}),
        model_lib=sim_d.get("model_lib", ""), analyses=sim_d.get("analyses", ["op", "ac"]),
        ac_start=sim_d.get("ac_start", 1.0), ac_stop=sim_d.get("ac_stop", 1e9),
        ac_points=sim_d.get("ac_points", 50), cload=sim_d.get("cload", 1e-12)
    )

    # process
    proc_d = d.get("process", {})
    process = ProcessInfo(**{k: v for k, v in proc_d.items()
                              if k in ProcessInfo.__dataclass_fields__})

    # history
    history = []
    for he in d.get("history", []):
        snap = {}
        for k, v in he.get("transistor_snapshot", {}).items():
            snap[k] = TransistorParameters(**v)
        history.append(HistoryEntry(
            iteration=he.get("iteration", 0), timestamp=he.get("timestamp", ""),
            strategy=he.get("strategy", ""), diagnosis=he.get("diagnosis", ""),
            constraint_changes=he.get("constraint_changes", []),
            loss_weight_changes=he.get("loss_weight_changes", []),
            loss_formula_changes=he.get("loss_formula_changes", []),
            evaluation_changes=he.get("evaluation_changes", []),
            final_loss=he.get("final_loss", 0.0),
            final_performance=he.get("final_performance", {}),
            convergence=he.get("convergence", False),
            transistor_snapshot=snap
        ))

    return DesignState(
        schema_version=d.get("schema_version", "2.0"),
        design_name=d.get("design_name", "unnamed"),
        topology=topology, targets=targets, constraints=constraints,
        design_variables=design_vars, transistors=transistors,
        global_parameters=d.get("global_parameters", {}),
        loss_terms=loss_terms, evaluations=evaluations,
        simulation=simulation, process=process, history=history,
        corrections=corrections,
    )

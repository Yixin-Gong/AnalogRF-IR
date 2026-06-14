"""AnalogRF-IR internal documentation."""

from __future__ import annotations

import copy
import yaml
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Union, Tuple
from pathlib import Path

from util.units import parse_value


# Internal implementation note.

@dataclass
class GlobalNet:
    """AnalogRF-IR internal documentation."""
    name: str          # e.g. vdd, gnd
    type: str          # supply | ground


@dataclass
class Port:
    """AnalogRF-IR internal documentation."""
    id: str            # e.g. vinp, vinn, vout, vbias
    direction: str     # input | output | bias | supply


@dataclass
class DeviceDefinition:
    """AnalogRF-IR internal documentation."""
    id: str                              # M1, M2, ...
    role: str                            # input_pair | current_mirror_load | tail_current_source | cascode | ...
    stage: str = "core"                  # input | core | output | bias
    type: str = "nmos"                   # nmos | pmos
    model: str = "nch_18"               # SPICE model name
    connections: Dict[str, str] = field(default_factory=dict)


@dataclass
class Topology:
    """AnalogRF-IR internal documentation."""
    name: str = "unnamed"
    class_: str = "ota"
    architecture: str = "single-stage"
    global_nets: List[GlobalNet] = field(default_factory=list)
    ports: List[Port] = field(default_factory=list)
    devices: List[DeviceDefinition] = field(default_factory=list)


# Internal implementation note.

@dataclass
class Target:
    """AnalogRF-IR internal documentation."""
    min: Optional[float] = None
    max: Optional[float] = None
    unit: str = ""
    priority: int = 1


@dataclass
class Range:
    """AnalogRF-IR internal documentation."""
    min: float
    max: float


# Internal implementation note.

@dataclass
class DeviceConstraint:
    """AnalogRF-IR internal documentation."""
    gm_id: Optional[Range] = None
    L: Optional[Range] = None
    VDS_min: Optional[float] = None
    VGS_max: Optional[float] = None


@dataclass
class Constraints:
    """AnalogRF-IR internal documentation."""
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
    """AnalogRF-IR internal documentation."""
    device: str = ""
    variable: str = ""        # gm_id | L | I_tail | ...
    range: Range = field(default_factory=lambda: Range(0, 1))
    initial: Optional[float] = None   # Internal implementation note.
    symmetry_label: Optional[str] = None
    unit: str = ""
    description: str = ""


@dataclass
class CorrectionFactors:
    """AnalogRF-IR internal documentation."""
    gm_factor: float = 1.0          # Internal implementation note.
    gds_factor: float = 1.0         # Internal implementation note.
    c_factor: float = 1.0           # Internal implementation note.
    description: str = ""


# Internal implementation note.

@dataclass
class TransistorParameters:
    """AnalogRF-IR internal documentation."""
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
    ic: float = 0.0          # Internal implementation note.
    layout_fingers: int = 1
    layout_parallel: int = 1
    layout_series: int = 1
    layout_finger_W: float = 0.0
    layout_instance_W: float = 0.0
    layout_segment_L: float = 0.0


@dataclass
class TransistorState:
    """AnalogRF-IR internal documentation."""
    device_id: str
    role: str
    type: str
    model: str
    connections: Dict[str, str] = field(default_factory=dict)
    gm_id_strategy: float = 10.0
    L_strategy: float = 1.0e-6
    parameters: TransistorParameters = field(default_factory=TransistorParameters)


# Internal implementation note.

@dataclass
class LossTerm:
    """AnalogRF-IR internal documentation."""
    id: str
    formula: str
    weight: float = 1.0
    description: str = ""


# Internal implementation note.

@dataclass
class Evaluation:
    """AnalogRF-IR internal documentation."""
    name: str                    # e.g. dc_gain, phase_margin, total_power, region_M1
    type: str                    # ac_gain | ugbw | phase_margin | dc_power | operating_region | cmrr | psrr
    probe: str = ""             # Internal implementation note.
    device: str = ""            # Internal implementation note.
    target_ref: str = ""        # Internal implementation note.
    meas_formula: str = ""      # Internal implementation note.


# Internal implementation note.

@dataclass
class SimulationConfig:
    """AnalogRF-IR internal documentation."""
    temperature: float = 27.0
    supply: Dict[str, float] = field(default_factory=dict)
    model_lib: str = ""
    analyses: List[str] = field(default_factory=lambda: ["op", "ac", "dc"])
    ac_start: float = 1.0
    ac_stop: float = 1e9
    ac_points: int = 50
    cload: float = 1e-12
    bias_voltage: float = 0.6


# Internal implementation note.

@dataclass
class ProcessInfo:
    """AnalogRF-IR internal documentation."""
    process_name: str = ""
    technology_node: float = 0.13
    foundry: str = ""
    model_lib: str = ""
    model_corner: str = ""
    osdi_libs: List[str] = field(default_factory=list)
    device_style: str = "mos"            # mos | subckt
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
    n_sub_nmos: float = 1.4       # Internal implementation note.
    n_sub_pmos: float = 1.4       # Internal implementation note.
    mu_n: float = 0.04            # Internal implementation note.
    mu_p: float = 0.01            # Internal implementation note.


# Internal implementation note.

@dataclass
class HistoryEntry:
    """AnalogRF-IR internal documentation."""
    iteration: int
    timestamp: str = ""
    strategy: str = ""
    diagnosis: str = ""                  # Internal implementation note.
    constraint_changes: List[str] = field(default_factory=list)
    loss_weight_changes: List[str] = field(default_factory=list)
    loss_formula_changes: List[str] = field(default_factory=list)
    evaluation_changes: List[str] = field(default_factory=list)
    final_loss: float = 0.0
    final_performance: Dict[str, float] = field(default_factory=dict)
    convergence: bool = False
    transistor_snapshot: Dict[str, TransistorParameters] = field(default_factory=dict)


# Internal implementation note.

@dataclass
class DesignState:
    """AnalogRF-IR internal documentation."""
    schema_version: str = "0.1"
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

    # Internal implementation note.
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    process: ProcessInfo = field(default_factory=ProcessInfo)

    # Internal implementation note.
    history: List[HistoryEntry] = field(default_factory=list)

    # Runtime outputs derived from optimization, validation, simulation, and diagnosis.
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    # Internal implementation note.

    def to_dict(self, *, include_runtime_context: bool = True) -> dict:
        data = _dataclass_to_dict(self)
        _compact_serialized_runtime_state(data)
        if not include_runtime_context:
            data.pop("process", None)
            data.pop("simulation", None)
        return data

    @classmethod
    def from_dict(cls, d: dict) -> "DesignState":
        return _dict_to_design_state(d)

    def to_yaml(self, path: Union[str, Path], *, include_runtime_context: bool = True) -> None:
        state = self.clone()
        state._ensure_wl_on_grid()
        path = Path(path)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(
                state.to_dict(include_runtime_context=include_runtime_context),
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

    def _ensure_wl_on_grid(self) -> None:
        import math
        proc = self.process
        W_grid = getattr(proc, "W_precision", 10e-9)
        L_grid = getattr(proc, "L_precision", 1e-9)
        W_min  = getattr(proc, "min_W", 150e-9)
        L_min  = getattr(proc, "min_L", 130e-9)
        W_max = getattr(proc, "max_W", 200e-6)
        min_area = getattr(proc, "min_area", 0.0)
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
            if min_area > 0 and ts.parameters.W > 0 and ts.parameters.L > 0:
                area = ts.parameters.W * ts.parameters.L
                if area < min_area:
                    required_w = min_area / ts.parameters.L
                    n = int(math.ceil(required_w / W_grid))
                    ts.parameters.W = round(min(max(n * W_grid, W_min), W_max), W_dec)

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
        """AnalogRF-IR internal documentation."""
        # Internal implementation note.
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


# Internal implementation note.

def _dataclass_to_dict(obj: Any) -> Any:
    if isinstance(obj, list):
        return [_dataclass_to_dict(item) for item in obj]
    elif isinstance(obj, tuple):
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


def _compact_serialized_runtime_state(data: dict[str, Any]) -> None:
    diagnostics = data.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return
    causal = diagnostics.get("causal_diagnostics")
    if isinstance(causal, dict):
        diagnostics["causal_diagnostics"] = _compact_serialized_causal_diagnostics(causal)
    commands = diagnostics.get("agent_tool_commands")
    if isinstance(commands, list):
        diagnostics["agent_tool_commands"] = [
            _compact_serialized_agent_command(command)
            for command in commands
            if isinstance(command, dict)
        ]


def _compact_serialized_causal_diagnostics(causal: dict[str, Any]) -> dict[str, Any]:
    out = dict(causal)
    if isinstance(out.get("root_cause_attribution"), list):
        out["root_cause_attribution"] = [
            _compact_serialized_root_cause(item)
            for item in out["root_cause_attribution"][:5]
            if isinstance(item, dict)
        ]
    if isinstance(out.get("constrained_action_optimizer"), dict):
        out["constrained_action_optimizer"] = _compact_serialized_action_optimizer(out["constrained_action_optimizer"])
    if isinstance(out.get("attribution_guided_tuning"), dict):
        out["attribution_guided_tuning"] = _compact_serialized_attribution_tuning(out["attribution_guided_tuning"])
    for heavy_key in ("dependency_graph", "agent_failure_attribution", "counterfactual_predictions", "suggested_validation_experiments"):
        out.pop(heavy_key, None)
    return out


def _compact_serialized_root_cause(item: dict[str, Any]) -> dict[str, Any]:
    keep = ("node", "score", "metrics", "component", "score_components", "propagation_path")
    return {key: item[key] for key in keep if key in item}


def _compact_serialized_action_optimizer(optimizer: dict[str, Any]) -> dict[str, Any]:
    keep = ("schema_version", "status", "model_source", "objective_before", "objective_after", "objective_improvement")
    out = {key: optimizer[key] for key in keep if key in optimizer}
    if isinstance(optimizer.get("strategy"), dict):
        out["strategy"] = _compact_serialized_strategy(optimizer["strategy"])
    if isinstance(optimizer.get("selected_actions"), list):
        out["selected_actions"] = [
            _compact_serialized_tuning_action(item)
            for item in optimizer["selected_actions"][:5]
            if isinstance(item, dict)
        ]
    return out


def _compact_serialized_attribution_tuning(tuning: dict[str, Any]) -> dict[str, Any]:
    out = {
        "author": tuning.get("author", ""),
        "planning_mode": tuning.get("planning_mode", ""),
    }
    if isinstance(tuning.get("decision_model"), dict):
        out["decision_model"] = _compact_serialized_decision_model(tuning["decision_model"])
    if isinstance(tuning.get("hard_physical_gate"), dict):
        out["hard_physical_gate"] = {
            "executor": tuning["hard_physical_gate"].get("executor", ""),
        }
    if isinstance(tuning.get("by_failure"), list):
        out["by_failure"] = [
            _compact_serialized_tuning_failure(item)
            for item in tuning["by_failure"]
            if isinstance(item, dict)
        ]
    return out


def _compact_serialized_decision_model(model: dict[str, Any]) -> dict[str, Any]:
    keep = ("type", "optimizer_status", "selected_action_ids", "objective_before", "objective_after", "model_source")
    out = {key: model[key] for key in keep if key in model}
    if isinstance(model.get("strategy"), dict):
        out["strategy"] = _compact_serialized_strategy(model["strategy"])
    return out


def _compact_serialized_strategy(strategy: dict[str, Any]) -> dict[str, Any]:
    keep = ("name", "planning_mode")
    return {key: strategy[key] for key in keep if key in strategy}


def _compact_serialized_tuning_failure(item: dict[str, Any]) -> dict[str, Any]:
    actions = item.get("actions", []) if isinstance(item.get("actions"), list) else []
    compact_actions = [
        _compact_serialized_tuning_action(action)
        for action in _select_serialized_tuning_actions(actions)
        if isinstance(action, dict)
    ]
    out = {
        "metric": item.get("metric"),
        "observed_direction": item.get("observed_direction"),
        "target_gap": item.get("target_gap", {}),
        "action_count": item.get("action_count", len(actions)),
        "omitted_action_count": max(0, len(actions) - len(compact_actions)),
        "actions": compact_actions,
    }
    strategy = str(item.get("strategy", "") or "")
    if strategy:
        out["strategy"] = strategy if len(strategy) <= 160 else strategy[:159].rstrip() + "..."
    return out


def _select_serialized_tuning_actions(actions: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    selected = [action for action in actions if isinstance(action, dict) and action.get("optimizer_selected")]
    out = selected[:limit]
    base_limit = min(limit, max(3, len(out)))
    for action in actions:
        if len(out) >= base_limit:
            break
        if action not in out:
            out.append(action)
    for action in actions:
        if len(out) >= limit:
            break
        if not isinstance(action, dict) or action in out:
            continue
        knob = str(action.get("knob", ""))
        action_class = str(action.get("action_class", ""))
        if knob.startswith("global.vbias") or action_class in {"operating_point_headroom", "telescopic_stack_balance"}:
            out.append(action)
    for action in actions:
        if len(out) >= limit:
            break
        if isinstance(action, dict) and action not in out:
            out.append(action)
    return out


def _compact_serialized_tuning_action(action: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "action_id",
        "metric",
        "priority",
        "action_class",
        "knob",
        "apply_to",
        "direction",
        "current_value",
        "suggested_next_value",
        "suggested_unclipped_value",
        "target_value",
        "target_formula",
        "per_knob_values",
        "agent_step_fraction",
        "tuning_mode",
        "min_step_fraction",
        "max_step_fraction",
        "range",
        "range_update",
        "multi_objective_guardrail",
        "gain_only_ro_policy",
        "optimizer_selected",
        "objective_delta",
        "local_model_source",
    )
    out = {key: action[key] for key in keep if key in action}
    admissibility = action.get("action_admissibility") or (action.get("optimizer", {}) or {}).get("action_admissibility")
    if isinstance(admissibility, dict):
        out["action_admissibility"] = _compact_serialized_action_admissibility(admissibility)
    evidence_gate = action.get("evidence_gate") or (action.get("optimizer", {}) or {}).get("evidence_gate")
    if isinstance(evidence_gate, dict):
        out["evidence_gate"] = _compact_serialized_evidence_gate(evidence_gate)
    optimizer = action.get("optimizer")
    if isinstance(optimizer, dict):
        out["optimizer"] = _compact_serialized_action_optimizer_trace(optimizer)
    return out


def _compact_serialized_action_optimizer_trace(optimizer: dict[str, Any]) -> dict[str, Any]:
    keep = ("optimizer_selected", "objective_delta", "local_model_source", "predicted_violation_delta", "uncertainty", "constraint_penalty")
    out = {key: optimizer[key] for key in keep if key in optimizer}
    if isinstance(optimizer.get("action_admissibility"), dict):
        out["action_admissibility"] = _compact_serialized_action_admissibility(optimizer["action_admissibility"])
    if isinstance(optimizer.get("evidence_gate"), dict):
        out["evidence_gate"] = _compact_serialized_evidence_gate(optimizer["evidence_gate"])
    return out


def _compact_serialized_action_admissibility(gate: dict[str, Any]) -> dict[str, Any]:
    out = {
        key: gate[key]
        for key in ("schema_version", "passed", "objective_delta")
        if key in gate
    }
    if gate.get("reasons"):
        out["reasons"] = list(gate.get("reasons", []) or [])[:2]
    return out


def _compact_serialized_evidence_gate(gate: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "schema_version",
        "required",
        "passed",
        "source",
        "objective_improvement",
        "relative_improvement",
        "weighted_tradeoff_worsening",
        "tradeoff_to_improvement_ratio",
        "max_component_worsening",
        "uncertainty",
        "improved_failed_metrics",
    )
    out = {key: gate[key] for key in keep if key in gate}
    if gate.get("reasons"):
        out["reasons"] = list(gate.get("reasons", []) or [])[:2]
    return out


def _compact_serialized_agent_command(command: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "schema_version",
        "id",
        "author",
        "tool",
        "status",
        "round_index",
        "state_source",
        "llm_notes",
        "llm_rationale",
        "llm_planner",
        "llm_increment",
    )
    out = {key: command[key] for key in keep if key in command}
    args = command.get("args", {}) if isinstance(command.get("args"), dict) else {}
    if args:
        compact_args = {
            key: args[key]
            for key in ("max_primary_actions_per_failure", "allowed_priorities")
            if key in args
        }
        selected = args.get("selected_actions")
        if isinstance(selected, list):
            compact_args["selected_actions"] = [
                _compact_serialized_action_selection(item)
                for item in selected
                if isinstance(item, dict)
            ]
        custom = args.get("custom_actions")
        if isinstance(custom, list):
            compact_args["custom_actions"] = [
                _compact_serialized_custom_action(item)
                for item in custom
                if isinstance(item, dict)
            ]
        available = args.get("available_actions")
        if isinstance(available, list):
            compact_args["available_action_count"] = len(available)
        out["args"] = compact_args
    application = command.get("application")
    if isinstance(application, dict):
        out["application"] = _compact_serialized_application(application)
    return out


def _compact_serialized_action_selection(item: dict[str, Any]) -> dict[str, Any]:
    keep = ("action_id", "decision", "reason", "overrides")
    return {key: item[key] for key in keep if key in item}


def _compact_serialized_custom_action(item: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "action_id",
        "decision",
        "knob",
        "apply_to",
        "metric",
        "direction",
        "suggested_next_value",
        "suggested_unclipped_value",
        "per_knob_values",
        "range_update",
        "priority",
        "action_class",
        "reason",
    )
    return {key: item[key] for key in keep if key in item}


def _compact_serialized_application(application: dict[str, Any]) -> dict[str, Any]:
    applied = application.get("applied_actions", []) or []
    skipped = application.get("skipped_actions", []) or []
    return {
        "round_index": application.get("round_index"),
        "command_id": application.get("command_id", ""),
        "applied_action_count": len(applied),
        "skipped_action_count": len(skipped),
        "applied_actions": [_compact_serialized_application_action(item) for item in applied[:5] if isinstance(item, dict)],
        "skipped_actions": [_compact_serialized_application_action(item) for item in skipped[:5] if isinstance(item, dict)],
    }


def _compact_serialized_application_action(item: dict[str, Any]) -> dict[str, Any]:
    keep = ("action_id", "knob", "apply_to", "applied_knobs", "applied", "reason")
    return {key: item[key] for key in keep if key in item}


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

    # Internal implementation note.
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
        model_lib=sim_d.get("model_lib", ""), analyses=sim_d.get("analyses", ["op", "ac", "dc"]),
        ac_start=sim_d.get("ac_start", 1.0), ac_stop=sim_d.get("ac_stop", 1e9),
        ac_points=sim_d.get("ac_points", 50), cload=sim_d.get("cload", 1e-12),
        bias_voltage=sim_d.get("bias_voltage", 0.6),
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
        schema_version=d.get("schema_version", "0.1"),
        design_name=d.get("design_name", "unnamed"),
        topology=topology, targets=targets, constraints=constraints,
        design_variables=design_vars, transistors=transistors,
        global_parameters=d.get("global_parameters", {}),
        loss_terms=loss_terms, evaluations=evaluations,
        simulation=simulation, process=process, history=history,
        corrections=corrections,
        diagnostics=d.get("diagnostics", {}),
    )

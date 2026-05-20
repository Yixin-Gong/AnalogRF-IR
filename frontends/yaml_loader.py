from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from schemas.design_state import (
    Constraints,
    CorrectionFactors,
    DesignState,
    DesignVariable,
    DeviceConstraint,
    DeviceDefinition,
    Evaluation,
    GlobalNet,
    HistoryEntry,
    LossTerm,
    Port,
    Range,
    Target,
    Topology,
    TransistorParameters,
    TransistorState,
)
from util.units import parse_value


MOS_TYPE_ALIASES = {
    "n": "nmos",
    "nch": "nmos",
    "nfet": "nmos",
    "nmos": "nmos",
    "p": "pmos",
    "pch": "pmos",
    "pfet": "pmos",
    "pmos": "pmos",
}


def load_yaml_mapping(path: str | Path) -> dict[str, Any]:
    yaml_path = Path(path)
    with yaml_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{yaml_path} must contain a YAML mapping")
    return data


def yaml_has_explicit_topology(path: str | Path) -> bool:
    data = load_yaml_mapping(path)
    topology = data.get("topology") or {}
    return isinstance(topology, dict) and bool(topology.get("devices"))


def build_design_state_from_yaml(data: dict[str, Any], env: dict[str, Any]) -> DesignState:
    """Build a DesignState directly from a designer-editable YAML document.

    This is the new frontend path: topology, targets, variables, constraints,
    evaluations, and optional initial transistor physical values are read from
    YAML instead of being reconstructed from a hard-coded OTA builder.
    """
    state = DesignState(
        schema_version=str(data.get("schema_version", "0.1")),
        design_name=str(data.get("design_name") or (data.get("topology") or {}).get("name") or "yaml_design"),
    )
    state.topology = _parse_topology(data, env)
    state.targets = _parse_targets(data.get("targets") or {})
    state.constraints = _parse_constraints(data.get("constraints") or {})
    state.corrections = _parse_corrections(data.get("corrections") or {})
    state.loss_terms = _parse_loss_terms(data.get("loss_terms") or [])
    if not state.loss_terms:
        state.loss_terms = _loss_terms_from_targets(state.targets)
    state.evaluations = _parse_evaluations(data.get("evaluations") or [])
    state.global_parameters = {
        str(k): _num(v)
        for k, v in (data.get("global_parameters") or {}).items()
        if _maybe_num(v) is not None
    }

    # Inject process/simulation from the active environment; process remains an
    # environment concern while the YAML describes the design/problem.
    from core.environment import build_process_info, build_simulation_config

    state.process = build_process_info(env)
    state.simulation = build_simulation_config(env)

    explicit_transistors = _parse_transistor_initials(data.get("transistors") or {})
    _initialize_transistors(state, explicit_transistors)

    explicit_dv = _parse_design_variables(data.get("design_variables") or [])
    if explicit_dv:
        state.design_variables = explicit_dv
    state.build_design_variables()
    report = diagnose_and_seed_initial_values(state)
    if report:
        state.history.append(
            HistoryEntry(
                iteration=0,
                strategy="yaml_frontend_initialization",
                diagnosis="; ".join(report),
            )
        )
    return state


def diagnose_and_seed_initial_values(state: DesignState) -> list[str]:
    report: list[str] = []
    for dev in state.topology.devices:
        ts = state.transistors.get(dev.id)
        if ts is None:
            continue
        gm_range = state.constraints.get_gm_id_range(dev.id)
        l_range = state.constraints.get_L_range(dev.id)
        if ts.gm_id_strategy <= 0:
            ts.gm_id_strategy = _role_default_gm_id(dev.role, dev.type)
            report.append(f"{dev.id}: gm/id seeded from role default")
        ts.gm_id_strategy = _clip(ts.gm_id_strategy, gm_range.min, gm_range.max)
        if ts.L_strategy <= 0:
            ts.L_strategy = _role_default_l(dev.role)
            report.append(f"{dev.id}: L seeded from role default")
        ts.L_strategy = _clip(ts.L_strategy, l_range.min, l_range.max)
        if ts.parameters.L <= 0:
            ts.parameters.L = ts.L_strategy

    for dv in state.design_variables:
        if dv.initial is None:
            if dv.device and dv.device in state.transistors:
                ts = state.transistors[dv.device]
                if dv.variable == "gm_id":
                    dv.initial = ts.gm_id_strategy
                elif dv.variable == "L":
                    dv.initial = ts.L_strategy
            if dv.initial is None:
                dv.initial = _midpoint(dv.range)
            report.append(f"{_dv_label(dv)}: initial value synthesized")
        dv.initial = _clip(float(dv.initial), dv.range.min, dv.range.max)
        if not dv.device:
            state.global_parameters.setdefault(dv.variable, float(dv.initial))
    return report


def _parse_topology(data: dict[str, Any], env: dict[str, Any]) -> Topology:
    topo = data.get("topology") or {}
    process = env.get("process", {}) or {}
    nmos_model = process.get("nmos_model", "nmos")
    pmos_model = process.get("pmos_model", "pmos")
    devices = []
    for item in topo.get("devices") or []:
        if not isinstance(item, dict) or not _is_mos_device(item):
            continue
        mos_type = _normalize_mos_type(item.get("type") or item.get("mos_type") or item.get("model") or "nmos")
        model = _process_model_name(item.get("model"), mos_type, nmos_model, pmos_model)
        devices.append(
            DeviceDefinition(
                id=str(item["id"]),
                role=str(item.get("role") or item.get("role_hint") or "device"),
                stage=str(item.get("stage") or "core"),
                type=mos_type,
                model=str(model),
                connections=_normalize_connections(item.get("connections") or item),
            )
        )
    return Topology(
        name=str(topo.get("name") or data.get("design_name") or "yaml_design"),
        class_=str(topo.get("class") or topo.get("class_") or "ota"),
        architecture=str(topo.get("architecture") or "custom"),
        global_nets=[GlobalNet(name=str(x.get("name") or x.get("id")), type=str(x.get("type") or "global"))
                     for x in topo.get("global_nets") or [] if isinstance(x, dict) and (x.get("name") or x.get("id"))],
        ports=[Port(id=str(x.get("id") or x.get("name")), direction=str(x.get("direction") or ""))
               for x in topo.get("ports") or [] if isinstance(x, dict) and (x.get("id") or x.get("name"))],
        devices=devices,
    )


def _parse_targets(raw: dict[str, Any]) -> dict[str, Target]:
    out = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        out[str(name)] = Target(
            min=_maybe_num(spec.get("min")),
            max=_maybe_num(spec.get("max")),
            unit=str(spec.get("unit") or ""),
            priority=int(spec.get("priority", 1)),
        )
    return out


def _parse_constraints(raw: dict[str, Any]) -> Constraints:
    global_ranges: dict[str, Range] = {}
    per_device: dict[str, DeviceConstraint] = {}
    for key in ("gm_id", "L"):
        if isinstance(raw.get(key), dict):
            global_ranges[key] = _range(raw[key])
    for key, item in (raw.get("global") or {}).items():
        if isinstance(item, dict):
            global_ranges[str(key)] = _range(item)
    device_map = raw.get("devices") if isinstance(raw.get("devices"), dict) else None
    if device_map is None:
        device_map = {
            k: v for k, v in raw.items()
            if isinstance(k, str) and k.lower().startswith("m") and isinstance(v, dict)
        }
    for dev_id, spec in (device_map or {}).items():
        dc = DeviceConstraint()
        if isinstance(spec.get("gm_id"), dict):
            dc.gm_id = _range(spec["gm_id"])
        elif isinstance(spec.get("gm_id"), list):
            dc.gm_id = Range(_num(spec["gm_id"][0]), _num(spec["gm_id"][1]))
        if isinstance(spec.get("L"), dict):
            dc.L = _range(spec["L"])
        elif isinstance(spec.get("L"), list):
            dc.L = Range(_num(spec["L"][0]), _num(spec["L"][1]))
        if spec.get("VDS_min") is not None:
            dc.VDS_min = _num(spec["VDS_min"])
        if spec.get("VGS_max") is not None:
            dc.VGS_max = _num(spec["VGS_max"])
        per_device[str(dev_id)] = dc
    return Constraints(global_=global_ranges, per_device=per_device)


def _parse_design_variables(raw: list[Any]) -> list[DesignVariable]:
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rng = _range(item.get("range") or {"min": 0, "max": 1})
        out.append(
            DesignVariable(
                device=str(item.get("device") or ""),
                variable=str(item.get("variable") or ""),
                range=rng,
                initial=_maybe_num(item.get("initial")),
                symmetry_label=item.get("symmetry_label"),
                unit=str(item.get("unit") or ""),
                description=str(item.get("description") or ""),
            )
        )
    return out


def _parse_corrections(raw: dict[str, Any]) -> CorrectionFactors:
    return CorrectionFactors(
        gm_factor=_num(raw.get("gm_factor", 1.0)),
        gds_factor=_num(raw.get("gds_factor", 1.0)),
        c_factor=_num(raw.get("c_factor", 1.0)),
        description=str(raw.get("description") or ""),
    )


def _parse_loss_terms(raw: list[Any]) -> list[LossTerm]:
    return [
        LossTerm(
            id=str(item.get("id") or ""),
            formula=str(item.get("formula") or ""),
            weight=_num(item.get("weight", 1.0)),
            description=str(item.get("description") or ""),
        )
        for item in raw if isinstance(item, dict) and item.get("id")
    ]


def _loss_terms_from_targets(targets: dict[str, Target]) -> list[LossTerm]:
    terms: list[LossTerm] = []
    for name, target in targets.items():
        weight = 1.0 / max(int(target.priority or 1), 1)
        if target.min is not None:
            terms.append(
                LossTerm(
                    id=f"{name}_shortfall",
                    formula=f"relu((targets.{name}.min - realized.{name}) / max(targets.{name}.min, 1e-12))",
                    weight=weight,
                    description=f"Auto-generated from targets.{name}.min",
                )
            )
        if target.max is not None:
            terms.append(
                LossTerm(
                    id=f"{name}_excess",
                    formula=f"relu((realized.{name} - targets.{name}.max) / max(targets.{name}.max, 1e-12))",
                    weight=weight,
                    description=f"Auto-generated from targets.{name}.max",
                )
            )
    return terms


def _parse_evaluations(raw: list[Any]) -> list[Evaluation]:
    return [
        Evaluation(
            name=str(item.get("name") or ""),
            type=str(item.get("type") or ""),
            probe=str(item.get("probe") or ""),
            device=str(item.get("device") or ""),
            target_ref=str(item.get("target_ref") or ""),
            meas_formula=str(item.get("meas_formula") or ""),
        )
        for item in raw if isinstance(item, dict) and item.get("name")
    ]


def _parse_transistor_initials(raw: dict[str, Any]) -> dict[str, TransistorParameters]:
    out = {}
    for dev_id, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        params = spec.get("parameters") if isinstance(spec.get("parameters"), dict) else spec
        clean = {k: _num(v) for k, v in params.items() if k in TransistorParameters.__dataclass_fields__ and _maybe_num(v) is not None}
        out[str(dev_id)] = TransistorParameters(**clean)
    return out


def _initialize_transistors(state: DesignState, explicit_params: dict[str, TransistorParameters]) -> None:
    for dev in state.topology.devices:
        params = explicit_params.get(dev.id, TransistorParameters())
        gm0 = _role_default_gm_id(dev.role, dev.type)
        l0 = params.L if params.L > 0 else _role_default_l(dev.role)
        state.transistors[dev.id] = TransistorState(
            device_id=dev.id,
            role=dev.role,
            type=dev.type,
            model=dev.model,
            connections=dev.connections,
            gm_id_strategy=gm0,
            L_strategy=l0,
            parameters=params,
        )


def _is_mos_device(item: dict[str, Any]) -> bool:
    kind = str(item.get("kind") or "").lower()
    if kind in {"capacitor", "cap", "resistor", "res", "voltage_source", "vsource"}:
        return False
    value = str(item.get("type") or item.get("mos_type") or item.get("model") or "nmos").lower()
    return any(token in value for token in ("nmos", "pmos", "nch", "pch")) or kind in {"", "mos", "transistor"}


def _normalize_mos_type(value: Any) -> str:
    low = str(value).lower()
    if "pmos" in low or "pch" in low or low in {"p", "pfet"}:
        return "pmos"
    return MOS_TYPE_ALIASES.get(low, "nmos")


def _process_model_name(model: Any, mos_type: str, nmos_model: str, pmos_model: str) -> str:
    if model is None:
        return pmos_model if mos_type == "pmos" else nmos_model
    low = str(model).lower()
    if low in {"n", "nmos", "nch", "nfet"}:
        return nmos_model
    if low in {"p", "pmos", "pch", "pfet"}:
        return pmos_model
    return str(model)


def _normalize_connections(raw: dict[str, Any]) -> dict[str, str]:
    aliases = {"d": "drain", "g": "gate", "s": "source", "b": "body", "bulk": "body"}
    out = {}
    for key, value in raw.items():
        canonical = aliases.get(str(key).lower(), str(key).lower())
        if canonical in {"drain", "gate", "source", "body"} and value is not None:
            out[canonical] = str(value)
    return out


def _range(raw: dict[str, Any]) -> Range:
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        return Range(min=_num(raw[0]), max=_num(raw[1]))
    return Range(min=_num(raw.get("min", 0.0)), max=_num(raw.get("max", 0.0)))


def _num(value: Any) -> float:
    parsed = _maybe_num(value)
    if parsed is None:
        raise ValueError(f"Cannot parse numeric value: {value!r}")
    return parsed


def _maybe_num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(parse_value(value))
        except Exception:
            try:
                return float(value)
            except ValueError:
                return None
    return None


def _role_default_gm_id(role: str, mos_type: str) -> float:
    role = role.lower()
    if "input" in role:
        return 16.0
    if "load" in role or "mirror" in role:
        return 7.0 if mos_type == "pmos" else 9.0
    if "tail" in role or "source" in role:
        return 9.0
    return 10.0


def _role_default_l(role: str) -> float:
    role = role.lower()
    if "tail" in role or "bias" in role or "source" in role:
        return 5.0e-7
    if "load" in role or "mirror" in role:
        return 1.0e-6
    return 5.0e-7


def _clip(value: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return value
    return min(max(value, lo), hi)


def _midpoint(rng: Range) -> float:
    return 0.5 * (rng.min + rng.max)


def _dv_label(dv: DesignVariable) -> str:
    return f"{dv.device}.{dv.variable}" if dv.device else dv.variable

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


GROUND_NAMES = {"0", "gnd", "vss"}
SUPPLY_NAMES = {"vdd", "vcc", "avdd", "dvdd"}


def parse_spice_file(path: str | Path, design_name: str | None = None) -> dict[str, Any]:
    spice_path = Path(path)
    text = spice_path.read_text(encoding="utf-8", errors="ignore")
    return parse_spice_text(text, design_name=design_name or spice_path.stem)


def parse_spice_text(text: str, design_name: str = "spice_design") -> dict[str, Any]:
    lines = _merge_continuations(text)
    devices: list[dict[str, Any]] = []
    transistors: dict[str, Any] = {}
    passives: list[dict[str, Any]] = []
    voltage_sources: list[dict[str, Any]] = []
    nets: set[str] = set()

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("*") or stripped.startswith(";"):
            continue
        if stripped.startswith("."):
            continue
        tokens = stripped.split()
        if not tokens:
            continue
        prefix = tokens[0][0].upper()
        if prefix == "M" and len(tokens) >= 6:
            dev, params = _parse_mos(tokens, subckt=False)
            devices.append(dev)
            transistors[dev["id"]] = {"parameters": params}
            nets.update(dev["connections"].values())
        elif prefix == "X" and len(tokens) >= 6 and _looks_like_mos_model(tokens[5]):
            dev, params = _parse_mos(tokens, subckt=True)
            devices.append(dev)
            transistors[dev["id"]] = {"parameters": params}
            nets.update(dev["connections"].values())
        elif prefix in {"C", "R"} and len(tokens) >= 4:
            item = _parse_passive(tokens)
            passives.append(item)
            nets.update(item["connections"].values())
        elif prefix == "V" and len(tokens) >= 3:
            src = _parse_voltage_source(tokens)
            voltage_sources.append(src)
            nets.update(src["connections"].values())

    ports = _infer_ports(nets, voltage_sources, devices)
    global_nets = _infer_global_nets(nets)
    topology = {
        "name": design_name,
        "class": "ota" if _looks_like_ota(devices, ports) else "analog",
        "architecture": _infer_architecture(devices, passives),
        "global_nets": global_nets,
        "ports": ports,
        "nets": {net: _net_type(net, ports, global_nets) for net in sorted(nets) if _net_type(net, ports, global_nets)},
        "devices": [*devices, *passives],
    }
    out = {
        "schema_version": "0.1",
        "design_name": design_name,
        "metadata": {"source": "spice_parser", "note": "Generated from SPICE; review roles and specs before optimization."},
        "topology": topology,
        "targets": _default_targets(topology["class"]),
        "constraints": _default_constraints(devices),
        "design_variables": _default_design_variables(devices, transistors, passives),
        "loss_terms": _default_loss_terms(topology["class"]),
        "corrections": {
            "gm_factor": 1.0,
            "gds_factor": 1.0,
            "c_factor": 1.0,
            "description": "Generated from SPICE parser defaults",
        },
        "evaluations": _default_evaluations(ports),
        "transistors": transistors,
    }
    globals_from_passives = _global_parameters_from_passives(passives)
    if globals_from_passives:
        out["global_parameters"] = globals_from_passives
    return out


def write_yaml(data: dict[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    return out


def _merge_continuations(text: str) -> list[str]:
    merged: list[str] = []
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if current:
                merged.append(current)
                current = ""
            continue
        if line.startswith("+"):
            current += " " + line[1:].strip()
            continue
        if current:
            merged.append(current)
        current = line
    if current:
        merged.append(current)
    return merged


def _parse_mos(tokens: list[str], subckt: bool) -> tuple[dict[str, Any], dict[str, float]]:
    raw_id = tokens[0]
    dev_id = _canonical_mos_id(raw_id, subckt=subckt)
    drain, gate, source, body = tokens[1:5]
    model = tokens[5]
    params = _parse_params(tokens[6:])
    mos_type = _infer_mos_type(model, raw_id)
    connections = {"drain": drain, "gate": gate, "source": source, "body": body}
    role = _infer_role(dev_id, mos_type, connections)
    dev = {
        "id": dev_id,
        "role": role,
        "stage": _infer_stage_from_role(role),
        "type": mos_type,
        "model": model,
        "connections": connections,
    }
    transistor_params = {}
    if "W" in params:
        transistor_params["W"] = params["W"]
    if "L" in params:
        transistor_params["L"] = params["L"]
    return dev, transistor_params


def _canonical_mos_id(raw_id: str, subckt: bool) -> str:
    """Normalize generated SPICE instance names back to schema device ids.

    NetlistGenerator emits ``M{device_id}`` for primitive MOS cards and
    ``X{device_id}`` for subckt MOS devices. A schema device ``M1`` therefore
    appears as ``MM1`` or ``XM1`` in SPICE; YAML should keep the stable id ``M1``.
    """
    if subckt and raw_id.upper().startswith("X"):
        raw_id = raw_id[1:]
    match = re.fullmatch(r"MM(\d+)", raw_id, flags=re.IGNORECASE)
    if match:
        return f"M{match.group(1)}"
    return raw_id


def _parse_passive(tokens: list[str]) -> dict[str, Any]:
    prefix = tokens[0][0].upper()
    kind = "capacitor" if prefix == "C" else "resistor"
    role = "compensation" if tokens[0].lower() in {"cc", "rz"} else ("output_load" if "load" in tokens[0].lower() else kind)
    return {
        "id": tokens[0],
        "kind": kind,
        "role": role,
        "value": tokens[3],
        "connections": {"plus": tokens[1], "minus": tokens[2]},
    }


def _parse_voltage_source(tokens: list[str]) -> dict[str, Any]:
    value = None
    if len(tokens) >= 5 and tokens[3].lower() == "dc":
        value = tokens[4]
    elif len(tokens) >= 4:
        value = tokens[3]
    return {"id": tokens[0], "connections": {"plus": tokens[1], "minus": tokens[2]}, "dc": value}


def _parse_params(tokens: list[str]) -> dict[str, float]:
    params = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        parsed = _parse_spice_number(value)
        if parsed is not None:
            params[key.upper()] = parsed
    return params


def _parse_spice_number(value: str) -> float | None:
    value = value.strip().strip("'\"")
    suffixes = {
        "meg": 1e6,
        "g": 1e9,
        "k": 1e3,
        "m": 1e-3,
        "u": 1e-6,
        "n": 1e-9,
        "p": 1e-12,
        "f": 1e-15,
    }
    match = re.fullmatch(r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)([a-zA-Z]+)?", value)
    if not match:
        return None
    number = float(match.group(1))
    suffix = (match.group(2) or "").lower()
    return number * suffixes.get(suffix, 1.0)


def _looks_like_mos_model(model: str) -> bool:
    low = model.lower()
    return any(token in low for token in ("nmos", "pmos", "nch", "pch", "nfet", "pfet"))


def _infer_mos_type(model: str, dev_id: str) -> str:
    low = f"{model} {dev_id}".lower()
    if any(token in low for token in ("pmos", "pch", "pfet", "sg13_lv_pmos")):
        return "pmos"
    return "nmos"


def _infer_role(dev_id: str, mos_type: str, c: dict[str, str]) -> str:
    name = dev_id.lower()
    gate = c["gate"].lower()
    source = c["source"].lower()
    drain = c["drain"].lower()
    if gate in {"vinp", "vinn", "inp", "inn", "vip", "vim"}:
        return "input_pair"
    if gate == drain and mos_type == "nmos" and ("stage2" in gate or "out" in gate) and source in GROUND_NAMES:
        return "output_bias_mirror"
    if gate == drain and mos_type == "nmos" and ("bias" in gate or gate.startswith("vb")) and source in GROUND_NAMES:
        return "tail_bias_mirror"
    if "tail" in name or ("tail" in drain and source in GROUND_NAMES):
        return "tail_current_source"
    if c["gate"] == c["drain"] and mos_type == "pmos":
        return "current_mirror_load"
    if drain in {"vout", "out", "outp", "outn"} and source in SUPPLY_NAMES and mos_type == "pmos":
        return "second_stage_gain"
    if drain in {"vout", "out", "outp", "outn"} and source in GROUND_NAMES and mos_type == "nmos":
        return "output_current_source"
    if mos_type == "pmos" and source in SUPPLY_NAMES and gate not in {"vinp", "vinn", "inp", "inn", "vip", "vim"}:
        return "current_mirror_load"
    if "latch" in name:
        return "latch_device"
    return "device"


def _infer_stage_from_role(role: str) -> str:
    if "input" in role:
        return "input"
    if "load" in role:
        return "load"
    if "output" in role or "second_stage" in role:
        return "output"
    if "tail" in role:
        return "bias"
    return "core"


def _infer_ports(nets: set[str], voltage_sources: list[dict[str, Any]], devices: list[dict[str, Any]]) -> list[dict[str, str]]:
    ports: dict[str, str] = {}
    for net in nets:
        low = net.lower()
        if low in {"vinp", "vinn", "inp", "inn", "vip", "vim"}:
            ports[net] = "input"
        elif low in {"vout", "out", "outp", "outn", "voutp", "voutn"}:
            ports[net] = "output"
        elif "bias" in low or low.startswith("vb"):
            ports[net] = "bias"
    for src in voltage_sources:
        plus = src["connections"]["plus"]
        low_src = src["id"].lower()
        if low_src.startswith("vin"):
            ports.setdefault(plus, "input")
        elif "bias" in low_src or low_src.startswith("vb"):
            ports.setdefault(plus, "bias")
    return [{"id": net, "direction": direction} for net, direction in sorted(ports.items())]


def _infer_global_nets(nets: set[str]) -> list[dict[str, str]]:
    out = []
    for net in sorted(nets):
        low = net.lower()
        if low in SUPPLY_NAMES:
            out.append({"name": net, "type": "supply"})
        elif low in GROUND_NAMES:
            out.append({"name": net, "type": "ground"})
    return out


def _net_type(net: str, ports: list[dict[str, str]], globals_: list[dict[str, str]]) -> str:
    for port in ports:
        if port["id"] == net:
            return port["direction"]
    for item in globals_:
        if item["name"] == net:
            return item["type"]
    return "internal"


def _looks_like_ota(devices: list[dict[str, Any]], ports: list[dict[str, str]]) -> bool:
    roles = {dev["role"] for dev in devices}
    return "input_pair" in roles and any(port["direction"] == "output" for port in ports)


def _infer_architecture(devices: list[dict[str, Any]], passives: list[dict[str, Any]]) -> str:
    if any(dev["role"] == "second_stage_gain" for dev in devices):
        return "two-stage-miller" if _has_miller_passives(passives) else "two-stage"
    if len([dev for dev in devices if dev["role"] != "device"]) >= 5:
        return "single-stage"
    return "spice-import"


def _default_targets(circuit_class: str) -> dict[str, Any]:
    if circuit_class == "ota":
        return {
            "dc_gain": {"min": 60, "unit": "dB", "priority": 1},
            "unity_gain_bandwidth": {"min": 5e8, "unit": "Hz", "priority": 1},
            "phase_margin": {"min": 60, "unit": "deg", "priority": 1},
            "power": {"max": 5e-4, "unit": "W", "priority": 2},
        }
    return {}


def _default_constraints(devices: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "gm_id": {"min": 5, "max": 25},
        "L": {"min": 1.3e-7, "max": 2.0e-6},
        "devices": {
            dev["id"]: {"gm_id": {"min": 5, "max": 25}, "L": {"min": 1.3e-7, "max": 2.0e-6}}
            for dev in devices
        },
    }


def _default_design_variables(
    devices: list[dict[str, Any]],
    transistors: dict[str, Any],
    passives: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    variables = []
    for dev in devices:
        params = transistors.get(dev["id"], {}).get("parameters", {})
        variables.append({"device": dev["id"], "variable": "gm_id", "range": {"min": 5, "max": 25}, "initial": _role_gm_id(dev["role"])})
        variables.append({"device": dev["id"], "variable": "L", "range": {"min": 1.3e-7, "max": 2.0e-6}, "initial": params.get("L", 5e-7)})
    if any(dev["role"] == "second_stage_gain" for dev in devices):
        variables.extend([
            {"device": "", "variable": "I_tail", "range": {"min": 5e-6, "max": 8e-5}, "initial": 2e-5, "unit": "A"},
            {"device": "", "variable": "I_stage2", "range": {"min": 1e-5, "max": 2.5e-4}, "initial": 8e-5, "unit": "A"},
        ])
        passive_ids = {str(item.get("id", "")).lower() for item in passives}
        if "cc" in passive_ids:
            variables.append(
                {"device": "", "variable": "Cc", "range": {"min": 1e-13, "max": 2.5e-12}, "initial": 4e-13, "unit": "F"}
            )
        if "rz" in passive_ids:
            variables.append(
                {"device": "", "variable": "Rz", "range": {"min": 1e2, "max": 2e4}, "initial": 1e3, "unit": "ohm"}
            )
    return variables


def _default_loss_terms(circuit_class: str) -> list[dict[str, Any]]:
    if circuit_class != "ota":
        return []
    return [
        {
            "id": "gain_shortfall",
            "formula": "relu((targets.dc_gain.min - realized.dc_gain) / max(targets.dc_gain.min, 1))",
            "weight": 4.0,
            "description": "Generated from dc_gain target",
        },
        {
            "id": "ugbw_shortfall",
            "formula": (
                "relu((targets.unity_gain_bandwidth.min - realized.unity_gain_bandwidth) / "
                "max(targets.unity_gain_bandwidth.min, 1))"
            ),
            "weight": 4.0,
            "description": "Generated from unity_gain_bandwidth target",
        },
        {
            "id": "pm_shortfall",
            "formula": "relu((targets.phase_margin.min - realized.phase_margin) / max(targets.phase_margin.min, 1))",
            "weight": 5.0,
            "description": "Generated from phase_margin target",
        },
        {
            "id": "power_excess",
            "formula": "relu((realized.power - targets.power.max) / max(targets.power.max, 1e-12))",
            "weight": 1.0,
            "description": "Generated from power target",
        },
    ]


def _default_evaluations(ports: list[dict[str, str]]) -> list[dict[str, str]]:
    output = next((port["id"] for port in ports if port["direction"] == "output"), "vout")
    return [
        {"name": "dc_gain", "type": "ac_gain", "probe": output, "target_ref": "dc_gain"},
        {"name": "unity_gain_bandwidth", "type": "ugbw", "probe": output, "target_ref": "unity_gain_bandwidth"},
        {"name": "phase_margin", "type": "phase_margin", "probe": output, "target_ref": "phase_margin"},
        {"name": "total_power", "type": "dc_power", "target_ref": "power"},
    ]


def _global_parameters_from_passives(passives: list[dict[str, Any]]) -> dict[str, float]:
    out = {}
    for item in passives:
        value = _parse_spice_number(str(item.get("value", "")))
        if value is None:
            continue
        if item["id"].lower() == "cc":
            out["Cc"] = value
        elif item["id"].lower() == "rz":
            out["Rz"] = value
    return out


def _has_miller_passives(passives: list[dict[str, Any]]) -> bool:
    passive_ids = {str(item.get("id", "")).lower() for item in passives}
    return "cc" in passive_ids


def _role_gm_id(role: str) -> float:
    if "input" in role:
        return 16.0
    if "load" in role:
        return 7.0
    if "tail" in role:
        return 9.0
    return 10.0

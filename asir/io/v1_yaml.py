from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from asir.design import ASIRDesign, build_design
from asir.topology import TopologyGraph


TERMINAL_ALIASES = {
    "d": "drain",
    "drain": "drain",
    "g": "gate",
    "gate": "gate",
    "s": "source",
    "source": "source",
    "b": "bulk",
    "body": "bulk",
    "bulk": "bulk",
}


def load_v1_yaml(path: str | Path) -> dict[str, Any]:
    yaml_path = Path(path)
    with yaml_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{yaml_path} must contain a YAML mapping at the top level")
    return data


def build_design_from_v1_yaml(path: str | Path) -> ASIRDesign:
    return build_design(build_topology_from_v1_dict(load_v1_yaml(path)))


def build_topology_from_v1_dict(data: dict[str, Any]) -> TopologyGraph:
    topology_data = data.get("topology") or {}
    if not isinstance(topology_data, dict):
        raise ValueError("Expected 'topology' to be a mapping")

    name = topology_data.get("name") or data.get("design_name") or "unnamed_comparator"
    architecture = topology_data.get("architecture") or _infer_architecture(str(name))
    circuit_class = topology_data.get("class") or topology_data.get("class_") or "comparator"
    topo = TopologyGraph(str(name), architecture=str(architecture), circuit_class=str(circuit_class))

    _add_global_nets(topo, topology_data)
    _add_ports(topo, topology_data)
    _add_extra_nets(topo, topology_data)
    _add_clocks(topo, topology_data, data)
    _add_devices(topo, topology_data)

    if not topo.mos_devices() and not topo.capacitor_devices():
        raise ValueError("No MOS or capacitor devices found under topology.devices")
    return topo


def embed_asir_output(source_data: dict[str, Any], design: ASIRDesign) -> dict[str, Any]:
    out = deepcopy(source_data)
    out["asir_output"] = design.to_dict()
    out.setdefault("metadata", {})
    if isinstance(out["metadata"], dict):
        out["metadata"]["asir_compiled_from"] = "analogrf_ir_yaml"
        out["metadata"]["asir_note"] = (
            "Original topology/spec fields are preserved; ASIR output is a layered semantic IR bundle."
        )
    return out


def export_v1_with_asir(source_path: str | Path, output_path: str | Path) -> Path:
    source_data = load_v1_yaml(source_path)
    design = build_design(build_topology_from_v1_dict(source_data))
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(embed_asir_output(source_data, design), handle, sort_keys=False, allow_unicode=True, width=120)
    return output


def _add_global_nets(topo: TopologyGraph, topology_data: dict[str, Any]) -> None:
    for item in topology_data.get("global_nets") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("id")
        if name:
            topo.add_net(str(name), net_type=str(item.get("type") or "global"))


def _add_ports(topo: TopologyGraph, topology_data: dict[str, Any]) -> None:
    for item in topology_data.get("ports") or []:
        if not isinstance(item, dict):
            continue
        port_id = item.get("id") or item.get("name")
        if not port_id:
            continue
        topo.add_net(str(port_id), net_type=_port_direction_to_net_type(str(port_id), str(item.get("direction") or "")))


def _add_extra_nets(topo: TopologyGraph, topology_data: dict[str, Any]) -> None:
    nets = topology_data.get("nets") or {}
    if isinstance(nets, dict):
        for name, net_type in nets.items():
            topo.add_net(str(name), net_type=str(net_type or "internal"))
    elif isinstance(nets, list):
        for item in nets:
            if isinstance(item, dict):
                name = item.get("name") or item.get("id")
                net_type = item.get("type") or "internal"
            else:
                name = item
                net_type = "internal"
            if name:
                topo.add_net(str(name), net_type=str(net_type))


def _add_clocks(topo: TopologyGraph, topology_data: dict[str, Any], data: dict[str, Any]) -> None:
    clocks = topology_data.get("clocks") or data.get("clocks") or {}
    if isinstance(clocks, dict):
        for clock_id, spec in clocks.items():
            if isinstance(spec, dict):
                topo.add_clock(str(clock_id), drives=_optional_str(spec.get("drives")), phases=list(spec.get("phases") or []))
            else:
                topo.add_clock(str(clock_id), drives=str(spec))
    elif isinstance(clocks, list):
        for item in clocks:
            if not isinstance(item, dict):
                topo.add_clock(str(item))
                continue
            clock_id = item.get("id") or item.get("name")
            if clock_id:
                topo.add_clock(str(clock_id), drives=_optional_str(item.get("drives")), phases=list(item.get("phases") or []))


def _add_devices(topo: TopologyGraph, topology_data: dict[str, Any]) -> None:
    for item in topology_data.get("devices") or []:
        if not isinstance(item, dict):
            continue
        device_id = item.get("id")
        if not device_id:
            continue
        device_kind = str(item.get("kind") or item.get("type") or "mos").lower()
        if device_kind in {"capacitor", "cap"}:
            connections = item.get("connections") or {}
            plus = item.get("plus") or connections.get("plus") or connections.get("p") or connections.get("top")
            minus = item.get("minus") or connections.get("minus") or connections.get("n") or connections.get("bottom")
            topo.add_capacitor(
                str(device_id),
                plus=str(plus),
                minus=str(minus),
                capacitance=item.get("capacitance") or item.get("value"),
                role_hint=str(item.get("role_hint") or item.get("role") or ""),
            )
            continue

        connections = _normalize_connections(item.get("connections") or item)
        missing = [terminal for terminal in ("drain", "gate", "source") if terminal not in connections]
        if missing:
            raise ValueError(f"{device_id} missing MOS terminal(s): {', '.join(missing)}")
        bulk = connections.get("bulk") or connections.get("source") or "vss"
        topo.add_mos(
            str(device_id),
            _normalize_mos_type(str(item.get("type") or item.get("mos_type") or "nmos")),
            drain=str(connections["drain"]),
            gate=str(connections["gate"]),
            source=str(connections["source"]),
            bulk=str(bulk),
            role_hint=str(item.get("role_hint") or item.get("role") or ""),
            model=item.get("model"),
            stage=item.get("stage"),
        )


def _normalize_connections(raw: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in raw.items():
        canonical = TERMINAL_ALIASES.get(str(key).lower())
        if canonical and value is not None:
            out[canonical] = str(value)
    return out


def _normalize_mos_type(value: str) -> str:
    low = value.lower()
    if low in {"pmos", "pch", "p"}:
        return "pmos"
    return "nmos"


def _port_direction_to_net_type(port_id: str, direction: str) -> str:
    direction = direction.lower()
    if direction == "input":
        return "differential_input" if port_id.lower() in {"vinp", "vinn", "inp", "inn"} else "input"
    if direction == "output":
        return "differential_output" if port_id.lower() in {"outp", "outn", "voutp", "voutn"} else "output"
    if direction in {"supply", "ground", "bias"}:
        return direction
    return direction or "port"


def _infer_architecture(name: str) -> str:
    low = name.lower().replace("_", "-")
    if "double" in low and "tail" in low:
        return "double-tail"
    if "sense" in low:
        return "sense-amplifier"
    if "strongarm" in low or "strong-arm" in low:
        return "strongarm"
    return "custom-comparator"


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)

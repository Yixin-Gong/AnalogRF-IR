from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx


MOS_TERMINALS = ("drain", "gate", "source", "bulk")
CAP_TERMINALS = ("plus", "minus")
RES_TERMINALS = ("plus", "minus")


@dataclass(frozen=True)
class MOSSpec:
    id: str
    mos_type: str
    drain: str
    gate: str
    source: str
    bulk: str
    role_hint: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapacitorSpec:
    id: str
    plus: str
    minus: str
    capacitance: str | float | None = None
    role_hint: str = ""


@dataclass(frozen=True)
class ResistorSpec:
    id: str
    plus: str
    minus: str
    resistance: str | float | None = None
    role_hint: str = ""


class TopologyGraph:
    """Layer 1: transistor-level connectivity graph."""

    layer_name = "topology"

    def __init__(self, name: str, architecture: str, circuit_class: str = "comparator"):
        self.name = name
        self.architecture = architecture
        self.circuit_class = circuit_class
        self.graph = nx.MultiGraph(
            name=name,
            layer=self.layer_name,
            circuit_class=circuit_class,
            architecture=architecture,
        )

    def add_net(self, net_id: str, net_type: str = "internal", **attrs: Any) -> None:
        if net_id in self.graph:
            node = self.graph.nodes[net_id]
            if node.get("kind") != "net":
                return
            current_type = node.get("net_type", "internal")
            if net_type != "internal" or current_type == "internal":
                node["net_type"] = net_type
            node.update(attrs)
            return
        self.graph.add_node(net_id, kind="net", net_type=net_type, label=net_id, **attrs)

    def add_clock(self, clock_id: str, drives: str | None = None, phases: list[str] | None = None) -> None:
        self.graph.add_node(
            clock_id,
            kind="clock",
            label=clock_id,
            phases=phases or [],
        )
        if drives:
            self.add_net(drives, net_type="clock")
            self.graph.add_edge(clock_id, drives, terminal="drive", relation="electrical_connection")

    def add_mos(
        self,
        mos_id: str,
        mos_type: str,
        drain: str,
        gate: str,
        source: str,
        bulk: str,
        role_hint: str = "",
        **parameters: Any,
    ) -> None:
        if mos_type not in {"nmos", "pmos"}:
            raise ValueError(f"Unknown MOS type '{mos_type}' for {mos_id}")
        self.graph.add_node(
            mos_id,
            kind="mos",
            mos_type=mos_type,
            role_hint=role_hint,
            label=mos_id,
            parameters=parameters,
        )
        for terminal, net in {
            "drain": drain,
            "gate": gate,
            "source": source,
            "bulk": bulk,
        }.items():
            self.add_net(net)
            self.graph.add_edge(mos_id, net, terminal=terminal, relation="electrical_connection")

    def add_capacitor(
        self,
        cap_id: str,
        plus: str,
        minus: str,
        capacitance: str | float | None = None,
        role_hint: str = "",
    ) -> None:
        self.graph.add_node(
            cap_id,
            kind="capacitor",
            role_hint=role_hint,
            capacitance=capacitance,
            label=cap_id,
        )
        for terminal, net in {"plus": plus, "minus": minus}.items():
            self.add_net(net)
            self.graph.add_edge(cap_id, net, terminal=terminal, relation="electrical_connection")

    def add_resistor(
        self,
        res_id: str,
        plus: str,
        minus: str,
        resistance: str | float | None = None,
        role_hint: str = "",
    ) -> None:
        self.graph.add_node(
            res_id,
            kind="resistor",
            role_hint=role_hint,
            resistance=resistance,
            label=res_id,
        )
        for terminal, net in {"plus": plus, "minus": minus}.items():
            self.add_net(net)
            self.graph.add_edge(res_id, net, terminal=terminal, relation="electrical_connection")

    def node_kind(self, node_id: str) -> str:
        return self.graph.nodes[node_id].get("kind", "")

    def mos_devices(self) -> list[str]:
        return sorted(n for n, d in self.graph.nodes(data=True) if d.get("kind") == "mos")

    def capacitor_devices(self) -> list[str]:
        return sorted(n for n, d in self.graph.nodes(data=True) if d.get("kind") == "capacitor")

    def resistor_devices(self) -> list[str]:
        return sorted(n for n, d in self.graph.nodes(data=True) if d.get("kind") == "resistor")

    def nets(self) -> list[str]:
        return sorted(n for n, d in self.graph.nodes(data=True) if d.get("kind") == "net")

    def clocks(self) -> list[str]:
        return sorted(n for n, d in self.graph.nodes(data=True) if d.get("kind") == "clock")

    def role_hint(self, device_id: str) -> str:
        return str(self.graph.nodes[device_id].get("role_hint", ""))

    def mos_type(self, device_id: str) -> str:
        return str(self.graph.nodes[device_id].get("mos_type", ""))

    def terminal_net(self, device_id: str, terminal: str) -> str | None:
        for _, net, data in self.graph.edges(device_id, data=True):
            if data.get("terminal") == terminal and self.node_kind(net) == "net":
                return str(net)
        return None

    def device_connections(self, device_id: str) -> dict[str, str]:
        return {
            str(data["terminal"]): str(net)
            for _, net, data in self.graph.edges(device_id, data=True)
            if "terminal" in data and self.node_kind(net) == "net"
        }

    def devices_on_net(self, net_id: str, terminal: str | None = None) -> list[str]:
        out: list[str] = []
        for dev, _, data in self.graph.edges(net_id, data=True):
            if self.node_kind(dev) not in {"mos", "capacitor", "resistor"}:
                continue
            if terminal is None or data.get("terminal") == terminal:
                out.append(str(dev))
        return sorted(out)

    def output_nets(self) -> list[str]:
        return sorted(
            n
            for n, d in self.graph.nodes(data=True)
            if d.get("kind") == "net" and d.get("net_type") in {"output", "differential_output"}
        )

    def input_nets(self) -> list[str]:
        return sorted(
            n
            for n, d in self.graph.nodes(data=True)
            if d.get("kind") == "net" and d.get("net_type") in {"input", "differential_input"}
        )

    def to_dict(self) -> dict[str, Any]:
        nodes = [
            {"id": str(node_id), **self._clean_attrs(attrs)}
            for node_id, attrs in sorted(self.graph.nodes(data=True), key=lambda item: str(item[0]))
        ]
        edges = []
        for u, v, key, attrs in self.graph.edges(keys=True, data=True):
            edges.append({"source": str(u), "target": str(v), "key": key, **self._clean_attrs(attrs)})
        edges.sort(key=lambda item: (item["source"], item["target"], str(item["key"])))
        return {
            "layer": self.layer_name,
            "name": self.name,
            "circuit_class": self.circuit_class,
            "architecture": self.architecture,
            "graph_type": "networkx.MultiGraph",
            "nodes": nodes,
            "edges": edges,
        }

    @staticmethod
    def _clean_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
        return {str(k): v for k, v in attrs.items() if v is not None}

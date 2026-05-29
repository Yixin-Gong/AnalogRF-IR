from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import networkx as nx


@dataclass
class SemanticPrimitive:
    id: str
    primitive_type: str
    role: str
    member_devices: list[str]
    equations: list[str]
    constraints: list[str]
    active_phases: list[str]
    state_variables: list[str]
    input_nets: list[str] = field(default_factory=list)
    output_nets: list[str] = field(default_factory=list)
    control_nets: list[str] = field(default_factory=list)
    internal_nets: list[str] = field(default_factory=list)
    notes: str = ""

    def signature(self) -> tuple[str, tuple[str, ...]]:
        return self.primitive_type, tuple(sorted(self.active_phases))


class SemanticPrimitiveGraph:
    """Layer 2: graph of extracted analog semantic primitives."""

    layer_name = "semantic_primitives"

    def __init__(self, name: str):
        self.name = name
        self.graph = nx.DiGraph(name=name, layer=self.layer_name)

    def add_primitive(self, primitive: SemanticPrimitive) -> None:
        self.graph.add_node(
            primitive.id,
            kind="semantic_primitive",
            primitive=primitive,
            primitive_type=primitive.primitive_type,
            role=primitive.role,
            active_phases=list(primitive.active_phases),
        )

    def add_relation(self, source: str, target: str, relation: str, through: str | None = None) -> None:
        if source == target:
            return
        self.graph.add_edge(source, target, relation=relation, through=through)

    def primitives(self) -> list[SemanticPrimitive]:
        primitives = [data["primitive"] for _, data in self.graph.nodes(data=True)]
        return sorted(primitives, key=lambda prim: prim.id)

    def by_type(self, primitive_type: str) -> list[SemanticPrimitive]:
        return [p for p in self.primitives() if p.primitive_type == primitive_type]

    def primitive(self, primitive_id: str) -> SemanticPrimitive:
        return self.graph.nodes[primitive_id]["primitive"]

    def active_in_phase(self, phase: str) -> list[str]:
        return [p.id for p in self.primitives() if phase in p.active_phases]

    def primitive_type_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for primitive in self.primitives():
            counts[primitive.primitive_type] = counts.get(primitive.primitive_type, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        nodes = []
        for primitive in self.primitives():
            item = asdict(primitive)
            item["kind"] = "semantic_primitive"
            nodes.append(item)
        edges = [
            {"source": str(u), "target": str(v), **dict(attrs)}
            for u, v, attrs in self.graph.edges(data=True)
        ]
        edges.sort(key=lambda item: (item["source"], item["target"], item.get("relation", "")))
        return {
            "layer": self.layer_name,
            "name": self.name,
            "graph_type": "networkx.DiGraph",
            "nodes": nodes,
            "edges": edges,
        }

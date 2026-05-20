from __future__ import annotations

from typing import Any

import networkx as nx

from asir.semantic import SemanticPrimitiveGraph


DEFAULT_PHASES = ["reset", "amplify", "regenerate", "saturate"]


class OperationalPhaseGraph:
    """Layer 4: temporal activation semantics."""

    layer_name = "operational_phases"

    def __init__(self, name: str):
        self.name = name
        self.graph = nx.DiGraph(name=name, layer=self.layer_name)

    def add_phase(
        self,
        phase: str,
        index: int,
        active_primitives: list[str] | None = None,
        invariants: list[str] | None = None,
        exit_condition: str = "",
    ) -> None:
        self.graph.add_node(
            phase,
            kind="phase",
            index=index,
            active_primitives=active_primitives or [],
            invariants=invariants or [],
            exit_condition=exit_condition,
        )

    def add_transition(self, source: str, target: str, condition: str) -> None:
        self.graph.add_edge(source, target, relation="phase_transition", condition=condition)

    def active_primitives(self, phase: str) -> list[str]:
        return list(self.graph.nodes[phase].get("active_primitives", []))

    def timeline_for_primitive(self, primitive_id: str) -> list[str]:
        phases = []
        for phase, attrs in sorted(self.graph.nodes(data=True), key=lambda item: item[1].get("index", 0)):
            if primitive_id in attrs.get("active_primitives", []):
                phases.append(str(phase))
        return phases

    def to_dict(self) -> dict[str, Any]:
        nodes = [
            {"id": str(node), **dict(attrs)}
            for node, attrs in sorted(self.graph.nodes(data=True), key=lambda item: item[1].get("index", 0))
        ]
        edges = [
            {"source": str(u), "target": str(v), **dict(attrs)}
            for u, v, attrs in self.graph.edges(data=True)
        ]
        edges.sort(key=lambda item: (self.graph.nodes[item["source"]].get("index", 0), item["target"]))
        return {
            "layer": self.layer_name,
            "name": self.name,
            "graph_type": "networkx.DiGraph",
            "nodes": nodes,
            "edges": edges,
        }


def build_phase_graph(semantics: SemanticPrimitiveGraph) -> OperationalPhaseGraph:
    graph = OperationalPhaseGraph(f"{semantics.name}_phases")
    invariants = {
        "reset": [
            "dynamic output nodes are precharged or equalized",
            "regeneration feedback is intentionally suppressed or balanced",
        ],
        "amplify": [
            "input differential voltage is converted into internal differential state",
            "latch positive feedback has not yet saturated",
        ],
        "regenerate": [
            "cross-coupled latch positive feedback dominates",
            "decision sign must preserve amplified differential state",
        ],
        "saturate": [
            "outputs settle to logic rails",
            "symbolic decision state becomes digital polarity",
        ],
    }
    exit_conditions = {
        "reset": "clock enters evaluate window",
        "amplify": "internal differential state is large enough to seed latch",
        "regenerate": "output differential voltage reaches logic threshold",
        "saturate": "next reset event",
    }
    for index, phase in enumerate(DEFAULT_PHASES):
        graph.add_phase(
            phase,
            index=index,
            active_primitives=semantics.active_in_phase(phase),
            invariants=invariants[phase],
            exit_condition=exit_conditions[phase],
        )
    graph.add_transition("reset", "amplify", "evaluate clock edge")
    graph.add_transition("amplify", "regenerate", "latch enable and sufficient internal delta")
    graph.add_transition("regenerate", "saturate", "positive feedback reaches logic threshold")
    graph.add_transition("saturate", "reset", "next clock cycle")
    return graph


def build_ota_phase_graph(semantics: SemanticPrimitiveGraph) -> OperationalPhaseGraph:
    graph = OperationalPhaseGraph(f"{semantics.name}_phases")
    phases = ["bias", "small_signal", "large_signal"]
    invariants = {
        "bias": [
            "all bias mirrors and active devices have a valid operating point",
            "gain and current-source devices remain in saturation",
        ],
        "small_signal": [
            "Miller compensation sets dominant-pole splitting",
            "Rz is referenced to the second-stage gm zero-cancellation target",
        ],
        "large_signal": [
            "slew currents charge Cc and CL_eff without violating output headroom",
            "output swing remains inside saturation limits",
        ],
    }
    exit_conditions = {
        "bias": "operating point is confirmed",
        "small_signal": "AC loop metrics are measured",
        "large_signal": "transient slew and swing checks complete",
    }
    for index, phase in enumerate(phases):
        graph.add_phase(
            phase,
            index=index,
            active_primitives=semantics.active_in_phase(phase),
            invariants=invariants[phase],
            exit_condition=exit_conditions[phase],
        )
    graph.add_transition("bias", "small_signal", "run AC after OP is valid")
    graph.add_transition("small_signal", "large_signal", "run transient and swing checks after AC")
    graph.add_transition("large_signal", "bias", "rebias if large-signal constraints fail")
    return graph

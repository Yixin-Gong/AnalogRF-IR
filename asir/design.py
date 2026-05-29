from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asir.dependency import DependencyGraph, build_comparator_dependency_graph, build_ota_dependency_graph
from asir.extraction import RuleBasedSemanticExtractor
from asir.io.yaml_export import export_design_yaml
from asir.phase import OperationalPhaseGraph, build_ota_phase_graph, build_phase_graph
from asir.rewrite import RewriteReasoner, RewriteReport
from asir.semantic import SemanticPrimitiveGraph
from asir.topology import TopologyGraph


@dataclass
class ASIRDesign:
    name: str
    comparator_family: str
    topology_graph: TopologyGraph
    semantic_graph: SemanticPrimitiveGraph
    dependency_graph: DependencyGraph
    phase_graph: OperationalPhaseGraph
    circuit_class: str = "comparator"

    def to_dict(self) -> dict[str, Any]:
        domain = "analog_ota" if self.circuit_class.lower() == "ota" else "analog_comparator"
        return {
            "asir_version": "0.1",
            "name": self.name,
            "domain": domain,
            "circuit_class": self.circuit_class,
            "comparator_family": self.comparator_family,
            "principles": {
                "separate_topology_from_semantics": True,
                "supports_operational_phase_semantics": True,
                "builds_symbolic_dependency_graphs": True,
                "supports_topology_rewrite_reasoning": True,
                "enables_constraint_propagation": True,
            },
            "layers": {
                "topology_graph": self.topology_graph.to_dict(),
                "semantic_primitive_graph": self.semantic_graph.to_dict(),
                "dependency_graph": self.dependency_graph.to_dict(),
                "operational_phase_graph": self.phase_graph.to_dict(),
            },
        }

    def export_yaml(self, path: str | Path) -> Path:
        return export_design_yaml(self, path)

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "comparator_family": self.comparator_family,
            "topology": {
                "mos_count": len(self.topology_graph.mos_devices()),
                "capacitor_count": len(self.topology_graph.capacitor_devices()),
                "resistor_count": len(self.topology_graph.resistor_devices()),
                "net_count": len(self.topology_graph.nets()),
                "clock_count": len(self.topology_graph.clocks()),
            },
            "semantic_primitives": self.semantic_graph.primitive_type_counts(),
            "phases": {
                phase: self.phase_graph.active_primitives(phase)
                for phase in ["reset", "amplify", "regenerate", "saturate"]
            },
            "dependency_targets": sorted(
                node
                for node, attrs in self.dependency_graph.graph.nodes(data=True)
                if attrs.get("symbol_type") == "derived"
            ),
        }

    def compare_rewrite_to(self, other: "ASIRDesign") -> RewriteReport:
        return RewriteReasoner().compare(self.semantic_graph, other.semantic_graph)


def build_design(topology: TopologyGraph) -> ASIRDesign:
    extractor = RuleBasedSemanticExtractor()
    semantics = extractor.extract(topology)
    if topology.circuit_class.lower() == "ota":
        dependencies = build_ota_dependency_graph(semantics)
        phases = build_ota_phase_graph(semantics)
    else:
        dependencies = build_comparator_dependency_graph(semantics)
        phases = build_phase_graph(semantics)
    return ASIRDesign(
        name=topology.name,
        comparator_family=topology.architecture,
        topology_graph=topology,
        semantic_graph=semantics,
        dependency_graph=dependencies,
        phase_graph=phases,
        circuit_class=topology.circuit_class,
    )

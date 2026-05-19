from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import networkx as nx

from asir.semantic import SemanticPrimitiveGraph


SAFE_FUNCTIONS = {
    "abs": abs,
    "max": max,
    "min": min,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
}


@dataclass
class DependencyRule:
    id: str
    output: str
    inputs: list[str]
    expression: str
    description: str
    primitive_refs: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)


class DependencyGraph:
    """Layer 3: symbolic causal dependency graph."""

    layer_name = "symbolic_dependencies"

    def __init__(self, name: str):
        self.name = name
        self.graph = nx.DiGraph(name=name, layer=self.layer_name)
        self.rules: dict[str, DependencyRule] = {}

    def add_symbol(self, symbol: str, symbol_type: str = "unknown", **attrs: Any) -> None:
        if symbol not in self.graph:
            self.graph.add_node(symbol, kind="symbol", symbol_type=symbol_type, **attrs)
        else:
            self.graph.nodes[symbol].update({k: v for k, v in attrs.items() if v is not None})
            current_type = self.graph.nodes[symbol].get("symbol_type", "unknown")
            if symbol_type == "derived" or (symbol_type != "unknown" and current_type == "unknown"):
                self.graph.nodes[symbol]["symbol_type"] = symbol_type

    def add_dependency(
        self,
        output: str,
        inputs: list[str],
        expression: str,
        description: str,
        primitive_refs: list[str] | None = None,
        constraints: list[str] | None = None,
    ) -> None:
        rule_id = f"rule_{len(self.rules) + 1:03d}_{output}"
        self.add_symbol(output, symbol_type="derived")
        for source in inputs:
            self.add_symbol(source, symbol_type="source")
            self.graph.add_edge(source, output, rule_id=rule_id, relation="causes")
        self.rules[rule_id] = DependencyRule(
            id=rule_id,
            output=output,
            inputs=list(inputs),
            expression=expression,
            description=description,
            primitive_refs=primitive_refs or [],
            constraints=constraints or [],
        )
        self.graph.nodes[output]["rule_id"] = rule_id
        self.graph.nodes[output]["expression"] = expression

    def forward_propagate(self, known_values: dict[str, float]) -> dict[str, float]:
        values = dict(known_values)
        for symbol, value in known_values.items():
            self.add_symbol(symbol)
            self.graph.nodes[symbol]["value"] = value

        for symbol in nx.topological_sort(self.graph):
            rule_id = self.graph.nodes[symbol].get("rule_id")
            if not rule_id or symbol in values:
                continue
            rule = self.rules[rule_id]
            if all(source in values for source in rule.inputs):
                env = {name: values[name] for name in rule.inputs}
                env.update(SAFE_FUNCTIONS)
                try:
                    values[symbol] = float(eval(rule.expression, {"__builtins__": {}}, env))
                    self.graph.nodes[symbol]["value"] = values[symbol]
                except Exception as exc:
                    self.graph.nodes[symbol]["evaluation_error"] = str(exc)
        return values

    def backward_trace(self, target: str) -> dict[str, Any]:
        if target not in self.graph:
            raise KeyError(f"Unknown dependency target '{target}'")
        ancestors = sorted(nx.ancestors(self.graph, target))
        paths: list[list[str]] = []
        for source in ancestors:
            if self.graph.in_degree(source) == 0:
                for path in nx.all_simple_paths(self.graph, source, target):
                    paths.append(path)
        return {
            "target": target,
            "source_symbols": sorted(source for source in ancestors if self.graph.in_degree(source) == 0),
            "intermediate_symbols": sorted(source for source in ancestors if self.graph.in_degree(source) > 0),
            "paths": paths,
            "rules": [
                asdict(rule)
                for rule in self.rules.values()
                if rule.output == target or rule.output in ancestors
            ],
        }

    def constraint_trace(self, target: str) -> list[dict[str, Any]]:
        trace = self.backward_trace(target)
        return [
            rule
            for rule in trace["rules"]
            if rule.get("constraints")
        ]

    def to_dict(self) -> dict[str, Any]:
        nodes = [
            {"id": str(node), **dict(attrs)}
            for node, attrs in sorted(self.graph.nodes(data=True), key=lambda item: str(item[0]))
        ]
        edges = [
            {"source": str(u), "target": str(v), **dict(attrs)}
            for u, v, attrs in self.graph.edges(data=True)
        ]
        edges.sort(key=lambda item: (item["source"], item["target"], item.get("rule_id", "")))
        return {
            "layer": self.layer_name,
            "name": self.name,
            "graph_type": "networkx.DiGraph",
            "nodes": nodes,
            "edges": edges,
            "rules": [asdict(rule) for rule in self.rules.values()],
        }


def build_comparator_dependency_graph(semantics: SemanticPrimitiveGraph) -> DependencyGraph:
    graph = DependencyGraph(f"{semantics.name}_dependencies")
    primitive_refs = [p.id for p in semantics.primitives()]
    latch_refs = [p.id for p in semantics.by_type("cross_coupled_latch")]
    input_refs = [p.id for p in semantics.by_type("differential_pair")]
    reset_refs = [p.id for p in semantics.by_type("reset_switch")]
    sampling_refs = [p.id for p in semantics.by_type("sampling_switch")]

    graph.add_dependency(
        "regeneration_time",
        ["CL", "gm_latch", "initial_delta_v", "logic_swing"],
        "(CL / gm_latch) * log(max(logic_swing / max(initial_delta_v, 1e-12), 1.000001))",
        "Regeneration time follows the latch time constant CL/gm and the initial differential seed.",
        primitive_refs=latch_refs,
        constraints=["gm_latch > 0", "initial_delta_v must be nonzero before regeneration"],
    )
    graph.add_dependency(
        "amplification_time",
        ["Cint", "gm_input"],
        "Cint / gm_input",
        "Input pair converts input voltage to differential internal charge during amplify.",
        primitive_refs=input_refs,
        constraints=["gm_input > 0"],
    )
    graph.add_dependency(
        "reset_time",
        ["R_reset", "CL"],
        "R_reset * CL",
        "Reset switches precharge or equalize dynamic nodes with an RC time constant.",
        primitive_refs=reset_refs,
        constraints=["reset phase must be long enough for output common-mode recovery"],
    )
    delay_inputs = ["reset_time", "amplification_time", "regeneration_time"]
    delay_expression = "reset_time + amplification_time + regeneration_time"
    if sampling_refs:
        graph.add_dependency(
            "sampling_time",
            ["R_sample", "Csample"],
            "R_sample * Csample",
            "Sampling switch settling when explicit input sampling is present.",
            primitive_refs=sampling_refs,
            constraints=["sampling switch on-resistance must settle within sample phase"],
        )
        delay_inputs.insert(1, "sampling_time")
        delay_expression = "reset_time + sampling_time + amplification_time + regeneration_time"
    graph.add_dependency(
        "delay",
        delay_inputs,
        delay_expression,
        "Comparator decision latency is the active path through reset, amplify, and regeneration.",
        primitive_refs=primitive_refs,
        constraints=["phase ordering reset -> amplify -> regenerate must be preserved"],
    )
    graph.add_dependency(
        "offset",
        ["mismatch", "device_area", "gm_input", "gm_latch"],
        "mismatch / sqrt(max(device_area, 1e-30)) * (1 / max(gm_input, 1e-12) + 1 / max(gm_latch, 1e-12))",
        "Input and latch mismatch map into input-referred offset, reduced by area and transconductance.",
        primitive_refs=input_refs + latch_refs,
        constraints=["differential devices should remain symmetric"],
    )
    graph.add_dependency(
        "noise",
        ["kT_over_C", "gm_input", "bandwidth"],
        "kT_over_C + bandwidth / max(gm_input, 1e-12)",
        "Sampling noise and input pair thermal noise are represented symbolically.",
        primitive_refs=input_refs + sampling_refs,
        constraints=["larger sampling capacitance reduces kT/C noise but increases delay"],
    )
    graph.add_dependency(
        "energy",
        ["CL", "VDD", "switching_activity"],
        "CL * VDD * VDD * switching_activity",
        "Dynamic comparator energy is dominated by charging and discharging capacitive nodes.",
        primitive_refs=primitive_refs,
        constraints=["lower CL improves energy and regeneration time together"],
    )
    return graph

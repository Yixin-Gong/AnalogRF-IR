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

DEPENDENCY_GRAPH_SCHEMA_VERSION = "analogrf_ir.typed_symbolic_dependency_graph.v0_1"
DEPENDENCY_RULE_SCHEMA_VERSION = "analogrf_ir.typed_symbolic_dependency_rule.v0_1"
DEPENDENCY_EDGE_SCHEMA_VERSION = "analogrf_ir.typed_symbolic_dependency_edge.v0_1"


@dataclass
class DependencyRule:
    id: str
    output: str
    inputs: list[str]
    expression: str
    description: str
    dependency_type: str = "symbolic_relation"
    output_quantity_type: str = "unknown"
    input_quantity_types: dict[str, str] = field(default_factory=dict)
    schema_version: str = DEPENDENCY_RULE_SCHEMA_VERSION
    primitive_refs: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)


class DependencyGraph:
    """Layer 3: symbolic causal dependency graph."""

    layer_name = "symbolic_dependencies"

    def __init__(self, name: str):
        self.name = name
        self.graph = nx.DiGraph(name=name, layer=self.layer_name)
        self.rules: dict[str, DependencyRule] = {}

    def add_symbol(self, symbol: str, symbol_type: str = "unknown", quantity_type: str | None = None, **attrs: Any) -> None:
        quantity = quantity_type or _infer_quantity_type(symbol)
        if symbol not in self.graph:
            self.graph.add_node(symbol, kind="symbol", symbol_type=symbol_type, quantity_type=quantity, **attrs)
        else:
            self.graph.nodes[symbol].update({k: v for k, v in attrs.items() if v is not None})
            current_type = self.graph.nodes[symbol].get("symbol_type", "unknown")
            if symbol_type == "derived" or (symbol_type != "unknown" and current_type == "unknown"):
                self.graph.nodes[symbol]["symbol_type"] = symbol_type
            if quantity_type and self.graph.nodes[symbol].get("quantity_type", "unknown") == "unknown":
                self.graph.nodes[symbol]["quantity_type"] = quantity_type

    def add_dependency(
        self,
        output: str,
        inputs: list[str],
        expression: str,
        description: str,
        primitive_refs: list[str] | None = None,
        constraints: list[str] | None = None,
        dependency_type: str | None = None,
    ) -> None:
        rule_id = f"rule_{len(self.rules) + 1:03d}_{output}"
        output_quantity = _infer_quantity_type(output)
        relation_type = dependency_type or _infer_dependency_type(output, inputs)
        self.add_symbol(output, symbol_type="derived", quantity_type=output_quantity)
        for source in inputs:
            input_quantity = _infer_quantity_type(source)
            self.add_symbol(source, symbol_type="source", quantity_type=input_quantity)
            self.graph.add_edge(
                source,
                output,
                schema_version=DEPENDENCY_EDGE_SCHEMA_VERSION,
                rule_id=rule_id,
                relation="causes",
                dependency_type=relation_type,
                source_quantity_type=input_quantity,
                target_quantity_type=output_quantity,
                polarity=_infer_dependency_polarity(expression, source),
            )
        self.rules[rule_id] = DependencyRule(
            id=rule_id,
            output=output,
            inputs=list(inputs),
            expression=expression,
            description=description,
            dependency_type=relation_type,
            output_quantity_type=output_quantity,
            input_quantity_types={source: _infer_quantity_type(source) for source in inputs},
            primitive_refs=primitive_refs or [],
            constraints=constraints or [],
        )
        self.graph.nodes[output]["rule_id"] = rule_id
        self.graph.nodes[output]["expression"] = expression
        self.graph.nodes[output]["dependency_type"] = relation_type

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
            "schema_version": DEPENDENCY_GRAPH_SCHEMA_VERSION,
            "layer": self.layer_name,
            "name": self.name,
            "graph_type": "networkx.DiGraph",
            "type_system": {
                "rule_schema_version": DEPENDENCY_RULE_SCHEMA_VERSION,
                "edge_schema_version": DEPENDENCY_EDGE_SCHEMA_VERSION,
                "quantity_types": [
                    "capacitance",
                    "current",
                    "energy",
                    "frequency",
                    "gain",
                    "impedance",
                    "noise",
                    "power",
                    "time",
                    "voltage",
                    "unknown",
                ],
                "dependency_types": [
                    "bias_dependency",
                    "energy_dependency",
                    "gain_bandwidth_dependency",
                    "noise_dependency",
                    "symbolic_relation",
                    "timing_dependency",
                    "voltage_headroom_dependency",
                ],
            },
            "nodes": nodes,
            "edges": edges,
            "rules": [asdict(rule) for rule in self.rules.values()],
        }


def _infer_quantity_type(symbol: str) -> str:
    name = symbol.lower()
    if any(token in name for token in ("cap", "cl", "cc", "cint", "csample", "cgs", "cgd")):
        return "capacitance"
    if any(token in name for token in ("gm", "gain")):
        return "gain"
    if any(token in name for token in ("ro", "rout", "rz", "resistance", "r_")):
        return "impedance"
    if any(token in name for token in ("time", "delay", "period")):
        return "time"
    if any(token in name for token in ("freq", "rad_s", "bandwidth", "pole", "zero", "sample_rate")):
        return "frequency"
    if any(token in name for token in ("current", "itail", "i_stage", "i_latch")):
        return "current"
    if "noise" in name or "kickback" in name:
        return "noise"
    if any(token in name for token in ("vdd", "vss", "vds", "vov", "swing", "offset", "icmr", "step", "noise", "margin")):
        return "voltage"
    if "power" in name:
        return "power"
    if any(token in name for token in ("energy", "pdp", "edp")):
        return "energy"
    return "unknown"


def _infer_dependency_type(output: str, inputs: list[str]) -> str:
    text = " ".join([output, *inputs]).lower()
    if any(token in text for token in ("delay", "time", "sample_rate", "regeneration")):
        return "timing_dependency"
    if any(token in text for token in ("noise", "kickback", "offset", "metastability")):
        return "noise_dependency"
    if any(token in text for token in ("energy", "power", "pdp", "edp")):
        return "energy_dependency"
    if any(token in text for token in ("icmr", "swing", "headroom", "vdd", "vss")):
        return "voltage_headroom_dependency"
    if any(token in text for token in ("gain", "pole", "zero", "bandwidth", "gm", "ro", "rout")):
        return "gain_bandwidth_dependency"
    if any(token in text for token in ("current", "itail", "i_stage")):
        return "bias_dependency"
    return "symbolic_relation"


def _infer_dependency_polarity(expression: str, source: str) -> str:
    expr = expression.replace(" ", "")
    token = source.replace(" ", "")
    if f"/{token}" in expr or f"max({token}," in expr and "/" in expr.split(f"max({token},", 1)[0]:
        return "negative"
    if token in expr:
        return "positive"
    return "unknown"


def build_comparator_dependency_graph(semantics: SemanticPrimitiveGraph) -> DependencyGraph:
    graph = DependencyGraph(f"{semantics.name}_dependencies")
    primitive_refs = [p.id for p in semantics.primitives()]
    latch_refs = [p.id for p in semantics.by_type("cross_coupled_latch")]
    input_refs = [p.id for p in semantics.by_type("differential_pair")]
    reset_refs = [p.id for p in semantics.by_type("reset_switch")]
    sampling_refs = [p.id for p in semantics.by_type("sampling_switch")]
    tail_refs = [p.id for p in semantics.by_type("tail_current_source")]

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
    delay_inputs = ["amplification_time", "regeneration_time"]
    delay_expression = "amplification_time + regeneration_time"
    if sampling_refs:
        graph.add_dependency(
            "sampling_time",
            ["R_sample", "Csample"],
            "R_sample * Csample",
            "Sampling switch settling when explicit input sampling is present.",
            primitive_refs=sampling_refs,
            constraints=["sampling switch on-resistance must settle within sample phase"],
        )
        delay_inputs.insert(0, "sampling_time")
        delay_expression = "sampling_time + amplification_time + regeneration_time"
    graph.add_dependency(
        "delay",
        delay_inputs,
        delay_expression,
        "Comparator decision latency is the active path from sampling or amplification through regeneration.",
        primitive_refs=primitive_refs,
        constraints=["phase ordering reset -> amplify -> regenerate must be preserved"],
    )
    graph.add_dependency(
        "cycle_time",
        ["reset_time", "delay"],
        "reset_time + delay",
        "Full comparator cycle time includes reset recovery plus active decision latency.",
        primitive_refs=primitive_refs,
        constraints=["reset recovery must complete before the next comparison"],
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
        "input_capacitance",
        ["Cgs_input", "Cgd_input", "Csample"],
        "Cgs_input + Cgd_input + Csample",
        "Input capacitance combines input-pair gate capacitance, Miller feedthrough capacitance, and sampling capacitance.",
        primitive_refs=input_refs + sampling_refs,
        constraints=["input loading trades off against kickback and noise"],
    )
    graph.add_dependency(
        "kickback_noise",
        ["Cgd_input", "input_capacitance", "Vclock_swing", "kickback_coupling"],
        "kickback_coupling * Vclock_swing * Cgd_input / max(input_capacitance, 1e-30)",
        "Kickback is modeled as clock/regeneration charge feedthrough reflected to the input.",
        primitive_refs=input_refs + latch_refs,
        constraints=["reduce input Cgd or add input isolation when kickback exceeds the front-end budget"],
    )
    graph.add_dependency(
        "clock_feedthrough",
        ["kickback_noise"],
        "0.5 * kickback_noise",
        "Clock feedthrough is tracked separately as the common-mode portion of kickback.",
        primitive_refs=input_refs + latch_refs + reset_refs,
        constraints=["clock feedthrough should remain below the receiver input disturbance budget"],
    )
    graph.add_dependency(
        "energy",
        ["CL", "VDD", "switching_activity"],
        "CL * VDD * VDD * switching_activity",
        "Dynamic comparator energy is dominated by charging and discharging capacitive nodes.",
        primitive_refs=primitive_refs,
        constraints=["lower CL improves energy and regeneration time together"],
    )
    graph.add_dependency(
        "energy_per_comparison",
        ["energy"],
        "energy",
        "Comparator energy is reported per comparison for clocked operation.",
        primitive_refs=primitive_refs,
    )
    graph.add_dependency(
        "pdp",
        ["energy", "delay"],
        "energy * delay",
        "Power-delay product captures the energy and latency trade-off.",
        primitive_refs=primitive_refs,
    )
    graph.add_dependency(
        "edp",
        ["energy", "delay"],
        "energy * delay * delay",
        "Energy-delay-squared emphasizes very slow low-energy points.",
        primitive_refs=primitive_refs,
    )
    graph.add_dependency(
        "max_sample_rate",
        ["cycle_time"],
        "1 / max(cycle_time, 1e-30)",
        "Maximum sample rate is bounded by reset plus active decision time.",
        primitive_refs=primitive_refs,
    )
    graph.add_dependency(
        "metastability_margin",
        ["input_step", "offset", "noise"],
        "input_step / max(sqrt(offset * offset + noise * noise), 1e-30)",
        "Metastability margin compares available input step to offset and noise uncertainty.",
        primitive_refs=input_refs + latch_refs,
        constraints=["increase input-referred decision margin for low-error operation"],
    )
    graph.add_dependency(
        "decision_margin",
        ["input_step", "offset", "noise"],
        "input_step - sqrt(offset * offset + noise * noise)",
        "Positive decision margin means the input step exceeds modeled uncertainty.",
        primitive_refs=input_refs + latch_refs,
    )
    graph.add_dependency(
        "output_swing",
        ["VDD", "VSS"],
        "VDD - VSS",
        "Regenerative latch outputs should resolve close to the available logic swing.",
        primitive_refs=latch_refs + reset_refs,
        constraints=["output swing must meet the downstream digital threshold budget"],
    )
    graph.add_dependency(
        "icmr",
        ["icmr_min", "icmr_max"],
        "max(icmr_max - icmr_min, 0.0)",
        "Input common-mode range is the valid interval for the input pair and tail stack.",
        primitive_refs=input_refs + tail_refs,
        constraints=["common-mode target must fit the input pair and tail headroom"],
    )
    graph.add_dependency(
        "area",
        ["device_area"],
        "device_area",
        "Total active device area is tracked as a layout and mismatch proxy.",
        primitive_refs=primitive_refs,
    )
    return graph


def build_ota_dependency_graph(semantics: SemanticPrimitiveGraph) -> DependencyGraph:
    graph = DependencyGraph(f"{semantics.name}_dependencies")
    primitive_refs = [p.id for p in semantics.primitives()]
    input_refs = [p.id for p in semantics.by_type("differential_pair")]
    stage2_refs = [p.id for p in semantics.by_type("second_stage_inverter")]
    comp_refs = [p.id for p in semantics.by_type("miller_compensation")]
    sf_refs = [p.id for p in semantics.by_type("source_follower_regulation")]
    cascode_refs = [p.id for p in semantics.by_type("cascode_stack")]
    load_refs = [p.id for p in semantics.by_type("current_mirror_load")]
    tail_refs = [p.id for p in semantics.by_type("tail_current_source")]

    if sf_refs:
        graph.add_dependency(
            "regulated_rout",
            ["ro1", "gm_sf", "ro_sf"],
            "ro1 * max(1.0 + gm_sf * ro_sf, 1.0)",
            "Source-follower local feedback holds the small-signal voltage across ro1 nearly constant, boosting output resistance.",
            primitive_refs=sf_refs,
            constraints=[
                "the ideal io2 -> 0 case is approached only when the follower loop gain is high",
                "source-follower headroom constrains the usable output common-mode range",
            ],
        )
        graph.add_dependency(
            "rout",
            ["regulated_rout"],
            "regulated_rout",
            "Use the regulated output resistance for pole placement when the source-follower loop is declared.",
            primitive_refs=sf_refs,
        )

    if cascode_refs:
        graph.add_dependency(
            "cascode_rout",
            ["ro_core", "gm_cascode", "ro_cascode"],
            "ro_core * max(1.0 + gm_cascode * ro_cascode, 1.0)",
            "Cascode devices multiply the core output resistance when their bias voltages keep the stack saturated.",
            primitive_refs=cascode_refs,
            constraints=[
                "cascode bias voltages must satisfy the stacked VDSAT headroom budget",
                "larger cascode gain improves dc gain but reduces output swing",
            ],
        )
        if not sf_refs:
            graph.add_dependency(
                "rout",
                ["cascode_rout"],
                "cascode_rout",
                "Use the cascode-boosted output resistance for single-stage cascode pole and gain estimates.",
                primitive_refs=cascode_refs,
            )
        graph.add_dependency(
            "headroom_margin",
            ["VDD", "VSS", "Vstack_required"],
            "VDD - VSS - Vstack_required",
            "Available voltage headroom after reserving saturation voltage for the stacked cascode devices.",
            primitive_refs=cascode_refs,
            constraints=["positive headroom margin is required before trusting the cascode gain boost"],
        )

    if comp_refs:
        graph.add_dependency(
            "unity_gain_rad_s",
            ["gm1", "Cc"],
            "gm1 / max(Cc, 1e-30)",
            "Two-stage Miller OTA unity-gain frequency is set primarily by gm1/Cc.",
            primitive_refs=input_refs + comp_refs,
            constraints=["Cc > 0", "gm1 > 0"],
        )
        graph.add_dependency(
            "dominant_pole_rad_s",
            ["ro1", "gm2", "ro2", "Cc"],
            "1 / max(ro1 * gm2 * ro2 * Cc, 1e-30)",
            "Miller multiplication pulls the first-stage dominant pole to a lower frequency.",
            primitive_refs=input_refs + load_refs + stage2_refs + comp_refs,
            constraints=["increase Cc to lower the dominant pole when phase margin is weak"],
        )
    else:
        graph.add_dependency(
            "dominant_pole_rad_s",
            ["rout", "CL_eff"],
            "1 / max(rout * CL_eff, 1e-30)",
            "Uncompensated OTA dominant pole is set by the highest-impedance node and its explicit capacitance.",
            primitive_refs=input_refs + load_refs + stage2_refs + cascode_refs,
            constraints=["do not assume Cc exists unless the topology declares a compensation network"],
        )
        graph.add_dependency(
            "unity_gain_rad_s",
            ["dc_gain", "dominant_pole_rad_s"],
            "dc_gain * dominant_pole_rad_s",
            "Uncompensated OTA unity-gain frequency follows the gain-bandwidth product of the actual pole locations.",
            primitive_refs=input_refs + load_refs + stage2_refs,
            constraints=["verify pole separation directly instead of applying Miller Rz-Cc rules"],
        )
    graph.add_dependency(
        "second_pole_rad_s",
        ["gm2", "CL_eff"],
        "gm2 / max(CL_eff, 1e-30)",
        "The non-dominant output pole is set by second-stage transconductance over effective output load.",
        primitive_refs=stage2_refs,
        constraints=["gm2 should place p2 well above unity-gain frequency"],
    )
    if comp_refs:
        graph.add_dependency(
            "zero_target_Rz",
            ["gm2"],
            "1 / max(gm2, 1e-30)",
            "A nulling resistor near 1/gm2 removes the right-half-plane Miller zero.",
            primitive_refs=stage2_refs + comp_refs,
            constraints=["Rz should track the measured or estimated second-stage gm"],
        )
        graph.add_dependency(
            "miller_zero_rad_s",
            ["Cc", "Rz", "gm2"],
            "1 / max(Cc * abs(1 / max(gm2, 1e-30) - Rz), 1e-30)",
            "Series Rz-Cc compensation places the zero according to 1/(Cc*(1/gm2 - Rz)).",
            primitive_refs=comp_refs,
            constraints=["Rz below 1/gm2 leaves a right-half-plane zero", "Rz above 1/gm2 creates a left-half-plane zero"],
        )
    if stage2_refs:
        graph.add_dependency(
            "dc_gain",
            ["gm1", "ro1", "gm2", "ro2"],
            "gm1 * ro1 * gm2 * ro2",
            "Two-stage open-loop gain is the product of first-stage and second-stage gains.",
            primitive_refs=input_refs + load_refs + stage2_refs,
            constraints=["all gain devices must remain in saturation at the operating point"],
        )
    else:
        graph.add_dependency(
            "dc_gain",
            ["gm1", "rout"],
            "gm1 * rout",
            "Single-stage OTA open-loop gain is input transconductance times the active-load output resistance.",
            primitive_refs=input_refs + load_refs + cascode_refs,
            constraints=["all input, load, and cascode devices must remain in saturation at the operating point"],
        )
    if comp_refs:
        graph.add_dependency(
            "slew_rate_pos",
            ["Itail", "Cc"],
            "Itail / max(Cc, 1e-30)",
            "Positive slew is bounded by first-stage tail current charging the compensation capacitor.",
            primitive_refs=tail_refs + comp_refs,
            constraints=["Itail must satisfy the requested large-signal slew rate"],
        )
    else:
        graph.add_dependency(
            "slew_rate_pos",
            ["I_stage2", "CL_eff"],
            "I_stage2 / max(CL_eff, 1e-30)",
            "Positive slew is bounded by available output current and explicit load capacitance.",
            primitive_refs=stage2_refs + tail_refs,
            constraints=["do not use Cc-based slew equations unless Cc exists"],
        )
    graph.add_dependency(
        "slew_rate_neg",
        ["I_stage2", "CL_eff"],
        "I_stage2 / max(CL_eff, 1e-30)",
        "Negative slew is bounded by the second-stage output current and effective load.",
        primitive_refs=stage2_refs,
        constraints=["I_stage2 must satisfy the requested output discharge slew rate"],
    )
    graph.add_dependency(
        "phase_margin_risk",
        ["unity_gain_rad_s", "second_pole_rad_s", "dominant_pole_rad_s"],
        "unity_gain_rad_s / max(second_pole_rad_s, 1e-30)",
        "Phase-margin risk rises when the non-dominant pole approaches unity gain.",
        primitive_refs=primitive_refs,
        constraints=(
            ["lower the dominant pole with Cc before chasing bandwidth when PM fails"]
            if comp_refs
            else ["reduce non-dominant pole loading or lower unity-gain frequency; no Rz-Cc compensation is declared"]
        ),
    )
    return graph

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from asir.capabilities import detect_circuit_capabilities
from diagnostics.action_optimizer import (
    apply_optimized_action_plan,
    build_surrogate_intervention_model,
    optimize_tuning_actions,
)
from schemas.design_state import DesignState
from simulator.ngspice import SimulationResult
from specs.models import CircuitSpecModel


STANDARD_PERFORMANCE_METRICS = (
    "dc_gain",
    "unity_gain_bandwidth",
    "phase_margin",
    "slew_rate",
    "output_swing",
    "saturation_margin",
)

HEADROOM_METRICS = {
    "output_swing",
    "swing",
    "icmr",
    "icmr_min",
    "icmr_max",
    "input_common_mode_min",
    "input_common_mode_max",
    "saturation_margin",
    "saturation_required_gap",
}

CAUSE_SCORE_WEIGHTS = {
    "structural": 0.40,
    "intervention": 0.42,
    "propagation": 0.18,
}

EDGE_STRENGTH_WEIGHTS = {
    "high": 1.0,
    "medium": 0.62,
    "low": 0.35,
}

EDGE_TYPE_WEIGHTS = {
    "gain_propagation_dependency": 1.0,
    "pole_zero_dependency": 0.92,
    "signal_dependency": 0.86,
    "bias_dependency": 0.74,
    "structural_decomposition": 0.45,
}

CAUSAL_GRAPH_SCHEMA_VERSION = "analogrf_ir.typed_causal_graph.v0_1"
CAUSAL_EDGE_SCHEMA_VERSION = "analogrf_ir.typed_causal_edge.v0_1"
CAUSAL_EDGE_TYPES = tuple(EDGE_TYPE_WEIGHTS)


@dataclass(frozen=True)
class CandidateCause:
    node: str
    score: float
    metrics: tuple[str, ...]
    evidence: tuple[str, ...]
    simulation_support: str
    component: str = ""
    score_components: dict[str, float] = field(default_factory=dict)
    structural_reason: str = ""
    propagation_path: tuple[str, ...] = ()
    propagation_reason: str = ""
    spec_impact: str = ""
    intervention_test: str = ""
    edge_types: tuple[str, ...] = ()
    sensitivity_prior: float = 0.0


def build_causal_diagnostics(
    state: DesignState,
    best_meta: dict[str, Any],
    sim_result: SimulationResult,
    target_status: dict[str, dict[str, Any]],
    spec_model: CircuitSpecModel,
    flow_meta: dict[str, Any],
) -> dict[str, Any]:
    capabilities = detect_circuit_capabilities(state)
    graph = _build_dependency_graph(state, target_status, spec_model, capabilities)
    symptoms = _failure_symptoms(target_status)
    causes = _rank_root_causes(state, symptoms, sim_result, capabilities, graph)
    paths = _causal_paths(state, symptoms, causes, capabilities)
    predictions = _counterfactual_predictions(causes)
    experiments = _validation_experiments(causes)
    tuning = _attribution_guided_tuning(state, symptoms, causes)
    intervention_model = (flow_meta or {}).get("local_intervention_model")
    if not intervention_model:
        intervention_model = build_surrogate_intervention_model(
            tuning=tuning,
            target_status=target_status,
        )
    action_optimizer = optimize_tuning_actions(
        tuning=tuning,
        target_status=target_status,
        intervention_model=intervention_model,
    )
    tuning = apply_optimized_action_plan(tuning, action_optimizer)
    attribution = _agent_failure_attribution(state, symptoms, causes, paths, tuning)
    sensitivity_comparison = _sensitivity_ranking_comparison(state, symptoms, causes)
    return {
        "schema_version": "analogrf_ir.causal_diagnostics.v0_1",
        "method": "structure_aware_causal_graph_with_intervention_approximation",
        "ranking_model": {
            "principle": "Rank structural root-cause nodes whose intervention is expected to reduce the specification violation.",
            "score_formula": "score = alpha * structural_influence + beta * intervention_impact + gamma * propagation_contribution",
            "weights": {
                "alpha": CAUSE_SCORE_WEIGHTS["structural"],
                "beta": CAUSE_SCORE_WEIGHTS["intervention"],
                "gamma": CAUSE_SCORE_WEIGHTS["propagation"],
            },
            "sensitivity_role": "legacy sensitivity ranking is retained only as a weak prior and debugging comparison, not as the causal decision rule.",
        },
        "scope": {
            "design_name": state.design_name,
            "topology": state.topology.name,
            "architecture": state.topology.architecture,
            "class": state.topology.class_,
            "profile": capabilities.profile_name,
            "capabilities": list(capabilities.names),
            "source_kind": (flow_meta or {}).get("source_kind", ""),
        },
        "dependency_graph": graph,
        "failure_symptom_analysis": symptoms,
        "causal_paths": paths,
        "root_cause_attribution": [cause.__dict__ for cause in causes],
        "agent_failure_attribution": attribution,
        "attribution_guided_tuning": tuning,
        "local_intervention_model": intervention_model,
        "constrained_action_optimizer": action_optimizer,
        "sensitivity_ranking_comparison": sensitivity_comparison,
        "counterfactual_predictions": predictions,
        "suggested_validation_experiments": experiments,
        "validation_protocol": {
            "principle": "Apply the attribution-guided tuning plan first, then use SPICE to close the loop.",
            "acceptance": "A tuning action is useful when the targeted metric improves without creating a larger primary-target violation.",
            "uncertainty_rule": "Scores are ranked hypotheses, not single-point certainty.",
        },
        "input_support": {
            "measured_metrics": dict(sim_result.measurements or {}),
            "estimated_metrics": dict(best_meta.get("performance", {}) or {}),
        },
    }


def _build_dependency_graph(
    state: DesignState,
    target_status: dict[str, dict[str, Any]],
    spec_model: CircuitSpecModel,
    capabilities,
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    block_members = _block_members(state)
    input_nets = _port_nets(state, "input")
    output_nets = _port_nets(state, "output")
    bias_nets = _port_nets(state, "bias")
    for net_name in sorted(input_nets | output_nets | bias_nets | _shared_internal_nets(state)):
        net_role = "internal"
        if net_name in input_nets:
            net_role = "input"
        elif net_name in output_nets:
            net_role = "output"
        elif net_name in bias_nets:
            net_role = "bias"
        nodes.append({"id": f"net.{net_name}", "type": "internal_node", "label": net_name, "net_role": net_role})

    for dev in state.topology.devices:
        ts = state.transistors.get(dev.id)
        if ts is None:
            continue
        p = ts.parameters
        cap = _device_capacitance(p)
        ro = 1.0 / p.gds if p.gds > 0 else None
        nodes.append(
            {
                "id": f"device.{dev.id}",
                "type": "transistor",
                "label": dev.id,
                "role": dev.role,
                "stage": dev.stage,
                "device_type": dev.type,
                "connections": dict(dev.connections or {}),
            }
        )
        device_attrs = {
            "device": dev.id,
            "role": dev.role,
            "stage": dev.stage,
            "type": dev.type,
        }
        for param, value, unit, extra in (
            ("gm", p.gm, "S", {}),
            ("ro", ro, "ohm", {"source": "1/gds"}),
            ("Vov", p.vdsat, "V", {"source": "vdsat_proxy"}),
            ("capacitance", cap, "F", {"components": {"cgg": p.cgg, "cgs": p.cgs, "cgd": p.cgd, "cdd": p.cdd}}),
            ("bias_current", p.id, "A", {}),
            ("headroom", p.vds - p.vdsat, "V", {"vds": p.vds, "vdsat": p.vdsat}),
        ):
            node_id = f"device.{dev.id}.{param}"
            nodes.append(
                {
                    "id": node_id,
                    "type": "device_parameter",
                    "label": f"{dev.id}.{param}",
                    "value": value,
                    "unit": unit,
                    **device_attrs,
                    **extra,
                }
            )
            edges.append(
                _edge(
                    f"device.{dev.id}",
                    node_id,
                    "high",
                    "Device-level structural node decomposes into its operating-point attributes.",
                    "structural_decomposition",
                )
            )
        edges.extend(
            [
                _edge(f"device.{dev.id}.bias_current", f"device.{dev.id}.gm", "high", "For fixed gm/ID sizing, higher current increases gm.", "bias_dependency"),
                _edge(f"device.{dev.id}.bias_current", f"device.{dev.id}.capacitance", "medium", "Larger current usually implies larger device width and capacitance.", "bias_dependency"),
                _edge(f"device.{dev.id}.Vov", f"device.{dev.id}.headroom", "high", "Higher overdrive/VDSAT consumes saturation headroom.", "bias_dependency"),
            ]
        )
        for terminal, net_name in (dev.connections or {}).items():
            if not net_name:
                continue
            edge_type = "bias_dependency" if net_name in bias_nets or terminal in {"body", "source"} and net_name in {"vdd", "gnd"} else "signal_dependency"
            edges.append(_edge(f"device.{dev.id}", f"net.{net_name}", "medium", f"{terminal} terminal connects {dev.id} to {net_name}.", edge_type))
        if any(net in output_nets for terminal, net in (dev.connections or {}).items() if terminal in {"drain", "source"}):
            edges.append(
                _edge(
                    f"device.{dev.id}.ro",
                    "behavior.output_resistance",
                    "high",
                    "This device is physically connected to the output branch, so its gds/ro contributes to the small-signal output resistance.",
                    "gain_propagation_dependency",
                )
            )
            edges.append(
                _edge(
                    f"device.{dev.id}.capacitance",
                    "behavior.non_dominant_pole",
                    "medium",
                    "Output-branch capacitance can move the non-dominant pole.",
                    "pole_zero_dependency",
                )
            )
        if any(net in input_nets for terminal, net in (dev.connections or {}).items() if terminal == "gate"):
            edges.append(
                _edge(
                    f"device.{dev.id}.gm",
                    "behavior.input_transconductance",
                    "high",
                    "Input-gate device gm launches the forward signal path.",
                    "gain_propagation_dependency",
                )
            )

    for block, members in block_members.items():
        nodes.append(
            {
                "id": f"block.{block}",
                "type": "functional_block",
                "label": block.replace("_", " "),
                "members": members,
                "present": bool(members) or block == "compensation_network",
            }
        )
        for dev_id in members:
            role = state.get_device_def(dev_id).role if state.get_device_def(dev_id) else ""
            gm_strength = "high" if block in {"differential_pair", "dynamic_latch"} else "medium"
            ro_strength = "high" if block in {"load_stage", "output_stage", "current_mirror"} else "medium"
            edges.append(_edge(f"device.{dev_id}.gm", f"block.{block}", gm_strength, f"{role} transconductance controls this block response.", "gain_propagation_dependency"))
            edges.append(_edge(f"device.{dev_id}.ro", f"block.{block}", ro_strength, f"{role} output resistance controls block gain or pole location.", "gain_propagation_dependency"))
            edges.append(_edge(f"device.{dev_id}.capacitance", f"block.{block}", "medium", f"{role} capacitance contributes to local pole and load.", "pole_zero_dependency"))
            edges.append(_edge(f"device.{dev_id}.headroom", "constraint.headroom", "high", f"{role} must keep VDS above saturation requirement.", "bias_dependency"))

    if block_members.get("current_mirror") and block_members.get("load_stage"):
        edges.append(
            _edge(
                "block.current_mirror",
                "block.load_stage",
                "high",
                "The diode-connected mirror side biases and mirrors the active load branch.",
                "bias_dependency",
            )
        )
    if block_members.get("bias_network") and block_members.get("differential_pair"):
        edges.append(
            _edge(
                "block.bias_network",
                "block.differential_pair",
                "high",
                "Tail or bias current sets the differential-pair operating point.",
                "bias_dependency",
            )
        )

    for name, value in state.global_parameters.items():
        if name in {"Cc", "Rz", "CL", "Cload"} or name.lower().startswith("c"):
            nodes.append({"id": f"global.{name}", "type": "global_parameter", "label": name, "value": value})
    if "Cc" in state.global_parameters:
        edges.append(_edge("global.Cc", "block.compensation_network", "high", "Miller capacitance sets dominant-pole and slew trade-off.", "pole_zero_dependency"))
    if "Rz" in state.global_parameters:
        edges.append(_edge("global.Rz", "block.compensation_network", "high", "Zero-setting resistor changes compensation zero location.", "pole_zero_dependency"))

    for constraint in ("headroom", "stability_margin", "linearity"):
        nodes.append({"id": f"constraint.{constraint}", "type": "constraint", "label": constraint.replace("_", " ")})

    for behavior in (
        "input_transconductance",
        "output_resistance",
        "bias_headroom",
        "dominant_pole",
        "non_dominant_pole",
        "compensation_zero",
        "large_signal_charge",
        "latch_regeneration",
    ):
        nodes.append({"id": f"behavior.{behavior}", "type": "circuit_behavior", "label": behavior.replace("_", " ")})

    metric_names = sorted(set(STANDARD_PERFORMANCE_METRICS) | set(target_status))
    for metric in metric_names:
        status = target_status.get(metric, {})
        nodes.append(
            {
                "id": f"metric.{metric}",
                "type": "performance_metric",
                "label": metric,
                "measurement_key": status.get("measurement_key", spec_model.measurement_key(metric)),
                "status": status.get("status", "not_targeted"),
                "value": status.get("value"),
                "min": status.get("min"),
                "max": status.get("max"),
            }
        )

    edges.extend(_metric_edges(capabilities))
    return {
        "schema_version": CAUSAL_GRAPH_SCHEMA_VERSION,
        "type_system": {
            "edge_schema_version": CAUSAL_EDGE_SCHEMA_VERSION,
            "edge_types": list(CAUSAL_EDGE_TYPES),
            "node_type_inference": "node id namespace with explicit node.type when present",
            "polarity_values": ["positive", "negative", "mixed", "conditional", "unknown"],
        },
        "nodes": nodes,
        "edges": edges,
    }


def _metric_edges(capabilities) -> list[dict[str, Any]]:
    edges = [
        _edge("block.differential_pair", "behavior.input_transconductance", "high", "Input-pair gm is the forward transconductance source."),
        _edge("block.differential_pair", "behavior.dominant_pole", "medium", "First-stage resistance and capacitance can create the dominant pole."),
        _edge("behavior.input_transconductance", "metric.dc_gain", "high", "Low-frequency gain scales with useful input transconductance."),
        _edge("behavior.input_transconductance", "metric.unity_gain_bandwidth", "high", "UGBW scales with input gm over effective dominant capacitance."),
        _edge("block.load_stage", "behavior.output_resistance", "high", "Load-stage ro contributes directly to voltage gain."),
        _edge("behavior.output_resistance", "metric.dc_gain", "high", "Higher signal-path output resistance increases voltage gain."),
        _edge("block.load_stage", "behavior.non_dominant_pole", "medium", "Load capacitance and resistance move non-dominant poles."),
        _edge("behavior.non_dominant_pole", "metric.phase_margin", "high", "Moving the non-dominant pole toward unity gain reduces phase margin."),
        _edge("block.output_stage", "metric.output_swing", "high", "Output device VDSAT and bias define available swing."),
        _edge("block.output_stage", "metric.slew_rate", "high", "Output current charges or discharges the load capacitance."),
        _edge("block.bias_network", "behavior.bias_headroom", "high", "Bias devices set stack voltage allocation and saturation margin."),
        _edge("behavior.bias_headroom", "constraint.headroom", "high", "Insufficient bias headroom pushes devices toward triode or weak saturation."),
        _edge("block.bias_network", "behavior.large_signal_charge", "medium", "Available bias current limits large-signal charging current."),
        _edge("behavior.large_signal_charge", "metric.slew_rate", "high", "Slew rate is available current over effective capacitance."),
        _edge("constraint.headroom", "metric.output_swing", "high", "Headroom loss clips the output range."),
        _edge("constraint.headroom", "metric.saturation_margin", "high", "Saturation margin is the measured VDS-VDSAT headroom of the limiting device."),
        _edge("constraint.headroom", "metric.dc_gain", "medium", "Devices leaving saturation reduce effective ro and gain."),
        _edge("constraint.stability_margin", "metric.phase_margin", "high", "Pole-zero separation directly determines phase margin."),
        _edge("constraint.linearity", "metric.output_swing", "medium", "Linearity range limits usable output excursion."),
    ]
    if capabilities.has("miller_capacitive_compensation"):
        edges.extend(
            [
                _edge("block.compensation_network", "behavior.dominant_pole", "high", "Cc intentionally pulls the main pole to lower frequency."),
                _edge("block.compensation_network", "behavior.compensation_zero", "high", "Rz places the compensation zero."),
                _edge("behavior.compensation_zero", "constraint.stability_margin", "high", "Zero placement can cancel or reinforce phase lag."),
                _edge("block.compensation_network", "metric.phase_margin", "high", "Cc/Rz control dominant pole and compensation zero."),
                _edge("block.compensation_network", "metric.unity_gain_bandwidth", "high", "Increasing Cc lowers UGBW for fixed gm."),
                _edge("block.compensation_network", "metric.slew_rate", "high", "Cc increases large-signal charge requirement."),
            ]
        )
    if capabilities.has("source_follower_regulation"):
        edges.extend(
            [
                _edge("block.source_follower_regulation", "metric.dc_gain", "high", "Local source-follower feedback boosts output resistance."),
                _edge("block.source_follower_regulation", "metric.output_swing", "high", "The follower path consumes output common-mode headroom."),
                _edge("block.source_follower_regulation", "metric.phase_margin", "medium", "Local feedback adds internal poles that must stay beyond unity gain."),
            ]
        )
    if capabilities.has("dynamic_latch"):
        edges.extend(
            [
                _edge("block.dynamic_latch", "metric.delay", "high", "Latch gm and capacitance set regeneration time."),
                _edge("block.dynamic_latch", "behavior.latch_regeneration", "high", "Positive-feedback gm over load capacitance controls regeneration."),
                _edge("behavior.latch_regeneration", "metric.regeneration_time", "high", "Regeneration behavior sets the comparator decision time."),
                _edge("block.dynamic_latch", "metric.energy", "medium", "Switched latch capacitance sets dynamic energy."),
            ]
        )
    return edges


def _failure_symptoms(target_status: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    symptoms = []
    for metric, status in target_status.items():
        if status.get("status") not in {"fail", "unverified"}:
            continue
        value = status.get("value")
        deviation = None
        direction = "unknown"
        if value is not None and status.get("min") is not None:
            deviation = float(value) - float(status["min"])
            direction = "below_min" if deviation < 0 else "measured_by_estimate_only"
        if value is not None and status.get("max") is not None:
            max_dev = float(status["max"]) - float(value)
            if deviation is None or max_dev < deviation:
                deviation = max_dev
                direction = "above_max" if max_dev < 0 else direction
        symptoms.append(
            {
                "metric": metric,
                "status": status.get("status"),
                "source": status.get("source"),
                "value": value,
                "min": status.get("min"),
                "max": status.get("max"),
                "deviation_abs": deviation,
                "deviation_rel": status.get("margin_rel"),
                "direction": direction,
                "measurement_key": status.get("measurement_key"),
            }
        )
    return symptoms


def _causal_paths(state: DesignState, symptoms: list[dict[str, Any]], causes: list[CandidateCause], capabilities) -> list[dict[str, Any]]:
    paths = []
    for symptom in symptoms:
        if symptom["status"] != "fail":
            continue
        metric = symptom["metric"]
        metric_causes = [cause for cause in causes if metric in cause.metrics]
        if not metric_causes:
            continue
        for rank, cause in enumerate(metric_causes[:3], start=1):
            chain = _path_chain_for_metric(state, metric, cause.node, capabilities)
            paths.append(
                {
                    "metric": metric,
                    "rank": rank,
                    "strength": _score_to_strength(cause.score),
                    "chain": chain,
                    "causal_direction": "root_cause_to_metric",
                    "explanation": _path_explanation(metric, cause.node),
                    "testability": "Perturb the root node by 5-10 percent and compare the metric direction in SPICE.",
                }
            )
    return paths


def _rank_root_causes(
    state: DesignState,
    symptoms: list[dict[str, Any]],
    sim_result: SimulationResult,
    capabilities,
    graph: dict[str, Any],
) -> list[CandidateCause]:
    failed_symptoms = [symptom for symptom in symptoms if symptom["status"] == "fail"]
    if not failed_symptoms:
        return []

    legacy = _legacy_sensitivity_ranking(state, symptoms, capabilities)
    legacy_prior = {item["node"]: float(item.get("score", 0.0) or 0.0) for item in legacy}
    records: dict[str, dict[str, Any]] = {}

    for symptom in failed_symptoms:
        metric = symptom["metric"]
        metric_node = f"metric.{metric}"
        for node in _candidate_cause_nodes(state, graph, metric):
            if not _candidate_relevant_to_metric(node, metric):
                continue
            path_score, path, edge_types = _best_dependency_path(graph, node, metric_node)
            if path_score <= 0.0:
                continue
            structural = _structural_influence_score(path_score, path)
            intervention = _intervention_impact_score(state, node, metric, symptom, sim_result)
            propagation = _propagation_contribution_score(state, node, metric, path_score)
            if max(structural, intervention, propagation) <= 0.02:
                continue
            score = (
                CAUSE_SCORE_WEIGHTS["structural"] * structural
                + CAUSE_SCORE_WEIGHTS["intervention"] * intervention
                + CAUSE_SCORE_WEIGHTS["propagation"] * propagation
            )
            prior = legacy_prior.get(node, 0.0)
            item = records.setdefault(
                node,
                {
                    "score": 0.0,
                    "metrics": set(),
                    "evidence": [],
                    "support": [],
                    "components": {"structural": [], "intervention": [], "propagation": []},
                    "paths": [],
                    "edge_types": set(),
                    "prior": prior,
                    "metric_scores": [],
                },
            )
            item["score"] += score * _target_priority_weight(state, metric)
            item["metrics"].add(metric)
            item["components"]["structural"].append(structural)
            item["components"]["intervention"].append(intervention)
            item["components"]["propagation"].append(propagation)
            item["paths"].append(path)
            item["edge_types"].update(edge_types)
            item["metric_scores"].append({"metric": metric, "score": round(score, 4)})
            item["evidence"].append(_causal_evidence_text(state, node, metric, structural, intervention, propagation))
            item["support"].append(_node_support(state, node))

    if not records and legacy:
        return _legacy_fallback_causes(state, legacy)

    ranked = []
    max_score = max((item["score"] for item in records.values()), default=1.0)
    for node, item in records.items():
        score = min(item["score"] / max(max_score, 1e-30), 1.0)
        best_path = _shortest_nonempty_path(item["paths"])
        metrics = tuple(sorted(item["metrics"]))
        components = {
            name: round(max(values), 4) if values else 0.0
            for name, values in item["components"].items()
        }
        components["weighted_score_raw"] = round(float(item["score"]), 4)
        components["sensitivity_prior_debug"] = round(float(item.get("prior", 0.0)), 4)
        ranked.append(
            CandidateCause(
                node=node,
                score=round(score, 4),
                metrics=metrics,
                evidence=tuple(_dedupe_text(item["evidence"])),
                simulation_support="; ".join(_dedupe_text(item["support"][:3])),
                component=_component_for_node(node),
                score_components=components,
                structural_reason=_structural_reason_text(node, best_path),
                propagation_path=tuple(best_path),
                propagation_reason=_propagation_reason_text(metrics, best_path),
                spec_impact=_spec_impact_text(node, metrics),
                intervention_test=_counterfactual_for_node(node)[0],
                edge_types=tuple(sorted(item["edge_types"])),
                sensitivity_prior=round(float(item.get("prior", 0.0)), 4),
            )
        )
    return sorted(ranked, key=lambda cause: (cause.score, cause.score_components.get("intervention", 0.0)), reverse=True)


def _candidate_cause_nodes(state: DesignState, graph: dict[str, Any], metric: str) -> list[str]:
    graph_nodes = {item.get("id") for item in graph.get("nodes", [])}
    out: list[str] = []
    for dev in state.topology.devices:
        for param in ("gm", "ro", "capacitance", "bias_current", "headroom", "Vov"):
            if param == "gm" and _is_bias_role(dev.role or "") and metric in {"dc_gain", "gain", "unity_gain_bandwidth", "ugbw", "bandwidth"}:
                continue
            node = f"device.{dev.id}.{param}"
            if node in graph_nodes and _candidate_relevant_to_metric(node, metric):
                out.append(node)
    for name in state.global_parameters:
        node = f"global.{name}"
        if node in graph_nodes and _candidate_relevant_to_metric(node, metric):
            out.append(node)
    if "block.compensation_network" in graph_nodes and _candidate_relevant_to_metric("block.compensation_network", metric):
        out.append("block.compensation_network")
    return out


def _candidate_relevant_to_metric(node: str, metric: str) -> bool:
    if metric in {"dc_gain", "gain"}:
        return node.endswith((".gm", ".ro", ".headroom", ".Vov")) or node in {"block.compensation_network"}
    if metric in {"unity_gain_bandwidth", "ugbw", "bandwidth"}:
        return node.endswith((".gm", ".capacitance", ".bias_current")) or node in {"global.Cc", "global.CL", "global.Cload", "block.compensation_network"}
    if metric == "phase_margin":
        return node.endswith((".capacitance", ".gm", ".ro")) or node in {"global.Cc", "global.Rz", "global.CL", "global.Cload", "block.compensation_network"}
    if metric in {"slew_rate", "slew_rate_pos", "slew_rate_neg"}:
        return node.endswith((".bias_current", ".capacitance")) or node in {"global.Cc", "global.CL", "global.Cload"}
    if metric in HEADROOM_METRICS:
        return node.endswith((".headroom", ".Vov", ".bias_current"))
    if metric in {"delay", "decision_time", "propagation_delay", "regeneration_time"}:
        return node.endswith((".gm", ".capacitance", ".bias_current"))
    if metric in {"power", "energy", "energy_per_comparison", "pdp", "edp"}:
        return node.endswith((".bias_current", ".capacitance")) or node.startswith("global.I_")
    return node.startswith("device.") or node.startswith("global.")


def _best_dependency_path(graph: dict[str, Any], source: str, target: str, *, max_depth: int = 7) -> tuple[float, list[str], list[str]]:
    adjacency: dict[str, list[dict[str, Any]]] = {}
    for edge in graph.get("edges", []):
        adjacency.setdefault(edge.get("source", ""), []).append(edge)
    best_score = 0.0
    best_path: list[str] = []
    best_types: list[str] = []
    stack: list[tuple[str, list[str], list[str], float]] = [(source, [source], [], 1.0)]
    while stack:
        node, path, edge_types, score = stack.pop()
        if len(path) > max_depth:
            continue
        if node == target:
            edge_count = max(1, len(path) - 1)
            normalized = score ** (1.0 / edge_count)
            normalized *= 1.0 / (1.0 + 0.04 * max(0, edge_count - 2))
            if normalized > best_score:
                best_score = normalized
                best_path = path
                best_types = edge_types
            continue
        for edge in adjacency.get(node, []):
            nxt = edge.get("target", "")
            if not nxt or nxt in path:
                continue
            strength = EDGE_STRENGTH_WEIGHTS.get(str(edge.get("strength", "medium")), 0.5)
            edge_type = str(edge.get("edge_type", "signal_dependency"))
            type_weight = EDGE_TYPE_WEIGHTS.get(edge_type, 0.65)
            stack.append((nxt, path + [nxt], edge_types + [edge_type], score * strength * type_weight))
    return round(best_score, 4), best_path, best_types


def _structural_influence_score(path_score: float, path: list[str]) -> float:
    if not path:
        return 0.0
    path_bonus = 1.0 if len(path) <= 4 else max(0.72, 1.0 - 0.05 * (len(path) - 4))
    return round(min(1.0, max(0.0, path_score * path_bonus)), 4)


def _intervention_impact_score(
    state: DesignState,
    node: str,
    metric: str,
    symptom: dict[str, Any],
    sim_result: SimulationResult,
) -> float:
    gap = _symptom_gap_fraction(symptom)
    if node.startswith("global."):
        return round(_global_intervention_impact(state, node, metric, gap), 4)
    if node == "block.compensation_network":
        return round(0.92 if metric == "phase_margin" else 0.55, 4)
    parts = node.split(".")
    if len(parts) < 3 or parts[0] != "device":
        return 0.0
    dev_id, param = parts[1], parts[2]
    base = 0.0
    if metric in {"dc_gain", "gain"}:
        if param == "ro":
            base = _gain_ro_impact(state, dev_id)
        elif param == "gm":
            base = _gain_gm_impact(state, dev_id)
        elif param in {"headroom", "Vov"}:
            base = _headroom_impact(state, dev_id)
    elif metric in {"unity_gain_bandwidth", "ugbw", "bandwidth"}:
        if param == "gm":
            base = _gain_gm_impact(state, dev_id)
        elif param == "capacitance":
            base = _capacitance_impact(state, dev_id)
        elif param == "bias_current":
            base = _bias_current_impact(state, dev_id)
    elif metric == "phase_margin":
        if param == "capacitance":
            base = _capacitance_impact(state, dev_id)
        elif param in {"gm", "ro"}:
            base = 0.45 * _signal_path_device_factor(state, dev_id)
    elif metric in {"slew_rate", "slew_rate_pos", "slew_rate_neg"}:
        if param == "bias_current":
            base = _bias_current_impact(state, dev_id)
        elif param == "capacitance":
            base = _capacitance_impact(state, dev_id)
    elif metric in HEADROOM_METRICS:
        if param in {"headroom", "Vov", "bias_current"}:
            base = max(_headroom_impact(state, dev_id), 0.35 * _bias_current_impact(state, dev_id))
    elif metric in {"power", "energy", "energy_per_comparison", "pdp", "edp"}:
        if param == "bias_current":
            base = _bias_current_impact(state, dev_id)
        elif param == "capacitance":
            base = 0.65 * _capacitance_impact(state, dev_id)
    elif metric in {"delay", "decision_time", "propagation_delay", "regeneration_time"}:
        if param == "gm":
            base = _gain_gm_impact(state, dev_id)
        elif param == "capacitance":
            base = _capacitance_impact(state, dev_id)
    if not sim_result.success:
        base *= 0.85
    return round(min(1.0, base * (0.68 + 0.55 * gap)), 4)


def _propagation_contribution_score(state: DesignState, node: str, metric: str, path_score: float) -> float:
    factor = 0.45
    if metric in {"dc_gain", "gain"} and node.endswith(".ro"):
        factor = 0.55 + 0.45 * _gain_ro_impact(state, _node_device(node) or "")
    elif metric in {"dc_gain", "gain"} and node.endswith(".gm"):
        factor = 0.55 + 0.35 * _gain_gm_impact(state, _node_device(node) or "")
    elif metric in {"unity_gain_bandwidth", "ugbw", "bandwidth"} and node.endswith(".gm"):
        factor = 0.75
    elif metric in {"unity_gain_bandwidth", "ugbw", "bandwidth", "phase_margin"} and node.endswith(".capacitance"):
        factor = 0.50 + 0.45 * _capacitance_impact(state, _node_device(node) or "")
    elif metric in {"slew_rate", "slew_rate_pos", "slew_rate_neg", "power"} and node.endswith(".bias_current"):
        factor = 0.80
    elif node in {"global.Cc", "global.Rz", "block.compensation_network"}:
        factor = 0.88
    return round(min(1.0, path_score * factor), 4)


def _gain_ro_impact(state: DesignState, dev_id: str) -> float:
    if not dev_id or dev_id not in state.transistors:
        return 0.0
    direct_share = _output_branch_gds_share(state, dev_id)
    if direct_share > 0.0:
        return min(1.0, 0.35 + 0.95 * direct_share)
    peer_share = max((_output_branch_gds_share(state, peer) for peer in _symmetry_peer_devices(state, dev_id, "L")), default=0.0)
    if peer_share > 0.0:
        return min(0.95, 0.25 + 0.80 * peer_share)
    role = _role_for_device(state, dev_id)
    if _is_direct_gain_ro_role(role):
        return 0.62
    return 0.20 if "input_pair" in role else 0.0


def _gain_gm_impact(state: DesignState, dev_id: str) -> float:
    role = _role_for_device(state, dev_id)
    if "input_pair" in role:
        return 0.82
    if "second_stage" in role or "latch" in role or "source_follower" in role:
        return 0.66
    if "current_mirror_load" in role:
        return 0.38
    return 0.30


def _headroom_impact(state: DesignState, dev_id: str) -> float:
    ts = state.transistors.get(dev_id)
    if ts is None:
        return 0.0
    p = ts.parameters
    margin = p.vds - p.vdsat - _required_margin(state, dev_id)
    if margin < 0.0:
        return min(1.0, 0.70 + abs(margin) / 0.15)
    return max(0.0, min(0.65, (0.12 - margin) / 0.12))


def _capacitance_impact(state: DesignState, dev_id: str) -> float:
    caps = [_device_capacitance(ts.parameters) for ts in state.transistors.values()]
    total = sum(caps)
    if total <= 0.0 or dev_id not in state.transistors:
        return 0.25
    cap = _device_capacitance(state.transistors[dev_id].parameters)
    peer_cap = max((_device_capacitance(state.transistors[peer].parameters) for peer in _symmetry_peer_devices(state, dev_id, "L") if peer in state.transistors), default=cap)
    return min(1.0, 0.25 + max(cap, peer_cap) / total)


def _bias_current_impact(state: DesignState, dev_id: str) -> float:
    currents = [abs(float(ts.parameters.id or 0.0)) for ts in state.transistors.values()]
    total = sum(currents)
    if total <= 0.0 or dev_id not in state.transistors:
        return 0.35
    return min(1.0, 0.25 + abs(float(state.transistors[dev_id].parameters.id or 0.0)) / total)


def _global_intervention_impact(state: DesignState, node: str, metric: str, gap: float) -> float:
    name = node.split(".", 1)[1]
    if name in {"Cc", "CL", "Cload"}:
        if metric == "phase_margin":
            return min(1.0, 0.78 + 0.2 * gap)
        if metric in {"unity_gain_bandwidth", "ugbw", "bandwidth", "slew_rate", "slew_rate_pos", "slew_rate_neg"}:
            return min(1.0, 0.70 + 0.2 * gap)
    if name == "Rz" and metric == "phase_margin":
        return 0.82
    if name.startswith("I_") and metric in {"power", "slew_rate", "slew_rate_pos", "slew_rate_neg", "unity_gain_bandwidth"}:
        return 0.72
    return 0.25


def _output_branch_gds_share(state: DesignState, dev_id: str) -> float:
    output_nets = _port_nets(state, "output")
    branch_devices = []
    for dev in state.topology.devices:
        if any(net in output_nets for terminal, net in (dev.connections or {}).items() if terminal in {"drain", "source"}):
            if not _is_bias_role(dev.role or "") and dev.id in state.transistors:
                branch_devices.append(dev.id)
    total_gds = sum(max(float(state.transistors[item].parameters.gds or 0.0), 0.0) for item in branch_devices)
    if total_gds <= 0.0 or dev_id not in branch_devices:
        return 0.0
    return max(float(state.transistors[dev_id].parameters.gds or 0.0), 0.0) / total_gds


def _signal_path_device_factor(state: DesignState, dev_id: str) -> float:
    role = _role_for_device(state, dev_id)
    if _output_branch_gds_share(state, dev_id) > 0.0:
        return 1.0
    if "input_pair" in role or _is_direct_gain_ro_role(role):
        return 0.75
    if _is_bias_role(role):
        return 0.35
    return 0.45


def _target_priority_weight(state: DesignState, metric: str) -> float:
    target = state.targets.get(metric)
    if target is None:
        return 1.0
    try:
        priority = int(target.priority)
    except (TypeError, ValueError):
        return 1.0
    if priority <= 1:
        return 1.0
    return max(0.35, 1.0 / (1.0 + 0.12 * (priority - 1)))


def _causal_evidence_text(
    state: DesignState,
    node: str,
    metric: str,
    structural: float,
    intervention: float,
    propagation: float,
) -> str:
    role = _role_for_cause_node(state, node)
    return (
        f"{node} affects {metric} through structural path score={structural:.2f}, "
        f"intervention approximation={intervention:.2f}, propagation contribution={propagation:.2f}; "
        f"role={role or 'global/block'}."
    )


def _node_support(state: DesignState, node: str) -> str:
    dev_id = _node_device(node)
    if dev_id:
        return _device_support(state, dev_id)
    if node.startswith("global."):
        name = node.split(".", 1)[1]
        return f"{name}={state.global_parameters.get(name)}"
    if node == "block.compensation_network":
        return _compensation_support(state)
    return node


def _component_for_node(node: str) -> str:
    parts = node.split(".")
    if len(parts) >= 2 and parts[0] == "device":
        return f"device.{parts[1]}"
    if len(parts) >= 2 and parts[0] in {"block", "global"}:
        return ".".join(parts[:2])
    return node


def _node_device(node: str) -> str | None:
    parts = node.split(".")
    if len(parts) >= 3 and parts[0] == "device":
        return parts[1]
    return None


def _role_for_device(state: DesignState, dev_id: str) -> str:
    dev = state.get_device_def(dev_id)
    return (dev.role or "").lower() if dev else ""


def _symmetry_peer_devices(state: DesignState, dev_id: str, variable: str) -> list[str]:
    label = None
    for dv in state.design_variables:
        if dv.device == dev_id and dv.variable == variable:
            label = dv.symmetry_label
            break
    if not label:
        return [dev_id]
    peers = [dv.device for dv in state.design_variables if dv.variable == variable and dv.symmetry_label == label and dv.device]
    return peers or [dev_id]


def _shortest_nonempty_path(paths: list[list[str]]) -> list[str]:
    candidates = [path for path in paths if path]
    if not candidates:
        return []
    return sorted(candidates, key=len)[0]


def _structural_reason_text(node: str, path: list[str]) -> str:
    if not path:
        return f"{node} has no validated structural path in the dependency graph."
    return f"{node} lies on a directed structural dependency path to {path[-1]}."


def _propagation_reason_text(metrics: tuple[str, ...], path: list[str]) -> str:
    metric_text = ", ".join(metrics)
    if any("output_resistance" in item for item in path):
        return f"Errors propagate through the small-signal output-resistance term into {metric_text}."
    if any("input_transconductance" in item for item in path):
        return f"Errors propagate through useful transconductance into {metric_text}."
    if any("pole" in item or "zero" in item for item in path):
        return f"Errors propagate through pole/zero movement into {metric_text}."
    if any("bias" in item or "headroom" in item for item in path):
        return f"Errors propagate through bias/headroom coupling into {metric_text}."
    return f"Errors propagate along the directed dependency chain into {metric_text}."


def _spec_impact_text(node: str, metrics: tuple[str, ...]) -> str:
    action, effects = _counterfactual_for_node(node)
    return f"Approximate intervention: {action}; expected metric movement: {effects}; targeted metrics: {list(metrics)}."


def _legacy_fallback_causes(state: DesignState, legacy: list[dict[str, Any]]) -> list[CandidateCause]:
    out = []
    for item in legacy[:5]:
        node = item["node"]
        out.append(
            CandidateCause(
                node=node,
                score=float(item.get("score", 0.0) or 0.0),
                metrics=tuple(item.get("metrics", [])),
                evidence=tuple(item.get("evidence", [])),
                simulation_support=str(item.get("simulation_support", "")),
                component=_component_for_node(node),
                score_components={"legacy_sensitivity_fallback": float(item.get("score", 0.0) or 0.0)},
                structural_reason="No directed graph path was available; this is a fallback diagnostic.",
                intervention_test=_counterfactual_for_node(node)[0],
                sensitivity_prior=float(item.get("score", 0.0) or 0.0),
            )
        )
    return out


def _legacy_sensitivity_ranking(state: DesignState, symptoms: list[dict[str, Any]], capabilities) -> list[dict[str, Any]]:
    failed_metrics = tuple(symptom["metric"] for symptom in symptoms if symptom["status"] == "fail")
    if not failed_metrics:
        return []

    candidates: dict[str, dict[str, Any]] = {}

    def add(node: str, metric: str, base: float, evidence: str, support: str) -> None:
        item = candidates.setdefault(node, {"score": 0.0, "metrics": set(), "evidence": [], "support": []})
        item["score"] += base
        item["metrics"].add(metric)
        item["evidence"].append(evidence)
        item["support"].append(support)

    weakest_headroom = _weakest_headroom_device(state)
    lowest_input_gm = _lowest_role_param(state, "input_pair", "gm")
    lowest_output_ro = _lowest_output_resistance_device(state)
    largest_cap = _largest_capacitance_device(state)
    tail_current = _lowest_role_param(state, "tail_current_source", "id")
    regulator = _first_role_device(state, "regulated_source_current_source")

    for metric in failed_metrics:
        if metric in {"dc_gain", "gain"}:
            if lowest_output_ro:
                add(f"device.{lowest_output_ro}.ro", metric, 0.78, "Legacy sensitivity proxy: output/load resistance is a direct gain factor.", _device_support(state, lowest_output_ro))
            if lowest_input_gm:
                add(f"device.{lowest_input_gm}.gm", metric, 0.58, "Legacy sensitivity proxy: input-pair gm is the forward gain source.", _device_support(state, lowest_input_gm))
            if weakest_headroom:
                add(f"device.{weakest_headroom}.headroom", metric, 0.50, "Legacy sensitivity proxy: headroom can lower gain indirectly.", _device_support(state, weakest_headroom))
            if capabilities.has("source_follower_regulation") and regulator:
                add(f"device.{regulator}.gm", metric, 0.44, "Legacy sensitivity proxy: source-follower local feedback gm boosts effective output resistance.", _device_support(state, regulator))
        elif metric in {"unity_gain_bandwidth", "ugbw", "bandwidth"}:
            if lowest_input_gm:
                add(f"device.{lowest_input_gm}.gm", metric, 0.82, "Legacy sensitivity proxy: UGBW scales with input gm over dominant capacitance.", _device_support(state, lowest_input_gm))
            if largest_cap:
                add(f"device.{largest_cap}.capacitance", metric, 0.70, "Legacy sensitivity proxy: large capacitance lowers pole frequencies.", _device_support(state, largest_cap))
            if "Cc" in state.global_parameters:
                add("global.Cc", metric, 0.62, "Legacy sensitivity proxy: Cc trades bandwidth for phase margin.", f"Cc={state.global_parameters['Cc']:.4e}")
        elif metric == "phase_margin":
            if largest_cap:
                add(f"device.{largest_cap}.capacitance", metric, 0.76, "Legacy sensitivity proxy: parasitic capacitance can pull a non-dominant pole toward unity gain.", _device_support(state, largest_cap))
            if capabilities.has("miller_rc_compensation"):
                add("block.compensation_network", metric, 0.90, "Legacy sensitivity proxy: Cc/Rz placement controls dominant pole and zero.", _compensation_support(state))
            if capabilities.has("source_follower_regulation") and regulator:
                add(f"device.{regulator}.gm", metric, 0.58, "Legacy sensitivity proxy: source-follower loop gm changes local-feedback pole placement.", _device_support(state, regulator))
        elif metric in {"slew_rate", "slew_rate_pos", "slew_rate_neg"}:
            if tail_current:
                add(f"device.{tail_current}.bias_current", metric, 0.86, "Legacy sensitivity proxy: slew rate is current divided by capacitance.", _device_support(state, tail_current))
            if "Cc" in state.global_parameters:
                add("global.Cc", metric, 0.75, "Legacy sensitivity proxy: larger Cc reduces slew rate for fixed current.", f"Cc={state.global_parameters['Cc']:.4e}")
            if largest_cap:
                add(f"device.{largest_cap}.capacitance", metric, 0.55, "Legacy sensitivity proxy: large capacitance increases charge demand.", _device_support(state, largest_cap))
        elif metric in HEADROOM_METRICS:
            if weakest_headroom:
                add(f"device.{weakest_headroom}.headroom", metric, 0.90, "Legacy sensitivity proxy: swing/common-mode range is limited by stack headroom.", _device_support(state, weakest_headroom))
        elif metric in {"delay", "decision_time", "propagation_delay", "regeneration_time"}:
            latch = _first_role_contains(state, "latch")
            if latch:
                add(f"device.{latch}.gm", metric, 0.90, "Legacy sensitivity proxy: comparator delay is dominated by latch gm over capacitance.", _device_support(state, latch))
            if largest_cap:
                add(f"device.{largest_cap}.capacitance", metric, 0.72, "Legacy sensitivity proxy: capacitance slows regeneration.", _device_support(state, largest_cap))
        elif metric in {"power", "energy", "energy_per_comparison", "pdp", "edp"}:
            if tail_current:
                add(f"device.{tail_current}.bias_current", metric, 0.70, "Legacy sensitivity proxy: current drives power.", _device_support(state, tail_current))
            if largest_cap:
                add(f"device.{largest_cap}.capacitance", metric, 0.55, "Legacy sensitivity proxy: capacitance contributes dynamic energy.", _device_support(state, largest_cap))

    max_score = max((item["score"] for item in candidates.values()), default=1.0)
    out = []
    for node, item in candidates.items():
        out.append(
            {
                "node": node,
                "score": round(min(item["score"] / max(max_score, 1e-30), 1.0), 4),
                "metrics": sorted(item["metrics"]),
                "evidence": _dedupe_text(item["evidence"]),
                "simulation_support": "; ".join(_dedupe_text(item["support"][:3])),
            }
        )
    return sorted(out, key=lambda item: item["score"], reverse=True)


def _sensitivity_ranking_comparison(
    state: DesignState,
    symptoms: list[dict[str, Any]],
    causes: list[CandidateCause],
) -> dict[str, Any]:
    capabilities = detect_circuit_capabilities(state)
    legacy = _legacy_sensitivity_ranking(state, symptoms, capabilities)
    causal_top = [cause.node for cause in causes[:5]]
    legacy_top = [item["node"] for item in legacy[:5]]
    overlap = [node for node in causal_top if node in legacy_top]
    divergences = []
    for node in causal_top:
        if node not in legacy_top:
            divergences.append(
                {
                    "node": node,
                    "causal_reason": "Selected by directed structural path plus intervention/propagation score.",
                    "legacy_position": None,
                }
            )
    for index, node in enumerate(legacy_top, start=1):
        if node not in causal_top:
            divergences.append(
                {
                    "node": node,
                    "legacy_rank": index,
                    "causal_reason": "Legacy sensitivity proxy ranked it, but graph intervention score did not place it in the top causal set.",
                }
            )
    return {
        "purpose": "Debugging hook only: compare causal graph ranking against the legacy sensitivity-style heuristic.",
        "decision_rule": "causal_graph_ranking",
        "legacy_role": "weak_prior_and_divergence_check",
        "causal_top": causal_top,
        "legacy_sensitivity_top": legacy_top,
        "top5_overlap": overlap,
        "top5_overlap_count": len(overlap),
        "divergences": divergences[:8],
    }


def _agent_failure_attribution(
    state: DesignState,
    symptoms: list[dict[str, Any]],
    causes: list[CandidateCause],
    paths: list[dict[str, Any]],
    tuning: dict[str, Any],
) -> dict[str, Any]:
    by_failure = []
    for symptom in symptoms:
        metric = symptom["metric"]
        metric_causes = [cause for cause in causes if metric in cause.metrics]
        metric_paths = [path for path in paths if path["metric"] == metric]
        tuning_items = [
            item for item in tuning.get("by_failure", [])
            if item.get("metric") == metric
        ]
        top = metric_causes[:3]
        by_failure.append(
            {
                "metric": metric,
                "status": symptom["status"],
                "observed_deviation": {
                    "value": symptom.get("value"),
                    "min": symptom.get("min"),
                    "max": symptom.get("max"),
                    "direction": symptom.get("direction"),
                    "deviation_abs": symptom.get("deviation_abs"),
                    "deviation_rel": symptom.get("deviation_rel"),
                },
                "minimal_causal_factor_set": [cause.node for cause in top],
                "primary_attribution": _primary_attribution_text(state, metric, top[0]) if top else "No ranked cause is available from the current schema state.",
                "ranked_hypotheses": [
                    {
                        "node": cause.node,
                        "score": cause.score,
                        "component": cause.component,
                        "score_components": cause.score_components,
                        "structural_reason": cause.structural_reason,
                        "propagation_path": list(cause.propagation_path),
                        "propagation_reason": cause.propagation_reason,
                        "spec_impact": cause.spec_impact,
                        "evidence": list(cause.evidence),
                        "simulation_support": cause.simulation_support,
                    }
                    for cause in top
                ],
                "strongest_path": metric_paths[0]["chain"] if metric_paths else [],
                "tuning_plan": tuning_items[0].get("actions", []) if tuning_items else [],
                "next_step": "Apply the tuning plan to the schema variables, rerun OP, then rerun AC/performance extraction.",
            }
        )
    return {
        "author": "analog_circuit_causal_diagnostic_agent",
        "state_source": "design_state.yaml:diagnostics.causal_diagnostics",
        "principle": "Ranked causal hypotheses and derived tuning actions are written into schema state; JSON artifacts are derived views only.",
        "by_failure": by_failure,
    }


def _primary_attribution_text(state: DesignState, metric: str, cause: CandidateCause) -> str:
    role = _role_for_cause_node(state, cause.node)
    dev_id = _node_device(cause.node) or ""
    if metric in {"dc_gain", "gain"} and cause.node.endswith(".ro") and _is_gain_path_ro_cause(state, dev_id, role):
        return "The leading hypothesis is low signal-path output resistance on a structurally relevant gain path, which directly reduces low-frequency gain."
    if metric in {"dc_gain", "gain"} and cause.node.endswith(".gm") and "input_pair" in role:
        return "The leading hypothesis is insufficient input-pair transconductance, which limits forward gain and gain-bandwidth."
    if cause.node.endswith(".headroom") or _is_bias_role(role):
        return "The leading hypothesis is insufficient operating-point headroom, which can indirectly reduce gain or swing by weakening saturation margins."
    if metric == "phase_margin" and cause.node in {"block.compensation_network", "global.Cc", "global.Rz"}:
        return "The leading hypothesis is compensation placement: the dominant pole and zero should be verified by sweeping Cc and Rz around the analytical target."
    if metric == "phase_margin":
        return "The leading hypothesis is a non-dominant pole moving too close to unity gain."
    return f"The leading hypothesis is {cause.node}; validate it with the proposed SPICE perturbation before accepting it."


def _attribution_guided_tuning(
    state: DesignState,
    symptoms: list[dict[str, Any]],
    causes: list[CandidateCause],
) -> dict[str, Any]:
    by_failure = []
    failed_metrics = {symptom["metric"] for symptom in symptoms if symptom["status"] == "fail"}
    planning_mode = _coarse_fine_mode(symptoms)
    for symptom in symptoms:
        if symptom["status"] != "fail":
            continue
        metric = symptom["metric"]
        actions = []
        for cause in _select_tuning_causes(causes, metric):
            actions.extend(_tuning_actions_for_cause(state, metric, cause))
        actions = [_apply_multi_objective_guardrail(action, failed_metrics) for action in actions]
        actions = _dedupe_actions(actions)
        actions = [_apply_agent_step(action, symptom, planning_mode) for action in actions]
        actions = _rank_tuning_actions(actions)
        by_failure.append(
            {
                "metric": metric,
                "observed_direction": symptom.get("direction"),
                "target_gap": {
                    "value": symptom.get("value"),
                    "min": symptom.get("min"),
                    "max": symptom.get("max"),
                    "deviation_abs": symptom.get("deviation_abs"),
                    "deviation_rel": symptom.get("deviation_rel"),
                },
                "strategy": _tuning_strategy_text(metric),
                "actions": actions,
            }
        )
    return {
        "author": "analog_circuit_causal_diagnostic_agent",
        "principle": "Translate ranked root causes into schema-safe combo actions for a constrained coarse-to-fine optimizer.",
        "planning_mode": planning_mode,
        "hard_physical_gate": {
            "principle": "Schema actions must pass write-policy, symmetry, range, OP, and layout-realization validation before SPICE.",
            "executor": "diagnostics.tuning.apply_attribution_guided_tuning",
        },
        "by_failure": by_failure,
    }


def _select_tuning_causes(
    causes: list[CandidateCause],
    metric: str,
    *,
    max_causes: int = 6,
) -> list[CandidateCause]:
    matching = [item for item in causes if metric in item.metrics]
    if not matching:
        return []

    selected: list[CandidateCause] = []
    seen_nodes: set[str] = set()
    seen_components: set[str] = set()

    def add(cause: CandidateCause, *, allow_component_repeat: bool = False) -> None:
        if len(selected) >= max_causes or cause.node in seen_nodes:
            return
        component = cause.component or _component_for_node(cause.node)
        if component in seen_components and not allow_component_repeat:
            return
        selected.append(cause)
        seen_nodes.add(cause.node)
        if component:
            seen_components.add(component)

    for cause in matching:
        add(cause)

    if metric in {"dc_gain", "gain"}:
        required_classes = (
            lambda c: c.node.endswith(".ro"),
            lambda c: c.node.endswith(".gm"),
            lambda c: "gain_propagation_dependency" in c.edge_types,
            lambda c: "bias_dependency" in c.edge_types,
        )
        for predicate in required_classes:
            if any(predicate(item) for item in selected):
                continue
            candidate = next((item for item in matching if predicate(item)), None)
            if candidate is not None:
                add(candidate, allow_component_repeat=True)

    for cause in matching:
        add(cause, allow_component_repeat=True)
        if len(selected) >= max_causes:
            break
    return selected


def _tuning_actions_for_cause(state: DesignState, metric: str, cause: CandidateCause) -> list[dict[str, Any]]:
    node = cause.node
    if node == "block.compensation_network":
        return _compensation_tuning_actions(state, metric, node, cause.score)
    if node.startswith("global."):
        return _global_tuning_action(state, metric, node, cause.score)

    parts = node.split(".")
    if len(parts) < 3 or parts[0] != "device":
        return []

    dev_id = parts[1]
    param = parts[2]
    role = _role_for_cause_node(state, node)

    if param == "ro":
        if _is_gain_path_ro_cause(state, dev_id, role):
            actions = [
                _knob_action(
                    state,
                    metric=metric,
                    cause_node=node,
                    score=cause.score,
                    device=dev_id,
                    variable="L",
                    direction="increase",
                    step_hint="increase by 10-25%; if already at the upper bound, expand the L upper bound before re-optimizing",
                    rationale="Increasing channel length raises signal-path ro and therefore DC gain.",
                    expected_effect={"dc_gain": "increase", "unity_gain_bandwidth": "may decrease", "phase_margin": "watch for lower pole frequency"},
                    tradeoffs=["Higher L can add capacitance and lower bandwidth.", "Keep mirrored load devices symmetric."],
                    priority="primary",
                )
            ]
            actions.extend(_companion_gain_ro_actions(state, metric, node, dev_id, cause.score))
            return actions + _bias_voltage_tuning_actions(
                state,
                metric=metric,
                cause_node=node,
                score=cause.score * 0.55,
                priority="secondary",
            )
        return _headroom_tuning_actions(state, metric, dev_id, node, cause.score)

    if param == "gm":
        return _gm_tuning_actions(state, metric, dev_id, role, node, cause.score)

    if param in {"headroom", "Vov"}:
        return _headroom_tuning_actions(state, metric, dev_id, node, cause.score)

    if param == "capacitance":
        return _capacitance_tuning_actions(state, metric, dev_id, node, cause.score)

    if param == "bias_current":
        return _bias_current_tuning_actions(state, metric, dev_id, role, node, cause.score)

    return []


def _companion_gain_ro_actions(
    state: DesignState,
    metric: str,
    cause_node: str,
    primary_device: str,
    score: float,
) -> list[dict[str, Any]]:
    if metric not in {"dc_gain", "gain"}:
        return []
    actions: list[dict[str, Any]] = []
    seen = set(_symmetry_peer_devices(state, primary_device, "L"))
    ranked_devices: list[tuple[float, str, str]] = []
    for dev in state.topology.devices:
        role = (dev.role or "").lower()
        if dev.id in seen or not _primary_design_variable(state, dev.id, "L"):
            continue
        if not _is_direct_gain_ro_role(role) and "cascode" not in role and "input_pair" not in role:
            continue
        role_score = 0.45
        if "second_stage_gain" in role:
            role_score = 0.92
        elif "current_mirror_load" in role or "active_load" in role:
            role_score = 0.86
        elif "cascode" in role:
            role_score = 0.82
        elif "output_current_source" in role or "second_stage_load" in role:
            role_score = 0.74
        elif "input_pair" in role:
            role_score = 0.56
        ranked_devices.append((role_score, dev.id, role))
    for role_score, dev_id, role in sorted(ranked_devices, reverse=True)[:4]:
        actions.append(
            _knob_action(
                state,
                metric=metric,
                cause_node=f"{cause_node}|companion_ro:{dev_id}",
                score=score * role_score,
                device=dev_id,
                variable="L",
                direction="increase",
                step_hint="probe a companion signal-path L increase because gain is a product of multiple Rout terms",
                rationale=(
                    f"Companion Rout probe: {dev_id} role={role} can limit the same gain path, "
                    "so local SPICE evidence should compare this L move with the originally ranked device."
                ),
                expected_effect={"dc_gain": "increase", "unity_gain_bandwidth": "may decrease", "output_swing": "verify"},
                tradeoffs=["Higher L can lower speed or output swing; action remains evidence-gated."],
                priority="primary",
            )
        )
    return actions


def _apply_multi_objective_guardrail(action: dict[str, Any], failed_metrics: set[str]) -> dict[str, Any]:
    metric = str(action.get("metric", ""))
    variable = str(action.get("knob", "")).split(".")[-1]
    direction = str(action.get("direction", ""))
    gain_failed = bool({"dc_gain", "gain"} & failed_metrics)
    bandwidth_failed = bool({"unity_gain_bandwidth", "ugbw", "bandwidth"} & failed_metrics)
    speed_failed = bool({"unity_gain_bandwidth", "ugbw", "bandwidth", "slew_rate", "slew_rate_pos", "slew_rate_neg"} & failed_metrics)
    if gain_failed and not speed_failed and metric in {"dc_gain", "gain"} and variable == "L" and direction == "increase":
        focused = dict(action)
        focused["priority"] = "primary"
        focused["min_step_fraction"] = max(float(focused.get("min_step_fraction", 0.0) or 0.0), 0.14)
        focused["max_step_fraction"] = max(float(focused.get("max_step_fraction", 0.0) or 0.0), 0.30)
        focused["auto_range_expansion_allowed"] = True
        focused["step_hint"] = "increase by 14-30% because gain is the isolated failing speed-independent metric; expand L range only after schema validation"
        focused["rationale"] = (
            str(focused.get("rationale", ""))
            + " Gain-only policy: speed and slew targets are not failing, so signal-path ro improvement should be a primary move."
        ).strip()
        focused["gain_only_ro_policy"] = {
            "reason": "dc_gain fails while bandwidth and slew-rate are not failing",
            "policy": "prioritize signal-path L/Rout increase under the physical gate",
        }
        return focused
    if not (gain_failed and bandwidth_failed and variable == "L"):
        return action

    guarded = dict(action)
    if metric in {"dc_gain", "gain"} and direction == "increase":
        guarded["priority"] = "secondary"
        guarded["max_step_fraction"] = 0.10
        guarded["auto_range_expansion_allowed"] = False
        guarded["step_hint"] = "increase by at most 5-10% while UGBW is also failing; do not expand the L upper bound in the same round"
        guarded["rationale"] = (
            str(guarded.get("rationale", ""))
            + " Multi-objective guardrail: bandwidth is also below target, so length increase is a small secondary move rather than a primary decision."
        ).strip()
        guarded.setdefault("tradeoffs", [])
        guarded["tradeoffs"] = list(guarded["tradeoffs"]) + [
            "UGBW is also failing, so excessive L increase can collapse bandwidth."
        ]
        guarded["multi_objective_guardrail"] = {
            "reason": "dc_gain and unity_gain_bandwidth fail together",
            "policy": "small secondary L increase only; prefer gm/current actions first",
        }
    elif metric in {"unity_gain_bandwidth", "ugbw", "bandwidth"} and direction == "decrease":
        guarded["priority"] = "secondary"
        guarded["max_step_fraction"] = 0.08
        guarded["auto_range_expansion_allowed"] = False
        guarded["step_hint"] = "decrease by at most 5-8% while gain is also failing; verify gain does not regress"
        guarded["rationale"] = (
            str(guarded.get("rationale", ""))
            + " Multi-objective guardrail: gain is also below target, so length decrease is a small secondary bandwidth move."
        ).strip()
        guarded.setdefault("tradeoffs", [])
        guarded["tradeoffs"] = list(guarded["tradeoffs"]) + [
            "DC gain is also failing, so excessive L decrease can reduce ro and worsen gain."
        ]
        guarded["multi_objective_guardrail"] = {
            "reason": "dc_gain and unity_gain_bandwidth fail together",
            "policy": "small secondary L decrease only; prefer gm/current actions first",
        }
    return guarded


def _gm_tuning_actions(state: DesignState, metric: str, dev_id: str, role: str, cause_node: str, score: float) -> list[dict[str, Any]]:
    actions = [
        _knob_action(
            state,
            metric=metric,
            cause_node=cause_node,
            score=score,
            device=dev_id,
            variable="gm_id",
            direction="increase",
            step_hint="increase by 5-15% within the allowed gm/ID range",
            rationale="Higher gm/ID increases useful transconductance for the same bias current and usually reduces VDSAT.",
            expected_effect={"dc_gain": "increase", "unity_gain_bandwidth": "increase", "headroom": "improve"},
            tradeoffs=["Larger device width can increase parasitic capacitance.", "Check phase margin after the OP is repaired."],
            priority="primary",
        )
    ]
    current_name = _current_variable_for_role(role)
    if current_name:
        actions.append(
            _knob_action(
                state,
                metric=metric,
                cause_node=cause_node,
                score=score * 0.72,
                device="",
                variable=current_name,
                direction="increase",
                step_hint="increase by 5-10% only when the power target still has margin",
                rationale="Increasing bias current raises gm directly when the gm/ID choice alone is not enough.",
                expected_effect={"gm": "increase", "unity_gain_bandwidth": "increase", "power": "increase"},
                tradeoffs=["Power rises directly.", "Extra current can worsen headroom in stacked devices."],
                priority="secondary",
            )
        )
    return actions


def _headroom_tuning_actions(state: DesignState, metric: str, dev_id: str, cause_node: str, score: float) -> list[dict[str, Any]]:
    role = _role_for_cause_node(state, cause_node)
    actions = [
        _knob_action(
            state,
            metric=metric,
            cause_node=cause_node,
            score=score,
            device=dev_id,
            variable="gm_id",
            direction="increase",
            step_hint="increase by 5-15% to reduce required VDSAT/Vov",
            rationale="Higher gm/ID lowers overdrive for the same current, recovering saturation headroom.",
            expected_effect={"headroom": "increase", "output_swing": "increase", "dc_gain": "increase if saturation margin improves"},
            tradeoffs=["Device width and capacitance can increase.", "Do not push into excessive weak inversion if speed is already marginal."],
            priority="primary",
        )
    ]
    current_name = _current_variable_for_role(role)
    if current_name:
        actions.append(
            _knob_action(
                state,
                metric=metric,
                cause_node=cause_node,
                score=score * 0.65,
                device="",
                variable=current_name,
                direction="decrease",
                step_hint="decrease by 5-10% only if gm, bandwidth, and slew-rate margins allow it",
                rationale="Reducing current lowers required overdrive and can recover headroom in the limiting stack.",
                expected_effect={"headroom": "increase", "power": "decrease", "gm": "decrease"},
                tradeoffs=["Can reduce gain-bandwidth and slew rate.", "Use after gm/ID and length actions if gain is also failing."],
                priority="guarded",
            )
        )
    actions.extend(
        _bias_voltage_tuning_actions(
            state,
            metric=metric,
            cause_node=cause_node,
            score=score * 0.95,
            priority="primary",
        )
    )
    return actions


def _capacitance_tuning_actions(state: DesignState, metric: str, dev_id: str, cause_node: str, score: float) -> list[dict[str, Any]]:
    return [
        _knob_action(
            state,
            metric=metric,
            cause_node=cause_node,
            score=score,
            device=dev_id,
            variable="L",
            direction="decrease",
            step_hint="decrease by 5-15% if gain and matching constraints still pass",
            rationale="Reducing channel length can lower parasitic capacitance and push a non-dominant pole upward.",
            expected_effect={"phase_margin": "increase when the non-dominant pole moves higher", "unity_gain_bandwidth": "increase", "dc_gain": "may decrease"},
            tradeoffs=["Lower L reduces ro and can hurt gain.", "Keep symmetric devices matched."],
            priority="primary",
        )
    ]


def _bias_current_tuning_actions(state: DesignState, metric: str, dev_id: str, role: str, cause_node: str, score: float) -> list[dict[str, Any]]:
    current_name = _current_variable_for_role(role) or "I_tail"
    direction = "increase"
    expected = {"slew_rate": "increase", "unity_gain_bandwidth": "may increase", "power": "increase"}
    rationale = "Increasing available bias current raises large-signal charging current."
    tradeoffs = ["Power rises directly.", "Headroom must be rechecked after the OP update."]
    if metric in {"power", "energy", "energy_per_comparison", "pdp", "edp"}:
        direction = "decrease"
        expected = {"power": "decrease", "energy": "decrease", "speed": "may decrease"}
        rationale = "Reducing bias current lowers static and dynamic energy pressure."
        tradeoffs = ["Delay, slew rate, and regeneration speed can worsen."]
    return [
        _knob_action(
            state,
            metric=metric,
            cause_node=cause_node,
            score=score,
            device="",
            variable=current_name,
            direction=direction,
            step_hint=f"{direction} by 5-15% within the schema range",
            rationale=rationale,
            expected_effect=expected,
            tradeoffs=tradeoffs,
            priority="primary",
        )
    ]


def _compensation_tuning_actions(state: DesignState, metric: str, cause_node: str, score: float) -> list[dict[str, Any]]:
    actions = []
    if metric == "phase_margin":
        actions.append(
            _knob_action(
                state,
                metric=metric,
                cause_node=cause_node,
                score=score,
                device="",
                variable="Cc",
                direction="increase",
                step_hint="increase by 10-25% to pull the dominant pole lower",
                rationale="For a two-stage OTA with low PM, the main pole should be pulled to lower frequency before fine zero placement.",
                expected_effect={"phase_margin": "increase", "unity_gain_bandwidth": "decrease", "slew_rate": "decrease"},
                tradeoffs=["Bandwidth and slew rate fall as Cc grows."],
                priority="primary",
            )
        )
        actions.append(
            _knob_action(
                state,
                metric=metric,
                cause_node=cause_node,
                score=score * 0.92,
                device="",
                variable="Rz",
                direction="set",
                step_hint="set Rz to 1/gm(second_stage_gain), then let optimization fine tune around that value",
                rationale="The compensation zero should be placed from the second-stage gm relation requested by the architecture rule.",
                expected_effect={"phase_margin": "increase when the zero placement was limiting"},
                tradeoffs=["Wrong zero placement can reduce PM even when Cc is adequate."],
                priority="primary",
                target_formula="1/gm(second_stage_gain)",
                target_value=_rz_target_from_second_stage(state),
            )
        )
    elif metric in {"unity_gain_bandwidth", "ugbw", "bandwidth", "slew_rate", "slew_rate_pos", "slew_rate_neg"}:
        actions.append(
            _knob_action(
                state,
                metric=metric,
                cause_node=cause_node,
                score=score,
                device="",
                variable="Cc",
                direction="decrease",
                step_hint="decrease by 5-15% only if phase-margin target remains satisfied",
                rationale="Lower Cc improves bandwidth and slew rate when stability has enough margin.",
                expected_effect={"unity_gain_bandwidth": "increase", "slew_rate": "increase", "phase_margin": "decrease"},
                tradeoffs=["Phase margin is the guardrail."],
                priority="guarded",
            )
        )
    return actions


def _global_tuning_action(state: DesignState, metric: str, node: str, score: float) -> list[dict[str, Any]]:
    name = node.split(".", 1)[1]
    if name == "Cc":
        direction = "increase" if metric == "phase_margin" else "decrease"
        return [
            _knob_action(
                state,
                metric=metric,
                cause_node=node,
                score=score,
                device="",
                variable="Cc",
                direction=direction,
                step_hint=f"{direction} Cc by 5-15% with phase margin as the guardrail",
                rationale="Cc directly trades stability against bandwidth and slew rate.",
                expected_effect={"phase_margin": "moves with Cc", "unity_gain_bandwidth": "moves opposite Cc", "slew_rate": "moves opposite Cc"},
                tradeoffs=["Do not tune Cc without rerunning PM."],
                priority="primary",
            )
        ]
    if name == "Rz":
        return [
            _knob_action(
                state,
                metric=metric,
                cause_node=node,
                score=score,
                device="",
                variable="Rz",
                direction="set",
                step_hint="set Rz to 1/gm(second_stage_gain)",
                rationale="Rz should align the compensation zero to the second-stage gm target.",
                expected_effect={"phase_margin": "increase when the zero was misplaced"},
                tradeoffs=["Rz is meaningful only with an explicit Miller RC compensation network."],
                priority="primary",
                target_formula="1/gm(second_stage_gain)",
                target_value=_rz_target_from_second_stage(state),
            )
        ]
    if _is_bias_voltage_variable(name):
        target = _bias_voltage_target_value(state, name)
        if target is None:
            return []
        return [
            _knob_action(
                state,
                metric=metric,
                cause_node=node,
                score=score,
                device="",
                variable=name,
                direction="set",
                step_hint="center a topology-guided bias search, then validate with the local SPICE intervention model",
                rationale="Bias voltage directly controls stack voltage allocation; the target is a topology-guided search anchor, not a schema preset.",
                expected_effect={
                    "dc_gain": "increase if saturation headroom improves",
                    "output_swing": "increase if stack headroom improves",
                    "headroom": "improve",
                },
                tradeoffs=["Can trade gain against bandwidth, slew rate, and current density; require SPICE evidence before applying."],
                priority="primary",
                target_formula=_bias_voltage_target_formula(state, name),
                target_value=target,
            )
        ]
    return []


def _bias_voltage_tuning_actions(
    state: DesignState,
    *,
    metric: str,
    cause_node: str,
    score: float,
    priority: str,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for variable in _global_bias_voltage_variables(state):
        target = _bias_voltage_target_value(state, variable)
        current = _current_variable_value(state, "", variable)
        if target is None or current is None:
            continue
        if abs(float(target) - float(current)) < 0.005:
            continue
        actions.append(
            _knob_action(
                state,
                metric=metric,
                cause_node=cause_node,
                score=score,
                device="",
                variable=variable,
                direction="set",
                step_hint="center a topology-guided bias search, then let the constrained optimizer verify the local SPICE delta",
                rationale="Explicit bias-voltage tuning brings OP/headroom repair into the constrained action optimizer instead of leaving it only to postprocess.",
                expected_effect={
                    "dc_gain": "increase if saturation headroom improves",
                    "unity_gain_bandwidth": "increase if transconductance bias improves",
                    "slew_rate": "increase if bias current remains adequate",
                    "output_swing": "increase if stack headroom improves",
                    "headroom": "improve",
                },
                tradeoffs=["Bias search anchors are topology dependent and may hurt another metric; local SPICE evidence is required for selection."],
                priority=priority,
                target_formula=_bias_voltage_target_formula(state, variable),
                target_value=target,
            )
        )
    actions.extend(_stack_balance_tuning_actions(state, metric=metric, cause_node=cause_node, score=score * 1.08))
    return actions


def _stack_balance_tuning_actions(
    state: DesignState,
    *,
    metric: str,
    cause_node: str,
    score: float,
) -> list[dict[str, Any]]:
    architecture = (state.topology.architecture or "").lower()
    if "telescopic" not in architecture:
        return []
    required = ("vbias_tail", "vbias_ncas", "vbias_pcas")
    if not all(_primary_design_variable(state, "", name) for name in required):
        return []
    candidate_quantiles = [
        {"vbias_tail": 0.03, "vbias_ncas": 0.90, "vbias_pcas": 0.05},
        {"vbias_tail": 0.08, "vbias_ncas": 0.90, "vbias_pcas": 0.05},
        {"vbias_tail": 0.03, "vbias_ncas": 1.00, "vbias_pcas": 0.05},
        {"vbias_tail": 0.16, "vbias_ncas": 0.90, "vbias_pcas": 0.14},
        {"vbias_tail": 0.08, "vbias_ncas": 0.82, "vbias_pcas": 0.14},
        {"vbias_tail": 0.16, "vbias_ncas": 0.82, "vbias_pcas": 0.28},
        {"vbias_tail": 0.32, "vbias_ncas": 0.70, "vbias_pcas": 0.28},
    ]
    actions: list[dict[str, Any]] = []
    current = {
        f"global.{name}": _current_variable_value(state, "", name)
        for name in required
    }
    for index, candidate in enumerate(candidate_quantiles, start=1):
        per_knob_values = {
            f"global.{name}": round(_range_quantile(state, name, quantile), 4)
            for name, quantile in candidate.items()
        }
        if all(
            current.get(knob) is not None and abs(float(current[knob]) - float(value)) < 0.005
            for knob, value in per_knob_values.items()
        ):
            continue
        actions.append(
            {
                "metric": metric,
                "cause_node": cause_node,
                "action_class": "telescopic_stack_balance",
                "score": round(float(score) * (1.0 - 0.04 * (index - 1)), 4),
                "priority": "primary",
                "knob": "global.vbias_tail",
                "apply_to": list(per_knob_values),
                "direction": "set",
                "current_value": current,
                "suggested_next_value": None,
                "per_knob_values": per_knob_values,
                "range": {
                    f"global.{name}": _variable_range(state, "", name)
                    for name in required
                },
                "limit_status": "topology_guided_bias_search",
                "step_hint": "set the telescopic tail, NMOS cascode, and PMOS cascode biases as one stack-balance candidate",
                "rationale": "The telescopic stack is strongly coupled; local evidence must evaluate the bias triple together instead of optimizing each bias port independently.",
                "expected_effect": {
                    "dc_gain": "increase if the stack recovers saturation headroom",
                    "unity_gain_bandwidth": "increase if gm and output resistance recover",
                    "phase_margin": "increase if the output pole is no longer collapsed by a bad OP",
                    "slew_rate": "increase if the output device bias recovers",
                    "output_swing": "increase if stack headroom improves",
                    "headroom": "improve",
                },
                "tradeoffs": [
                    "A stack-balance candidate can move several operating-point voltages at once; accept it only with optimizer evidence.",
                    "The best point is topology and seed dependent, so ngspice remains the authority.",
                ],
                "schema_variable_present": True,
                "auto_range_expansion_allowed": False,
                "target_formula": "telescopic_stack_balance_search",
            }
        )
    return actions


def _global_bias_voltage_variables(state: DesignState) -> list[str]:
    bias_ports = {port.id for port in state.topology.ports if (port.direction or "").lower() == "bias"}
    variables = [
        dv.variable
        for dv in state.design_variables
        if not dv.device and _is_bias_voltage_variable(dv.variable) and (not bias_ports or dv.variable in bias_ports)
    ]
    return list(dict.fromkeys(variables))


def _is_bias_voltage_variable(variable: str) -> bool:
    name = (variable or "").lower()
    return name == "vbias" or name.startswith("vbias_")


def _bias_voltage_target_formula(state: DesignState, variable: str) -> str:
    architecture = (state.topology.architecture or "").lower()
    if "telescopic" in architecture:
        return "telescopic_topology_guided_bias_search"
    if "folded" in architecture:
        return "folded_cascode_topology_guided_bias_search"
    return "single_stage_topology_guided_bias_search"


def _bias_voltage_target_value(state: DesignState, variable: str) -> float | None:
    architecture = (state.topology.architecture or "").lower()
    name = variable.lower()
    if "telescopic" in architecture:
        quantiles = {
            "vbias_tail": 0.03,
            "vbias_ncas": 0.90,
            "vbias_pcas": 0.05,
        }
    elif "folded" in architecture:
        quantiles = {
            "vbias_ptail": 0.56,
            "vbias_ncas": 0.66,
        }
    else:
        quantiles = {
            "vbias": 0.47,
            "vbias_tail": 0.46,
            "vbias_ncas": 0.55,
            "vbias_pcas": 0.48,
            "vbias_ptail": 0.56,
        }
    if name not in quantiles:
        return None
    return round(_range_quantile(state, variable, quantiles[name]), 4)


def _range_quantile(state: DesignState, variable: str, quantile: float) -> float:
    bounds = _variable_range(state, "", variable)
    if not bounds:
        vdd = float(state.simulation.supply.get("vdd", 1.2) or 1.2)
        return float(quantile) * vdd
    lo = float(bounds["min"])
    hi = float(bounds["max"])
    return _clip_to_bounds(lo + float(quantile) * (hi - lo), bounds)


def _knob_action(
    state: DesignState,
    *,
    metric: str,
    cause_node: str,
    score: float,
    device: str,
    variable: str,
    direction: str,
    step_hint: str,
    rationale: str,
    expected_effect: dict[str, str],
    tradeoffs: list[str],
    priority: str,
    target_formula: str | None = None,
    target_value: float | None = None,
) -> dict[str, Any]:
    role = _role_for_device(state, device) if device else ""
    guard_tail_gm_id = bool(device and variable == "gm_id" and direction == "increase" and _is_bias_role(role))
    if guard_tail_gm_id:
        priority = "guarded"
        tradeoffs = list(tradeoffs) + [
            "Do not expand the gm/ID upper bound for bias or tail devices automatically; it can push the current source into weak inversion."
        ]
    knobs = _knob_group(state, device, variable)
    current = _current_variable_value(state, device, variable)
    bounds = _variable_range(state, device, variable)
    suggested = _suggested_next_value(current, bounds, direction, target_value)
    limit_status = _limit_status(current, bounds, direction)
    action = {
        "metric": metric,
        "cause_node": cause_node,
        "action_class": _action_class(metric, cause_node, variable),
        "score": round(float(score), 4),
        "priority": priority,
        "knob": _format_knob(device, variable),
        "apply_to": knobs,
        "direction": direction,
        "current_value": current,
        "suggested_next_value": suggested,
        "range": bounds,
        "limit_status": limit_status,
        "step_hint": step_hint,
        "rationale": rationale,
        "expected_effect": expected_effect,
        "tradeoffs": tradeoffs,
        "schema_variable_present": bool(_design_variables_for_knob(state, device, variable)),
        "auto_range_expansion_allowed": not guard_tail_gm_id,
    }
    if target_formula:
        action["target_formula"] = target_formula
    if target_value is not None:
        action["target_value"] = target_value
    range_update = None if guard_tail_gm_id else _range_update_hint(bounds, direction, limit_status, variable)
    if range_update:
        action["range_update"] = range_update
    return action


def _action_class(metric: str, cause_node: str, variable: str) -> str:
    if variable in {"Cc", "Rz"} or cause_node in {"block.compensation_network", "global.Cc", "global.Rz"}:
        return "compensation"
    if _is_bias_voltage_variable(variable):
        return "operating_point_headroom"
    if variable in {"I_tail", "I_stage2", "I_latch"}:
        return "operating_point_balance"
    if "headroom" in cause_node or "Vov" in cause_node or metric in HEADROOM_METRICS:
        return "operating_point_headroom"
    if variable == "gm_id":
        return "transconductance_bias"
    if variable == "L":
        return "gain_pole_tradeoff"
    return "schema_parameter_tuning"


def _rank_tuning_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority_order = {"primary": 0, "secondary": 1, "guarded": 2}
    ranked = sorted(actions, key=lambda item: (priority_order.get(item["priority"], 9), -float(item["score"])))
    for idx, action in enumerate(ranked, start=1):
        action["rank"] = idx
        action["action_id"] = _action_id(action, idx)
    return ranked


def _action_id(action: dict[str, Any], rank: int) -> str:
    metric = str(action.get("metric", "metric")).replace(".", "_")
    knob = str(action.get("knob", "knob")).replace(".", "_")
    direction = str(action.get("direction", "tune")).replace(".", "_")
    return f"{metric}_{rank:02d}_{knob}_{direction}"


def _coarse_fine_mode(symptoms: list[dict[str, Any]]) -> str:
    gaps = [
        _symptom_gap_fraction(symptom)
        for symptom in symptoms
        if symptom.get("status") == "fail"
    ]
    if not gaps:
        return "fine"
    return "coarse" if max(gaps) >= 0.25 else "fine"


def _apply_agent_step(action: dict[str, Any], symptom: dict[str, Any], planning_mode: str) -> dict[str, Any]:
    direction = action.get("direction")
    if direction == "set":
        action["agent_step_basis"] = "explicit target formula"
        action["tuning_mode"] = planning_mode
        return action
    current = action.get("current_value")
    bounds = action.get("range")
    if current is None or direction not in {"increase", "decrease"}:
        action["agent_step_basis"] = "no numeric current value"
        return action

    step = _agent_step_fraction(
        metric=action.get("metric", ""),
        variable=str(action.get("knob", "")).split(".")[-1],
        priority=action.get("priority", ""),
        score=float(action.get("score", 0.0) or 0.0),
        symptom=symptom,
        planning_mode=planning_mode,
    )
    if action.get("max_step_fraction") is not None:
        step = min(step, float(action["max_step_fraction"]))
    if action.get("min_step_fraction") is not None:
        step = max(step, float(action["min_step_fraction"]))
    current_f = float(current)
    sign = 1.0 if direction == "increase" else -1.0
    raw_next = current_f * (1.0 + sign * step)
    suggested = _clip_to_bounds(raw_next, bounds)
    action["agent_step_fraction"] = step
    action["agent_step_basis"] = "coarse-fine mode, spec gap, attribution score, action priority, and schema bounds"
    action["tuning_mode"] = planning_mode
    action["suggested_unclipped_value"] = raw_next
    action["suggested_next_value"] = suggested
    action["limit_status"] = _limit_status_from_suggestion(current_f, raw_next, suggested, bounds, direction)
    if action.get("auto_range_expansion_allowed", True):
        range_update = _range_update_hint(bounds, direction, action["limit_status"], str(action.get("knob", "")).split(".")[-1], current_f, step)
    else:
        range_update = None
    if range_update:
        action["range_update"] = range_update
    elif "range_update" in action:
        action.pop("range_update")
    return action


def _agent_step_fraction(
    metric: str,
    variable: str,
    priority: str,
    score: float,
    symptom: dict[str, Any],
    planning_mode: str = "fine",
) -> float:
    gap = _symptom_gap_fraction(symptom)
    raw = gap * (0.55 + 0.85 * max(score, 0.0))
    if priority == "secondary":
        raw *= 0.75
    elif priority == "guarded":
        raw *= 0.45

    min_step, max_step = {
        "L": (0.08, 0.35),
        "gm_id": (0.04, 0.25),
        "I_tail": (0.04, 0.18),
        "I_stage2": (0.04, 0.20),
        "I_latch": (0.04, 0.20),
        "Cc": (0.04, 0.30),
        "Rz": (0.04, 0.25),
        "vbias": (0.02, 0.12),
        "vbias_tail": (0.02, 0.12),
        "vbias_ncas": (0.02, 0.12),
        "vbias_pcas": (0.02, 0.12),
        "vbias_ptail": (0.02, 0.12),
    }.get(variable, (0.05, 0.20))
    if metric in {"phase_margin"} and variable == "Cc":
        max_step = 0.40
    if planning_mode == "fine":
        min_step *= 0.55
        max_step = min(max_step, 0.10)
        raw *= 0.50
    elif planning_mode == "coarse":
        min_step *= 1.05
        max_step *= 1.10
    return round(max(min_step, min(raw, max_step)), 4)


def _symptom_gap_fraction(symptom: dict[str, Any]) -> float:
    rel = symptom.get("deviation_rel")
    if rel is not None:
        try:
            return min(abs(float(rel)), 1.0)
        except (TypeError, ValueError):
            pass
    dev = symptom.get("deviation_abs")
    ref = symptom.get("min") if symptom.get("min") is not None else symptom.get("max")
    try:
        if dev is not None and ref not in (None, 0):
            return min(abs(float(dev) / max(abs(float(ref)), 1e-30)), 1.0)
    except (TypeError, ValueError):
        pass
    return 0.10


def _limit_status_from_suggestion(
    current: float,
    raw_next: float,
    suggested: float,
    bounds: dict[str, Any] | None,
    direction: str,
) -> str:
    if not bounds:
        return "unbounded"
    lo = float(bounds["min"])
    hi = float(bounds["max"])
    if direction == "increase" and raw_next > hi >= current:
        return "suggestion_clipped_to_upper_bound"
    if direction == "decrease" and raw_next < lo <= current:
        return "suggestion_clipped_to_lower_bound"
    return _limit_status(current, bounds, direction)


def _dedupe_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    seen = set()
    for action in actions:
        per_knob_values = action.get("per_knob_values") or {}
        value_key = (
            tuple(sorted((str(key), float(value)) for key, value in per_knob_values.items()))
            if isinstance(per_knob_values, dict) and per_knob_values
            else action.get("target_value")
        )
        key = (tuple(action.get("apply_to", [])), action.get("direction"), action.get("metric"), value_key)
        if key in seen:
            continue
        seen.add(key)
        out.append(action)
    return out


def _counterfactual_predictions(causes: list[CandidateCause]) -> list[dict[str, Any]]:
    predictions = []
    for rank, cause in enumerate(causes[:5], start=1):
        action, effects = _counterfactual_for_node(cause.node)
        predictions.append(
            {
                "rank": rank,
                "root_cause_node": cause.node,
                "intervention": action,
                "predicted_effect": effects,
                "expected_spice_result": "The affected metric should move in the predicted direction under a 5-10 percent perturbation if the hypothesis is causal.",
            }
        )
    return predictions


def _validation_experiments(causes: list[CandidateCause]) -> list[dict[str, Any]]:
    experiments = []
    for cause in causes[:6]:
        action, effects = _counterfactual_for_node(cause.node)
        experiments.append(
            {
                "hypothesis_node": cause.node,
                "perturbation": action,
                "sweep": ["-10%", "-5%", "+5%", "+10%"],
                "observe": list(cause.metrics),
                "expected_direction": effects,
                "validation_rule": "Support increases if the observed SPICE derivative sign matches the predicted direction.",
            }
        )
    return experiments


def _block_members(state: DesignState) -> dict[str, list[str]]:
    blocks = {
        "differential_pair": [],
        "current_mirror": [],
        "load_stage": [],
        "bias_network": [],
        "compensation_network": [],
        "source_follower_regulation": [],
        "output_stage": [],
        "dynamic_latch": [],
    }
    for dev in state.topology.devices:
        role = (dev.role or "").lower()
        if "input_pair" in role:
            blocks["differential_pair"].append(dev.id)
        if "mirror" in role:
            blocks["current_mirror"].append(dev.id)
        if "load" in role:
            blocks["load_stage"].append(dev.id)
        if _is_bias_role(role):
            blocks["bias_network"].append(dev.id)
        if "source_follower" in role or "regulated_source" in role or "follower" in role:
            blocks["source_follower_regulation"].append(dev.id)
        if "second_stage" in role or "output" in role:
            blocks["output_stage"].append(dev.id)
        if any(token in role for token in ("latch", "reset", "precharge", "equalize")):
            blocks["dynamic_latch"].append(dev.id)
    return blocks


def _port_nets(state: DesignState, direction: str) -> set[str]:
    return {port.id for port in state.topology.ports if (port.direction or "").lower() == direction}


def _shared_internal_nets(state: DesignState) -> set[str]:
    counts: dict[str, int] = {}
    global_nets = {net.name for net in state.topology.global_nets}
    for dev in state.topology.devices:
        for net_name in (dev.connections or {}).values():
            if not net_name or net_name in global_nets:
                continue
            counts[net_name] = counts.get(net_name, 0) + 1
    return {name for name, count in counts.items() if count > 1}


def _edge(source: str, target: str, strength: str, condition: str = "", edge_type: str | None = None) -> dict[str, Any]:
    typed_edge = edge_type or _infer_edge_type(source, target)
    source_type = _infer_causal_node_type(source)
    target_type = _infer_causal_node_type(target)
    return {
        "schema_version": CAUSAL_EDGE_SCHEMA_VERSION,
        "edge_id": _typed_edge_id(source, target, typed_edge),
        "source": source,
        "target": target,
        "source_type": source_type,
        "target_type": target_type,
        "direction": f"{source} -> {target}",
        "strength": strength,
        "weight": EDGE_STRENGTH_WEIGHTS.get(strength, 0.5) * EDGE_TYPE_WEIGHTS.get(typed_edge, 0.65),
        "edge_type": typed_edge,
        "causal_direction": "source_to_target",
        "polarity": _infer_edge_polarity(source, target, typed_edge, condition),
        "mechanism": condition or _mechanism_for_edge_type(typed_edge),
        "typing": {
            "source_node_type": source_type,
            "target_node_type": target_type,
            "relation_type": typed_edge,
        },
        "condition": condition,
    }


def _typed_edge_id(source: str, target: str, edge_type: str) -> str:
    raw = f"{source}__{edge_type}__{target}"
    return raw.replace(".", "_").replace(" ", "_").replace(">", "to")


def _infer_causal_node_type(node: str) -> str:
    if node.startswith("device.") and len(node.split(".")) >= 3:
        return "device_parameter"
    if node.startswith("device."):
        return "transistor"
    if node.startswith("net."):
        return "circuit_net"
    if node.startswith("block."):
        return "functional_block"
    if node.startswith("behavior."):
        return "circuit_behavior"
    if node.startswith("metric."):
        return "performance_metric"
    if node.startswith("constraint."):
        return "constraint"
    if node.startswith("global."):
        return "global_parameter"
    return "unknown"


def _infer_edge_polarity(source: str, target: str, edge_type: str, condition: str) -> str:
    text = f"{source} {target} {condition}".lower()
    if any(token in text for token in ("trade-off", "opposite", "lowers", "reduces", "decrease", "fall")):
        if any(token in text for token in ("increase", "raises", "higher", "boost")):
            return "mixed"
        return "negative"
    if any(token in text for token in ("increase", "raises", "higher", "boost", "contributes", "scales")):
        return "positive"
    if edge_type in {"pole_zero_dependency", "bias_dependency"}:
        return "conditional"
    return "unknown"


def _mechanism_for_edge_type(edge_type: str) -> str:
    return {
        "gain_propagation_dependency": "small-signal gain or transconductance dependency",
        "pole_zero_dependency": "frequency-domain pole/zero dependency",
        "signal_dependency": "signal-path connectivity dependency",
        "bias_dependency": "bias or operating-point dependency",
        "structural_decomposition": "hierarchical structural decomposition",
    }.get(edge_type, "typed causal dependency")


def _infer_edge_type(source: str, target: str) -> str:
    joined = f"{source} {target}".lower()
    if any(token in joined for token in ("pole", "zero", "compensation", "capacitance", "cc", "rz", "phase_margin", "stability_margin")):
        return "pole_zero_dependency"
    if any(token in joined for token in ("bias", "headroom", "vov", "current", "tail")):
        return "bias_dependency"
    if any(token in joined for token in ("gain", "gm", "ro", "output_resistance", "input_transconductance", "ugbw", "unity_gain")):
        return "gain_propagation_dependency"
    return "signal_dependency"


def _device_capacitance(p) -> float:
    return max(float(p.cgg or 0.0), float(p.cgs or 0.0) + float(p.cgd or 0.0) + float(p.cdd or 0.0))


def _required_margin(state: DesignState, dev_id: str) -> float:
    target = state.targets.get("saturation_margin")
    if target is not None and target.min is not None:
        return max(0.0, float(target.min))
    dev = state.get_device_def(dev_id)
    role = dev.role if dev else ""
    return {
        "input_pair": 0.08,
        "cascode": 0.20,
        "tail_current_source": 0.05,
        "tail_bias_mirror": 0.03,
        "current_mirror_load": 0.03,
        "current_mirror_load_regulated_output": 0.03,
        "source_follower_regulator": 0.05,
        "regulated_source_current_source": 0.05,
        "second_stage_gain": 0.05,
        "second_stage_load": 0.05,
        "output_current_source": 0.05,
        "output_bias_mirror": 0.03,
    }.get(role, 0.05)


def _weakest_headroom_device(state: DesignState) -> str | None:
    best = None
    best_margin = float("inf")
    for dev in state.topology.devices:
        ts = state.transistors.get(dev.id)
        if ts is None:
            continue
        p = ts.parameters
        if p.vds <= 0 or p.vdsat <= 0:
            continue
        margin = p.vds - p.vdsat - _required_margin(state, dev.id)
        if margin < best_margin:
            best = dev.id
            best_margin = margin
    return best


def _lowest_role_param(state: DesignState, role_token: str, param: str) -> str | None:
    best = None
    best_value = float("inf")
    for dev in state.topology.devices:
        if role_token not in (dev.role or "").lower():
            continue
        ts = state.transistors.get(dev.id)
        if ts is None:
            continue
        value = float(getattr(ts.parameters, param, 0.0) or 0.0)
        if 0.0 < value < best_value:
            best = dev.id
            best_value = value
    return best


def _lowest_output_resistance_device(state: DesignState) -> str | None:
    best = None
    best_ro = float("inf")
    for dev in state.topology.devices:
        role = (dev.role or "").lower()
        if not _is_gain_path_ro_cause(state, dev.id, role):
            continue
        ts = state.transistors.get(dev.id)
        if ts is None or ts.parameters.gds <= 0:
            continue
        ro = 1.0 / ts.parameters.gds
        if ro < best_ro:
            best = dev.id
            best_ro = ro
    return best


def _is_gain_path_ro_cause(state: DesignState, dev_id: str, role: str) -> bool:
    if _is_direct_gain_ro_role(role):
        return True
    if not dev_id or _is_bias_role(role):
        return False
    output_nets = _port_nets(state, "output")
    dev = state.get_device_def(dev_id)
    if dev and any(net in output_nets for terminal, net in (dev.connections or {}).items() if terminal in {"drain", "source"}):
        return True
    peers = _symmetry_peer_devices(state, dev_id, "L")
    for peer in peers:
        peer_dev = state.get_device_def(peer)
        if peer_dev and any(net in output_nets for terminal, net in (peer_dev.connections or {}).items() if terminal in {"drain", "source"}):
            return True
    return False


def _is_direct_gain_ro_role(role: str) -> bool:
    role = role.lower()
    if any(token in role for token in ("tail", "bias_mirror", "regulated_source_current_source")):
        return False
    return any(
        token in role
        for token in (
            "current_mirror_load",
            "load",
            "cascode",
            "second_stage_gain",
            "second_stage_load",
            "output_current_source",
        )
    )


def _is_bias_role(role: str) -> bool:
    role = role.lower()
    return any(token in role for token in ("tail", "bias", "regulated_source_current_source"))


def _largest_capacitance_device(state: DesignState) -> str | None:
    best = None
    best_cap = 0.0
    for dev in state.topology.devices:
        ts = state.transistors.get(dev.id)
        if ts is None:
            continue
        cap = _device_capacitance(ts.parameters)
        if cap > best_cap:
            best = dev.id
            best_cap = cap
    return best


def _first_role_device(state: DesignState, role: str) -> str | None:
    for dev in state.topology.devices:
        if dev.role == role:
            return dev.id
    return None


def _first_role_contains(state: DesignState, token: str) -> str | None:
    for dev in state.topology.devices:
        if token in (dev.role or "").lower():
            return dev.id
    return None


def _device_support(state: DesignState, dev_id: str) -> str:
    ts = state.transistors.get(dev_id)
    dev = state.get_device_def(dev_id)
    if ts is None:
        return f"{dev_id}: no transistor state"
    p = ts.parameters
    ro = 1.0 / p.gds if p.gds > 0 else 0.0
    cap = _device_capacitance(p)
    margin = p.vds - p.vdsat - _required_margin(state, dev_id)
    return (
        f"{dev_id} role={dev.role if dev else ''}, gm={p.gm:.4e}, ro={ro:.4e}, "
        f"cap={cap:.4e}, id={p.id:.4e}, headroom_margin={margin:.4e}, region={p.region}"
    )


def _compensation_support(state: DesignState) -> str:
    cc = state.global_parameters.get("Cc")
    rz = state.global_parameters.get("Rz")
    gm2 = 0.0
    for dev in state.topology.devices:
        if dev.role == "second_stage_gain" and dev.id in state.transistors:
            gm2 = state.transistors[dev.id].parameters.gm
            break
    rz_target = 1.0 / gm2 if gm2 > 1e-12 else None
    return f"Cc={cc}, Rz={rz}, Rz_target_1_over_gm2={rz_target}"


def _tuning_strategy_text(metric: str) -> str:
    if metric in {"dc_gain", "gain"}:
        return "Raise signal-path ro first, then raise useful gm, while repairing any headroom limiter that can collapse ro."
    if metric == "phase_margin":
        return "Move the main pole lower for two-stage OTA compensation, align Rz to 1/gm(second_stage_gain), then push non-dominant poles higher."
    if metric in {"unity_gain_bandwidth", "ugbw", "bandwidth"}:
        return "Increase input/stage gm and reduce avoidable dominant capacitance, with phase margin as the guardrail."
    if metric in {"slew_rate", "slew_rate_pos", "slew_rate_neg"}:
        return "Increase available charging current or reduce compensation/load capacitance, with power and PM as guardrails."
    if metric in HEADROOM_METRICS:
        return "Reduce VDSAT/Vov of the limiting stack and rebalance bias currents to recover voltage headroom."
    if metric in {"delay", "decision_time", "propagation_delay", "regeneration_time"}:
        return "Increase latch/input gm and reduce switched capacitance to shorten regeneration time."
    if metric in {"power", "energy", "energy_per_comparison", "pdp", "edp"}:
        return "Reduce bias current and switched capacitance while checking delay and noise constraints."
    return "Tune the highest-ranked causal knobs first, then rerun OP before measuring performance."


def _current_variable_for_role(role: str) -> str | None:
    role = role.lower()
    if any(token in role for token in ("second_stage", "output_current", "stage2")):
        return "I_stage2"
    if "latch" in role:
        return "I_latch"
    if any(token in role for token in ("tail", "input_pair", "current_mirror_load", "bias")):
        return "I_tail"
    return None


def _rz_target_from_second_stage(state: DesignState) -> float | None:
    gm = _gm_for_role(state, "second_stage_gain")
    if gm and gm > 1e-12:
        return 1.0 / gm
    return None


def _gm_for_role(state: DesignState, role: str) -> float | None:
    for dev in state.topology.devices:
        if dev.role == role and dev.id in state.transistors:
            gm = state.transistors[dev.id].parameters.gm
            if gm > 0:
                return gm
    return None


def _format_knob(device: str, variable: str) -> str:
    return f"{device}.{variable}" if device else f"global.{variable}"


def _design_variables_for_knob(state: DesignState, device: str, variable: str) -> list[Any]:
    return [dv for dv in state.design_variables if dv.device == device and dv.variable == variable]


def _primary_design_variable(state: DesignState, device: str, variable: str):
    matches = _design_variables_for_knob(state, device, variable)
    return matches[0] if matches else None


def _knob_group(state: DesignState, device: str, variable: str) -> list[str]:
    dv = _primary_design_variable(state, device, variable)
    if not dv or not dv.symmetry_label:
        return [_format_knob(device, variable)]
    group = [
        _format_knob(item.device, item.variable)
        for item in state.design_variables
        if item.symmetry_label == dv.symmetry_label and item.variable == variable
    ]
    return group or [_format_knob(device, variable)]


def _variable_range(state: DesignState, device: str, variable: str) -> dict[str, Any] | None:
    dv = _primary_design_variable(state, device, variable)
    if dv and dv.range:
        return {"min": float(dv.range.min), "max": float(dv.range.max), "unit": dv.unit}
    if device and variable == "gm_id":
        r = state.constraints.get_gm_id_range(device)
        return {"min": float(r.min), "max": float(r.max), "unit": ""}
    if device and variable == "L":
        r = state.constraints.get_L_range(device)
        return {"min": float(r.min), "max": float(r.max), "unit": "m"}
    return None


def _current_variable_value(state: DesignState, device: str, variable: str) -> float | None:
    if not device:
        if variable in state.global_parameters:
            return float(state.global_parameters[variable])
        dv = _primary_design_variable(state, device, variable)
        return float(dv.initial) if dv and dv.initial is not None else None

    ts = state.transistors.get(device)
    if ts is None:
        dv = _primary_design_variable(state, device, variable)
        return float(dv.initial) if dv and dv.initial is not None else None
    if variable == "L":
        if ts.parameters.L > 0:
            return float(ts.parameters.L)
        return float(ts.L_strategy) if ts.L_strategy else None
    if variable == "gm_id":
        if ts.gm_id_strategy:
            return float(ts.gm_id_strategy)
        if ts.parameters.gm_id_realized:
            return float(ts.parameters.gm_id_realized)
    value = getattr(ts.parameters, variable, None)
    if value is not None:
        return float(value)
    dv = _primary_design_variable(state, device, variable)
    return float(dv.initial) if dv and dv.initial is not None else None


def _suggested_next_value(
    current: float | None,
    bounds: dict[str, Any] | None,
    direction: str,
    target_value: float | None,
) -> float | None:
    if direction == "set" and target_value is not None:
        return _clip_to_bounds(float(target_value), bounds)
    if current is None:
        if not bounds:
            return target_value
        if direction == "increase":
            return float(bounds["min"] + 0.65 * (bounds["max"] - bounds["min"]))
        if direction == "decrease":
            return float(bounds["min"] + 0.35 * (bounds["max"] - bounds["min"]))
        return target_value
    if direction == "increase":
        return _clip_to_bounds(float(current) * 1.15, bounds)
    if direction == "decrease":
        return _clip_to_bounds(float(current) * 0.90, bounds)
    return _clip_to_bounds(float(current), bounds)


def _clip_to_bounds(value: float, bounds: dict[str, Any] | None) -> float:
    if not bounds:
        return value
    return min(max(value, float(bounds["min"])), float(bounds["max"]))


def _limit_status(current: float | None, bounds: dict[str, Any] | None, direction: str) -> str:
    if current is None or not bounds:
        return "unknown"
    lo = float(bounds["min"])
    hi = float(bounds["max"])
    if hi <= lo:
        return "invalid_range"
    position = (float(current) - lo) / (hi - lo)
    if direction == "increase" and position >= 0.95:
        return "at_upper_bound"
    if direction == "decrease" and position <= 0.05:
        return "at_lower_bound"
    if direction == "set":
        return "setpoint"
    return "within_range"


def _range_update_hint(
    bounds: dict[str, Any] | None,
    direction: str,
    limit_status: str,
    variable: str,
    current: float | None = None,
    step_fraction: float | None = None,
) -> dict[str, Any] | None:
    if not bounds:
        return None
    lo = float(bounds["min"])
    hi = float(bounds["max"])
    step = float(step_fraction if step_fraction is not None else 0.20)
    anchor_hi = float(current) if current is not None and current > 0 else hi
    anchor_lo = float(current) if current is not None and current > 0 else lo
    if limit_status in {"at_upper_bound", "suggestion_clipped_to_upper_bound"} and direction == "increase":
        multiplier = 1.0 + min(max(step * 1.5, 0.15), 0.60)
        return {"type": "expand_upper_bound", "suggested_max": max(hi, anchor_hi * multiplier)}
    if limit_status in {"at_lower_bound", "suggestion_clipped_to_lower_bound"} and direction == "decrease":
        multiplier = 1.0 - min(max(step * 1.5, 0.10), 0.50)
        return {"type": "expand_lower_bound", "suggested_min": max(0.0, min(lo, anchor_lo * multiplier))}
    return None


def _path_chain_for_metric(state: DesignState, metric: str, cause_node: str, capabilities) -> list[str]:
    role = _role_for_cause_node(state, cause_node)
    if metric == "phase_margin":
        if cause_node in {"block.compensation_network", "global.Cc", "global.Rz"}:
            return [cause_node, "block.compensation_network", "behavior.compensation_zero", "constraint.stability_margin", f"metric.{metric}"]
        block = _signal_block_for_role(role)
        return [cause_node, block, "behavior.non_dominant_pole", "constraint.stability_margin", f"metric.{metric}"]
    if metric in {"unity_gain_bandwidth", "ugbw", "bandwidth"}:
        if cause_node == "global.Cc":
            return [cause_node, "block.compensation_network", "behavior.dominant_pole", f"metric.{metric}"]
        return [cause_node, "block.differential_pair", "behavior.input_transconductance", f"metric.{metric}"]
    if metric in {"dc_gain", "gain"}:
        if cause_node.endswith(".gm") and "input_pair" in role:
            return [cause_node, "block.differential_pair", "behavior.input_transconductance", f"metric.{metric}"]
        dev_id = _node_device(cause_node) or ""
        if cause_node.endswith(".ro") and _is_gain_path_ro_cause(state, dev_id, role):
            return [cause_node, _signal_block_for_role(role), "behavior.output_resistance", f"metric.{metric}"]
        if _is_bias_role(role) or cause_node.endswith(".headroom"):
            return [cause_node, "block.bias_network", "behavior.bias_headroom", "constraint.headroom", f"metric.{metric}"]
        if "source_follower" in role or "regulated_source" in role:
            return [cause_node, "block.source_follower_regulation", "behavior.output_resistance", f"metric.{metric}"]
        return [cause_node, _signal_block_for_role(role), "behavior.output_resistance", f"metric.{metric}"]
    if metric in HEADROOM_METRICS:
        return [cause_node, "behavior.bias_headroom", "constraint.headroom", f"metric.{metric}"]
    if metric in {"slew_rate", "slew_rate_pos", "slew_rate_neg"}:
        return [cause_node, "behavior.large_signal_charge", f"metric.{metric}"]
    return [cause_node, f"metric.{metric}"]


def _role_for_cause_node(state: DesignState, cause_node: str) -> str:
    parts = cause_node.split(".")
    if len(parts) < 3 or parts[0] != "device":
        return ""
    dev = state.get_device_def(parts[1])
    return (dev.role or "").lower() if dev else ""


def _signal_block_for_role(role: str) -> str:
    role = role.lower()
    if "input_pair" in role:
        return "block.differential_pair"
    if "second_stage" in role or "output" in role:
        return "block.output_stage"
    if "load" in role:
        return "block.load_stage"
    if _is_bias_role(role):
        return "block.bias_network"
    return "block.load_stage"


def _path_explanation(metric: str, cause_node: str) -> str:
    return f"{cause_node} is a ranked upstream hypothesis for {metric}; the chain is directional and should be validated by perturbation."


def _counterfactual_for_node(node: str) -> tuple[str, dict[str, str]]:
    if node.endswith(".gm"):
        return ("increase device gm by 5-10 percent through gm/ID, W, or bias-current perturbation", {"gain": "increase", "ugbw_or_delay": "speed improves"})
    if node.endswith(".ro"):
        return ("increase device ro by 5-10 percent through longer L or lower channel-length modulation", {"gain": "increase", "dominant_pole": "may move lower"})
    if node.endswith(".capacitance"):
        return ("reduce effective capacitance by 5-10 percent", {"ugbw": "increase", "phase_margin": "usually improves if non-dominant pole moves higher", "slew_rate": "increase"})
    if node.endswith(".bias_current"):
        return ("increase available bias current by 5-10 percent", {"slew_rate": "increase", "ugbw": "may increase", "power": "increase"})
    if node.endswith(".headroom") or node.endswith(".Vov"):
        return ("reduce required VDSAT/Vov or adjust bias to add 5-10 percent more headroom", {"output_swing": "increase", "gain": "increase if saturation is restored"})
    if node == "global.Cc":
        return ("sweep Cc by +/-5-10 percent", {"phase_margin": "increases when Cc is too small", "ugbw": "decreases as Cc increases", "slew_rate": "decreases as Cc increases"})
    if node == "global.Rz" or node == "block.compensation_network":
        return ("move Rz toward 1/gm2 and sweep +/-10 percent around that value", {"phase_margin": "improves when zero placement was the cause"})
    return ("perturb this node by +/-5-10 percent in the netlist or sizing variables", {"affected_metrics": "should follow outgoing dependency edges"})


def _score_to_strength(score: float) -> str:
    if score >= 0.75:
        return "high"
    if score >= 0.4:
        return "medium"
    return "low"


def _dedupe_text(items: list[str]) -> list[str]:
    out = []
    for item in items:
        if item not in out:
            out.append(item)
    return out

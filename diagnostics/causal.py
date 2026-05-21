from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from asir.capabilities import detect_circuit_capabilities
from schemas.design_state import DesignState
from simulator.ngspice import SimulationResult
from specs.models import CircuitSpecModel


STANDARD_PERFORMANCE_METRICS = (
    "dc_gain",
    "unity_gain_bandwidth",
    "phase_margin",
    "slew_rate",
    "output_swing",
)


@dataclass(frozen=True)
class CandidateCause:
    node: str
    score: float
    metrics: tuple[str, ...]
    evidence: tuple[str, ...]
    simulation_support: str


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
    causes = _rank_root_causes(state, symptoms, sim_result, capabilities)
    paths = _causal_paths(state, symptoms, causes, capabilities)
    predictions = _counterfactual_predictions(causes)
    experiments = _validation_experiments(causes)
    attribution = _agent_failure_attribution(state, symptoms, causes, paths)
    return {
        "schema_version": "analogrf_ir.causal_diagnostics.v0_1",
        "method": "directional_dependency_graph_with_ranked_testable_hypotheses",
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
        "counterfactual_predictions": predictions,
        "suggested_validation_experiments": experiments,
        "validation_protocol": {
            "principle": "Perturb each proposed cause by 5-10 percent and compare predicted metric direction against SPICE.",
            "acceptance": "A hypothesis gains support when the predicted metric direction appears without creating a larger primary-target violation.",
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
    for dev in state.topology.devices:
        ts = state.transistors.get(dev.id)
        if ts is None:
            continue
        p = ts.parameters
        cap = _device_capacitance(p)
        ro = 1.0 / p.gds if p.gds > 0 else None
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
        edges.extend(
            [
                _edge(f"device.{dev.id}.bias_current", f"device.{dev.id}.gm", "high", "For fixed gm/ID sizing, higher current increases gm."),
                _edge(f"device.{dev.id}.bias_current", f"device.{dev.id}.capacitance", "medium", "Larger current usually implies larger device width and capacitance."),
                _edge(f"device.{dev.id}.Vov", f"device.{dev.id}.headroom", "high", "Higher overdrive/VDSAT consumes saturation headroom."),
            ]
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
            edges.append(_edge(f"device.{dev_id}.gm", f"block.{block}", gm_strength, f"{role} transconductance controls this block response."))
            edges.append(_edge(f"device.{dev_id}.ro", f"block.{block}", ro_strength, f"{role} output resistance controls block gain or pole location."))
            edges.append(_edge(f"device.{dev_id}.capacitance", f"block.{block}", "medium", f"{role} capacitance contributes to local pole and load."))
            edges.append(_edge(f"device.{dev_id}.headroom", "constraint.headroom", "high", f"{role} must keep VDS above saturation requirement."))

    for name, value in state.global_parameters.items():
        if name in {"Cc", "Rz", "CL", "Cload"} or name.lower().startswith("c"):
            nodes.append({"id": f"global.{name}", "type": "global_parameter", "label": name, "value": value})
    if "Cc" in state.global_parameters:
        edges.append(_edge("global.Cc", "block.compensation_network", "high", "Miller capacitance sets dominant-pole and slew trade-off."))
    if "Rz" in state.global_parameters:
        edges.append(_edge("global.Rz", "block.compensation_network", "high", "Zero-setting resistor changes compensation zero location."))

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
    return {"nodes": nodes, "edges": edges}


def _metric_edges(capabilities) -> list[dict[str, str]]:
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
) -> list[CandidateCause]:
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
                add(f"device.{lowest_output_ro}.ro", metric, 0.78, "Output/load resistance is a direct gain factor.", _device_support(state, lowest_output_ro))
            if lowest_input_gm:
                add(f"device.{lowest_input_gm}.gm", metric, 0.58, "Input-pair gm is the forward gain source.", _device_support(state, lowest_input_gm))
            if weakest_headroom:
                add(f"device.{weakest_headroom}.headroom", metric, 0.50, "Headroom loss can lower gain indirectly by moving bias devices or signal devices away from saturation.", _device_support(state, weakest_headroom))
            if capabilities.has("source_follower_regulation") and regulator:
                add(f"device.{regulator}.gm", metric, 0.44, "Source-follower local feedback gm boosts effective output resistance.", _device_support(state, regulator))
        elif metric in {"unity_gain_bandwidth", "ugbw", "bandwidth"}:
            if lowest_input_gm:
                add(f"device.{lowest_input_gm}.gm", metric, 0.82, "UGBW scales with useful input gm over dominant capacitance.", _device_support(state, lowest_input_gm))
            if largest_cap:
                add(f"device.{largest_cap}.capacitance", metric, 0.70, "Large internal capacitance lowers pole frequencies and UGBW.", _device_support(state, largest_cap))
            if "Cc" in state.global_parameters:
                add("global.Cc", metric, 0.62, "Miller capacitance trades bandwidth for phase margin.", f"Cc={state.global_parameters['Cc']:.4e}")
        elif metric == "phase_margin":
            if largest_cap:
                add(f"device.{largest_cap}.capacitance", metric, 0.76, "Large parasitic capacitance can pull a non-dominant pole toward unity gain.", _device_support(state, largest_cap))
            if capabilities.has("miller_rc_compensation"):
                add("block.compensation_network", metric, 0.90, "Cc/Rz placement directly controls dominant pole and zero.", _compensation_support(state))
            if capabilities.has("source_follower_regulation") and regulator:
                add(f"device.{regulator}.gm", metric, 0.58, "Source-follower loop gm changes local-feedback pole placement.", _device_support(state, regulator))
        elif metric in {"slew_rate", "slew_rate_pos", "slew_rate_neg"}:
            if tail_current:
                add(f"device.{tail_current}.bias_current", metric, 0.86, "Slew rate is current divided by effective load or compensation capacitance.", _device_support(state, tail_current))
            if "Cc" in state.global_parameters:
                add("global.Cc", metric, 0.75, "Larger Cc reduces slew rate for fixed current.", f"Cc={state.global_parameters['Cc']:.4e}")
            if largest_cap:
                add(f"device.{largest_cap}.capacitance", metric, 0.55, "Large capacitance increases large-signal charge demand.", _device_support(state, largest_cap))
        elif metric in {"output_swing", "swing"}:
            if weakest_headroom:
                add(f"device.{weakest_headroom}.headroom", metric, 0.92, "Output swing is directly limited by device saturation headroom.", _device_support(state, weakest_headroom))
            if capabilities.has("source_follower_regulation") and regulator:
                add(f"device.{regulator}.Vov", metric, 0.75, "Regulated-source bias Vov consumes output common-mode range.", _device_support(state, regulator))
        elif metric in {"icmr", "icmr_min", "icmr_max", "input_common_mode_min", "input_common_mode_max"}:
            if weakest_headroom:
                add(f"device.{weakest_headroom}.headroom", metric, 0.86, "Input common-mode range is set by stacked device headroom.", _device_support(state, weakest_headroom))
        elif metric in {"delay", "decision_time", "propagation_delay", "regeneration_time"}:
            latch = _first_role_contains(state, "latch")
            if latch:
                add(f"device.{latch}.gm", metric, 0.90, "Comparator regeneration delay is dominated by latch gm over capacitance.", _device_support(state, latch))
            if largest_cap:
                add(f"device.{largest_cap}.capacitance", metric, 0.72, "Latch and output capacitance slow regeneration.", _device_support(state, largest_cap))
        elif metric in {"power", "energy", "energy_per_comparison", "pdp", "edp"}:
            if tail_current:
                add(f"device.{tail_current}.bias_current", metric, 0.70, "Bias current and switched capacitance drive power or energy.", _device_support(state, tail_current))
            if largest_cap:
                add(f"device.{largest_cap}.capacitance", metric, 0.55, "Switched capacitance contributes dynamic energy.", _device_support(state, largest_cap))

    normalized = []
    max_score = max((item["score"] for item in candidates.values()), default=1.0)
    for node, item in candidates.items():
        score = min(item["score"] / max(max_score, 1e-30), 1.0)
        normalized.append(
            CandidateCause(
                node=node,
                score=round(score, 4),
                metrics=tuple(sorted(item["metrics"])),
                evidence=tuple(_dedupe_text(item["evidence"])),
                simulation_support="; ".join(_dedupe_text(item["support"][:3])),
            )
        )
    return sorted(normalized, key=lambda cause: cause.score, reverse=True)


def _agent_failure_attribution(
    state: DesignState,
    symptoms: list[dict[str, Any]],
    causes: list[CandidateCause],
    paths: list[dict[str, Any]],
) -> dict[str, Any]:
    by_failure = []
    for symptom in symptoms:
        metric = symptom["metric"]
        metric_causes = [cause for cause in causes if metric in cause.metrics]
        metric_paths = [path for path in paths if path["metric"] == metric]
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
                        "evidence": list(cause.evidence),
                        "simulation_support": cause.simulation_support,
                    }
                    for cause in top
                ],
                "strongest_path": metric_paths[0]["chain"] if metric_paths else [],
                "validation_required": "Run the listed perturbation experiments before treating the attribution as confirmed.",
            }
        )
    return {
        "author": "analog_circuit_causal_diagnostic_agent",
        "state_source": "design_state.yaml:diagnostics.causal_diagnostics",
        "principle": "Ranked causal hypotheses are written into schema state; JSON artifacts are derived views only.",
        "by_failure": by_failure,
    }


def _primary_attribution_text(state: DesignState, metric: str, cause: CandidateCause) -> str:
    role = _role_for_cause_node(state, cause.node)
    if metric in {"dc_gain", "gain"} and cause.node.endswith(".ro") and _is_direct_gain_ro_role(role):
        return "The leading hypothesis is low signal-path output resistance in the load/output stage, which directly reduces low-frequency gain."
    if metric in {"dc_gain", "gain"} and cause.node.endswith(".gm") and "input_pair" in role:
        return "The leading hypothesis is insufficient input-pair transconductance, which limits forward gain and gain-bandwidth."
    if cause.node.endswith(".headroom") or _is_bias_role(role):
        return "The leading hypothesis is insufficient operating-point headroom, which can indirectly reduce gain or swing by weakening saturation margins."
    if metric == "phase_margin" and cause.node in {"block.compensation_network", "global.Cc", "global.Rz"}:
        return "The leading hypothesis is compensation placement: the dominant pole and zero should be verified by sweeping Cc and Rz around the analytical target."
    if metric == "phase_margin":
        return "The leading hypothesis is a non-dominant pole moving too close to unity gain."
    return f"The leading hypothesis is {cause.node}; validate it with the proposed SPICE perturbation before accepting it."


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


def _edge(source: str, target: str, strength: str, condition: str = "") -> dict[str, str]:
    return {
        "source": source,
        "target": target,
        "direction": f"{source} -> {target}",
        "strength": strength,
        "condition": condition,
    }


def _device_capacitance(p) -> float:
    return max(float(p.cgg or 0.0), float(p.cgs or 0.0) + float(p.cgd or 0.0) + float(p.cdd or 0.0))


def _required_margin(state: DesignState, dev_id: str) -> float:
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
        if not _is_direct_gain_ro_role(role):
            continue
        ts = state.transistors.get(dev.id)
        if ts is None or ts.parameters.gds <= 0:
            continue
        ro = 1.0 / ts.parameters.gds
        if ro < best_ro:
            best = dev.id
            best_ro = ro
    return best


def _is_direct_gain_ro_role(role: str) -> bool:
    role = role.lower()
    if any(token in role for token in ("tail", "bias_mirror", "regulated_source_current_source")):
        return False
    return any(
        token in role
        for token in (
            "current_mirror_load",
            "load",
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
        if cause_node.endswith(".ro") and _is_direct_gain_ro_role(role):
            return [cause_node, _signal_block_for_role(role), "behavior.output_resistance", f"metric.{metric}"]
        if _is_bias_role(role) or cause_node.endswith(".headroom"):
            return [cause_node, "block.bias_network", "behavior.bias_headroom", "constraint.headroom", f"metric.{metric}"]
        if "source_follower" in role or "regulated_source" in role:
            return [cause_node, "block.source_follower_regulation", "behavior.output_resistance", f"metric.{metric}"]
        return [cause_node, _signal_block_for_role(role), "behavior.output_resistance", f"metric.{metric}"]
    if metric in {"output_swing", "swing", "icmr", "icmr_min", "icmr_max"}:
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

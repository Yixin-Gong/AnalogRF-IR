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
    tuning = _attribution_guided_tuning(state, symptoms, causes)
    attribution = _agent_failure_attribution(state, symptoms, causes, paths, tuning)
    return {
        "schema_version": "analogrf_ir.causal_diagnostics.v0_1",
        "method": "directional_dependency_graph_with_attribution_guided_tuning",
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


def _attribution_guided_tuning(
    state: DesignState,
    symptoms: list[dict[str, Any]],
    causes: list[CandidateCause],
) -> dict[str, Any]:
    by_failure = []
    for symptom in symptoms:
        if symptom["status"] != "fail":
            continue
        metric = symptom["metric"]
        actions = []
        for cause in [item for item in causes if metric in item.metrics][:3]:
            actions.extend(_tuning_actions_for_cause(state, metric, cause))
        actions = _dedupe_actions(actions)
        actions = [_apply_agent_step(action, symptom) for action in actions]
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
        "principle": "Translate ranked root causes into direct schema-variable tuning actions.",
        "by_failure": by_failure,
    }


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
        if _is_direct_gain_ro_role(role):
            return [
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
    return []


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
    knobs = _knob_group(state, device, variable)
    current = _current_variable_value(state, device, variable)
    bounds = _variable_range(state, device, variable)
    suggested = _suggested_next_value(current, bounds, direction, target_value)
    limit_status = _limit_status(current, bounds, direction)
    action = {
        "metric": metric,
        "cause_node": cause_node,
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
    }
    if target_formula:
        action["target_formula"] = target_formula
    if target_value is not None:
        action["target_value"] = target_value
    range_update = _range_update_hint(bounds, direction, limit_status, variable)
    if range_update:
        action["range_update"] = range_update
    return action


def _rank_tuning_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priority_order = {"primary": 0, "secondary": 1, "guarded": 2}
    ranked = sorted(actions, key=lambda item: (priority_order.get(item["priority"], 9), -float(item["score"])))
    for idx, action in enumerate(ranked, start=1):
        action["rank"] = idx
    return ranked


def _apply_agent_step(action: dict[str, Any], symptom: dict[str, Any]) -> dict[str, Any]:
    direction = action.get("direction")
    if direction == "set":
        action["agent_step_basis"] = "explicit target formula"
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
    )
    current_f = float(current)
    sign = 1.0 if direction == "increase" else -1.0
    raw_next = current_f * (1.0 + sign * step)
    suggested = _clip_to_bounds(raw_next, bounds)
    action["agent_step_fraction"] = step
    action["agent_step_basis"] = "spec gap, attribution score, action priority, and schema bounds"
    action["suggested_unclipped_value"] = raw_next
    action["suggested_next_value"] = suggested
    action["limit_status"] = _limit_status_from_suggestion(current_f, raw_next, suggested, bounds, direction)
    range_update = _range_update_hint(bounds, direction, action["limit_status"], str(action.get("knob", "")).split(".")[-1], current_f, step)
    if range_update:
        action["range_update"] = range_update
    elif "range_update" in action:
        action.pop("range_update")
    return action


def _agent_step_fraction(metric: str, variable: str, priority: str, score: float, symptom: dict[str, Any]) -> float:
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
    }.get(variable, (0.05, 0.20))
    if metric in {"phase_margin"} and variable == "Cc":
        max_step = 0.40
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
        key = (tuple(action.get("apply_to", [])), action.get("direction"), action.get("cause_node"))
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


def _tuning_strategy_text(metric: str) -> str:
    if metric in {"dc_gain", "gain"}:
        return "Raise signal-path ro first, then raise useful gm, while repairing any headroom limiter that can collapse ro."
    if metric == "phase_margin":
        return "Move the main pole lower for two-stage OTA compensation, align Rz to 1/gm(second_stage_gain), then push non-dominant poles higher."
    if metric in {"unity_gain_bandwidth", "ugbw", "bandwidth"}:
        return "Increase input/stage gm and reduce avoidable dominant capacitance, with phase margin as the guardrail."
    if metric in {"slew_rate", "slew_rate_pos", "slew_rate_neg"}:
        return "Increase available charging current or reduce compensation/load capacitance, with power and PM as guardrails."
    if metric in {"output_swing", "swing", "icmr", "icmr_min", "icmr_max"}:
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

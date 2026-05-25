from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from netlist.generator import generate_netlist
from schemas.design_state import DesignState, Range
from simulator.ngspice import SimulationResult
from specs.models import CircuitSpecModel


INTERVENTION_SCHEMA_VERSION = "analogrf_ir.local_intervention_model.v0_1"
OPTIMIZER_SCHEMA_VERSION = "analogrf_ir.constrained_action_optimizer.v0_1"
EVIDENCE_GATE_SCHEMA_VERSION = "analogrf_ir.evidence_gate.v0_1"
ACTION_ADMISSIBILITY_SCHEMA_VERSION = "analogrf_ir.formal_action_admissibility.v0_1"
ACTION_ADMISSIBILITY_RULE = (
    "apply_allowed := optimizer_selected OR objective_delta < 0; guarded actions also require evidence_gate.passed"
)
EVIDENCE_GATE_THRESHOLDS = {
    "min_absolute_objective_improvement": 0.002,
    "min_relative_objective_improvement": 0.015,
    "max_tradeoff_to_improvement_ratio": 0.60,
    "max_component_worsening": 0.12,
    "max_uncertainty": 0.25,
}


def build_spice_intervention_model(
    *,
    state: DesignState,
    sim: Any,
    work_dir: str | Path,
    spec_model: CircuitSpecModel,
    target_status: dict[str, dict[str, Any]],
    tuning: dict[str, Any],
    max_actions: int = 4,
    perturbation_fraction: float = 0.10,
) -> dict[str, Any]:
    """Build a local action-to-violation model from small SPICE perturbations.

    The action set is intentionally small. This is not a sweep or optimizer
    rerun; it perturbs the current netlist-visible state around the measured
    operating point and estimates one local effect column per action.
    """

    metrics = _ordered_metrics(target_status)
    base_violation = _violation_vector(target_status, metrics)
    actions = _candidate_actions(tuning, limit=max(0, int(max_actions)))
    work_path = Path(work_dir)
    work_path.mkdir(parents=True, exist_ok=True)
    effects: list[dict[str, Any]] = []

    if not metrics:
        return _empty_intervention_model("no_target_metrics", target_status, metrics, base_violation)
    if not any(base_violation.values()):
        return _empty_intervention_model("no_failed_targets", target_status, metrics, base_violation)

    for index, action in enumerate(actions, start=1):
        action_id = str(action.get("action_id") or f"action_{index:03d}")
        trial_state = state.clone()
        applied = _apply_netlist_proxy_action(trial_state, action, perturbation_fraction)
        if not applied["applied"]:
            effects.append(
                {
                    "action_id": action_id,
                    "knob": action.get("knob"),
                    "source": "spice_small_perturbation",
                    "status": "skipped",
                    "reason": applied["reason"],
                    "delta_violation_vector": {metric: 0.0 for metric in metrics},
                    "violation_reduction": 0.0,
                    "uncertainty": 1.0,
                }
            )
            continue

        try:
            netlist = generate_netlist(trial_state)
            result: SimulationResult = sim.run(
                netlist,
                work_dir=str(work_path / _safe_action_dir(index, action_id)),
            )
        except Exception as exc:  # pragma: no cover - defensive around external simulator
            effects.append(
                {
                    "action_id": action_id,
                    "knob": action.get("knob"),
                    "source": "spice_small_perturbation",
                    "status": "error",
                    "reason": str(exc),
                    "applied_proxy": applied,
                    "delta_violation_vector": {metric: 0.0 for metric in metrics},
                    "violation_reduction": 0.0,
                    "uncertainty": 1.0,
                }
            )
            continue

        after_status = {
            name: spec_model.target_status(name, target, result.measurements or {}, {})
            for name, target in trial_state.targets.items()
        }
        after_violation = _violation_vector(after_status, metrics)
        delta = {
            metric: round(float(after_violation.get(metric, 0.0) - base_violation.get(metric, 0.0)), 6)
            for metric in metrics
        }
        reduction = _weighted_objective(base_violation, target_status) - _weighted_objective(after_violation, target_status)
        effects.append(
            {
                "action_id": action_id,
                "metric": action.get("metric"),
                "knob": action.get("knob"),
                "apply_to": action.get("apply_to", []),
                "direction": action.get("direction"),
                "source": "spice_small_perturbation",
                "status": "ok" if result.success else "sim_failed",
                "applied_proxy": applied,
                "measurements": dict(result.measurements or {}),
                "base_violation_vector": base_violation,
                "after_violation_vector": after_violation,
                "delta_violation_vector": delta,
                "violation_reduction": round(float(reduction), 6),
                "uncertainty": 0.10 if result.success else 0.80,
                "interpretation": _effect_interpretation(delta),
            }
        )

    return _intervention_model_from_effects(
        method="spice_small_perturbation",
        status="ok",
        target_status=target_status,
        metrics=metrics,
        base_violation=base_violation,
        effects=effects,
        perturbation_fraction=perturbation_fraction,
    )


def build_surrogate_intervention_model(
    *,
    tuning: dict[str, Any],
    target_status: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    metrics = _ordered_metrics(target_status)
    base_violation = _violation_vector(target_status, metrics)
    effects = []
    for action in _candidate_actions(tuning, limit=16):
        delta = _surrogate_delta(action, target_status, base_violation)
        reduction = _weighted_objective(base_violation, target_status) - _weighted_objective(
            {metric: max(0.0, base_violation.get(metric, 0.0) + delta.get(metric, 0.0)) for metric in metrics},
            target_status,
        )
        effects.append(
            {
                "action_id": action.get("action_id"),
                "metric": action.get("metric"),
                "knob": action.get("knob"),
                "apply_to": action.get("apply_to", []),
                "direction": action.get("direction"),
                "source": "surrogate_from_structural_action_model",
                "status": "ok",
                "delta_violation_vector": delta,
                "violation_reduction": round(float(reduction), 6),
                "uncertainty": 0.55,
                "interpretation": _effect_interpretation(delta),
            }
        )
    return _intervention_model_from_effects(
        method="surrogate_structural_prior",
        status="surrogate",
        target_status=target_status,
        metrics=metrics,
        base_violation=base_violation,
        effects=effects,
        perturbation_fraction=0.0,
    )


def optimize_tuning_actions(
    *,
    tuning: dict[str, Any],
    target_status: dict[str, dict[str, Any]],
    intervention_model: dict[str, Any] | None = None,
    max_selected_actions: int = 3,
    max_candidate_actions: int = 10,
) -> dict[str, Any]:
    metrics = _ordered_metrics(target_status)
    base_violation = (
        dict((intervention_model or {}).get("base_violation_vector") or {})
        or _violation_vector(target_status, metrics)
    )
    actions = _candidate_actions(tuning, limit=max_candidate_actions)
    effect_by_id = {
        str(effect.get("action_id")): effect
        for effect in (intervention_model or {}).get("action_effects", [])
        if effect.get("action_id") and effect.get("status") == "ok"
    }
    action_records = [_action_record(action, effect_by_id.get(str(action.get("action_id"))), target_status, base_violation) for action in actions]
    action_records = [record for record in action_records if record["action_id"]]
    planning_mode = str(tuning.get("planning_mode") or _optimizer_planning_mode(base_violation))
    has_spice_evidence = any(
        item.get("local_model_source") == "spice_small_perturbation"
        for item in action_records
    )
    searchable_records = [
        item
        for item in action_records
        if not has_spice_evidence or item.get("local_model_source") == "spice_small_perturbation"
    ]
    if not metrics or not action_records:
        return _optimizer_result(
            status="no_candidate_actions",
            metrics=metrics,
            base_violation=base_violation,
            target_status=target_status,
            candidates=action_records,
            selected=[],
            planning_mode=planning_mode,
        )

    baseline = _weighted_objective(base_violation, target_status)
    best_combo: tuple[dict[str, Any], ...] = ()
    best_objective = baseline
    max_size = max(1, min(int(max_selected_actions), len(searchable_records)))
    for size in range(1, max_size + 1):
        for combo in itertools.combinations(searchable_records, size):
            if not _combo_is_allowed(combo):
                continue
            objective = _combo_objective(combo, base_violation, target_status)
            if objective < best_objective:
                best_combo = combo
                best_objective = objective

    selected = list(best_combo)
    return _optimizer_result(
        status="ok" if selected else "no_improving_combination",
        metrics=metrics,
        base_violation=base_violation,
        target_status=target_status,
        candidates=action_records,
        selected=selected,
        objective_before=baseline,
        objective_after=best_objective,
        planning_mode=planning_mode,
    )


def apply_optimized_action_plan(tuning: dict[str, Any], optimizer_result: dict[str, Any]) -> dict[str, Any]:
    selected_ids = [item["action_id"] for item in optimizer_result.get("selected_actions", [])]
    selected_index = {action_id: idx for idx, action_id in enumerate(selected_ids)}
    candidate_by_id = {
        item["action_id"]: item
        for item in optimizer_result.get("candidate_actions", [])
        if item.get("action_id")
    }
    out = dict(tuning)
    out["decision_model"] = {
        "type": "constrained_local_action_optimizer",
        "optimizer_status": optimizer_result.get("status"),
        "admissibility_rule": ACTION_ADMISSIBILITY_RULE,
        "selected_action_ids": selected_ids,
        "objective_before": optimizer_result.get("objective_before"),
        "objective_after": optimizer_result.get("objective_after"),
        "model_source": optimizer_result.get("model_source"),
        "strategy": optimizer_result.get("strategy", {}),
    }
    by_failure = []
    for failure in tuning.get("by_failure", []):
        item = dict(failure)
        ranked_actions = []
        for action in item.get("actions", []):
            action_copy = dict(action)
            action_id = action_copy.get("action_id")
            action_copy["optimizer_selected"] = action_id in selected_index
            if action_id in candidate_by_id:
                if candidate_by_id[action_id].get("evidence_gate"):
                    action_copy["evidence_gate"] = candidate_by_id[action_id]["evidence_gate"]
                action_copy["optimizer"] = {
                    key: candidate_by_id[action_id][key]
                    for key in (
                        "optimizer_selected",
                        "objective_delta",
                        "local_model_source",
                        "predicted_violation_delta",
                        "uncertainty",
                        "constraint_penalty",
                        "evidence_gate",
                        "action_admissibility",
                        "selection_reason",
                    )
                    if key in candidate_by_id[action_id]
                }
                if candidate_by_id[action_id].get("action_admissibility"):
                    action_copy["action_admissibility"] = candidate_by_id[action_id]["action_admissibility"]
            ranked_actions.append(action_copy)
        ranked_actions.sort(
            key=lambda action: (
                0 if action.get("optimizer_selected") else 1,
                selected_index.get(action.get("action_id"), 999),
                int(action.get("rank", 999)),
            )
        )
        for rank, action in enumerate(ranked_actions, start=1):
            action["rank"] = rank
        item["actions"] = ranked_actions
        by_failure.append(item)
    out["by_failure"] = by_failure
    return out


def default_selected_actions_from_optimizer(
    state: DesignState,
    *,
    allowed_priorities: list[str] | None = None,
) -> list[dict[str, Any]] | None:
    causal = state.diagnostics.get("causal_diagnostics", {}) if state.diagnostics else {}
    optimizer = causal.get("constrained_action_optimizer", {}) or {}
    if not optimizer:
        return None
    selected = optimizer.get("selected_actions", []) or []
    allowed = set(allowed_priorities or ["primary"])
    out = []
    for item in selected:
        action_id = item.get("action_id")
        priority = item.get("priority", "primary")
        evidence_passed_guarded = priority == "guarded" and _passes_evidence_gate(item)
        if not action_id or (priority not in allowed and not evidence_passed_guarded):
            continue
        out.append(
            {
                "action_id": action_id,
                "decision": "apply",
                "reason": item.get("selection_reason", "Selected by constrained local action optimizer."),
                "overrides": {},
            }
        )
    return out


def _empty_intervention_model(
    status: str,
    target_status: dict[str, dict[str, Any]],
    metrics: list[str],
    base_violation: dict[str, float],
) -> dict[str, Any]:
    return _intervention_model_from_effects(
        method="spice_small_perturbation",
        status=status,
        target_status=target_status,
        metrics=metrics,
        base_violation=base_violation,
        effects=[],
        perturbation_fraction=0.0,
    )


def _intervention_model_from_effects(
    *,
    method: str,
    status: str,
    target_status: dict[str, dict[str, Any]],
    metrics: list[str],
    base_violation: dict[str, float],
    effects: list[dict[str, Any]],
    perturbation_fraction: float,
) -> dict[str, Any]:
    ok_effects = [effect for effect in effects if effect.get("status") == "ok"]
    columns = [str(effect.get("action_id")) for effect in ok_effects]
    matrix = [
        [
            float((effect.get("delta_violation_vector") or {}).get(metric, 0.0))
            for effect in ok_effects
        ]
        for metric in metrics
    ]
    return {
        "schema_version": INTERVENTION_SCHEMA_VERSION,
        "method": method,
        "status": status,
        "principle": "Estimate a local A matrix where each column is the measured or approximated change in normalized specification violation caused by one small action intervention.",
        "perturbation_fraction": perturbation_fraction,
        "metrics": metrics,
        "base_violation_vector": base_violation,
        "target_weights": {metric: _target_weight(target_status, metric) for metric in metrics},
        "A": {
            "rows": metrics,
            "columns": columns,
            "values": matrix,
            "sign_convention": "negative entries reduce normalized violation; positive entries worsen it",
        },
        "action_effects": effects,
    }


def action_admissibility_trace(action: dict[str, Any]) -> dict[str, Any]:
    """Return the formal executor gate for one schema action.

    The trace is intentionally recomputable by the executor. LLM text may
    explain an action, but this predicate is the authority for apply/skip.
    """

    optimizer = action.get("optimizer", {}) or {}
    selected = bool(action.get("optimizer_selected") or optimizer.get("optimizer_selected"))
    objective_delta = _optional_float(
        action.get("objective_delta")
        if action.get("objective_delta") is not None
        else optimizer.get("objective_delta")
    )
    objective_delta_negative = objective_delta is not None and objective_delta < 0.0
    guarded = action.get("priority") == "guarded"
    evidence_passed = _passes_evidence_gate(action) or _passes_evidence_gate(optimizer)
    has_optimizer_math = selected or objective_delta is not None
    passed = has_optimizer_math and (selected or objective_delta_negative) and (not guarded or evidence_passed)
    reasons: list[str] = []
    if not has_optimizer_math:
        reasons.append("no optimizer candidate math was attached to this action")
    if has_optimizer_math and not (selected or objective_delta_negative):
        reasons.append("neither optimizer_selected nor objective_delta < 0")
    if guarded and not evidence_passed:
        reasons.append("guarded action lacks passing evidence_gate")
    if passed:
        reasons.append("formal admissibility predicate passed")
    return {
        "schema_version": ACTION_ADMISSIBILITY_SCHEMA_VERSION,
        "formal_rule": ACTION_ADMISSIBILITY_RULE,
        "passed": passed,
        "conditions": {
            "has_optimizer_math": has_optimizer_math,
            "optimizer_selected": selected,
            "objective_delta_negative": objective_delta_negative,
            "guarded_evidence_passed": (not guarded) or evidence_passed,
        },
        "objective_delta": round(float(objective_delta), 6) if objective_delta is not None else None,
        "reasons": reasons,
    }


def _optimizer_result(
    *,
    status: str,
    metrics: list[str],
    base_violation: dict[str, float],
    target_status: dict[str, dict[str, Any]],
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    objective_before: float | None = None,
    objective_after: float | None = None,
    planning_mode: str = "fine",
) -> dict[str, Any]:
    if objective_before is None:
        objective_before = _weighted_objective(base_violation, target_status)
    if objective_after is None:
        objective_after = objective_before
    selected_ids = {item["action_id"] for item in selected}
    source = "none"
    if any(item.get("local_model_source") == "spice_small_perturbation" for item in candidates):
        source = "spice_small_perturbation"
    elif candidates:
        source = "surrogate_structural_prior"
    annotated = []
    for item in candidates:
        rec = dict(item)
        rec["optimizer_selected"] = rec["action_id"] in selected_ids
        if rec["optimizer_selected"]:
            rec["selection_reason"] = (
                "Selected because the constrained combination reduced the weighted normalized violation objective."
            )
        rec["action_admissibility"] = action_admissibility_trace(rec)
        annotated.append(rec)
    return {
        "schema_version": OPTIMIZER_SCHEMA_VERSION,
        "status": status,
        "problem": {
            "objective": "minimize weighted residual normalized spec violation plus action size, guard, and uncertainty penalties",
            "variables": "bounded combinations of discrete schema actions produced by causal attribution",
            "constraints": [
                "schema writable knobs only",
                "no duplicate knob writes in one combination",
                "guarded actions require passing evidence_gate from the local SPICE intervention model",
                "prefer lower uncertainty when objective improvement is similar",
                ACTION_ADMISSIBILITY_RULE,
            ],
            "formal_apply_gate": {
                "schema_version": ACTION_ADMISSIBILITY_SCHEMA_VERSION,
                "rule": ACTION_ADMISSIBILITY_RULE,
                "executor": "diagnostics.tuning.apply_attribution_guided_tuning",
            },
        },
        "strategy": {
            "name": "combo_coarse_fine",
            "planning_mode": planning_mode,
            "combo_search": "exhaustive bounded combinations over ranked causal actions",
            "hard_physical_gate": "executor validates write policy and full schema before SPICE",
        },
        "model_source": source,
        "metrics": metrics,
        "base_violation_vector": base_violation,
        "objective_before": round(float(objective_before), 6),
        "objective_after": round(float(objective_after), 6),
        "objective_improvement": round(float(objective_before - objective_after), 6),
        "selected_actions": [item for item in annotated if item.get("optimizer_selected")],
        "candidate_actions": annotated,
    }


def _candidate_actions(tuning: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    actions = [
        action
        for failure in tuning.get("by_failure", [])
        for action in failure.get("actions", [])
        if action.get("action_id")
    ]
    priority_order = {"primary": 0, "secondary": 1, "guarded": 2}
    actions = sorted(actions, key=lambda item: (priority_order.get(item.get("priority", ""), 9), int(item.get("rank", 999))))
    out = []
    seen_ids: set[str] = set()
    seen_effect_keys: set[tuple[tuple[str, ...], str]] = set()
    for action in actions:
        action_id = str(action.get("action_id"))
        effect_key = _candidate_effect_key(action)
        if action_id in seen_ids or effect_key in seen_effect_keys:
            continue
        seen_ids.add(action_id)
        seen_effect_keys.add(effect_key)
        out.append(action)
        if len(out) >= limit:
            break
    if limit > 1 and not any(item.get("priority") == "guarded" for item in out):
        guarded = next(
            (
                item
                for item in actions
                if item.get("priority") == "guarded"
                and str(item.get("action_id")) not in seen_ids
                and _candidate_effect_key(item) not in seen_effect_keys
            ),
            None,
        )
        if guarded is not None and len(out) >= limit:
            out[-1] = guarded
        elif guarded is not None:
            out.append(guarded)
    return out


def _optimizer_planning_mode(base_violation: dict[str, float]) -> str:
    max_violation = max((float(value) for value in base_violation.values()), default=0.0)
    return "coarse" if max_violation >= 0.25 else "fine"


def _candidate_effect_key(action: dict[str, Any]) -> tuple[tuple[str, ...], str]:
    knobs = tuple(str(item) for item in (action.get("apply_to") or [action.get("knob", "")]) if item)
    return (knobs, str(action.get("direction", "")))


def _action_record(
    action: dict[str, Any],
    effect: dict[str, Any] | None,
    target_status: dict[str, dict[str, Any]],
    base_violation: dict[str, float],
) -> dict[str, Any]:
    if effect is None:
        delta = _surrogate_delta(action, target_status, base_violation)
        source = "surrogate_structural_prior"
        uncertainty = 0.60
    else:
        delta = {
            metric: float(value)
            for metric, value in (effect.get("delta_violation_vector") or {}).items()
        }
        source = str(effect.get("source") or "local_intervention_model")
        uncertainty = float(effect.get("uncertainty", 0.35) or 0.35)
    residual = {
        metric: max(0.0, base_violation.get(metric, 0.0) + delta.get(metric, 0.0))
        for metric in base_violation
    }
    before = _weighted_objective(base_violation, target_status)
    after = _weighted_objective(residual, target_status)
    evidence_gate = _evidence_gate(
        action=action,
        delta=delta,
        target_status=target_status,
        base_violation=base_violation,
        local_model_source=str(source),
        uncertainty=uncertainty,
    )
    penalty = _constraint_penalty(action, delta, target_status, uncertainty, evidence_gate=evidence_gate)
    return {
        "action_id": action.get("action_id"),
        "metric": action.get("metric"),
        "priority": action.get("priority"),
        "action_class": action.get("action_class", "schema_parameter_tuning"),
        "knob": action.get("knob"),
        "apply_to": action.get("apply_to", []),
        "direction": action.get("direction"),
        "objective_delta": round(float(after - before + penalty), 6),
        "predicted_violation_delta": {metric: round(float(delta.get(metric, 0.0)), 6) for metric in base_violation},
        "local_model_source": source,
        "uncertainty": round(float(uncertainty), 4),
        "constraint_penalty": round(float(penalty), 6),
        "evidence_gate": evidence_gate,
    }


def _combo_objective(
    combo: tuple[dict[str, Any], ...],
    base_violation: dict[str, float],
    target_status: dict[str, dict[str, Any]],
) -> float:
    residual = dict(base_violation)
    penalty = 0.0
    for action in combo:
        for metric, delta in action.get("predicted_violation_delta", {}).items():
            residual[metric] = max(0.0, residual.get(metric, 0.0) + float(delta or 0.0))
        penalty += 0.012
        penalty += float(action.get("constraint_penalty", 0.0) or 0.0)
        penalty += 0.018 * float(action.get("uncertainty", 0.0) or 0.0)
    return _weighted_objective(residual, target_status) + penalty


def _combo_is_allowed(combo: tuple[dict[str, Any], ...]) -> bool:
    touched: set[str] = set()
    for action in combo:
        knobs = action.get("apply_to") or [action.get("knob")]
        for knob in knobs:
            if not knob:
                continue
            if knob in touched:
                return False
            touched.add(str(knob))
        if action.get("priority") == "guarded" and not _passes_evidence_gate(action):
            return False
    return True


def _constraint_penalty(
    action: dict[str, Any],
    delta: dict[str, float],
    target_status: dict[str, dict[str, Any]],
    uncertainty: float,
    *,
    evidence_gate: dict[str, Any] | None = None,
) -> float:
    penalty = 0.0
    if action.get("priority") == "guarded":
        penalty += 0.025 if (evidence_gate or {}).get("passed") else 0.20
    if uncertainty > 0.5:
        penalty += 0.02
    primary_metric = action.get("metric")
    for metric, status in target_status.items():
        if status.get("status") not in {"fail", "unverified"}:
            continue
        worsening = float(delta.get(metric, 0.0) or 0.0)
        if worsening > 0 and metric != primary_metric:
            penalty += min(0.25, 0.25 * worsening)
    return penalty


def _evidence_gate(
    *,
    action: dict[str, Any],
    delta: dict[str, float],
    target_status: dict[str, dict[str, Any]],
    base_violation: dict[str, float],
    local_model_source: str,
    uncertainty: float,
) -> dict[str, Any]:
    required = action.get("priority") == "guarded"
    residual = {
        metric: max(0.0, float(base_violation.get(metric, 0.0)) + float(delta.get(metric, 0.0) or 0.0))
        for metric in base_violation
    }
    before = _weighted_objective(base_violation, target_status)
    after = _weighted_objective(residual, target_status)
    improvement = before - after
    relative_improvement = improvement / max(before, 1e-12)
    thresholds = dict(EVIDENCE_GATE_THRESHOLDS)
    improvement_floor = max(
        thresholds["min_absolute_objective_improvement"],
        thresholds["min_relative_objective_improvement"] * before,
    )
    tradeoff_worsening = 0.0
    max_component_worsening = 0.0
    improved_failed_metrics = []
    for metric, base in base_violation.items():
        value_delta = float(delta.get(metric, 0.0) or 0.0)
        if value_delta < -1e-9 and target_status.get(metric, {}).get("status") in {"fail", "unverified"}:
            improved_failed_metrics.append(metric)
        if value_delta <= 0:
            continue
        max_component_worsening = max(max_component_worsening, value_delta)
        residual_if_worse = max(0.0, float(base) + value_delta)
        tradeoff_worsening += _target_weight(target_status, metric) * max(0.0, residual_if_worse**2 - float(base) ** 2)
    tradeoff_ratio = tradeoff_worsening / max(improvement, 1e-12) if improvement > 0 else float("inf")
    conditions = {
        "spice_local_intervention": local_model_source == "spice_small_perturbation",
        "objective_improvement": improvement >= improvement_floor,
        "failed_metric_reduced": bool(improved_failed_metrics),
        "bounded_tradeoff": tradeoff_ratio <= thresholds["max_tradeoff_to_improvement_ratio"],
        "bounded_component_worsening": max_component_worsening <= thresholds["max_component_worsening"],
        "bounded_uncertainty": uncertainty <= thresholds["max_uncertainty"],
    }
    reasons = []
    if not required:
        reasons.append("not required for non-guarded action")
    else:
        for name, passed in conditions.items():
            if not passed:
                reasons.append(f"failed condition: {name}")
    passed = (not required) or all(conditions.values())
    return {
        "schema_version": EVIDENCE_GATE_SCHEMA_VERSION,
        "required": required,
        "passed": passed,
        "source": local_model_source,
        "objective": "J(v)=sum_i w_i*v_i^2 with v_i as normalized spec violation; residual is [v + A_j]_+.",
        "decision_rule": "Guarded actions pass iff SPICE local intervention evidence predicts sufficient J decrease, at least one failed metric is reduced, tradeoff worsening is bounded, and uncertainty is bounded.",
        "objective_before": round(float(before), 6),
        "objective_after": round(float(after), 6),
        "objective_improvement": round(float(improvement), 6),
        "relative_improvement": round(float(relative_improvement), 6),
        "improvement_floor": round(float(improvement_floor), 6),
        "weighted_tradeoff_worsening": round(float(tradeoff_worsening), 6),
        "tradeoff_to_improvement_ratio": round(float(tradeoff_ratio), 6) if tradeoff_ratio != float("inf") else "inf",
        "max_component_worsening": round(float(max_component_worsening), 6),
        "uncertainty": round(float(uncertainty), 4),
        "improved_failed_metrics": improved_failed_metrics,
        "conditions": conditions,
        "thresholds": thresholds,
        "reasons": reasons,
    }


def _passes_evidence_gate(action: dict[str, Any]) -> bool:
    gate = action.get("evidence_gate") or {}
    return bool(gate.get("passed"))


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _surrogate_delta(
    action: dict[str, Any],
    target_status: dict[str, dict[str, Any]],
    base_violation: dict[str, float],
) -> dict[str, float]:
    delta = {metric: 0.0 for metric in base_violation}
    expected = {str(k): str(v).lower() for k, v in (action.get("expected_effect") or {}).items()}
    knob = str(action.get("knob", ""))
    variable = knob.split(".")[-1]
    direction = str(action.get("direction", ""))
    score = float(action.get("score", 0.45) or 0.45)
    for metric, violation in base_violation.items():
        if violation <= 0:
            continue
        status = target_status.get(metric, {})
        target_direction = "increase" if status.get("min") is not None else "decrease"
        effect = _expected_metric_direction(metric, variable, direction, expected)
        if effect == target_direction:
            delta[metric] = -min(violation, violation * (0.20 + 0.45 * score))
        elif effect and effect != "neutral":
            delta[metric] = min(0.35, violation * 0.35)
    return {metric: round(float(value), 6) for metric, value in delta.items()}


def _expected_metric_direction(metric: str, variable: str, direction: str, expected: dict[str, str]) -> str:
    aliases = {
        "dc_gain": ("dc_gain", "gain"),
        "unity_gain_bandwidth": ("unity_gain_bandwidth", "ugbw", "bandwidth", "speed"),
        "phase_margin": ("phase_margin", "pm", "stability"),
        "slew_rate": ("slew_rate", "slew"),
        "output_swing": ("output_swing", "swing", "headroom"),
        "power": ("power", "total_power"),
    }
    for key in aliases.get(metric, (metric,)):
        text = expected.get(key)
        if not text:
            continue
        if "increase" in text or "improve" in text:
            return "increase"
        if "decrease" in text or "reduce" in text or "fall" in text:
            return "decrease"
    if variable == "Cc":
        if metric == "phase_margin":
            return "increase" if direction == "increase" else "decrease"
        if metric in {"unity_gain_bandwidth", "slew_rate"}:
            return "decrease" if direction == "increase" else "increase"
    if variable in {"gm_id", "I_tail", "I_stage2"} and direction == "increase":
        if metric in {"dc_gain", "unity_gain_bandwidth", "slew_rate"}:
            return "increase"
        if metric == "power":
            return "increase"
    if variable == "L":
        if metric == "dc_gain" and direction == "increase":
            return "increase"
        if metric == "unity_gain_bandwidth":
            return "decrease" if direction == "increase" else "increase"
    return "neutral"


def _apply_netlist_proxy_action(
    state: DesignState,
    action: dict[str, Any],
    perturbation_fraction: float,
) -> dict[str, Any]:
    knobs = action.get("apply_to") or [action.get("knob")]
    if not knobs:
        return {"applied": False, "reason": "action has no target knobs"}
    applied = []
    for knob in knobs:
        if not isinstance(knob, str) or not knob:
            continue
        device, variable = _parse_knob(knob)
        current = _current_value(state, device, variable)
        target = _action_target_value(state, action, device, variable, current, perturbation_fraction)
        if target is None:
            continue
        target = _clip_design_value(state, device, variable, target)
        _write_design_value(state, device, variable, target, action, perturbation_fraction)
        applied.append({"knob": knob, "proxy_value": target, "previous_value": current})
    if not applied:
        return {"applied": False, "reason": "no netlist-visible proxy could be applied"}
    return {
        "applied": True,
        "proxy_policy": "global variables and L are written directly; gm_id uses a bounded W/VDSAT proxy on the current sized netlist",
        "applied_knobs": applied,
    }


def _parse_knob(knob: str) -> tuple[str, str]:
    if knob.startswith("global."):
        return "", knob.split(".", 1)[1]
    if "." not in knob:
        return "", knob
    return tuple(knob.split(".", 1))  # type: ignore[return-value]


def _current_value(state: DesignState, device: str, variable: str) -> float | None:
    if not device:
        value = state.global_parameters.get(variable)
        if value is not None:
            return float(value)
        dv = _design_variable(state, device, variable)
        return float(dv.initial) if dv and dv.initial is not None else None
    ts = state.transistors.get(device)
    dv = _design_variable(state, device, variable)
    if variable == "L" and ts and ts.parameters.L > 0:
        return float(ts.parameters.L)
    if variable == "gm_id" and ts:
        return float(ts.gm_id_strategy or ts.parameters.gm_id_realized or (dv.initial if dv else 0.0) or 0.0)
    if ts and hasattr(ts.parameters, variable):
        raw = getattr(ts.parameters, variable)
        if raw is not None:
            return float(raw)
    return float(dv.initial) if dv and dv.initial is not None else None


def _action_target_value(
    state: DesignState,
    action: dict[str, Any],
    device: str,
    variable: str,
    current: float | None,
    perturbation_fraction: float,
) -> float | None:
    explicit = action.get("suggested_unclipped_value")
    if explicit is None:
        explicit = action.get("suggested_next_value")
    if explicit is None:
        explicit = action.get("target_value")
    if explicit is not None:
        return float(explicit)
    if current is None:
        bounds = _bounds(state, device, variable)
        if bounds is None:
            return None
        return 0.5 * (bounds.min + bounds.max)
    direction = str(action.get("direction", ""))
    sign = 1.0 if direction == "increase" else -1.0 if direction == "decrease" else 0.0
    return float(current) * (1.0 + sign * abs(float(perturbation_fraction)))


def _write_design_value(
    state: DesignState,
    device: str,
    variable: str,
    value: float,
    action: dict[str, Any],
    perturbation_fraction: float,
) -> None:
    dv = _design_variable(state, device, variable)
    if dv:
        dv.initial = value
    if not device:
        state.global_parameters[variable] = value
        return
    ts = state.transistors.get(device)
    if ts is None:
        return
    if variable == "L":
        ts.L_strategy = value
        ts.parameters.L = value
    elif variable == "gm_id":
        previous = float(ts.gm_id_strategy or value)
        ts.gm_id_strategy = value
        if ts.parameters.W > 0 and previous > 0:
            direction = str(action.get("direction", ""))
            sign = 1.0 if direction == "increase" else -1.0 if direction == "decrease" else 0.0
            proxy_step = min(abs(value / max(previous, 1e-30) - 1.0), abs(float(perturbation_fraction)))
            ts.parameters.W = max(state.process.min_W, ts.parameters.W * (1.0 + sign * proxy_step))
            if ts.parameters.vdsat > 0:
                ts.parameters.vdsat = max(1e-3, ts.parameters.vdsat * (1.0 - 0.35 * sign * proxy_step))


def _clip_design_value(state: DesignState, device: str, variable: str, value: float) -> float:
    bounds = _bounds(state, device, variable)
    if bounds is None:
        return value
    return min(max(float(value), float(bounds.min)), float(bounds.max))


def _bounds(state: DesignState, device: str, variable: str) -> Range | None:
    dv = _design_variable(state, device, variable)
    if dv and dv.range:
        return dv.range
    if device and variable == "L":
        return state.constraints.get_L_range(device)
    if device and variable == "gm_id":
        return state.constraints.get_gm_id_range(device)
    return state.constraints.global_.get(variable)


def _design_variable(state: DesignState, device: str, variable: str):
    for dv in state.design_variables:
        if dv.device == device and dv.variable == variable:
            return dv
    return None


def _ordered_metrics(target_status: dict[str, dict[str, Any]]) -> list[str]:
    failed = [name for name, status in target_status.items() if status.get("status") in {"fail", "unverified"}]
    rest = [name for name in target_status if name not in failed]
    return failed + rest


def _violation_vector(target_status: dict[str, dict[str, Any]], metrics: list[str]) -> dict[str, float]:
    return {metric: round(float(_normalized_violation(target_status.get(metric, {}))), 6) for metric in metrics}


def _normalized_violation(status: dict[str, Any]) -> float:
    value = status.get("value")
    if value is None:
        return 0.15 if status.get("status") == "unverified" else 0.0
    violation = 0.0
    if status.get("min") is not None:
        ref = max(abs(float(status["min"])), 1e-30)
        violation = max(violation, max(0.0, (float(status["min"]) - float(value)) / ref))
    if status.get("max") is not None:
        ref = max(abs(float(status["max"])), 1e-30)
        violation = max(violation, max(0.0, (float(value) - float(status["max"])) / ref))
    return violation


def _weighted_objective(violation: dict[str, float], target_status: dict[str, dict[str, Any]]) -> float:
    return sum(_target_weight(target_status, metric) * float(value) ** 2 for metric, value in violation.items())


def _target_weight(target_status: dict[str, dict[str, Any]], metric: str) -> float:
    try:
        priority = int((target_status.get(metric, {}) or {}).get("priority", 1) or 1)
    except (TypeError, ValueError):
        priority = 1
    return max(0.25, 1.0 / max(priority, 1))


def _safe_action_dir(index: int, action_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in action_id)
    return f"intervention_{index:02d}_{safe[:60]}"


def _effect_interpretation(delta: dict[str, float]) -> str:
    improved = [metric for metric, value in delta.items() if value < -1e-9]
    worsened = [metric for metric, value in delta.items() if value > 1e-9]
    if improved and not worsened:
        return f"reduces normalized violation for {improved}"
    if improved and worsened:
        return f"tradeoff: improves {improved} but worsens {worsened}"
    if worsened:
        return f"worsens normalized violation for {worsened}"
    return "no measurable violation change"

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from schemas.design_state import DesignState, Range


@dataclass
class TuningApplication:
    schema_version: str = "analogrf_ir.agent_tuning_application.v0_1"
    round_index: int = 0
    applied_actions: list[dict[str, Any]] = field(default_factory=list)
    skipped_actions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "round_index": self.round_index,
            "applied_actions": self.applied_actions,
            "skipped_actions": self.skipped_actions,
        }


def apply_attribution_guided_tuning(
    state: DesignState,
    *,
    round_index: int = 0,
    max_primary_actions_per_failure: int = 3,
) -> dict[str, Any]:
    causal = state.diagnostics.get("causal_diagnostics", {}) if state.diagnostics else {}
    plan = causal.get("attribution_guided_tuning", {})
    application = TuningApplication(round_index=round_index)
    selected = _select_actions(plan, max_primary_actions_per_failure)
    for action in selected:
        if action.get("priority") == "guarded":
            application.skipped_actions.append({**_action_summary(action), "reason": "guarded action is not applied automatically"})
            continue
        result = _apply_action(state, action)
        if result["applied"]:
            application.applied_actions.append(result)
        else:
            application.skipped_actions.append(result)

    payload = application.to_dict()
    state.diagnostics.setdefault("agent_tuning_applications", []).append(payload)
    return payload


def _select_actions(plan: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_knobs: set[str] = set()
    for failure in plan.get("by_failure", []):
        actions = failure.get("actions", [])
        primary = [item for item in actions if item.get("priority") == "primary"]
        fallback = [item for item in actions if item.get("priority") == "secondary"]
        candidates = primary[:limit] if primary else fallback[:limit]
        for action in candidates:
            knob_key = ",".join(action.get("apply_to", [action.get("knob", "")]))
            if knob_key in seen_knobs:
                continue
            seen_knobs.add(knob_key)
            selected.append(action)
    return selected


def _apply_action(state: DesignState, action: dict[str, Any]) -> dict[str, Any]:
    target_knobs = action.get("apply_to") or [action.get("knob")]
    if not target_knobs:
        return {**_action_summary(action), "applied": False, "reason": "action has no target knobs"}

    applied_knobs = []
    for knob in target_knobs:
        device, variable = _parse_knob(knob)
        design_var = _find_design_variable(state, device, variable)
        if design_var is None:
            return {**_action_summary(action), "applied": False, "reason": f"schema variable not found: {knob}"}
        _apply_range_update(design_var, action)
        _apply_constraint_update(state, device, variable, design_var.range)
        next_value = _value_after_range_update(action, design_var.range)
        if next_value is None:
            return {**_action_summary(action), "applied": False, "reason": f"action has no numeric value: {knob}"}
        design_var.initial = next_value
        if device:
            _apply_device_strategy(state, device, variable, next_value)
        else:
            state.global_parameters[variable] = next_value
        applied_knobs.append(
            {
                "knob": knob,
                "new_initial": next_value,
                "range": {"min": design_var.range.min, "max": design_var.range.max},
            }
        )

    return {
        **_action_summary(action),
        "applied": True,
        "applied_knobs": applied_knobs,
    }


def _action_summary(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "metric": action.get("metric"),
        "cause_node": action.get("cause_node"),
        "priority": action.get("priority"),
        "direction": action.get("direction"),
        "knob": action.get("knob"),
        "apply_to": action.get("apply_to", []),
        "agent_step_fraction": action.get("agent_step_fraction"),
        "rationale": action.get("rationale", ""),
    }


def _parse_knob(knob: str) -> tuple[str, str]:
    if knob.startswith("global."):
        return "", knob.split(".", 1)[1]
    if "." not in knob:
        return "", knob
    device, variable = knob.split(".", 1)
    return device, variable


def _find_design_variable(state: DesignState, device: str, variable: str):
    for design_var in state.design_variables:
        if design_var.device == device and design_var.variable == variable:
            return design_var
    return None


def _apply_range_update(design_var, action: dict[str, Any]) -> None:
    update = action.get("range_update") or {}
    if update.get("type") == "expand_upper_bound" and update.get("suggested_max") is not None:
        design_var.range.max = max(float(design_var.range.max), float(update["suggested_max"]))
    elif update.get("type") == "expand_lower_bound" and update.get("suggested_min") is not None:
        design_var.range.min = min(float(design_var.range.min), float(update["suggested_min"]))


def _apply_constraint_update(state: DesignState, device: str, variable: str, updated_range: Range) -> None:
    if not device:
        if variable in state.constraints.global_:
            state.constraints.global_[variable] = Range(updated_range.min, updated_range.max)
        return
    constraint = state.constraints.per_device.get(device)
    if constraint is None:
        return
    if variable == "gm_id" and constraint.gm_id is not None:
        constraint.gm_id = Range(updated_range.min, updated_range.max)
    elif variable == "L" and constraint.L is not None:
        constraint.L = Range(updated_range.min, updated_range.max)


def _value_after_range_update(action: dict[str, Any], range_: Range) -> float | None:
    raw = action.get("suggested_unclipped_value")
    if raw is None:
        raw = action.get("suggested_next_value")
    if raw is None and action.get("target_value") is not None:
        raw = action["target_value"]
    if raw is None:
        return None
    value = float(raw)
    return min(max(value, float(range_.min)), float(range_.max))


def _apply_device_strategy(state: DesignState, device: str, variable: str, value: float) -> None:
    transistor = state.transistors.get(device)
    if transistor is None:
        return
    if variable == "gm_id":
        transistor.gm_id_strategy = value
    elif variable == "L":
        transistor.L_strategy = value


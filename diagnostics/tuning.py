from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from schemas.design_state import DesignState, Range


@dataclass
class TuningApplication:
    schema_version: str = "analogrf_ir.agent_tuning_application.v0_1"
    round_index: int = 0
    command_id: str = ""
    applied_actions: list[dict[str, Any]] = field(default_factory=list)
    skipped_actions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "round_index": self.round_index,
            "command_id": self.command_id,
            "applied_actions": self.applied_actions,
            "skipped_actions": self.skipped_actions,
        }


def write_tuning_tool_command(
    state: DesignState,
    *,
    round_index: int,
    author: str = "llm_schema_planner",
    max_primary_actions_per_failure: int = 3,
    allowed_priorities: list[str] | None = None,
    selected_actions: list[dict[str, Any]] | None = None,
    custom_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    available_actions = _available_actions(state)
    priorities = allowed_priorities or ["primary"]
    selected = selected_actions
    if selected is None:
        selected = _default_selected_actions(
            available_actions,
            limit=max_primary_actions_per_failure,
            allowed_priorities=priorities,
        )
    command = {
        "schema_version": "analogrf_ir.agent_tool_command.v0_1",
        "id": f"tuning_round_{round_index:03d}",
        "author": author,
        "tool": "apply_attribution_guided_tuning",
        "status": "requested",
        "round_index": round_index,
        "state_source": "design_state.yaml:diagnostics.causal_diagnostics.attribution_guided_tuning",
        "args": {
            "max_primary_actions_per_failure": max_primary_actions_per_failure,
            "allowed_priorities": priorities,
            "available_actions": available_actions,
            "selected_actions": selected,
            "custom_actions": custom_actions or [],
        },
        "llm_editable_fields": {
            "selected_actions": [
                "action_id",
                "decision",
                "overrides.apply_to",
                "overrides.direction",
                "overrides.suggested_next_value",
                "overrides.suggested_unclipped_value",
                "overrides.agent_step_fraction",
                "overrides.range_update",
                "overrides.rationale",
                "reason",
            ],
            "custom_actions": [
                "action_id",
                "decision",
                "knob",
                "apply_to",
                "suggested_next_value",
                "suggested_unclipped_value",
                "range_update",
                "direction",
                "metric",
                "priority",
                "reason",
            ],
            "decision_values": ["apply", "skip"],
            "range_update_types": ["expand_upper_bound", "expand_lower_bound", "set_range"],
            "notes": "Select existing actions by action_id or add custom per-knob actions. The executor only applies actions with decision=apply.",
        },
        "rationale": "Call the tuning executor through schema command state instead of direct in-memory invocation.",
    }
    commands = state.diagnostics.setdefault("agent_tool_commands", [])
    commands.append(command)
    return command


def execute_tuning_tool_commands(state: DesignState, *, round_index: int = 0) -> dict[str, Any]:
    commands = state.diagnostics.get("agent_tool_commands", [])
    command = next(
        (
            item for item in reversed(commands)
            if item.get("tool") == "apply_attribution_guided_tuning" and item.get("status") == "requested"
        ),
        None,
    )
    if command is None:
        application = TuningApplication(round_index=round_index)
        application.skipped_actions.append({"applied": False, "reason": "no requested tuning command found"})
        payload = application.to_dict()
        state.diagnostics.setdefault("agent_tuning_applications", []).append(payload)
        return payload

    args = command.get("args", {})
    application = apply_attribution_guided_tuning(
        state,
        round_index=int(command.get("round_index", round_index)),
        command_id=str(command.get("id", "")),
        max_primary_actions_per_failure=int(args.get("max_primary_actions_per_failure", 3)),
        allowed_priorities=_as_string_list(args.get("allowed_priorities"), ["primary"]),
        selected_actions=args.get("selected_actions") if isinstance(args.get("selected_actions"), list) else None,
        custom_actions=args.get("custom_actions") if isinstance(args.get("custom_actions"), list) else None,
    )
    command["status"] = "executed" if application.get("applied_actions") else "skipped"
    command["application"] = application
    return application


def apply_attribution_guided_tuning(
    state: DesignState,
    *,
    round_index: int = 0,
    command_id: str = "",
    max_primary_actions_per_failure: int = 3,
    allowed_priorities: list[str] | None = None,
    selected_actions: list[dict[str, Any]] | None = None,
    custom_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    causal = state.diagnostics.get("causal_diagnostics", {}) if state.diagnostics else {}
    plan = causal.get("attribution_guided_tuning", {})
    application = TuningApplication(round_index=round_index, command_id=command_id)
    if selected_actions is None:
        selected = _select_actions(plan, max_primary_actions_per_failure, allowed_priorities or ["primary", "secondary"])
    else:
        selected = _actions_from_llm_selection(plan, selected_actions)
    selected.extend(_actions_from_custom_actions(custom_actions or []))
    for action in selected:
        if action.get("_selection_error"):
            application.skipped_actions.append({**_action_summary(action), "reason": action["_selection_error"]})
            continue
        if action.get("llm_decision", "apply") != "apply":
            application.skipped_actions.append({**_action_summary(action), "reason": action.get("llm_reason", "LLM skipped action")})
            continue
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


def _available_actions(state: DesignState) -> list[dict[str, Any]]:
    causal = state.diagnostics.get("causal_diagnostics", {}) if state.diagnostics else {}
    plan = causal.get("attribution_guided_tuning", {})
    return [
        _action_for_llm(action)
        for failure in plan.get("by_failure", [])
        for action in failure.get("actions", [])
    ]


def _action_for_llm(action: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_id": action.get("action_id"),
        "metric": action.get("metric"),
        "rank": action.get("rank"),
        "priority": action.get("priority"),
        "knob": action.get("knob"),
        "apply_to": action.get("apply_to", []),
        "direction": action.get("direction"),
        "current_value": action.get("current_value"),
        "suggested_next_value": action.get("suggested_next_value"),
        "suggested_unclipped_value": action.get("suggested_unclipped_value"),
        "agent_step_fraction": action.get("agent_step_fraction"),
        "range": action.get("range"),
        "range_update": action.get("range_update"),
        "expected_effect": action.get("expected_effect", {}),
        "tradeoffs": action.get("tradeoffs", []),
        "rationale": action.get("rationale", ""),
    }


def _default_selected_actions(actions: list[dict[str, Any]], *, limit: int, allowed_priorities: list[str]) -> list[dict[str, Any]]:
    selected = []
    seen_knobs = set()
    allowed = set(allowed_priorities)
    for action in actions:
        if action.get("priority") not in allowed:
            continue
        knob_key = ",".join(action.get("apply_to", [action.get("knob", "")]))
        if knob_key in seen_knobs:
            continue
        seen_knobs.add(knob_key)
        selected.append(
            {
                "action_id": action.get("action_id"),
                "decision": "apply",
                "reason": "Selected by default deterministic schema planner.",
                "overrides": {},
            }
        )
        if len(selected) >= limit:
            break
    return selected


def _actions_from_llm_selection(plan: dict[str, Any], selected_actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {
        action.get("action_id"): action
        for failure in plan.get("by_failure", [])
        for action in failure.get("actions", [])
        if action.get("action_id")
    }
    out = []
    for selected in selected_actions:
        action_id = selected.get("action_id")
        base = by_id.get(action_id)
        if base is None:
            out.append(
                {
                    "action_id": action_id,
                    "priority": selected.get("priority", "unknown"),
                    "decision": selected.get("decision", "skip"),
                    "knob": selected.get("knob"),
                    "apply_to": selected.get("apply_to", []),
                    "rationale": selected.get("reason", ""),
                    "_selection_error": f"unknown action_id: {action_id}",
                }
            )
            continue
        action = dict(base)
        action["llm_decision"] = selected.get("decision", "apply")
        action["llm_reason"] = selected.get("reason", "")
        overrides = selected.get("overrides", {}) or {}
        for key in (
            "apply_to",
            "direction",
            "suggested_next_value",
            "suggested_unclipped_value",
            "agent_step_fraction",
            "range_update",
            "rationale",
        ):
            if key in overrides:
                action[key] = overrides[key]
        out.append(action)
    return out


def _actions_from_custom_actions(custom_actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for index, custom in enumerate(custom_actions, start=1):
        knob = custom.get("knob")
        apply_to = custom.get("apply_to") or ([knob] if knob else [])
        action_id = custom.get("action_id") or _custom_action_id(index, apply_to)
        out.append(
            {
                "action_id": action_id,
                "metric": custom.get("metric", "llm_direct_tuning"),
                "cause_node": custom.get("cause_node", "llm_schema_command"),
                "priority": custom.get("priority", "primary"),
                "direction": custom.get("direction", "set"),
                "knob": knob or (apply_to[0] if apply_to else ""),
                "apply_to": apply_to,
                "suggested_next_value": custom.get("suggested_next_value"),
                "suggested_unclipped_value": custom.get("suggested_unclipped_value", custom.get("value")),
                "agent_step_fraction": custom.get("agent_step_fraction"),
                "range_update": custom.get("range_update"),
                "rationale": custom.get("reason", custom.get("rationale", "LLM requested direct schema tuning.")),
                "llm_decision": custom.get("decision", "apply"),
                "llm_reason": custom.get("reason", ""),
            }
        )
    return out


def _custom_action_id(index: int, apply_to: list[str]) -> str:
    knob = apply_to[0] if apply_to else "knob"
    return f"custom_{index:03d}_{knob.replace('.', '_')}"


def _select_actions(plan: dict[str, Any], limit: int, allowed_priorities: list[str]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_knobs: set[str] = set()
    allowed = set(allowed_priorities)
    for failure in plan.get("by_failure", []):
        actions = failure.get("actions", [])
        candidates = [item for item in actions if item.get("priority") in allowed][:limit]
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
        "action_id": action.get("action_id"),
        "metric": action.get("metric"),
        "cause_node": action.get("cause_node"),
        "priority": action.get("priority"),
        "direction": action.get("direction"),
        "knob": action.get("knob"),
        "apply_to": action.get("apply_to", []),
        "agent_step_fraction": action.get("agent_step_fraction"),
        "llm_decision": action.get("llm_decision"),
        "llm_reason": action.get("llm_reason", ""),
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
    elif update.get("type") == "set_range":
        if update.get("min") is not None:
            design_var.range.min = float(update["min"])
        if update.get("max") is not None:
            design_var.range.max = float(update["max"])


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


def _as_string_list(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return default

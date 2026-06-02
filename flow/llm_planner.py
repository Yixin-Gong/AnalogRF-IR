from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from diagnostics import write_tuning_tool_command
from schemas.design_state import DesignState


@dataclass
class LLMPlannerConfig:
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"
    timeout_seconds: float = 45.0
    temperature: float = 0.0
    max_tokens: int = 1400
    thinking: str = "disabled"
    reasoning_effort: str = ""

    @classmethod
    def from_env(
        cls,
        *,
        provider: str = "deepseek",
        model: str | None = None,
        base_url: str | None = None,
        api_key_env: str = "DEEPSEEK_API_KEY",
        timeout_seconds: float | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking: str | None = None,
        reasoning_effort: str | None = None,
    ) -> "LLMPlannerConfig":
        selected_provider = os.environ.get("ANALOGRF_IR_LLM_PROVIDER", provider)
        selected_model = model or os.environ.get("ANALOGRF_IR_LLM_MODEL") or "deepseek-v4-flash"
        selected_base_url = (
            base_url
            or os.environ.get("ANALOGRF_IR_LLM_BASE_URL")
            or os.environ.get("DEEPSEEK_BASE_URL")
            or "https://api.deepseek.com"
        )
        selected_key_env = os.environ.get("ANALOGRF_IR_LLM_API_KEY_ENV", api_key_env)
        return cls(
            provider=selected_provider,
            model=selected_model,
            base_url=selected_base_url,
            api_key_env=selected_key_env,
            timeout_seconds=(
                timeout_seconds
                if timeout_seconds is not None
                else float(os.environ.get("ANALOGRF_IR_LLM_TIMEOUT_SECONDS", "45"))
            ),
            temperature=(
                temperature
                if temperature is not None
                else float(os.environ.get("ANALOGRF_IR_LLM_TEMPERATURE", "0"))
            ),
            max_tokens=(
                max_tokens
                if max_tokens is not None
                else int(os.environ.get("ANALOGRF_IR_LLM_MAX_TOKENS", "1400"))
            ),
            thinking=thinking or os.environ.get("ANALOGRF_IR_LLM_THINKING", "disabled"),
            reasoning_effort=reasoning_effort or os.environ.get("ANALOGRF_IR_LLM_REASONING_EFFORT", ""),
        )


@dataclass
class LLMPlannerResult:
    command: dict[str, Any]
    used_llm: bool
    status: str
    reason: str


class DeepSeekSchemaPlanner:
    def __init__(self, config: LLMPlannerConfig | None = None) -> None:
        self.config = config or LLMPlannerConfig.from_env()

    def write_command(self, state: DesignState, *, round_index: int, agent_model: dict[str, Any] | None = None) -> LLMPlannerResult:
        command = write_tuning_tool_command(
            state,
            round_index=round_index,
            author=f"llm:{self.config.provider}:{self.config.model}",
            max_primary_actions_per_failure=3,
            allowed_priorities=["primary"],
        )
        api_key = os.environ.get(self.config.api_key_env, "")
        if self.config.provider == "deterministic":
            return self._mark_fallback(command, "deterministic provider requested")
        if not api_key:
            return self._mark_fallback(command, f"{self.config.api_key_env} is not set")

        try:
            planner_payload = self._call_planner(command, agent_model or {}, api_key)
            self._apply_planner_payload(command, planner_payload)
            command["llm_planner"] = {
                "provider": self.config.provider,
                "model": self.config.model,
                "thinking": self.config.thinking,
                "reasoning_effort": self.config.reasoning_effort,
                "temperature": self.config.temperature,
                "max_tokens": self.config.max_tokens,
                "status": "ok",
                "reason": planner_payload.get("rationale", "LLM returned a schema tuning command."),
            }
            return LLMPlannerResult(command=command, used_llm=True, status="ok", reason="LLM command accepted")
        except (ValueError, urllib.error.URLError, TimeoutError) as exc:
            return self._mark_fallback(command, f"LLM planner failed: {exc}")

    def _call_planner(self, command: dict[str, Any], agent_model: dict[str, Any], api_key: str) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(_planner_context(command, agent_model), indent=2)},
        ]
        body = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
            "thinking": {"type": self.config.thinking},
        }
        if self.config.thinking == "enabled" and self.config.reasoning_effort:
            body["reasoning_effort"] = self.config.reasoning_effort
        request = urllib.request.Request(
            _completion_url(self.config.base_url),
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
            raw = response.read().decode("utf-8")
        data = json.loads(raw)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"unexpected chat completion response: {exc}") from exc
        return _loads_json_object(content)

    def _apply_planner_payload(self, command: dict[str, Any], payload: dict[str, Any]) -> None:
        args = command.setdefault("args", {})
        if isinstance(payload.get("selected_actions"), list):
            args["selected_actions"] = payload["selected_actions"]
        if isinstance(payload.get("custom_actions"), list):
            args["custom_actions"] = payload["custom_actions"]
        command["llm_notes"] = payload.get("notes", "")
        command["llm_rationale"] = payload.get("rationale", "")

    def _mark_fallback(self, command: dict[str, Any], reason: str) -> LLMPlannerResult:
        command["llm_planner"] = {
            "provider": self.config.provider,
            "model": self.config.model,
            "thinking": self.config.thinking,
            "reasoning_effort": self.config.reasoning_effort,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "status": "fallback",
            "reason": reason,
        }
        return LLMPlannerResult(command=command, used_llm=False, status="fallback", reason=reason)


def _planner_context(command: dict[str, Any], agent_model: dict[str, Any]) -> dict[str, Any]:
    args = command.get("args", {})
    actions = _rank_planner_actions(args.get("available_actions", []))
    return {
        "task": "Write a schema-level tuning command for the next analog optimization round.",
        "rules": [
            "Return JSON only.",
            "Use selected_actions for action_id values that already exist in available_actions.",
            "Use custom_actions only as notes or when no constrained optimizer evidence is active; never use them to bypass the formal apply gate.",
            "Every action must include decision='apply' or decision='skip'.",
            "Prefer a small number of high-confidence actions per round.",
            "Prefer actions backed by causal_root_causes structural paths and intervention impact.",
            "Use the combo_coarse_fine strategy: coarse actions can take larger schema-safe steps when violations are large; fine actions should be small near feasibility.",
            "Favor compatible action combinations selected by the constrained_action_optimizer instead of hand-picking isolated knobs when optimizer evidence is available.",
            "An action may use decision='apply' only when action_admissibility.passed is true, or when optimizer_selected is true, or when optimizer.objective_delta < 0.",
            "If constrained_action_optimizer.status is no_improving_combination, apply only existing actions whose action_admissibility.passed is true or optimizer.objective_delta < 0; otherwise return skip decisions or notes.",
            "A priority='guarded' action may be selected with decision='apply' only when evidence_gate.passed is true.",
            "If a guarded action lacks a passing evidence_gate, skip it or mention it in notes; the executor rejects it without local SPICE intervention evidence.",
            "Do not use legacy_sensitivity_top as the final decision rule when it diverges from causal_top.",
            "All reasons and notes must be written in English.",
        ],
        "output_schema": {
            "selected_actions": [
                {
                    "action_id": "existing action_id",
                    "decision": "apply | skip",
                    "reason": "short English reason",
                    "overrides": {
                        "suggested_unclipped_value": "optional number",
                        "range_update": "optional range update object",
                    },
                }
            ],
            "custom_actions": [
                {
                    "action_id": "stable custom id",
                    "decision": "apply | skip",
                    "knob": "device.variable or global.variable",
                    "suggested_unclipped_value": "number",
                    "range_update": {"type": "set_range | expand_upper_bound | expand_lower_bound"},
                    "reason": "short English reason",
                }
            ],
            "rationale": "short English explanation",
            "notes": "optional English notes",
        },
        "design_state": _compact_agent_model(agent_model),
        "available_actions": [_compact_action_for_planner(action) for action in actions[:10]],
        "default_selected_actions": _compact_selected_actions(args.get("selected_actions", [])),
        "formal_apply_gate": (command.get("write_policy", {}) or {}).get("action_admissibility", {}),
        "editable_scope": _compact_editable_fields(command.get("llm_editable_fields", {})),
    }


def _rank_planner_actions(actions: Any) -> list[dict[str, Any]]:
    if not isinstance(actions, list):
        return []
    typed = [action for action in actions if isinstance(action, dict)]

    def score(action: dict[str, Any]) -> tuple[int, float, str]:
        admissibility = action.get("action_admissibility", {}) or {}
        optimizer = action.get("optimizer", {}) or {}
        passed = bool(admissibility.get("passed"))
        selected = bool(
            action.get("optimizer_selected")
            or optimizer.get("optimizer_selected")
            or optimizer.get("selected")
        )
        objective_delta = _as_float(
            admissibility.get("objective_delta", optimizer.get("objective_delta")),
            default=float("inf"),
        )
        priority = str(action.get("priority") or "")
        rank = 0
        if selected:
            rank -= 60
        if passed:
            rank -= 40
        if objective_delta < 0.0:
            rank -= 25
        if priority == "primary":
            rank -= 8
        elif priority == "guarded":
            rank += 8
        return (rank, objective_delta, str(action.get("action_id") or ""))

    return sorted(typed, key=score)


def _compact_agent_model(agent_model: dict[str, Any]) -> dict[str, Any]:
    status = agent_model.get("status", {}) if isinstance(agent_model, dict) else {}
    optimizer = agent_model.get("constrained_action_optimizer", {}) if isinstance(agent_model, dict) else {}
    intervention = agent_model.get("local_intervention_model", {}) if isinstance(agent_model, dict) else {}
    return {
        "state_source": agent_model.get("state_source", "") if isinstance(agent_model, dict) else "",
        "status": _compact_status(status),
        "failed_targets": list(agent_model.get("failed_targets", []) or [])[:8] if isinstance(agent_model, dict) else [],
        "local_intervention": {
            "method": intervention.get("method", ""),
            "status": intervention.get("status", ""),
            "action_count": intervention.get("action_count"),
            "ok_action_count": intervention.get("ok_action_count"),
            "base_violation_vector": intervention.get("base_violation_vector", {}),
            "evidence_location": intervention.get("evidence_location", ""),
        },
        "constrained_action_optimizer": {
            "status": optimizer.get("status", ""),
            "model_source": optimizer.get("model_source", ""),
            "objective_before": optimizer.get("objective_before"),
            "objective_after": optimizer.get("objective_after"),
            "selected_actions": optimizer.get("selected_actions", [])[:5],
        },
        "tuning_failures": [
            {
                "metric": item.get("metric"),
                "strategy": item.get("strategy"),
                "actions": item.get("actions", [])[:2],
            }
            for item in (agent_model.get("tuning_failures", []) if isinstance(agent_model, dict) else [])[:6]
            if isinstance(item, dict)
        ],
    }


def _compact_status(status: Any) -> dict[str, Any]:
    if not isinstance(status, dict):
        return {}
    keep = (
        "spec_pass",
        "failed_targets",
        "measured_violation_score",
        "dc_gain_db",
        "unity_gain_bandwidth",
        "phase_margin",
        "slew_rate",
        "output_swing",
        "total_power",
        "saturation_margin",
    )
    return {key: status[key] for key in keep if key in status}


def _compact_action_for_planner(action: dict[str, Any]) -> dict[str, Any]:
    optimizer = action.get("optimizer", {}) if isinstance(action.get("optimizer"), dict) else {}
    admissibility = (
        action.get("action_admissibility", {})
        if isinstance(action.get("action_admissibility"), dict)
        else {}
    )
    evidence_gate = action.get("evidence_gate", {}) if isinstance(action.get("evidence_gate"), dict) else {}
    out = {
        "action_id": action.get("action_id"),
        "decision_scope": "schema_action_candidate",
        "priority": action.get("priority"),
        "metric": action.get("metric"),
        "action_class": action.get("action_class") or action.get("class"),
        "knob": action.get("knob"),
        "apply_to": action.get("apply_to"),
        "direction": action.get("direction"),
        "current_value": action.get("current_value"),
        "suggested_next_value": action.get("suggested_next_value"),
        "per_knob_values": action.get("per_knob_values"),
        "range_update": action.get("range_update"),
        "optimizer_selected": bool(
            action.get("optimizer_selected")
            or optimizer.get("optimizer_selected")
            or optimizer.get("selected")
        ),
        "admissibility": _compact_gate(admissibility),
        "optimizer": {
            "status": optimizer.get("status"),
            "objective_delta": optimizer.get("objective_delta"),
            "model_source": optimizer.get("model_source") or optimizer.get("local_model_source"),
        },
        "evidence_gate": _compact_gate(evidence_gate),
        "expected_effect": _compact_expected_effect(action.get("expected_effect", {})),
        "reason": _short_text(action.get("rationale") or action.get("reason") or ""),
    }
    return {key: value for key, value in out.items() if value not in (None, "", [], {})}


def _compact_gate(gate: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(gate, dict):
        return {}
    out = {
        key: gate[key]
        for key in ("passed", "objective_delta", "formal_rule", "status")
        if key in gate
    }
    conditions = gate.get("conditions")
    if isinstance(conditions, dict):
        out["conditions"] = {
            key: conditions.get(key)
            for key in (
                "physical_gate",
                "optimizer_selected",
                "objective_delta_negative",
                "objective_delta_source_trusted",
                "guarded_evidence_passed",
            )
            if key in conditions
        }
    reasons = gate.get("reasons")
    if isinstance(reasons, list):
        out["reasons"] = [_short_text(str(item), 160) for item in reasons[:3]]
    return out


def _compact_expected_effect(effect: Any) -> dict[str, Any]:
    if not isinstance(effect, dict):
        return {}
    return {
        key: effect[key]
        for key in ("improves", "may_hurt", "target_metric", "polarity")
        if key in effect
    }


def _compact_selected_actions(actions: Any) -> list[dict[str, Any]]:
    if not isinstance(actions, list):
        return []
    out: list[dict[str, Any]] = []
    for item in actions[:8]:
        if isinstance(item, dict):
            out.append(
                {
                    key: item[key]
                    for key in ("action_id", "decision", "reason", "overrides")
                    if key in item
                }
            )
    return out


def _compact_editable_fields(fields: Any) -> dict[str, Any]:
    if not isinstance(fields, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("globals", "devices"):
        value = fields.get(key)
        if isinstance(value, list):
            out[key] = value[:16]
        elif isinstance(value, dict):
            out[key] = {str(k): v for k, v in list(value.items())[:16]}
    return out


def _short_text(value: Any, limit: int = 220) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text[:limit]


def _as_float(value: Any, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _completion_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def _loads_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        extracted = _extract_first_json_object(text)
        if extracted is None:
            prefix = text[:160].replace("\n", "\\n")
            raise ValueError(f"LLM response was not parseable JSON; prefix={prefix!r}") from exc
        try:
            data = json.loads(extracted)
        except json.JSONDecodeError as nested_exc:
            prefix = extracted[:160].replace("\n", "\\n")
            raise ValueError(f"LLM JSON object extraction failed; prefix={prefix!r}") from nested_exc
    if not isinstance(data, dict):
        raise ValueError("LLM response must be a JSON object")
    return data


def _extract_first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


_SYSTEM_PROMPT = """You are an analog circuit tuning planner.
You read structure-aware causal diagnostics and write the next schema-level tuning command.
You are not executing simulations and you are not editing files directly.
Return one JSON object only. Do not include Markdown.
Use conservative, testable tuning moves based on directed structural paths, intervention-impact intuition, and propagation evidence.
Do not rank or choose actions purely from raw gm/W/L sensitivity. Treat any legacy sensitivity comparison only as a debugging prior.
Prefer actions that target root-cause nodes where a small intervention should reduce the reported specification violation.
"""

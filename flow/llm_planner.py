from __future__ import annotations

import json
import http.client
import os
import socket
import time
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
    max_retries: int = 2
    retry_backoff_seconds: float = 1.0

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
            max_retries=max(0, int(os.environ.get("ANALOGRF_IR_LLM_MAX_RETRIES", "2"))),
            retry_backoff_seconds=max(0.0, float(os.environ.get("ANALOGRF_IR_LLM_RETRY_BACKOFF_SECONDS", "1.0"))),
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
            transport = planner_payload.pop("_planner_transport", None)
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
            if isinstance(transport, dict) and transport:
                command["llm_planner"]["transport"] = transport
            return LLMPlannerResult(command=command, used_llm=True, status="ok", reason="LLM command accepted")
        except (
            ValueError,
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            OSError,
            http.client.HTTPException,
        ) as exc:
            return self._mark_fallback(command, f"LLM planner failed: {exc}")

    def _call_planner(self, command: dict[str, Any], agent_model: dict[str, Any], api_key: str) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(_planner_context(command, agent_model), indent=2)},
        ]
        body = self._request_body(messages, thinking=self.config.thinking, max_tokens=self.config.max_tokens)
        data = self._post_chat_completion(body, api_key)
        content = _completion_content(data)
        try:
            payload = _loads_json_object(content)
            payload["_planner_transport"] = {
                "retry": False,
                "finish_reason": _completion_finish_reason(data),
            }
            return payload
        except ValueError as first_exc:
            if self.config.thinking == "enabled":
                retry_body = self._request_body(
                    messages,
                    thinking="disabled",
                    max_tokens=min(max(self.config.max_tokens, 1), 2048),
                )
                retry_data = self._post_chat_completion(retry_body, api_key)
                retry_content = _completion_content(retry_data)
                try:
                    payload = _loads_json_object(retry_content)
                    payload["_planner_transport"] = {
                        "retry": True,
                        "retry_reason": str(first_exc),
                        "first_finish_reason": _completion_finish_reason(data),
                        "retry_finish_reason": _completion_finish_reason(retry_data),
                    }
                    return payload
                except ValueError as retry_exc:
                    raise ValueError(
                        f"{retry_exc}; first_completion={_completion_debug(data)}; "
                        f"retry_completion={_completion_debug(retry_data)}"
                    ) from retry_exc
            raise ValueError(f"{first_exc}; completion={_completion_debug(data)}") from first_exc

    def _request_body(self, messages: list[dict[str, str]], *, thinking: str, max_tokens: int) -> dict[str, Any]:
        body = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
            "thinking": {"type": thinking},
        }
        if thinking == "enabled" and self.config.reasoning_effort:
            body["reasoning_effort"] = self.config.reasoning_effort
        return body

    def _post_chat_completion(self, body: dict[str, Any], api_key: str) -> dict[str, Any]:
        attempts = max(1, int(self.config.max_retries) + 1)
        transient_errors: tuple[type[BaseException], ...] = (
            TimeoutError,
            ConnectionError,
            OSError,
            socket.timeout,
            urllib.error.URLError,
            http.client.HTTPException,
        )
        last_exc: BaseException | None = None
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(
                _completion_url(self.config.base_url),
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                    raw = response.read().decode("utf-8")
                return json.loads(raw)
            except transient_errors as exc:
                last_exc = exc
                if attempt >= attempts:
                    break
                time.sleep(self.config.retry_backoff_seconds * (2 ** (attempt - 1)))
        assert last_exc is not None
        raise last_exc

    def _apply_planner_payload(self, command: dict[str, Any], payload: dict[str, Any]) -> None:
        args = command.setdefault("args", {})
        if isinstance(payload.get("selected_actions"), list):
            args["selected_actions"] = [
                item
                for item in payload["selected_actions"]
                if isinstance(item, dict) and str(item.get("decision", "apply")).lower() == "apply"
            ]
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
    policy = str(agent_model.get("llm_policy", "auto") if isinstance(agent_model, dict) else "auto").strip().lower()
    status = agent_model.get("status", {}) if isinstance(agent_model, dict) else {}
    failed_targets = list(agent_model.get("failed_targets", []) or []) if isinstance(agent_model, dict) else []
    unverified_targets = list(agent_model.get("unverified_targets", []) or []) if isinstance(agent_model, dict) else []
    unresolved_targets = bool(not status.get("spec_pass", False) or failed_targets or unverified_targets)
    residual_rules = []
    if policy in {"residual", "residual_escape"}:
        residual_rules = [
            "You are in residual LLM audit mode: existing available_actions are evidence only; selected_actions for existing action_id values will be recorded as audit and ignored by the executor.",
            "In residual mode, if spec_pass is false or any failed_targets/unverified_targets are present, return one or two custom_actions with decision='apply' unless schema_context.writable_variables has no relevant safe knob.",
            "Do not return custom_actions: [] merely because available_actions look sufficient; express your incremental hypothesis as schema-context writable knobs with suggested_unclipped_value or per_knob_values.",
            "Residual custom_actions must be locally testable hypotheses; the executor will run SPICE probes and apply them only if the formal objective improves.",
        ]
        if policy == "residual_escape":
            residual_rules.extend(
                [
                    "You are in residual_escape mode: custom_actions are exploratory schema patches, not optimizer-selected actions.",
                    "In residual_escape mode, you may propose an unusual but physically bounded multi-knob perturbation to escape a local optimum; the executor will reject it unless measured SPICE validation reduces J.",
                    "Use per_knob_values for coupled escape moves so the patch is executable as one composite hypothesis.",
                ]
            )
    return {
        "task": "Write a schema-level tuning command for the next analog optimization round.",
        "planner_mode": policy,
        "residual_hypothesis_required": bool(policy in {"residual", "residual_escape"} and unresolved_targets),
        "rules": residual_rules + [
            "Return JSON only.",
            "Use selected_actions for action_id values that already exist in available_actions.",
            "Use custom_actions as typed action hypotheses; they will be SPICE-probed and optimizer-gated before execution.",
            "When available_actions is empty, all candidates are inadmissible, or existing actions address only part of the failed target tradeoff, propose at most two custom_actions using only knobs listed in schema_context.writable_variables.",
            "When admissible existing actions are present, you may still add one custom_action as an alternative hypothesis if it targets a coupled gain-bandwidth-headroom or slew-headroom tradeoff not covered by the existing action set.",
            "Custom action values must be local trust-region hypotheses: use roughly 10-30 mV moves for bias voltages, modest current changes, and small geometry/range moves. Do not jump bias ports to mid-range values unless the current value is already near that value.",
            "For telescopic or folded cascode headroom failures, prefer one composite custom_action with per_knob_values over isolated single-knob hypotheses; include coupled bias and device-length knobs when they are writable.",
            "For telescopic load-cascode saturation failures, a useful hypothesis may keep NMOS cascode bias near its current/high value, raise PMOS cascode bias only slightly to reduce PMOS overdrive, and adjust tail bias/current conservatively; do not propose only I_tail or a large vbias_pcas jump unless no coupled writable knobs exist.",
            "If slew_rate is a target, assume every bias/current/geometry custom_action will be transient-probed; do not trade away slew_rate to fix headroom.",
            "Treat unverified_targets as required unresolved specifications, even when failed_targets is empty.",
            "If spec_pass is false and failed_targets is empty but unverified_targets is non-empty, propose a hypothesis for an unverified target instead of saying no tuning is required.",
            "selected_actions must contain only entries with decision='apply'. Never include skipped existing actions.",
            "If no existing action is admissible, return selected_actions: [] and explain briefly in rationale or notes.",
            "Prefer a small number of high-confidence actions per round.",
            "Prefer actions backed by causal_root_causes structural paths and intervention impact.",
            "Use the combo_coarse_fine strategy: coarse actions can take larger schema-safe steps when violations are large; fine actions should be small near feasibility.",
            "Favor compatible action combinations selected by the constrained_action_optimizer instead of hand-picking isolated knobs when optimizer evidence is available.",
            "An action may use decision='apply' only when action_admissibility.passed is true, or when optimizer_selected is true, or when optimizer.objective_delta < 0.",
            "If constrained_action_optimizer.status is no_improving_combination, apply only existing actions whose action_admissibility.passed is true or optimizer.objective_delta < 0; otherwise return skip decisions or notes.",
            "When constrained_action_optimizer.status is no_improving_combination, do not copy any inadmissible available action into selected_actions.",
            "A priority='guarded' action may be selected with decision='apply' only when evidence_gate.passed is true.",
            "If a guarded action lacks a passing evidence_gate, skip it or mention it in notes; the executor rejects it without local SPICE intervention evidence.",
            "Do not use legacy_sensitivity_top as the final decision rule when it diverges from causal_top.",
            "All reasons and notes must be written in English.",
        ],
        "output_schema": {
            "selected_actions": [
                {
                    "action_id": "existing action_id",
                    "decision": "apply",
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
                    "apply_to": ["optional list of symmetric writable knobs"],
                    "metric": "failed metric the hypothesis targets",
                    "direction": "increase | decrease | set",
                    "suggested_unclipped_value": "number",
                    "per_knob_values": {"optional knob": "optional numeric value"},
                    "range_update": {"type": "set_range | expand_upper_bound | expand_lower_bound"},
                    "reason": "short English reason",
                }
            ],
            "rationale": "short English explanation",
            "notes": "optional English notes",
        },
        "design_state": _compact_agent_model(agent_model),
        "schema_context": command.get("schema_context", {}),
        "available_actions": [_compact_action_for_planner(action) for action in actions[:5]],
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
        "llm_policy": agent_model.get("llm_policy", "") if isinstance(agent_model, dict) else "",
        "status": _compact_status(status),
        "failed_targets": list(agent_model.get("failed_targets", []) or [])[:8] if isinstance(agent_model, dict) else [],
        "unverified_targets": list(agent_model.get("unverified_targets", []) or [])[:8] if isinstance(agent_model, dict) else [],
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
        "unverified_targets",
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


def _completion_content(data: dict[str, Any]) -> str:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"unexpected chat completion response: {exc}") from exc
    if not isinstance(message, dict):
        raise ValueError("unexpected chat completion response: message is not an object")
    content = message.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _completion_finish_reason(data: dict[str, Any]) -> str:
    try:
        value = data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
    return str(value or "")


def _completion_debug(data: dict[str, Any]) -> dict[str, Any]:
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        return {"status": "malformed_completion", "top_level_keys": sorted(map(str, data.keys()))[:8]}
    if not isinstance(choice, dict):
        return {"status": "malformed_choice"}
    message = choice.get("message")
    message_keys = sorted(map(str, message.keys())) if isinstance(message, dict) else []
    content = _completion_content(data)
    return {
        "finish_reason": choice.get("finish_reason"),
        "message_keys": message_keys[:10],
        "content_len": len(content),
        "content_prefix": content[:120].replace("\n", "\\n"),
    }


def _loads_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if not text:
        raise ValueError("LLM response content was empty")
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
        if isinstance(data, str):
            return _loads_json_object(data)
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

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
    return {
        "task": "Write a schema-level tuning command for the next analog optimization round.",
        "rules": [
            "Return JSON only.",
            "Use selected_actions for action_id values that already exist in available_actions.",
            "Use custom_actions only for explicit per-knob edits not covered by available_actions.",
            "Every action must include decision='apply' or decision='skip'.",
            "Prefer a small number of high-confidence actions per round.",
            "Prefer actions backed by causal_root_causes structural paths and intervention impact.",
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
        "agent_model": agent_model,
        "write_policy": command.get("write_policy", {}),
        "available_actions": args.get("available_actions", []),
        "default_selected_actions": args.get("selected_actions", []),
        "editable_fields": command.get("llm_editable_fields", {}),
    }


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

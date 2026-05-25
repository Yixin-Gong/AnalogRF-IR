from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from core.environment import resolve_project_path


CLI_CONFIG_SCHEMA_VERSION = "analogrf_ir.cli_config.v0_1"

CONFIG_KEY_ALIASES = {
    "input.env": "env",
    "input.schema": "schema",
    "input.spice": "spice",
    "input.spice_yaml_out": "spice_yaml_out",
    "input.topology": "topology",
    "optimizer.generations": "generations",
    "optimizer.pop_size": "pop_size",
    "optimizer.seed": "seed",
    "agent.rounds": "agent_rounds",
    "llm.provider": "llm_provider",
    "llm.model": "llm_model",
    "llm.base_url": "llm_base_url",
    "llm.api_key_env": "llm_api_key_env",
    "llm.timeout": "llm_timeout",
    "llm.temperature": "llm_temperature",
    "llm.max_tokens": "llm_max_tokens",
    "llm.thinking": "llm_thinking",
    "llm.reasoning_effort": "llm_reasoning_effort",
    "llm.api_key": "llm_api_key",
    "llm.api_key_file": "llm_api_key_file",
    "llm.api_key_stdin": "llm_api_key_stdin",
    "tools.ngspice_bin": "ngspice_bin",
    "features.tail_current_mirror": "tail_current_mirror",
    "features.run_asir": "run_asir",
    "features.no_asir": "no_asir",
    "postprocess.skip_dc_repair": "skip_dc_repair",
    "postprocess.skip_comp_tune": "skip_comp_tune",
    "postprocess.policy": "postprocess_policy",
    "postprocess.near_feasible_ratio": "postprocess_near_feasible_ratio",
    "reoptimization.generations": "reopt_generations",
    "reoptimization.pop_size": "reopt_pop_size",
    "actions.strategy": "action_strategy",
    "intervention.enable": "enable_intervention_model",
    "intervention.max_actions": "intervention_max_actions",
    "intervention.perturbation": "intervention_perturbation",
    "output.runs_dir": "runs_dir",
}


def load_cli_config(path_like: str | Path | None) -> dict[str, Any]:
    if not path_like:
        return {}
    path = resolve_project_path(path_like)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    flat = flatten_cli_config(data)
    if "run_asir" in flat and "no_asir" not in flat:
        flat["no_asir"] = not _as_bool(flat.pop("run_asir"))
    if isinstance(flat.get("postprocess_policy"), bool):
        flat["postprocess_policy"] = "always" if flat["postprocess_policy"] else "off"
    return {key: value for key, value in flat.items() if value is not None}


def flatten_cli_config(data: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        if key == "schema_version":
            continue
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                alias = CONFIG_KEY_ALIASES.get(f"{key}.{sub_key}", sub_key)
                flat[alias] = sub_value
        else:
            flat[CONFIG_KEY_ALIASES.get(key, key)] = value
    return flat


def deep_merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def write_yaml_mapping(path: str | Path, data: dict[str, Any]) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)
    return out


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)

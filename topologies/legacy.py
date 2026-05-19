from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from core.environment import default_environment, resolve_project_path
from frontends.yaml_loader import build_design_state_from_yaml, load_yaml_mapping, yaml_has_explicit_topology
from schemas.design_state import DesignState


def build_design_state(env: dict[str, Any], schema_path: str | Path, topology: str = "auto") -> DesignState:
    """Compatibility builder for old schema files without explicit topology.

    New schemas should carry `topology.devices` and go directly through the YAML
    frontend. This module exists so older OTA schemas keep working without
    pulling legacy builders back into `main.py`.
    """
    env = env or default_environment()
    path = resolve_project_path(schema_path)
    if topology == "yaml" or (topology == "auto" and path.exists() and yaml_has_explicit_topology(path)):
        return build_design_state_from_yaml(load_yaml_mapping(path), env)
    schema = load_yaml_mapping(path) if path.exists() else {}
    signature = f"{schema.get('design_name', '')} {((schema.get('topology') or {}) if isinstance(schema.get('topology'), dict) else {}).get('architecture', '')}".lower()
    if topology == "two_stage" or "two" in signature or "2stage" in signature or "two_stage" in signature:
        return build_two_stage_ota(env, schema)
    return build_five_transistor_ota(env, schema)


def build_five_transistor_ota(env: dict[str, Any] | None = None, schema: dict[str, Any] | None = None) -> DesignState:
    environment = env or default_environment()
    return build_design_state_from_yaml(_merge_schema(_five_transistor_defaults(environment), schema or {}), environment)


def build_two_stage_ota(env: dict[str, Any] | None = None, schema: dict[str, Any] | None = None) -> DesignState:
    environment = env or default_environment()
    return build_design_state_from_yaml(_merge_schema(_two_stage_defaults(environment), schema or {}), environment)


def _merge_schema(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(defaults)
    for key, value in (overrides or {}).items():
        if key == "topology" and isinstance(value, dict):
            merged[key] = _merge_mapping(merged.get(key, {}), value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _merge_mapping(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_mapping(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def _models(env: dict[str, Any]) -> tuple[str, str]:
    process = env.get("process", {}) or {}
    return process.get("nmos_model", "nmos"), process.get("pmos_model", "pmos")


def _five_transistor_defaults(env: dict[str, Any]) -> dict[str, Any]:
    nmos, pmos = _models(env)
    return {
        "schema_version": "2.1",
        "design_name": "five_transistor_ota",
        "topology": {
            "name": "five_transistor_ota",
            "class": "ota",
            "architecture": "single-stage",
            "global_nets": [{"name": "vdd", "type": "supply"}, {"name": "gnd", "type": "ground"}],
            "ports": [
                {"id": "vinp", "direction": "input"},
                {"id": "vinn", "direction": "input"},
                {"id": "vout", "direction": "output"},
                {"id": "vbias", "direction": "bias"},
            ],
            "devices": [
                {"id": "M1", "role": "input_pair", "stage": "input", "type": "nmos", "model": nmos, "connections": {"drain": "net1", "gate": "vinn", "source": "tail", "body": "gnd"}},
                {"id": "M2", "role": "input_pair", "stage": "input", "type": "nmos", "model": nmos, "connections": {"drain": "vout", "gate": "vinp", "source": "tail", "body": "gnd"}},
                {"id": "M3", "role": "current_mirror_load", "stage": "load", "type": "pmos", "model": pmos, "connections": {"drain": "net1", "gate": "net1", "source": "vdd", "body": "vdd"}},
                {"id": "M4", "role": "current_mirror_load", "stage": "load", "type": "pmos", "model": pmos, "connections": {"drain": "vout", "gate": "net1", "source": "vdd", "body": "vdd"}},
                {"id": "M5", "role": "tail_current_source", "stage": "bias", "type": "nmos", "model": nmos, "connections": {"drain": "tail", "gate": "vbias", "source": "gnd", "body": "gnd"}},
            ],
        },
        "targets": {
            "dc_gain": {"min": 35, "unit": "dB", "priority": 1},
            "unity_gain_bandwidth": {"min": 40e6, "unit": "Hz", "priority": 1},
            "phase_margin": {"min": 45, "unit": "deg", "priority": 1},
            "power": {"max": 0.3e-3, "unit": "W", "priority": 2},
        },
        "constraints": {
            "gm_id": {"min": 5, "max": 25},
            "L": {"min": 1.3e-7, "max": 2.0e-6},
            "devices": {
                "M1": {"gm_id": {"min": 12, "max": 25}},
                "M2": {"gm_id": {"min": 12, "max": 25}},
                "M3": {"gm_id": {"min": 5, "max": 12}},
                "M4": {"gm_id": {"min": 5, "max": 12}},
                "M5": {"gm_id": {"min": 8, "max": 20}},
            },
        },
        "loss_terms": [
            {"id": "gain_deficit", "formula": "relu(targets.dc_gain.min - realized.dc_gain) / max(targets.dc_gain.min, 1)", "weight": 1.0},
            {"id": "bw_deficit", "formula": "relu(targets.unity_gain_bandwidth.min - realized.unity_gain_bandwidth) / max(targets.unity_gain_bandwidth.min, 1)", "weight": 0.8},
            {"id": "pm_deficit", "formula": "relu(targets.phase_margin.min - realized.phase_margin) / max(targets.phase_margin.min, 1)", "weight": 0.25},
            {"id": "power_ratio", "formula": "realized.power / max(targets.power.max, 1e-9)", "weight": 0.5},
        ],
        "evaluations": [
            {"name": "dc_gain", "type": "ac_gain", "probe": "vout", "target_ref": "dc_gain"},
            {"name": "unity_gain_bandwidth", "type": "ugbw", "probe": "vout", "target_ref": "unity_gain_bandwidth"},
            {"name": "phase_margin", "type": "phase_margin", "probe": "vout", "target_ref": "phase_margin"},
            {"name": "total_power", "type": "dc_power", "target_ref": "power"},
        ],
    }


def _two_stage_defaults(env: dict[str, Any]) -> dict[str, Any]:
    nmos, pmos = _models(env)
    return {
        "schema_version": "2.1",
        "design_name": "two_stage_ota",
        "topology": {
            "name": "two_stage_ota",
            "class": "ota",
            "architecture": "two-stage",
            "global_nets": [{"name": "vdd", "type": "supply"}, {"name": "gnd", "type": "ground"}],
            "ports": [
                {"id": "vinp", "direction": "input"},
                {"id": "vinn", "direction": "input"},
                {"id": "vout", "direction": "output"},
                {"id": "vbias_tail", "direction": "bias"},
                {"id": "vbias_stage2", "direction": "bias"},
            ],
            "devices": [
                {"id": "M1", "role": "input_pair", "stage": "input", "type": "nmos", "model": nmos, "connections": {"drain": "net1", "gate": "vinn", "source": "tail", "body": "gnd"}},
                {"id": "M2", "role": "input_pair", "stage": "input", "type": "nmos", "model": nmos, "connections": {"drain": "n1", "gate": "vinp", "source": "tail", "body": "gnd"}},
                {"id": "M3", "role": "current_mirror_load", "stage": "load", "type": "pmos", "model": pmos, "connections": {"drain": "net1", "gate": "net1", "source": "vdd", "body": "vdd"}},
                {"id": "M4", "role": "current_mirror_load", "stage": "load", "type": "pmos", "model": pmos, "connections": {"drain": "n1", "gate": "net1", "source": "vdd", "body": "vdd"}},
                {"id": "M5", "role": "tail_current_source", "stage": "bias", "type": "nmos", "model": nmos, "connections": {"drain": "tail", "gate": "vbias_tail", "source": "gnd", "body": "gnd"}},
                {"id": "M6", "role": "second_stage_gain", "stage": "output", "type": "pmos", "model": pmos, "connections": {"drain": "vout", "gate": "n1", "source": "vdd", "body": "vdd"}},
                {"id": "M7", "role": "output_current_source", "stage": "output", "type": "nmos", "model": nmos, "connections": {"drain": "vout", "gate": "vbias_stage2", "source": "gnd", "body": "gnd"}},
            ],
        },
        "targets": {
            "dc_gain": {"min": 60, "unit": "dB", "priority": 1},
            "unity_gain_bandwidth": {"min": 80e6, "unit": "Hz", "priority": 1},
            "phase_margin": {"min": 60, "unit": "deg", "priority": 1},
            "power": {"max": 0.5e-3, "unit": "W", "priority": 2},
        },
        "constraints": {"gm_id": {"min": 5, "max": 25}, "L": {"min": 1.3e-7, "max": 2.0e-6}},
        "loss_terms": [
            {"id": "gain_deficit", "formula": "relu(targets.dc_gain.min - realized.dc_gain)/max(targets.dc_gain.min, 1)", "weight": 4.0},
            {"id": "bw_deficit", "formula": "relu(targets.unity_gain_bandwidth.min - realized.unity_gain_bandwidth)/max(targets.unity_gain_bandwidth.min, 1)", "weight": 3.0},
            {"id": "pm_deficit", "formula": "relu(targets.phase_margin.min - realized.phase_margin)/max(targets.phase_margin.min, 1)", "weight": 6.0},
            {"id": "power_ratio", "formula": "realized.power/max(targets.power.max, 1e-9)", "weight": 0.4},
            {"id": "zero_alignment", "formula": "abs(realized.Rz - realized.zero_target_rz)/max(realized.zero_target_rz, 1)", "weight": 0.08},
        ],
        "evaluations": [
            {"name": "dc_gain", "type": "ac_gain", "probe": "vout", "target_ref": "dc_gain"},
            {"name": "unity_gain_bandwidth", "type": "ugbw", "probe": "vout", "target_ref": "unity_gain_bandwidth"},
            {"name": "phase_margin", "type": "phase_margin", "probe": "vout", "target_ref": "phase_margin"},
            {"name": "total_power", "type": "dc_power", "target_ref": "power"},
        ],
    }

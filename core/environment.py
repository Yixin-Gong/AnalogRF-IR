from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from schemas.design_state import ProcessInfo, SimulationConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = PROJECT_ROOT / "environment.yaml"


def resolve_project_path(path_like: str | Path) -> Path:
    path = Path(path_like)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def existing_project_path(path_like: str | Path | None) -> str | None:
    if not path_like:
        return None
    path = resolve_project_path(path_like)
    return str(path) if path.exists() else None


def load_environment(env_path: str | Path | None = None) -> dict[str, Any]:
    path = resolve_project_path(env_path or DEFAULT_ENV_PATH)
    if not path.exists():
        return default_environment()
    with path.open("r", encoding="utf-8") as handle:
        env = yaml.safe_load(handle) or {}
    if not isinstance(env, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return env


def default_environment() -> dict[str, Any]:
    return {
        "process": {
            "process_name": "PTM_130nm",
            "technology_node": 0.13,
            "foundry": "PTM",
            "model_lib": "ptm_130.lib",
            "model_corner": "",
            "osdi_libs": [],
            "device_style": "mos",
            "nmos_model": "nmos",
            "pmos_model": "pmos",
            "VTH_n": 0.3782,
            "VTH_p": 0.321,
            "KP_n": 0.00091,
            "KP_p": 0.000123,
            "COX": 0.01535,
        },
        "simulation": {
            "simulator": "ngspice",
            "temperature": 27.0,
            "supply": {"vdd": 1.2, "vss": 0.0},
            "analyses": ["op", "ac"],
            "ac_start": 1.0,
            "ac_stop": 1e9,
            "ac_points": 50,
            "cload": 1e-12,
            "bias_voltage": 0.6,
        },
        "tools": {
            "ngspice_bin": "ngspice",
            "pygmid_tables_dir": "tables",
            "nmos_table": "tables/ptm130_nmos.npz",
            "pmos_table": "tables/ptm130_pmos.npz",
        },
        "design_rules": {
            "min_L": 1.3e-7,
            "max_L": 1e-5,
            "min_W": 1.5e-7,
            "max_W": 2e-4,
            "W_precision": 1e-8,
            "L_precision": 1e-9,
            "min_area": 2e-14,
            "max_W_L_ratio": 1000.0,
            "min_W_L_ratio": 0.1,
            "max_finger_width": 1e-5,
            "max_VGS": 1.32,
            "max_VDS": 1.32,
            "VDS_safe_margin": 0.15,
            "nominal_VDD": 1.2,
            "VDD_min": 1.08,
            "VDD_max": 1.32,
            "gm_id_min": 3.0,
            "gm_id_max": 28.0,
            "VDSAT_headroom_factor": 1.3,
            "n_sub_nmos": 1.4,
            "n_sub_pmos": 1.4,
            "mu_n": 0.04,
            "mu_p": 0.01,
        },
    }


def build_process_info(env: dict[str, Any]) -> ProcessInfo:
    process = env.get("process", {}) or {}
    design_rules = env.get("design_rules", {}) or {}
    return ProcessInfo(
        process_name=process.get("process_name", "PTM_130nm"),
        technology_node=process.get("technology_node", 0.13),
        foundry=process.get("foundry", "PTM"),
        model_lib=process.get("model_lib", "ptm_130.lib"),
        model_corner=process.get("model_corner", ""),
        osdi_libs=process.get("osdi_libs", []) or [],
        device_style=process.get("device_style", "mos"),
        nmos_model=process.get("nmos_model", "nmos"),
        pmos_model=process.get("pmos_model", "pmos"),
        VTH_n=process.get("VTH_n", 0.3782),
        VTH_p=process.get("VTH_p", 0.321),
        KP_n=process.get("KP_n", 910e-6),
        KP_p=process.get("KP_p", 123e-6),
        COX=process.get("COX", 1.535e-2),
        min_L=design_rules.get("min_L", 1.3e-7),
        max_L=design_rules.get("max_L", 10e-6),
        min_W=design_rules.get("min_W", 1.5e-7),
        max_W=design_rules.get("max_W", 200e-6),
        W_precision=design_rules.get("W_precision", 10e-9),
        L_precision=design_rules.get("L_precision", 1e-9),
        min_area=design_rules.get("min_area", 2e-14),
        max_W_L_ratio=design_rules.get("max_W_L_ratio", 1000.0),
        min_W_L_ratio=design_rules.get("min_W_L_ratio", 0.1),
        max_finger_width=design_rules.get("max_finger_width", 10e-6),
        max_VGS=design_rules.get("max_VGS", 1.32),
        max_VDS=design_rules.get("max_VDS", 1.32),
        VDS_safe_margin=design_rules.get("VDS_safe_margin", 0.15),
        nominal_VDD=design_rules.get("nominal_VDD", 1.2),
        VDD_min=design_rules.get("VDD_min", 1.08),
        VDD_max=design_rules.get("VDD_max", 1.32),
        gm_id_min=design_rules.get("gm_id_min", 3.0),
        gm_id_max=design_rules.get("gm_id_max", 28.0),
        VDSAT_headroom_factor=design_rules.get("VDSAT_headroom_factor", 1.3),
        n_sub_nmos=design_rules.get("n_sub_nmos", 1.4),
        n_sub_pmos=design_rules.get("n_sub_pmos", 1.4),
        mu_n=design_rules.get("mu_n", 0.04),
        mu_p=design_rules.get("mu_p", 0.01),
    )


def build_simulation_config(env: dict[str, Any]) -> SimulationConfig:
    sim = env.get("simulation", {}) or {}
    return SimulationConfig(
        temperature=sim.get("temperature", 27.0),
        supply=sim.get("supply", {"vdd": 1.2, "vss": 0.0}),
        model_lib=(env.get("process", {}) or {}).get("model_lib", "ptm_130.lib"),
        analyses=sim.get("analyses", ["op", "ac"]),
        ac_start=sim.get("ac_start", 1.0),
        ac_stop=sim.get("ac_stop", 1e9),
        ac_points=sim.get("ac_points", 50),
        cload=sim.get("cload", 1e-12),
        bias_voltage=sim.get("bias_voltage", 0.6),
    )

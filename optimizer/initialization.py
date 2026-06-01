"""Topology-guided initial population construction for OTA sizing."""

from __future__ import annotations

from typing import Any

import numpy as np


_LOG_VARIABLES = {"I_tail", "I_stage2", "I_out", "I_latch", "Cc", "Rz", "CL"}


def build_topology_guided_initial_points(
    evaluator: Any,
    bounds: np.ndarray,
    *,
    base: np.ndarray,
    max_points: int,
    rng: np.random.RandomState | None = None,
) -> list[np.ndarray]:
    """Return bounded, schema-derived initial points for the optimizer.

    The points are not written back to the schema. They only seed the optimizer
    population with topology-aware gain, speed, and operating-point hypotheses.
    """

    if max_points <= 0 or bounds.size == 0:
        return []

    rng = rng or np.random.RandomState()
    index = _EncodedIndex(evaluator)
    architecture = str(getattr(evaluator.schema.topology, "architecture", "") or "").lower()
    points: list[tuple[str, np.ndarray]] = []

    def add(label: str, edits: list[tuple[str, Any, float]]) -> None:
        x = np.array(base, dtype=float, copy=True)
        changed = False
        for selector, value, quantile in edits:
            for idx in index.match(selector, value):
                x[idx] = _quantile_value(bounds[idx, 0], bounds[idx, 1], quantile, index.variable(idx))
                changed = True
        if changed:
            points.append((label, _clip(x, bounds)))

    # Generic OTA hypotheses.
    add(
        "gain_path_ro",
        [
            ("var_role", ("L", _gain_path_role), 0.86),
            ("var_role", ("gm_id", _input_or_second_stage_role), 0.58),
            ("var_name", "I_tail", 0.52),
        ],
    )
    add(
        "gm_speed",
        [
            ("var_role", ("gm_id", _input_or_second_stage_role), 0.72),
            ("var_role", ("L", _input_or_second_stage_role), 0.38),
            ("var_name", "I_tail", 0.78),
            ("var_name", "I_stage2", 0.78),
            ("var_name", "Cc", 0.25),
        ],
    )

    if "single-stage" in architecture:
        add(
            "single_stage_gain_recovery",
            [
                ("var_role", ("L", _load_role), 0.95),
                ("var_role", ("L", _input_role), 0.72),
                ("var_role", ("L", _tail_role), 0.78),
                ("var_role", ("gm_id", _input_role), 0.60),
                ("var_role", ("gm_id", _load_role), 0.52),
                ("var_name", "I_tail", 0.52),
            ],
        )
        add(
            "single_stage_speed_guarded",
            [
                ("var_role", ("L", _load_role), 0.82),
                ("var_role", ("L", _input_role), 0.48),
                ("var_role", ("gm_id", _input_role), 0.70),
                ("var_name", "I_tail", 0.70),
            ],
        )

    if "current" in architecture and "mirror" in architecture:
        add(
            "current_mirror_gain_speed",
            [
                ("var_role", ("L", _load_role), 0.90),
                ("var_role", ("gm_id", _input_role), 0.70),
                ("var_role", ("gm_id", _load_role), 0.55),
                ("var_name", "I_tail", 0.82),
                ("var_name", "vbias", 0.62),
            ],
        )

    if "folded" in architecture:
        add(
            "folded_balanced_gain_speed",
            [
                ("var_role", ("L", _input_role), 0.38),
                ("var_role", ("gm_id", _input_role), 0.58),
                ("var_role", ("L", _tail_role), 0.22),
                ("var_role", ("gm_id", _tail_role), 0.80),
                ("var_role", ("L", _load_role), 0.52),
                ("var_role", ("gm_id", _load_role), 0.04),
                ("var_role", ("L", _cascode_role), 0.33),
                ("var_role", ("gm_id", _cascode_role), 0.26),
                ("var_name", "I_tail", 0.62),
                ("var_name", "vbias_ptail", 0.44),
                ("var_name", "vbias_ncas", 0.74),
            ],
        )
        add(
            "folded_gain_headroom",
            [
                ("var_role", ("L", _cascode_or_load_role), 0.88),
                ("var_role", ("L", _input_role), 0.64),
                ("var_role", ("L", _tail_role), 0.68),
                ("var_role", ("gm_id", _input_role), 0.60),
                ("var_name", "I_tail", 0.60),
                ("var_name", "vbias_ptail", 0.68),
                ("var_name", "vbias_ncas", 0.72),
            ],
        )
        add(
            "folded_bandwidth_guarded",
            [
                ("var_role", ("L", _cascode_or_load_role), 0.72),
                ("var_role", ("L", _input_role), 0.48),
                ("var_role", ("gm_id", _input_role), 0.68),
                ("var_name", "I_tail", 0.80),
                ("var_name", "vbias_ptail", 0.56),
                ("var_name", "vbias_ncas", 0.62),
            ],
        )

    if "telescopic" in architecture:
        add(
            "telescopic_stack_speed",
            [
                ("var_role", ("L", _input_role), 0.42),
                ("var_role", ("L", _cascode_or_load_role), 0.72),
                ("var_role", ("gm_id", _input_role), 0.68),
                ("var_name", "I_tail", 0.86),
                ("var_name", "vbias_tail", 0.10),
                ("var_name", "vbias_ncas", 0.90),
                ("var_name", "vbias_pcas", 0.08),
            ],
        )
        add(
            "telescopic_gain_headroom",
            [
                ("var_role", ("L", _gain_path_role), 0.88),
                ("var_role", ("gm_id", _input_role), 0.62),
                ("var_name", "I_tail", 0.72),
                ("var_name", "vbias_tail", 0.18),
                ("var_name", "vbias_ncas", 0.82),
                ("var_name", "vbias_pcas", 0.18),
            ],
        )

    if "two-stage" in architecture or "miller" in architecture or evaluator.capabilities.has("two_stage_gain"):
        add(
            "two_stage_bandwidth",
            [
                ("var_role", ("L", _input_role), 0.50),
                ("var_role", ("L", _two_stage_gain_role), 0.66),
                ("var_role", ("L", _load_or_output_role), 0.72),
                ("var_role", ("gm_id", _input_or_second_stage_role), 0.66),
                ("var_name", "I_tail", 0.72),
                ("var_name", "I_stage2", 0.86),
                ("var_name", "Cc", 0.24),
                ("var_name", "Rz", 0.30),
            ],
        )
        add(
            "two_stage_gain",
            [
                ("var_role", ("L", _input_or_second_stage_role), 0.88),
                ("var_role", ("L", _load_or_output_role), 0.92),
                ("var_role", ("gm_id", _input_or_second_stage_role), 0.58),
                ("var_name", "I_tail", 0.56),
                ("var_name", "I_stage2", 0.66),
                ("var_name", "Cc", 0.38),
                ("var_name", "Rz", 0.35),
            ],
        )
        add(
            "two_stage_ro_recovery",
            [
                ("var_role", ("L", _input_role), 0.94),
                ("var_role", ("L", _two_stage_gain_role), 0.88),
                ("var_role", ("L", _load_or_output_role), 0.96),
                ("var_role", ("gm_id", _input_or_second_stage_role), 0.54),
                ("var_name", "I_tail", 0.50),
                ("var_name", "I_stage2", 0.62),
                ("var_name", "Cc", 0.44),
                ("var_name", "Rz", 0.40),
            ],
        )

    # Light deterministic jitter around guided points prevents duplicate starts
    # after symmetry reduction while keeping all points near interpretable anchors.
    out: list[np.ndarray] = []
    seen: set[tuple[float, ...]] = set()
    for _label, point in points:
        for candidate in (point, _jitter(point, bounds, rng)):
            key = tuple(np.round(candidate, decimals=15))
            if key in seen:
                continue
            seen.add(key)
            out.append(candidate)
            if len(out) >= max_points:
                return out
    return out


class _EncodedIndex:
    def __init__(self, evaluator: Any):
        self.evaluator = evaluator
        self.devices = {
            getattr(device, "id", ""): device
            for device in getattr(evaluator.schema.topology, "devices", [])
        }

    def variable(self, idx: int) -> str:
        return self.evaluator.design_variable_at(idx).variable

    def role(self, idx: int) -> str:
        dv = self.evaluator.design_variable_at(idx)
        if not dv.device:
            return ""
        device = self.devices.get(dv.device)
        return str(getattr(device, "role", "") or "").lower()

    def match(self, selector: str, value: Any) -> list[int]:
        matches: list[int] = []
        for idx, dv in enumerate(self.evaluator.encoded_design_variables):
            role = self.role(idx)
            if selector == "var_name" and dv.variable == value:
                matches.append(idx)
            elif selector == "role" and callable(value) and value(role):
                matches.append(idx)
            elif selector == "var_role":
                variable, predicate = value
                if dv.variable == variable and predicate(role):
                    matches.append(idx)
        return matches


def _quantile_value(low: float, high: float, quantile: float, variable: str) -> float:
    q = min(max(float(quantile), 0.0), 1.0)
    if high <= low:
        return float(low)
    if variable in _LOG_VARIABLES and low > 0.0:
        return float(np.exp(np.log(low) + q * (np.log(high) - np.log(low))))
    return float(low + q * (high - low))


def _clip(x: np.ndarray, bounds: np.ndarray) -> np.ndarray:
    return np.clip(x, bounds[:, 0], bounds[:, 1])


def _jitter(x: np.ndarray, bounds: np.ndarray, rng: np.random.RandomState) -> np.ndarray:
    span = np.maximum(bounds[:, 1] - bounds[:, 0], 0.0)
    noise = rng.normal(loc=0.0, scale=0.025, size=len(x)) * span
    return _clip(x + noise, bounds)


def _has_any(role: str, *tokens: str) -> bool:
    return any(token in role for token in tokens)


def _input_role(role: str) -> bool:
    return _has_any(role, "input_pair")


def _load_role(role: str) -> bool:
    return _has_any(role, "load", "current_mirror")


def _tail_role(role: str) -> bool:
    return _has_any(role, "tail_current_source")


def _cascode_or_load_role(role: str) -> bool:
    return _has_any(role, "cascode", "load", "current_mirror")


def _cascode_role(role: str) -> bool:
    return _has_any(role, "cascode")


def _two_stage_gain_role(role: str) -> bool:
    return _has_any(role, "second_stage_gain")


def _load_or_output_role(role: str) -> bool:
    return _has_any(role, "load", "output_current_source", "second_stage_load", "output_bias")


def _input_or_second_stage_role(role: str) -> bool:
    return _input_role(role) or _two_stage_gain_role(role)


def _gain_path_role(role: str) -> bool:
    if _has_any(role, "tail", "bias_mirror", "regulated_source_current_source"):
        return False
    return _has_any(
        role,
        "input_pair",
        "current_mirror_load",
        "active_load",
        "cascode",
        "load_cascode",
        "folded_cascode",
        "second_stage_gain",
        "second_stage_load",
        "output_current_source",
    )

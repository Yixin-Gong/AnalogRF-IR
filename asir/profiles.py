from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from schemas.design_state import DesignState, LossTerm, Target


@dataclass(frozen=True)
class CircuitProfile:
    """IR-level policy bundle for one circuit family."""

    name: str
    circuit_classes: tuple[str, ...] = ("generic",)
    architectures: tuple[str, ...] = ()
    metric_map: dict[str, str] = field(default_factory=dict)
    metric_groups: dict[str, set[str]] = field(default_factory=dict)
    required_context: tuple[str, ...] = ()
    dynamic_role_tokens: tuple[str, ...] = ()
    skip_static_voltage_stack: bool = False

    def matches(self, state: DesignState) -> bool:
        circuit_class = (state.topology.class_ or "").lower()
        architecture = (state.topology.architecture or "").lower()
        return (
            circuit_class in self.circuit_classes
            or any(token in architecture for token in self.architectures)
        )

    def measurement_key(self, metric: str) -> str:
        return self.metric_map.get(metric, metric)

    def is_dynamic_role(self, role: str) -> bool:
        role_l = (role or "").lower()
        return any(token in role_l for token in self.dynamic_role_tokens)

    def missing_metric_groups(self, targets: Iterable[str]) -> list[str]:
        target_names = set(targets)
        return sorted(
            group for group, aliases in self.metric_groups.items()
            if not (aliases & target_names)
        )

    def build_loss_terms_from_targets(self, targets: dict[str, Target]) -> list[LossTerm]:
        terms: list[LossTerm] = []
        for name, target in targets.items():
            weight = 1.0 / max(int(target.priority or 1), 1)
            if target.min is not None:
                terms.append(
                    LossTerm(
                        id=f"{name}_shortfall",
                        formula=f"relu((targets.{name}.min - realized.{name}) / max(targets.{name}.min, 1e-12))",
                        weight=weight,
                        description=f"Auto-generated from targets.{name}.min by {self.name} profile",
                    )
                )
            if target.max is not None:
                terms.append(
                    LossTerm(
                        id=f"{name}_excess",
                        formula=f"relu((realized.{name} - targets.{name}.max) / max(targets.{name}.max, 1e-12))",
                        weight=weight,
                        description=f"Auto-generated from targets.{name}.max by {self.name} profile",
                    )
                )
        return terms


OTA_PROFILE = CircuitProfile(
    name="ota",
    circuit_classes=("ota", "opamp", "operational_amplifier"),
    architectures=("ota", "opamp", "two-stage", "single-stage"),
    metric_map={
        "dc_gain": "dc_gain_db",
        "unity_gain_bandwidth": "unity_gain_bandwidth",
        "ugbw": "unity_gain_bandwidth",
        "phase_margin": "phase_margin",
        "slew_rate": "slew_rate",
        "slew_rate_pos": "slew_rate_pos",
        "slew_rate_neg": "slew_rate_neg",
        "output_swing": "output_swing",
        "swing": "output_swing",
        "saturation_margin": "saturation_margin",
        "saturation_required_gap": "saturation_required_gap",
        "icmr": "icmr",
        "icmr_range": "icmr",
        "icmr_min": "icmr_min",
        "input_common_mode_min": "icmr_min",
        "icmr_max": "icmr_max",
        "input_common_mode_max": "icmr_max",
        "power": "total_power",
    },
)


COMPARATOR_PROFILE = CircuitProfile(
    name="comparator",
    circuit_classes=("comparator",),
    architectures=("strongarm", "double-tail", "sense-amplifier", "comparator"),
    metric_map={
        "delay": "delay",
        "decision_time": "delay",
        "propagation_delay": "delay",
        "regeneration_time": "regeneration_time",
        "reset_time": "reset_time",
        "offset": "offset",
        "input_referred_offset": "offset",
        "noise": "noise",
        "input_referred_noise": "noise",
        "kickback": "kickback_noise",
        "kickback_noise": "kickback_noise",
        "clock_feedthrough": "clock_feedthrough",
        "energy": "energy",
        "energy_per_comparison": "energy",
        "pdp": "pdp",
        "power_delay_product": "pdp",
        "edp": "edp",
        "input_capacitance": "input_capacitance",
        "cin": "input_capacitance",
        "output_swing": "output_swing",
        "logic_swing": "output_swing",
        "icmr": "icmr",
        "input_common_mode_range": "icmr",
        "icmr_min": "icmr_min",
        "input_common_mode_min": "icmr_min",
        "icmr_max": "icmr_max",
        "input_common_mode_max": "icmr_max",
        "metastability_margin": "metastability_margin",
        "decision_margin": "decision_margin",
        "max_sample_rate": "max_sample_rate",
        "area": "area",
        "device_area": "device_area",
        "power": "power",
        "average_power": "average_power",
    },
    metric_groups={
        "delay": {"delay", "decision_time", "propagation_delay"},
        "regeneration_time": {"regeneration_time"},
        "reset_time": {"reset_time"},
        "offset": {"offset", "input_referred_offset"},
        "noise": {"noise", "input_referred_noise"},
        "kickback": {"kickback", "kickback_noise", "clock_feedthrough"},
        "energy": {"energy", "energy_per_comparison"},
        "pdp": {"pdp", "power_delay_product", "edp"},
        "input_capacitance": {"input_capacitance", "cin"},
        "output_swing": {"output_swing", "logic_swing"},
        "icmr": {"icmr", "input_common_mode_range"},
        "metastability_margin": {"metastability_margin", "decision_margin"},
        "max_sample_rate": {"max_sample_rate"},
    },
    required_context=("CL", "f_clk", "input_step"),
    dynamic_role_tokens=("latch", "reset", "precharge", "equalize"),
    skip_static_voltage_stack=True,
)


SAMPLE_HOLD_PROFILE = CircuitProfile(
    name="sample_hold",
    circuit_classes=("sample_hold", "sample-and-hold", "track_hold"),
    architectures=("sample", "hold", "track"),
    metric_map={
        "settling_time": "settling_time",
        "acquisition_time": "acquisition_time",
        "hold_step": "hold_step",
        "droop": "droop",
        "snr": "snr",
        "power": "total_power",
    },
)


GENERIC_PROFILE = CircuitProfile(name="generic", circuit_classes=("generic",))


PROFILES: tuple[CircuitProfile, ...] = (
    OTA_PROFILE,
    COMPARATOR_PROFILE,
    SAMPLE_HOLD_PROFILE,
    GENERIC_PROFILE,
)


def select_circuit_profile(state: DesignState) -> CircuitProfile:
    for profile in PROFILES:
        if profile.matches(state):
            return profile
    return GENERIC_PROFILE

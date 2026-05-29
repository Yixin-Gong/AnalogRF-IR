from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from asir.profiles import COMPARATOR_PROFILE, OTA_PROFILE, SAMPLE_HOLD_PROFILE, CircuitProfile, select_circuit_profile
from schemas.design_state import DesignState, Target


@dataclass(frozen=True)
class CircuitSpecModel:
    """Target/evaluation naming policy for a circuit family."""

    name: str = "generic"
    circuit_classes: tuple[str, ...] = ("generic",)
    architectures: tuple[str, ...] = ()
    metric_map: dict[str, str] = field(default_factory=dict)
    profile: CircuitProfile | None = None

    def matches(self, state: DesignState) -> bool:
        if self.profile is not None:
            return select_circuit_profile(state).name == self.profile.name
        circuit_class = (state.topology.class_ or "").lower()
        architecture = (state.topology.architecture or "").lower()
        return circuit_class in self.circuit_classes or any(token in architecture for token in self.architectures)

    def measurement_key(self, target_name: str) -> str:
        return self.metric_map.get(target_name, target_name)

    def measured_value(
        self,
        target_name: str,
        measurements: dict[str, float],
        estimates: dict[str, float],
    ) -> tuple[str, float | None]:
        key = self.measurement_key(target_name)
        if key in measurements:
            return "ngspice", measurements[key]
        if target_name in estimates:
            return "optimizer_estimate", estimates[target_name]
        return "missing", None

    def target_status(
        self,
        target_name: str,
        target: Target,
        measurements: dict[str, float],
        estimates: dict[str, float],
        *,
        require_ngspice: bool = True,
    ) -> dict[str, Any]:
        source, value = self.measured_value(target_name, measurements, estimates)
        status = "unknown"
        model_status = "unknown"
        margin_abs = None
        margin_rel = None
        counts_for_pass = int(target.priority or 1) <= 2
        requires_ngspice = require_ngspice and counts_for_pass
        if value is None and requires_ngspice:
            status = "unverified"
        if value is not None:
            status = "pass"
            if target.min is not None:
                margin_abs = float(value) - float(target.min)
                margin_rel = margin_abs / max(abs(float(target.min)), 1e-30)
                if margin_abs < 0:
                    status = "fail"
            if target.max is not None:
                max_margin = float(target.max) - float(value)
                max_margin_rel = max_margin / max(abs(float(target.max)), 1e-30)
                if margin_abs is None or max_margin < margin_abs:
                    margin_abs = max_margin
                    margin_rel = max_margin_rel
                if max_margin < 0:
                    status = "fail"
            model_status = status
            if requires_ngspice and source != "ngspice":
                status = "unverified"
        return {
            "status": status,
            "model_status": model_status,
            "source": source,
            "requires_ngspice": requires_ngspice,
            "counts_for_pass": counts_for_pass,
            "measurement_key": self.measurement_key(target_name),
            "value": value,
            "min": target.min,
            "max": target.max,
            "unit": target.unit,
            "priority": target.priority,
            "margin_abs": margin_abs,
            "margin_rel": margin_rel,
        }


class OTASpecModel(CircuitSpecModel):
    def __init__(self) -> None:
        super().__init__(
            name=OTA_PROFILE.name,
            circuit_classes=OTA_PROFILE.circuit_classes,
            architectures=OTA_PROFILE.architectures,
            metric_map=OTA_PROFILE.metric_map,
            profile=OTA_PROFILE,
        )


class ComparatorSpecModel(CircuitSpecModel):
    def __init__(self) -> None:
        super().__init__(
            name=COMPARATOR_PROFILE.name,
            circuit_classes=COMPARATOR_PROFILE.circuit_classes,
            architectures=COMPARATOR_PROFILE.architectures,
            metric_map=COMPARATOR_PROFILE.metric_map,
            profile=COMPARATOR_PROFILE,
        )


class SampleHoldSpecModel(CircuitSpecModel):
    def __init__(self) -> None:
        super().__init__(
            name=SAMPLE_HOLD_PROFILE.name,
            circuit_classes=SAMPLE_HOLD_PROFILE.circuit_classes,
            architectures=SAMPLE_HOLD_PROFILE.architectures,
            metric_map=SAMPLE_HOLD_PROFILE.metric_map,
            profile=SAMPLE_HOLD_PROFILE,
        )


class SpecRegistry:
    def __init__(self, models: list[CircuitSpecModel] | None = None) -> None:
        self.models = models or [
            OTASpecModel(),
            ComparatorSpecModel(),
            SampleHoldSpecModel(),
            CircuitSpecModel(),
        ]

    def select(self, state: DesignState) -> CircuitSpecModel:
        for model in self.models:
            if model.matches(state):
                return model
        return self.models[-1]

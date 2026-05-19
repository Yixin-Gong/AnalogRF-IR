from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from schemas.design_state import DesignState, Target


@dataclass(frozen=True)
class CircuitSpecModel:
    """Target/evaluation naming policy for a circuit family."""

    name: str = "generic"
    circuit_classes: tuple[str, ...] = ("generic",)
    architectures: tuple[str, ...] = ()
    metric_map: dict[str, str] = field(default_factory=dict)

    def matches(self, state: DesignState) -> bool:
        circuit_class = (state.topology.class_ or "").lower()
        architecture = (state.topology.architecture or "").lower()
        return (
            circuit_class in self.circuit_classes
            or any(token in architecture for token in self.architectures)
        )

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
    ) -> dict[str, Any]:
        source, value = self.measured_value(target_name, measurements, estimates)
        status = "unknown"
        model_status = "unknown"
        margin_abs = None
        margin_rel = None
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
            if int(target.priority or 1) <= 1 and source != "ngspice":
                status = "unverified"
        return {
            "status": status,
            "model_status": model_status,
            "source": source,
            "requires_ngspice": int(target.priority or 1) <= 1,
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
            name="ota",
            circuit_classes=("ota", "opamp", "operational_amplifier"),
            architectures=("ota", "opamp", "two-stage", "single-stage"),
            metric_map={
                "dc_gain": "dc_gain_db",
                "unity_gain_bandwidth": "unity_gain_bandwidth",
                "ugbw": "unity_gain_bandwidth",
                "phase_margin": "phase_margin",
                "power": "total_power",
            },
        )


class ComparatorSpecModel(CircuitSpecModel):
    def __init__(self) -> None:
        super().__init__(
            name="comparator",
            circuit_classes=("comparator",),
            architectures=("strongarm", "double-tail", "sense-amplifier", "comparator"),
            metric_map={
                "delay": "delay",
                "offset": "offset",
                "input_referred_offset": "offset",
                "noise": "noise",
                "energy": "energy",
                "power": "total_power",
            },
        )


class SampleHoldSpecModel(CircuitSpecModel):
    def __init__(self) -> None:
        super().__init__(
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

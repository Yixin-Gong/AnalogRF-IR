from __future__ import annotations

from dataclasses import dataclass

from asir.capabilities import CircuitCapabilities, detect_circuit_capabilities
from asir.profiles import CircuitProfile, select_circuit_profile
from schemas.design_state import DesignState


@dataclass(frozen=True)
class OptimizationProblem:
    """Unified optimization context independent of the selected algorithm."""

    state: DesignState
    profile: CircuitProfile
    capabilities: CircuitCapabilities

    @classmethod
    def from_state(cls, state: DesignState) -> "OptimizationProblem":
        profile = select_circuit_profile(state)
        capabilities = detect_circuit_capabilities(state, profile)
        return cls(state=state, profile=profile, capabilities=capabilities)

    @property
    def estimator_key(self) -> str:
        if self.profile.name == "comparator":
            return "comparator_dynamic"
        if self.capabilities.has("two_stage_gain"):
            if self.capabilities.has("miller_capacitive_compensation"):
                return "ota_two_stage_miller"
            return "ota_two_stage_uncompensated"
        return f"{self.profile.name}_compact"

    def to_flow_meta(self) -> dict[str, object]:
        return {
            "profile": self.profile.name,
            "estimator": self.estimator_key,
            "capabilities": self.capabilities.to_dict(),
        }

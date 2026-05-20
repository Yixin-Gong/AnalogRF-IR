from __future__ import annotations

from dataclasses import dataclass, field

from asir.profiles import CircuitProfile, select_circuit_profile
from core.compensation import has_miller_capacitive_compensation, has_miller_rc_compensation
from schemas.design_state import DesignState


@dataclass(frozen=True)
class CircuitCapabilities:
    """IR-level feature flags derived from topology, roles, and declared knobs."""

    profile_name: str
    circuit_class: str
    architecture: str
    names: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)

    def has(self, name: str) -> bool:
        return name in self.names

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile_name,
            "class": self.circuit_class,
            "architecture": self.architecture,
            "names": list(self.names),
            "roles": list(self.roles),
            "metadata": dict(self.metadata),
        }


def detect_circuit_capabilities(
    state: DesignState,
    profile: CircuitProfile | None = None,
) -> CircuitCapabilities:
    profile = profile or select_circuit_profile(state)
    architecture = (state.topology.architecture or "").lower()
    circuit_class = (state.topology.class_ or "").lower()
    roles = tuple(sorted({(dev.role or "").lower() for dev in state.topology.devices if dev.role}))
    role_text = " ".join(roles)
    bias_ports = tuple(sorted(port.id for port in state.topology.ports if port.direction == "bias"))

    names: set[str] = set()
    if any(token in architecture for token in ("two-stage", "two_stage", "two", "2")):
        names.add("two_stage_gain")
    if any(role.startswith("second_stage") for role in roles):
        names.add("two_stage_gain")
    if has_miller_capacitive_compensation(state):
        names.add("miller_capacitive_compensation")
    if has_miller_rc_compensation(state):
        names.add("miller_rc_compensation")
    if "source_follower" in role_text or "follower" in role_text:
        names.add("source_follower_regulation")
    if "regulated_source_current_source" in roles:
        names.add("regulated_source_current_source")
    if "tail_bias_mirror" in roles:
        names.add("tail_current_mirror")
    if "output_bias_mirror" in roles:
        names.add("output_bias_mirror")
    if any(profile.is_dynamic_role(role) for role in roles):
        names.add("dynamic_latch")
    if profile.name == "comparator":
        names.add("comparator_decision")
    if bias_ports:
        names.add("explicit_bias_ports")

    return CircuitCapabilities(
        profile_name=profile.name,
        circuit_class=circuit_class,
        architecture=architecture,
        names=tuple(sorted(names)),
        roles=roles,
        metadata={"bias_ports": bias_ports},
    )

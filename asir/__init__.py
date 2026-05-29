"""Analog Semantic IR (ASIR) package."""

from asir.design import ASIRDesign, build_design
from asir.examples.comparators import (
    build_double_tail_comparator,
    build_sense_amplifier_comparator,
    build_strongarm_comparator,
)
from asir.profiles import (
    CircuitProfile,
    COMPARATOR_PROFILE,
    GENERIC_PROFILE,
    OTA_PROFILE,
    SAMPLE_HOLD_PROFILE,
    select_circuit_profile,
)

__all__ = [
    "ASIRDesign",
    "build_design",
    "build_double_tail_comparator",
    "build_sense_amplifier_comparator",
    "build_strongarm_comparator",
    "CircuitProfile",
    "COMPARATOR_PROFILE",
    "GENERIC_PROFILE",
    "OTA_PROFILE",
    "SAMPLE_HOLD_PROFILE",
    "select_circuit_profile",
]

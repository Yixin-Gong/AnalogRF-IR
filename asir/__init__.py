"""Analog Semantic IR (ASIR) package."""

from asir.design import ASIRDesign, build_design
from asir.examples.comparators import (
    build_double_tail_comparator,
    build_sense_amplifier_comparator,
    build_strongarm_comparator,
)

__all__ = [
    "ASIRDesign",
    "build_design",
    "build_double_tail_comparator",
    "build_sense_amplifier_comparator",
    "build_strongarm_comparator",
]

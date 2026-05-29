from __future__ import annotations

from typing import Any


SPICE_OPERATING_REGIONS = {"saturation", "linear", "subthreshold", "off", "unknown"}
INVERSION_REGIONS = {"weak", "moderate", "strong", "unknown"}


def compact_operating_region(vds: float, vdsat: float, current: float | None = None) -> str:
    """Estimate a SPICE-like operating region from compact-model voltages."""
    if current is not None and current <= 0:
        return "off"
    if vds <= 0 or vdsat <= 0:
        return "unknown"
    return "saturation" if vds >= vdsat else "linear"


def inversion_region_from_gm_id(gm_id: float) -> str:
    """Classify inversion level without overloading the SPICE operating region."""
    if gm_id <= 0:
        return "unknown"
    if gm_id < 15:
        return "strong"
    if gm_id < 22:
        return "moderate"
    return "weak"


def normalize_spice_region(
    value: Any,
    *,
    vds: float = 0.0,
    vdsat: float = 0.0,
    current: float | None = None,
) -> str:
    region = str(value or "unknown").lower()
    if region in SPICE_OPERATING_REGIONS:
        return region
    return compact_operating_region(vds, vdsat, current)

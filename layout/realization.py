from __future__ import annotations

import math
from dataclasses import dataclass

from schemas.design_state import DesignState


@dataclass(frozen=True)
class LayoutRealization:
    effective_W: float
    effective_L: float
    instance_W: float
    finger_W: float
    segment_L: float
    fingers: int
    parallel: int
    series: int

    @property
    def folded(self) -> bool:
        return self.fingers > 1 or self.parallel > 1

    @property
    def segmented(self) -> bool:
        return self.series > 1


def realize_transistor_layout(state: DesignState, device_id: str) -> LayoutRealization:
    ts = state.transistors[device_id]
    proc = state.process
    W = float(ts.parameters.W or 0.0)
    L = float(ts.parameters.L or ts.L_strategy or 0.0)

    min_w = float(getattr(proc, "min_W", 150e-9) or 150e-9)
    max_w = float(getattr(proc, "max_W", 200e-6) or 200e-6)
    max_finger_w = float(getattr(proc, "max_finger_width", max_w) or max_w)
    max_l = float(getattr(proc, "max_L", 10e-6) or 10e-6)

    if W <= 0.0:
        W = min_w
    if L <= 0.0:
        L = float(getattr(proc, "min_L", 130e-9) or 130e-9)

    max_instance_w = max(max_w, min_w)
    parallel = max(1, math.ceil(W / max_instance_w))
    instance_w = W / parallel

    finger_limit = max(min(max_finger_w, max_instance_w), min_w)
    fingers = max(1, math.ceil(instance_w / finger_limit))
    if instance_w / fingers < min_w:
        fingers = max(1, math.floor(instance_w / min_w))
    finger_w = instance_w / max(fingers, 1)

    series = max(1, math.ceil(L / max_l)) if max_l > 0.0 else 1
    segment_l = L / series

    return LayoutRealization(
        effective_W=W,
        effective_L=L,
        instance_W=instance_w,
        finger_W=finger_w,
        segment_L=segment_l,
        fingers=fingers,
        parallel=parallel,
        series=series,
    )

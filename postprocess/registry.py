from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from asir.capabilities import CircuitCapabilities
from asir.profiles import CircuitProfile
from postprocess.cascode import (
    tune_cascode_ota_operating_point,
    tune_current_mirror_ota_operating_point,
)
from postprocess.ota import tune_single_stage_ota_operating_point
from postprocess.source_follower import tune_source_follower_operating_point
from postprocess.two_stage import TwoStagePostProcessor
from schemas.design_state import DesignState
from simulator.ngspice import NgspiceSimulator


@dataclass(frozen=True)
class PostprocessConfig:
    skip_dc_repair: bool = False
    skip_comp_tune: bool = False


@dataclass(frozen=True)
class PostprocessContext:
    state: DesignState
    sim: NgspiceSimulator
    work_dir: Path
    config: PostprocessConfig
    profile: CircuitProfile
    capabilities: CircuitCapabilities


class PostprocessPass(Protocol):
    name: str

    def applies(self, context: PostprocessContext) -> bool:
        ...

    def run(self, context: PostprocessContext) -> list[dict]:
        ...


@dataclass(frozen=True)
class TwoStagePass:
    name: str = "two_stage"

    def applies(self, context: PostprocessContext) -> bool:
        return context.capabilities.has("two_stage_gain")

    def run(self, context: PostprocessContext) -> list[dict]:
        processor = TwoStagePostProcessor(
            skip_dc_repair=context.config.skip_dc_repair,
            skip_comp_tune=context.config.skip_comp_tune,
        )
        return processor.run(context.state, context.sim, context.work_dir)


@dataclass(frozen=True)
class SingleStageOTAPass:
    name: str = "single_stage_ota_operating_point"

    def applies(self, context: PostprocessContext) -> bool:
        return (
            context.profile.name == "ota"
            and context.capabilities.has("explicit_bias_ports")
            and not context.capabilities.has("two_stage_gain")
            and not context.capabilities.has("cascode_ota")
            and not context.capabilities.has("current_mirror_ota")
            and not context.capabilities.has("source_follower_regulation")
            and not context.config.skip_dc_repair
        )

    def run(self, context: PostprocessContext) -> list[dict]:
        event = tune_single_stage_ota_operating_point(context.state, context.sim, context.work_dir)
        return [{"type": "single_stage_ota_op_tune", **event}] if event else []


@dataclass(frozen=True)
class CurrentMirrorOTAPass:
    name: str = "current_mirror_ota_operating_point"

    def applies(self, context: PostprocessContext) -> bool:
        return (
            context.profile.name == "ota"
            and context.capabilities.has("current_mirror_ota")
            and context.capabilities.has("explicit_bias_ports")
            and not context.capabilities.has("two_stage_gain")
            and not context.capabilities.has("source_follower_regulation")
            and not context.config.skip_dc_repair
        )

    def run(self, context: PostprocessContext) -> list[dict]:
        event = tune_current_mirror_ota_operating_point(context.state, context.sim, context.work_dir)
        return [{"type": "current_mirror_ota_op_tune", **event}] if event else []


@dataclass(frozen=True)
class CascodeOTAPass:
    name: str = "cascode_ota_operating_point"

    def applies(self, context: PostprocessContext) -> bool:
        return (
            context.profile.name == "ota"
            and context.capabilities.has("cascode_ota")
            and context.capabilities.has("explicit_bias_ports")
            and not context.capabilities.has("two_stage_gain")
            and not context.capabilities.has("source_follower_regulation")
            and not context.config.skip_dc_repair
        )

    def run(self, context: PostprocessContext) -> list[dict]:
        event = tune_cascode_ota_operating_point(context.state, context.sim, context.work_dir)
        return [{"type": "cascode_ota_op_tune", **event}] if event else []


@dataclass(frozen=True)
class SourceFollowerOperatingPointPass:
    name: str = "source_follower_operating_point"

    def applies(self, context: PostprocessContext) -> bool:
        return (
            context.capabilities.has("source_follower_regulation")
            and not context.config.skip_dc_repair
        )

    def run(self, context: PostprocessContext) -> list[dict]:
        event = tune_source_follower_operating_point(context.state, context.sim, context.work_dir)
        return [{"type": "source_follower_op_tune", **event}] if event else []


class PostprocessRegistry:
    def __init__(self, passes: list[PostprocessPass] | None = None) -> None:
        self.passes = passes or [
            TwoStagePass(),
            CascodeOTAPass(),
            CurrentMirrorOTAPass(),
            SingleStageOTAPass(),
            SourceFollowerOperatingPointPass(),
        ]

    def resolve(self, context: PostprocessContext) -> list[PostprocessPass]:
        return [postprocess_pass for postprocess_pass in self.passes if postprocess_pass.applies(context)]

    def run(self, context: PostprocessContext) -> list[dict]:
        events: list[dict] = []
        for postprocess_pass in self.resolve(context):
            events.extend(postprocess_pass.run(context))
        return events

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from diagnostics import apply_attribution_guided_tuning
from flow.runner import AnalogRFIRFlowRunner, FlowConfig, FlowResult
from frontends.design_input import StateBuilder


Emit = Callable[[str], None]


@dataclass
class AgentLoopResult:
    rounds: list[dict[str, Any]]
    final_result: FlowResult


class DiagnosticAgentLoop:
    def __init__(
        self,
        *,
        config: FlowConfig,
        rounds: int,
        legacy_state_builder: StateBuilder | None = None,
        emit: Emit | None = None,
    ) -> None:
        self.config = config
        self.rounds = max(1, int(rounds))
        self.legacy_state_builder = legacy_state_builder
        self.emit = emit or (lambda _msg: None)

    def run(self) -> AgentLoopResult:
        loop_dir = Path(self.config.runs_dir) / f"agent_loop_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        loop_dir.mkdir(parents=True, exist_ok=True)
        current_schema = self.config.schema
        summaries: list[dict[str, Any]] = []
        final_result: FlowResult | None = None

        for round_index in range(1, self.rounds + 1):
            self.emit("\n" + "=" * 70)
            self.emit(f"  Diagnostic agent round {round_index}/{self.rounds}")
            self.emit("=" * 70)
            round_seed = None if self.config.seed is None else int(self.config.seed) + round_index - 1
            round_config = replace(self.config, schema=current_schema, seed=round_seed)
            result = AnalogRFIRFlowRunner(
                config=round_config,
                legacy_state_builder=self.legacy_state_builder,
                emit=self.emit,
            ).run()
            final_result = result
            summary = self._round_summary(round_index, result)
            summaries.append(summary)
            self._print_round_summary(summary)

            if round_index >= self.rounds or summary["spec_pass"]:
                break

            application = apply_attribution_guided_tuning(result.state, round_index=round_index)
            self._print_application(application)
            if not application["applied_actions"]:
                self.emit("       No automatic tuning actions were applied; stopping diagnostic loop.")
                break
            tuned_schema = loop_dir / f"round_{round_index + 1:03d}_input.yaml"
            result.state.to_yaml(tuned_schema)
            current_schema = tuned_schema
            self.emit(f"       Next schema: {tuned_schema}")

        if final_result is None:
            raise RuntimeError("Diagnostic agent loop did not run any rounds")
        return AgentLoopResult(rounds=summaries, final_result=final_result)

    def _round_summary(self, round_index: int, result: FlowResult) -> dict[str, Any]:
        status = result.state.diagnostics.get("result", {}).get("status", {})
        causal = result.state.diagnostics.get("causal_diagnostics", {})
        tuning = causal.get("attribution_guided_tuning", {})
        failures = tuning.get("by_failure", [])
        return {
            "round": round_index,
            "artifact_dir": str(result.artifacts.output_dir),
            "spec_pass": bool(status.get("spec_pass", False)),
            "failed_targets": list(status.get("failed_targets", [])),
            "best_loss": status.get("best_loss"),
            "top_actions": [
                action
                for failure in failures
                for action in failure.get("actions", [])[:3]
            ],
        }

    def _print_round_summary(self, summary: dict[str, Any]) -> None:
        self.emit("       Round summary:")
        self.emit(f"         artifacts: {summary['artifact_dir']}")
        self.emit(f"         spec_pass: {summary['spec_pass']}")
        self.emit(f"         failed_targets: {summary['failed_targets']}")
        for action in summary["top_actions"][:5]:
            self.emit(
                "         tuning candidate: "
                f"{action.get('knob')} {action.get('direction')} "
                f"step={action.get('agent_step_fraction')} "
                f"next={action.get('suggested_next_value')}"
            )

    def _print_application(self, application: dict[str, Any]) -> None:
        self.emit("       Applied agent tuning:")
        for action in application.get("applied_actions", []):
            knobs = ", ".join(item["knob"] for item in action.get("applied_knobs", []))
            values = ", ".join(f"{item['new_initial']:.4e}" for item in action.get("applied_knobs", []))
            self.emit(f"         {knobs}: new_initial={values}")
        for action in application.get("skipped_actions", []):
            self.emit(f"         skipped {action.get('knob')}: {action.get('reason')}")


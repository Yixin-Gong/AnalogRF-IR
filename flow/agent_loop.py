from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph

from core.environment import build_process_info, build_simulation_config, load_environment
from diagnostics import execute_tuning_tool_commands
from flow.llm_planner import DeepSeekSchemaPlanner, LLMPlannerConfig
from flow.runner import AnalogRFIRFlowRunner, FlowConfig, FlowResult
from frontends.design_input import StateBuilder
from schemas.design_state import DesignState


Emit = Callable[[str], None]


@dataclass
class AgentLoopResult:
    rounds: list[dict[str, Any]]
    final_result: FlowResult


class AgentGraphState(TypedDict, total=False):
    current_schema: str
    loop_dir: str
    round_index: int
    max_rounds: int
    rounds: list[dict[str, Any]]
    last_design_state: str
    last_artifact_dir: str
    last_spec_pass: bool
    last_failed_targets: list[str]
    agent_model: dict[str, Any]
    llm_planner: dict[str, Any]
    last_tool_command: dict[str, Any]
    last_tuning_application: dict[str, Any]
    force_postprocess_once: bool
    postprocess_rescue_attempted: bool
    stop_reason: str


class DiagnosticAgentLoop:
    def __init__(
        self,
        *,
        config: FlowConfig,
        rounds: int,
        legacy_state_builder: StateBuilder | None = None,
        llm_config: LLMPlannerConfig | None = None,
        emit: Emit | None = None,
    ) -> None:
        self.config = config
        self.rounds = max(1, int(rounds))
        self.legacy_state_builder = legacy_state_builder
        self.llm_config = llm_config or LLMPlannerConfig.from_env()
        self.emit = emit or (lambda _msg: None)
        self._final_result: FlowResult | None = None
        self._best_result: FlowResult | None = None
        self._best_summary: dict[str, Any] | None = None
        self._environment = load_environment(config.env)

    def run(self) -> AgentLoopResult:
        loop_dir = Path(self.config.runs_dir) / f"agent_loop_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        loop_dir.mkdir(parents=True, exist_ok=True)
        graph = self._build_graph()
        final_state = graph.invoke(
            {
                "current_schema": str(self.config.schema),
                "loop_dir": str(loop_dir),
                "round_index": 1,
                "max_rounds": self.rounds,
                "rounds": [],
                "force_postprocess_once": False,
                "postprocess_rescue_attempted": False,
                "stop_reason": "",
            },
            config={"recursion_limit": max(12, self.rounds * 5 + 4)},
        )
        if self._final_result is None:
            raise RuntimeError("Diagnostic agent loop did not run any rounds")
        if final_state.get("stop_reason"):
            self.emit(f"       Agent graph stopped: {final_state['stop_reason']}")
        if self._best_result is not None:
            self._final_result = self._best_result
        self._print_final_result(final_state)
        return AgentLoopResult(rounds=final_state.get("rounds", []), final_result=self._final_result)

    def _build_graph(self):
        graph = StateGraph(AgentGraphState)
        graph.add_node("execute_main_flow", self._execute_main_flow_node)
        graph.add_node("read_schema_diagnostics", self._read_schema_diagnostics_node)
        graph.add_node("llm_write_schema_command", self._llm_write_schema_command_node)
        graph.add_node("execute_schema_command", self._execute_schema_command_node)
        graph.set_entry_point("execute_main_flow")
        graph.add_edge("execute_main_flow", "read_schema_diagnostics")
        graph.add_conditional_edges(
            "read_schema_diagnostics",
            self._route_after_diagnostics,
            {"write_command": "llm_write_schema_command", "stop": END},
        )
        graph.add_edge("llm_write_schema_command", "execute_schema_command")
        graph.add_conditional_edges(
            "execute_schema_command",
            self._route_after_schema_edit,
            {"execute": "execute_main_flow", "stop": END},
        )
        return graph.compile()

    def _execute_main_flow_node(self, state: AgentGraphState) -> AgentGraphState:
        round_index = int(state["round_index"])
        self.emit("\n" + "=" * 70)
        self.emit(f"  LangGraph node: execute_main_flow ({round_index}/{state['max_rounds']})")
        self.emit("=" * 70)
        round_seed = None if self.config.seed is None else int(self.config.seed) + round_index - 1
        round_config = self._round_flow_config(state, round_index, round_seed)
        result = AnalogRFIRFlowRunner(
            config=round_config,
            legacy_state_builder=self.legacy_state_builder,
            emit=self.emit,
        ).run()
        self._final_result = result
        summary = self._round_summary(round_index, result)
        if self._is_better_summary(summary, self._best_summary):
            self._best_result = result
            self._best_summary = summary
        self._print_round_summary(summary)
        return {
            "rounds": list(state.get("rounds", [])) + [summary],
            "last_design_state": str(result.artifacts.design_state),
            "last_artifact_dir": str(result.artifacts.output_dir),
            "last_spec_pass": bool(summary["spec_pass"]),
            "last_failed_targets": list(summary["failed_targets"]),
            "force_postprocess_once": False,
            "stop_reason": "",
        }

    def _read_schema_diagnostics_node(self, state: AgentGraphState) -> AgentGraphState:
        self.emit("       LangGraph node: read_schema_diagnostics")
        design_state_path = Path(state["last_design_state"])
        schema_state = self._load_design_state(design_state_path)
        status = schema_state.diagnostics.get("result", {}).get("status", {})
        causal = schema_state.diagnostics.get("causal_diagnostics", {})
        tuning = causal.get("attribution_guided_tuning", {})
        ranking_comparison = causal.get("sensitivity_ranking_comparison", {})
        intervention_model = causal.get("local_intervention_summary", {}) or causal.get("local_intervention_model", {})
        action_optimizer = causal.get("constrained_action_optimizer", {})
        agent_model = {
            "state_source": str(design_state_path),
            "status": status,
            "failed_targets": list(status.get("failed_targets", [])),
            "causal_root_causes": causal.get("root_cause_attribution", [])[:5],
            "local_intervention_model": {
                "method": intervention_model.get("method", ""),
                "status": intervention_model.get("status", ""),
                "metrics": intervention_model.get("metrics", []),
                "base_violation_vector": intervention_model.get("base_violation_vector", {}),
                "action_count": intervention_model.get("action_count"),
                "ok_action_count": intervention_model.get("ok_action_count"),
                "evidence_location": intervention_model.get("evidence_location", ""),
            },
            "constrained_action_optimizer": {
                "status": action_optimizer.get("status", ""),
                "model_source": action_optimizer.get("model_source", ""),
                "objective_before": action_optimizer.get("objective_before"),
                "objective_after": action_optimizer.get("objective_after"),
                "selected_actions": action_optimizer.get("selected_actions", [])[:5],
            },
            "ranking_comparison": {
                "decision_rule": ranking_comparison.get("decision_rule", ""),
                "legacy_role": ranking_comparison.get("legacy_role", ""),
                "causal_top": ranking_comparison.get("causal_top", [])[:5],
                "legacy_sensitivity_top": ranking_comparison.get("legacy_sensitivity_top", [])[:5],
                "top5_overlap_count": ranking_comparison.get("top5_overlap_count", 0),
                "divergences": ranking_comparison.get("divergences", [])[:3],
            },
            "tuning_failures": [
                {
                    "metric": item.get("metric"),
                    "strategy": item.get("strategy"),
                    "actions": item.get("actions", [])[:3],
                }
                for item in tuning.get("by_failure", [])
            ],
        }
        stop_reason = ""
        if bool(status.get("spec_pass", False)):
            stop_reason = "spec satisfied"
        elif int(state.get("round_index", 1)) >= int(state.get("max_rounds", 1)):
            stop_reason = "maximum iterations reached"
        return {
            "agent_model": agent_model,
            "last_spec_pass": bool(status.get("spec_pass", False)),
            "last_failed_targets": list(status.get("failed_targets", [])),
            "stop_reason": stop_reason,
        }

    def _execute_schema_command_node(self, state: AgentGraphState) -> AgentGraphState:
        self.emit("       LangGraph node: execute_schema_command")
        schema_state = self._load_design_state(Path(state["last_design_state"]))
        round_index = int(state["round_index"])
        application = execute_tuning_tool_commands(schema_state, round_index=round_index)
        self._print_application(application)
        if not application["applied_actions"]:
            if self._should_force_postprocess_rescue(state):
                self.emit("       No schema edits were available; running one forced postprocess fallback before stopping")
                return {
                    "current_schema": state["current_schema"],
                    "round_index": round_index + 1,
                    "last_tuning_application": application,
                    "force_postprocess_once": True,
                    "postprocess_rescue_attempted": True,
                    "stop_reason": "",
                }
            return {
                "last_tuning_application": application,
                "stop_reason": "no automatic schema edits were available",
            }
        tuned_schema = Path(state["loop_dir"]) / f"round_{round_index + 1:03d}_input.yaml"
        schema_state.diagnostics = _next_round_diagnostics(application)
        schema_state.to_yaml(tuned_schema, include_runtime_context=False)
        self.emit(f"       Next schema: {tuned_schema}")
        return {
            "current_schema": str(tuned_schema),
            "round_index": round_index + 1,
            "last_tuning_application": application,
            "stop_reason": "",
        }

    def _llm_write_schema_command_node(self, state: AgentGraphState) -> AgentGraphState:
        self.emit("       LangGraph node: llm_write_schema_command")
        design_state_path = Path(state["last_design_state"])
        schema_state = self._load_design_state(design_state_path)
        planner = DeepSeekSchemaPlanner(self.llm_config)
        result = planner.write_command(
            schema_state,
            round_index=int(state["round_index"]),
            agent_model=state.get("agent_model", {}),
        )
        command = result.command
        schema_state.to_yaml(design_state_path, include_runtime_context=False)
        self.emit(
            "       Wrote schema command: "
            f"{command['id']} with {len(command['args'].get('available_actions', []))} available actions "
            f"and {len(command['args'].get('selected_actions', []))} selected actions"
        )
        self.emit(
            "       LLM planner: "
            f"{self.llm_config.provider}/{self.llm_config.model} status={result.status} reason={result.reason}"
        )
        return {
            "last_tool_command": command,
            "llm_planner": command.get("llm_planner", {}),
        }

    def _round_flow_config(
        self,
        state: AgentGraphState,
        round_index: int,
        round_seed: int | None,
    ) -> FlowConfig:
        generations = int(self.config.generations)
        pop_size = int(self.config.pop_size)
        if round_index > 1:
            generations = self._short_reopt_generations()
            pop_size = self._short_reopt_pop_size()
        force_once = bool(state.get("force_postprocess_once", False))
        stagnated = self._stagnation_detected(state.get("rounds", []))
        force_postprocess = force_once or stagnated
        if force_once:
            self.emit("       Adaptive policy: enabling postprocess fallback because no admissible schema edit was available")
        elif stagnated:
            self.emit("       Adaptive policy: enabling postprocess fallback because recent rounds stagnated")
        return replace(
            self.config,
            schema=state["current_schema"],
            seed=round_seed,
            generations=generations,
            pop_size=pop_size,
            force_postprocess_fallback=force_postprocess,
        )

    def _short_reopt_generations(self) -> int:
        if self.config.reopt_generations is not None:
            return max(1, int(self.config.reopt_generations))
        return max(2, min(int(self.config.generations), 4))

    def _short_reopt_pop_size(self) -> int:
        if self.config.reopt_pop_size is not None:
            return max(4, int(self.config.reopt_pop_size))
        return max(8, min(int(self.config.pop_size), max(8, int(self.config.pop_size) // 2)))

    @staticmethod
    def _stagnation_detected(rounds: list[dict[str, Any]]) -> bool:
        if len(rounds) < 2:
            return False
        recent = rounds[-2:]
        losses = []
        for item in recent:
            try:
                losses.append(float(item.get("best_loss")))
            except (TypeError, ValueError):
                return False
        if losses[0] <= 0:
            return False
        improvement = losses[0] - losses[1]
        return improvement <= max(1e-9, 0.03 * abs(losses[0]))

    def _should_force_postprocess_rescue(self, state: AgentGraphState) -> bool:
        if str(self.config.postprocess_policy or "fallback").lower() != "fallback":
            return False
        if self.config.skip_dc_repair and self.config.skip_comp_tune:
            return False
        if state.get("postprocess_rescue_attempted"):
            return False
        if int(state.get("round_index", 1)) >= int(state.get("max_rounds", 1)):
            return False
        rounds = state.get("rounds", []) or []
        if not rounds or rounds[-1].get("spec_pass"):
            return False
        return int(rounds[-1].get("postprocess_event_count", 0) or 0) == 0

    def _load_design_state(self, path: Path) -> DesignState:
        schema_state = DesignState.from_yaml(path)
        env = getattr(self, "_environment", None)
        if env is None:
            config = getattr(self, "config", None)
            env = load_environment(config.env if config else None)
            self._environment = env
        schema_state.process = build_process_info(env)
        schema_state.simulation = build_simulation_config(env)
        return schema_state

    def _route_after_diagnostics(self, state: AgentGraphState) -> str:
        if state.get("stop_reason"):
            return "stop"
        return "write_command"

    def _route_after_schema_edit(self, state: AgentGraphState) -> str:
        if state.get("stop_reason"):
            return "stop"
        return "execute"

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
            "postprocess_decision": result.flow_meta.get("postprocess_decision", {}),
            "postprocess_event_count": len(result.flow_meta.get("postprocess", []) or []),
        }

    @staticmethod
    def _is_better_summary(candidate: dict[str, Any], incumbent: dict[str, Any] | None) -> bool:
        if incumbent is None:
            return True
        return _summary_rank(candidate) < _summary_rank(incumbent)

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
        pp = summary.get("postprocess_decision", {}) or {}
        self.emit(
            "         postprocess: "
            f"run={pp.get('run')} reason={pp.get('reason', '')} "
            f"events={summary.get('postprocess_event_count', 0)}"
        )

    def _print_application(self, application: dict[str, Any]) -> None:
        self.emit("       Applied agent tuning:")
        for action in application.get("applied_actions", []):
            knobs = ", ".join(item["knob"] for item in action.get("applied_knobs", []))
            values = ", ".join(f"{item['new_initial']:.4e}" for item in action.get("applied_knobs", []))
            self.emit(f"         {knobs}: new_initial={values}")
        for action in application.get("skipped_actions", []):
            self.emit(f"         skipped {action.get('knob')}: {action.get('reason')}")

    def _print_final_result(self, state: AgentGraphState) -> None:
        rounds = state.get("rounds", [])
        final_round = getattr(self, "_best_summary", None) or (rounds[-1] if rounds else {})
        self.emit("\n" + "=" * 70)
        self.emit("  LangGraph final result")
        self.emit("=" * 70)
        self.emit(f"       stop_reason: {state.get('stop_reason') or 'not set'}")
        self.emit(f"       completed_iterations: {len(rounds)}")
        self.emit(f"       max_iterations: {state.get('max_rounds')}")
        self.emit(f"       final_artifacts: {final_round.get('artifact_dir', state.get('last_artifact_dir', ''))}")
        self.emit(f"       spec_pass: {final_round.get('spec_pass', state.get('last_spec_pass', False))}")
        self.emit(f"       failed_targets: {final_round.get('failed_targets', state.get('last_failed_targets', []))}")
        self.emit(f"       best_loss: {final_round.get('best_loss')}")


def _summary_rank(summary: dict[str, Any]) -> tuple[float, float, float]:
    try:
        best_loss = float(summary.get("best_loss"))
    except (TypeError, ValueError):
        best_loss = float("inf")
    failed_count = len(summary.get("failed_targets", []) or [])
    return (
        0.0 if summary.get("spec_pass", False) else 1.0,
        float(failed_count),
        best_loss,
    )


def _next_round_diagnostics(application: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "analogrf_ir.state_diagnostics.v0_3",
        "previous_agent_tuning": {
            "round_index": application.get("round_index"),
            "command_id": application.get("command_id", ""),
            "applied_action_count": len(application.get("applied_actions", []) or []),
            "skipped_action_count": len(application.get("skipped_actions", []) or []),
            "applied_actions": [
                {
                    "action_id": action.get("action_id"),
                    "knob": action.get("knob"),
                    "apply_to": action.get("apply_to", []),
                    "applied_knobs": action.get("applied_knobs", []),
                }
                for action in application.get("applied_actions", []) or []
            ],
        },
    }

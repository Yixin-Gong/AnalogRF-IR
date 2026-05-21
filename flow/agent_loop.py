from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph

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
                "stop_reason": "",
            },
            config={"recursion_limit": max(12, self.rounds * 5 + 4)},
        )
        if self._final_result is None:
            raise RuntimeError("Diagnostic agent loop did not run any rounds")
        if final_state.get("stop_reason"):
            self.emit(f"       Agent graph stopped: {final_state['stop_reason']}")
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
        round_config = replace(self.config, schema=state["current_schema"], seed=round_seed)
        result = AnalogRFIRFlowRunner(
            config=round_config,
            legacy_state_builder=self.legacy_state_builder,
            emit=self.emit,
        ).run()
        self._final_result = result
        summary = self._round_summary(round_index, result)
        self._print_round_summary(summary)
        return {
            "rounds": list(state.get("rounds", [])) + [summary],
            "last_design_state": str(result.artifacts.design_state),
            "last_artifact_dir": str(result.artifacts.output_dir),
            "last_spec_pass": bool(summary["spec_pass"]),
            "last_failed_targets": list(summary["failed_targets"]),
            "stop_reason": "",
        }

    def _read_schema_diagnostics_node(self, state: AgentGraphState) -> AgentGraphState:
        self.emit("       LangGraph node: read_schema_diagnostics")
        design_state_path = Path(state["last_design_state"])
        schema_state = DesignState.from_yaml(design_state_path)
        status = schema_state.diagnostics.get("result", {}).get("status", {})
        causal = schema_state.diagnostics.get("causal_diagnostics", {})
        tuning = causal.get("attribution_guided_tuning", {})
        agent_model = {
            "state_source": str(design_state_path),
            "status": status,
            "failed_targets": list(status.get("failed_targets", [])),
            "tuning_failures": [
                {
                    "metric": item.get("metric"),
                    "strategy": item.get("strategy"),
                    "actions": item.get("actions", [])[:3],
                }
                for item in tuning.get("by_failure", [])
            ],
        }
        return {
            "agent_model": agent_model,
            "last_spec_pass": bool(status.get("spec_pass", False)),
            "last_failed_targets": list(status.get("failed_targets", [])),
        }

    def _execute_schema_command_node(self, state: AgentGraphState) -> AgentGraphState:
        self.emit("       LangGraph node: execute_schema_command")
        schema_state = DesignState.from_yaml(Path(state["last_design_state"]))
        round_index = int(state["round_index"])
        application = execute_tuning_tool_commands(schema_state, round_index=round_index)
        self._print_application(application)
        if not application["applied_actions"]:
            return {
                "last_tuning_application": application,
                "stop_reason": "no automatic schema edits were available",
            }
        tuned_schema = Path(state["loop_dir"]) / f"round_{round_index + 1:03d}_input.yaml"
        schema_state.to_yaml(tuned_schema)
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
        schema_state = DesignState.from_yaml(design_state_path)
        planner = DeepSeekSchemaPlanner(self.llm_config)
        result = planner.write_command(
            schema_state,
            round_index=int(state["round_index"]),
            agent_model=state.get("agent_model", {}),
        )
        command = result.command
        schema_state.to_yaml(design_state_path)
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

    def _route_after_diagnostics(self, state: AgentGraphState) -> str:
        if state.get("last_spec_pass"):
            return "stop"
        if int(state.get("round_index", 1)) >= int(state.get("max_rounds", 1)):
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

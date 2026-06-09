from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph

from core.environment import build_process_info, build_simulation_config, load_environment
from diagnostics import (
    apply_optimized_action_plan,
    build_spice_intervention_model,
    default_selected_actions_from_optimizer,
    execute_tuning_tool_commands,
    optimize_tuning_actions,
    write_tuning_tool_command,
)
from flow.llm_planner import DeepSeekSchemaPlanner, LLMPlannerConfig
from flow.runner import AnalogRFIRFlowRunner, FlowConfig, FlowResult
from frontends.design_input import StateBuilder
from schemas.design_state import DesignState
from simulator.ngspice import NgspiceSimulator
from specs.models import SpecRegistry


Emit = Callable[[str], None]

_MEASUREMENT_BY_TARGET = {
    "dc_gain": "dc_gain_db",
    "unity_gain_bandwidth": "unity_gain_bandwidth",
    "phase_margin": "phase_margin",
    "slew_rate": "slew_rate",
    "slew_rate_pos": "slew_rate_pos",
    "slew_rate_neg": "slew_rate_neg",
    "output_swing": "output_swing",
    "saturation_margin": "saturation_margin",
    "power": "total_power",
}


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
        loop_started = time.perf_counter()
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
        self.emit(f"       [timing] agent_loop_total: {time.perf_counter() - loop_started:.2f}s")
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
        node_started = time.perf_counter()
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
        summary["round_elapsed_sec"] = round(time.perf_counter() - node_started, 6)
        if self._is_better_summary(summary, self._best_summary):
            self._best_result = result
            self._best_summary = summary
        rescue_attempted = bool(state.get("postprocess_rescue_attempted", False))
        if round_config.force_postprocess_fallback and summary.get("postprocess_event_count", 0):
            rescue_attempted = True
        self._print_round_summary(summary)
        self.emit(f"       [timing] agent.execute_main_flow.round_{round_index}: {summary['round_elapsed_sec']:.2f}s")
        return {
            "rounds": list(state.get("rounds", [])) + [summary],
            "last_design_state": str(result.artifacts.design_state),
            "last_artifact_dir": str(result.artifacts.output_dir),
            "last_spec_pass": bool(summary["spec_pass"]),
            "last_failed_targets": list(summary["failed_targets"]),
            "force_postprocess_once": False,
            "postprocess_rescue_attempted": rescue_attempted,
            "stop_reason": "",
        }

    def _read_schema_diagnostics_node(self, state: AgentGraphState) -> AgentGraphState:
        node_started = time.perf_counter()
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
            "llm_policy": self._llm_policy(),
            "status": status,
            "failed_targets": list(status.get("failed_targets", [])),
            "unverified_targets": list(status.get("unverified_targets", [])),
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
        elif (
            self._runtime_stagnation_detected(state.get("rounds", []))
            and not self._should_force_postprocess_rescue(state)
            and not _is_residual_llm_policy(self._llm_policy())
        ):
            stop_reason = "runtime stagnation: measured violations stopped improving"
        self.emit(f"       [timing] agent.read_schema_diagnostics: {time.perf_counter() - node_started:.2f}s")
        return {
            "agent_model": agent_model,
            "last_spec_pass": bool(status.get("spec_pass", False)),
            "last_failed_targets": list(status.get("failed_targets", [])),
            "last_unverified_targets": list(status.get("unverified_targets", [])),
            "stop_reason": stop_reason,
        }

    def _execute_schema_command_node(self, state: AgentGraphState) -> AgentGraphState:
        node_started = time.perf_counter()
        self.emit("       LangGraph node: execute_schema_command")
        command_has_selection = _command_has_executable_selection(state.get("last_tool_command", {}))
        if self._should_prefer_postprocess_before_schema_edit(state) and not (
            _is_residual_llm_policy(self._llm_policy()) and command_has_selection
        ):
            round_index = int(state["round_index"])
            self.emit("       Close repair-sensitive failure; running one forced postprocess fallback before schema edits")
            self.emit(f"       [timing] agent.execute_schema_command: {time.perf_counter() - node_started:.2f}s")
            return {
                "current_schema": state["current_schema"],
                "round_index": round_index + 1,
                "last_tuning_application": {
                    "applied_actions": [],
                    "skipped_actions": [],
                    "reason": "close repair-sensitive failure; postprocess fallback preferred before schema edit",
                },
                "force_postprocess_once": True,
                "postprocess_rescue_attempted": True,
                "stop_reason": "",
            }
        schema_state = self._load_design_state(Path(state["last_design_state"]))
        round_index = int(state["round_index"])
        application = execute_tuning_tool_commands(schema_state, round_index=round_index)
        self._print_application(application)
        if not application["applied_actions"]:
            if self._should_force_postprocess_rescue(state):
                self.emit("       No schema edits were available; running one forced postprocess fallback before stopping")
                self.emit(f"       [timing] agent.execute_schema_command: {time.perf_counter() - node_started:.2f}s")
                return {
                    "current_schema": state["current_schema"],
                    "round_index": round_index + 1,
                    "last_tuning_application": application,
                    "force_postprocess_once": True,
                    "postprocess_rescue_attempted": True,
                    "stop_reason": "",
                }
            self.emit(f"       [timing] agent.execute_schema_command: {time.perf_counter() - node_started:.2f}s")
            return {
                "last_tuning_application": application,
                "stop_reason": "no automatic schema edits were available",
            }
        tuned_schema = Path(state["loop_dir"]) / f"round_{round_index + 1:03d}_input.yaml"
        schema_state.diagnostics = _next_round_diagnostics(application)
        schema_state.to_yaml(tuned_schema, include_runtime_context=False)
        self.emit(f"       Next schema: {tuned_schema}")
        force_postprocess = bool(getattr(self.config, "force_postprocess_after_schema_edit", False))
        if force_postprocess:
            self.emit("       Adaptive policy: enabling one postprocess fallback after the applied schema edit")
        self.emit(f"       [timing] agent.execute_schema_command: {time.perf_counter() - node_started:.2f}s")
        return {
            "current_schema": str(tuned_schema),
            "round_index": round_index + 1,
            "last_tuning_application": application,
            "force_postprocess_once": force_postprocess,
            "stop_reason": "",
        }

    def _llm_write_schema_command_node(self, state: AgentGraphState) -> AgentGraphState:
        node_started = time.perf_counter()
        self.emit("       LangGraph node: llm_write_schema_command")
        design_state_path = Path(state["last_design_state"])
        schema_state = self._load_design_state(design_state_path)
        llm_policy = self._llm_policy()
        if not _is_residual_llm_policy(llm_policy) and self._should_prefer_postprocess_before_schema_edit(state):
            command = write_tuning_tool_command(
                schema_state,
                round_index=int(state["round_index"]),
                author="postprocess_rescue_fast_path",
                selected_actions=[],
            )
            command["llm_planner"] = {
                "provider": self.llm_config.provider,
                "model": self.llm_config.model,
                "policy": llm_policy,
                "status": "skipped",
                "reason": "Close repair-sensitive failure; postprocess fallback is preferred before schema edits.",
                "elapsed_sec": round(time.perf_counter() - node_started, 6),
            }
            if llm_policy == "shadow":
                self._attach_llm_shadow(command, schema_state, state, source="postprocess_rescue_fast_path")
            schema_state.to_yaml(design_state_path, include_runtime_context=False)
            self.emit("       Skipped LLM request: close repair-sensitive failure will use postprocess fallback")
            self.emit(f"       [timing] agent.llm_write_schema_command.rescue_fast_path: {time.perf_counter() - node_started:.2f}s")
            return {
                "last_tool_command": command,
                "llm_planner": command.get("llm_planner", {}),
            }
        if not _is_residual_llm_policy(llm_policy):
            fast_command = self._optimizer_evidence_fast_path(schema_state, int(state["round_index"]))
            if fast_command is not None:
                if llm_policy == "shadow":
                    self._attach_llm_shadow(fast_command, schema_state, state, source="optimizer_evidence_fast_path")
                schema_state.to_yaml(design_state_path, include_runtime_context=False)
                selected = fast_command.get("args", {}).get("selected_actions", []) or []
                self.emit(
                    "       Skipped LLM request: constrained optimizer already selected "
                    f"{len(selected)} admissible action(s)"
                )
                fast_command.setdefault("llm_planner", {})["elapsed_sec"] = round(time.perf_counter() - node_started, 6)
                fast_command.setdefault("llm_planner", {})["policy"] = llm_policy
                self.emit(f"       [timing] agent.llm_write_schema_command.fast_path: {time.perf_counter() - node_started:.2f}s")
                return {
                    "last_tool_command": fast_command,
                    "llm_planner": fast_command.get("llm_planner", {}),
                }
        planner = DeepSeekSchemaPlanner(self.llm_config)
        planner_started = time.perf_counter()
        result = planner.write_command(
            schema_state,
            round_index=int(state["round_index"]),
            agent_model=state.get("agent_model", {}),
        )
        planner_elapsed = time.perf_counter() - planner_started
        command = result.command
        command.setdefault("llm_planner", {})["elapsed_sec"] = round(planner_elapsed, 6)
        effective_llm_policy = "residual" if llm_policy == "auto" else llm_policy
        command.setdefault("llm_planner", {})["policy"] = effective_llm_policy
        if _is_residual_llm_policy(effective_llm_policy):
            if effective_llm_policy == "residual_escape":
                self._validate_llm_escape_hypotheses(command, schema_state, state, used_llm=result.used_llm)
            else:
                self._validate_llm_action_hypotheses(command, schema_state, state, used_llm=result.used_llm)
            has_validated_hypothesis = _command_has_validated_llm_hypothesis_selection(command)
            if not has_validated_hypothesis:
                args = command.setdefault("args", {})
                ignored_existing = [
                    item
                    for item in (args.get("selected_actions") if isinstance(args.get("selected_actions"), list) else [])
                    if isinstance(item, dict) and item.get("action_id")
                ]
                args["selected_actions"] = []
                if ignored_existing:
                    command.setdefault("llm_increment", {}).update(
                        {
                            "existing_action_selection_ignored": True,
                            "ignored_existing_action_ids": [
                                str(item.get("action_id")) for item in ignored_existing[:8]
                            ],
                            "reason": (
                                "Residual LLM mode treats existing action selections as audit-only; "
                                "only SPICE-validated custom hypotheses may override deterministic execution."
                            ),
                        }
                    )
                if self._should_prefer_postprocess_before_schema_edit(state):
                    command.setdefault("llm_increment", {}).update(
                        {
                            "schema_version": "analogrf_ir.llm_residual_audit.v0_1",
                            "status": "no_validated_llm_hypothesis_use_postprocess_rescue",
                            "used_llm": bool(result.used_llm),
                            "attempted_llm": True,
                            "planner_status": result.status,
                            "planner_reason": result.reason,
                            "fallback_source": "postprocess_rescue_fast_path",
                            "elapsed_sec": round(time.perf_counter() - planner_started, 6),
                        }
                    )
                    schema_state.to_yaml(design_state_path, include_runtime_context=False)
                    self.emit(
                        "       Residual LLM audit produced no validated custom hypothesis; "
                        "postprocess rescue remains preferred for this repair-sensitive failure"
                    )
                    self.emit(
                        f"       [timing] agent.llm_write_schema_command.residual_postprocess_rescue: "
                        f"{time.perf_counter() - node_started:.2f}s"
                    )
                    return {
                        "last_tool_command": command,
                        "llm_planner": command.get("llm_planner", {}),
                    }
                fallback_command = self._optimizer_evidence_fast_path(schema_state, int(state["round_index"]))
                if fallback_command is not None:
                    command["status"] = "audited"
                    fallback_command["llm_increment"] = {
                        "schema_version": "analogrf_ir.llm_residual_audit.v0_1",
                        "status": "no_validated_llm_hypothesis_use_optimizer_fast_path",
                        "used_llm": bool(result.used_llm),
                        "attempted_llm": True,
                        "planner_status": result.status,
                        "planner_reason": result.reason,
                        "fallback_source": "optimizer_evidence_fast_path",
                        "llm_command_id": command.get("id"),
                        "ignored_existing_action_ids": [
                            str(item.get("action_id")) for item in ignored_existing[:8]
                        ],
                        "elapsed_sec": round(time.perf_counter() - planner_started, 6),
                    }
                    fallback_command.setdefault("llm_planner", {}).update(
                        {
                            "provider": self.llm_config.provider,
                            "model": self.llm_config.model,
                            "policy": effective_llm_policy,
                            "status": result.status,
                            "reason": (
                                "Residual LLM audit produced no validated custom hypothesis; "
                                "falling back to optimizer-selected admissible action."
                            ),
                            "used_llm": bool(result.used_llm),
                            "elapsed_sec": round(planner_elapsed, 6),
                        }
                    )
                    schema_state.to_yaml(design_state_path, include_runtime_context=False)
                    selected = fallback_command.get("args", {}).get("selected_actions", []) or []
                    self.emit(
                        "       Residual LLM audit produced no validated custom hypothesis; "
                        f"using {len(selected)} optimizer fast-path action(s)"
                    )
                    self.emit(
                        f"       [timing] agent.llm_write_schema_command.residual_fallback: "
                        f"{time.perf_counter() - node_started:.2f}s"
                    )
                    return {
                        "last_tool_command": fallback_command,
                        "llm_planner": fallback_command.get("llm_planner", {}),
                    }
        schema_state.to_yaml(design_state_path, include_runtime_context=False)
        self.emit(
            "       Wrote schema command: "
            f"{command['id']} with {len(command['args'].get('available_actions', []))} available actions "
            f"and {len(command['args'].get('selected_actions', []))} selected actions"
        )
        self.emit(
            "       LLM planner: "
            f"{self.llm_config.provider}/{self.llm_config.model} status={result.status} "
            f"elapsed={planner_elapsed:.2f}s reason={result.reason}"
        )
        self.emit(f"       [timing] agent.llm_write_schema_command: {time.perf_counter() - node_started:.2f}s")
        return {
            "last_tool_command": command,
            "llm_planner": command.get("llm_planner", {}),
        }

    def _validate_llm_action_hypotheses(
        self,
        command: dict[str, Any],
        schema_state: DesignState,
        state: AgentGraphState,
        *,
        used_llm: bool,
    ) -> None:
        started = time.perf_counter()
        args = command.setdefault("args", {})
        custom_actions = args.get("custom_actions") if isinstance(args.get("custom_actions"), list) else []
        if not custom_actions:
            command["llm_increment"] = {
                "schema_version": "analogrf_ir.llm_action_hypothesis.v0_1",
                "status": "no_hypotheses",
                "used_llm": bool(used_llm),
                "selected_existing_count": len(
                    [
                        item
                        for item in (args.get("selected_actions") if isinstance(args.get("selected_actions"), list) else [])
                        if isinstance(item, dict) and str(item.get("decision", "apply")).lower() == "apply"
                    ]
                ),
                "reason": "LLM did not propose custom action hypotheses.",
                "elapsed_sec": round(time.perf_counter() - started, 6),
            }
            return
        final_result = self._final_result
        if final_result is None:
            command["llm_increment"] = {
                "schema_version": "analogrf_ir.llm_action_hypothesis.v0_1",
                "status": "validation_unavailable",
                "used_llm": bool(used_llm),
                "reason": "No previous simulator result is available for SPICE hypothesis validation.",
                "elapsed_sec": round(time.perf_counter() - started, 6),
            }
            args["selected_actions"] = []
            args["custom_actions"] = []
            return
        spec_model = SpecRegistry().select(schema_state)
        measurements = final_result.sim_result.measurements or {}
        estimates = (final_result.best_meta or {}).get("performance", {}) or {}
        target_status = {
            name: spec_model.target_status(name, target, measurements, estimates)
            for name, target in schema_state.targets.items()
        }
        target_status = _force_unverified_targets_from_status(schema_state, target_status)
        tuning = _tuning_from_llm_hypotheses(schema_state, custom_actions, target_status)
        hypothesis_actions = [
            action
            for failure in tuning.get("by_failure", []) or []
            for action in failure.get("actions", []) or []
        ]
        if not hypothesis_actions:
            command["llm_increment"] = {
                "schema_version": "analogrf_ir.llm_action_hypothesis.v0_1",
                "status": "no_valid_hypotheses",
                "used_llm": bool(used_llm),
                "reason": "LLM hypotheses did not reference writable schema knobs.",
                "hypothesis_count": len(custom_actions),
                "elapsed_sec": round(time.perf_counter() - started, 6),
            }
            args["selected_actions"] = []
            args["custom_actions"] = []
            return
        sim = NgspiceSimulator(ngspice_bin=self.config.ngspice_bin or "ngspice")
        transient_policy = str(self.config.intervention_transient_policy or "targeted")
        if "slew_rate" in target_status:
            # Residual LLM hypotheses often touch bias or geometry. When slew is
            # part of the target vector, AC/DC-only probes can falsely accept a
            # headroom fix that steals output current. Keep this stricter path
            # local to LLM residual validation so deterministic ablations retain
            # their existing runtime profile.
            transient_policy = "on"
        model = build_spice_intervention_model(
            state=schema_state,
            sim=sim,
            work_dir=Path(state["last_artifact_dir"]) / "llm_hypothesis_interventions",
            spec_model=spec_model,
            target_status=target_status,
            tuning=tuning,
            max_actions=min(len(hypothesis_actions), max(1, int(self.config.intervention_max_actions or 1))),
            perturbation_fraction=float(self.config.intervention_perturbation_fraction),
            transient_policy=transient_policy,
        )
        optimizer = optimize_tuning_actions(
            tuning=tuning,
            target_status=target_status,
            intervention_model=model,
            max_selected_actions=2,
            max_candidate_actions=max(1, len(hypothesis_actions)),
        )
        optimized_tuning = apply_optimized_action_plan(tuning, optimizer)
        causal = schema_state.diagnostics.setdefault("causal_diagnostics", {})
        original_tuning = causal.get("attribution_guided_tuning", {}) or {}
        causal["attribution_guided_tuning"] = _merge_hypothesis_tuning(original_tuning, optimized_tuning)
        causal["llm_action_hypothesis_validation"] = {
            "schema_version": "analogrf_ir.llm_action_hypothesis.v0_1",
            "status": optimizer.get("status"),
            "used_llm": bool(used_llm),
            "hypothesis_count": len(hypothesis_actions),
            "model_status": model.get("status"),
            "ok_probe_count": sum(1 for item in model.get("action_effects", []) or [] if item.get("status") == "ok"),
            "selected_action_ids": [
                item.get("action_id")
                for item in optimizer.get("selected_actions", []) or []
                if item.get("action_id")
            ],
            "objective_before": optimizer.get("objective_before"),
            "objective_after": optimizer.get("objective_after"),
            "candidate_actions": _compact_llm_hypothesis_candidates(optimizer, model),
            "elapsed_sec": round(time.perf_counter() - started, 6),
        }
        selected = _selected_actions_from_optimizer_result(optimizer)
        args["selected_actions"] = selected
        args["custom_actions"] = []
        command["llm_increment"] = {
            **causal["llm_action_hypothesis_validation"],
            "status": "applied" if selected else "all_rejected",
            "selected_count": len(selected),
            "rejected_count": max(0, len(hypothesis_actions) - len(selected)),
        }

    def _validate_llm_escape_hypotheses(
        self,
        command: dict[str, Any],
        schema_state: DesignState,
        state: AgentGraphState,
        *,
        used_llm: bool,
    ) -> None:
        started = time.perf_counter()
        args = command.setdefault("args", {})
        custom_actions = args.get("custom_actions") if isinstance(args.get("custom_actions"), list) else []
        if not custom_actions:
            command["llm_increment"] = {
                "schema_version": "analogrf_ir.llm_residual_escape.v0_1",
                "status": "no_hypotheses",
                "used_llm": bool(used_llm),
                "reason": "LLM did not propose exploratory custom action hypotheses.",
                "elapsed_sec": round(time.perf_counter() - started, 6),
            }
            return
        final_result = self._final_result
        if final_result is None:
            command["llm_increment"] = {
                "schema_version": "analogrf_ir.llm_residual_escape.v0_1",
                "status": "validation_unavailable",
                "used_llm": bool(used_llm),
                "reason": "No previous simulator result is available for measured escape validation.",
                "elapsed_sec": round(time.perf_counter() - started, 6),
            }
            args["selected_actions"] = []
            args["custom_actions"] = []
            return
        spec_model = SpecRegistry().select(schema_state)
        measurements = final_result.sim_result.measurements or {}
        estimates = (final_result.best_meta or {}).get("performance", {}) or {}
        target_status = {
            name: spec_model.target_status(name, target, measurements, estimates)
            for name, target in schema_state.targets.items()
        }
        target_status = _force_unverified_targets_from_status(schema_state, target_status)
        tuning = _tuning_from_llm_hypotheses(schema_state, custom_actions, target_status)
        hypothesis_actions = [
            action
            for failure in tuning.get("by_failure", []) or []
            for action in failure.get("actions", []) or []
        ]
        if not hypothesis_actions:
            command["llm_increment"] = {
                "schema_version": "analogrf_ir.llm_residual_escape.v0_1",
                "status": "no_valid_hypotheses",
                "used_llm": bool(used_llm),
                "reason": "LLM exploratory hypotheses did not reference writable schema knobs.",
                "hypothesis_count": len(custom_actions),
                "elapsed_sec": round(time.perf_counter() - started, 6),
            }
            args["selected_actions"] = []
            args["custom_actions"] = []
            return

        sim = NgspiceSimulator(ngspice_bin=self.config.ngspice_bin or "ngspice")
        transient_policy = str(self.config.intervention_transient_policy or "targeted")
        if "slew_rate" in target_status:
            transient_policy = "on"
        model = build_spice_intervention_model(
            state=schema_state,
            sim=sim,
            work_dir=Path(state["last_artifact_dir"]) / "llm_escape_interventions",
            spec_model=spec_model,
            target_status=target_status,
            tuning=tuning,
            max_actions=min(len(hypothesis_actions), max(1, int(self.config.intervention_max_actions or 1))),
            perturbation_fraction=float(self.config.intervention_perturbation_fraction),
            transient_policy=transient_policy,
        )
        action_by_id = {str(action.get("action_id")): action for action in hypothesis_actions if action.get("action_id")}
        accepted = _select_llm_escape_action(model, target_status, action_by_id)
        causal = schema_state.diagnostics.setdefault("causal_diagnostics", {})
        causal["llm_residual_escape_validation"] = {
            "schema_version": "analogrf_ir.llm_residual_escape.v0_1",
            "status": "accepted" if accepted else "all_rejected",
            "used_llm": bool(used_llm),
            "hypothesis_count": len(hypothesis_actions),
            "model_status": model.get("status"),
            "ok_probe_count": sum(1 for item in model.get("action_effects", []) or [] if item.get("status") == "ok"),
            "acceptance_rule": "accept iff measured violation_reduction > max(0.002, 1% of J_before); no optimizer pre-selection is required",
            "candidate_actions": _compact_llm_escape_candidates(model, target_status),
            "elapsed_sec": round(time.perf_counter() - started, 6),
        }
        args["custom_actions"] = []
        if not accepted:
            args["selected_actions"] = []
            command["llm_increment"] = {
                **causal["llm_residual_escape_validation"],
                "status": "all_rejected",
                "selected_count": 0,
                "rejected_count": len(hypothesis_actions),
                "reason": "No exploratory LLM hypothesis produced sufficient measured objective improvement.",
            }
            return

        selected_action = accepted["action"]
        original_tuning = causal.get("attribution_guided_tuning", {}) or {}
        escape_tuning = {
            "strategy": "llm_residual_escape_validation",
            "planning_mode": "escape",
            "by_failure": [
                {
                    "metric": selected_action.get("metric"),
                    "status": target_status.get(str(selected_action.get("metric")), {}).get("status", "unknown"),
                    "strategy": "measured_spice_escape_probe",
                    "actions": [selected_action],
                }
            ],
        }
        causal["attribution_guided_tuning"] = _merge_hypothesis_tuning(original_tuning, escape_tuning)
        args["selected_actions"] = [
            {
                "action_id": selected_action["action_id"],
                "decision": "apply",
                "reason": selected_action.get(
                    "selection_reason",
                    "LLM exploratory patch reduced the measured violation objective in SPICE.",
                ),
                "overrides": {},
            }
        ]
        command["llm_increment"] = {
            **causal["llm_residual_escape_validation"],
            "status": "applied_escape",
            "selected_count": 1,
            "rejected_count": max(0, len(hypothesis_actions) - 1),
            "selected_action_ids": [selected_action["action_id"]],
            "objective_before": accepted["objective_before"],
            "objective_after": accepted["objective_after"],
            "objective_delta": accepted["objective_delta"],
            "acceptance_threshold": accepted["acceptance_threshold"],
        }

    def _attach_llm_shadow(
        self,
        command: dict[str, Any],
        schema_state: DesignState,
        state: AgentGraphState,
        *,
        source: str,
    ) -> None:
        shadow_started = time.perf_counter()
        planner = DeepSeekSchemaPlanner(self.llm_config)
        try:
            result = planner.write_command(
                deepcopy(schema_state),
                round_index=int(state["round_index"]),
                agent_model=state.get("agent_model", {}),
            )
        except Exception as exc:  # pragma: no cover - shadow audit must not alter execution.
            elapsed = time.perf_counter() - shadow_started
            command["llm_shadow"] = {
                "schema_version": "analogrf_ir.llm_shadow_audit.v0_1",
                "source": source,
                "used_llm": False,
                "attempted_llm": True,
                "planner_status": "error",
                "planner_reason": str(exc),
                "elapsed_sec": round(elapsed, 6),
            }
            self.emit(f"       LLM shadow audit failed after {elapsed:.2f}s: {exc}")
            return
        elapsed = time.perf_counter() - shadow_started
        result.command.setdefault("llm_planner", {})["elapsed_sec"] = round(elapsed, 6)
        command["llm_shadow"] = _llm_shadow_audit_record(
            result.command,
            source=source,
            used_llm=result.used_llm,
            status=result.status,
            reason=result.reason,
            elapsed_sec=elapsed,
        )
        self.emit(
            "       LLM shadow audit: "
            f"{self.llm_config.provider}/{self.llm_config.model} status={result.status} "
            f"used_llm={result.used_llm} elapsed={elapsed:.2f}s"
        )

    def _optimizer_evidence_fast_path(self, schema_state: DesignState, round_index: int) -> dict[str, Any] | None:
        causal = schema_state.diagnostics.get("causal_diagnostics", {}) if schema_state.diagnostics else {}
        optimizer = causal.get("constrained_action_optimizer", {}) or {}
        status = str(optimizer.get("status") or "")
        if status not in {"ok", "no_improving_combination"}:
            return None
        selected = default_selected_actions_from_optimizer(schema_state, allowed_priorities=["primary"])
        if not selected:
            return None
        command = write_tuning_tool_command(
            schema_state,
            round_index=round_index,
            author="optimizer_evidence_fast_path",
            max_primary_actions_per_failure=3,
            allowed_priorities=["primary"],
            selected_actions=selected,
        )
        command["llm_planner"] = {
            "provider": self.llm_config.provider,
            "model": self.llm_config.model,
            "thinking": self.llm_config.thinking,
            "reasoning_effort": self.llm_config.reasoning_effort,
            "temperature": self.llm_config.temperature,
            "max_tokens": self.llm_config.max_tokens,
            "status": "skipped",
            "reason": (
                "Constrained optimizer selected admissible actions; LLM request was unnecessary for this round."
                if status == "ok"
                else "Formal gate found admissible negative-objective candidates despite no improving penalized combination."
            ),
        }
        command["llm_notes"] = "Evidence fast path: applied formal-admissible optimizer candidates without an LLM request."
        command["llm_rationale"] = "The formal action gate already had admissible optimizer evidence."
        return command

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
        stagnated = self._stagnation_detected(state.get("rounds", [])) and not bool(
            state.get("postprocess_rescue_attempted", False)
        )
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
        latest = rounds[-1]
        fast_profile = str(getattr(self.config, "runtime_profile", "standard") or "standard") == "ablation_fast"
        if fast_profile and not self._repair_sensitive_failure(latest, max_score=1.5):
            return False
        postprocess_count = int(latest.get("postprocess_event_count", 0) or 0)
        if postprocess_count == 0:
            return True
        return self._repair_sensitive_failure(latest, max_score=0.03)

    def _should_prefer_postprocess_before_schema_edit(self, state: AgentGraphState) -> bool:
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
        latest = rounds[-1]
        postprocess_count = int(latest.get("postprocess_event_count", 0) or 0)
        try:
            measured_score = float(latest.get("measured_violation_score"))
        except (TypeError, ValueError):
            return False
        fast_profile = str(getattr(self.config, "runtime_profile", "standard") or "standard") == "ablation_fast"
        max_score = 1.5 if fast_profile and postprocess_count == 0 else 0.03
        if not (0.0 < measured_score <= max_score):
            return False
        return self._repair_sensitive_failure(latest, max_score=max_score)

    @staticmethod
    def _repair_sensitive_failure(summary: dict[str, Any], *, max_score: float) -> bool:
        try:
            measured_score = float(summary.get("measured_violation_score"))
        except (TypeError, ValueError):
            return False
        if not (0.0 < measured_score <= max_score):
            return False
        failed = set(summary.get("failed_targets", []) or [])
        if not failed:
            return False
        repair_sensitive = {
            "dc_gain",
            "unity_gain_bandwidth",
            "phase_margin",
            "slew_rate",
            "slew_rate_pos",
            "slew_rate_neg",
            "output_swing",
            "saturation_margin",
        }
        return bool(failed) and failed.issubset(repair_sensitive)

    def _llm_policy(self) -> str:
        config = getattr(self, "config", None)
        policy = str(getattr(config, "llm_policy", "auto") or "auto").strip().lower()
        if policy not in {"auto", "residual", "residual_escape", "shadow"}:
            return "auto"
        return policy

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
            "unverified_targets": list(status.get("unverified_targets", [])),
            "best_loss": status.get("best_loss"),
            "measured_violation_score": _measured_violation_score(result.state, result.sim_result.measurements or {}),
            "top_actions": [
                action
                for failure in failures
                for action in failure.get("actions", [])[:3]
            ],
            "postprocess_decision": result.flow_meta.get("postprocess_decision", {}),
            "postprocess_event_count": len(result.flow_meta.get("postprocess", []) or []),
            "flow_timing": result.flow_meta.get("timing", {}),
        }

    @staticmethod
    def _is_better_summary(candidate: dict[str, Any], incumbent: dict[str, Any] | None) -> bool:
        if incumbent is None:
            return True
        return _summary_rank(candidate) < _summary_rank(incumbent)

    def _runtime_stagnation_detected(self, rounds: list[dict[str, Any]]) -> bool:
        fast_profile = str(getattr(self.config, "runtime_profile", "standard") or "standard") == "ablation_fast"
        min_rounds = 3 if fast_profile else 6
        window = 2 if fast_profile else 4
        tolerance = 0.03 if fast_profile else 0.02
        if len(rounds) < min_rounds:
            return False
        current_failed = set(rounds[-1].get("failed_targets", []) or [])
        if not current_failed:
            return False
        recent = rounds[-window:]
        if any(set(item.get("failed_targets", []) or []) != current_failed for item in recent):
            return False
        scores = []
        for item in rounds:
            try:
                score = float(item.get("measured_violation_score"))
            except (TypeError, ValueError):
                return False
            if not (score >= 0.0):
                return False
            scores.append(score)
        older_best = min(scores[:-window])
        recent_best = min(scores[-window:])
        if older_best <= 1e-12:
            return False
        return (older_best - recent_best) <= max(1e-6, tolerance * older_best)

    def _print_round_summary(self, summary: dict[str, Any]) -> None:
        self.emit("       Round summary:")
        self.emit(f"         artifacts: {summary['artifact_dir']}")
        self.emit(f"         spec_pass: {summary['spec_pass']}")
        self.emit(f"         failed_targets: {summary['failed_targets']}")
        self.emit(f"         unverified_targets: {summary.get('unverified_targets', [])}")
        self.emit(f"         measured_violation_score: {summary.get('measured_violation_score')}")
        if summary.get("round_elapsed_sec") is not None:
            self.emit(f"         round_elapsed: {float(summary.get('round_elapsed_sec') or 0.0):.2f}s")
        slowest = ((summary.get("flow_timing", {}) or {}).get("slowest_stage", {}) or {})
        if slowest:
            self.emit(
                "         slowest_flow_stage: "
                f"{slowest.get('stage')}={float(slowest.get('elapsed_sec', 0.0) or 0.0):.2f}s"
            )
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
        self.emit(f"       unverified_targets: {final_round.get('unverified_targets', state.get('last_unverified_targets', []))}")
        self.emit(f"       best_loss: {final_round.get('best_loss')}")


def _summary_rank(summary: dict[str, Any]) -> tuple[float, float, float]:
    try:
        best_loss = float(summary.get("measured_violation_score"))
    except (TypeError, ValueError):
        try:
            best_loss = float(summary.get("best_loss"))
        except (TypeError, ValueError):
            best_loss = float("inf")
    failed_count = len(summary.get("failed_targets", []) or [])
    unverified_count = len(summary.get("unverified_targets", []) or [])
    return (
        0.0 if summary.get("spec_pass", False) else 1.0,
        float(unverified_count),
        best_loss,
        float(failed_count),
    )


def _command_has_executable_selection(command: Any) -> bool:
    if not isinstance(command, dict):
        return False
    args = command.get("args", {}) if isinstance(command.get("args"), dict) else {}
    selected = args.get("selected_actions") if isinstance(args.get("selected_actions"), list) else []
    return any(
        isinstance(item, dict) and str(item.get("decision", "apply")).strip().lower() == "apply"
        for item in selected
    )


def _is_residual_llm_policy(policy: str) -> bool:
    return str(policy or "").strip().lower() in {"residual", "residual_escape"}


def _command_has_validated_llm_hypothesis_selection(command: Any) -> bool:
    if not isinstance(command, dict):
        return False
    args = command.get("args", {}) if isinstance(command.get("args"), dict) else {}
    selected = args.get("selected_actions") if isinstance(args.get("selected_actions"), list) else []
    for item in selected:
        if not isinstance(item, dict):
            continue
        if str(item.get("decision", "apply")).strip().lower() != "apply":
            continue
        if str(item.get("action_id", "")).startswith("llm_hypothesis_"):
            return True
    return False


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


def _llm_shadow_audit_record(
    command: dict[str, Any],
    *,
    source: str,
    used_llm: bool,
    status: str,
    reason: str,
    elapsed_sec: float,
) -> dict[str, Any]:
    args = command.get("args", {}) or {}
    planner = command.get("llm_planner", {}) or {}
    selected = args.get("selected_actions", []) or []
    custom = args.get("custom_actions", []) or []
    available = args.get("available_actions", []) or []
    return {
        "schema_version": "analogrf_ir.llm_shadow_audit.v0_1",
        "source": source,
        "used_llm": bool(used_llm),
        "attempted_llm": planner.get("status") != "fallback" or bool(used_llm),
        "planner_status": planner.get("status", status),
        "planner_reason": planner.get("reason", reason),
        "elapsed_sec": round(elapsed_sec, 6),
        "available_action_count": len(available),
        "shadow_selected_action_ids": [
            str(item.get("action_id"))
            for item in selected
            if isinstance(item, dict) and item.get("action_id")
        ],
        "shadow_custom_action_ids": [
            str(item.get("action_id"))
            for item in custom
            if isinstance(item, dict) and item.get("action_id")
        ],
    }


def _compact_llm_hypothesis_candidates(
    optimizer: dict[str, Any],
    intervention_model: dict[str, Any],
) -> list[dict[str, Any]]:
    effects = {
        str(effect.get("action_id")): effect
        for effect in (intervention_model.get("action_effects", []) or [])
        if isinstance(effect, dict) and effect.get("action_id")
    }
    out: list[dict[str, Any]] = []
    for item in (optimizer.get("candidate_actions", []) or [])[:8]:
        if not isinstance(item, dict):
            continue
        action_id = str(item.get("action_id") or "")
        effect = effects.get(action_id, {})
        admissibility = item.get("action_admissibility", {}) if isinstance(item.get("action_admissibility"), dict) else {}
        gate = item.get("evidence_gate", {}) if isinstance(item.get("evidence_gate"), dict) else {}
        record = {
            "action_id": action_id,
            "selected": bool(item.get("optimizer_selected")),
            "metric": item.get("metric"),
            "knob": item.get("knob"),
            "apply_to": item.get("apply_to", []),
            "direction": item.get("direction"),
            "per_knob_values": item.get("per_knob_values", {}),
            "local_model_source": item.get("local_model_source"),
            "objective_delta": item.get("objective_delta"),
            "predicted_violation_delta": item.get("predicted_violation_delta", {}),
            "violation_reduction": effect.get("violation_reduction"),
            "after_violation_vector": effect.get("after_violation_vector", {}),
            "measurements": {
                key: effect.get("measurements", {}).get(key)
                for key in (
                    "dc_gain_db",
                    "unity_gain_bandwidth",
                    "phase_margin",
                    "slew_rate",
                    "output_swing",
                    "saturation_margin",
                    "total_power",
                )
                if isinstance(effect.get("measurements"), dict) and key in effect.get("measurements", {})
            },
            "admissibility_passed": admissibility.get("passed"),
            "admissibility_reasons": admissibility.get("reasons", []),
            "evidence_gate_passed": gate.get("passed"),
            "evidence_gate_reasons": gate.get("reasons", []),
        }
        out.append({key: value for key, value in record.items() if value not in (None, "", [], {})})
    return out


def _select_llm_escape_action(
    intervention_model: dict[str, Any],
    target_status: dict[str, dict[str, Any]],
    action_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for effect in intervention_model.get("action_effects", []) or []:
        if not isinstance(effect, dict) or effect.get("status") != "ok":
            continue
        action_id = str(effect.get("action_id") or "")
        base_violation = {
            str(metric): float(value)
            for metric, value in (effect.get("base_violation_vector") or intervention_model.get("base_violation_vector") or {}).items()
        }
        after_violation = {
            str(metric): float(value)
            for metric, value in (effect.get("after_violation_vector") or {}).items()
        }
        if not after_violation:
            delta = {str(metric): float(value) for metric, value in (effect.get("delta_violation_vector") or {}).items()}
            after_violation = {
                metric: max(0.0, float(base_violation.get(metric, 0.0)) + float(delta.get(metric, 0.0)))
                for metric in base_violation
            }
        objective_before = _weighted_violation_objective(base_violation, target_status)
        objective_after = _weighted_violation_objective(after_violation, target_status)
        reduction = objective_before - objective_after
        threshold = max(0.002, 0.01 * max(objective_before, 0.0))
        if reduction <= threshold:
            continue
        action = action_by_id.get(action_id)
        if action is None:
            continue
        accepted_action = _annotated_llm_escape_action(
            action,
            effect=effect,
            objective_before=objective_before,
            objective_after=objective_after,
            threshold=threshold,
        )
        candidates.append(
            {
                "action": accepted_action,
                "objective_before": round(float(objective_before), 6),
                "objective_after": round(float(objective_after), 6),
                "objective_delta": round(float(objective_after - objective_before), 6),
                "acceptance_threshold": round(float(threshold), 6),
                "violation_reduction": round(float(reduction), 6),
                "uncertainty": float(effect.get("uncertainty", 0.25) or 0.25),
            }
        )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            float(item.get("violation_reduction", 0.0) or 0.0),
            -float(item.get("uncertainty", 0.25) or 0.25),
        ),
    )


def _annotated_llm_escape_action(
    action: dict[str, Any],
    *,
    effect: dict[str, Any],
    objective_before: float,
    objective_after: float,
    threshold: float,
) -> dict[str, Any]:
    out = dict(action)
    objective_delta = float(objective_after) - float(objective_before)
    out["priority"] = "primary"
    out["action_class"] = "llm_exploratory_schema_patch"
    out["local_model_source"] = "spice_small_perturbation"
    out["objective_delta"] = round(float(objective_delta), 6)
    out["optimizer_selected"] = False
    out["selection_reason"] = (
        "Selected by residual_escape because the LLM exploratory patch reduced "
        "the measured weighted violation objective in SPICE."
    )
    out["evidence_gate"] = {
        "schema_version": "analogrf_ir.llm_residual_escape_gate.v0_1",
        "required": False,
        "passed": True,
        "source": "spice_small_perturbation",
        "objective_before": round(float(objective_before), 6),
        "objective_after": round(float(objective_after), 6),
        "objective_delta": round(float(objective_delta), 6),
        "acceptance_threshold": round(float(threshold), 6),
        "decision_rule": "Exploratory LLM patches bypass optimizer pre-selection but must reduce the measured SPICE violation objective.",
    }
    out["llm_escape_validation"] = {
        "schema_version": "analogrf_ir.llm_residual_escape.v0_1",
        "status": "accepted",
        "effect_status": effect.get("status"),
        "measurements": dict(effect.get("measurements") or {}),
        "base_violation_vector": dict(effect.get("base_violation_vector") or {}),
        "after_violation_vector": dict(effect.get("after_violation_vector") or {}),
        "delta_violation_vector": dict(effect.get("delta_violation_vector") or {}),
        "violation_reduction": effect.get("violation_reduction"),
        "uncertainty": effect.get("uncertainty"),
    }
    return out


def _compact_llm_escape_candidates(
    intervention_model: dict[str, Any],
    target_status: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for effect in (intervention_model.get("action_effects", []) or [])[:8]:
        if not isinstance(effect, dict):
            continue
        base_violation = {
            str(metric): float(value)
            for metric, value in (effect.get("base_violation_vector") or intervention_model.get("base_violation_vector") or {}).items()
        }
        after_violation = {
            str(metric): float(value)
            for metric, value in (effect.get("after_violation_vector") or {}).items()
        }
        objective_before = _weighted_violation_objective(base_violation, target_status) if base_violation else None
        objective_after = _weighted_violation_objective(after_violation, target_status) if after_violation else None
        threshold = max(0.002, 0.01 * max(objective_before or 0.0, 0.0))
        record = {
            "action_id": effect.get("action_id"),
            "status": effect.get("status"),
            "metric": effect.get("metric"),
            "knob": effect.get("knob"),
            "apply_to": effect.get("apply_to", []),
            "direction": effect.get("direction"),
            "per_knob_values": effect.get("per_knob_values", {}),
            "violation_reduction": effect.get("violation_reduction"),
            "objective_before": round(float(objective_before), 6) if objective_before is not None else None,
            "objective_after": round(float(objective_after), 6) if objective_after is not None else None,
            "acceptance_threshold": round(float(threshold), 6),
            "accepted": (
                effect.get("status") == "ok"
                and objective_before is not None
                and objective_after is not None
                and (float(objective_before) - float(objective_after)) > threshold
            ),
            "uncertainty": effect.get("uncertainty"),
            "elapsed_sec": effect.get("elapsed_sec"),
            "sim_elapsed_sec": effect.get("sim_elapsed_sec"),
        }
        out.append({key: value for key, value in record.items() if value not in (None, "", [], {})})
    return out


def _weighted_violation_objective(
    violation: dict[str, float],
    target_status: dict[str, dict[str, Any]],
) -> float:
    total = 0.0
    for metric, value in violation.items():
        status = target_status.get(metric, {}) or {}
        if "counts_for_pass" in status and not bool(status.get("counts_for_pass")):
            continue
        try:
            priority = int(status.get("priority", 1) or 1)
        except (TypeError, ValueError):
            priority = 1
        weight = max(0.25, 1.0 / max(priority, 1))
        total += weight * float(value) ** 2
    return float(total)


def _tuning_from_llm_hypotheses(
    state: DesignState,
    custom_actions: list[dict[str, Any]],
    target_status: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    writable = {_knob_name(dv.device, dv.variable): dv for dv in state.design_variables}
    failed_metrics = [
        name
        for name, status in target_status.items()
        if status.get("status") in {"fail", "unverified"} and status.get("counts_for_pass", True)
    ]
    default_metric = failed_metrics[0] if failed_metrics else next(iter(target_status), "llm_hypothesis")
    by_metric: dict[str, list[dict[str, Any]]] = {}
    for index, custom in enumerate(custom_actions, start=1):
        if not isinstance(custom, dict):
            continue
        if str(custom.get("decision", "apply")).lower() != "apply":
            continue
        knobs = _hypothesis_apply_to(custom, writable)
        if not knobs:
            continue
        metric = str(custom.get("metric") or default_metric)
        if metric not in target_status:
            metric = default_metric
        direction = _normalize_hypothesis_direction(custom.get("direction"))
        per_knob_values = _hypothesis_per_knob_values(state, custom, knobs, writable, direction)
        if not per_knob_values:
            continue
        knobs = [knob for knob in knobs if knob in per_knob_values]
        if not knobs:
            continue
        range_update = custom.get("range_update") if isinstance(custom.get("range_update"), dict) else None
        range_update = _filter_hypothesis_range_update(range_update, knobs)
        action_id = str(custom.get("action_id") or f"llm_hypothesis_{index:03d}_{_safe_id(knobs[0])}")
        action = {
            "action_id": f"llm_hypothesis_{index:03d}_{_safe_id(action_id)}",
            "metric": metric,
            "rank": len(by_metric.get(metric, [])) + 1,
            "priority": "guarded",
            "action_class": "llm_action_hypothesis",
            "knob": knobs[0],
            "apply_to": knobs,
            "direction": direction,
            "per_knob_values": per_knob_values,
            "range_update": range_update,
            "expected_effect": custom.get("expected_effect") if isinstance(custom.get("expected_effect"), dict) else {metric: "improve"},
            "score": 0.50,
            "rationale": custom.get("reason", custom.get("rationale", "LLM proposed a typed action hypothesis.")),
            "llm_hypothesis": {
                "source_action_id": custom.get("action_id"),
                "reason": custom.get("reason", ""),
            },
        }
        by_metric.setdefault(metric, []).append(action)
    return {
        "strategy": "llm_action_hypothesis_validation",
        "planning_mode": "fine",
        "by_failure": [
            {
                "metric": metric,
                "status": target_status.get(metric, {}).get("status", "unknown"),
                "strategy": "llm_hypothesis_spice_probe",
                "actions": actions,
            }
            for metric, actions in by_metric.items()
        ],
    }


def _force_unverified_targets_from_status(
    state: DesignState,
    target_status: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    status = state.diagnostics.get("result", {}).get("status", {}) if state.diagnostics else {}
    unverified = {
        str(name)
        for name in status.get("unverified_targets", []) or []
        if str(name) in state.targets
    }
    if not unverified:
        return target_status
    out = {name: dict(item) for name, item in target_status.items()}
    for name in unverified:
        target = state.targets[name]
        item = dict(out.get(name, {}))
        estimated_value = item.get("value")
        item.update(
            {
                "status": "unverified",
                "model_status": item.get("model_status", "unknown"),
                "source": item.get("source", "missing"),
                "requires_ngspice": True,
                "counts_for_pass": bool(item.get("counts_for_pass", int(target.priority or 1) <= 2 or name == "saturation_margin")),
                "measurement_key": item.get("measurement_key", name),
                "value": None,
                "optimizer_value": estimated_value,
                "min": item.get("min", target.min),
                "max": item.get("max", target.max),
                "unit": item.get("unit", target.unit),
                "priority": item.get("priority", target.priority),
                "margin_abs": None,
                "margin_rel": None,
                "forced_unverified": True,
            }
        )
        out[name] = item
    return out


def _merge_hypothesis_tuning(original: dict[str, Any], hypothesis_tuning: dict[str, Any]) -> dict[str, Any]:
    merged = dict(original or {})
    merged["by_failure"] = list((original or {}).get("by_failure", []) or []) + list(hypothesis_tuning.get("by_failure", []) or [])
    merged["llm_hypothesis_strategy"] = hypothesis_tuning.get("strategy")
    return merged


def _selected_actions_from_optimizer_result(optimizer: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for item in optimizer.get("selected_actions", []) or []:
        action_id = item.get("action_id")
        if not action_id:
            continue
        out.append(
            {
                "action_id": action_id,
                "decision": "apply",
                "reason": item.get("selection_reason", "LLM hypothesis passed local SPICE validation and formal admissibility."),
                "overrides": {},
            }
        )
    return out


def _hypothesis_apply_to(custom: dict[str, Any], writable: dict[str, Any]) -> list[str]:
    raw = custom.get("apply_to")
    if isinstance(raw, list) and raw:
        knobs = [str(item) for item in raw if isinstance(item, str) and str(item) in writable]
        if knobs:
            return _dedupe_knobs(knobs)
    knob = custom.get("knob")
    if not isinstance(knob, str) or knob not in writable:
        return []
    dv = writable[knob]
    label = getattr(dv, "symmetry_label", None)
    if not label:
        return [knob]
    symmetric = [
        name
        for name, other in writable.items()
        if getattr(other, "variable", None) == getattr(dv, "variable", None)
        and getattr(other, "symmetry_label", None) == label
    ]
    return _dedupe_knobs(symmetric or [knob])


def _filter_hypothesis_range_update(range_update: dict[str, Any] | None, knobs: list[str]) -> dict[str, Any] | None:
    if not isinstance(range_update, dict):
        return None
    out = dict(range_update)
    per_knob = out.get("per_knob")
    if isinstance(per_knob, dict):
        knob_set = set(knobs)
        out["per_knob"] = {knob: value for knob, value in per_knob.items() if knob in knob_set}
    return out


def _hypothesis_per_knob_values(
    state: DesignState,
    custom: dict[str, Any],
    knobs: list[str],
    writable: dict[str, Any],
    direction: str,
) -> dict[str, float]:
    raw_values = custom.get("per_knob_values")
    if isinstance(raw_values, dict):
        values = {}
        for knob in knobs:
            if knob not in raw_values:
                continue
            value = _optional_number(raw_values.get(knob))
            if value is not None:
                values[knob] = _clip_hypothesis_to_trust_region(value, writable[knob], _current_knob_value(state, writable[knob]))
        if values:
            return values
    explicit = _optional_number(
        custom.get("suggested_unclipped_value", custom.get("suggested_next_value", custom.get("value")))
    )
    values = {}
    for knob in knobs:
        dv = writable[knob]
        current = _current_knob_value(state, dv)
        value = explicit if explicit is not None else _step_from_design_variable(dv, direction, current)
        if value is None:
            continue
        values[knob] = _clip_hypothesis_to_trust_region(value, dv, current)
    return values


def _current_knob_value(state: DesignState, dv: Any) -> float | None:
    device = str(getattr(dv, "device", "") or "")
    variable = str(getattr(dv, "variable", "") or "")
    if not device and variable in (state.global_parameters or {}):
        return _optional_number((state.global_parameters or {}).get(variable))
    return _optional_number(getattr(dv, "initial", None))


def _step_from_design_variable(dv: Any, direction: str, current_value: float | None = None) -> float | None:
    current = current_value if current_value is not None else _optional_number(getattr(dv, "initial", None))
    range_ = getattr(dv, "range", None)
    if current is None:
        if range_ is None:
            return None
        return 0.5 * (float(range_.min) + float(range_.max))
    if direction == "increase":
        return current * 1.10
    if direction == "decrease":
        return current * 0.90
    return current


def _clip_to_range(value: float, dv: Any) -> float:
    range_ = getattr(dv, "range", None)
    if range_ is None:
        return float(value)
    return min(max(float(value), float(range_.min)), float(range_.max))


def _clip_hypothesis_to_trust_region(value: float, dv: Any, current_value: float | None = None) -> float:
    clipped = _clip_to_range(value, dv)
    current = current_value if current_value is not None else _optional_number(getattr(dv, "initial", None))
    range_ = getattr(dv, "range", None)
    if current is None or range_ is None:
        return clipped
    variable = str(getattr(dv, "variable", "") or "").lower()
    is_global = not str(getattr(dv, "device", "") or "")
    span = max(abs(float(range_.max) - float(range_.min)), 1e-30)
    if is_global and (variable.startswith("v") or "bias" in variable):
        max_delta = min(0.03, 0.10 * span)
    elif is_global and variable.startswith("i"):
        max_delta = max(0.25 * abs(current), 0.05 * span)
    else:
        max_delta = 0.25 * span
    trusted = min(max(clipped, current - max_delta), current + max_delta)
    return _clip_to_range(trusted, dv)


def _normalize_hypothesis_direction(value: Any) -> str:
    text = str(value or "set").strip().lower()
    if text in {"increase", "up", "raise"}:
        return "increase"
    if text in {"decrease", "down", "lower", "reduce"}:
        return "decrease"
    return "set"


def _knob_name(device: str, variable: str) -> str:
    return f"{device}.{variable}" if device else f"global.{variable}"


def _dedupe_knobs(knobs: list[str]) -> list[str]:
    out = []
    seen = set()
    for knob in knobs:
        if knob in seen:
            continue
        seen.add(knob)
        out.append(knob)
    return out


def _optional_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_id(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in str(value))[:80]


def _measured_violation_score(state: DesignState, measurements: dict[str, Any]) -> float:
    score = 0.0
    for name, target in state.targets.items():
        if int(target.priority or 1) > 2 and name != "saturation_margin":
            continue
        metric = _MEASUREMENT_BY_TARGET.get(name)
        if not metric:
            continue
        raw = measurements.get(metric)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            score += 1.0
            continue
        if target.min is not None:
            denom = max(abs(float(target.min)), 1e-30)
            violation = max(0.0, (float(target.min) - value) / denom)
            score += violation * violation
        if target.max is not None:
            denom = max(abs(float(target.max)), 1e-30)
            violation = max(0.0, (value - float(target.max)) / denom)
            score += violation * violation
    return round(score, 9)

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from flow.config import load_cli_config
from flow.agent_loop import DiagnosticAgentLoop
from flow.llm_planner import LLMPlannerConfig
from flow.runner import AnalogRFIRFlowRunner, FlowConfig
from topologies.legacy import build_design_state


DEFAULT_AGENT_MAX_ITERATIONS = 20
DEFAULT_CLI_CONFIG = "configs/default.yaml"


def _parse_args(argv=None):
    explicit_args = list(sys.argv[1:] if argv is None else argv)
    defaults = _config_defaults(argv)

    def default(name: str, fallback):
        return defaults.get(name, fallback)

    parser = argparse.ArgumentParser(description="AnalogRF-IR v0.1 modular analog/RF optimizer")
    parser.add_argument("--config", default=defaults.get("config", DEFAULT_CLI_CONFIG), help="Editable run config YAML path")
    parser.add_argument("--env", default=default("env", "environment.yaml"), help="Environment YAML path")
    parser.add_argument("--schema", default=default("schema", "inputs/ota/five_transistor/five_transistor_ota.yaml"), help="Schema YAML path")
    parser.add_argument("--spice", default=default("spice", ""), help="Optional SPICE netlist to import before optimization")
    parser.add_argument("--spice-yaml-out", default=default("spice_yaml_out", ""), help="YAML path generated from --spice")
    parser.add_argument("--topology", choices=("auto", "five", "two_stage", "yaml"), default=default("topology", "auto"))
    parser.add_argument("--generations", type=int, default=default("generations", 50), help="NSGA-II generations")
    parser.add_argument("--pop-size", type=int, default=default("pop_size", 100), help="NSGA-II population size")
    parser.add_argument("--seed", type=int, default=default("seed", None), help="Random seed")
    parser.add_argument(
        "--agent-rounds",
        type=int,
        default=default("agent_rounds", DEFAULT_AGENT_MAX_ITERATIONS),
        help="Maximum diagnosis-guided schema tuning iterations; stops early when all specs pass",
    )
    parser.add_argument(
        "--llm-policy",
        choices=("auto", "residual", "residual_escape", "shadow"),
        default=default("llm_policy", "auto"),
        help=(
            "LLM use policy for diagnosis rounds. auto preserves the legacy flow; "
            "residual calls the LLM only when deterministic evidence paths have no executable command; "
            "residual_escape lets LLM custom actions act as SPICE-validated exploratory patches; "
            "shadow records an LLM audit while executing the deterministic command."
        ),
    )
    parser.add_argument("--llm-provider", default=default("llm_provider", "deepseek"), help="LLM planner provider for LangGraph rounds")
    parser.add_argument("--llm-model", default=default("llm_model", ""), help="LLM planner model, defaults to deepseek-v4-flash")
    parser.add_argument("--llm-base-url", default=default("llm_base_url", ""), help="OpenAI-compatible LLM base URL")
    parser.add_argument("--llm-api-key-env", default=default("llm_api_key_env", "DEEPSEEK_API_KEY"), help="Environment variable containing the LLM API key")
    parser.add_argument("--llm-timeout", type=float, default=default("llm_timeout", None), help="LLM planner request timeout in seconds")
    parser.add_argument("--llm-temperature", type=float, default=default("llm_temperature", None), help="LLM planner sampling temperature")
    parser.add_argument("--llm-max-tokens", type=int, default=default("llm_max_tokens", None), help="LLM planner max output tokens")
    parser.add_argument(
        "--llm-thinking",
        choices=("disabled", "enabled"),
        default=default("llm_thinking", ""),
        help="LLM planner thinking mode for providers that support it",
    )
    parser.add_argument(
        "--llm-reasoning-effort",
        default=default("llm_reasoning_effort", ""),
        help="LLM planner reasoning effort, for example max",
    )
    api_key_group = parser.add_mutually_exclusive_group()
    api_key_group.add_argument(
        "--llm-api-key",
        default=default("llm_api_key", ""),
        help="LLM API key value. Prefer --llm-api-key-file or --llm-api-key-env for regular use.",
    )
    api_key_group.add_argument(
        "--llm-api-key-file",
        default=default("llm_api_key_file", ""),
        help="Path to a file containing the LLM API key.",
    )
    api_key_group.add_argument(
        "--llm-api-key-stdin",
        action="store_true",
        default=bool(default("llm_api_key_stdin", False)),
        help="Read the LLM API key from standard input.",
    )
    parser.add_argument("--ngspice-bin", default=default("ngspice_bin", ""), help="Override ngspice executable path")
    parser.add_argument("--runs-dir", default=default("runs_dir", "runs"), help="Directory for run artifacts")
    parser.add_argument(
        "--tail-current-mirror",
        action="store_true",
        default=bool(default("tail_current_mirror", False)),
        help="Enable five-transistor OTA tail mirror bias generation",
    )
    parser.add_argument("--no-tail-current-mirror", dest="tail_current_mirror", action="store_false", help=argparse.SUPPRESS)
    parser.add_argument(
        "--skip-dc-repair",
        action="store_true",
        default=bool(default("skip_dc_repair", False)),
        help="Skip ngspice-driven DC balance/headroom repair before final verification",
    )
    parser.add_argument("--no-skip-dc-repair", dest="skip_dc_repair", action="store_false", help=argparse.SUPPRESS)
    parser.add_argument(
        "--skip-comp-tune",
        action="store_true",
        default=bool(default("skip_comp_tune", False)),
        help="Skip ngspice-driven Cc/Rz compensation sweep before final verification",
    )
    parser.add_argument("--no-skip-comp-tune", dest="skip_comp_tune", action="store_false", help=argparse.SUPPRESS)
    parser.add_argument(
        "--postprocess-policy",
        choices=("fallback", "always", "off"),
        default=default("postprocess_policy", "fallback"),
        help="Postprocess scheduling policy. fallback runs only near feasibility or stagnation.",
    )
    parser.add_argument(
        "--postprocess-near-feasible-ratio",
        type=float,
        default=default("postprocess_near_feasible_ratio", 0.20),
        help="Maximum normalized estimated violation for near-feasible postprocess fallback",
    )
    parser.add_argument(
        "--reopt-generations",
        type=int,
        default=default("reopt_generations", 0),
        help="Short re-optimization generations after an agent schema edit; 0 uses adaptive default",
    )
    parser.add_argument(
        "--reopt-pop-size",
        type=int,
        default=default("reopt_pop_size", 0),
        help="Short re-optimization population after an agent schema edit; 0 uses adaptive default",
    )
    parser.add_argument(
        "--action-strategy",
        choices=("combo_coarse_fine",),
        default=default("action_strategy", "combo_coarse_fine"),
        help="Schema action planning strategy",
    )
    parser.add_argument(
        "--enable-intervention-model",
        action="store_true",
        default=bool(default("enable_intervention_model", False)),
        help="Build a local action-to-spec model with small SPICE perturbations before agent action selection",
    )
    parser.add_argument("--disable-intervention-model", dest="enable_intervention_model", action="store_false", help=argparse.SUPPRESS)
    parser.add_argument(
        "--intervention-max-actions",
        type=int,
        default=default("intervention_max_actions", 4),
        help="Maximum causal tuning actions to perturb with SPICE for the local intervention model",
    )
    parser.add_argument(
        "--intervention-perturbation",
        type=float,
        default=default("intervention_perturbation", 0.10),
        help="Default fractional perturbation for local intervention modeling when an action has no explicit value",
    )
    parser.add_argument(
        "--intervention-transient-policy",
        choices=("targeted", "auto", "off"),
        default=default("intervention_transient_policy", "targeted"),
        help="Transient use in local intervention probes: targeted only runs TRAN for slew actions.",
    )
    parser.add_argument(
        "--runtime-profile",
        choices=("standard", "ablation_fast"),
        default=default("runtime_profile", "standard"),
        help="Runtime budget profile for agent/postprocess execution.",
    )
    parser.add_argument(
        "--force-postprocess-after-schema-edit",
        action="store_true",
        default=bool(default("force_postprocess_after_schema_edit", False)),
        help="Run one fallback postprocess pass after each applied schema edit.",
    )
    parser.add_argument(
        "--no-force-postprocess-after-schema-edit",
        dest="force_postprocess_after_schema_edit",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--asir", dest="no_asir", action="store_false", help=argparse.SUPPRESS)
    parser.add_argument(
        "--no-asir",
        action="store_true",
        default=bool(default("no_asir", False)),
        help="Disable ASIR semantic enrichment on schema/SPICE input",
    )
    args = parser.parse_args(argv)
    if "--llm-api-key" in explicit_args:
        args.llm_api_key_file = ""
        args.llm_api_key_stdin = False
    elif "--llm-api-key-file" in explicit_args:
        args.llm_api_key = ""
        args.llm_api_key_stdin = False
    elif "--llm-api-key-stdin" in explicit_args:
        args.llm_api_key = ""
        args.llm_api_key_file = ""
    return args


def _config_defaults(argv=None) -> dict[str, object]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", default=DEFAULT_CLI_CONFIG)
    known, _unknown = parser.parse_known_args(argv)
    defaults = load_cli_config(known.config)
    local_defaults = load_cli_config("configs/local/llm.yaml")
    defaults.update(local_defaults)
    if local_defaults.get("llm_api_key_file"):
        defaults["llm_api_key"] = ""
        defaults["llm_api_key_stdin"] = False
    defaults["config"] = known.config
    return defaults


def _configure_llm_api_key(args) -> None:
    key = ""
    if args.llm_api_key:
        key = args.llm_api_key
    elif args.llm_api_key_file:
        key = Path(args.llm_api_key_file).expanduser().read_text(encoding="utf-8")
    elif args.llm_api_key_stdin:
        key = sys.stdin.read()

    key = key.strip()
    if key:
        os.environ[args.llm_api_key_env] = key


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        _configure_llm_api_key(args)
        config = FlowConfig(
            env=args.env,
            schema=args.schema,
            spice=args.spice or None,
            spice_yaml_out=args.spice_yaml_out or None,
            topology=args.topology,
            pop_size=args.pop_size,
            generations=args.generations,
            seed=args.seed,
            skip_dc_repair=bool(args.skip_dc_repair),
            skip_comp_tune=bool(args.skip_comp_tune),
            postprocess_policy=args.postprocess_policy,
            postprocess_near_feasible_ratio=float(args.postprocess_near_feasible_ratio),
            ngspice_bin=args.ngspice_bin or None,
            tail_current_mirror=bool(args.tail_current_mirror),
            run_asir=not bool(args.no_asir),
            runs_dir=args.runs_dir,
            enable_intervention_model=bool(args.enable_intervention_model or args.agent_rounds > 1),
            intervention_max_actions=max(0, int(args.intervention_max_actions)),
            intervention_perturbation_fraction=float(args.intervention_perturbation),
            intervention_transient_policy=args.intervention_transient_policy,
            reopt_generations=int(args.reopt_generations) if int(args.reopt_generations) > 0 else None,
            reopt_pop_size=int(args.reopt_pop_size) if int(args.reopt_pop_size) > 0 else None,
            action_strategy=args.action_strategy,
            runtime_profile=args.runtime_profile,
            force_postprocess_after_schema_edit=bool(args.force_postprocess_after_schema_edit),
            llm_policy=args.llm_policy,
        )
        if args.agent_rounds > 1:
            DiagnosticAgentLoop(
                config=config,
                rounds=args.agent_rounds,
                legacy_state_builder=build_design_state,
                llm_config=LLMPlannerConfig.from_env(
                    provider=args.llm_provider,
                    model=args.llm_model or None,
                    base_url=args.llm_base_url or None,
                    api_key_env=args.llm_api_key_env,
                    timeout_seconds=args.llm_timeout,
                    temperature=args.llm_temperature,
                    max_tokens=args.llm_max_tokens,
                    thinking=args.llm_thinking or None,
                    reasoning_effort=args.llm_reasoning_effort or None,
                ),
                emit=print,
            ).run()
        else:
            AnalogRFIRFlowRunner(
                config=config,
                legacy_state_builder=build_design_state,
                emit=print,
            ).run()
    except RuntimeError as exc:
        print(f"\n[FATAL] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

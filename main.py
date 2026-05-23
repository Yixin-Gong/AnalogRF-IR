#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from flow.agent_loop import DiagnosticAgentLoop
from flow.llm_planner import LLMPlannerConfig
from flow.runner import AnalogRFIRFlowRunner, FlowConfig
from topologies.legacy import build_design_state


DEFAULT_AGENT_MAX_ITERATIONS = 20


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="AnalogRF-IR v0.1 modular analog/RF optimizer")
    parser.add_argument("--env", default="environment.yaml", help="Environment YAML path")
    parser.add_argument("--schema", default="inputs/ota/five_transistor/five_transistor_ota.yaml", help="Schema YAML path")
    parser.add_argument("--spice", default="", help="Optional SPICE netlist to import before optimization")
    parser.add_argument("--spice-yaml-out", default="", help="YAML path generated from --spice")
    parser.add_argument("--topology", choices=("auto", "five", "two_stage", "yaml"), default="auto")
    parser.add_argument("--generations", type=int, default=50, help="NSGA-II generations")
    parser.add_argument("--pop-size", type=int, default=100, help="NSGA-II population size")
    parser.add_argument("--seed", type=int, default=None, help="Random seed")
    parser.add_argument(
        "--agent-rounds",
        type=int,
        default=DEFAULT_AGENT_MAX_ITERATIONS,
        help="Maximum diagnosis-guided schema tuning iterations; stops early when all specs pass",
    )
    parser.add_argument("--llm-provider", default="deepseek", help="LLM planner provider for LangGraph rounds")
    parser.add_argument("--llm-model", default="", help="LLM planner model, defaults to deepseek-v4-flash")
    parser.add_argument("--llm-base-url", default="", help="OpenAI-compatible LLM base URL")
    parser.add_argument("--llm-api-key-env", default="DEEPSEEK_API_KEY", help="Environment variable containing the LLM API key")
    parser.add_argument("--llm-timeout", type=float, default=None, help="LLM planner request timeout in seconds")
    parser.add_argument("--llm-temperature", type=float, default=None, help="LLM planner sampling temperature")
    parser.add_argument("--llm-max-tokens", type=int, default=None, help="LLM planner max output tokens")
    parser.add_argument(
        "--llm-thinking",
        choices=("disabled", "enabled"),
        default="",
        help="LLM planner thinking mode for providers that support it",
    )
    parser.add_argument(
        "--llm-reasoning-effort",
        default="",
        help="LLM planner reasoning effort, for example max",
    )
    api_key_group = parser.add_mutually_exclusive_group()
    api_key_group.add_argument(
        "--llm-api-key",
        default="",
        help="LLM API key value. Prefer --llm-api-key-file or --llm-api-key-env for regular use.",
    )
    api_key_group.add_argument(
        "--llm-api-key-file",
        default="",
        help="Path to a file containing the LLM API key.",
    )
    api_key_group.add_argument(
        "--llm-api-key-stdin",
        action="store_true",
        help="Read the LLM API key from standard input.",
    )
    parser.add_argument("--ngspice-bin", default="", help="Override ngspice executable path")
    parser.add_argument(
        "--tail-current-mirror",
        action="store_true",
        help="Enable five-transistor OTA tail mirror bias generation",
    )
    parser.add_argument(
        "--skip-dc-repair",
        action="store_true",
        help="Skip ngspice-driven DC balance/headroom repair before final verification",
    )
    parser.add_argument(
        "--skip-comp-tune",
        action="store_true",
        help="Skip ngspice-driven Cc/Rz compensation sweep before final verification",
    )
    parser.add_argument(
        "--enable-intervention-model",
        action="store_true",
        help="Build a local action-to-spec model with small SPICE perturbations before agent action selection",
    )
    parser.add_argument(
        "--intervention-max-actions",
        type=int,
        default=4,
        help="Maximum causal tuning actions to perturb with SPICE for the local intervention model",
    )
    parser.add_argument(
        "--intervention-perturbation",
        type=float,
        default=0.10,
        help="Default fractional perturbation for local intervention modeling when an action has no explicit value",
    )
    parser.add_argument(
        "--no-asir",
        action="store_true",
        help="Disable ASIR semantic enrichment on schema/SPICE input",
    )
    return parser.parse_args(argv)


def _configure_llm_api_key(args) -> None:
    key = ""
    if args.llm_api_key:
        key = args.llm_api_key
    elif args.llm_api_key_file:
        key = Path(args.llm_api_key_file).read_text(encoding="utf-8")
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
            ngspice_bin=args.ngspice_bin or None,
            tail_current_mirror=bool(args.tail_current_mirror),
            run_asir=not bool(args.no_asir),
            enable_intervention_model=bool(args.enable_intervention_model or args.agent_rounds > 1),
            intervention_max_actions=max(0, int(args.intervention_max_actions)),
            intervention_perturbation_fraction=float(args.intervention_perturbation),
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

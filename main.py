#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys

from flow.agent_loop import DiagnosticAgentLoop
from flow.runner import AnalogRFIRFlowRunner, FlowConfig
from topologies.legacy import build_design_state


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
        default=1,
        help="Run diagnosis-guided schema tuning for N rounds",
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
        "--no-asir",
        action="store_true",
        help="Disable ASIR semantic enrichment on schema/SPICE input",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
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
    )
    try:
        if args.agent_rounds > 1:
            DiagnosticAgentLoop(
                config=config,
                rounds=args.agent_rounds,
                legacy_state_builder=build_design_state,
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

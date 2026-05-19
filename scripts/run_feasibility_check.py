#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.environment import load_environment, resolve_project_path  # noqa: E402
from feasibility import FeasibilityConfig, TwoStageMillerFeasibilityChecker  # noqa: E402
from frontends.yaml_loader import build_design_state_from_yaml, load_yaml_mapping  # noqa: E402
from pygmid.plugin import GmIdPlugin  # noqa: E402
from topologies.legacy import build_design_state as build_legacy_state  # noqa: E402


def next_output_dir(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    ids = []
    for path in root.glob("feasibility_*"):
        try:
            ids.append(int(path.name.split("_")[-1]))
        except ValueError:
            pass
    return root / f"feasibility_{(max(ids) + 1) if ids else 1:03d}"


def load_state(env: dict, schema_path: Path, topology: str):
    schema = load_yaml_mapping(schema_path)
    topology_dict = schema.get("topology") or {}
    if topology == "yaml" or (topology == "auto" and isinstance(topology_dict, dict) and topology_dict.get("devices")):
        return build_design_state_from_yaml(schema, env)
    return build_legacy_state(env, schema_path, topology)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run physics-informed feasibility check")
    parser.add_argument("--env", default="environment.yaml")
    parser.add_argument("--schema", default="ir/schema_two_stage.yaml")
    parser.add_argument("--topology", choices=("auto", "five", "two_stage", "yaml"), default="auto")
    parser.add_argument("--samples", type=int, default=6000)
    parser.add_argument("--seed", type=int, default=19)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--current-overhead", type=float, default=None)
    parser.add_argument("--pm-k-min", type=float, default=2.2)
    parser.add_argument("--ft-multiple", type=float, default=5.0)
    args = parser.parse_args(argv)

    env_path = resolve_project_path(args.env)
    schema_path = resolve_project_path(args.schema)
    env = load_environment(env_path)
    state = load_state(env, schema_path, args.topology)
    arch = (state.topology.architecture or "").lower()
    if "two" not in arch and not any(dev.role == "second_stage_gain" for dev in state.topology.devices):
        raise RuntimeError("Only two-stage Miller OTA feasibility is currently implemented")

    gm_plugin = GmIdPlugin.from_environment(env)
    gm_adapter = gm_plugin.load(env)
    config = FeasibilityConfig(
        samples=args.samples,
        seed=args.seed,
        current_overhead=args.current_overhead,
        pm_margin_factor_min=args.pm_k_min,
        ft_multiple=args.ft_multiple,
    )
    checker = TwoStageMillerFeasibilityChecker(state, gm_adapter, config)
    report = checker.run()
    out_dir = resolve_project_path(args.output_dir) if args.output_dir else next_output_dir(ROOT / "runs")
    checker.write_report(out_dir, report)

    summary = {
        "classification": report["classification"],
        "output": str(out_dir),
        "evaluated": report["population_summary"]["evaluated"],
        "best": report["best_candidates"][0] if report["best_candidates"] else None,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

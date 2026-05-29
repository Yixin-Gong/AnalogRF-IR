from __future__ import annotations

import argparse
import json
from pathlib import Path

from asir.design import build_design
from asir.examples import COMPARATOR_BUILDERS
from asir.io.v1_yaml import build_design_from_v1_yaml, embed_asir_output, load_v1_yaml
import yaml


def _build_one(name: str):
    try:
        builder = COMPARATOR_BUILDERS[name]
    except KeyError as exc:
        raise SystemExit(f"Unknown architecture '{name}'. Choices: {', '.join(COMPARATOR_BUILDERS)}") from exc
    return build_design(builder())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analog Semantic IR comparator prototype")
    parser.add_argument(
        "architecture",
        nargs="?",
        default="strongarm",
        choices=[*COMPARATOR_BUILDERS.keys(), "all", "from-yaml"],
        help="Comparator architecture to export, or from-yaml for AnalogRF-IR YAML input",
    )
    parser.add_argument("input", nargs="?", help="YAML input path when architecture is from-yaml")
    parser.add_argument("--out", default="exports", help="Output YAML file or directory")
    parser.add_argument("--summary", action="store_true", help="Print JSON summary")
    parser.add_argument("--trace", default="", help="Trace a dependency target, e.g. delay or offset")
    parser.add_argument("--propagate", action="store_true", help="Run a sample forward propagation")
    parser.add_argument("--compare", default="", choices=list(COMPARATOR_BUILDERS.keys()), help="Compare semantic rewrite against another architecture")
    parser.add_argument("--asir-only", action="store_true", help="For from-yaml, write only ASIR output instead of embedding it")
    parser.add_argument("--in-place", action="store_true", help="For from-yaml, embed ASIR output back into the input YAML")
    args = parser.parse_args(argv)

    if args.architecture == "from-yaml":
        if not args.input:
            raise SystemExit("from-yaml requires an input YAML path")
        if args.in_place and args.asir_only:
            raise SystemExit("--in-place cannot be combined with --asir-only")
        design = build_design_from_v1_yaml(args.input)
        out_path = Path(args.input) if args.in_place else Path(args.out)
        if out_path.suffix.lower() not in {".yaml", ".yml"}:
            out_path = out_path / f"{design.name}_asir.yaml"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if args.asir_only:
            design.export_yaml(out_path)
        else:
            source_data = load_v1_yaml(args.input)
            with out_path.open("w", encoding="utf-8") as handle:
                yaml.safe_dump(embed_asir_output(source_data, design), handle, sort_keys=False, allow_unicode=True, width=120)
        _print_optional_queries(args, design)
        print(f"Exported AnalogRF-IR ASIR YAML: {out_path}")
        return 0

    if args.architecture == "all":
        out_dir = Path(args.out)
        for name in COMPARATOR_BUILDERS:
            design = _build_one(name)
            design.export_yaml(out_dir / f"{name}.yaml")
            if args.summary:
                print(json.dumps(design.summary(), indent=2))
        return 0

    design = _build_one(args.architecture)
    out_path = Path(args.out)
    if out_path.suffix.lower() not in {".yaml", ".yml"}:
        out_path = out_path / f"{args.architecture}.yaml"
    design.export_yaml(out_path)

    _print_optional_queries(args, design)

    print(f"Exported ASIR YAML: {out_path}")
    return 0


def _print_optional_queries(args, design) -> None:
    if args.summary:
        print(json.dumps(design.summary(), indent=2))
    if args.trace:
        print(json.dumps(design.dependency_graph.backward_trace(args.trace), indent=2))
    if args.propagate:
        values = design.dependency_graph.forward_propagate(
            {
                "CL": 20e-15,
                "Cint": 10e-15,
                "Csample": 30e-15,
                "gm_latch": 1e-3,
                "gm_input": 0.5e-3,
                "initial_delta_v": 2e-3,
                "logic_swing": 0.8,
                "R_reset": 1e3,
                "R_sample": 800.0,
                "mismatch": 2e-3,
                "device_area": 1e-12,
                "kT_over_C": 1e-4,
                "bandwidth": 1e8,
                "VDD": 1.0,
                "VSS": 0.0,
                "Cgs_input": 5e-15,
                "Cgd_input": 1e-15,
                "Vclock_swing": 1.0,
                "kickback_coupling": 0.02,
                "input_step": 10e-3,
                "icmr_min": 0.2,
                "icmr_max": 0.9,
                "switching_activity": 2.0,
            }
        )
        interesting = {
            key: values[key]
            for key in sorted(values)
            if key in {
                "delay",
                "cycle_time",
                "offset",
                "noise",
                "regeneration_time",
                "sampling_time",
                "energy",
                "energy_per_comparison",
                "kickback_noise",
                "input_capacitance",
                "metastability_margin",
                "max_sample_rate",
                "output_swing",
                "icmr",
            }
        }
        print(json.dumps(interesting, indent=2))
    if args.compare:
        other = _build_one(args.compare)
        print(json.dumps(design.compare_rewrite_to(other).to_dict(), indent=2))

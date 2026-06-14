#!/usr/bin/env python3
"""Create OTA stress schemas from the measured LLM-full frontier."""
from __future__ import annotations

import argparse
import csv
import json
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_MANIFEST = (
    "runs/ablations_ihp130_ota_llm_full_residual_escape_compact_reverse_20260611/manifest.json"
)
DEFAULT_CASE = "llm_full_residual_escape_postprocess_fallback"
DEFAULT_OUTPUT_DIR = "inputs/ota_stress/frontier"
DEFAULT_SUMMARY = "docs/assets/frontier_stress_targets.csv"

MIN_METRICS: dict[str, dict[str, Any]] = {
    "dc_gain": {"measurement": "dc_gain_db", "percentile": 35.0, "absolute_guard": 0.05},
    "unity_gain_bandwidth": {
        "measurement": "unity_gain_bandwidth",
        "percentile": 35.0,
        "scale_guard": 0.98,
    },
    "slew_rate": {"measurement": "slew_rate", "percentile": 35.0, "scale_guard": 0.98},
    "output_swing": {"measurement": "output_swing", "percentile": 35.0, "scale_guard": 0.98},
    "saturation_margin": {
        "measurement": "saturation_margin",
        "percentile": 35.0,
        "scale_guard": 0.95,
    },
}

FIXED_TARGETS: dict[str, dict[str, Any]] = {
    "phase_margin": {"direction": "min", "value": 60.0, "formula": "fixed_60_deg"},
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST, help="Source ablation manifest JSON")
    parser.add_argument("--case", default=DEFAULT_CASE, help="Manifest case used as frontier source")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Directory for generated stress schemas")
    parser.add_argument("--summary", default=DEFAULT_SUMMARY, help="CSV summary of generated target changes")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest_path = _project_path(args.manifest)
    output_dir = _project_path(args.output_dir)
    summary_path = _project_path(args.summary)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    grouped = _collect_measurements(manifest, args.case)
    grouped = {schema: _pareto_frontier(rows) for schema, rows in grouped.items()}
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    for schema_path_str in sorted(grouped):
        source_schema = _project_path(schema_path_str)
        schema = yaml.safe_load(source_schema.read_text(encoding="utf-8"))
        if not isinstance(schema, dict):
            raise ValueError(f"{source_schema} must contain a YAML mapping")
        stress_schema, schema_records = _build_stress_schema(
            schema,
            source_schema=schema_path_str,
            source_manifest=args.manifest,
            source_case=args.case,
            measurements=grouped[schema_path_str],
        )
        stress_name = f"{source_schema.stem}_frontier_stress.yaml"
        stress_path = output_dir / stress_name
        stress_path.write_text(
            yaml.safe_dump(stress_schema, sort_keys=False, allow_unicode=False),
            encoding="utf-8",
        )
        for record in schema_records:
            record["stress_schema"] = _relative(stress_path)
            records.append(record)

    _write_summary(summary_path, records)
    print(f"Wrote {len(grouped)} stress schemas to {_relative(output_dir)}")
    print(f"Wrote stress target summary to {_relative(summary_path)}")
    return 0


def _collect_measurements(manifest: dict[str, Any], case_name: str) -> dict[str, list[dict[str, float]]]:
    grouped: dict[str, list[dict[str, float]]] = {}
    for job in manifest.get("jobs", []) or []:
        if job.get("case") != case_name:
            continue
        summary = job.get("summary", {}) or {}
        if summary.get("spec_pass") is not True:
            continue
        measurements = summary.get("measurements", {}) or {}
        numeric = {
            key: float(value)
            for key, value in measurements.items()
            if _is_number(value) and math.isfinite(float(value))
        }
        grouped.setdefault(str(job.get("schema", "")), []).append(numeric)
    if not grouped:
        raise ValueError(f"No passing measurements found for case {case_name!r}")
    return grouped


def _pareto_frontier(rows: list[dict[str, float]]) -> list[dict[str, float]]:
    if len(rows) < 2:
        return rows
    return [
        row
        for row in rows
        if not any(_dominates(other, row) for other in rows if other is not row)
    ]


def _dominates(a: dict[str, float], b: dict[str, float]) -> bool:
    min_keys = [str(rule["measurement"]) for rule in MIN_METRICS.values()]
    if not all(key in a and key in b for key in min_keys):
        return False
    no_worse = all(a[key] >= b[key] for key in min_keys)
    better = any(a[key] > b[key] for key in min_keys)
    return no_worse and better


def _build_stress_schema(
    schema: dict[str, Any],
    *,
    source_schema: str,
    source_manifest: str,
    source_case: str,
    measurements: list[dict[str, float]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    stress = deepcopy(schema)
    original_design_name = str(stress.get("design_name", Path(source_schema).stem))
    stress["design_name"] = f"{original_design_name}_frontier_stress"
    metadata = dict(stress.get("metadata", {}) or {})
    metadata["stress_profile"] = {
        "name": "frontier_stress",
        "source_manifest": source_manifest,
        "source_case": source_case,
        "rule": "PM fixed at 60 deg; gain/UGB/SR/swing/saturation use non-power Pareto p35 with guard bands; power is excluded from the stress objective",
    }
    stress["metadata"] = metadata

    targets = deepcopy(stress.get("targets", {}) or {})
    records: list[dict[str, Any]] = []
    for target_name, rule in MIN_METRICS.items():
        if target_name not in targets or "min" not in targets[target_name]:
            continue
        nominal = float(targets[target_name]["min"])
        values = [row[rule["measurement"]] for row in measurements if rule["measurement"] in row]
        frontier = percentile(values, float(rule["percentile"]))
        if "absolute_guard" in rule:
            stress_value = max(nominal, frontier - float(rule["absolute_guard"]))
            formula = f"max(nominal, p{rule['percentile']}-abs_guard)"
        else:
            stress_value = max(nominal, frontier * float(rule["scale_guard"]))
            formula = f"max(nominal, p{rule['percentile']}*scale_guard)"
        targets[target_name]["min"] = round_sig(stress_value)
        records.append(
            _record(source_schema, target_name, "min", nominal, frontier, targets[target_name]["min"], formula)
        )

    for target_name, rule in FIXED_TARGETS.items():
        if target_name not in targets:
            continue
        direction = str(rule["direction"])
        nominal = float(targets[target_name].get(direction, rule["value"]))
        targets[target_name][direction] = round_sig(float(rule["value"]))
        records.append(
            _record(
                source_schema,
                target_name,
                direction,
                nominal,
                float(rule["value"]),
                targets[target_name][direction],
                str(rule["formula"]),
            )
        )

    if "power" in targets:
        targets["power"]["priority"] = 9

    stress["targets"] = targets
    stress["loss_terms"] = _without_power_loss_terms(stress.get("loss_terms", []) or [])
    return stress, records


def _without_power_loss_terms(loss_terms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    for term in loss_terms:
        term_id = str(term.get("id", "")).lower()
        formula = str(term.get("formula", "")).lower()
        if "power" in term_id or "targets.power" in formula or "realized.power" in formula:
            continue
        kept.append(term)
    return kept


def _record(
    source_schema: str,
    target_name: str,
    direction: str,
    nominal: float,
    frontier: float,
    stress_value: float,
    formula: str,
) -> dict[str, Any]:
    return {
        "source_schema": source_schema,
        "target": target_name,
        "direction": direction,
        "nominal": nominal,
        "frontier_percentile": frontier,
        "stress_target": stress_value,
        "formula": formula,
    }


def _write_summary(path: Path, records: list[dict[str, Any]]) -> None:
    fields = [
        "source_schema",
        "stress_schema",
        "target",
        "direction",
        "nominal",
        "frontier_percentile",
        "stress_target",
        "formula",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def percentile(values: list[float], p: float) -> float:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        raise ValueError("Cannot compute percentile from empty values")
    if len(clean) == 1:
        return clean[0]
    position = (len(clean) - 1) * p / 100.0
    lo = math.floor(position)
    hi = math.ceil(position)
    if lo == hi:
        return clean[lo]
    weight = position - lo
    return clean[lo] * (1.0 - weight) + clean[hi] * weight


def round_sig(value: float, sig: int = 4) -> float:
    if value == 0:
        return 0.0
    return round(value, sig - int(math.floor(math.log10(abs(value)))) - 1)


def _project_path(path_like: str) -> Path:
    path = Path(path_like)
    if not path.is_absolute():
        path = ROOT / path
    return path


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def _is_number(value: Any) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())

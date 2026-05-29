#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flow.config import deep_merge_config, write_yaml_mapping  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run or dry-run AnalogRF-IR ablation comparisons")
    parser.add_argument("--config", default="configs/ablation.yaml", help="Ablation matrix YAML")
    parser.add_argument("--case", action="append", default=[], help="Run only a named case; may be repeated")
    parser.add_argument("--schema", action="append", default=[], help="Run only a schema path; may be repeated")
    parser.add_argument("--seed", action="append", type=int, default=[], help="Run only a seed; may be repeated")
    parser.add_argument("--limit", type=int, default=0, help="Limit generated jobs after filtering")
    parser.add_argument("--output-dir", default="", help="Override ablation output directory")
    parser.add_argument("--python", default=sys.executable, help="Python executable used to launch main.py")
    parser.add_argument(
        "--local-config",
        action="append",
        default=[],
        help="Local YAML overrides merged into generated job configs; may be repeated",
    )
    parser.add_argument(
        "--llm-api-key-file",
        default="",
        help="Path to a file containing the LLM API key, injected into generated job configs",
    )
    parser.add_argument("--run", action="store_true", help="Execute jobs; default is dry-run")
    parser.add_argument("--keep-going", action="store_true", help="Continue after a failing job")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    plan = _load_yaml(ROOT / args.config if not Path(args.config).is_absolute() else Path(args.config))
    output_dir = ROOT / (args.output_dir or plan.get("output_dir", "runs/ablations"))
    output_dir.mkdir(parents=True, exist_ok=True)
    local_overrides = [_load_yaml(_resolve_project_path(path)) for path in args.local_config]
    llm_api_key_file = _normalize_optional_path(args.llm_api_key_file)
    jobs = build_jobs(
        plan,
        output_dir=output_dir,
        selected_cases=args.case,
        selected_schemas=args.schema,
        selected_seeds=args.seed,
        local_overrides=local_overrides,
        llm_api_key_file=llm_api_key_file,
    )
    if args.limit and args.limit > 0:
        jobs = jobs[: args.limit]
    manifest = {
        "schema_version": "analogrf_ir.ablation_manifest.v0_1",
        "created_at": datetime.now().isoformat(),
        "source_config": args.config,
        "mode": "run" if args.run else "dry_run",
        "job_count": len(jobs),
        "jobs": [],
    }
    for job in jobs:
        config_path = write_yaml_mapping(job["config_path"], job["config"])
        command = [args.python, str(ROOT / "main.py"), "--config", str(config_path)]
        record = {
            "name": job["name"],
            "case": job["case"],
            "schema": job["schema"],
            "seed": job["seed"],
            "family": job.get("family", ""),
            "description": job.get("description", ""),
            "config": str(config_path),
            "runs_dir": str(job["runs_dir"]),
            "command": command,
            "status": "pending",
        }
        print(" ".join(command))
        if args.run:
            result = subprocess.run(command, cwd=str(ROOT), check=False)
            record["return_code"] = result.returncode
            record["status"] = "passed" if result.returncode == 0 else "failed"
            record["summary"] = _latest_result_summary(job["runs_dir"])
            manifest["jobs"].append(record)
            _write_manifest(output_dir, manifest)
            if result.returncode != 0 and not args.keep_going:
                return result.returncode
        else:
            record["status"] = "dry_run"
            manifest["jobs"].append(record)
    _write_manifest(output_dir, manifest)
    _write_summary_table(output_dir, manifest)
    return 0


def build_jobs(
    plan: dict[str, Any],
    *,
    output_dir: Path,
    selected_cases: list[str],
    selected_schemas: list[str],
    selected_seeds: list[int],
    local_overrides: list[dict[str, Any]] | None = None,
    llm_api_key_file: str = "",
) -> list[dict[str, Any]]:
    base_config_path = ROOT / plan.get("base_config", "configs/default.yaml")
    base_config = _load_yaml(base_config_path)
    base_overrides = plan.get("base_overrides", {}) or {}
    runtime_overrides: dict[str, Any] = {}
    for override in local_overrides or []:
        runtime_overrides = deep_merge_config(runtime_overrides, override)
    if llm_api_key_file:
        runtime_overrides = deep_merge_config(runtime_overrides, {"llm": {"api_key_file": llm_api_key_file}})
    schemas = selected_schemas or list(plan.get("schemas", []) or [])
    seeds = selected_seeds or list(plan.get("seeds", []) or [None])
    case_filter = set(selected_cases)
    jobs: list[dict[str, Any]] = []
    for case in plan.get("cases", []) or []:
        case_name = str(case.get("name", "unnamed"))
        if case_filter and case_name not in case_filter:
            continue
        for schema in schemas:
            schema_stem = _safe_name(Path(schema).stem)
            for seed in seeds:
                run_name = _safe_name(f"{case_name}__{schema_stem}__seed_{seed if seed is not None else 'none'}")
                runs_dir = output_dir / run_name
                config = deep_merge_config(base_config, base_overrides)
                config = deep_merge_config(config, runtime_overrides)
                config = deep_merge_config(config, case.get("overrides", {}) or {})
                config = deep_merge_config(
                    config,
                    {
                        "input": {"schema": schema},
                        "optimizer": {"seed": seed},
                        "output": {"runs_dir": str(runs_dir)},
                    },
                )
                jobs.append(
                    {
                        "name": run_name,
                        "case": case_name,
                        "family": case.get("family", ""),
                        "description": case.get("description", ""),
                        "schema": schema,
                        "seed": seed,
                        "runs_dir": runs_dir,
                        "config_path": output_dir / "generated_configs" / f"{run_name}.yaml",
                        "config": config,
                    }
                )
    return jobs


def _latest_result_summary(runs_dir: Path) -> dict[str, Any]:
    results = sorted(runs_dir.rglob("result.json"), key=lambda path: path.stat().st_mtime)
    if not results:
        return {}
    ranked: list[tuple[tuple[float, float, float, float], Path, dict[str, Any]]] = []
    for result_path in results:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        ranked.append((_result_rank(payload), result_path, payload))
    _rank, result_path, payload = min(ranked, key=lambda item: item[0])
    status = payload.get("status", {}) or {}
    return {
        "result_json": str(result_path),
        "spec_pass": status.get("spec_pass"),
        "failed_targets": status.get("failed_targets", []),
        "unverified_targets": status.get("unverified_targets", []),
        "best_loss": status.get("best_loss"),
        "measurements": payload.get("measurements", {}),
    }


def _result_rank(payload: dict[str, Any]) -> tuple[float, float, float, float]:
    status = payload.get("status", {}) or {}
    try:
        best_loss = float(status.get("best_loss"))
    except (TypeError, ValueError):
        best_loss = float("inf")
    failed_count = len(status.get("failed_targets", []) or [])
    unverified_count = len(status.get("unverified_targets", []) or [])
    measurements = payload.get("measurements", {}) or {}
    gain = float(measurements.get("dc_gain_db", -200.0) or -200.0)
    return (
        0.0 if status.get("spec_pass", False) else 1.0,
        float(failed_count + unverified_count),
        best_loss,
        -gain,
    )


def _write_manifest(output_dir: Path, manifest: dict[str, Any]) -> None:
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _write_summary_table(output_dir: Path, manifest: dict[str, Any]) -> None:
    lines = ["case,schema,seed,status,spec_pass,best_loss,failed_targets,unverified_targets,runs_dir"]
    for job in manifest.get("jobs", []):
        summary = job.get("summary", {}) or {}
        lines.append(
            ",".join(
                [
                    str(job.get("case", "")),
                    str(job.get("schema", "")),
                    str(job.get("seed", "")),
                    str(job.get("status", "")),
                    str(summary.get("spec_pass", "")),
                    str(summary.get("best_loss", "")),
                    "|".join(str(item) for item in summary.get("failed_targets", []) or []),
                    "|".join(str(item) for item in summary.get("unverified_targets", []) or []),
                    str(job.get("runs_dir", "")),
                ]
            )
        )
    (output_dir / "summary.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _resolve_project_path(path_like: str) -> Path:
    path = Path(path_like).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def _normalize_optional_path(path_like: str) -> str:
    if not path_like:
        return ""
    path = _resolve_project_path(path_like)
    return str(path)


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)


if __name__ == "__main__":
    raise SystemExit(main())

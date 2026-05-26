#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import csv
import json
import math
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


MEASUREMENT_KEYS = {
    "dc_gain": "dc_gain_db",
    "unity_gain_bandwidth": "unity_gain_bandwidth",
    "phase_margin": "phase_margin",
    "slew_rate": "slew_rate",
    "output_swing": "output_swing",
    "power": "total_power",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run progressive spec tightening and extract an ngspice Pareto frontier")
    parser.add_argument("--base-config", default="configs/default.yaml", help="Base editable run config YAML")
    parser.add_argument("--env", default="environment_ihp_sg13g2.yaml", help="Environment YAML")
    parser.add_argument("--schema", action="append", default=[], help="Input schema; may be repeated")
    parser.add_argument("--seed", action="append", type=int, default=[], help="Optimizer seed; may be repeated")
    parser.add_argument("--output-dir", default="", help="Output directory; defaults to runs/progressive_pareto_NNN")
    parser.add_argument("--levels", type=int, default=6, help="Maximum target ladder levels per schema/seed")
    parser.add_argument("--stop-after-fail", type=int, default=2, help="Stop a ladder after this many consecutive failing levels")
    parser.add_argument("--generations", type=int, default=50)
    parser.add_argument("--pop-size", type=int, default=100)
    parser.add_argument("--agent-rounds", type=int, default=1)
    parser.add_argument("--postprocess-policy", choices=("fallback", "always", "off"), default="fallback")
    parser.add_argument("--llm-provider", default="deterministic")
    parser.add_argument("--intervention", action="store_true", help="Enable local intervention model")
    parser.add_argument("--intervention-max-actions", type=int, default=8)
    parser.add_argument("--gain-step-db", type=float, default=2.0)
    parser.add_argument("--ugbw-step", type=float, default=1.25)
    parser.add_argument("--pm-step-deg", type=float, default=0.0)
    parser.add_argument("--slew-step", type=float, default=1.20)
    parser.add_argument("--swing-step-v", type=float, default=0.025)
    parser.add_argument("--power-step", type=float, default=0.92)
    parser.add_argument("--run", action="store_true", help="Execute jobs; default is dry-run")
    parser.add_argument("--keep-going", action="store_true", help="Continue other ladders after a job process failure")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = _output_dir(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "generated_configs").mkdir(exist_ok=True)
    (output_dir / "generated_schemas").mkdir(exist_ok=True)

    schemas = args.schema or [
        "inputs/ota/current_mirror/current_mirror_ota_ihp130.yaml",
        "inputs/ota/telescopic/telescopic_ota_ihp130.yaml",
        "inputs/ota/folded_cascode/folded_cascode_ota_ihp130.yaml",
    ]
    seeds = args.seed or [1]
    base_config = _load_yaml(_resolve(args.base_config))

    manifest: dict[str, Any] = {
        "schema_version": "analogrf_ir.progressive_pareto_manifest.v0_1",
        "created_at": datetime.now().isoformat(),
        "mode": "run" if args.run else "dry_run",
        "base_config": str(_resolve(args.base_config)),
        "env": args.env,
        "jobs": [],
    }
    rows: list[dict[str, Any]] = []

    for schema_path_text in schemas:
        schema_path = _resolve(schema_path_text)
        schema = _load_yaml(schema_path)
        for seed in seeds:
            fail_streak = 0
            for level in range(max(args.levels, 0)):
                ladder_schema = tighten_schema_targets(schema, level, args)
                run_name = _safe_name(f"{schema_path.stem}__seed_{seed}__level_{level:02d}")
                schema_out = output_dir / "generated_schemas" / f"{run_name}.yaml"
                config_out = output_dir / "generated_configs" / f"{run_name}.yaml"
                runs_dir = output_dir / run_name

                write_yaml_mapping(schema_out, ladder_schema)
                config = _job_config(base_config, args, schema_out, runs_dir, seed)
                write_yaml_mapping(config_out, config)

                command = [sys.executable, str(ROOT / "main.py"), "--config", str(config_out)]
                job: dict[str, Any] = {
                    "name": run_name,
                    "schema": str(schema_path),
                    "generated_schema": str(schema_out),
                    "seed": seed,
                    "level": level,
                    "runs_dir": str(runs_dir),
                    "config": str(config_out),
                    "command": command,
                    "targets": target_snapshot(ladder_schema),
                    "status": "pending",
                }
                print(" ".join(command))
                if args.run:
                    proc = subprocess.run(command, cwd=str(ROOT), check=False)
                    job["return_code"] = proc.returncode
                    job["status"] = "passed" if proc.returncode == 0 else "failed"
                    job["summary"] = _latest_result_summary(runs_dir)
                    row = row_from_job(job)
                    rows.append(row)
                    fail_streak = 0 if row.get("spec_pass") is True else fail_streak + 1
                    manifest["jobs"].append(job)
                    _write_outputs(output_dir, manifest, rows)
                    if proc.returncode != 0 and not args.keep_going:
                        return proc.returncode
                    if fail_streak >= max(args.stop_after_fail, 1):
                        break
                else:
                    job["status"] = "dry_run"
                    manifest["jobs"].append(job)
                    rows.append(row_from_job(job))
                    _write_outputs(output_dir, manifest, rows)

    _write_outputs(output_dir, manifest, rows)
    print(json.dumps(_summary(rows, output_dir), indent=2))
    print(f"Output: {output_dir}")
    return 0


def tighten_schema_targets(schema: dict[str, Any], level: int, args: argparse.Namespace) -> dict[str, Any]:
    out = copy.deepcopy(schema)
    targets = out.setdefault("targets", {})
    _add_min(targets, "dc_gain", level * float(args.gain_step_db))
    _scale_min(targets, "unity_gain_bandwidth", float(args.ugbw_step) ** level)
    _add_min(targets, "phase_margin", level * float(args.pm_step_deg))
    _scale_min(targets, "slew_rate", float(args.slew_step) ** level)
    _add_min(targets, "output_swing", level * float(args.swing_step_v))
    _scale_max(targets, "power", float(args.power_step) ** level)

    metadata = out.setdefault("metadata", {})
    metadata["progressive_target_level"] = level
    metadata["progressive_target_policy"] = {
        "gain_step_db": float(args.gain_step_db),
        "ugbw_step": float(args.ugbw_step),
        "phase_margin_step_deg": float(args.pm_step_deg),
        "slew_step": float(args.slew_step),
        "swing_step_v": float(args.swing_step_v),
        "power_step": float(args.power_step),
    }
    return out


def target_snapshot(schema: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for name, target in (schema.get("targets") or {}).items():
        if not isinstance(target, dict):
            continue
        item: dict[str, Any] = {}
        if "min" in target:
            item["min"] = target.get("min")
        if "max" in target:
            item["max"] = target.get("max")
        if item:
            snapshot[name] = item
    return snapshot


def pareto_frontier(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    feasible = [row for row in rows if row.get("spec_pass") is True]
    front: list[dict[str, Any]] = []
    for idx, row in enumerate(feasible):
        if any(_dominates(other, row) for j, other in enumerate(feasible) if idx != j):
            continue
        front.append(row)
    return sorted(front, key=lambda item: (float(item.get("total_power") or math.inf), -float(item.get("unity_gain_bandwidth") or 0.0)))


def _dominates(a: dict[str, Any], b: dict[str, Any]) -> bool:
    objectives = [
        ("total_power", "min"),
        ("dc_gain_db", "max"),
        ("unity_gain_bandwidth", "max"),
        ("phase_margin", "max"),
        ("slew_rate", "max"),
        ("output_swing", "max"),
    ]
    no_worse = True
    strictly_better = False
    for key, direction in objectives:
        av = _objective_value(a, key, direction)
        bv = _objective_value(b, key, direction)
        if direction == "min":
            no_worse = no_worse and av <= bv
            strictly_better = strictly_better or av < bv
        else:
            no_worse = no_worse and av >= bv
            strictly_better = strictly_better or av > bv
    return no_worse and strictly_better


def row_from_job(job: dict[str, Any]) -> dict[str, Any]:
    summary = job.get("summary", {}) or {}
    status = summary.get("status", {}) or {}
    measurements = summary.get("measurements", {}) or {}
    row: dict[str, Any] = {
        "name": job.get("name", ""),
        "schema": job.get("schema", ""),
        "seed": job.get("seed", ""),
        "level": job.get("level", ""),
        "job_status": job.get("status", ""),
        "return_code": job.get("return_code", ""),
        "spec_pass": status.get("spec_pass", summary.get("spec_pass")),
        "failed_targets": "|".join(status.get("failed_targets", summary.get("failed_targets", [])) or []),
        "result_json": summary.get("result_json", ""),
        "runs_dir": job.get("runs_dir", ""),
    }
    for spec, target in (job.get("targets", {}) or {}).items():
        if isinstance(target, dict):
            if "min" in target:
                row[f"target_{spec}_min"] = target.get("min")
            if "max" in target:
                row[f"target_{spec}_max"] = target.get("max")
    for key in sorted(set(MEASUREMENT_KEYS.values()) | set(measurements)):
        row[key] = measurements.get(key, "")
    row["score"] = _frontier_score(row)
    return row


def plot_progressive_frontier(out_dir: Path, rows: list[dict[str, Any]], front: list[dict[str, Any]]) -> None:
    try:
        import matplotlib.pyplot as plt
        import seaborn as sns
    except Exception:
        return
    if not rows:
        return

    fig, axes = plt.subplots(1, 2, figsize=(13.5, 5.2))
    data = [row for row in rows if _is_number(row.get("unity_gain_bandwidth")) and _is_number(row.get("dc_gain_db"))]
    if data:
        x = [float(row["unity_gain_bandwidth"]) / 1e6 for row in data]
        y = [float(row["dc_gain_db"]) for row in data]
        power = [max(float(row.get("total_power") or 0.0) * 1e6, 0.1) for row in data]
        level = [int(row.get("level") or 0) for row in data]
        markers = ["o" if row.get("spec_pass") is True else "X" for row in data]
        for marker in sorted(set(markers)):
            idxs = [idx for idx, item in enumerate(markers) if item == marker]
            label = "pass" if marker == "o" else "fail"
            sc = axes[0].scatter(
                [x[idx] for idx in idxs],
                [y[idx] for idx in idxs],
                c=[level[idx] for idx in idxs],
                s=[40.0 + 9.0 * power[idx] for idx in idxs],
                cmap="viridis",
                alpha=0.82,
                marker=marker,
                edgecolor="white",
                linewidth=0.7,
                label=label,
            )
        fig.colorbar(sc, ax=axes[0], label="Target level")
        if front:
            ordered = sorted(front, key=lambda row: float(row.get("unity_gain_bandwidth") or 0.0))
            axes[0].plot(
                [float(row.get("unity_gain_bandwidth") or 0.0) / 1e6 for row in ordered],
                [float(row.get("dc_gain_db") or 0.0) for row in ordered],
                color="#111111",
                linewidth=1.5,
                label="ngspice Pareto",
            )
        axes[0].set_xlabel("UGBW (MHz)")
        axes[0].set_ylabel("DC gain (dB)")
        axes[0].set_title("Gain-Bandwidth Frontier")
        axes[0].grid(True, alpha=0.25)
        axes[0].legend(loc="best")

    metric_rows: list[dict[str, Any]] = []
    for row in rows:
        for spec, measurement_key in MEASUREMENT_KEYS.items():
            achieved = _achievement(row, spec, measurement_key)
            if achieved is not None:
                metric_rows.append({"level": int(row.get("level") or 0), "spec": spec, "achievement": min(achieved, 1.5), "pass": row.get("spec_pass") is True})
    if metric_rows:
        import pandas as pd

        df = pd.DataFrame(metric_rows)
        sns.lineplot(data=df, x="level", y="achievement", hue="spec", marker="o", ax=axes[1])
        axes[1].axhline(1.0, color="#222222", linewidth=0.9, linestyle="--")
        axes[1].set_ylim(0, 1.55)
        axes[1].set_xlabel("Target level")
        axes[1].set_ylabel("Target achievement, capped")
        axes[1].set_title("Progressive Target Stress")
        axes[1].grid(True, alpha=0.25)
        axes[1].legend(loc="best", fontsize=8)

    fig.suptitle("Progressive ngspice Pareto Frontier", y=1.02)
    fig.tight_layout()
    fig.savefig(out_dir / "progressive_pareto_frontier.png", dpi=180, bbox_inches="tight")
    fig.savefig(out_dir / "progressive_pareto_frontier.pdf", bbox_inches="tight")
    plt.close(fig)


def _write_outputs(output_dir: Path, manifest: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    front = pareto_frontier(rows)
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _write_csv(output_dir / "progressive_results.csv", rows)
    _write_csv(output_dir / "pareto_frontier.csv", front)
    plot_progressive_frontier(output_dir, rows, front)
    (output_dir / "summary.json").write_text(json.dumps(_summary(rows, output_dir), indent=2), encoding="utf-8")


def _job_config(base_config: dict[str, Any], args: argparse.Namespace, schema_out: Path, runs_dir: Path, seed: int) -> dict[str, Any]:
    return deep_merge_config(
        base_config,
        {
            "input": {"env": args.env, "schema": str(schema_out), "topology": "yaml"},
            "optimizer": {"generations": args.generations, "pop_size": args.pop_size, "seed": seed},
            "agent": {"rounds": args.agent_rounds},
            "llm": {"provider": args.llm_provider},
            "intervention": {"enable": bool(args.intervention), "max_actions": args.intervention_max_actions},
            "postprocess": {"policy": args.postprocess_policy},
            "output": {"runs_dir": str(runs_dir)},
        },
    )


def _latest_result_summary(runs_dir: Path) -> dict[str, Any]:
    results = sorted(runs_dir.rglob("result.json"), key=lambda path: path.stat().st_mtime)
    if not results:
        return {}
    payload = json.loads(results[-1].read_text(encoding="utf-8"))
    return {
        "result_json": str(results[-1]),
        "status": payload.get("status", {}) or {},
        "measurements": payload.get("measurements", {}) or {},
    }


def _summary(rows: list[dict[str, Any]], output_dir: Path) -> dict[str, Any]:
    front = pareto_frontier(rows)
    return {
        "output_dir": str(output_dir),
        "runs": len(rows),
        "passes": sum(1 for row in rows if row.get("spec_pass") is True),
        "frontier_points": len(front),
        "max_passed_level": max((int(row.get("level") or 0) for row in rows if row.get("spec_pass") is True), default=None),
        "best_frontier_score": min((_frontier_score(row) for row in front), default=None),
    }


def _achievement(row: dict[str, Any], spec: str, measurement_key: str) -> float | None:
    if not _is_number(row.get(measurement_key)):
        return None
    value = float(row[measurement_key])
    min_key = f"target_{spec}_min"
    max_key = f"target_{spec}_max"
    if _is_number(row.get(min_key)):
        return value / max(float(row[min_key]), 1e-30)
    if _is_number(row.get(max_key)):
        return float(row[max_key]) / max(value, 1e-30)
    return None


def _frontier_score(row: dict[str, Any]) -> float:
    power = float(row.get("total_power") or 0.0)
    gain = float(row.get("dc_gain_db") or 0.0)
    bw = float(row.get("unity_gain_bandwidth") or 0.0)
    pm = float(row.get("phase_margin") or 0.0)
    sr = float(row.get("slew_rate") or 0.0)
    return power * 1e6 - 0.08 * gain - 0.02 * (bw / 1e6) - 0.01 * pm - 0.005 * (sr / 1e6)


def _objective_value(row: dict[str, Any], key: str, direction: str) -> float:
    if _is_number(row.get(key)):
        return float(row[key])
    return math.inf if direction == "min" else -math.inf


def _add_min(targets: dict[str, Any], name: str, delta: float) -> None:
    target = targets.get(name)
    if isinstance(target, dict) and target.get("min") is not None:
        target["min"] = float(target["min"]) + float(delta)


def _add_max(targets: dict[str, Any], name: str, delta: float) -> None:
    target = targets.get(name)
    if isinstance(target, dict) and target.get("max") is not None:
        target["max"] = max(0.0, float(target["max"]) + float(delta))


def _scale_min(targets: dict[str, Any], name: str, scale: float) -> None:
    target = targets.get(name)
    if isinstance(target, dict) and target.get("min") is not None:
        target["min"] = float(target["min"]) * float(scale)


def _scale_max(targets: dict[str, Any], name: str, scale: float) -> None:
    target = targets.get(name)
    if isinstance(target, dict) and target.get("max") is not None:
        target["max"] = float(target["max"]) * float(scale)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def _output_dir(value: str) -> Path:
    if value:
        return _resolve(value)
    root = ROOT / "runs"
    ids: list[int] = []
    for path in root.glob("progressive_pareto_*"):
        try:
            ids.append(int(path.name.split("_")[-1]))
        except ValueError:
            pass
    return root / f"progressive_pareto_{(max(ids) + 1) if ids else 1:03d}"


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _resolve(path_like: str | Path) -> Path:
    path = Path(path_like)
    return path if path.is_absolute() else ROOT / path


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value)


def _is_number(value: Any) -> bool:
    try:
        return value is not None and value != "" and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
import pandas as pd
import seaborn as sns
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


METRICS = (
    ("dc_gain", "dc_gain_db", "Gain", "dB"),
    ("unity_gain_bandwidth", "unity_gain_bandwidth", "UGBW", "MHz"),
    ("phase_margin", "phase_margin", "PM", "deg"),
    ("slew_rate", "slew_rate", "SR", "V/us"),
    ("output_swing", "output_swing", "Swing", "V"),
    ("power", "total_power", "Power", "uW"),
)

BLUE_CMAP = LinearSegmentedColormap.from_list(
    "analogdiag_blue",
    ["#f7fbff", "#d6e9f8", "#93c4df", "#3f8fc3", "#0b4f9c"],
)
HEADER_BLUE = "#123a70"
ACCENT_BLUE = "#d7e9fb"
GRID_BLUE = "#b7c9dc"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot full-flow OTA results from one ablation manifest")
    parser.add_argument(
        "--manifest",
        default="runs/ablations_ihp130_ota_unified_seed1_10/manifest.json",
        help="Unified ablation manifest JSON",
    )
    parser.add_argument(
        "--case",
        default="llm_diagnosis_postprocess_fallback",
        help="Case name to summarize from the manifest",
    )
    parser.add_argument("--out-dir", action="append", default=[], help="Output directory; may repeat")
    parser.add_argument("--format", action="append", default=[], choices=("png", "pdf", "svg"))
    parser.add_argument("--write-csv", action="store_true")
    parser.add_argument("--dpi", type=int, default=240)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dirs = [Path(p) for p in args.out_dir] or [ROOT / "docs" / "assets"]
    formats = args.format or ["png"]
    rows = collect_rows(_resolve(args.manifest), args.case)
    if not rows:
        raise SystemExit("No full-flow result.json files found in the requested manifest/case.")
    records = pd.DataFrame(rows)
    figures = {
        "full_flow_ota_achievement": plot_achievement_heatmap(records),
        "full_flow_ota_summary": plot_summary_table(records),
    }
    for out_dir in out_dirs:
        out_dir = _resolve(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        if args.write_csv:
            records.to_csv(out_dir / "full_flow_ota_results.csv", index=False)
        for fmt in formats:
            for name, fig in figures.items():
                fig.savefig(out_dir / f"{name}.{fmt}", dpi=args.dpi)
    for fig in figures.values():
        plt.close(fig)
    print(f"Wrote full-flow OTA figures for {len(records)} topologies.")
    return 0


def collect_rows(manifest_path: Path, case_name: str) -> list[dict[str, Any]]:
    manifest = _load_json(manifest_path)
    jobs = [job for job in manifest.get("jobs", []) or [] if str(job.get("case", "")) == case_name]
    per_run: list[dict[str, Any]] = []
    schema_cache: dict[str, dict[str, Any]] = {}
    for job in jobs:
        schema_path = str(job.get("schema", ""))
        schema = schema_cache.setdefault(schema_path, _load_yaml(_resolve(schema_path)))
        targets = schema.get("targets", {}) or {}
        result_path = _manifest_result_path(job)
        if result_path is None:
            continue
        result = _load_json(result_path)
        measurements = result.get("measurements", {}) or {}
        status = _status_label(measurements, targets)
        run = {
            "topology": _topology_label(schema, schema_path),
            "schema": schema_path,
            "seed": job.get("seed", ""),
            "result_json": str(result_path.relative_to(ROOT)) if result_path.is_relative_to(ROOT) else str(result_path),
            "iteration": _iteration_number(result_path),
            "status": status,
            "spec_pass": status == "pass",
        }
        for target, metric, label, unit in METRICS:
            raw = _to_float(measurements.get(metric))
            run[f"raw_{label}"] = raw
            run[f"ratio_{label}"] = _achievement_ratio(raw, targets.get(target, {}))
            run[f"target_{label}"] = _target_value(targets.get(target, {}) or {}, unit)
        per_run.append(run)

    if not per_run:
        return []
    frame = pd.DataFrame(per_run)
    rows: list[dict[str, Any]] = []
    for topology, group in frame.groupby("topology", sort=False):
        row: dict[str, Any] = {
            "topology": topology,
            "n_runs": int(len(group)),
            "pass_count": int(group["spec_pass"].sum()),
            "pass_fraction": f"{int(group['spec_pass'].sum())}/{int(len(group))}",
            "median_iteration": int(round(float(group["iteration"].median()))) if "iteration" in group else 0,
            "spec_pass_rate": float(group["spec_pass"].mean()),
        }
        for _target, _metric, label, unit in METRICS:
            raw_median = float(group[f"raw_{label}"].median())
            ratio_median = float(group[f"ratio_{label}"].median())
            row[f"raw_{label}"] = raw_median
            row[f"ratio_{label}"] = ratio_median
            row[label] = _display_value(raw_median, unit)
        rows.append(row)
    return rows


def plot_records(records: pd.DataFrame) -> plt.Figure:
    sns.set_theme(style="whitegrid", context="paper")

    fig = plt.figure(figsize=(10.8, 4.8), constrained_layout=True)
    grid = fig.add_gridspec(1, 2, width_ratios=[1.35, 0.9])
    ax0 = fig.add_subplot(grid[0, 0])
    ax1 = fig.add_subplot(grid[0, 1])

    _draw_achievement_heatmap(ax0, records)
    _draw_summary_table(ax1, records)
    return fig


def plot_achievement_heatmap(records: pd.DataFrame) -> plt.Figure:
    sns.set_theme(style="whitegrid", context="paper")
    fig, ax = plt.subplots(figsize=(5.25, 3.85), constrained_layout=True)
    _draw_achievement_heatmap(ax, records)
    return fig


def plot_summary_table(records: pd.DataFrame) -> plt.Figure:
    sns.set_theme(style="whitegrid", context="paper")
    fig, ax = plt.subplots(figsize=(5.25, 3.85), constrained_layout=True)
    _draw_summary_table(ax, records)
    return fig


def _achievement_frame(records: pd.DataFrame) -> pd.DataFrame:
    labels = [metric[2] for metric in METRICS]
    heatmap = records.set_index("topology")[[f"ratio_{label}" for label in labels]]
    heatmap.columns = labels
    return heatmap.clip(upper=1.5)


def _draw_achievement_heatmap(ax: plt.Axes, records: pd.DataFrame) -> None:
    sns.heatmap(
        _achievement_frame(records),
        annot=True,
        fmt=".2f",
        cmap=BLUE_CMAP,
        vmin=0,
        vmax=1.5,
        linewidths=0.6,
        linecolor="white",
        annot_kws={"fontsize": 7.4, "color": "#0f172a"},
        cbar_kws={"label": "achievement ratio", "shrink": 0.82},
        ax=ax,
    )
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.tick_params(axis="x", rotation=0, labelsize=8)
    ax.tick_params(axis="y", rotation=0, labelsize=8)
    ax.collections[0].colorbar.ax.tick_params(labelsize=7)
    ax.collections[0].colorbar.ax.yaxis.label.set_size(8)


def _draw_summary_table(ax: plt.Axes, records: pd.DataFrame) -> None:
    display = records[["topology", "pass_fraction", "Gain", "UGBW", "PM", "SR"]].copy()
    display["topology"] = display["topology"].replace(
        {
            "Current mirror": "Current\nmirror",
            "Folded cascode": "Folded\ncascode",
        }
    )
    ax.axis("off")
    table = ax.table(
        cellText=display[["topology", "pass_fraction", "Gain", "UGBW", "PM", "SR"]].values,
        colLabels=["Topology", "Pass", "Gain\n(dB)", "UGBW\n(MHz)", "PM\n(deg)", "SR\n(V/us)"],
        loc="center",
        cellLoc="center",
        colLoc="center",
        colWidths=[0.26, 0.09, 0.14, 0.17, 0.13, 0.15],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(7.5)
    table.scale(1.0, 1.62)
    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor(GRID_BLUE)
        cell.set_linewidth(0.55)
        if row == 0:
            cell.set_facecolor(HEADER_BLUE)
            cell.get_text().set_color("white")
            cell.get_text().set_weight("bold")
        elif row % 2 == 0:
            cell.set_facecolor("#f4f8fc")
        else:
            cell.set_facecolor("white")
        if row > 0 and col == 0:
            cell.set_facecolor(ACCENT_BLUE)
            cell.get_text().set_weight("bold")


def _manifest_result_path(job: dict[str, Any]) -> Path | None:
    summary = job.get("summary", {}) or {}
    if summary.get("result_json"):
        path = _resolve(summary["result_json"])
        if path.exists():
            return path
    runs_dir = _resolve(job.get("runs_dir", ""))
    results = sorted(runs_dir.rglob("result.json"), key=lambda path: path.stat().st_mtime)
    return results[-1] if results else None


def _result_rank(measurements: dict[str, Any], targets: dict[str, Any], result_path: Path) -> tuple[Any, ...]:
    failed = _failed_targets(measurements, targets, priorities=(1, 2))
    score = _violation_score(measurements, targets)
    iteration = -_iteration_number(result_path)
    return (len(failed), score, iteration)


def _failed_targets(measurements: dict[str, Any], targets: dict[str, Any], priorities: tuple[int, ...]) -> list[str]:
    failed: list[str] = []
    metric_map = {target: metric for target, metric, _label, _unit in METRICS}
    for name, target in targets.items():
        if int((target or {}).get("priority", 1)) not in priorities:
            continue
        metric = metric_map.get(name)
        if metric is None:
            continue
        value = _to_float(measurements.get(metric))
        if _target_fails(value, target):
            failed.append(name)
    return failed


def _status_label(measurements: dict[str, Any], targets: dict[str, Any]) -> str:
    failed = _failed_targets(measurements, targets, priorities=(1, 2))
    if not failed:
        return "pass"
    aliases = {
        "dc_gain": "gain",
        "unity_gain_bandwidth": "UGBW",
        "phase_margin": "PM",
        "slew_rate": "SR",
        "output_swing": "swing",
        "power": "power",
    }
    return "/".join(aliases.get(item, item) for item in failed) + " short"


def _topology_label(schema: dict[str, Any], schema_path: str) -> str:
    raw = str((schema.get("topology", {}) or {}).get("name") or schema.get("design_name") or Path(schema_path).stem)
    labels = {
        "five_transistor_ota": "5T",
        "current_mirror_ota_ihp130": "Current mirror",
        "folded_cascode_ota_ihp130": "Folded cascode",
        "telescopic_ota_ihp130": "Telescopic",
        "two_stage_miller_ota": "Two-stage",
    }
    return labels.get(raw, raw.replace("_", " ").title())


def _violation_score(measurements: dict[str, Any], targets: dict[str, Any]) -> float:
    score = 0.0
    metric_map = {target: metric for target, metric, _label, _unit in METRICS}
    for name, target in targets.items():
        if int((target or {}).get("priority", 1)) > 2:
            continue
        metric = metric_map.get(name)
        if metric is None:
            continue
        value = _to_float(measurements.get(metric))
        if value is None:
            score += 1.0
            continue
        target_min = _to_float((target or {}).get("min"))
        target_max = _to_float((target or {}).get("max"))
        if target_min is not None:
            score += max(0.0, (target_min - value) / max(abs(target_min), 1e-30)) ** 2
        if target_max is not None:
            score += max(0.0, (value - target_max) / max(abs(target_max), 1e-30)) ** 2
    return score


def _achievement_ratio(value: Any, target: dict[str, Any]) -> float:
    measured = _to_float(value)
    if measured is None:
        return 0.0
    target_min = _to_float((target or {}).get("min"))
    target_max = _to_float((target or {}).get("max"))
    if target_min is not None:
        return measured / max(abs(target_min), 1e-30)
    if target_max is not None:
        return target_max / max(abs(measured), 1e-30)
    return 0.0


def _target_fails(value: float | None, target: dict[str, Any]) -> bool:
    target_min = _to_float((target or {}).get("min"))
    target_max = _to_float((target or {}).get("max"))
    if target_min is not None and (value is None or value < target_min):
        return True
    if target_max is not None and (value is None or value > target_max):
        return True
    return False


def _display_value(value: Any, unit: str) -> str:
    numeric = _to_float(value)
    if numeric is None or not math.isfinite(numeric):
        return "N/A"
    if unit == "MHz":
        return f"{numeric / 1e6:.2f}"
    if unit == "V/us":
        return f"{numeric / 1e6:.2f}"
    if unit == "uW":
        return f"{numeric * 1e6:.1f}"
    if unit in {"dB", "deg"}:
        return f"{numeric:.1f}"
    if unit == "V":
        return f"{numeric:.3f}"
    return f"{numeric:.3g}"


def _target_value(target: dict[str, Any], unit: str) -> str:
    raw = (target or {}).get("min")
    if raw is None:
        raw = (target or {}).get("max")
    return _display_value(raw, unit)


def _iteration_number(path: Path) -> int:
    for parent in path.parents:
        if parent.name.startswith("iter_"):
            try:
                return int(parent.name.split("_", 1)[1])
            except ValueError:
                return 0
    return 0


def _to_float(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _resolve(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate


if __name__ == "__main__":
    raise SystemExit(main())

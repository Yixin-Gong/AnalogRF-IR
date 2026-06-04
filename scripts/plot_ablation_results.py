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


METRIC_MAP = {
    "dc_gain": "dc_gain_db",
    "unity_gain_bandwidth": "unity_gain_bandwidth",
    "phase_margin": "phase_margin",
    "slew_rate": "slew_rate",
    "output_swing": "output_swing",
    "power": "total_power",
}

SPEC_LABELS = {
    "dc_gain": "Gain",
    "unity_gain_bandwidth": "UGBW",
    "phase_margin": "Phase margin",
    "slew_rate": "Slew rate",
    "output_swing": "Output swing",
    "power": "Power",
}

CASE_LABELS = {
    "optimizer_only": "Optimizer",
    "optimizer_postprocess_fallback": "Opt + fallback PP",
    "optimizer_postprocess_always": "Opt + always PP",
    "diagnosis_surrogate_no_postprocess": "Diagnosis",
    "diagnosis_spice_intervention_no_postprocess": "Diagnosis + SPICE",
    "diagnosis_spice_postprocess_fallback": "Diagnosis + PP",
    "llm_diagnosis_no_postprocess": "LLM diagnosis",
    "llm_diagnosis_postprocess_fallback": "LLM + fallback PP",
    "llm_diagnosis_postprocess_always": "LLM + always PP",
}

TOPOLOGY_LABELS = {
    "current_mirror_ota_ihp130": "Current mirror",
    "telescopic_ota_ihp130": "Telescopic",
    "folded_cascode_ota_ihp130": "Folded cascode",
    "five_transistor_ota": "Five transistor",
    "two_stage_miller_ota": "Two-stage Miller",
    "source_follower_boosted_ota": "Source follower",
}

PLOT_TOPOLOGY_LABELS = {
    "Current mirror": "Current\nmirror",
    "Five transistor": "5T",
    "Folded cascode": "Folded\ncascode",
    "Telescopic": "Telescopic",
    "Two-stage Miller": "Two-stage\nMiller",
}

BLUE_CMAP = LinearSegmentedColormap.from_list(
    "analogdiag_ablation_blue",
    ["#f7fbff", "#d7e9fb", "#8bbce0", "#357db7", "#113b75"],
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot AnalogRF-IR ablation results")
    parser.add_argument("--manifest", default="runs/ablations_ihp130_ota/manifest.json", help="Ablation manifest JSON")
    parser.add_argument("--out-dir", default="", help="Output directory for CSV and figures")
    parser.add_argument("--format", action="append", default=[], choices=("png", "pdf", "svg"), help="Figure format; may repeat")
    parser.add_argument("--dpi", type=int, default=220, help="Raster figure DPI")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest_path = _resolve(args.manifest)
    out_dir = _resolve(args.out_dir) if args.out_dir else manifest_path.parent / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)
    formats = args.format or ["png", "pdf"]

    runs = collect_run_records(manifest_path)
    specs = collect_spec_records(runs)
    if runs.empty:
        raise SystemExit(f"No completed ablation results found under {manifest_path}")

    summary = summarize_runs(runs, specs)
    runs.to_csv(out_dir / "ablation_records.csv", index=False)
    specs.to_csv(out_dir / "spec_records.csv", index=False)
    summary.to_csv(out_dir / "method_topology_summary.csv", index=False)

    _setup_style()
    plot_success_heatmap(summary, out_dir, formats, args.dpi)
    plot_spec_achievement(specs, out_dir, formats, args.dpi)
    plot_metric_distributions(specs, out_dir, formats, args.dpi)
    plot_gain_bandwidth_tradeoff(runs, out_dir, formats, args.dpi)
    plot_method_traceability(runs, out_dir, formats, args.dpi)
    print(f"Wrote {len(runs)} runs, {len(specs)} spec rows, and figures to {out_dir}")
    return 0


def collect_run_records(manifest_path: Path) -> pd.DataFrame:
    manifest = _load_json(manifest_path)
    jobs = manifest.get("jobs", []) or []
    records: list[dict[str, Any]] = []
    schema_cache: dict[str, dict[str, Any]] = {}
    for job in jobs:
        runs_dir = _resolve(job.get("runs_dir", ""))
        result_path = _result_path(job, runs_dir)
        if result_path is None:
            continue
        result = _load_json(result_path)
        sim_log = _load_json(result_path.with_name("sim_log.json")) if result_path.with_name("sim_log.json").exists() else {}
        schema_path = str(job.get("schema", ""))
        schema = schema_cache.setdefault(schema_path, _load_yaml(_resolve(schema_path)))
        topology = _topology_name(schema, schema_path)
        measurements = result.get("measurements", {}) or {}
        status = result.get("status", {}) or {}
        target_status = _evaluate_targets(measurements, schema.get("targets", {}) or {}, max_priority=2)
        llm = _llm_usage(runs_dir)
        postprocess_events = _postprocess_events(runs_dir)
        record = {
            "job": job.get("name", ""),
            "case": job.get("case", ""),
            "method": CASE_LABELS.get(str(job.get("case", "")), str(job.get("case", ""))),
            "family": job.get("family", ""),
            "schema": schema_path,
            "topology": topology,
            "topology_label": TOPOLOGY_LABELS.get(topology, topology.replace("_", " ").title()),
            "seed": job.get("seed", ""),
            "status": job.get("status", ""),
            "return_code": job.get("return_code", ""),
            "result_json": str(result_path),
            "runs_dir": str(runs_dir),
            "spec_pass": target_status["spec_pass"],
            "artifact_spec_pass": bool(status.get("spec_pass", False)),
            "ngspice_success": bool(status.get("ngspice_success", False)),
            "best_loss": _to_float(status.get("best_loss")),
            "failed_targets": "|".join(target_status["failed_targets"]),
            "artifact_failed_targets": "|".join(str(item) for item in status.get("failed_targets", []) or []),
            "llm_used": llm["used"],
            "llm_status": llm["status"],
            "llm_reason": llm["reason"],
            "postprocess_event_count": len(postprocess_events),
            "postprocess_types": "|".join(sorted({str(item.get("type", "")) for item in postprocess_events if item.get("type")})),
            "postprocess_any_pass": any(bool(item.get("spec_pass", False)) for item in postprocess_events),
        }
        for target_name, metric_name in METRIC_MAP.items():
            record[metric_name] = _to_float(measurements.get(metric_name))
        flow = sim_log.get("flow", {}) if isinstance(sim_log, dict) else {}
        decision = flow.get("postprocess_decision", {}) if isinstance(flow, dict) else {}
        record["postprocess_decision"] = decision.get("reason", "")
        records.append(record)
    return pd.DataFrame.from_records(records)


def _evaluate_targets(
    measurements: dict[str, Any],
    targets: dict[str, Any],
    *,
    max_priority: int,
) -> dict[str, Any]:
    failed: list[str] = []
    for target_name, target in targets.items():
        metric_name = METRIC_MAP.get(target_name)
        if not metric_name or not isinstance(target, dict):
            continue
        if int(target.get("priority", 1) or 1) > max_priority:
            continue
        measured = _to_float(measurements.get(metric_name))
        target_min = _to_float(target.get("min"))
        target_max = _to_float(target.get("max"))
        if target_min is not None and (measured is None or measured < target_min):
            failed.append(str(target_name))
        if target_max is not None and (measured is None or measured > target_max):
            failed.append(str(target_name))
    return {"spec_pass": not failed, "failed_targets": failed}


def collect_spec_records(runs: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    target_cache: dict[str, dict[str, Any]] = {}
    for item in runs.to_dict("records"):
        schema = str(item["schema"])
        targets = target_cache.setdefault(schema, (_load_yaml(_resolve(schema)).get("targets", {}) or {}))
        for target_name, target in targets.items():
            metric_name = METRIC_MAP.get(target_name)
            if not metric_name:
                continue
            measured = _to_float(item.get(metric_name))
            target_min = _to_float(target.get("min"))
            target_max = _to_float(target.get("max"))
            direction = "min" if target_min is not None else "max" if target_max is not None else ""
            target_value = target_min if direction == "min" else target_max
            passed = False
            achievement = math.nan
            margin = math.nan
            if measured is not None and target_value is not None:
                if direction == "min":
                    passed = measured >= target_value
                    achievement = measured / max(abs(target_value), 1e-30)
                    margin = (measured - target_value) / max(abs(target_value), 1e-30)
                elif direction == "max":
                    passed = measured <= target_value
                    achievement = target_value / max(abs(measured), 1e-30)
                    margin = (target_value - measured) / max(abs(target_value), 1e-30)
            rows.append(
                {
                    "job": item["job"],
                    "case": item["case"],
                    "method": item["method"],
                    "family": item["family"],
                    "schema": schema,
                    "topology": item["topology"],
                    "topology_label": item["topology_label"],
                    "seed": item["seed"],
                    "spec": target_name,
                    "spec_label": SPEC_LABELS.get(target_name, target_name),
                    "metric": metric_name,
                    "direction": direction,
                    "target": target_value,
                    "measured": measured,
                    "unit": str(target.get("unit", "")),
                    "passed": passed,
                    "achievement": achievement,
                    "capped_achievement": min(achievement, 1.5) if math.isfinite(achievement) else math.nan,
                    "normalized_margin": margin,
                }
            )
    return pd.DataFrame.from_records(rows)


def summarize_runs(runs: pd.DataFrame, specs: pd.DataFrame) -> pd.DataFrame:
    if runs.empty:
        return pd.DataFrame()
    spec_summary = (
        specs.groupby(["method", "case", "topology_label"], dropna=False)
        .agg(spec_pass_rate=("passed", "mean"), median_spec_margin=("normalized_margin", "median"))
        .reset_index()
        if not specs.empty
        else pd.DataFrame()
    )
    run_summary = (
        runs.groupby(["method", "case", "topology_label"], dropna=False)
        .agg(
            n_runs=("job", "count"),
            success_rate=("spec_pass", "mean"),
            ngspice_success_rate=("ngspice_success", "mean"),
            median_best_loss=("best_loss", "median"),
            llm_used_rate=("llm_used", "mean"),
            postprocess_rate=("postprocess_event_count", lambda values: (values > 0).mean()),
        )
        .reset_index()
    )
    if spec_summary.empty:
        return run_summary
    return run_summary.merge(spec_summary, on=["method", "case", "topology_label"], how="left")


def plot_success_heatmap(summary: pd.DataFrame, out_dir: Path, formats: list[str], dpi: int) -> None:
    data = summary.pivot_table(index="method", columns="topology_label", values="success_rate", aggfunc="mean")
    data = _ordered(data)
    data = data.rename(columns=PLOT_TOPOLOGY_LABELS)
    fig, ax = plt.subplots(figsize=(7.2, 3.55), constrained_layout=True)
    sns.heatmap(
        data,
        annot=True,
        fmt=".0%",
        cmap=BLUE_CMAP,
        vmin=0,
        vmax=1,
        linewidths=0.7,
        linecolor="white",
        annot_kws={"fontsize": 8.0, "color": "#0f172a"},
        cbar_kws={"label": "pass rate", "shrink": 0.82},
        ax=ax,
    )
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.tick_params(axis="x", rotation=0, labelsize=8)
    ax.tick_params(axis="y", rotation=0, labelsize=8.5)
    ax.collections[0].colorbar.ax.tick_params(labelsize=7.5)
    ax.collections[0].colorbar.ax.yaxis.label.set_size(8)
    for text, value in zip(ax.texts, data.to_numpy().ravel()):
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = 0.0
        text.set_color("white" if numeric >= 0.65 else "#0f172a")
    _save(fig, out_dir / "success_rate_by_method_topology", formats, dpi)


def plot_spec_achievement(specs: pd.DataFrame, out_dir: Path, formats: list[str], dpi: int) -> None:
    if specs.empty:
        return
    data = (
        specs.groupby(["method", "spec_label"], dropna=False)["capped_achievement"]
        .median()
        .reset_index()
        .pivot(index="method", columns="spec_label", values="capped_achievement")
    )
    data = _ordered(data)
    fig, ax = plt.subplots(figsize=(10.5, 5.2))
    sns.heatmap(data, annot=True, fmt=".2f", cmap="rocket_r", vmin=0, vmax=1.5, linewidths=0.8, linecolor="white", cbar_kws={"label": "Median target achievement, capped at 1.5"}, ax=ax)
    ax.axhline(0, color="#222222", linewidth=0.8)
    ax.set_title("Spec Achievement Across Methods")
    ax.set_xlabel("")
    ax.set_ylabel("")
    _save(fig, out_dir / "spec_achievement_heatmap", formats, dpi)


def plot_metric_distributions(specs: pd.DataFrame, out_dir: Path, formats: list[str], dpi: int) -> None:
    if specs.empty:
        return
    ordered_specs = [
        "dc_gain",
        "unity_gain_bandwidth",
        "phase_margin",
        "slew_rate",
        "output_swing",
        "power",
    ]
    selected = specs[specs["spec"].isin(ordered_specs)].copy()
    if selected.empty:
        return
    selected["spec"] = pd.Categorical(selected["spec"], categories=ordered_specs, ordered=True)
    selected = selected.sort_values("spec")
    selected["margin_pct"] = selected["normalized_margin"] * 100.0
    spec_labels = list(selected["spec_label"].drop_duplicates())
    fig_width = max(17.0, 3.1 * len(spec_labels))
    fig, axes = plt.subplots(1, len(spec_labels), figsize=(fig_width, 4.2), sharey=False)
    axes = list(axes) if hasattr(axes, "__iter__") else [axes]
    for ax, spec_label in zip(axes, spec_labels):
        chunk = selected[selected["spec_label"] == spec_label]
        sns.boxplot(data=chunk, x="topology_label", y="margin_pct", hue="method", fliersize=2.0, linewidth=0.9, ax=ax)
        ax.axhline(0.0, color="#222222", linewidth=0.9, linestyle="--")
        ax.set_title(spec_label)
        ax.set_xlabel("")
        ax.set_ylabel("Margin (%)" if ax is axes[0] else "")
        ax.tick_params(axis="x", rotation=25)
        if ax.legend_:
            ax.legend_.remove()
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=min(len(labels), 4), frameon=False)
    fig.suptitle("Normalized Spec Margins by Topology and Method", y=1.02)
    fig.tight_layout(rect=(0, 0.12, 1, 1))
    _save(fig, out_dir / "spec_margin_distributions", formats, dpi)


def plot_gain_bandwidth_tradeoff(runs: pd.DataFrame, out_dir: Path, formats: list[str], dpi: int) -> None:
    required = {"dc_gain_db", "unity_gain_bandwidth", "total_power"}
    if not required.issubset(runs.columns):
        return
    data = runs.dropna(subset=["dc_gain_db", "unity_gain_bandwidth"]).copy()
    if data.empty:
        return
    data["ugbw_mhz"] = data["unity_gain_bandwidth"] / 1e6
    data["power_uw"] = data["total_power"].fillna(0.0) * 1e6
    fig, ax = plt.subplots(figsize=(9.8, 5.6))
    sns.scatterplot(
        data=data,
        x="ugbw_mhz",
        y="dc_gain_db",
        hue="method",
        style="topology_label",
        size="power_uw",
        sizes=(50, 260),
        alpha=0.82,
        edgecolor="white",
        linewidth=0.6,
        ax=ax,
    )
    ax.set_xscale("log")
    ax.set_xlabel("Unity-gain bandwidth (MHz, log scale)")
    ax.set_ylabel("DC gain (dB)")
    ax.set_title("Gain-Bandwidth-Power Tradeoff")
    ax.grid(True, which="both", axis="x", alpha=0.22)
    ax.grid(True, which="major", axis="y", alpha=0.22)
    ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), frameon=False)
    fig.tight_layout()
    _save(fig, out_dir / "gain_bandwidth_power_tradeoff", formats, dpi)


def plot_method_traceability(runs: pd.DataFrame, out_dir: Path, formats: list[str], dpi: int) -> None:
    data = (
        runs.groupby("method", dropna=False)
        .agg(
            llm_used_rate=("llm_used", "mean"),
            postprocess_rate=("postprocess_event_count", lambda values: (values > 0).mean()),
            ngspice_success_rate=("ngspice_success", "mean"),
        )
        .reset_index()
    )
    long = data.melt(id_vars="method", var_name="trace", value_name="rate")
    long["trace"] = long["trace"].map(
        {
            "llm_used_rate": "LLM planner ok",
            "postprocess_rate": "Postprocess ran",
            "ngspice_success_rate": "ngspice ok",
        }
    )
    fig, ax = plt.subplots(figsize=(10.5, 5.8))
    sns.barplot(data=long, x="method", y="rate", hue="trace", palette=["#4c78a8", "#f58518", "#54a24b"], ax=ax)
    ax.set_ylim(0, 1.02)
    ax.set_ylabel("Rate")
    ax.set_xlabel("")
    ax.set_title("Method Traceability: LLM, Postprocess, and Simulation")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda value, _pos: f"{value:.0%}"))
    ax.tick_params(axis="x", rotation=20)
    ax.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.30))
    fig.tight_layout(rect=(0, 0.22, 1, 1))
    _save(fig, out_dir / "method_traceability", formats, dpi)


def _result_path(job: dict[str, Any], runs_dir: Path) -> Path | None:
    summary = job.get("summary", {}) or {}
    if summary.get("result_json"):
        path = _resolve(summary["result_json"])
        if path.exists():
            return path
    results = sorted(runs_dir.rglob("result.json"), key=lambda path: path.stat().st_mtime)
    return results[-1] if results else None


def _postprocess_events(runs_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for log_path in sorted(runs_dir.rglob("sim_log.json")):
        payload = _load_json(log_path)
        flow = payload.get("flow", {}) if isinstance(payload, dict) else {}
        for item in flow.get("postprocess", []) or []:
            if isinstance(item, dict):
                events.append(item)
    return events


def _llm_usage(runs_dir: Path) -> dict[str, Any]:
    statuses: list[str] = []
    reasons: list[str] = []
    for state_path in sorted(runs_dir.rglob("design_state.yaml")):
        for planner in _llm_planner_blocks(state_path):
            status = str(planner.get("status", ""))
            if status:
                statuses.append(status)
            reason = str(planner.get("reason", ""))
            if reason:
                reasons.append(reason)
    return {
        "used": any(status == "ok" for status in statuses),
        "status": "|".join(sorted(set(statuses))),
        "reason": " | ".join(sorted(set(reasons)))[:500],
    }


def _llm_planner_blocks(path: Path) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return blocks
    for idx, line in enumerate(lines):
        if line.strip() != "llm_planner:":
            continue
        base_indent = len(line) - len(line.lstrip(" "))
        block: dict[str, str] = {}
        for child in lines[idx + 1:]:
            if not child.strip():
                continue
            indent = len(child) - len(child.lstrip(" "))
            if indent <= base_indent:
                break
            stripped = child.strip()
            if stripped.startswith("status:"):
                block["status"] = _yaml_scalar_text(stripped.partition(":")[2])
            elif stripped.startswith("reason:"):
                block["reason"] = _yaml_scalar_text(stripped.partition(":")[2])
        if block:
            blocks.append(block)
    return blocks


def _yaml_scalar_text(value: str) -> str:
    return value.strip().strip("'\"")


def _topology_name(schema: dict[str, Any], schema_path: str) -> str:
    topology = schema.get("topology", {}) or {}
    return str(topology.get("name") or schema.get("design_name") or Path(schema_path).stem)


def _ordered(data: pd.DataFrame) -> pd.DataFrame:
    method_order = [label for key, label in CASE_LABELS.items() if label in data.index]
    rest = [item for item in data.index if item not in method_order]
    return data.loc[method_order + rest]


def _setup_style() -> None:
    sns.set_theme(context="notebook", style="whitegrid", font_scale=1.05)
    plt.rcParams.update(
        {
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.edgecolor": "#333333",
            "axes.labelcolor": "#222222",
            "text.color": "#222222",
            "savefig.bbox": "tight",
            "savefig.facecolor": "white",
        }
    )


def _save(fig, stem: Path, formats: list[str], dpi: int) -> None:
    for fmt in formats:
        fig.savefig(stem.with_suffix(f".{fmt}"), dpi=dpi if fmt == "png" else None)
    plt.close(fig)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, dict) else {}


def _resolve(path_like: str | Path) -> Path:
    path = Path(path_like)
    if path.is_absolute():
        return path
    return ROOT / path


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


if __name__ == "__main__":
    raise SystemExit(main())

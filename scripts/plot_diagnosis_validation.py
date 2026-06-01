#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "runs" / "ablations_ihp130_ota_unified_seed1_10" / "manifest.json"
DEFAULT_OUTPUT_DIRS = [
    ROOT / "docs" / "assets",
    ROOT / "paper" / "date" / "figures",
]

METHOD_LABELS = {
    "diagnosis_spice_postprocess_fallback": "Diagnosis + PP",
    "llm_diagnosis_postprocess_fallback": "LLM + fallback PP",
}

TOPOLOGY_LABELS = {
    "five_transistor_ota": "5T",
    "current_mirror_ota_ihp130": "Current mirror",
    "telescopic_ota_ihp130": "Telescopic",
    "folded_cascode_ota_ihp130": "Folded cascode",
    "two_stage_miller_ota": "Two-stage",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot diagnosis validation artifacts")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Unified ablation manifest JSON")
    parser.add_argument("--case", action="append", default=[], help="Manifest case to include; may repeat")
    parser.add_argument("--root", action="append", default=[], help="Ad-hoc run root to scan; may repeat")
    parser.add_argument("--out-dir", action="append", default=[], help="Output directory; may repeat")
    parser.add_argument("--format", action="append", default=[], choices=("png", "pdf", "svg"))
    parser.add_argument("--write-csv", action="store_true", help="Also write the extracted validation records")
    parser.add_argument("--dpi", type=int, default=240)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sources = _run_sources(args)
    out_dirs = [Path(p).resolve() for p in args.out_dir] if args.out_dir else DEFAULT_OUTPUT_DIRS
    formats = args.format or ["png"]

    actions, selected, overlaps = collect_records(sources)
    if actions.empty and selected.empty:
        raise SystemExit("No causal_diagnostics.json files with intervention records were found.")

    for out_dir in out_dirs:
        out_dir.mkdir(parents=True, exist_ok=True)
        if args.write_csv:
            actions.to_csv(out_dir / "diagnosis_intervention_records.csv", index=False)
            selected.to_csv(out_dir / "diagnosis_selected_action_records.csv", index=False)
            overlaps.to_csv(out_dir / "diagnosis_ranking_overlap_records.csv", index=False)
        fig = plot_validation(actions, selected, overlaps)
        for fmt in formats:
            fig.savefig(out_dir / f"diagnosis_validation.{fmt}", dpi=args.dpi, bbox_inches="tight")
        plt.close(fig)

    print(
        "Wrote diagnosis validation figure from "
        f"{len(actions)} local probes and {len(selected)} selected actions."
    )
    return 0


def collect_records(sources: list[dict[str, Any]]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    action_rows: list[dict[str, Any]] = []
    selected_rows: list[dict[str, Any]] = []
    overlap_rows: list[dict[str, Any]] = []

    for source in sources:
        root = Path(source["root"])
        for path in sorted(root.glob("**/iter_*/causal_diagnostics.json")):
            data = _load_json(path)
            run_name = path.parents[1].name
            method_id = str(source.get("case") or run_name.split("__", 1)[0])
            if method_id not in METHOD_LABELS:
                continue
            method = METHOD_LABELS.get(method_id, method_id.replace("_", " ").title())
            scope = data.get("scope", {}) if isinstance(data, dict) else {}
            topology_id = str(scope.get("topology", "unknown"))
            if topology_id == "unknown" and source.get("schema"):
                topology_id = Path(str(source["schema"])).stem
            topology = TOPOLOGY_LABELS.get(topology_id, topology_id.replace("_", " ").title())
            iteration = path.parent.name.replace("iter_", "")

            local_model = data.get("local_intervention_model", {}) or {}
            base = local_model.get("base_violation_vector", {}) or {}
            failed_metrics = {metric for metric, value in base.items() if _float(value) and _float(value) > 0}
            for action in local_model.get("action_effects", []) or []:
                if action.get("status") != "ok":
                    continue
                deltas = action.get("delta_violation_vector", {}) or {}
                reduced = {
                    metric: -_float(delta)
                    for metric, delta in deltas.items()
                    if metric in failed_metrics and _float(delta) is not None and _float(delta) < 0
                }
                worsened = {
                    metric: _float(delta)
                    for metric, delta in deltas.items()
                    if metric in failed_metrics and _float(delta) is not None and _float(delta) > 0
                }
                reduction = sum(reduced.values())
                worsening = sum(worsened.values())
                action_rows.append(
                    {
                        "run": run_name,
                        "iteration": iteration,
                        "method_id": method_id,
                        "method": method,
                        "topology_id": topology_id,
                        "topology": topology,
                        "action_id": action.get("action_id", ""),
                        "action_metric": action.get("metric", ""),
                        "source": action.get("source", ""),
                        "supported": bool(reduced),
                        "reduced_metrics": "|".join(sorted(reduced)),
                        "worsened_metrics": "|".join(sorted(worsened)),
                        "failed_metric_reduction": reduction,
                        "failed_metric_worsening": worsening,
                        "violation_reduction": _float(action.get("violation_reduction")),
                    }
                )

            optimizer = data.get("constrained_action_optimizer", {}) or {}
            for action in optimizer.get("selected_actions", []) or []:
                admissibility = action.get("action_admissibility", {}) or {}
                selected_rows.append(
                    {
                        "run": run_name,
                        "iteration": iteration,
                        "method_id": method_id,
                        "method": method,
                        "topology_id": topology_id,
                        "topology": topology,
                        "action_id": action.get("action_id", ""),
                        "action_class": action.get("action_class", ""),
                        "objective_delta": _float(action.get("objective_delta")),
                        "admissible": bool(admissibility.get("passed")),
                        "optimizer_selected": bool(action.get("optimizer_selected")),
                    }
                )

            comparison = data.get("sensitivity_ranking_comparison", {}) or {}
            if comparison:
                overlap_rows.append(
                    {
                        "run": run_name,
                        "iteration": iteration,
                        "method_id": method_id,
                        "method": method,
                        "topology_id": topology_id,
                        "topology": topology,
                        "top5_overlap_count": _float(comparison.get("top5_overlap_count")),
                    }
                )

    return (
        pd.DataFrame.from_records(action_rows),
        pd.DataFrame.from_records(selected_rows),
        pd.DataFrame.from_records(overlap_rows),
    )


def _run_sources(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.root:
        return [{"root": Path(path).resolve(), "case": ""} for path in args.root]
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = ROOT / manifest_path
    manifest = _load_json(manifest_path)
    selected_cases = set(args.case or METHOD_LABELS)
    sources: list[dict[str, Any]] = []
    for job in manifest.get("jobs", []) or []:
        case = str(job.get("case", ""))
        if case not in selected_cases:
            continue
        runs_dir = Path(str(job.get("runs_dir", "")))
        if not runs_dir.is_absolute():
            runs_dir = ROOT / runs_dir
        if runs_dir.exists():
            sources.append({"root": runs_dir, "case": case, "schema": job.get("schema", "")})
    if not sources:
        raise SystemExit(f"No diagnosis run roots found in manifest {manifest_path}")
    return sources


def plot_validation(actions: pd.DataFrame, selected: pd.DataFrame, overlaps: pd.DataFrame) -> plt.Figure:
    sns.set_theme(style="whitegrid", font="DejaVu Sans", rc={"axes.edgecolor": "#d1d5db"})
    fig, axes = plt.subplots(1, 3, figsize=(11.2, 3.35), gridspec_kw={"width_ratios": [1.15, 0.9, 1.05]})
    palette = ["#3b6f8f", "#789c5c", "#d08c45", "#8a6bb8", "#c75c5c"]

    if not actions.empty:
        order = [label for label in TOPOLOGY_LABELS.values() if label in set(actions["topology"])]
        topo = (
            actions.groupby("topology", as_index=False)
            .agg(support_rate=("supported", "mean"), n=("supported", "size"))
            .sort_values("topology")
        )
        if order:
            topo["topology"] = pd.Categorical(topo["topology"], categories=order, ordered=True)
            topo = topo.sort_values("topology")
        ax = axes[0]
        ax.bar(topo["topology"], topo["support_rate"], color=palette[: len(topo)], width=0.72)
        ax.set_ylim(0, 1.0)
        ax.set_ylabel("Supported probes")
        ax.set_xlabel("")
        ax.set_title("Intervention support", fontsize=10)
        ax.tick_params(axis="x", rotation=35, labelsize=8)
        ax.yaxis.set_major_formatter(lambda x, _pos: f"{x:.0%}")
        for idx, row in enumerate(topo.to_dict("records")):
            ax.text(idx, row["support_rate"] + 0.035, f"{row['support_rate']:.0%}\nn={int(row['n'])}", ha="center", va="bottom", fontsize=7)

        counts = [
            ("probes", len(actions)),
            ("supported", int(actions["supported"].sum())),
            ("selected", len(selected)),
            ("admitted", int(selected["admissible"].sum()) if not selected.empty else 0),
        ]
        ax = axes[1]
        ax.bar([item[0] for item in counts], [item[1] for item in counts], color=["#94a3b8", "#3b6f8f", "#789c5c", "#2f7d57"], width=0.68)
        ax.set_title("Evidence gate", fontsize=10)
        ax.set_ylabel("Count")
        ax.tick_params(axis="x", rotation=25, labelsize=8)
        for idx, (_label, value) in enumerate(counts):
            ax.text(idx, value + max(2, len(actions) * 0.015), str(value), ha="center", va="bottom", fontsize=8)

    if not selected.empty:
        ax = axes[2]
        data = selected.dropna(subset=["objective_delta"]).copy()
        sns.stripplot(data=data, x="objective_delta", y="method", hue="method", dodge=False, legend=False, size=4.8, alpha=0.72, ax=ax)
        ax.axvline(0, color="#b91c1c", linewidth=1.0, linestyle="--")
        ax.set_title("Selected action Delta J", fontsize=10)
        ax.set_xlabel("Objective delta")
        ax.set_ylabel("")
        ax.tick_params(axis="y", labelsize=8)
        if not data.empty:
            median_delta = data["objective_delta"].median()
            ax.text(0.02, 0.08, f"median {median_delta:.3f}", transform=ax.transAxes, fontsize=8, color="#374151")
    elif not overlaps.empty:
        ax = axes[2]
        sns.boxplot(data=overlaps, x="top5_overlap_count", y="method", color="#94a3b8", ax=ax)
        ax.set_title("Causal vs. sensitivity", fontsize=10)
        ax.set_xlabel("Top-5 overlap")
        ax.set_ylabel("")

    for ax in axes:
        ax.grid(axis="y", color="#e5e7eb", linewidth=0.8)
        ax.grid(axis="x", visible=False)
        for spine in ax.spines.values():
            spine.set_color("#d1d5db")
    fig.tight_layout(w_pad=1.5)
    return fig


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


if __name__ == "__main__":
    raise SystemExit(main())

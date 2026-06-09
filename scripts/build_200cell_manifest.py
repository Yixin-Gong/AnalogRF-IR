#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NONLLM = ROOT / "runs" / "ablations_ihp130_ota_nonllm_seed1_10_20260604_r1" / "manifest.json"
DEFAULT_LLM = ROOT / "runs" / "ablations_ihp130_ota_llm_full_residual_escape_seed1_10_20260609_r1" / "manifest.json"
DEFAULT_OUT_DIR = ROOT / "runs" / "ablations_ihp130_ota_200cell_seed1_10_20260610"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the unified 200-cell OTA ablation manifest")
    parser.add_argument("--nonllm-manifest", default=str(DEFAULT_NONLLM), help="150-cell non-LLM manifest")
    parser.add_argument("--llm-manifest", default=str(DEFAULT_LLM), help="50-cell LLM full-flow manifest")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Output directory for manifest.json and summary.csv")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    nonllm_path = _resolve(args.nonllm_manifest)
    llm_path = _resolve(args.llm_manifest)
    out_dir = _resolve(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    nonllm = _load_manifest(nonllm_path)
    llm = _load_manifest(llm_path)
    jobs = list(nonllm.get("jobs", []) or []) + list(llm.get("jobs", []) or [])
    manifest = {
        "schema_version": "analogrf_ir.ablation_manifest.v0_1",
        "created_at": datetime.now().isoformat(),
        "source_config": "combined_200cell",
        "mode": "combined",
        "job_count": len(jobs),
        "source_manifests": [
            _display_path(nonllm_path),
            _display_path(llm_path),
        ],
        "jobs": jobs,
    }
    _validate_manifest(manifest)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _write_summary_csv(out_dir / "summary.csv", manifest)
    print(f"Wrote {len(jobs)} jobs to {_display_path(out_dir / 'manifest.json')}")
    return 0


def _validate_manifest(manifest: dict[str, Any]) -> None:
    jobs = manifest.get("jobs", []) or []
    expected_cases = {
        "optimizer_only": 50,
        "optimizer_postprocess_fallback": 50,
        "diagnosis_spice_postprocess_fallback": 50,
        "llm_full_residual_escape_postprocess_fallback": 50,
    }
    counts: dict[str, int] = {}
    for job in jobs:
        counts[str(job.get("case", ""))] = counts.get(str(job.get("case", "")), 0) + 1
    if counts != expected_cases:
        raise SystemExit(f"Unexpected 200-cell case counts: {counts}")


def _write_summary_csv(path: Path, manifest: dict[str, Any]) -> None:
    lines = ["case,schema,seed,status,spec_pass,best_loss,failed_targets,unverified_targets,runs_dir"]
    for job in manifest.get("jobs", []) or []:
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
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return data


def _resolve(path_like: str | Path) -> Path:
    path = Path(path_like)
    if not path.is_absolute():
        path = ROOT / path
    return path


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())

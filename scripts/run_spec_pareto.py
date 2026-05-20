#!/usr/bin/env python3
"""Run a spec-level Pareto search for the current OTA schema.

The normal optimizer returns one scalar-loss winner. This script keeps the
same decoding/physics model but changes the objective vector to expose the
tradeoff between spec deficit, power, gain, bandwidth, and phase margin.
It can also ngspice-check a small, diverse subset of the estimated front.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.environment import (  # noqa: E402
    existing_project_path as _existing_project_path,
    load_environment,
    resolve_project_path as _resolve_project_path,
)
from netlist.generator import generate_netlist  # noqa: E402
from optimizer.nsga2 import CircuitEvaluator, NSGA2Config, NSGA2Optimizer, round_and_update_state  # noqa: E402
from postprocess.common import backfill_state_from_ngspice  # noqa: E402
from postprocess.two_stage import balance_two_stage_output, improve_tail_headroom  # noqa: E402
from schemas.design_state import DesignState, Target, TransistorParameters  # noqa: E402
from simulator.ngspice import NgspiceSimulator  # noqa: E402
from topologies.legacy import build_design_state  # noqa: E402
from pygmid.adapter import create_pygmid_adapter  # noqa: E402


class SpecParetoEvaluator:
    """Multi-objective wrapper around the existing compact OTA evaluator."""

    def __init__(self, base: CircuitEvaluator):
        self.base = base
        self.schema = base.schema

    @property
    def n_vars(self) -> int:
        return self.base.n_vars

    @property
    def bounds(self):
        return self.base.bounds

    def evaluate(self, x: np.ndarray):
        _, violation, meta = self.base.evaluate(x)
        perf = meta.get("performance", {})
        t = self.schema.targets

        gain = float(perf.get("dc_gain", 0.0))
        bw = float(perf.get("unity_gain_bandwidth", 0.0))
        pm = float(perf.get("phase_margin", 0.0))
        power = float(perf.get("power", 0.0))

        gain_min = (t.get("dc_gain") or Target()).min or 1.0
        bw_min = (t.get("unity_gain_bandwidth") or Target()).min or 1.0
        pm_min = (t.get("phase_margin") or Target()).min or 1.0
        power_max = (t.get("power") or Target()).max or 1e-3

        spec_deficit = 0.0
        spec_deficit += max(0.0, gain_min - gain) / max(gain_min, 1.0)
        spec_deficit += max(0.0, bw_min - bw) / max(bw_min, 1.0)
        spec_deficit += max(0.0, pm_min - pm) / max(pm_min, 1.0)
        spec_deficit += max(0.0, power - power_max) / max(power_max, 1e-12)

        objectives = np.array([
            spec_deficit,
            power / max(power_max, 1e-12),
            -gain / max(gain_min, 1.0),
            -bw / max(bw_min, 1.0),
            -pm / max(pm_min, 1.0),
        ])
        meta["pareto"] = {
            "spec_deficit": spec_deficit,
            "power_norm": power / max(power_max, 1e-12),
            "gain_norm": gain / max(gain_min, 1.0),
            "bw_norm": bw / max(bw_min, 1.0),
            "pm_norm": pm / max(pm_min, 1.0),
        }
        return objectives, violation, meta


def next_pareto_dir(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    ids = []
    for path in root.glob("pareto_*"):
        try:
            ids.append(int(path.name.split("_")[-1]))
        except ValueError:
            pass
    return root / f"pareto_{(max(ids) + 1) if ids else 1:03d}"


def apply_meta_to_state(base_state: DesignState, meta: Dict[str, Any]) -> DesignState:
    state = base_state.clone()
    decoded = meta.get("decoded", {})
    tp = meta.get("transistor_params", {})
    state.global_parameters = {k: float(v) for k, v in decoded.get("__global__", {}).items()}

    for dev_id, vars_dict in decoded.items():
        if dev_id.startswith("__") or dev_id not in state.transistors:
            continue
        ts = state.transistors[dev_id]
        ts.gm_id_strategy = vars_dict.get("gm_id", 10)
        ts.L_strategy = vars_dict.get("L", 1e-7)
        if dev_id in tp:
            phys = tp[dev_id]
            ts.parameters = TransistorParameters(
                W=phys.get("W", 0.0),
                L=vars_dict.get("L", 1e-7),
                gm=phys.get("gm", 0.0),
                gds=phys.get("gds", 0.0),
                vgs=phys.get("vgs", 0.0),
                vds=phys.get("vds", 0.0),
                vdsat=phys.get("vdsat", 0.0),
                region=phys.get("region", "unknown"),
                id=phys.get("id", 0.0),
                ft=phys.get("ft", 0.0),
                gm_id_realized=phys.get("gm_id", 0.0),
                cgs=phys.get("cgs", 0.0),
                cgd=phys.get("cgd", 0.0),
            )
    round_and_update_state(state, decoded, tp)
    return state


def row_from_meta(index: int, rank: int, meta: Dict[str, Any]) -> Dict[str, Any]:
    perf = meta.get("performance", {})
    decoded = meta.get("decoded", {})
    glob = decoded.get("__global__", {})
    pareto = meta.get("pareto", {})
    row = {
        "index": index,
        "rank": rank,
        "spec_deficit": pareto.get("spec_deficit", ""),
        "dc_gain_db_est": perf.get("dc_gain", ""),
        "ugbw_hz_est": perf.get("unity_gain_bandwidth", ""),
        "phase_margin_deg_est": perf.get("phase_margin", ""),
        "power_w_est": perf.get("power", ""),
        "Cc_est": perf.get("Cc", glob.get("Cc", "")),
        "Rz_est": perf.get("Rz", glob.get("Rz", "")),
        "I_tail": glob.get("I_tail", ""),
        "I_stage2": glob.get("I_stage2", ""),
    }
    for dev_id, vars_dict in decoded.items():
        if dev_id.startswith("__"):
            continue
        row[f"{dev_id}_gm_id"] = vars_dict.get("gm_id", "")
        row[f"{dev_id}_L"] = vars_dict.get("L", "")
    return row


def write_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: List[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def diverse_subset(items: List[Dict[str, Any]], count: int) -> List[Dict[str, Any]]:
    if count <= 0 or len(items) <= count:
        return items
    items = sorted(items, key=lambda item: float(item["row"].get("ugbw_hz_est") or 0.0))
    picks = []
    for pos in np.linspace(0, len(items) - 1, count):
        picks.append(items[int(round(pos))])
    out = []
    seen = set()
    for item in picks:
        idx = item["row"]["index"]
        if idx not in seen:
            out.append(item)
            seen.add(idx)
    return out


def dominates(a: Dict[str, float], b: Dict[str, float]) -> bool:
    a_obj = np.array([
        a["power_w_meas"],
        -a["dc_gain_db_meas"],
        -a["ugbw_hz_meas"],
        -a["phase_margin_deg_meas"],
    ])
    b_obj = np.array([
        b["power_w_meas"],
        -b["dc_gain_db_meas"],
        -b["ugbw_hz_meas"],
        -b["phase_margin_deg_meas"],
    ])
    return bool(np.all(a_obj <= b_obj) and np.any(a_obj < b_obj))


def measured_front(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    front = []
    for i, row in enumerate(rows):
        if not any(dominates(other, row) for j, other in enumerate(rows) if i != j):
            front.append(row)
    return front


def plot_front(out_dir: Path, est_rows: List[Dict[str, Any]], meas_rows: List[Dict[str, Any]]) -> None:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return

    fig, ax = plt.subplots(figsize=(8, 5))
    if est_rows:
        x = [float(r["power_w_est"]) * 1e6 for r in est_rows]
        y = [float(r["ugbw_hz_est"]) / 1e6 for r in est_rows]
        c = [float(r["phase_margin_deg_est"]) for r in est_rows]
        sc = ax.scatter(x, y, c=c, s=28, cmap="viridis", alpha=0.7, label="estimated rank-0")
        fig.colorbar(sc, ax=ax, label="PM est (deg)")
    if meas_rows:
        x = [float(r["power_w_meas"]) * 1e6 for r in meas_rows]
        y = [float(r["ugbw_hz_meas"]) / 1e6 for r in meas_rows]
        ax.scatter(x, y, marker="x", c="crimson", s=70, label="ngspice checked")
    ax.axhline(500.0, color="0.55", lw=1.0, ls="--", label="UGBW spec")
    ax.set_xlabel("Power (uW)")
    ax.set_ylabel("UGBW (MHz)")
    ax.set_title("Spec Pareto Front")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(out_dir / "pareto_front.png", dpi=160)
    plt.close(fig)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run spec-level Pareto search")
    parser.add_argument("--env", default="environment.yaml")
    parser.add_argument("--schema", default="inputs/ota/two_stage_miller/two_stage_miller_ota.yaml")
    parser.add_argument("--topology", choices=("auto", "five", "two_stage"), default="auto")
    parser.add_argument("--pop-size", type=int, default=180)
    parser.add_argument("--generations", type=int, default=80)
    parser.add_argument("--seed", type=int, default=31)
    parser.add_argument("--verify", type=int, default=16, help="ngspice-check this many diverse front points")
    parser.add_argument("--ngspice-bin", default=None)
    args = parser.parse_args(argv)

    env_path = _resolve_project_path(args.env)
    env = load_environment(env_path)
    schema_path = _resolve_project_path(args.schema)
    state = build_design_state(env, schema_path, args.topology)

    tables_dir = env.get("tools", {}).get("pygmid_tables_dir", "tables")
    nmos_table = env.get("tools", {}).get("nmos_table")
    pmos_table = env.get("tools", {}).get("pmos_table")
    explicit_tables = bool(nmos_table or pmos_table)
    pygmid = create_pygmid_adapter(
        nmos_path=_existing_project_path(nmos_table),
        pmos_path=_existing_project_path(pmos_table),
        tables_dir=None if explicit_tables else str(_resolve_project_path(tables_dir)),
    )

    evaluator = SpecParetoEvaluator(CircuitEvaluator(state, pygmid))
    config = NSGA2Config(
        pop_size=args.pop_size,
        n_generations=args.generations,
        seed=args.seed,
        patience=args.generations + 1,
        verbose=True,
    )
    optimizer = NSGA2Optimizer(state, evaluator, config)
    optimizer.optimize()

    out_dir = next_pareto_dir(ROOT / "runs")
    out_dir.mkdir(parents=True, exist_ok=True)

    population = optimizer.last_population
    fronts = optimizer.last_fronts
    front0 = fronts[0] if fronts else []

    pop_rows = [row_from_meta(i, population[i].rank, population[i].meta) for i in range(len(population))]
    front_items = [
        {"index": i, "individual": population[i], "row": row_from_meta(i, 0, population[i].meta)}
        for i in front0
    ]
    front_rows = [item["row"] for item in front_items]

    write_csv(out_dir / "population_estimated.csv", pop_rows)
    write_csv(out_dir / "pareto_estimated.csv", front_rows)

    ngspice_rows: List[Dict[str, Any]] = []
    ngspice_bin = args.ngspice_bin or env.get("tools", {}).get("ngspice_bin", "ngspice")
    sim = NgspiceSimulator(timeout_sec=30, ngspice_bin=ngspice_bin)
    if args.verify > 0 and sim.check_available():
        for n, item in enumerate(diverse_subset(front_items, args.verify), start=1):
            cand_dir = out_dir / f"verify_{n:02d}"
            cand_dir.mkdir(parents=True, exist_ok=True)
            cand_state = apply_meta_to_state(state, item["individual"].meta)
            balance_two_stage_output(cand_state, sim, cand_dir)
            if improve_tail_headroom(cand_state, sim, cand_dir):
                balance_two_stage_output(cand_state, sim, cand_dir)
            netlist = generate_netlist(cand_state)
            result = sim.run(netlist, work_dir=str(cand_dir))
            backfill_state_from_ngspice(cand_state, result)
            meas = result.measurements
            row = dict(item["row"])
            row.update({
                "verify_dir": str(cand_dir.relative_to(ROOT)),
                "dc_gain_db_meas": meas.get("dc_gain_db", 0.0),
                "ugbw_hz_meas": meas.get("unity_gain_bandwidth", 0.0),
                "phase_margin_deg_meas": meas.get("phase_margin", 0.0),
                "power_w_meas": meas.get("total_power", 0.0),
                "unity_gain_crossings_meas": meas.get("unity_gain_crossings", 0.0),
            })
            ngspice_rows.append(row)
            cand_state.to_yaml(cand_dir / "design_state.yaml")
            (cand_dir / "netlist.cir").write_text(netlist, encoding="utf-8")

    spice_front = measured_front(ngspice_rows) if ngspice_rows else []
    write_csv(out_dir / "ngspice_checked.csv", ngspice_rows)
    write_csv(out_dir / "ngspice_pareto.csv", spice_front)
    plot_front(out_dir, front_rows, ngspice_rows)

    summary = {
        "env": str(env_path),
        "schema": str(schema_path),
        "pop_size": args.pop_size,
        "generations": args.generations,
        "seed": args.seed,
        "estimated_population": len(pop_rows),
        "estimated_pareto": len(front_rows),
        "ngspice_checked": len(ngspice_rows),
        "ngspice_pareto": len(spice_front),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Output: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

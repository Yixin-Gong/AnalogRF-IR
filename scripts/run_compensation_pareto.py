#!/usr/bin/env python3
"""Sweep Cc/Rz on an existing netlist and export an ngspice Pareto front."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from simulator.ngspice import NgspiceSimulator  # noqa: E402


def fmt_tag(cc: float, rz: float) -> str:
    return f"cc{cc:.2e}_rz{rz:.0f}".replace("+", "").replace("-", "m").replace(".", "p")


def replace_compensation(netlist: str, cc: float, rz: float) -> str:
    netlist = re.sub(r"^Rz\s+\S+\s+\S+\s+\S+", f"Rz n1 ncc {rz}", netlist, flags=re.M)
    netlist = re.sub(r"^Cc\s+\S+\s+\S+\s+\S+", f"Cc ncc vout {cc}", netlist, flags=re.M)
    return netlist


def dominates(a: dict, b: dict) -> bool:
    return (
        a["power_w"] <= b["power_w"]
        and a["dc_gain_db"] >= b["dc_gain_db"]
        and a["ugbw_hz"] >= b["ugbw_hz"]
        and a["phase_margin_deg"] >= b["phase_margin_deg"]
        and (
            a["power_w"] < b["power_w"]
            or a["dc_gain_db"] > b["dc_gain_db"]
            or a["ugbw_hz"] > b["ugbw_hz"]
            or a["phase_margin_deg"] > b["phase_margin_deg"]
        )
    )


def write_csv(path: Path, rows: list[dict]) -> None:
    keys = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        if keys:
            writer.writeheader()
            writer.writerows(rows)


def write_svg(path: Path, rows: list[dict], front: list[dict]) -> None:
    width, height, pad = 900, 560, 70
    xs = [r["phase_margin_deg"] for r in rows]
    ys = [r["ugbw_hz"] / 1e6 for r in rows]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)

    def sx(x: float) -> float:
        return pad + (x - xmin) / max(xmax - xmin, 1e-12) * (width - 2 * pad)

    def sy(y: float) -> float:
        return height - pad - (y - ymin) / max(ymax - ymin, 1e-12) * (height - 2 * pad)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<line x1="{pad}" y1="{height-pad}" x2="{width-pad}" y2="{height-pad}" stroke="#333"/>',
        f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{height-pad}" stroke="#333"/>',
        f'<line x1="{sx(60):.1f}" y1="{pad}" x2="{sx(60):.1f}" y2="{height-pad}" stroke="#999" stroke-dasharray="6 5"/>',
        f'<line x1="{pad}" y1="{sy(500):.1f}" x2="{width-pad}" y2="{sy(500):.1f}" stroke="#999" stroke-dasharray="6 5"/>',
    ]
    for row in rows:
        color = "#2ca25f" if row["spec_pass"] else "#9ecae1"
        parts.append(
            f'<circle cx="{sx(row["phase_margin_deg"]):.1f}" cy="{sy(row["ugbw_hz"]/1e6):.1f}" '
            f'r="4" fill="{color}" opacity="0.72"/>'
        )
    if front:
        pts = " ".join(f'{sx(r["phase_margin_deg"]):.1f},{sy(r["ugbw_hz"]/1e6):.1f}' for r in front)
        parts.append(f'<polyline points="{pts}" fill="none" stroke="#de2d26" stroke-width="2.5"/>')
        for row in front:
            parts.append(
                f'<circle cx="{sx(row["phase_margin_deg"]):.1f}" cy="{sy(row["ugbw_hz"]/1e6):.1f}" '
                f'r="5" fill="#de2d26"/>'
            )
    parts.extend([
        f'<text x="{width/2}" y="32" text-anchor="middle" font-family="Arial" font-size="20">Cc/Rz ngspice Pareto: UGBW vs PM</text>',
        f'<text x="{width/2}" y="{height-20}" text-anchor="middle" font-family="Arial" font-size="15">Phase margin (deg)</text>',
        f'<text x="22" y="{height/2}" text-anchor="middle" font-family="Arial" font-size="15" transform="rotate(-90 22 {height/2})">UGBW (MHz)</text>',
        f'<text x="{sx(60)+6:.1f}" y="{pad+18}" font-family="Arial" font-size="12" fill="#666">PM=60</text>',
        f'<text x="{pad+6}" y="{sy(500)-7:.1f}" font-family="Arial" font-size="12" fill="#666">UGBW=500MHz</text>',
    ])
    for val in (0, 30, 60, 90, 120):
        if xmin <= val <= xmax:
            parts.append(f'<text x="{sx(val):.1f}" y="{height-pad+22}" text-anchor="middle" font-family="Arial" font-size="12">{val}</text>')
    for val in (0, 250, 500, 750, 1000, 1250):
        if ymin <= val <= ymax:
            parts.append(f'<text x="{pad-10}" y="{sy(val)+4:.1f}" text-anchor="end" font-family="Arial" font-size="12">{val}</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sweep compensation Pareto")
    parser.add_argument("--netlist", default="runs/iter_024/netlist.cir")
    parser.add_argument("--out", default="runs/pareto_comp_001")
    parser.add_argument("--ngspice-bin", default="ngspice")
    args = parser.parse_args(argv)

    base = (ROOT / args.netlist).read_text(encoding="utf-8")
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    sim = NgspiceSimulator(timeout_sec=30, ngspice_bin=args.ngspice_bin)

    ccs = [5e-14, 7.5e-14, 1e-13, 1.25e-13, 1.5e-13, 2e-13, 2.5e-13,
           3e-13, 4e-13, 5e-13, 7.5e-13, 1e-12, 1.5e-12, 2.5e-12]
    rzs = [100, 300, 500, 750, 1000, 1500, 2000, 2500, 3000, 3500, 4000,
           4500, 5000, 6000, 7500, 10000, 15000, 20000]
    rows: list[dict] = []
    for cc in ccs:
        for rz in rzs:
            netlist = replace_compensation(base, cc, rz)
            result = sim.run(netlist, work_dir=str(out_dir / fmt_tag(cc, rz)))
            meas = result.measurements
            row = {
                "Cc": cc,
                "Rz": rz,
                "dc_gain_db": meas.get("dc_gain_db", 0.0),
                "ugbw_hz": meas.get("unity_gain_bandwidth", 0.0),
                "phase_margin_deg": meas.get("phase_margin", 0.0),
                "power_w": meas.get("total_power", 0.0),
                "unity_gain_crossings": meas.get("unity_gain_crossings", 0.0),
            }
            row["spec_pass"] = int(
                row["dc_gain_db"] >= 60
                and row["ugbw_hz"] >= 500e6
                and row["phase_margin_deg"] >= 60
                and row["power_w"] <= 5e-4
            )
            rows.append(row)

    front = []
    for i, row in enumerate(rows):
        if not any(dominates(other, row) for j, other in enumerate(rows) if i != j):
            front.append(row)
    front.sort(key=lambda r: r["ugbw_hz"])

    feasible = [row for row in rows if row["spec_pass"]]
    best_bw = max(feasible, key=lambda r: r["ugbw_hz"]) if feasible else None
    best_pm = max(feasible, key=lambda r: r["phase_margin_deg"]) if feasible else None
    summary = {
        "points": len(rows),
        "pareto_points": len(front),
        "spec_pass_points": len(feasible),
        "best_bw_feasible": best_bw,
        "best_pm_feasible": best_pm,
    }

    write_csv(out_dir / "sweep.csv", rows)
    write_csv(out_dir / "pareto.csv", front)
    write_svg(out_dir / "pareto_front.svg", rows, front)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Output: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import csv
import json
import math
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from schemas.design_state import DesignState, Target


@dataclass
class FeasibilityConfig:
    samples: int = 6000
    seed: int = 19
    fixed_point_iterations: int = 3
    current_overhead: float | None = None
    pm_margin_factor_min: float = 2.2
    ft_multiple: float = 5.0
    headroom_margin: float = 0.05
    top_count: int = 12


@dataclass
class FeasibilityCandidate:
    candidate_id: str
    gmID_in: float
    gmID_stage2: float
    gmID_load: float
    IC_in: float
    IC_stage2: float
    L_in: float
    L_load: float
    L_stage2: float
    Cc: float
    Cc_over_CL: float
    Rz_factor: float
    Rz: float
    Itail: float
    I2: float
    gm1: float
    gm2: float
    Av_pred_dB: float
    GBW_pred_Hz: float
    PM_pred_deg: float
    SRp_pred_V_s: float
    SRn_pred_V_s: float
    output_swing_V: float
    output_swing_low_V: float
    output_swing_high_V: float
    ICMR_min_V: float
    ICMR_max_V: float
    ICMR_range_V: float
    power_pred_W: float
    CL_eff: float
    Cpar_out: float
    parasitic_ratio: float
    headroom_margin_V: float
    ft_min_Hz: float
    max_width_m: float
    feasibility_score: float
    slacks: dict[str, float | None] = field(default_factory=dict)
    normalized_slacks: dict[str, float] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("slacks", None)
        row.pop("normalized_slacks", None)
        for key, value in self.slacks.items():
            row[f"slack_{key}"] = value
        return row


class TwoStageMillerFeasibilityChecker:
    """Physics-informed feasibility estimator for a two-stage Miller OTA.

    The checker searches high-level analog design variables (gm/ID, L, Cc/CL,
    current allocation, and nulling resistor factor). It deliberately does not
    optimize final transistor W/L. Widths are only estimated through gm/ID lookup
    to account for current density, parasitics, ft, and headroom risk.
    """

    def __init__(self, state: DesignState, gm_id_adapter: Any, config: FeasibilityConfig | None = None) -> None:
        self.state = state
        self.gm = gm_id_adapter
        self.config = config or FeasibilityConfig()
        self.spec = self._extract_specs()
        self.assumptions: list[str] = []
        if not (getattr(self.gm, "has_nmos_table", False) and getattr(self.gm, "has_pmos_table", False)):
            self.assumptions.append(
                "gm/ID lookup table is incomplete; analytical EKV/square-law fallback is used, so accuracy is lower."
            )
        else:
            self.assumptions.append("gm/ID lookup tables are used for current density, gds/ID, capacitance, Vdsat, and ft.")
        self.assumptions.append("Reported IC values are EKV-style estimates inferred from gm/ID and n*UT.")

    def run(self) -> dict[str, Any]:
        candidates = self._evaluate_candidates()
        ranked = sorted(candidates, key=lambda c: c.feasibility_score, reverse=True)
        top = self._select_top_candidates(ranked)
        classification = self._classify(ranked)
        bottlenecks = self._rank_bottlenecks(ranked)
        relax = self._relaxation_suggestions(ranked, bottlenecks)
        validation_plan = self._validation_plan(top)

        return {
            "schema_version": "objective_ir.feasibility.v1",
            "topology": {
                "name": self.state.topology.name,
                "class": self.state.topology.class_,
                "architecture": self.state.topology.architecture,
            },
            "process": {
                "name": self.state.process.process_name,
                "technology_node_um": self.state.process.technology_node,
                "vdd": self._vdd(),
                "cload_F": self.state.simulation.cload,
            },
            "spec": self.spec,
            "model": {
                "name": "two_stage_miller_physics_informed",
                "samples": self.config.samples,
                "seed": self.config.seed,
                "fixed_point_iterations": self.config.fixed_point_iterations,
                "pm_margin_factor_min": self.config.pm_margin_factor_min,
                "ft_multiple": self.config.ft_multiple,
                "assumptions": self.assumptions,
            },
            "classification": classification,
            "best_candidates": [asdict(c) for c in top],
            "bottleneck_ranking": bottlenecks,
            "spec_relaxation_recommendation": relax,
            "validation_plan": validation_plan,
            "population_summary": {
                "evaluated": len(ranked),
                "positive_score": sum(1 for c in ranked if c.feasibility_score > 0),
                "near_score": sum(1 for c in ranked if c.feasibility_score > -1),
                "best_score": ranked[0].feasibility_score if ranked else None,
            },
        }

    def write_report(self, out_dir: Path, report: dict[str, Any]) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "feasibility_report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        self._write_markdown(out_dir / "feasibility_report.md", report)
        self._write_candidates_csv(out_dir / "best_candidates.csv", report.get("best_candidates", []))

    def _evaluate_candidates(self) -> list[FeasibilityCandidate]:
        rng = random.Random(self.config.seed)
        choices = self._choice_space()
        seen: set[tuple[float, ...]] = set()
        candidates: list[FeasibilityCandidate] = []

        deterministic = self._deterministic_seed_points(choices)
        for point in deterministic:
            candidate = self._evaluate_point(len(candidates), point)
            if candidate:
                candidates.append(candidate)

        attempts = 0
        while len(candidates) < self.config.samples and attempts < self.config.samples * 8:
            attempts += 1
            point = (
                rng.choice(choices["gmID_in"]),
                rng.choice(choices["gmID_stage2"]),
                rng.choice(choices["gmID_load"]),
                rng.choice(choices["L_in"]),
                rng.choice(choices["L_load"]),
                rng.choice(choices["L_stage2"]),
                rng.choice(choices["Cc_over_CL"]),
                rng.choice(choices["Rz_factor"]),
            )
            if point in seen:
                continue
            seen.add(point)
            candidate = self._evaluate_point(len(candidates), point)
            if candidate:
                candidates.append(candidate)
        return candidates

    def _evaluate_point(self, index: int, point: tuple[float, ...]) -> FeasibilityCandidate | None:
        gm_id_in, gm_id_stage2, gm_id_load, l_in, l_load, l_stage2, cc_ratio, rz_factor = point
        vdd = self._vdd()
        cl = max(self.state.simulation.cload, 1e-18)
        gbw = max(float(self.spec["GBW_Hz"]), 1.0)
        wu = 2.0 * math.pi * gbw
        cc = self._clip_global("Cc", cc_ratio * cl)
        gm1 = wu * cc
        itail = 2.0 * gm1 / max(gm_id_in, 1e-12)
        id_in = itail / 2.0
        srp = itail / max(cc, 1e-30)
        pm_spec = float(self.spec["PM_deg"])
        k_pm = max(math.tan(math.radians(max(pm_spec, 1.0))), self.config.pm_margin_factor_min)
        srn_spec = self.spec.get("SRn_V_s")
        srn_required_current = 0.0
        cl_eff = cl

        n_in = p_load = p_stage2 = n_out = n_tail = None
        gm2 = i2 = srn = cpar_out = 0.0
        for _ in range(max(1, self.config.fixed_point_iterations)):
            gm2_required = k_pm * wu * cl_eff
            i2_pm = gm2_required / max(gm_id_stage2, 1e-12)
            srn_required_current = cl_eff * srn_spec if srn_spec else 0.0
            i2 = max(i2_pm, srn_required_current)
            n_in = self.gm.forward(gm_id_in, l_in, id_in, "nmos")
            p_load = self.gm.forward(gm_id_load, l_load, id_in, "pmos")
            p_stage2 = self.gm.forward(gm_id_stage2, l_stage2, i2, "pmos")
            n_out = self.gm.forward(gm_id_stage2, l_stage2, i2, "nmos")
            n_tail = self.gm.forward(max(6.0, min(gm_id_stage2, 14.0)), l_in, itail, "nmos")
            gm2 = gm_id_stage2 * i2
            cpar_out = self._output_parasitic(p_stage2) + self._output_parasitic(n_out)
            cl_eff = cl + cpar_out

        if not all((n_in, p_load, p_stage2, n_out, n_tail)):
            return None

        gds_corr = self.state.corrections.gds_factor
        c_corr = self.state.corrections.c_factor
        cpar_out *= c_corr
        cl_eff = cl + cpar_out
        gm2_required = k_pm * wu * cl_eff
        i2 = max(gm2_required / max(gm_id_stage2, 1e-12), cl_eff * srn_spec if srn_spec else 0.0)
        p_stage2 = self.gm.forward(gm_id_stage2, l_stage2, i2, "pmos")
        n_out = self.gm.forward(gm_id_stage2, l_stage2, i2, "nmos")
        gm2 = gm_id_stage2 * i2
        srn = i2 / max(cl_eff, 1e-30)
        a1 = gm1 / max((n_in.get("gds", 0.0) + p_load.get("gds", 0.0)) * gds_corr, 1e-15)
        a2 = gm2 / max((p_stage2.get("gds", 0.0) + n_out.get("gds", 0.0)) * gds_corr, 1e-15)
        av_db = 20.0 * math.log10(max(a1 * a2, 1e-30))
        gbw_pred = gm1 / (2.0 * math.pi * max(cc, 1e-30))
        wp2 = gm2 / max(cl_eff, 1e-30)
        pm_pred = 90.0 - math.degrees(math.atan(wu / max(wp2, 1e-30)))
        rz = rz_factor / max(gm2, 1e-30)
        power = vdd * (itail + i2) * (1.0 + self._current_overhead())
        headroom = self._headroom_margin(vdd, n_in, p_load, n_tail, p_stage2, n_out)
        output_swing, output_low, output_high = self._output_swing(vdd, p_stage2, n_out)
        icmr_min, icmr_max = self._input_common_mode_range(vdd, n_in, p_load, n_tail)
        ft_min = min(
            float(n_in.get("ft", 0.0) or n_in.get("ft_approx", 0.0) or 0.0),
            float(p_load.get("ft", 0.0) or p_load.get("ft_approx", 0.0) or 0.0),
            float(p_stage2.get("ft", 0.0) or p_stage2.get("ft_approx", 0.0) or 0.0),
            float(n_out.get("ft", 0.0) or n_out.get("ft_approx", 0.0) or 0.0),
        )
        widths = [
            float(n_in.get("W", 0.0)),
            float(p_load.get("W", 0.0)),
            float(p_stage2.get("W", 0.0)),
            float(n_out.get("W", 0.0)),
            float(n_tail.get("W", 0.0)),
        ]
        max_width = max(widths)
        slacks = self._raw_slacks(
            av_db=av_db,
            gbw_pred=gbw_pred,
            srp=srp,
            srn=srn,
            pm_pred=pm_pred,
            output_swing=output_swing,
            icmr_min=icmr_min,
            icmr_max=icmr_max,
            power=power,
            headroom=headroom,
            ft_min=ft_min,
            max_width=max_width,
        )
        normalized = self._normalized_slacks(slacks)
        score = min(normalized.values()) if normalized else float("-inf")
        return FeasibilityCandidate(
            candidate_id=f"cand_{index:05d}",
            gmID_in=gm_id_in,
            gmID_stage2=gm_id_stage2,
            gmID_load=gm_id_load,
            IC_in=self._ekv_ic_from_gm_id(gm_id_in, "nmos"),
            IC_stage2=self._ekv_ic_from_gm_id(gm_id_stage2, "pmos"),
            L_in=l_in,
            L_load=l_load,
            L_stage2=l_stage2,
            Cc=cc,
            Cc_over_CL=cc / max(cl, 1e-30),
            Rz_factor=rz_factor,
            Rz=rz,
            Itail=itail,
            I2=i2,
            gm1=gm1,
            gm2=gm2,
            Av_pred_dB=av_db,
            GBW_pred_Hz=gbw_pred,
            PM_pred_deg=pm_pred,
            SRp_pred_V_s=srp,
            SRn_pred_V_s=srn,
            output_swing_V=output_swing,
            output_swing_low_V=output_low,
            output_swing_high_V=output_high,
            ICMR_min_V=icmr_min,
            ICMR_max_V=icmr_max,
            ICMR_range_V=max(0.0, icmr_max - icmr_min),
            power_pred_W=power,
            CL_eff=cl_eff,
            Cpar_out=cpar_out,
            parasitic_ratio=cpar_out / max(cl, 1e-30),
            headroom_margin_V=headroom,
            ft_min_Hz=ft_min,
            max_width_m=max_width,
            feasibility_score=score,
            slacks=slacks,
            normalized_slacks=normalized,
        )

    def _choice_space(self) -> dict[str, list[float]]:
        return {
            "gmID_in": self._gm_id_values("input_pair", default=(4.0, 30.0)),
            "gmID_stage2": self._gm_id_values("second_stage_gain", default=(4.0, 25.0)),
            "gmID_load": self._gm_id_values("current_mirror_load", default=(4.0, 25.0)),
            "L_in": self._length_values("input_pair"),
            "L_load": self._length_values("current_mirror_load"),
            "L_stage2": self._length_values("second_stage_gain"),
            "Cc_over_CL": [0.05, 0.075, 0.1, 0.125, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0],
            "Rz_factor": [0.7, 0.85, 1.0, 1.2, 1.5],
        }

    def _deterministic_seed_points(self, choices: dict[str, list[float]]) -> list[tuple[float, ...]]:
        points = []
        gm_pairs = [
            (10.0, 8.0, 7.0),
            (12.0, 8.0, 7.0),
            (15.0, 10.0, 8.0),
            (18.0, 12.0, 8.0),
        ]
        for gm_in, gm2, gm_load in gm_pairs:
            points.append(
                (
                    self._nearest(choices["gmID_in"], gm_in),
                    self._nearest(choices["gmID_stage2"], gm2),
                    self._nearest(choices["gmID_load"], gm_load),
                    choices["L_in"][len(choices["L_in"]) // 2],
                    choices["L_load"][len(choices["L_load"]) // 2],
                    choices["L_stage2"][len(choices["L_stage2"]) // 2],
                    0.2,
                    1.0,
                )
            )
        return points

    def _raw_slacks(
        self,
        *,
        av_db: float,
        gbw_pred: float,
        srp: float,
        srn: float,
        pm_pred: float,
        output_swing: float,
        icmr_min: float,
        icmr_max: float,
        power: float,
        headroom: float,
        ft_min: float,
        max_width: float,
    ) -> dict[str, float | None]:
        pmax = self.spec.get("Pmax_W")
        srp_spec = self.spec.get("SRp_V_s")
        srn_spec = self.spec.get("SRn_V_s")
        swing_min = self.spec.get("output_swing_min_V")
        icmr_min_max = self.spec.get("ICMR_min_max_V")
        icmr_max_min = self.spec.get("ICMR_max_min_V")
        icmr_range_min = self.spec.get("ICMR_range_min_V")
        max_w = getattr(self.state.process, "max_W", 200e-6)
        return {
            "gain_dB": av_db - float(self.spec["Av_dB"]),
            "gbw_log": math.log(max(gbw_pred, 1e-30) / max(float(self.spec["GBW_Hz"]), 1.0)),
            "srp_log": math.log(max(srp, 1e-30) / srp_spec) if srp_spec else None,
            "srn_log": math.log(max(srn, 1e-30) / srn_spec) if srn_spec else None,
            "pm_deg": pm_pred - float(self.spec["PM_deg"]),
            "output_swing_V": output_swing - swing_min if swing_min else None,
            "icmr_min_V": icmr_min_max - icmr_min if icmr_min_max else None,
            "icmr_max_V": icmr_max - icmr_max_min if icmr_max_min else None,
            "icmr_range_V": max(0.0, icmr_max - icmr_min) - icmr_range_min if icmr_range_min else None,
            "power_log": math.log(pmax / max(power, 1e-30)) if pmax else None,
            "headroom_V": headroom,
            "ft_log": math.log(max(ft_min, 1.0) / max(self.config.ft_multiple * float(self.spec["GBW_Hz"]), 1.0)),
            "width_log": math.log(max_w / max(max_width, 1e-30)),
        }

    def _normalized_slacks(self, slacks: dict[str, float | None]) -> dict[str, float]:
        normalizers = {
            "gain_dB": 3.0,
            "gbw_log": math.log(1.2),
            "srp_log": math.log(1.2),
            "srn_log": math.log(1.2),
            "pm_deg": 5.0,
            "output_swing_V": 0.05,
            "icmr_min_V": 0.05,
            "icmr_max_V": 0.05,
            "icmr_range_V": 0.05,
            "power_log": math.log(1.2),
            "headroom_V": 0.05,
            "ft_log": math.log(2.0),
            "width_log": math.log(2.0),
        }
        return {
            key: float(value) / normalizers[key]
            for key, value in slacks.items()
            if value is not None and key in normalizers
        }

    def _classify(self, ranked: list[FeasibilityCandidate]) -> dict[str, Any]:
        if not ranked:
            return {"label": "likely infeasible", "reason": "No valid high-level candidates were generated."}
        best = ranked[0]
        hard_headroom = max(c.slacks.get("headroom_V") or -1e9 for c in ranked[: min(200, len(ranked))]) < -0.05
        if hard_headroom:
            label = "infeasible due to hard physical bounds"
            reason = "All high-ranked candidates violate available voltage headroom by more than 50 mV."
        elif best.feasibility_score >= -1e-9:
            label = "roughly feasible"
            reason = "At least one coarse candidate meets or exceeds every active feasibility indicator."
        elif self._is_near_feasible(best):
            label = "near-feasible"
            reason = "Best coarse candidate has only mild negative slack; ngspice/testbench details may decide the final result."
        else:
            label = "likely infeasible"
            reason = "No coarse candidate approaches all active specs under the current topology and power/headroom assumptions."
        return {
            "label": label,
            "reason": reason,
            "best_score": best.feasibility_score,
            "best_candidate": best.candidate_id,
        }

    def _is_near_feasible(self, candidate: FeasibilityCandidate) -> bool:
        s = candidate.slacks
        return (
            (s.get("gain_dB") is None or s["gain_dB"] >= -3.0)
            and (s.get("pm_deg") is None or s["pm_deg"] >= -5.0)
            and (s.get("gbw_log") is None or s["gbw_log"] >= math.log(1.0 / 1.2))
            and (s.get("power_log") is None or s["power_log"] >= math.log(1.0 / 1.2))
            and (s.get("headroom_V") is None or s["headroom_V"] >= -0.05)
        )

    def _rank_bottlenecks(self, ranked: list[FeasibilityCandidate]) -> list[dict[str, Any]]:
        if not ranked:
            return []
        window = ranked[: min(50, len(ranked))]
        severity: dict[str, float] = {}
        for cand in window:
            for key, value in cand.normalized_slacks.items():
                if value < 0:
                    severity[key] = severity.get(key, 0.0) + abs(value)
        best = ranked[0]
        categories = []
        mapping = {
            "power_log": ("GBW-SR-power / PM-CL-GBW-power", "Power lower bound is tight against required Itail/I2."),
            "gbw_log": ("Av-GBW-speed conflict", "Unity-gain bandwidth is the active sizing boundary."),
            "gain_dB": ("Av-GBW-speed conflict", "Predicted gm/gds gain margin is tight."),
            "pm_deg": ("PM-CL-GBW-power conflict", "Second-pole separation and phase-margin reserve are tight."),
            "headroom_V": ("Headroom-swing conflict", "Estimated Vdsat stack is close to available supply margin."),
            "output_swing_V": ("Headroom-swing conflict", "Output swing is limited by output pull-up/pull-down saturation headroom."),
            "icmr_min_V": ("Headroom-swing conflict", "Input common-mode low end is limited by tail and input-pair headroom."),
            "icmr_max_V": ("Headroom-swing conflict", "Input common-mode high end is limited by input-pair and load headroom."),
            "icmr_range_V": ("Headroom-swing conflict", "Input common-mode range is too narrow under estimated saturation limits."),
            "ft_log": ("Parasitic/speed conflict", "Device ft reserve over target GBW is tight."),
            "width_log": ("Parasitic-dominated conflict", "Estimated widths approach process max width."),
            "srp_log": ("GBW-SR-power conflict", "Positive slew requirement is close to the gm/ID/GBW current boundary."),
            "srn_log": ("PM-CL-GBW-power conflict", "Negative slew requirement raises second-stage current."),
        }
        for key, value in sorted(severity.items(), key=lambda item: item[1], reverse=True):
            cat, reason = mapping.get(key, (key, "Negative slack observed."))
            categories.append({
                "category": cat,
                "indicator": key,
                "severity": value / max(len(window), 1),
                "best_candidate_slack": best.slacks.get(key),
                "reason": reason,
            })
        if not categories:
            for key, value in sorted(best.normalized_slacks.items(), key=lambda item: item[1])[:4]:
                cat, reason = mapping.get(key, (key, "Closest positive slack."))
                categories.append({
                    "category": cat,
                    "indicator": key,
                    "severity": max(0.0, 1.0 / (1.0 + max(value, 0.0))),
                    "best_candidate_slack": best.slacks.get(key),
                    "reason": f"Feasible but closest to the boundary. {reason}",
                })
        if best.parasitic_ratio > 0.2:
            categories.append({
                "category": "Parasitic-dominated conflict",
                "indicator": "parasitic_ratio",
                "severity": best.parasitic_ratio,
                "best_candidate_slack": best.parasitic_ratio,
                "reason": "Estimated output parasitics are a significant fraction of CL_eff.",
            })
        categories.append({
            "category": "Loop-gain measurement risk",
            "indicator": "middlebrook_required",
            "severity": 0.0,
            "best_candidate_slack": None,
            "reason": "Analytical PM is a screening metric only; Middlebrook return-ratio verification is required.",
        })
        return categories[:8]

    def _relaxation_suggestions(
        self,
        ranked: list[FeasibilityCandidate],
        bottlenecks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not ranked:
            return [{"spec": "topology", "suggestion": "No feasible coarse candidates; inspect topology parsing and gm/ID tables."}]
        best = ranked[0]
        suggestions = []
        pmax = self.spec.get("Pmax_W")
        if pmax and (best.slacks.get("power_log") or 0.0) < 0:
            suggestions.append({
                "spec": "power",
                "current": pmax,
                "suggested_min": best.power_pred_W * 1.2,
                "reason": "Add 20% guard band above coarse lower-bound current.",
            })
        if (best.slacks.get("gbw_log") or 0.0) < 0:
            suggestions.append({
                "spec": "GBW",
                "current_Hz": self.spec["GBW_Hz"],
                "suggested_max_Hz": best.GBW_pred_Hz * 0.9,
                "reason": "Best coarse candidate bandwidth is below target.",
            })
        if (best.slacks.get("gain_dB") or 0.0) < 0:
            suggestions.append({
                "spec": "Av",
                "current_dB": self.spec["Av_dB"],
                "suggested_max_dB": best.Av_pred_dB - 1.0,
                "reason": "Best gm/gds product is below target; lower gain or use gain boosting/cascode topology.",
            })
        if (best.slacks.get("pm_deg") or 0.0) < 0:
            suggestions.append({
                "spec": "phase_margin",
                "current_deg": self.spec["PM_deg"],
                "suggested_max_deg": max(35.0, best.PM_pred_deg - 3.0),
                "reason": "Second-stage gm or CL_eff cannot provide enough pole separation.",
            })
        if (best.slacks.get("headroom_V") or 0.0) < 0:
            suggestions.append({
                "spec": "VDD/headroom",
                "current_V": self._vdd(),
                "suggested_min_V": self._vdd() + abs(float(best.slacks["headroom_V"])) + 0.05,
                "reason": "Vdsat stack needs more supply or relaxed swing/headroom.",
            })
        if (best.slacks.get("output_swing_V") or 0.0) < 0:
            suggestions.append({
                "spec": "output_swing",
                "current": self.spec.get("output_swing_min_V"),
                "suggested_max_V": best.output_swing_V,
                "reason": "Output pull-up/pull-down VDSAT leaves less swing than requested.",
            })
        if (best.slacks.get("icmr_min_V") or 0.0) < 0 or (best.slacks.get("icmr_max_V") or 0.0) < 0:
            suggestions.append({
                "spec": "ICMR",
                "suggested_range_V": [best.ICMR_min_V, best.ICMR_max_V],
                "reason": "Input common-mode range is limited by input pair, load, and tail headroom.",
            })
        if not suggestions:
            suggestions.append({
                "spec": "none",
                "suggestion": "No spec relaxation is indicated by the coarse feasibility model; proceed to ngspice validation.",
            })
        return suggestions[:3]

    def _validation_plan(self, top: list[FeasibilityCandidate]) -> list[dict[str, Any]]:
        selected = []
        labels = ("best_overall", "lowest_power_near", "highest_gain_near", "best_pm_margin")
        for label, cand in zip(labels, top[:4]):
            selected.append({
                "label": label,
                "candidate_id": cand.candidate_id,
                "high_level_vars": {
                    "gmID_in": cand.gmID_in,
                    "gmID_stage2": cand.gmID_stage2,
                    "gmID_load": cand.gmID_load,
                    "L_in": cand.L_in,
                    "L_load": cand.L_load,
                    "L_stage2": cand.L_stage2,
                    "Cc": cand.Cc,
                    "Rz": cand.Rz,
                    "Itail": cand.Itail,
                    "I2": cand.I2,
                },
                "ngspice_tests": [
                    ".op operating point and saturation/headroom check",
                    ".ac open-loop gain/UGBW estimate",
                    "Middlebrook return-ratio loop-gain PM with loading-preserving loop break",
                    "Transient large-signal slew-rate test for SR+ and SR-",
                    "Output swing and ICMR sweeps if those specs are present",
                ],
            })
        return selected

    def _select_top_candidates(self, ranked: list[FeasibilityCandidate]) -> list[FeasibilityCandidate]:
        if not ranked:
            return []
        picks: list[FeasibilityCandidate] = []

        def add(cand: FeasibilityCandidate) -> None:
            if all(c.candidate_id != cand.candidate_id for c in picks):
                picks.append(cand)

        add(ranked[0])
        near = [c for c in ranked if self._is_near_feasible(c)]
        if near:
            add(min(near, key=lambda c: c.power_pred_W))
            add(max(near, key=lambda c: c.Av_pred_dB))
            add(max(near, key=lambda c: c.PM_pred_deg))
        for cand in ranked:
            add(cand)
            if len(picks) >= self.config.top_count:
                break
        return picks[: self.config.top_count]

    def _extract_specs(self) -> dict[str, float | None]:
        av = self._target_min("dc_gain", "gain", default=0.0)
        gbw = self._target_min("unity_gain_bandwidth", "ugbw", "GBW", default=1.0)
        pm = self._target_min("phase_margin", "pm", default=60.0)
        sr = self._target_min("slew_rate", "sr", default=None)
        srp = self._target_min("slew_rate_pos", "sr_positive", "srp", default=sr)
        srn = self._target_min("slew_rate_neg", "sr_negative", "srn", default=sr)
        return {
            "Av_dB": av,
            "Av_linear": 10.0 ** (float(av) / 20.0),
            "GBW_Hz": gbw,
            "omega_u_rad_s": 2.0 * math.pi * float(gbw),
            "PM_deg": pm,
            "SRp_V_s": srp,
            "SRn_V_s": srn,
            "Pmax_W": self._target_max("power", "total_power", default=None),
            "CL_F": self.state.simulation.cload,
            "output_swing_min_V": self._target_min("output_swing", "swing", default=None),
            "ICMR_min_max_V": self._target_max("icmr_min", "input_common_mode_min", default=None),
            "ICMR_max_min_V": self._target_min("icmr_max", "input_common_mode_max", default=None),
            "ICMR_range_min_V": self._target_min("icmr", "icmr_range", default=None),
        }

    def _target_min(self, *names: str, default: float | None) -> float | None:
        for name in names:
            target = self.state.targets.get(name)
            if target and target.min is not None:
                return float(target.min)
        return default

    def _target_max(self, *names: str, default: float | None) -> float | None:
        for name in names:
            target = self.state.targets.get(name)
            if target and target.max is not None:
                return float(target.max)
        return default

    def _gm_id_values(self, role: str, default: tuple[float, float]) -> list[float]:
        low, high = self._role_range(role, "gm_id", default)
        process_low = getattr(self.state.process, "gm_id_min", low)
        process_high = getattr(self.state.process, "gm_id_max", high)
        low = max(low, process_low, 4.0)
        high = min(high, process_high, default[1])
        values = [4.0, 5.0, 6.0, 8.0, 10.0, 12.0, 15.0, 18.0, 22.0, 25.0, 30.0]
        clipped = sorted({round(min(max(v, low), high), 6) for v in values if low <= min(max(v, low), high) <= high})
        return clipped or [low, high]

    def _length_values(self, role: str) -> list[float]:
        lmin = getattr(self.state.process, "min_L", 130e-9)
        lmax = getattr(self.state.process, "max_L", 10e-6)
        low, high = self._role_range(role, "L", (lmin, min(10.0 * lmin, lmax)))
        base = [lmin, 2.0 * lmin, 3.0 * lmin, 5.0 * lmin, 10.0 * lmin]
        values = []
        for value in base + [low, high]:
            clipped = min(max(value, low), high)
            if not any(abs(clipped - old) < 1e-15 for old in values):
                values.append(clipped)
        return sorted(values)

    def _role_range(self, role: str, variable: str, default: tuple[float, float]) -> tuple[float, float]:
        device_ids = [dev.id for dev in self.state.topology.devices if dev.role == role]
        for dev_id in device_ids:
            for dv in self.state.design_variables:
                if dv.device == dev_id and dv.variable == variable:
                    return float(dv.range.min), float(dv.range.max)
        return default

    def _clip_global(self, variable: str, value: float) -> float:
        for dv in self.state.design_variables:
            if not dv.device and dv.variable == variable:
                return min(max(value, dv.range.min), dv.range.max)
        return value

    def _current_overhead(self) -> float:
        if self.config.current_overhead is not None:
            return self.config.current_overhead
        bias_count = sum(1 for dev in self.state.topology.devices if "bias" in dev.role or "mirror" in dev.role)
        return min(max(0.10 + 0.03 * bias_count, 0.10), 0.30)

    def _headroom_margin(
        self,
        vdd: float,
        n_in: dict,
        p_load: dict,
        n_tail: dict,
        p_stage2: dict,
        n_out: dict,
    ) -> float:
        margin = self.config.headroom_margin
        stack_input = (
            abs(float(n_tail.get("vdsat", 0.0)))
            + abs(float(n_in.get("vdsat", 0.0)))
            + abs(float(p_load.get("vdsat", 0.0)))
            + margin
        )
        stack_output = abs(float(p_stage2.get("vdsat", 0.0))) + abs(float(n_out.get("vdsat", 0.0))) + margin
        return min(vdd - stack_input, vdd - stack_output)

    def _output_swing(self, vdd: float, p_stage2: dict, n_out: dict) -> tuple[float, float, float]:
        factor = getattr(self.state.process, "VDSAT_headroom_factor", 1.0)
        low = abs(float(n_out.get("vdsat", 0.0))) * factor
        high = vdd - abs(float(p_stage2.get("vdsat", 0.0))) * factor
        return max(0.0, high - low), low, high

    def _input_common_mode_range(
        self,
        vdd: float,
        n_in: dict,
        p_load: dict,
        n_tail: dict,
    ) -> tuple[float, float]:
        factor = getattr(self.state.process, "VDSAT_headroom_factor", 1.0)
        vgs_in = abs(float(n_in.get("vgs", 0.0)))
        vdsat_in = abs(float(n_in.get("vdsat", 0.0)))
        vdsat_tail = abs(float(n_tail.get("vdsat", 0.0)))
        vdsat_load = abs(float(p_load.get("vdsat", 0.0)))
        low = vgs_in + vdsat_tail * factor
        high = vdd - vdsat_load * factor - vdsat_in * factor + vgs_in
        return low, max(low, high)

    def _output_parasitic(self, phys: dict[str, float]) -> float:
        return abs(float(phys.get("cgd", 0.0))) + 0.2 * abs(float(phys.get("cgg", 0.0)))

    def _ekv_ic_from_gm_id(self, gm_id: float, device_type: str) -> float:
        n_sub = (
            getattr(self.state.process, "n_sub_nmos", 1.4)
            if device_type == "nmos"
            else getattr(self.state.process, "n_sub_pmos", 1.4)
        )
        thermal_voltage = 0.02585
        return (1.0 / max(gm_id * n_sub * thermal_voltage, 1e-12)) ** 2

    def _vdd(self) -> float:
        supply = self.state.simulation.supply or {}
        return float(supply.get("vdd", self.state.process.nominal_VDD or 1.2))

    def _nearest(self, values: list[float], target: float) -> float:
        return min(values, key=lambda value: abs(value - target))

    def _write_candidates_csv(self, path: Path, candidates: list[dict[str, Any]]) -> None:
        if not candidates:
            path.write_text("", encoding="utf-8")
            return
        rows = []
        for raw in candidates:
            cand = FeasibilityCandidate(**raw)
            rows.append(cand.to_row())
        keys = list(rows[0].keys())
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)

    def _write_markdown(self, path: Path, report: dict[str, Any]) -> None:
        cls = report["classification"]
        lines = [
            "# Feasibility Report",
            "",
            f"Classification: **{cls['label']}**",
            "",
            cls["reason"],
            "",
            "## Spec",
            "",
            f"- Av: {self.spec['Av_dB']:.3g} dB",
            f"- GBW: {self.spec['GBW_Hz']:.3g} Hz",
            f"- PM: {self.spec['PM_deg']:.3g} deg",
            f"- Pmax: {self.spec.get('Pmax_W') if self.spec.get('Pmax_W') is not None else 'not specified'} W",
            f"- CL: {self.spec['CL_F']:.3g} F",
            "",
            "## Best Candidates",
            "",
            "| id | score | gmID_in | gmID_2 | gmID_load | L_in | L_load | L_2 | Cc | Rz | Itail | I2 | Av(dB) | GBW(MHz) | PM(deg) | SR+/SR-(V/us) | swing(V) | ICMR(V) | P(uW) | headroom(mV) |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for raw in report.get("best_candidates", [])[:8]:
            lines.append(
                "| {candidate_id} | {feasibility_score:.2f} | {gmID_in:.2f} | {gmID_stage2:.2f} | "
                "{gmID_load:.2f} | {L_in:.2e} | {L_load:.2e} | {L_stage2:.2e} | {Cc:.2e} | {Rz:.2e} | "
                "{Itail:.2e} | {I2:.2e} | {Av_pred_dB:.2f} | {gbw_mhz:.2f} | {PM_pred_deg:.2f} | "
                "{srp_vus:.2f}/{srn_vus:.2f} | {output_swing_V:.2f} | {ICMR_min_V:.2f}-{ICMR_max_V:.2f} | "
                "{power_uW:.2f} | {headroom_mV:.1f} |".format(
                    **raw,
                    gbw_mhz=raw["GBW_pred_Hz"] / 1e6,
                    srp_vus=raw["SRp_pred_V_s"] / 1e6,
                    srn_vus=raw["SRn_pred_V_s"] / 1e6,
                    power_uW=raw["power_pred_W"] * 1e6,
                    headroom_mV=raw["headroom_margin_V"] * 1e3,
                )
            )
        lines += ["", "## Bottlenecks", ""]
        for item in report.get("bottleneck_ranking", []):
            lines.append(f"- {item['category']} ({item['indicator']}): {item['reason']}")
        lines += ["", "## Relaxation Suggestions", ""]
        for item in report.get("spec_relaxation_recommendation", []):
            lines.append(f"- {self._format_relaxation(item)}")
        lines += ["", "## Validation Plan", ""]
        for item in report.get("validation_plan", []):
            lines.append(f"- {item['label']} / {item['candidate_id']}: run op, ac, Middlebrook loop gain, transient SR, saturation checks.")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _format_relaxation(self, item: dict[str, Any]) -> str:
        spec = item.get("spec", "unknown")
        if spec == "none":
            return item.get("suggestion", "No relaxation suggested.")
        reason = item.get("reason", "")
        values = []
        for key in ("current", "current_Hz", "current_dB", "current_deg", "current_V"):
            if key in item:
                values.append(f"{key}={item[key]:.3g}" if isinstance(item[key], (int, float)) else f"{key}={item[key]}")
        for key in (
            "suggested_min",
            "suggested_max_Hz",
            "suggested_max_dB",
            "suggested_max_deg",
            "suggested_min_V",
            "suggested_max_V",
            "suggested_range_V",
        ):
            if key in item:
                values.append(f"{key}={item[key]:.3g}" if isinstance(item[key], (int, float)) else f"{key}={item[key]}")
        suffix = f" ({'; '.join(values)})" if values else ""
        return f"{spec}: {reason}{suffix}"

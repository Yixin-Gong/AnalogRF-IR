"""AnalogRF-IR internal documentation."""

from __future__ import annotations

import math
from typing import Optional

from asir.profiles import COMPARATOR_PROFILE, select_circuit_profile
from core.regions import SPICE_OPERATING_REGIONS
from layout.realization import realize_transistor_layout
from schemas.design_state import DesignState, ProcessInfo, TransistorParameters
from core.rule_registry import register_rule, ValidationReport, DiagnosisResult


# ===============================================================
# Internal implementation note.
# ===============================================================

@register_rule("check_min_width", layer=4,
               description="W must be greater than or equal to process.min_W for every transistor.")
def check_min_width(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        W = ts.parameters.W or 0
        if W <= 0:
            continue
        if W < proc.min_W * 0.999:
            report.add(DiagnosisResult(
                check_name="dr:min_width", passed=False, severity="error",
                message=f"{tid}: W={W:.2e}m < min_W={proc.min_W:.2e}m",
                layer=4, device=tid,
                details={"W": W, "min_W": proc.min_W}
            ))
    return report


@register_rule("check_max_width", layer=4,
               description="Wide devices must be realized with legal layout folding.")
def check_max_width(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        W = ts.parameters.W or 0
        if W > proc.max_W:
            layout = realize_transistor_layout(state, tid)
            report.add(DiagnosisResult(
                check_name="dr:max_width", passed=False, severity="info",
                message=f"{tid}: effective W={W*1e6:.1f}um > max_W={proc.max_W*1e6:.1f}um "
                        f"-> realized as {layout.parallel} parallel device groups",
                layer=4, device=tid,
                details={
                    "effective_W": W,
                    "max_W": proc.max_W,
                    "parallel": layout.parallel,
                    "fingers": layout.fingers,
                    "finger_W": layout.finger_W,
                },
            ))
    return report


@register_rule("check_min_length", layer=4,
               description="L must be greater than or equal to process.min_L for every transistor.")
def check_min_length(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        L_val = ts.L_strategy or ts.parameters.L or 0
        if L_val <= 0:
            continue
        if L_val < proc.min_L * 0.999:
            report.add(DiagnosisResult(
                check_name="dr:min_length", passed=False, severity="error",
                message=f"{tid}: L={L_val*1e9:.1f}nm < min_L={proc.min_L*1e9:.1f}nm",
                layer=4, device=tid
            ))
    return report


@register_rule("check_max_length", layer=4,
               description="Long devices must be realized with legal series segments.")
def check_max_length(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        L_val = ts.L_strategy or ts.parameters.L or 0
        if L_val > proc.max_L:
            layout = realize_transistor_layout(state, tid)
            report.add(DiagnosisResult(
                check_name="dr:max_length", passed=False, severity="info",
                message=f"{tid}: effective L={L_val*1e6:.1f}um > max_L={proc.max_L*1e6:.1f}um "
                        f"-> realized as {layout.series} series segments",
                layer=4, device=tid,
                details={
                    "effective_L": L_val,
                    "max_L": proc.max_L,
                    "series": layout.series,
                    "segment_L": layout.segment_L,
                },
            ))
    return report


@register_rule("check_W_precision", layer=4,
               description="W must align to the process width grid.")
def check_W_precision(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        W = ts.parameters.W or 0
        if W <= 0 or proc.W_precision <= 0:
            continue
        steps = W / proc.W_precision
        if abs(steps - round(steps)) > 0.01:
            report.add(DiagnosisResult(
                check_name="dr:W_precision", passed=False, severity="warning",
                message=f"{tid}: W={W*1e6:.4f}um not on {proc.W_precision*1e9:.1f}nm grid",
                layer=4, device=tid
            ))
    return report


@register_rule("check_L_precision", layer=4,
               description="L must align to the process length grid.")
def check_L_precision(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        L_val = ts.L_strategy or ts.parameters.L or 0
        if L_val <= 0 or proc.L_precision <= 0:
            continue
        steps = L_val / proc.L_precision
        if abs(steps - round(steps)) > 0.01:
            report.add(DiagnosisResult(
                check_name="dr:L_precision", passed=False, severity="warning",
                message=f"{tid}: L={L_val*1e9:.2f}nm not on {proc.L_precision*1e9:.1f}nm grid",
                layer=4, device=tid
            ))
    return report


@register_rule("check_W_L_ratio", layer=4,
               description="W/L must stay within process ratio limits.")
def check_W_L_ratio(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        W = ts.parameters.W or 0
        L_val = ts.L_strategy or ts.parameters.L or 1e-15
        if W <= 0 or L_val <= 0:
            continue
        ratio = W / L_val
        if ratio > proc.max_W_L_ratio:
            report.add(DiagnosisResult(
                check_name="dr:W_L_ratio", passed=False, severity="warning",
                message=f"{tid}: W/L={ratio:.1f} > max={proc.max_W_L_ratio:.0f}",
                layer=4, device=tid
            ))
        elif ratio < proc.min_W_L_ratio:
            report.add(DiagnosisResult(
                check_name="dr:W_L_ratio", passed=False, severity="info",
                message=f"{tid}: W/L={ratio:.3f} < min={proc.min_W_L_ratio}",
                layer=4, device=tid
            ))
    return report


# ===============================================================
# Internal implementation note.
# ===============================================================

@register_rule("check_min_area", layer=4,
               description="W*L must exceed the process minimum device area.")
def check_min_area(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        W = ts.parameters.W or 0
        L_val = ts.L_strategy or ts.parameters.L or 0
        area = W * L_val
        if area <= 0:
            continue
        if area < proc.min_area * 0.99:
            report.add(DiagnosisResult(
                check_name="dr:min_area", passed=False, severity="error",
                message=f"{tid}: area={area:.2e}m^2 < min={proc.min_area:.2e}m^2",
                layer=4, device=tid
            ))
    return report


@register_rule("check_finger_width", layer=4,
               description="Wide devices must be split into legal layout fingers.")
def check_finger_width(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        W = ts.parameters.W or 0
        if W > proc.max_finger_width:
            layout = realize_transistor_layout(state, tid)
            report.add(DiagnosisResult(
                check_name="dr:finger_width", passed=False, severity="info",
                message=f"{tid}: W={W*1e6:.1f}um > {proc.max_finger_width*1e6:.0f}um/finger "
                        f"-> realized with {layout.fingers} fingers"
                        f"{' and ' + str(layout.parallel) + ' parallel groups' if layout.parallel > 1 else ''}",
                layer=4,
                device=tid,
                details={
                    "fingers": layout.fingers,
                    "parallel": layout.parallel,
                    "finger_W": layout.finger_W,
                    "instance_W": layout.instance_W,
                    "max_finger_width": proc.max_finger_width,
                },
            ))
    return report


@register_rule("check_layout_realization_bounds", layer=4,
               description="Folded/segmented layout unit devices must stay inside process geometry bounds.")
def check_layout_realization_bounds(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        if (ts.parameters.W or 0.0) <= 0.0 or (ts.parameters.L or ts.L_strategy or 0.0) <= 0.0:
            continue
        layout = realize_transistor_layout(state, tid)
        checks = [
            (
                layout.finger_W > proc.max_finger_width * 1.001,
                "finger_W",
                layout.finger_W,
                proc.max_finger_width,
                "folding failed to keep each finger inside max_finger_width",
            ),
            (
                layout.instance_W > proc.max_W * 1.001,
                "instance_W",
                layout.instance_W,
                proc.max_W,
                "parallel decomposition failed to keep each instance inside max_W",
            ),
            (
                layout.segment_L > proc.max_L * 1.001,
                "segment_L",
                layout.segment_L,
                proc.max_L,
                "series segmentation failed to keep each segment inside max_L",
            ),
        ]
        for failed, field, value, limit, reason in checks:
            if failed:
                report.add(DiagnosisResult(
                    check_name="dr:layout_realization_bounds",
                    passed=False,
                    severity="error",
                    message=f"{tid}: {reason}: {field}={value:.3e} > limit={limit:.3e}",
                    layer=4,
                    device=tid,
                    details={
                        "field": field,
                        "value": value,
                        "limit": limit,
                        "fingers": layout.fingers,
                        "parallel": layout.parallel,
                        "series": layout.series,
                    },
                ))
    return report


# ===============================================================
# Internal implementation note.
# ===============================================================

PAIR_TOLERANCE = 0.05  # Internal implementation note.


@register_rule("check_pair_W_mismatch", layer=4,
               description="Matched-pair W mismatch should stay below 5%.")
def check_pair_W_mismatch(state: DesignState) -> ValidationReport:
    return _check_pair_param(state, "W", PAIR_TOLERANCE, "W", lambda p: p.W)


@register_rule("check_pair_L_mismatch", layer=4,
               description="Matched-pair L mismatch should stay below 5%.")
def check_pair_L_mismatch(state: DesignState) -> ValidationReport:
    return _check_pair_param(state, "L", PAIR_TOLERANCE, "L",
                              lambda p, ts: ts.L_strategy or p.L)


@register_rule("check_pair_gm_mismatch", layer=4,
               description="Matched-pair gm mismatch should stay below 5%.")
def check_pair_gm_mismatch(state: DesignState) -> ValidationReport:
    return _check_pair_param(state, "gm", PAIR_TOLERANCE, "gm", lambda p: p.gm)


def _check_pair_param(state: DesignState, param_name: str, tolerance: float,
                       label: str, getter) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    checked: set[tuple[str, str]] = set()
    role_groups = {}
    for dev in state.topology.devices:
        role_groups.setdefault(dev.role, []).append(dev.id)
    label_groups: dict[str, list[str]] = {}
    for dv in state.design_variables:
        if dv.symmetry_label and dv.device:
            label_groups.setdefault(dv.symmetry_label, []).append(dv.device)

    all_groups = {**role_groups, **{k: sorted(set(v)) for k, v in label_groups.items()}}
    for role, dev_ids in all_groups.items():
        if len(dev_ids) < 2:
            continue
        for i in range(len(dev_ids)):
            for j in range(i + 1, len(dev_ids)):
                a, b = dev_ids[i], dev_ids[j]
                pair_key = (a, b)
                if pair_key in checked:
                    continue
                checked.add(pair_key)
                if a not in state.transistors or b not in state.transistors:
                    continue
                pa = state.transistors[a].parameters
                pb = state.transistors[b].parameters
                tsa = state.transistors[a]
                tsb = state.transistors[b]
                va = getter(pa, tsa) if "L" in param_name else getter(pa)
                vb = getter(pb, tsb) if "L" in param_name else getter(pb)
                if va <= 0 or vb <= 0:
                    continue
                dev = abs(va - vb) / max(va, vb)
                if dev > tolerance:
                    severity = "error" if param_name in {"W", "L"} else "warning"
                    report.add(DiagnosisResult(
                        check_name=f"dr:pair_{param_name}_mismatch",
                        passed=False, severity=severity,
                        message=f"{a}/{b} ({role}) {label}: {va:.4e} vs {vb:.4e} "
                                f"(deviation={dev*100:.1f}%)",
                        layer=4, device=f"{a}/{b}",
                        details={"deviation": dev}
                    ))
    return report


@register_rule("check_current_mirror_ratio", layer=4,
               description="Current-mirror width ratios should stay inside the supported range.")
def check_current_mirror_ratio(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    mirror_groups = {}
    mirror_roles = {
        "current_mirror_load",
        "tail_current_source",
        "tail_bias_mirror",
        "output_current_source",
        "output_bias_mirror",
        "second_stage_load",
    }
    for dev in state.topology.devices:
        if dev.role in mirror_roles or "mirror" in dev.role.lower():
            gate_net = dev.connections.get("gate", "")
            mirror_groups.setdefault(gate_net, []).append(dev.id)
    for gate_net, dev_ids in mirror_groups.items():
        if len(dev_ids) < 2:
            continue
        # Internal implementation note.
        widths = []
        for did in dev_ids:
            ts = state.transistors.get(did)
            if ts and ts.parameters.W > 0:
                widths.append((did, ts.parameters.W))
        if len(widths) < 2:
            continue
        _, ref_w = widths[0]
        for did, W in widths[1:]:
            ratio = W / ref_w if ref_w > 0 else 0
            if ratio > 10 or ratio < 0.1:
                report.add(DiagnosisResult(
                    check_name="dr:mirror_ratio", passed=False, severity="info",
                    message=f"Current mirror {did}/{widths[0][0]}: W ratio={ratio:.1f} "
                            f"(extreme, check intentional)",
                    layer=4, device=did
                ))
    return report


# ===============================================================
# Internal implementation note.
# ===============================================================

@register_rule("check_saturation_margin", layer=4,
               description="Active devices should satisfy VDS >= VDSAT times the headroom factor.")
def check_saturation_margin(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    proc = state.process
    factor = proc.VDSAT_headroom_factor
    for tid, ts in state.transistors.items():
        p = ts.parameters
        if p.region == "unknown" or p.vdsat <= 0:
            continue
        dev_def = state.get_device_def(tid)
        role = dev_def.role if dev_def else ""
        if _is_dynamic_comparator_role(state, role):
            continue
        if p.vds < p.vdsat * factor:
            report.add(DiagnosisResult(
                check_name="dr:saturation_margin", passed=False, severity="warning",
                message=f"{tid} ({role}): vds={p.vds:.3f}V < {factor}*vdsat={p.vdsat*factor:.3f}V - "
                        f"not in saturation",
                layer=4, device=tid,
                details={"vds": p.vds, "vdsat": p.vdsat, "factor": factor}
            ))
    return report


@register_rule("check_region_validity", layer=4,
               description="SPICE operating-region labels must be valid and non-exempt devices should saturate.")
def check_region_validity(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    SATURATION_OK = {"saturation", "subthreshold"}
    for tid, ts in state.transistors.items():
        p = ts.parameters
        dev_def = state.get_device_def(tid)
        role = dev_def.role if dev_def else ""
        if _is_dynamic_comparator_role(state, role):
            continue

        # Internal implementation note.
        if p.region not in SPICE_OPERATING_REGIONS:
            report.add(DiagnosisResult(
                check_name="dr:region_valid", passed=False, severity="error",
                message=f"{tid}: region='{p.region}' is not a valid SPICE operating region",
                layer=4, device=tid
            ))
            continue

        # Internal implementation note.
        if p.region == "unknown":
            report.add(DiagnosisResult(
                check_name="dr:region_valid", passed=False, severity="warning",
                message=f"{tid}: region='unknown' - simulation may not have properly "
                        f"back-filled operating region",
                layer=4, device=tid
            ))
            continue

        if p.region not in SATURATION_OK:
            report.add(DiagnosisResult(
                check_name="dr:region_saturation", passed=False,
                severity="warning" if p.region == "linear" else "error",
                message=f"{tid} ({role}): region='{p.region}' - expected saturation, "
                        f"vds={p.vds:.3f}V, vdsat={p.vdsat:.3f}V",
                layer=4, device=tid,
                details={"region": p.region, "vds": p.vds, "vdsat": p.vdsat}
            ))

    return report


# Internal implementation note.

# Internal implementation note.
_SATURATION_DEPTH_MARGIN: dict = {
    "input_pair": 0.08,
    "cascode": 0.20,
    "tail_current_source": 0.05,
    "tail_bias_mirror": 0.03,
    "current_mirror_load": 0.03,
    "second_stage_gain": 0.05,
    "second_stage_load": 0.05,
    "output_current_source": 0.05,
    "output_bias_mirror": 0.03,
}
_DEFAULT_DEPTH_MARGIN = 0.05  # Internal implementation note.


@register_rule("check_saturation_depth", layer=4,
               description="Role-dependent VDS-VDSAT saturation depth should meet policy margins.")
def check_saturation_depth(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    for tid, ts in state.transistors.items():
        p = ts.parameters
        if p.vdsat <= 0 or p.region == "unknown":
            continue
        dev_def = state.get_device_def(tid)
        role = dev_def.role if dev_def else ""
        if _is_dynamic_comparator_role(state, role):
            continue
        margin_v = p.vds - p.vdsat
        required = _SATURATION_DEPTH_MARGIN.get(role, _DEFAULT_DEPTH_MARGIN)
        if required <= 0:
            continue

        if margin_v < required:
            report.add(DiagnosisResult(
                check_name="dr:saturation_depth", passed=False,
                severity="warning",
                message=f"{tid} ({role}): VDS-VDSAT={margin_v*1e3:.0f}mV "
                        f"< required {required*1e3:.0f}mV - marginal saturation",
                layer=4, device=tid,
                details={"vds": p.vds, "vdsat": p.vdsat,
                         "margin": margin_v, "required": required}
            ))
    return report


# Internal implementation note.

# Internal implementation note.
# Internal implementation note.
# Internal implementation note.
# Internal implementation note.
# Internal implementation note.
_INVERSION_EXPECTATION: dict = {
    "input_pair":         (0.3, 8.0,   "moderate inversion for gain-bandwidth efficiency"),
    "current_mirror_load": (2.0, 50.0, "moderate-to-strong inversion for mirror accuracy"),
    "tail_current_source": (5.0, 100.0, "strong inversion for stable bias current and high output resistance"),
    "tail_bias_mirror": (5.0, 100.0, "strong inversion for stable bias reference current"),
    "second_stage_gain": (2.0, 80.0, "moderate-to-strong inversion for gain and output drive"),
    "second_stage_load": (5.0, 100.0, "strong inversion for output current source behavior"),
    "output_current_source": (5.0, 100.0, "strong inversion for output current source behavior"),
    "output_bias_mirror": (5.0, 100.0, "strong inversion for output bias reference current"),
    "cascode":            (5.0, 100.0, "strong inversion for high intrinsic gain"),
}


@register_rule("check_inversion_region", layer=4,
               description="Device inversion coefficient should match the expected role-dependent range.")
def check_inversion_region(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    try:
        from core.inversion import InversionAnalyzer
    except ImportError:
        report.add(DiagnosisResult(
            check_name="dr:inversion_region", passed=False, severity="error",
            message="core.inversion module not available - cannot compute IC",
            layer=4
        ))
        return report

    proc = state.process
    analyzer = InversionAnalyzer()

    for tid, ts in state.transistors.items():
        dev_def = state.get_device_def(tid)
        role = dev_def.role if dev_def else ""
        expectation = _INVERSION_EXPECTATION.get(role)
        if expectation is None:
            continue  # Internal implementation note.

        ic_min, ic_max, desc = expectation
        result = analyzer.analyze_transistor(ts, proc)
        ic = result.ic

        if ic <= 0:
            report.add(DiagnosisResult(
                check_name="dr:inversion_region", passed=False, severity="warning",
                message=f"{tid} ({role}): IC not computable (no gm_id or id data)",
                layer=4, device=tid
            ))
            continue

        if ic < ic_min:
            report.add(DiagnosisResult(
                check_name="dr:inversion_region", passed=False,
                severity="warning",
                message=f"{tid} ({role}): IC={ic:.3f} < min={ic_min} - "
                        f"too weak (expected: {desc})",
                layer=4, device=tid,
                details={"ic": ic, "ic_min": ic_min, "ic_max": ic_max,
                         "gm_id": result.gm_id, "region": result.region}
            ))
        elif ic > ic_max:
            report.add(DiagnosisResult(
                check_name="dr:inversion_region", passed=False,
                severity="warning",
                message=f"{tid} ({role}): IC={ic:.3f} > max={ic_max} - "
                        f"too strong (expected: {desc})",
                layer=4, device=tid,
                details={"ic": ic, "ic_min": ic_min, "ic_max": ic_max,
                         "gm_id": result.gm_id, "region": result.region}
            ))

    return report


# Internal implementation note.

_IC_CONSISTENCY_TOLERANCE = 0.30  # Internal implementation note.


# Internal implementation note.


@register_rule("diagnose_saturation_failure", layer=4,
               description="Diagnose likely causes when device saturation headroom is weak.")
def diagnose_saturation_failure(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    proc = state.process

    # Internal implementation note.
    vdd = state.simulation.supply.get("vdd", proc.nominal_VDD)
    vss = state.simulation.supply.get("vss", 0.0)
    span = vdd - vss
    if span <= 0:
        return report

    # Internal implementation note.
    devices_info = {}
    for tid, ts in state.transistors.items():
        p = ts.parameters
        dev_def = state.get_device_def(tid)
        role = dev_def.role if dev_def else ""
        devices_info[tid] = {
            "vds": p.vds, "vdsat": p.vdsat, "region": p.region,
            "role": role, "type": dev_def.type if dev_def else "nmos",
            "margin": p.vds - p.vdsat,
        }

    # Internal implementation note.
    for tid, info in devices_info.items():
        if info["region"] == "unknown" or info["vdsat"] <= 0:
            continue
        role = info["role"]
        if _is_dynamic_comparator_role(state, role):
            continue
        margin = info["margin"]

        # Internal implementation note.
        required = _SATURATION_DEPTH_MARGIN.get(role, _DEFAULT_DEPTH_MARGIN)
        if margin >= required:
            continue  # Internal implementation note.

        # Internal implementation note.
        causes = []

        # Internal implementation note.
        vds_ratio = info["vds"] / span if span > 0 else 0

        if role == "tail_current_source":
            if info["vdsat"] > 0.25:
                causes.append("high VDSAT; gm/ID may be too strong or L may be too short")
            if vds_ratio < 0.15:
                causes.append(f"VDS is only {vds_ratio*100:.0f}% of VDD; "
                              "tail headroom is tight and input-pair VGS may be too large")

        elif role == "input_pair":
            if info["vdsat"] > 0.35:
                causes.append("high VDSAT; consider increasing gm/ID or L")
            if vds_ratio < 0.20:
                causes.append(f"VDS is only {vds_ratio*100:.0f}% of VDD; "
                              "load or tail devices consume too much headroom")

        elif role == "cascode":
            if info["vdsat"] > 0.3:
                causes.append("high VDSAT; cascode devices usually need longer L")
            causes.append("insufficient cascode VDS; check bias voltage and device ratios")

        else:
            if info["vdsat"] > 0.3:
                causes.append("high VDSAT; adjust gm/ID, current, or L")
            if vds_ratio < 0.10:
                causes.append(f"VDS is only {vds_ratio*100:.0f}% of VDD; "
                              "voltage allocation is highly unbalanced")

        if not causes:
            causes.append("insufficient VDS margin; check supply, stack allocation, and gm/ID strategy")

        report.add(DiagnosisResult(
            check_name="dr:diagnose_saturation", passed=True, severity="info",
            message=f"{tid} ({role}): VDS-VDSAT={margin*1e3:.0f}mV "
                    f"(required {required*1e3:.0f}mV) -> {'; '.join(causes)}",
            layer=4, device=tid,
            details={"vds": info["vds"], "vdsat": info["vdsat"],
                     "margin": margin, "vds_ratio": vds_ratio,
                     "causes": causes}
        ))

    # Internal implementation note.
    total_vds = sum(info["vds"] for info in devices_info.values() if info["vds"] > 0)
    active_count = sum(1 for info in devices_info.values() if info["vds"] > 0)
    if active_count > 0:
        avg_vds = total_vds / active_count
        if avg_vds < 0.15:
            report.add(DiagnosisResult(
                check_name="dr:diagnose_headroom", passed=True, severity="info",
                message=f"Average VDS/device = {avg_vds*1e3:.0f}mV (VDD={span:.2f}V) - "
                        "stacked path is crowded; consider reducing stack depth or increasing VDD",
                layer=4
            ))

    return report


@register_rule("check_inversion_consistency", layer=4,
               description="Devices with the same role should have consistent inversion coefficients.")
def check_inversion_consistency(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    try:
        from core.inversion import InversionAnalyzer
    except ImportError:
        return report

    proc = state.process
    analyzer = InversionAnalyzer()

    # Internal implementation note.
    role_groups: dict = {}
    for dev in state.topology.devices:
        role_groups.setdefault(dev.role, []).append(dev.id)

    for role, dev_ids in role_groups.items():
        if len(dev_ids) < 2:
            continue

        # Internal implementation note.
        ic_map = {}
        for did in dev_ids:
            ts = state.transistors.get(did)
            if ts is None:
                continue
            result = analyzer.analyze_transistor(ts, proc)
            if result.ic > 0:
                ic_map[did] = result.ic

        if len(ic_map) < 2:
            continue

        # Internal implementation note.
        dev_list = list(ic_map.keys())
        for i in range(len(dev_list)):
            for j in range(i + 1, len(dev_list)):
                a, b = dev_list[i], dev_list[j]
                ica, icb = ic_map[a], ic_map[b]
                if ica <= 0 or icb <= 0:
                    continue
                dev_pct = abs(ica - icb) / max(ica, icb)
                if dev_pct > _IC_CONSISTENCY_TOLERANCE:
                    report.add(DiagnosisResult(
                        check_name="dr:inversion_consistency", passed=False,
                        severity="warning",
                        message=f"{a}/{b} ({role}): IC={ica:.3f} vs {icb:.3f} "
                                f"(deviation={dev_pct*100:.1f}%)",
                        layer=4, device=f"{a}/{b}",
                        details={"ic_a": ica, "ic_b": icb, "deviation": dev_pct}
                    ))

    return report


@register_rule("check_VGS_breakdown", layer=4,
               description="Gate-source voltage must remain below the process reliability limit.")
def check_VGS_breakdown(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        vgs = ts.parameters.vgs
        if vgs <= 0:
            continue
        if abs(vgs) > proc.max_VGS * 0.95:
            report.add(DiagnosisResult(
                check_name="dr:VGS_breakdown", passed=False, severity="error",
                message=f"{tid}: |VGS|={abs(vgs):.3f}V >= 0.95*max_VGS={proc.max_VGS*0.95:.3f}V",
                layer=4, device=tid
            ))
    return report


@register_rule("check_VDS_reliability", layer=4,
               description="Drain-source voltage must remain inside the safe reliability range.")
def check_VDS_reliability(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    for tid, ts in state.transistors.items():
        vds = ts.parameters.vds
        if vds <= 0:
            continue
        if abs(vds) > proc.max_VDS - proc.VDS_safe_margin:
            report.add(DiagnosisResult(
                check_name="dr:VDS_reliability", passed=False, severity="warning",
                message=f"{tid}: VDS={abs(vds):.3f}V exceeds safe limit "
                        f"({proc.max_VDS - proc.VDS_safe_margin:.3f}V)",
                layer=4, device=tid
            ))
    return report


@register_rule("check_VGS_safe_range", layer=4,
               description="VGS should stay in a practical operating range around threshold.")
def check_VGS_safe_range(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    for tid, ts in state.transistors.items():
        vgs = ts.parameters.vgs
        if vgs <= 0:
            continue
        dev_def = state.get_device_def(tid)
        dev_type = dev_def.type if dev_def else "nmos"
        role = dev_def.role if dev_def else ""
        if _is_dynamic_comparator_role(state, role):
            continue
        VTH = state.process.VTH_n if dev_type == "nmos" else state.process.VTH_p
        vov = vgs - VTH
        if vov < 0.05:
            report.add(DiagnosisResult(
                check_name="dr:VGS_safe", passed=False, severity="info",
                message=f"{tid}: vov={vov*1e3:.1f}mV - deep subthreshold, noise may be high",
                layer=4, device=tid
            ))
        elif vov > 0.8:
            report.add(DiagnosisResult(
                check_name="dr:VGS_safe", passed=False, severity="info",
                message=f"{tid}: vov={vov:.2f}V - strong overdrive, check reliability",
                layer=4, device=tid
            ))
    return report


@register_rule("check_headroom", layer=4,
               description="Estimated stacked VDS demand must fit inside the supply range.")
def check_headroom(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    vdd = state.simulation.supply.get("vdd", 1.8)
    vss = state.simulation.supply.get("vss", 0.0)
    span = vdd - vss
    for dev in state.topology.devices:
        ts = state.transistors.get(dev.id)
        if ts and ts.parameters.vds > span * 0.85:
            report.add(DiagnosisResult(
                check_name="dr:headroom", passed=False, severity="warning",
                message=f"{dev.id}: VDS={ts.parameters.vds:.3f}V > 85% span ({span}V)",
                layer=4, device=dev.id
            ))
    return report


# ===============================================================
# Internal implementation note.
# ===============================================================

@register_rule("check_hard_targets", layer=4,
               description="All priority-1 targets must pass when measurements are available.")
def check_hard_targets(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    # Internal implementation note.
    perf = {}
    if state.history:
        perf = state.history[-1].final_performance
    if not perf:
        return report  # Internal implementation note.

    for name, t in state.targets.items():
        if t.priority != 1:
            continue
        realized = perf.get(name)
        if realized is None:
            continue
        if t.min is not None and realized < t.min:
            report.add(DiagnosisResult(
                check_name="dr:hard_target", passed=False, severity="error",
                message=f"Target '{name}': {realized:.3g} < min={t.min}",
                layer=4
            ))
        if t.max is not None and realized > t.max:
            report.add(DiagnosisResult(
                check_name="dr:hard_target", passed=False, severity="error",
                message=f"Target '{name}': {realized:.3g} > max={t.max}",
                layer=4
            ))
    return report


@register_rule("check_all_targets", layer=4,
               description="Check all targets, including lower-priority specifications.")
def check_all_targets(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    perf = {}
    if state.history:
        perf = state.history[-1].final_performance
    if not perf:
        return report
    for name, t in state.targets.items():
        realized = perf.get(name)
        if realized is None:
            continue
        if t.min is not None and realized < t.min:
            report.add(DiagnosisResult(
                check_name="dr:target", passed=False,
                severity="warning" if t.priority > 1 else "error",
                message=f"Target '{name}' (P{t.priority}): {realized:.3g} < min={t.min}",
                layer=4
            ))
        if t.max is not None and realized > t.max:
            report.add(DiagnosisResult(
                check_name="dr:target", passed=False,
                severity="warning" if t.priority > 1 else "error",
                message=f"Target '{name}' (P{t.priority}): {realized:.3g} > max={t.max}",
                layer=4
            ))
    return report


# ===============================================================
# Internal implementation note.
# ===============================================================

@register_rule("check_gm_id_physical_range", layer=4,
               description="gm/ID strategy values must stay inside process-level feasible bounds.")
def check_gm_id_physical_range(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    for dv in state.design_variables:
        if dv.variable != "gm_id":
            continue
        if dv.range.min < proc.gm_id_min or dv.range.max > proc.gm_id_max:
            report.add(DiagnosisResult(
                check_name="dr:gm_id_range", passed=False, severity="warning",
                message=f"{dv.device}.gm_id range [{dv.range.min:.1f}, {dv.range.max:.1f}] "
                        f"exceeds physical range [{proc.gm_id_min:.1f}, {proc.gm_id_max:.1f}]",
                layer=4, device=dv.device
            ))
    return report


@register_rule("check_L_step_resolution", layer=4,
               description="Optimizer L search ranges should be wide enough relative to process precision.")
def check_L_step_resolution(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    proc = state.process
    for dv in state.design_variables:
        if dv.variable != "L":
            continue
        span = dv.range.max - dv.range.min
        # Internal implementation note.
        if span < proc.L_precision * 10:
            report.add(DiagnosisResult(
                check_name="dr:L_resolution", passed=False, severity="info",
                message=f"{dv.device}.L range [{dv.range.min*1e9:.0f}, {dv.range.max*1e9:.0f}]nm "
                        f"span={span*1e9:.1f}nm < 10*L_precision={proc.L_precision*10*1e9:.1f}nm",
                layer=4, device=dv.device
            ))
    return report


@register_rule("check_symmetry_in_design_vars", layer=4,
               description="Symmetric design variables must use identical ranges.")
def check_symmetry_in_design_vars(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    groups: dict = {}
    for dv in state.design_variables:
        if dv.symmetry_label:
            key = (dv.symmetry_label, dv.variable)
            groups.setdefault(key, []).append(dv)
    for (label, var_name), dvs in groups.items():
        if len(dvs) < 2:
            continue
        ref = dvs[0]
        for dv in dvs[1:]:
            if (abs(dv.range.min - ref.range.min) > 1e-15 or
                abs(dv.range.max - ref.range.max) > 1e-15):
                report.add(DiagnosisResult(
                    check_name="dr:design_var_symmetry", passed=False, severity="error",
                    message=f"Symmetry group '{label}' {var_name}: "
                            f"{dv.device}.{dv.variable} range != {ref.device}.{ref.variable} range",
                    layer=4, device=dv.device
                ))
            if dv.initial is not None and ref.initial is not None and abs(float(dv.initial) - float(ref.initial)) > 1e-18:
                report.add(DiagnosisResult(
                    check_name="dr:design_var_symmetry", passed=False, severity="error",
                    message=f"Symmetry group '{label}' {var_name}: "
                            f"{dv.device}.{dv.variable} initial != {ref.device}.{ref.variable} initial",
                    layer=4, device=dv.device
                ))
    return report


# ===============================================================
# Internal implementation note.
# ===============================================================

@register_rule("check_temperature_range", layer=4,
               description="Simulation temperature should stay inside the IC operating range.")
def check_temperature_range(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    t = state.simulation.temperature
    if t < -40 or t > 125:
        report.add(DiagnosisResult(
            check_name="dr:temperature", passed=False, severity="warning",
            message=f"Simulation temperature {t} degC outside standard IC range (-40~125 degC)",
            layer=4
        ))
    return report


@register_rule("check_supply_valid", layer=4,
               description="The configured supply voltage must fit the process supply domain.")
def check_supply_valid(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    proc = state.process
    vdd = state.simulation.supply.get("vdd", proc.nominal_VDD)
    if vdd < proc.VDD_min * 0.95 or vdd > proc.VDD_max * 1.05:
        report.add(DiagnosisResult(
            check_name="dr:supply", passed=False, severity="warning",
            message=f"VDD={vdd:.2f}V outside process domain [{proc.VDD_min:.2f}, {proc.VDD_max:.2f}]V",
            layer=4
        ))
    return report


@register_rule("check_power_density", layer=4,
               description="Estimated power density should remain below a practical warning threshold.")
def check_power_density(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    total_area = 0.0
    for ts in state.transistors.values():
        W = ts.parameters.W or 0
        L_val = ts.L_strategy or ts.parameters.L or 0
        total_area += W * L_val
    # Internal implementation note.
    total_power = 0.0
    for ts in state.transistors.values():
        p = ts.parameters
        if p.id > 0 and p.vds > 0:
            total_power += p.id * p.vds
    if total_area > 0 and total_power > 0:
        density = total_power / total_area  # W/m^2
        if density > 1e6:  # Internal implementation note.
            report.add(DiagnosisResult(
                check_name="dr:power_density", passed=False, severity="info",
                message=f"Power density={density/1e4:.1f}W/mm^2 - high, consider reliability",
                layer=4
            ))
    return report


@register_rule("check_process_config", layer=4,
               description="Process configuration must include required electrical and geometry limits.")
def check_process_config(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    report = ValidationReport()
    proc = state.process
    if not proc.process_name:
        report.add(DiagnosisResult(
            check_name="dr:process_name", passed=False, severity="warning",
            message="Process name not configured", layer=4
        ))
    if proc.min_W <= 0 or proc.min_L <= 0:
        report.add(DiagnosisResult(
            check_name="dr:process_drc", passed=False, severity="error",
            message="Process min_W or min_L not set", layer=4
        ))
    if proc.max_VGS <= 0:
        report.add(DiagnosisResult(
            check_name="dr:process_reliability", passed=False, severity="error",
            message="Process max_VGS not set", layer=4
        ))
    if proc.VTH_n <= 0:
        report.add(DiagnosisResult(
            check_name="dr:process_physics", passed=False, severity="warning",
            message="VTH_n not configured", layer=4
        ))
    return report


def _is_comparator_state(state: DesignState) -> bool:
    return select_circuit_profile(state).name == COMPARATOR_PROFILE.name


def _is_dynamic_comparator_role(state: DesignState, role: str) -> bool:
    return select_circuit_profile(state).is_dynamic_role(role)


def _global_design_names(state: DesignState) -> set[str]:
    names = set((state.global_parameters or {}).keys())
    names.update(dv.variable for dv in state.design_variables if not dv.device)
    return names


@register_rule(
    "check_comparator_metric_coverage",
    layer=3,
    description="Comparator schemas should declare the standard dynamic-comparator metric set.",
    circuit_profiles=("comparator",),
)
def check_comparator_metric_coverage(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    profile = select_circuit_profile(state)
    missing = profile.missing_metric_groups(state.targets)
    if missing:
        report.add(DiagnosisResult(
            check_name="dr:comparator_metric_coverage",
            passed=False,
            severity="warning",
            message="Comparator target set is missing recommended metrics: " + ", ".join(missing),
            layer=3,
            details={"missing": missing},
        ))
    return report


@register_rule(
    "check_comparator_dynamic_context",
    layer=3,
    description="Dynamic comparator metrics need load, clock, and input-step context.",
    circuit_profiles=("comparator",),
)
def check_comparator_dynamic_context(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    profile = select_circuit_profile(state)
    names = _global_design_names(state)
    required = set(profile.required_context)
    missing = sorted(required - names)
    if missing:
        report.add(DiagnosisResult(
            check_name="dr:comparator_dynamic_context",
            passed=False,
            severity="warning",
            message="Comparator dynamic estimates need global parameters: " + ", ".join(missing),
            layer=3,
            details={"missing": missing},
        ))
    return report


@register_rule(
    "check_comparator_symmetry_labels",
    layer=4,
    description="Strongly matched comparator branches should share symmetry labels.",
    circuit_profiles=("comparator",),
)
def check_comparator_symmetry_labels(state: DesignState) -> ValidationReport:
    report = ValidationReport()
    role_groups: dict[str, list[str]] = {
        "input_pair": [],
        "latch_nmos": [],
        "latch_pmos": [],
        "reset_precharge": [],
    }
    for dev in state.topology.devices:
        role = dev.role.lower()
        if "input_pair" in role:
            role_groups["input_pair"].append(dev.id)
        elif "latch_nmos" in role:
            role_groups["latch_nmos"].append(dev.id)
        elif "latch_pmos" in role:
            role_groups["latch_pmos"].append(dev.id)
        elif "reset_precharge" in role:
            role_groups["reset_precharge"].append(dev.id)

    labels: dict[tuple[str, str], str] = {}
    for dv in state.design_variables:
        if dv.device and dv.symmetry_label:
            labels[(dv.device, dv.variable)] = dv.symmetry_label

    for group_name, dev_ids in role_groups.items():
        if len(dev_ids) < 2:
            continue
        for variable in ("gm_id", "L"):
            group_labels = {labels.get((dev_id, variable), "") for dev_id in dev_ids}
            if "" in group_labels or len(group_labels) > 1:
                report.add(DiagnosisResult(
                    check_name="dr:comparator_symmetry_labels",
                    passed=False,
                    severity="warning",
                    message=f"{group_name} {variable} variables should share one symmetry label",
                    layer=4,
                    device="/".join(dev_ids),
                    details={"group": group_name, "variable": variable, "labels": sorted(group_labels)},
                ))
    return report


# ===============================================================
# Internal implementation note.
# ===============================================================

ALL_RULES = [
    # Internal implementation note.
    check_min_width, check_max_width, check_min_length, check_max_length,
    check_W_precision, check_L_precision, check_W_L_ratio,
    # Internal implementation note.
    check_min_area, check_finger_width, check_layout_realization_bounds,
    # Internal implementation note.
    check_pair_W_mismatch, check_pair_L_mismatch, check_pair_gm_mismatch,
    check_current_mirror_ratio,
    # Internal implementation note.
    check_region_validity, check_saturation_margin,
    check_saturation_depth,
    check_VGS_breakdown, check_VDS_reliability,
    check_VGS_safe_range, check_headroom,
    # Internal implementation note.
    check_inversion_region, check_inversion_consistency,
    # Internal implementation note.
    diagnose_saturation_failure,
    # Internal implementation note.
    check_hard_targets, check_all_targets,
    # Internal implementation note.
    check_gm_id_physical_range, check_L_step_resolution,
    check_symmetry_in_design_vars,
    # Internal implementation note.
    check_temperature_range, check_supply_valid, check_power_density,
    check_process_config,
    # Internal implementation note.
    check_comparator_metric_coverage, check_comparator_dynamic_context,
    check_comparator_symmetry_labels,
]

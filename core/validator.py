"""AnalogRF-IR internal documentation."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Any, Set, Tuple

from schemas.design_state import (
    DesignState, TransistorState, TransistorParameters,
    DeviceDefinition, Target, Range, DesignVariable, LossTerm,
    Constraints, DeviceConstraint, Topology, SimulationConfig, GlobalNet,
)
from asir.profiles import CircuitProfile, select_circuit_profile
from util.units import Unit, Dimension, Length, Voltage, Current

# Internal implementation note.
import core.design_rules  # noqa: F401

# Internal implementation note.
from core.rule_registry import (
    register_rule, get_rule, list_rules, run_registered_rules,
    DiagnosisResult, ValidationReport,
)
# Internal implementation note.

class SyntaxValidator:
    """AnalogRF-IR internal documentation."""

    @staticmethod
    def validate(state: DesignState) -> ValidationReport:
        report = ValidationReport()

        # Internal implementation note.
        if not state.design_name or state.design_name == "unnamed":
            report.add(DiagnosisResult(
                check_name="syntax:required_field", passed=False, severity="warning",
                message="design_name is empty or default", layer=1
            ))

        if not state.schema_version:
            report.add(DiagnosisResult(
                check_name="syntax:required_field", passed=False, severity="error",
                message="schema_version is required", layer=1
            ))

        if not state.topology:
            report.add(DiagnosisResult(
                check_name="syntax:required_field", passed=False, severity="error",
                message="topology is required", layer=1
            ))
        elif not state.topology.devices:
            report.add(DiagnosisResult(
                check_name="syntax:required_field", passed=False, severity="error",
                message="topology.devices is empty - at least one device required", layer=1
            ))

        # Internal implementation note.
        for dev in state.topology.devices:
            if not isinstance(dev.connections, dict):
                report.add(DiagnosisResult(
                    check_name="syntax:type_check", passed=False, severity="error",
                    message=f"{dev.id}: connections must be a dict", layer=1, device=dev.id
                ))
            if dev.type not in ("nmos", "pmos"):
                report.add(DiagnosisResult(
                    check_name="syntax:type_check", passed=False, severity="warning",
                    message=f"{dev.id}: unknown device type '{dev.type}' (expected nmos/pmos)",
                    layer=1, device=dev.id
                ))

        if not state.loss_terms:
            report.add(DiagnosisResult(
                check_name="syntax:required_field", passed=False, severity="warning",
                message="loss_terms is empty - optimizer has no objective", layer=1
            ))

        return report


# Internal implementation note.

class SemanticValidator:
    """AnalogRF-IR internal documentation."""

    @staticmethod
    def validate(state: DesignState) -> ValidationReport:
        report = ValidationReport()
        topo_ids = {dev.id for dev in state.topology.devices}
        transistor_ids = set(state.transistors.keys())

        # devices <-> transistors
        missing_in_transistors = topo_ids - transistor_ids
        for mid in missing_in_transistors:
            report.add(DiagnosisResult(
                check_name="semantic:device_transistor_match", passed=False,
                severity="error",
                message=f"Device {mid} defined in topology but missing in transistors",
                layer=2, device=mid
            ))

        extra_in_transistors = transistor_ids - topo_ids
        for eid in extra_in_transistors:
            report.add(DiagnosisResult(
                check_name="semantic:device_transistor_match", passed=False,
                severity="error",
                message=f"Transistor {eid} present but not defined in topology",
                layer=2, device=eid
            ))

        # devices <-> constraints.per_device
        for dev_id in state.constraints.per_device:
            if dev_id not in topo_ids:
                report.add(DiagnosisResult(
                    check_name="semantic:constraint_device_match", passed=False,
                    severity="error",
                    message=f"Constraint defined for unknown device '{dev_id}'",
                    layer=2, device=dev_id
                ))

        # design_variables.device -> devices
        for dv in state.design_variables:
            if not dv.device:
                continue  # Internal implementation note.
            if dv.device not in topo_ids:
                report.add(DiagnosisResult(
                    check_name="semantic:design_var_device", passed=False,
                    severity="error",
                    message=f"Design variable references unknown device '{dv.device}'",
                    layer=2, device=dv.device
                ))

        # Internal implementation note.
        valid_nets = {gn.name for gn in state.topology.global_nets}
        valid_nets |= {p.id for p in state.topology.ports}
        # Internal implementation note.
        for dev in state.topology.devices:
            for pin, net_name in dev.connections.items():
                if not net_name or (isinstance(net_name, str) and net_name.strip() == ""):
                    report.add(DiagnosisResult(
                        check_name="semantic:connection_net", passed=False,
                        severity="error",
                        message=f"{dev.id}.{pin} connected to empty net",
                        layer=2, device=dev.id
                    ))

        return report


# Internal implementation note.

class ValueValidator:
    """AnalogRF-IR internal documentation."""

    @staticmethod
    def validate(state: DesignState) -> ValidationReport:
        report = ValidationReport()

        # Internal implementation note.
        for key, rng in state.constraints.global_.items():
            if rng.min > rng.max:
                report.add(DiagnosisResult(
                    check_name="value:range_validity", passed=False,
                    severity="error",
                    message=f"Global constraint '{key}': min ({rng.min}) > max ({rng.max})",
                    layer=3
                ))

        for dev_id, dc in state.constraints.per_device.items():
            if dc.gm_id and dc.gm_id.min > dc.gm_id.max:
                report.add(DiagnosisResult(
                    check_name="value:range_validity", passed=False,
                    severity="error",
                    message=f"{dev_id} gm_id range invalid: min > max", layer=3, device=dev_id
                ))
            if dc.L and dc.L.min > dc.L.max:
                report.add(DiagnosisResult(
                    check_name="value:range_validity", passed=False,
                    severity="error",
                    message=f"{dev_id} L range invalid: min > max", layer=3, device=dev_id
                ))

        # Internal implementation note.
        sim = state.simulation
        if not (-50 < sim.temperature < 200):
            report.add(DiagnosisResult(
                check_name="value:temperature_range", passed=False,
                severity="warning",
                message=f"Temperature {sim.temperature} degC outside IC range (-50~200 degC)",
                layer=3
            ))

        vdd = sim.supply.get("vdd", 0)
        vss = sim.supply.get("vss", 0)
        if vdd <= vss:
            report.add(DiagnosisResult(
                check_name="value:supply_polarity", passed=False,
                severity="error",
                message=f"vdd ({vdd}V) must be > vss ({vss}V)", layer=3
            ))

        # Internal implementation note.
        for tid, ts in state.transistors.items():
            p = ts.parameters
            if p.W < 0:
                report.add(DiagnosisResult(
                    check_name="value:positive_width", passed=False,
                    severity="warning",
                    message=f"{tid}: W={p.W:.2e} should be positive", layer=3, device=tid
                ))
            if p.L <= 0 and ts.L_strategy <= 0:
                report.add(DiagnosisResult(
                    check_name="value:positive_length", passed=False,
                    severity="warning",
                    message=f"{tid}: L is undefined (both strategy and parameters)", layer=3, device=tid
                ))

        # Internal implementation note.
        for lt in state.loss_terms:
            if lt.weight < 0:
                report.add(DiagnosisResult(
                    check_name="value:loss_weight_positive", passed=False,
                    severity="warning",
                    message=f"Loss '{lt.id}': negative weight {lt.weight}", layer=3
                ))
            if lt.weight > 100:
                report.add(DiagnosisResult(
                    check_name="value:loss_weight_sanity", passed=False,
                    severity="warning",
                    message=f"Loss '{lt.id}': weight {lt.weight} > 100 may cause instability",
                    layer=3
                ))

        # Internal implementation note.
        for name, t in state.targets.items():
            if t.min is not None and t.max is not None and t.min > t.max:
                report.add(DiagnosisResult(
                    check_name="value:target_range", passed=False,
                    severity="error",
                    message=f"Target '{name}': min ({t.min}) > max ({t.max})", layer=3
                ))

        return report


# Internal implementation note.

class PhysicalValidator:
    """AnalogRF-IR internal documentation."""

    SATURATION_EXEMPT_ROLES: Set[str] = set()

    @staticmethod
    def validate(state: DesignState,
                  vdsat_margin: float = 0.05,
                  min_headroom: float = 0.15,
                  symmetry_tolerance: float = 0.05,
                  profile: CircuitProfile | None = None) -> ValidationReport:
        report = ValidationReport()
        profile = profile or select_circuit_profile(state)

        # Internal implementation note.
        report.results.extend(
            PhysicalValidator._check_saturation(state, vdsat_margin, profile)
        )

        # Internal implementation note.
        report.results.extend(
            PhysicalValidator._check_symmetry(state, symmetry_tolerance)
        )

        # Internal implementation note.
        report.results.extend(
            PhysicalValidator._check_voltage_stack(state, min_headroom, profile)
        )

        # Internal implementation note.
        report.results.extend(
            PhysicalValidator._check_current_balance(state)
        )

        return report

    # Internal implementation note.

    @staticmethod
    def _check_saturation(
        state: DesignState,
        margin: float,
        profile: CircuitProfile,
    ) -> List[DiagnosisResult]:
        results = []
        for tid, ts in state.transistors.items():
            p = ts.parameters
            if p.region == "unknown" or p.vds == 0:
                continue
            dev_def = state.get_device_def(tid)
            role = dev_def.role if dev_def else ""
            if role in PhysicalValidator.SATURATION_EXEMPT_ROLES or profile.is_dynamic_role(role):
                continue

            if p.vds < p.vdsat + margin:
                results.append(DiagnosisResult(
                    check_name="physical:saturation", passed=False,
                    severity="warning",
                    message=f"{tid} ({role}): vds={p.vds:.3f}V < vdsat+margin={p.vdsat + margin:.3f}V",
                    layer=4, device=tid,
                    details={"vds": p.vds, "vdsat": p.vdsat, "margin": p.vds - p.vdsat}
                ))
            else:
                results.append(DiagnosisResult(
                    check_name="physical:saturation", passed=True,
                    severity="info",
                    message=f"{tid} ({role}): vds={p.vds:.3f}V >= vdsat={p.vdsat:.3f}V, margin={p.vds - p.vdsat:.3f}V",
                    layer=4, device=tid
                ))
        return results

    @staticmethod
    def _check_symmetry(state: DesignState, tolerance: float) -> List[DiagnosisResult]:
        """AnalogRF-IR internal documentation."""
        results = []
        seen: set[tuple[str, str]] = set()
        role_groups: Dict[str, List[str]] = {}
        for dev in state.topology.devices:
            role_groups.setdefault(dev.role, []).append(dev.id)

        for role, dev_ids in role_groups.items():
            if len(dev_ids) < 2:
                continue

            # Internal implementation note.
            for i in range(len(dev_ids)):
                for j in range(i + 1, len(dev_ids)):
                    a, b = dev_ids[i], dev_ids[j]
                    if a not in state.transistors or b not in state.transistors:
                        continue
                    seen.add((a, b))
                    p1 = state.transistors[a].parameters
                    p2 = state.transistors[b].parameters
                    results.extend(
                        PhysicalValidator._compare_pair(a, b, p1, p2, tolerance, role)
                    )
        label_groups: Dict[str, List[str]] = {}
        for dv in state.design_variables:
            if dv.symmetry_label and dv.device:
                label_groups.setdefault(dv.symmetry_label, []).append(dv.device)
        for label, raw_ids in label_groups.items():
            dev_ids = sorted(set(raw_ids))
            if len(dev_ids) < 2:
                continue
            for i in range(len(dev_ids)):
                for j in range(i + 1, len(dev_ids)):
                    a, b = dev_ids[i], dev_ids[j]
                    key = (a, b)
                    if key in seen or a not in state.transistors or b not in state.transistors:
                        continue
                    p1 = state.transistors[a].parameters
                    p2 = state.transistors[b].parameters
                    results.extend(
                        PhysicalValidator._compare_pair(a, b, p1, p2, tolerance, label)
                    )
        return results

    @staticmethod
    def _compare_pair(a: str, b: str, p1: TransistorParameters,
                       p2: TransistorParameters, tolerance: float,
                       role: str) -> List[DiagnosisResult]:
        results = []
        checks = {
            "W": (p1.W, p2.W),
            "L": (p1.L, p2.L),
            "gm": (p1.gm, p2.gm),
            "id": (p1.id, p2.id),
            "gm_id": (p1.gm_id_realized, p2.gm_id_realized),
            "vgs": (p1.vgs, p2.vgs),
            "vds": (p1.vds, p2.vds),
        }
        for param, (v1, v2) in checks.items():
            if v1 <= 0 or v2 <= 0:
                continue
            denom = max(abs(v1), abs(v2))
            if denom < 1e-30:
                continue
            deviation = abs(v1 - v2) / denom
            if deviation > tolerance:
                severity = "error" if param in {"W", "L"} else "warning"
                results.append(DiagnosisResult(
                    check_name="physical:symmetry", passed=False,
                    severity=severity,
                    message=f"{a}/{b} ({role}) {param} mismatch: {v1:.4e} vs {v2:.4e} "
                            f"(deviation={deviation*100:.1f}%)",
                    layer=4, device=f"{a}/{b}",
                    details={"param": param, "deviation": deviation}
                ))
        return results

    @staticmethod
    def _check_voltage_stack(
        state: DesignState,
        headroom: float,
        profile: CircuitProfile,
    ) -> List[DiagnosisResult]:
        """AnalogRF-IR internal documentation."""
        results = []
        if profile.skip_static_voltage_stack:
            return results
        vdd = state.simulation.supply.get("vdd", 1.8)
        vss = state.simulation.supply.get("vss", 0.0)
        supply_span = vdd - vss

        # Internal implementation note.
        stage_devices: Dict[str, List[str]] = {}
        for dev in state.topology.devices:
            stage_devices.setdefault(dev.stage, []).append(dev.id)

        # Internal implementation note.
        for stage, dev_ids in stage_devices.items():
            total_vds = 0.0
            for did in dev_ids:
                if did in state.transistors:
                    total_vds += state.transistors[did].parameters.vds
            if total_vds > supply_span - headroom:
                results.append(DiagnosisResult(
                    check_name="physical:voltage_stack", passed=False,
                    severity="warning",
                    message=f"Stage '{stage}': total VDS ({total_vds:.3f}V) "
                            f"> supply ({supply_span}V) - headroom ({headroom}V)",
                    layer=4
                ))

        return results

    @staticmethod
    def _check_current_balance(state: DesignState) -> List[DiagnosisResult]:
        """AnalogRF-IR internal documentation."""
        results = []
        # Internal implementation note.
        role_currents: Dict[str, List[float]] = {}
        for tid, ts in state.transistors.items():
            dev_def = state.get_device_def(tid)
            if not dev_def:
                continue
            p = ts.parameters
            if p.id <= 0:
                continue
            role_currents.setdefault(dev_def.role, []).append(p.id)

        # Internal implementation note.
        tail_currents = role_currents.get("tail_current_source", [])
        input_currents = role_currents.get("input_pair", [])
        if tail_currents and input_currents:
            tail_sum = sum(tail_currents)
            input_sum = sum(input_currents)
            if tail_sum > 0 and abs(tail_sum - input_sum) / tail_sum > 0.3:
                results.append(DiagnosisResult(
                    check_name="physical:current_balance", passed=False,
                    severity="warning",
                    message=f"Tail current ({tail_sum*1e6:.1f}uA) vs input pair sum "
                            f"({input_sum*1e6:.1f}uA) mismatch >30%",
                    layer=4
                ))

        return results


# Internal implementation note.

class Validator:
    """AnalogRF-IR internal documentation."""

    def __init__(self):
        self.syntax = SyntaxValidator()
        self.semantic = SemanticValidator()
        self.value_validator = ValueValidator()
        self.physical = PhysicalValidator()

    def validate(self, state: DesignState,
                  layers: Optional[List[int]] = None,
                  include_custom: bool = True) -> ValidationReport:
        report = ValidationReport()

        if layers is None:
            layers = [1, 2, 3, 4]

        if 1 in layers:
            report.results.extend(self.syntax.validate(state).results)
        if 2 in layers:
            report.results.extend(self.semantic.validate(state).results)
        if 3 in layers:
            report.results.extend(self.value_validator.validate(state).results)
        if 4 in layers:
            profile = select_circuit_profile(state)
            report.results.extend(self.physical.validate(state, profile=profile).results)
        if include_custom:
            profile = select_circuit_profile(state)
            report.results.extend(run_registered_rules(state, circuit_profile=profile.name).results)

        report.schema_valid = all(
            r.severity != "error" for r in report.results
        )
        return report


# Internal implementation note.

def validate(state: DesignState) -> ValidationReport:
    """AnalogRF-IR internal documentation."""
    return Validator().validate(state)

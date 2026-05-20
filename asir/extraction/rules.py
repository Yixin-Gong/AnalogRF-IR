from __future__ import annotations

from collections import defaultdict

from asir.semantic import SemanticPrimitive, SemanticPrimitiveGraph
from asir.topology import TopologyGraph


class RuleBasedSemanticExtractor:
    """Rule-based semantic extraction for comparator and OTA topologies."""

    def extract(self, topology: TopologyGraph) -> SemanticPrimitiveGraph:
        semantics = SemanticPrimitiveGraph(f"{topology.name}_semantics")
        if topology.circuit_class.lower() == "ota":
            for primitive in self._extract_ota_primitives(topology):
                semantics.add_primitive(primitive)
            self._add_semantic_relations(topology, semantics)
            return semantics

        for primitive in self._extract_differential_pairs(topology):
            semantics.add_primitive(primitive)
        for primitive in self._extract_cross_coupled_latches(topology):
            semantics.add_primitive(primitive)
        for primitive in self._extract_reset_switches(topology):
            semantics.add_primitive(primitive)
        for primitive in self._extract_tail_sources(topology):
            semantics.add_primitive(primitive)
        for primitive in self._extract_sampling_switches(topology):
            semantics.add_primitive(primitive)
        self._add_semantic_relations(topology, semantics)
        return semantics

    def _extract_ota_primitives(self, topology: TopologyGraph) -> list[SemanticPrimitive]:
        primitives: list[SemanticPrimitive] = []
        primitives.extend(self._extract_ota_input_pairs(topology))
        primitives.extend(self._extract_ota_current_mirror_loads(topology))
        primitives.extend(self._extract_ota_tail_bias(topology))
        primitives.extend(self._extract_ota_second_stage(topology))
        primitives.extend(self._extract_ota_miller_compensation(topology))
        return primitives

    def _extract_ota_input_pairs(self, topology: TopologyGraph) -> list[SemanticPrimitive]:
        primitives: list[SemanticPrimitive] = []
        for index, devices in enumerate(self._devices_by_role_token(topology, "input_pair"), start=1):
            if len(devices) < 2:
                continue
            gates = sorted({topology.terminal_net(dev, "gate") or "" for dev in devices})
            drains = sorted({topology.terminal_net(dev, "drain") or "" for dev in devices})
            sources = sorted({topology.terminal_net(dev, "source") or "" for dev in devices})
            primitives.append(
                SemanticPrimitive(
                    id=f"ota_diff_pair_{index}",
                    primitive_type="differential_pair",
                    role="input transconductance stage",
                    member_devices=devices,
                    equations=[
                        "i_diff = gm1 * (vinp - vinn)",
                        "omega_u ~= gm1 / Cc",
                    ],
                    constraints=[
                        "input pair devices must be symmetric",
                        "input pair devices must remain in saturation at the operating point",
                    ],
                    active_phases=["bias", "small_signal"],
                    state_variables=["v_stage1", "i_diff"],
                    input_nets=gates,
                    output_nets=drains,
                    internal_nets=sources,
                )
            )
        return primitives

    def _extract_ota_current_mirror_loads(self, topology: TopologyGraph) -> list[SemanticPrimitive]:
        devices = [
            dev for dev in topology.mos_devices()
            if "current_mirror_load" in topology.role_hint(dev)
        ]
        if not devices:
            return []
        drains = sorted({topology.terminal_net(dev, "drain") or "" for dev in devices})
        gates = sorted({topology.terminal_net(dev, "gate") or "" for dev in devices})
        sources = sorted({topology.terminal_net(dev, "source") or "" for dev in devices})
        return [
            SemanticPrimitive(
                id="ota_active_load_1",
                primitive_type="current_mirror_load",
                role="single-ended active load for the first-stage differential pair",
                member_devices=sorted(devices),
                equations=[
                    "A1 ~= gm1 / (gds_input + gds_load)",
                    "p1 is lowered by Miller multiplication from the second-stage gain",
                ],
                constraints=[
                    "mirror load devices should be matched",
                    "mirror output device must remain in saturation at the operating point",
                ],
                active_phases=["bias", "small_signal"],
                state_variables=["v_stage1"],
                input_nets=gates,
                output_nets=drains,
                internal_nets=sources,
            )
        ]

    def _extract_ota_tail_bias(self, topology: TopologyGraph) -> list[SemanticPrimitive]:
        devices = [
            dev for dev in topology.mos_devices()
            if any(token in topology.role_hint(dev) for token in ("tail_current_source", "tail_bias_mirror"))
        ]
        if not devices:
            return []
        return [
            SemanticPrimitive(
                id="ota_tail_bias_1",
                primitive_type="tail_current_source",
                role="continuous bias current for the input differential pair",
                member_devices=sorted(devices),
                equations=[
                    "Itail sets gm1 through the selected gm/ID",
                    "SR+ ~= Itail / Cc",
                ],
                constraints=[
                    "tail source and tail reference devices should preserve the intended mirror ratio",
                    "tail source must remain in saturation at the operating point",
                ],
                active_phases=["bias", "small_signal"],
                state_variables=["Itail"],
                input_nets=sorted({topology.terminal_net(dev, "gate") or "" for dev in devices}),
                output_nets=sorted({topology.terminal_net(dev, "drain") or "" for dev in devices}),
                internal_nets=sorted({topology.terminal_net(dev, "source") or "" for dev in devices}),
            )
        ]

    def _extract_ota_second_stage(self, topology: TopologyGraph) -> list[SemanticPrimitive]:
        devices = [
            dev for dev in topology.mos_devices()
            if any(token in topology.role_hint(dev) for token in (
                "second_stage_gain",
                "second_stage_load",
                "output_current_source",
                "output_bias_mirror",
            ))
        ]
        gain_devices = [dev for dev in devices if "second_stage_gain" in topology.role_hint(dev)]
        if not gain_devices:
            return []
        output_nets = topology.output_nets() or sorted(
            {topology.terminal_net(dev, "drain") or "" for dev in devices}
        )
        return [
            SemanticPrimitive(
                id="ota_second_stage_1",
                primitive_type="second_stage_inverter",
                role="common-source inverter gain stage",
                member_devices=sorted(devices),
                equations=[
                    "A2 ~= gm2 / (gds_pullup + gds_pulldown)",
                    "p2 ~= gm2 / CL_eff",
                ],
                constraints=[
                    "second-stage pull-up and pull-down devices must remain in saturation at the operating point",
                    "second-stage bias mirror should preserve the intended current ratio",
                ],
                active_phases=["bias", "small_signal"],
                state_variables=["vout", "I_stage2", "gm2"],
                input_nets=sorted({topology.terminal_net(dev, "gate") or "" for dev in gain_devices}),
                output_nets=output_nets,
                internal_nets=sorted({topology.terminal_net(dev, "source") or "" for dev in devices}),
            )
        ]

    def _extract_ota_miller_compensation(self, topology: TopologyGraph) -> list[SemanticPrimitive]:
        comp_devices = [
            dev for dev in topology.capacitor_devices() + topology.resistor_devices()
            if "compensation" in topology.role_hint(dev).lower() or dev.lower() in {"cc", "rz"}
        ]
        if not comp_devices:
            return []
        touched_nets = sorted(
            {
                net
                for dev in comp_devices
                for terminal in ("plus", "minus")
                if (net := topology.terminal_net(dev, terminal))
            }
        )
        return [
            SemanticPrimitive(
                id="ota_miller_comp_1",
                primitive_type="miller_compensation",
                role="series Rz-Cc Miller compensation network",
                member_devices=sorted(comp_devices),
                equations=[
                    "omega_u ~= gm1 / Cc",
                    "omega_z = 1 / (Cc * (1/gm2 - Rz))",
                    "zero_target_Rz = 1 / gm2",
                ],
                constraints=[
                    "Cc pulls the dominant pole to lower frequency and pushes the output pole higher",
                    "Rz should be set near 1/gm2 to remove the right-half-plane zero",
                ],
                active_phases=["small_signal"],
                state_variables=["Cc", "Rz", "omega_z"],
                input_nets=touched_nets,
                output_nets=touched_nets,
            )
        ]

    def _extract_differential_pairs(self, topology: TopologyGraph) -> list[SemanticPrimitive]:
        role_groups = self._devices_by_role_token(topology, "input_pair")
        primitives: list[SemanticPrimitive] = []
        for index, devices in enumerate(role_groups, start=1):
            if len(devices) < 2:
                continue
            gates = sorted({topology.terminal_net(dev, "gate") or "" for dev in devices})
            drains = sorted({topology.terminal_net(dev, "drain") or "" for dev in devices})
            sources = sorted({topology.terminal_net(dev, "source") or "" for dev in devices})
            primitives.append(
                SemanticPrimitive(
                    id=f"diff_pair_{index}",
                    primitive_type="differential_pair",
                    role="input transconductance and differential charge steering",
                    member_devices=devices,
                    equations=[
                        "i_diff = gm_input * (vinp - vinn)",
                        "d(v_internal_diff)/dt = i_diff / Cint",
                        "input_capacitance ~= Cgs_input + Cgd_input + Csample",
                        "kickback_noise ~= alpha_kb * Vclock * Cgd_input / input_capacitance",
                    ],
                    constraints=[
                        "input pair devices must be symmetric",
                        "common source node must be phase-enabled by a tail device",
                        "input capacitance and kickback must fit the previous-stage drive budget",
                    ],
                    active_phases=["amplify"],
                    state_variables=["v_internal_diff", "i_diff"],
                    input_nets=gates,
                    output_nets=drains,
                    internal_nets=sources,
                )
            )
        return primitives

    def _extract_cross_coupled_latches(self, topology: TopologyGraph) -> list[SemanticPrimitive]:
        latch_devices = [
            dev
            for dev in topology.mos_devices()
            if "latch" in topology.role_hint(dev) or self._is_cross_coupled_device(topology, dev)
        ]
        if not latch_devices:
            return []
        output_nets = topology.output_nets() or sorted(
            {topology.terminal_net(dev, "drain") or "" for dev in latch_devices}
        )
        control_nets = sorted({topology.terminal_net(dev, "gate") or "" for dev in latch_devices})
        internal_nets = sorted(
            {
                topology.terminal_net(dev, "source") or ""
                for dev in latch_devices
                if topology.terminal_net(dev, "source") not in {"vdd", "vss", "gnd"}
            }
        )
        return [
            SemanticPrimitive(
                id="latch_1",
                primitive_type="cross_coupled_latch",
                role="positive feedback regeneration and digital decision",
                member_devices=sorted(latch_devices),
                    equations=[
                        "d(vout_diff)/dt = (gm_latch / CL) * vout_diff",
                        "regeneration_time = (CL / gm_latch) * ln(Vlogic / v_initial_diff)",
                        "metastability_margin = input_step / sqrt(offset^2 + noise^2)",
                    ],
                    constraints=[
                        "gm_latch must exceed effective load conductance",
                        "cross-coupled devices must preserve output polarity",
                        "output capacitances should be balanced",
                        "latch output swing must cross the downstream logic threshold",
                    ],
                active_phases=["regenerate", "saturate"],
                state_variables=["vout_diff", "decision_polarity"],
                input_nets=control_nets,
                output_nets=output_nets,
                internal_nets=internal_nets,
            )
        ]

    def _extract_reset_switches(self, topology: TopologyGraph) -> list[SemanticPrimitive]:
        devices = [
            dev
            for dev in topology.mos_devices()
            if any(token in topology.role_hint(dev) for token in ("reset", "precharge", "equalize"))
        ]
        groups: dict[str, list[str]] = defaultdict(list)
        for dev in devices:
            hint = topology.role_hint(dev)
            key = "equalize" if "equalize" in hint else "precharge"
            groups[key].append(dev)

        primitives: list[SemanticPrimitive] = []
        for index, (kind, members) in enumerate(sorted(groups.items()), start=1):
            touched_nets = sorted(
                {
                    net
                    for dev in members
                    for terminal in ("drain", "source")
                    if (net := topology.terminal_net(dev, terminal))
                }
            )
            control_nets = sorted({topology.terminal_net(dev, "gate") or "" for dev in members})
            primitives.append(
                SemanticPrimitive(
                    id=f"reset_{index}_{kind}",
                    primitive_type="reset_switch",
                    role=f"{kind} dynamic node initialization",
                    member_devices=sorted(members),
                    equations=[
                        "v_dynamic -> v_reset during reset",
                        "reset_settling_time = R_reset * C_dynamic",
                    ],
                    constraints=[
                        "reset phase must settle dynamic nodes before amplify",
                        "paired reset switches should be symmetric",
                    ],
                    active_phases=["reset"],
                    state_variables=["v_reset_common_mode"],
                    output_nets=touched_nets,
                    control_nets=control_nets,
                )
            )
        return primitives

    def _extract_tail_sources(self, topology: TopologyGraph) -> list[SemanticPrimitive]:
        devices = [
            dev
            for dev in topology.mos_devices()
            if "tail" in topology.role_hint(dev)
        ]
        primitives: list[SemanticPrimitive] = []
        for index, dev in enumerate(sorted(devices), start=1):
            primitives.append(
                SemanticPrimitive(
                    id=f"tail_{index}",
                    primitive_type="tail_current_source",
                    role="phase-gated evaluation current",
                    member_devices=[dev],
                    equations=[
                        "I_eval = f(clock, bias, device_size)",
                        "gm_input is bounded by tail current",
                    ],
                    constraints=[
                        "tail current must be disabled or reduced during reset",
                        "tail device headroom constrains input pair operation",
                    ],
                    active_phases=["amplify", "regenerate"],
                    state_variables=["I_eval"],
                    input_nets=[topology.terminal_net(dev, "gate") or ""],
                    output_nets=[topology.terminal_net(dev, "drain") or ""],
                    internal_nets=[topology.terminal_net(dev, "source") or ""],
                )
            )
        return primitives

    def _extract_sampling_switches(self, topology: TopologyGraph) -> list[SemanticPrimitive]:
        devices = [
            dev
            for dev in topology.mos_devices()
            if "sampling" in topology.role_hint(dev) or "sample" in topology.role_hint(dev)
        ]
        if not devices:
            return []
        touched_nets = sorted(
            {
                net
                for dev in devices
                for terminal in ("drain", "source")
                if (net := topology.terminal_net(dev, terminal))
            }
        )
        return [
            SemanticPrimitive(
                id="sampling_1",
                primitive_type="sampling_switch",
                role="input sampling and kT/C noise injection",
                member_devices=sorted(devices),
                equations=[
                    "q_sample = Csample * vin",
                    "noise_sample = kT / Csample",
                ],
                constraints=[
                    "sampling switches should be symmetric",
                    "sampling phase must precede regeneration",
                ],
                active_phases=["reset", "amplify"],
                state_variables=["q_sample_p", "q_sample_n"],
                input_nets=topology.input_nets(),
                output_nets=touched_nets,
                control_nets=sorted({topology.terminal_net(dev, "gate") or "" for dev in devices}),
            )
        ]

    def _add_semantic_relations(self, topology: TopologyGraph, semantics: SemanticPrimitiveGraph) -> None:
        primitives = semantics.primitives()
        for source in primitives:
            source_outputs = set(source.output_nets) | set(source.internal_nets)
            for target in primitives:
                if source.id == target.id:
                    continue
                shared = source_outputs & (set(target.input_nets) | set(target.internal_nets))
                for net in sorted(shared):
                    semantics.add_relation(source.id, target.id, relation="symbolic_signal_flow", through=net)
                control_shared = set(source.control_nets) & set(target.control_nets)
                for net in sorted(control_shared):
                    semantics.add_relation(source.id, target.id, relation="shared_phase_control", through=net)

    def _devices_by_role_token(self, topology: TopologyGraph, token: str) -> list[list[str]]:
        devices = [dev for dev in topology.mos_devices() if token in topology.role_hint(dev)]
        if not devices:
            devices = self._infer_input_pair_candidates(topology)
        if not devices:
            return []
        groups: dict[str, list[str]] = defaultdict(list)
        for dev in devices:
            source = topology.terminal_net(dev, "source") or "floating"
            groups[source].append(dev)
        return [sorted(group) for group in groups.values()]

    def _infer_input_pair_candidates(self, topology: TopologyGraph) -> list[str]:
        inputs = set(topology.input_nets())
        candidates = [
            dev
            for dev in topology.mos_devices()
            if topology.terminal_net(dev, "gate") in inputs and topology.mos_type(dev) == "nmos"
        ]
        source_counts: dict[str, int] = defaultdict(int)
        for dev in candidates:
            source_counts[topology.terminal_net(dev, "source") or ""] += 1
        return [dev for dev in candidates if source_counts[topology.terminal_net(dev, "source") or ""] >= 2]

    def _is_cross_coupled_device(self, topology: TopologyGraph, device_id: str) -> bool:
        drain = topology.terminal_net(device_id, "drain")
        gate = topology.terminal_net(device_id, "gate")
        if not drain or not gate:
            return False
        for other in topology.mos_devices():
            if other == device_id:
                continue
            if topology.terminal_net(other, "drain") == gate and topology.terminal_net(other, "gate") == drain:
                return True
        return False

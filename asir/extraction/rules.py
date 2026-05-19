from __future__ import annotations

from collections import defaultdict

from asir.semantic import SemanticPrimitive, SemanticPrimitiveGraph
from asir.topology import TopologyGraph


class RuleBasedSemanticExtractor:
    """Rule-based semantic extraction for comparator topologies."""

    def extract(self, topology: TopologyGraph) -> SemanticPrimitiveGraph:
        semantics = SemanticPrimitiveGraph(f"{topology.name}_semantics")
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
                    ],
                    constraints=[
                        "input pair devices must be symmetric",
                        "common source node must be phase-enabled by a tail device",
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
                ],
                constraints=[
                    "gm_latch must exceed effective load conductance",
                    "cross-coupled devices must preserve output polarity",
                    "output capacitances should be balanced",
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

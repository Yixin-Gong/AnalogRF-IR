from __future__ import annotations

from asir.topology import TopologyGraph


def _add_common_ports(topo: TopologyGraph) -> None:
    topo.add_net("vdd", net_type="supply")
    topo.add_net("vss", net_type="ground")
    topo.add_net("vinp", net_type="differential_input")
    topo.add_net("vinn", net_type="differential_input")
    topo.add_net("outp", net_type="differential_output")
    topo.add_net("outn", net_type="differential_output")
    topo.add_clock("CLK", drives="clk", phases=["amplify", "regenerate"])
    topo.add_clock("CLKB", drives="clkb", phases=["reset"])


def build_strongarm_comparator() -> TopologyGraph:
    topo = TopologyGraph("strongarm_comparator", architecture="strongarm")
    _add_common_ports(topo)
    topo.add_net("tail", net_type="dynamic_internal")
    topo.add_net("intp", net_type="dynamic_internal")
    topo.add_net("intn", net_type="dynamic_internal")

    topo.add_mos("M_INP", "nmos", drain="intp", gate="vinp", source="tail", bulk="vss", role_hint="input_pair_left")
    topo.add_mos("M_INN", "nmos", drain="intn", gate="vinn", source="tail", bulk="vss", role_hint="input_pair_right")
    topo.add_mos("M_TAIL", "nmos", drain="tail", gate="clk", source="vss", bulk="vss", role_hint="tail_current_source")

    topo.add_mos("M_NL_P", "nmos", drain="outp", gate="outn", source="intp", bulk="vss", role_hint="latch_nmos_left")
    topo.add_mos("M_NL_N", "nmos", drain="outn", gate="outp", source="intn", bulk="vss", role_hint="latch_nmos_right")
    topo.add_mos("M_PL_P", "pmos", drain="outp", gate="outn", source="vdd", bulk="vdd", role_hint="latch_pmos_left")
    topo.add_mos("M_PL_N", "pmos", drain="outn", gate="outp", source="vdd", bulk="vdd", role_hint="latch_pmos_right")

    topo.add_mos("M_RST_P", "pmos", drain="outp", gate="clkb", source="vdd", bulk="vdd", role_hint="reset_precharge_left")
    topo.add_mos("M_RST_N", "pmos", drain="outn", gate="clkb", source="vdd", bulk="vdd", role_hint="reset_precharge_right")
    topo.add_mos("M_EQ", "pmos", drain="outp", gate="clkb", source="outn", bulk="vdd", role_hint="reset_equalize_outputs")

    topo.add_capacitor("C_OUTP", plus="outp", minus="vss", capacitance="CL", role_hint="output_load")
    topo.add_capacitor("C_OUTN", plus="outn", minus="vss", capacitance="CL", role_hint="output_load")
    return topo


def build_double_tail_comparator() -> TopologyGraph:
    topo = TopologyGraph("double_tail_comparator", architecture="double-tail")
    _add_common_ports(topo)
    topo.add_net("tail_input", net_type="dynamic_internal")
    topo.add_net("tail_latch", net_type="dynamic_internal")
    topo.add_net("fp", net_type="dynamic_internal")
    topo.add_net("fn", net_type="dynamic_internal")

    topo.add_mos("M_INP", "nmos", drain="fp", gate="vinp", source="tail_input", bulk="vss", role_hint="input_pair_left")
    topo.add_mos("M_INN", "nmos", drain="fn", gate="vinn", source="tail_input", bulk="vss", role_hint="input_pair_right")
    topo.add_mos("M_TAIL_IN", "nmos", drain="tail_input", gate="clk", source="vss", bulk="vss", role_hint="tail_current_source_input_stage")

    topo.add_mos("M_RST_FP", "pmos", drain="fp", gate="clkb", source="vdd", bulk="vdd", role_hint="reset_precharge_first_stage_left")
    topo.add_mos("M_RST_FN", "pmos", drain="fn", gate="clkb", source="vdd", bulk="vdd", role_hint="reset_precharge_first_stage_right")

    topo.add_mos("M_ISO_P", "nmos", drain="outp", gate="fn", source="tail_latch", bulk="vss", role_hint="latch_input_steering_left")
    topo.add_mos("M_ISO_N", "nmos", drain="outn", gate="fp", source="tail_latch", bulk="vss", role_hint="latch_input_steering_right")
    topo.add_mos("M_TAIL_L", "nmos", drain="tail_latch", gate="clk", source="vss", bulk="vss", role_hint="tail_current_source_latch_stage")

    topo.add_mos("M_NL_P", "nmos", drain="outp", gate="outn", source="tail_latch", bulk="vss", role_hint="latch_nmos_left")
    topo.add_mos("M_NL_N", "nmos", drain="outn", gate="outp", source="tail_latch", bulk="vss", role_hint="latch_nmos_right")
    topo.add_mos("M_PL_P", "pmos", drain="outp", gate="outn", source="vdd", bulk="vdd", role_hint="latch_pmos_left")
    topo.add_mos("M_PL_N", "pmos", drain="outn", gate="outp", source="vdd", bulk="vdd", role_hint="latch_pmos_right")

    topo.add_mos("M_RST_OUTP", "pmos", drain="outp", gate="clkb", source="vdd", bulk="vdd", role_hint="reset_precharge_output_left")
    topo.add_mos("M_RST_OUTN", "pmos", drain="outn", gate="clkb", source="vdd", bulk="vdd", role_hint="reset_precharge_output_right")

    topo.add_capacitor("C_FP", plus="fp", minus="vss", capacitance="Cint", role_hint="first_stage_load")
    topo.add_capacitor("C_FN", plus="fn", minus="vss", capacitance="Cint", role_hint="first_stage_load")
    topo.add_capacitor("C_OUTP", plus="outp", minus="vss", capacitance="CL", role_hint="output_load")
    topo.add_capacitor("C_OUTN", plus="outn", minus="vss", capacitance="CL", role_hint="output_load")
    return topo


def build_sense_amplifier_comparator() -> TopologyGraph:
    topo = TopologyGraph("sense_amplifier_comparator", architecture="sense-amplifier")
    _add_common_ports(topo)
    topo.add_clock("SAMPLE", drives="sample", phases=["reset", "amplify"])
    topo.add_net("sample_p", net_type="sampled_input")
    topo.add_net("sample_n", net_type="sampled_input")
    topo.add_net("tail", net_type="dynamic_internal")

    topo.add_mos("M_SAMP_P", "nmos", drain="sample_p", gate="sample", source="vinp", bulk="vss", role_hint="sampling_switch_left")
    topo.add_mos("M_SAMP_N", "nmos", drain="sample_n", gate="sample", source="vinn", bulk="vss", role_hint="sampling_switch_right")

    topo.add_mos("M_INP", "nmos", drain="outp", gate="sample_p", source="tail", bulk="vss", role_hint="input_pair_left")
    topo.add_mos("M_INN", "nmos", drain="outn", gate="sample_n", source="tail", bulk="vss", role_hint="input_pair_right")
    topo.add_mos("M_TAIL", "nmos", drain="tail", gate="clk", source="vss", bulk="vss", role_hint="tail_current_source")

    topo.add_mos("M_NL_P", "nmos", drain="outp", gate="outn", source="tail", bulk="vss", role_hint="latch_nmos_left")
    topo.add_mos("M_NL_N", "nmos", drain="outn", gate="outp", source="tail", bulk="vss", role_hint="latch_nmos_right")
    topo.add_mos("M_PL_P", "pmos", drain="outp", gate="outn", source="vdd", bulk="vdd", role_hint="latch_pmos_left")
    topo.add_mos("M_PL_N", "pmos", drain="outn", gate="outp", source="vdd", bulk="vdd", role_hint="latch_pmos_right")

    topo.add_mos("M_RST_P", "pmos", drain="outp", gate="clkb", source="vdd", bulk="vdd", role_hint="reset_precharge_left")
    topo.add_mos("M_RST_N", "pmos", drain="outn", gate="clkb", source="vdd", bulk="vdd", role_hint="reset_precharge_right")
    topo.add_mos("M_EQ", "pmos", drain="outp", gate="clkb", source="outn", bulk="vdd", role_hint="reset_equalize_outputs")

    topo.add_capacitor("C_SAMPLE_P", plus="sample_p", minus="vss", capacitance="Csample", role_hint="sampling_capacitance")
    topo.add_capacitor("C_SAMPLE_N", plus="sample_n", minus="vss", capacitance="Csample", role_hint="sampling_capacitance")
    topo.add_capacitor("C_OUTP", plus="outp", minus="vss", capacitance="CL", role_hint="output_load")
    topo.add_capacitor("C_OUTN", plus="outn", minus="vss", capacitance="CL", role_hint="output_load")
    return topo


COMPARATOR_BUILDERS = {
    "strongarm": build_strongarm_comparator,
    "double-tail": build_double_tail_comparator,
    "sense-amplifier": build_sense_amplifier_comparator,
}

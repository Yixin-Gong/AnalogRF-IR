from asir.design import build_design
from asir.examples import (
    build_double_tail_comparator,
    build_sense_amplifier_comparator,
    build_strongarm_comparator,
)
from asir.io.v1_yaml import build_design_from_v1_yaml, embed_asir_output, load_v1_yaml
from asir.cli import main as cli_main


def test_strongarm_extracts_required_primitives():
    design = build_design(build_strongarm_comparator())
    counts = design.semantic_graph.primitive_type_counts()
    assert counts["differential_pair"] == 1
    assert counts["cross_coupled_latch"] == 1
    assert counts["reset_switch"] >= 1
    assert counts["tail_current_source"] == 1
    assert "delay" in design.dependency_graph.graph
    assert "kickback_noise" in design.dependency_graph.graph
    assert "metastability_margin" in design.dependency_graph.graph
    assert "max_sample_rate" in design.dependency_graph.graph
    assert "reset" in design.phase_graph.graph
    assert design.topology_graph.graph.nodes["outp"]["net_type"] == "differential_output"
    assert design.topology_graph.graph.nodes["clk"]["net_type"] == "clock"


def test_sense_amp_has_sampling_semantics():
    design = build_design(build_sense_amplifier_comparator())
    counts = design.semantic_graph.primitive_type_counts()
    assert counts["sampling_switch"] == 1
    assert "sampling_1" in design.phase_graph.active_primitives("amplify")
    assert "sampling_time" in design.dependency_graph.graph


def test_double_tail_preserves_layer_separation():
    design = build_design(build_double_tail_comparator())
    assert design.topology_graph.graph is not design.semantic_graph.graph
    assert design.semantic_graph.graph is not design.dependency_graph.graph
    assert design.dependency_graph.graph is not design.phase_graph.graph
    assert design.to_dict()["layers"]["topology_graph"]["layer"] == "topology"
    assert design.to_dict()["layers"]["dependency_graph"]["layer"] == "symbolic_dependencies"


def test_dependency_forward_and_backward_reasoning():
    design = build_design(build_strongarm_comparator())
    values = design.dependency_graph.forward_propagate(
        {
            "CL": 20e-15,
            "Cint": 10e-15,
            "Csample": 30e-15,
            "gm_latch": 1e-3,
            "gm_input": 0.5e-3,
            "initial_delta_v": 2e-3,
            "logic_swing": 0.8,
            "R_reset": 1e3,
            "R_sample": 800.0,
            "mismatch": 2e-3,
            "device_area": 1e-12,
            "kT_over_C": 1e-4,
            "bandwidth": 1e8,
            "VDD": 1.0,
            "VSS": 0.0,
            "Cgs_input": 5e-15,
            "Cgd_input": 1e-15,
            "Vclock_swing": 1.0,
            "kickback_coupling": 0.02,
            "input_step": 10e-3,
            "icmr_min": 0.2,
            "icmr_max": 0.9,
            "switching_activity": 2.0,
        }
    )
    assert values["regeneration_time"] > 0
    assert values["delay"] > values["regeneration_time"]
    assert values["cycle_time"] > values["delay"]
    assert values["kickback_noise"] > 0
    assert values["input_capacitance"] > 0
    assert values["metastability_margin"] > 0
    assert values["max_sample_rate"] > 0
    trace = design.dependency_graph.backward_trace("delay")
    assert "gm_latch" in trace["source_symbols"]
    assert "CL" in trace["source_symbols"]


def test_rewrite_reasoning_between_architectures():
    strongarm = build_design(build_strongarm_comparator())
    sense_amp = build_design(build_sense_amplifier_comparator())
    report = strongarm.compare_rewrite_to(sense_amp)
    assert report.preserved_primitives["differential_pair"] == 1
    assert report.preserved_primitives["cross_coupled_latch"] == 1


def test_v1_yaml_input_embeds_asir_output():
    design = build_design_from_v1_yaml("inputs/comparator/strongarm/strongarm_v1.yaml")
    assert design.name == "strongarm_comparator"
    assert design.semantic_graph.primitive_type_counts()["cross_coupled_latch"] == 1
    embedded = embed_asir_output(load_v1_yaml("inputs/comparator/strongarm/strongarm_v1.yaml"), design)
    assert "topology" in embedded
    assert "asir_output" in embedded
    assert embedded["asir_output"]["layers"]["semantic_primitive_graph"]["layer"] == "semantic_primitives"


def test_from_yaml_cli_writes_embedded_output(tmp_path):
    out = tmp_path / "compiled.yaml"
    assert cli_main(["from-yaml", "inputs/comparator/strongarm/strongarm_v1.yaml", "--out", str(out)]) == 0
    data = load_v1_yaml(out)
    assert "topology" in data
    assert "asir_output" in data


def test_two_stage_ota_extracts_miller_semantics():
    design = build_design_from_v1_yaml("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml")
    payload = design.to_dict()
    counts = design.semantic_graph.primitive_type_counts()

    assert payload["domain"] == "analog_ota"
    assert counts["differential_pair"] == 1
    assert counts["second_stage_inverter"] == 1
    assert counts["miller_compensation"] == 1
    assert "dominant_pole_rad_s" in design.dependency_graph.graph
    assert "zero_target_Rz" in design.dependency_graph.graph
    assert "bias" in design.phase_graph.graph

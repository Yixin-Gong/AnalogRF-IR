import argparse
import json
import os
import re

import numpy as np
import pytest

from asir.capabilities import detect_circuit_capabilities
from core.compensation import has_miller_rc_compensation
from asir.profiles import select_circuit_profile
from core.environment import default_environment
from core.rule_registry import list_rules
from core.validator import Validator
from diagnostics import (
    agent_write_policy,
    apply_optimized_action_plan,
    apply_attribution_guided_tuning,
    build_causal_diagnostics,
    build_spice_intervention_model,
    execute_tuning_tool_commands,
    optimize_tuning_actions,
    write_tuning_tool_command,
)
from diagnostics.action_optimizer import _candidate_actions, _target_status_with_measured_updates
from flow.llm_planner import DeepSeekSchemaPlanner, LLMPlannerConfig, _loads_json_object, _planner_context
from flow.config import load_cli_config
from flow.agent_loop import DiagnosticAgentLoop, _tuning_from_llm_hypotheses
from flow.runner import AnalogRFIRFlowRunner, FlowConfig
from feasibility import FeasibilityConfig, TwoStageMillerFeasibilityChecker
from main import DEFAULT_AGENT_MAX_ITERATIONS, _configure_llm_api_key, _parse_args
from flow.state_update import apply_optimizer_meta_to_state
from frontends.design_input import load_design_input
from frontends.yaml_loader import build_design_state_from_yaml, load_yaml_mapping
from netlist.generator import generate_netlist
from optimizer.initialization import build_topology_guided_initial_points
from optimizer.nsga2 import CircuitEvaluator, Individual, NSGA2Optimizer
from optimizer.problem import OptimizationProblem
from outputs.artifacts import ArtifactWriter, _compact_tuning_action
from postprocess.cascode import (
    _candidate_points as _cascode_candidate_points,
    _select_candidate as _select_cascode_candidate,
    tune_cascode_ota_operating_point,
)
from postprocess.ota import _select_two_phase_candidate, tune_single_stage_ota_operating_point
from postprocess.registry import PostprocessConfig, PostprocessContext, PostprocessRegistry
from postprocess.source_follower import _candidate_points
from postprocess.two_stage import set_symmetric_width, tune_two_stage_compensation
from pygmid.adapter import create_pygmid_adapter
from schemas.design_state import DesignState
from simulator.ngspice import NgspiceSimulator, SimulationResult
from specs.models import SpecRegistry
from scripts.run_ablation import _execute_job, _latest_result_summary, build_jobs
from scripts.run_progressive_pareto import pareto_frontier, tighten_schema_targets


def test_design_input_accepts_spice_and_writes_schema(tmp_path):
    spice = tmp_path / "ota.cir"
    spice.write_text(
        """
        M1 net1 vinn tail gnd nmos W=10u L=300n
        M2 n1 vinp tail gnd nmos W=10u L=300n
        M3 net1 net1 vdd vdd pmos W=20u L=1u
        M4 n1 net1 vdd vdd pmos W=20u L=1u
        M5 tail vbias_tail gnd gnd nmos W=8u L=500n
        M6 vout n1 vdd vdd pmos W=60u L=500n
        M7 vout vbias_stage2 gnd gnd nmos W=30u L=500n
        Cc n1 vout 500f
        """,
        encoding="utf-8",
    )
    out = tmp_path / "compiled.yaml"

    bundle = load_design_input(
        env=default_environment(),
        schema_path="inputs/ota/five_transistor/five_transistor_ota.yaml",
        spice_path=spice,
        spice_yaml_out=out,
    )

    assert bundle.source_kind == "spice"
    assert out.exists()
    assert bundle.state.topology.architecture == "two-stage-miller"
    assert bundle.state.global_parameters["Cc"] == 500e-15


def test_progressive_pareto_tightens_targets_without_mutating_base():
    schema = {
        "targets": {
            "dc_gain": {"min": 24},
            "unity_gain_bandwidth": {"min": 5.0e6},
            "phase_margin": {"min": 45},
            "slew_rate": {"min": 4.0e6},
            "output_swing": {"min": 0.4},
            "power": {"max": 4.0e-4},
        }
    }
    args = argparse.Namespace(
        gain_step_db=2.0,
        ugbw_step=1.25,
        pm_step_deg=1.0,
        slew_step=1.2,
        swing_step_v=0.025,
        power_step=0.9,
    )

    tightened = tighten_schema_targets(schema, 2, args)

    assert tightened["targets"]["dc_gain"]["min"] == 28
    assert tightened["targets"]["unity_gain_bandwidth"]["min"] == 5.0e6 * 1.25**2
    assert tightened["targets"]["slew_rate"]["min"] == 4.0e6 * 1.2**2
    assert tightened["targets"]["output_swing"]["min"] == 0.45
    assert tightened["targets"]["power"]["max"] == 4.0e-4 * 0.9**2
    assert schema["targets"]["dc_gain"]["min"] == 24


def test_progressive_pareto_filters_dominated_and_failed_rows():
    rows = [
        {
            "name": "dominated",
            "spec_pass": True,
            "total_power": 2.0e-6,
            "dc_gain_db": 25.0,
            "unity_gain_bandwidth": 6.0e6,
            "phase_margin": 55.0,
            "slew_rate": 5.0e6,
            "output_swing": 0.45,
        },
        {
            "name": "dominates",
            "spec_pass": True,
            "total_power": 1.8e-6,
            "dc_gain_db": 26.0,
            "unity_gain_bandwidth": 7.0e6,
            "phase_margin": 56.0,
            "slew_rate": 5.5e6,
            "output_swing": 0.46,
        },
        {
            "name": "tradeoff",
            "spec_pass": True,
            "total_power": 1.6e-6,
            "dc_gain_db": 24.0,
            "unity_gain_bandwidth": 6.0e6,
            "phase_margin": 54.0,
            "slew_rate": 4.8e6,
            "output_swing": 0.44,
        },
        {"name": "failed", "spec_pass": False, "total_power": 0.5e-6, "dc_gain_db": 40.0},
    ]

    front = pareto_frontier(rows)
    names = {row["name"] for row in front}

    assert names == {"dominates", "tradeoff"}


def test_spec_registry_selects_ota_and_comparator():
    registry = SpecRegistry()
    ota = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    comparator = build_design_state_from_yaml(load_yaml_mapping("inputs/comparator/strongarm/strongarm_v1.yaml"), default_environment())

    assert registry.select(ota).name == "ota"
    assert registry.select(comparator).name == "comparator"
    assert registry.select(ota).measurement_key("dc_gain") == "dc_gain_db"
    assert registry.select(ota).measurement_key("slew_rate") == "slew_rate"
    assert registry.select(ota).measurement_key("swing") == "output_swing"
    assert registry.select(ota).measurement_key("input_common_mode_min") == "icmr_min"
    assert registry.select(comparator).measurement_key("offset") == "offset"
    assert registry.select(comparator).measurement_key("kickback") == "kickback_noise"
    assert registry.select(comparator).measurement_key("energy_per_comparison") == "energy"
    assert registry.select(comparator).measurement_key("power") == "power"


def test_ir_profile_drives_objectives_and_rule_filtering():
    ota = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    comparator = build_design_state_from_yaml(load_yaml_mapping("inputs/comparator/strongarm/strongarm_v1.yaml"), default_environment())
    source_follower = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/source_follower_boosted/source_follower_boosted_ota.yaml"),
        default_environment(),
    )

    ota_profile = select_circuit_profile(ota)
    comparator_profile = select_circuit_profile(comparator)
    ota_capabilities = detect_circuit_capabilities(ota, ota_profile)
    comparator_capabilities = detect_circuit_capabilities(comparator, comparator_profile)
    source_follower_capabilities = detect_circuit_capabilities(source_follower)
    comparator_rules = {item["name"] for item in list_rules(circuit_profile=comparator_profile.name)}
    ota_rules = {item["name"] for item in list_rules(circuit_profile=ota_profile.name)}

    assert ota_profile.name == "ota"
    assert comparator_profile.name == "comparator"
    assert comparator_profile.required_context == ("CL", "f_clk", "input_step")
    assert ota_capabilities.has("two_stage_gain")
    assert ota_capabilities.has("miller_rc_compensation")
    assert comparator_capabilities.has("dynamic_latch")
    assert comparator_capabilities.has("comparator_decision")
    assert source_follower_capabilities.has("source_follower_regulation")
    assert not source_follower_capabilities.has("miller_rc_compensation")
    assert any("by comparator profile" in term.description for term in comparator.loss_terms)
    assert "check_comparator_metric_coverage" in comparator_rules
    assert "check_comparator_metric_coverage" not in ota_rules


def test_optimization_problem_and_postprocess_registry_are_capability_driven(tmp_path):
    five_transistor = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"),
        default_environment(),
    )
    two_stage = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"),
        default_environment(),
    )
    source_follower = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/source_follower_boosted/source_follower_boosted_ota.yaml"),
        default_environment(),
    )

    five_transistor_problem = OptimizationProblem.from_state(five_transistor)
    two_stage_problem = OptimizationProblem.from_state(two_stage)
    source_follower_problem = OptimizationProblem.from_state(source_follower)
    registry = PostprocessRegistry()

    five_transistor_context = PostprocessContext(
        state=five_transistor,
        sim=NgspiceSimulator(),
        work_dir=tmp_path,
        config=PostprocessConfig(),
        profile=five_transistor_problem.profile,
        capabilities=five_transistor_problem.capabilities,
    )
    two_stage_context = PostprocessContext(
        state=two_stage,
        sim=NgspiceSimulator(),
        work_dir=tmp_path,
        config=PostprocessConfig(),
        profile=two_stage_problem.profile,
        capabilities=two_stage_problem.capabilities,
    )
    source_follower_context = PostprocessContext(
        state=source_follower,
        sim=NgspiceSimulator(),
        work_dir=tmp_path,
        config=PostprocessConfig(),
        profile=source_follower_problem.profile,
        capabilities=source_follower_problem.capabilities,
    )

    assert five_transistor_problem.estimator_key == "ota_compact"
    assert two_stage_problem.estimator_key == "ota_two_stage_miller"
    assert source_follower_problem.estimator_key == "ota_compact"
    assert [item.name for item in registry.resolve(five_transistor_context)] == ["single_stage_ota_operating_point"]
    assert [item.name for item in registry.resolve(two_stage_context)] == ["two_stage"]
    assert [item.name for item in registry.resolve(source_follower_context)] == ["source_follower_operating_point"]


def test_ihp130_ota_topology_suite_loads_and_routes_to_postprocess(tmp_path):
    ihp_env = load_yaml_mapping("environment_ihp_sg13g2.yaml")
    cases = {
        "inputs/ota/current_mirror/current_mirror_ota_ihp130.yaml": {
            "capability": "current_mirror_ota",
            "postprocess": ["current_mirror_ota_operating_point"],
        },
        "inputs/ota/telescopic/telescopic_ota_ihp130.yaml": {
            "capability": "telescopic_cascode",
            "postprocess": ["cascode_ota_operating_point"],
        },
        "inputs/ota/folded_cascode/folded_cascode_ota_ihp130.yaml": {
            "capability": "folded_cascode",
            "postprocess": ["cascode_ota_operating_point"],
        },
    }
    registry = PostprocessRegistry()

    for schema_path, expected in cases.items():
        state = build_design_state_from_yaml(load_yaml_mapping(schema_path), ihp_env)
        problem = OptimizationProblem.from_state(state)
        context = PostprocessContext(
            state=state,
            sim=NgspiceSimulator(),
            work_dir=tmp_path,
            config=PostprocessConfig(),
            profile=problem.profile,
            capabilities=problem.capabilities,
        )
        netlist = generate_netlist(state)

        assert state.process.process_name == "IHP_SG13G2_130nm"
        assert "sg13_lv_nmos" in netlist
        assert "sg13_lv_pmos" in netlist
        assert problem.capabilities.has(expected["capability"])
        assert [item.name for item in registry.resolve(context)] == expected["postprocess"]


def test_ihp130_two_stage_miller_compensation_uses_mim_capacitor():
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"),
        load_yaml_mapping("environment_ihp_sg13g2.yaml"),
    )

    netlist = generate_netlist(state)

    assert 'cornerCAP.lib" cap_typ' in netlist
    assert 'cornerRES.lib" res_typ' in netlist
    assert "* .osdi" in netlist and "r3_cmc.osdi" in netlist
    assert "\nXRz n1 ncc gnd rhigh " in netlist
    assert "\nXCc ncc vout cap_cmim " in netlist
    assert "w=16.33u l=16.33u" in netlist
    assert "\nCc " not in netlist
    assert "\nRz " not in netlist


def test_cascode_ota_postprocess_selects_bias_stack_candidate(tmp_path):
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/telescopic/telescopic_ota_ihp130.yaml"),
        load_yaml_mapping("environment_ihp_sg13g2.yaml"),
    )
    state.global_parameters["vbias_tail"] = 0.42
    state.global_parameters["vbias_ncas"] = 0.48
    state.global_parameters["vbias_pcas"] = 0.78

    class FakeCascodeSimulator:
        timeout_sec = 30.0

        def __init__(self):
            self.calls = 0

        def run(self, _netlist, work_dir=None, include_transient=False):
            self.calls += 1
            tail = state.global_parameters.get("vbias_tail", 0.0)
            ncas = state.global_parameters.get("vbias_ncas", 0.0)
            pcas = state.global_parameters.get("vbias_pcas", 0.0)
            passing = tail <= 0.38 and ncas >= 0.80 and pcas <= 0.43
            measurements = {
                "dc_gain_db": 70.0 if passing else 24.0,
                "unity_gain_bandwidth": 1.2e8 if passing else 8.0e6,
                "phase_margin": 80.0 if passing else 35.0,
                "slew_rate": 1.0e8 if passing else 1.0e6,
                "output_swing": 0.90 if passing else 0.12,
                "total_power": 1.2e-4,
            }
            margin = 0.16 if passing else 0.02
            operating_points = {
                dev.id: {"vds": margin + 0.12, "vdsat": 0.12, "gm": 1.0e-4, "gds": 1.0e-6, "id": 1.0e-5}
                for dev in state.topology.devices
            }
            return SimulationResult(success=True, return_code=0, measurements=measurements, operating_points=operating_points)

    result = tune_cascode_ota_operating_point(state, FakeCascodeSimulator(), tmp_path)

    assert result["spec_pass"] is True
    assert result["topology_family"] == "telescopic_cascode_ota"
    assert result["new_bias_values"]["vbias_tail"] <= 0.38
    assert result["new_bias_values"]["vbias_ncas"] >= 0.80
    assert result["new_bias_values"]["vbias_pcas"] <= 0.43
    assert state.global_parameters["vbias_ncas"] == result["new_bias_values"]["vbias_ncas"]


def test_cascode_bias_candidates_are_topology_guided_initial_searches():
    folded = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/folded_cascode/folded_cascode_ota_ihp130.yaml"),
        load_yaml_mapping("environment_ihp_sg13g2.yaml"),
    )
    folded_points = _cascode_candidate_points(
        folded,
        {"vbias_ptail": 0.82, "vbias_ncas": 0.55},
        1.2,
    )
    folded_guided = [item for item in folded_points if item.get("phase") == "folded_initial_search"]

    assert folded_guided
    assert all(0.55 <= item["vbias_ptail"] <= 1.05 for item in folded_guided)
    assert all(0.35 <= item["vbias_ncas"] <= 0.80 for item in folded_guided)
    assert any(item["vbias_ptail"] != 0.82 and item["vbias_ncas"] != 0.55 for item in folded_guided)

    telescopic = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/telescopic/telescopic_ota_ihp130.yaml"),
        load_yaml_mapping("environment_ihp_sg13g2.yaml"),
    )
    telescopic_points = _cascode_candidate_points(
        telescopic,
        {"vbias_tail": 0.50, "vbias_ncas": 0.72, "vbias_pcas": 0.55},
        1.2,
    )
    telescopic_guided = [item for item in telescopic_points if item.get("phase") == "telescopic_initial_search"]

    assert telescopic_guided
    assert all(0.30 <= item["vbias_tail"] <= 0.75 for item in telescopic_guided)
    assert all(0.45 <= item["vbias_ncas"] <= 0.90 for item in telescopic_guided)
    assert all(0.35 <= item["vbias_pcas"] <= 0.80 for item in telescopic_guided)


def test_optimizer_injects_topology_guided_initial_points():
    folded = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/folded_cascode/folded_cascode_ota_ihp130.yaml"),
        load_yaml_mapping("environment_ihp_sg13g2.yaml"),
    )
    folded_eval = CircuitEvaluator(folded, create_pygmid_adapter())
    folded_bounds = np.array(folded_eval.bounds)
    folded_base = np.array([
        dv.initial if dv.initial is not None else 0.5 * (dv.range.min + dv.range.max)
        for dv in folded_eval.encoded_design_variables
    ])

    folded_points = build_topology_guided_initial_points(
        folded_eval,
        folded_bounds,
        base=folded_base,
        max_points=8,
        rng=np.random.RandomState(7),
    )
    m6_l_idx = next(
        idx for idx, dv in enumerate(folded_eval.encoded_design_variables)
        if dv.device == "M6" and dv.variable == "L"
    )
    i_tail_idx = next(
        idx for idx, dv in enumerate(folded_eval.encoded_design_variables)
        if dv.device == "" and dv.variable == "I_tail"
    )

    assert folded_points
    assert any(point[m6_l_idx] > folded_base[m6_l_idx] for point in folded_points)
    assert any(point[i_tail_idx] > folded_base[i_tail_idx] for point in folded_points)
    assert all(np.all(point >= folded_bounds[:, 0]) and np.all(point <= folded_bounds[:, 1]) for point in folded_points)

    two_stage = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"),
        load_yaml_mapping("environment_ihp_sg13g2.yaml"),
    )
    two_eval = CircuitEvaluator(two_stage, create_pygmid_adapter())
    two_bounds = np.array(two_eval.bounds)
    two_base = np.array([
        dv.initial if dv.initial is not None else 0.5 * (dv.range.min + dv.range.max)
        for dv in two_eval.encoded_design_variables
    ])
    two_points = build_topology_guided_initial_points(
        two_eval,
        two_bounds,
        base=two_base,
        max_points=8,
        rng=np.random.RandomState(8),
    )
    stage2_current_idx = next(
        idx for idx, dv in enumerate(two_eval.encoded_design_variables)
        if dv.device == "" and dv.variable == "I_stage2"
    )
    cc_idx = next(
        idx for idx, dv in enumerate(two_eval.encoded_design_variables)
        if dv.device == "" and dv.variable == "Cc"
    )

    assert two_points
    assert any(point[stage2_current_idx] > two_base[stage2_current_idx] for point in two_points)
    assert any(point[cc_idx] < two_base[cc_idx] for point in two_points)


def test_uncompensated_two_stage_does_not_trigger_rc_logic(tmp_path):
    data = load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml")
    data["design_name"] = "uncompensated_two_stage_ota"
    data["topology"]["name"] = "uncompensated_two_stage_ota"
    data["topology"]["architecture"] = "two-stage"
    data["design_variables"] = [
        item for item in data["design_variables"]
        if item.get("device") or item.get("variable") not in {"Cc", "Rz"}
    ]
    data["loss_terms"] = [
        item for item in data["loss_terms"]
        if item.get("id") != "zero_alignment"
    ]
    state = build_design_state_from_yaml(data, default_environment())
    evaluator = CircuitEvaluator(state, create_pygmid_adapter())
    x = [dv.initial if dv.initial is not None else 0.5 * (dv.range.min + dv.range.max) for dv in state.design_variables]

    _obj, _violation, meta = evaluator.evaluate(x)
    netlist = generate_netlist(state)

    assert not has_miller_rc_compensation(state)
    assert "Cc" not in meta["performance"]
    assert "Rz" not in meta["performance"]
    assert "zero_target_rz" not in meta["performance"]
    assert "\nCc " not in netlist
    assert "\nRz " not in netlist
    assert tune_two_stage_compensation(state, NgspiceSimulator(), tmp_path) == {}


def test_source_follower_boosted_ota_has_no_rc_compensation():
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/source_follower_boosted/source_follower_boosted_ota.yaml"),
        default_environment(),
    )
    evaluator = CircuitEvaluator(state, create_pygmid_adapter())
    x = [dv.initial if dv.initial is not None else 0.5 * (dv.range.min + dv.range.max) for dv in state.design_variables]

    _obj, _violation, meta = evaluator.evaluate(x)
    netlist = generate_netlist(state)

    assert not has_miller_rc_compensation(state)
    assert "Cc" not in state.global_parameters
    assert "Rz" not in state.global_parameters
    assert "Cc" not in meta["performance"]
    assert "Rz" not in meta["performance"]
    assert "\nCc " not in netlist
    assert "\nRz " not in netlist
    assert "Vvbias_p vbias_p 0 DC 0.8400" in netlist
    assert "Vvbias_reg vbias_reg 0 DC 0.5500" in netlist
    assert any("source_follower" in dev.role for dev in state.topology.devices)


def test_source_follower_op_tune_includes_width_repair_candidates():
    points = _candidate_points([0.80, 0.82], [0.50, 0.55], ["M7"])

    assert (0.81, 0.47, 2.5) in points
    assert (0.80, 0.50, 2.0) in points
    assert (0.82, 0.55, 1.0) in points


def test_single_stage_ota_postprocess_selects_ngspice_guided_bias(tmp_path):
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"),
        default_environment(),
    )
    for dev_id in ("M1", "M2"):
        state.transistors[dev_id].parameters.W = 1.0e-6
        state.transistors[dev_id].parameters.L = 2.0e-7
        state.transistors[dev_id].parameters.vgs = 0.55
    for dev_id in ("M3", "M4"):
        state.transistors[dev_id].parameters.W = 2.0e-6
        state.transistors[dev_id].parameters.L = 3.0e-7
    state.transistors["M5"].parameters.W = 1.0e-6
    state.transistors["M5"].parameters.L = 2.0e-7
    state.transistors["M5"].parameters.vgs = 0.50
    state.global_parameters["vbias"] = 0.50

    class FakeOTASimulator:
        timeout_sec = 30.0

        def __init__(self):
            self.calls = 0

        def run(self, _netlist, work_dir=None, include_transient=False):
            self.calls += 1
            vbias = state.global_parameters.get("vbias", 0.0)
            load_l = state.transistors["M3"].parameters.L
            input_w = state.transistors["M1"].parameters.W
            input_l = state.transistors["M1"].parameters.L
            passing = vbias >= 0.56 and load_l >= 4.5e-7 and input_w >= 1.2e-6 and input_l >= 3.0e-7
            if passing:
                measurements = {
                    "dc_gain_db": 48.0,
                    "unity_gain_bandwidth": 1.2e8,
                    "phase_margin": 80.0,
                    "slew_rate": 8.0e7,
                    "output_swing": 0.75,
                    "total_power": 1.0e-4,
                }
                margin = 0.2
            else:
                measurements = {
                    "dc_gain_db": 24.0,
                    "unity_gain_bandwidth": 2.0e7,
                    "phase_margin": 80.0,
                    "slew_rate": 2.0e7,
                    "output_swing": 0.6,
                    "total_power": 5.0e-6,
                }
                margin = 0.1
            operating_points = {
                f"M{dev_id}": {"vds": margin + 0.12, "vdsat": 0.12, "gm": 1.0e-4, "gds": 1.0e-6, "id": 1.0e-5}
                for dev_id in ("M1", "M2", "M3", "M4", "M5")
            }
            return SimulationResult(success=True, return_code=0, measurements=measurements, operating_points=operating_points)

    sim = FakeOTASimulator()
    result = tune_single_stage_ota_operating_point(state, sim, tmp_path)

    assert result["spec_pass"] is True
    assert result["candidate_count"] > 1
    assert result["new_vbias"] >= 0.56
    assert result["load_length_scale"] > 1.0
    assert result["input_width_scale"] > 1.0
    assert result["input_length_scale"] > 1.0
    assert state.global_parameters["vbias"] == result["new_vbias"]


def test_single_stage_ota_postprocess_keeps_bandwidth_guardrail():
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"),
        default_environment(),
    )
    records = [
        {
            "phase": "gain",
            "vbias": 0.18,
            "load_length_scale": 2.0,
            "input_length_scale": 6.0,
            "input_width_scale": 2.3,
            "tail_width_scale": 0.8,
            "tail_length_scale": 2.0,
            "success": True,
            "spec_pass": False,
            "op_required_margin": 0.10,
            "measurements": {
                "dc_gain_db": 37.0,
                "unity_gain_bandwidth": 6.0e5,
                "phase_margin": 88.0,
                "total_power": 1.0e-7,
            },
            "score": 1.0,
        },
        {
            "phase": "bandwidth",
            "vbias": 0.46,
            "load_length_scale": 1.5,
            "input_length_scale": 3.0,
            "input_width_scale": 2.0,
            "tail_width_scale": 1.4,
            "tail_length_scale": 1.5,
            "success": True,
            "spec_pass": False,
            "op_required_margin": 0.09,
            "measurements": {
                "dc_gain_db": 35.5,
                "unity_gain_bandwidth": 6.0e7,
                "phase_margin": 82.0,
                "total_power": 8.0e-6,
            },
            "score": 2.0,
        },
        {
            "phase": "bandwidth",
            "vbias": 0.58,
            "load_length_scale": 1.0,
            "input_length_scale": 1.0,
            "input_width_scale": 1.0,
            "tail_width_scale": 2.0,
            "tail_length_scale": 1.0,
            "success": True,
            "spec_pass": False,
            "op_required_margin": 0.06,
            "measurements": {
                "dc_gain_db": 20.0,
                "unity_gain_bandwidth": 1.4e8,
                "phase_margin": 70.0,
                "total_power": 2.0e-5,
            },
            "score": 3.0,
        },
    ]

    selected = _select_two_phase_candidate(state, records)

    assert selected["phase"] == "bandwidth"
    assert selected["selected_phase"] == "bandwidth_guarded_after_gain"
    assert selected["measurements"]["unity_gain_bandwidth"] == 6.0e7
    assert selected["gain_anchor"]["unity_gain_bandwidth"] == 6.0e5
    assert selected["bandwidth_guard_floor"] > 0.0


def test_artifact_writer_emits_result_json(tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    result = SimulationResult(
        success=True,
        return_code=0,
        measurements={
            "dc_gain_db": 80.0,
            "unity_gain_bandwidth": 5.1e8,
            "phase_margin": 65.0,
            "slew_rate": 6.0e7,
            "output_swing": 0.85,
            "saturation_margin": 0.12,
            "icmr_min": 0.8,
            "icmr_max": 0.7,
            "total_power": 2e-4,
        },
    )
    best_meta = {
        "performance": {
            "dc_gain": 80.0,
            "unity_gain_bandwidth": 5.0e8,
            "phase_margin": 64.0,
            "slew_rate": 5.5e7,
            "output_swing": 0.85,
            "saturation_margin": 0.12,
            "icmr_min": 0.8,
            "icmr_max": 0.7,
            "power": 2.1e-4,
        },
        "decoded": {"__global__": {}},
        "loss_breakdown": {},
    }

    artifacts = ArtifactWriter(tmp_path).write(
        state=state,
        best_meta=best_meta,
        sim_result=result,
        iteration=1,
        netlist_str="* netlist\n.end\n",
        flow_meta={"options": {}, "validation": [{"summary": "verbose validation report"}]},
    )

    assert artifacts.design_state.exists()
    assert artifacts.netlist.exists()
    assert artifacts.causal_diagnostics.exists()
    design_state_text = artifacts.design_state.read_text(encoding="utf-8")
    assert "!!python" not in design_state_text
    assert "\n  simulation_log:" not in design_state_text
    assert "\n  agent_diagnostics:" not in design_state_text
    assert "verbose validation report" not in design_state_text
    payload = json.loads(artifacts.result_json.read_text(encoding="utf-8"))
    assert payload["status"]["spec_pass"] is True
    assert payload["data_alignment"]["aligned"] is True
    assert payload["data_alignment"]["missing_measurements"] == []
    assert payload["data_alignment"]["missing_required_measurements"] == []
    state_payload = load_yaml_mapping(artifacts.design_state)
    assert "diagnostics" in state_payload
    assert "process" not in state_payload
    assert "simulation" not in state_payload
    assert state_payload["diagnostics"]["result"]["status"]["spec_pass"] is True
    assert state_payload["diagnostics"]["result"]["measurements"]["saturation_margin"] == 0.12
    assert state_payload["diagnostics"]["result"]["data_alignment"]["aligned"] is True
    assert state_payload["diagnostics"]["contract"]["schema_role"] == "compact decision view"
    assert "phase_at_unity_meas" not in state_payload["diagnostics"]["result"]["measurements"]
    assert "simulation_log" not in state_payload["diagnostics"]
    assert "agent_diagnostics" not in state_payload["diagnostics"]
    assert "dependency_graph" not in state_payload["diagnostics"]["causal_diagnostics"]
    assert "local_intervention_model" not in state_payload["diagnostics"]["causal_diagnostics"]
    full_causal = json.loads(artifacts.causal_diagnostics.read_text(encoding="utf-8"))
    assert full_causal["dependency_graph"]["schema_version"] == "analogrf_ir.typed_causal_graph.v0_1"
    assert full_causal["dependency_graph"]["nodes"]
    typed_edge = full_causal["dependency_graph"]["edges"][0]
    assert typed_edge["schema_version"] == "analogrf_ir.typed_causal_edge.v0_1"
    assert typed_edge["source_type"]
    assert typed_edge["target_type"]
    assert typed_edge["typing"]["relation_type"] == typed_edge["edge_type"]


def test_priority_target_without_ngspice_measurement_is_unverified():
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"),
        default_environment(),
    )
    spec_model = SpecRegistry().select(state)
    target = state.targets["dc_gain"]

    missing = spec_model.target_status("dc_gain", target, {}, {})
    estimate_only = spec_model.target_status(
        "dc_gain",
        target,
        {},
        {"dc_gain": 80.0},
    )
    diagnostic = spec_model.target_status(
        "saturation_margin",
        state.targets["saturation_margin"],
        {"saturation_margin": 0.005},
        {},
    )

    assert missing["status"] == "unverified"
    assert missing["source"] == "missing"
    assert missing["requires_ngspice"] is True
    assert missing["counts_for_pass"] is True
    assert estimate_only["status"] == "unverified"
    assert estimate_only["model_status"] == "pass"
    assert estimate_only["source"] == "optimizer_estimate"
    assert diagnostic["status"] == "fail"
    assert diagnostic["counts_for_pass"] is True
    assert diagnostic["requires_ngspice"] is True


def test_design_state_simulation_roundtrip_preserves_bias_and_defaults():
    state = DesignState()
    state.simulation.bias_voltage = 0.72
    state.simulation.analyses = ["op", "ac", "dc", "tran"]

    restored = DesignState.from_dict(state.to_dict())

    assert restored.simulation.bias_voltage == 0.72
    assert restored.simulation.analyses == ["op", "ac", "dc", "tran"]
    assert DesignState.from_dict({}).simulation.analyses == ["op", "ac", "dc"]


def test_compact_telescopic_stack_balance_action_remains_executable():
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/telescopic/telescopic_ota_ihp130.yaml"),
        load_yaml_mapping("environment_ihp_sg13g2.yaml"),
    )
    action = {
        "action_id": "dc_gain_01_global_vbias_tail_set",
        "metric": "dc_gain",
        "cause_node": "device.M1.headroom",
        "priority": "primary",
        "action_class": "telescopic_stack_balance",
        "knob": "global.vbias_tail",
        "apply_to": ["global.vbias_tail", "global.vbias_ncas", "global.vbias_pcas"],
        "direction": "set",
        "current_value": {
            "global.vbias_tail": 0.7465,
            "global.vbias_ncas": 0.6457,
            "global.vbias_pcas": 0.35,
        },
        "per_knob_values": {
            "global.vbias_tail": 0.3000,
            "global.vbias_ncas": 0.8880,
            "global.vbias_pcas": 0.4320,
        },
        "optimizer_selected": True,
        "action_admissibility": {
            "schema_version": "analogrf_ir.formal_action_admissibility.v0_1",
            "formal_rule": "apply_allowed := optimizer_selected OR objective_delta < 0; guarded actions also require evidence_gate.passed",
            "passed": True,
            "conditions": {
                "has_optimizer_math": True,
                "optimizer_selected": True,
                "objective_delta_negative": True,
                "guarded_evidence_passed": True,
            },
            "objective_delta": -0.25,
            "reasons": ["formal admissibility predicate passed"],
        },
        "optimizer": {"objective_delta": -0.25, "optimizer_selected": True},
    }
    compact_action = _compact_tuning_action(action)
    state.diagnostics = {
        "schema_version": "analogrf_ir.state_diagnostics.v0_3",
        "causal_diagnostics": {
            "constrained_action_optimizer": {
                "candidate_actions": [compact_action],
                "selected_actions": [compact_action],
            },
            "attribution_guided_tuning": {
                "decision_model": {"type": "constrained_local_action_optimizer"},
                "by_failure": [{"metric": "dc_gain", "actions": [compact_action]}],
            },
        },
    }

    command = write_tuning_tool_command(state, round_index=1, allowed_priorities=["primary"])
    application = execute_tuning_tool_commands(state, round_index=1)

    assert compact_action["per_knob_values"] == action["per_knob_values"]
    assert command["args"]["selected_actions"][0]["action_id"] == action["action_id"]
    assert application["applied_actions"]
    applied = application["applied_actions"][0]["applied_knobs"]
    assert {item["knob"]: item["new_initial"] for item in applied} == action["per_knob_values"]
    assert state.global_parameters["vbias_ncas"] == 0.8880


def test_composite_action_accepts_per_knob_range_update():
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"),
        default_environment(),
    )
    action = {
        "action_id": "llm_hypothesis_001_custom_escape_gain_headroom_01",
        "metric": "dc_gain",
        "priority": "guarded",
        "action_class": "llm_exploratory_schema_patch",
        "knob": "M3.L",
        "apply_to": ["M3.L", "M4.L"],
        "direction": "set",
        "per_knob_values": {"M3.L": 8.0e-7, "M4.L": 8.0e-7},
        "range_update": {
            "type": "set_range",
            "per_knob": {
                "M3.L": {"min": 1.3e-7, "max": 1.0e-6},
                "M4.L": {"min": 1.3e-7, "max": 1.0e-6},
            },
        },
        "optimizer_selected": True,
        "action_admissibility": {"passed": True, "objective_delta": -0.1},
        "evidence_gate": {"passed": True, "objective_delta": -0.1},
        "rationale": "Validated LLM composite escape patch.",
    }
    state.diagnostics = {
        "schema_version": "analogrf_ir.state_diagnostics.v0_3",
        "causal_diagnostics": {
            "constrained_action_optimizer": {
                "candidate_actions": [action],
                "selected_actions": [action],
            },
            "attribution_guided_tuning": {
                "decision_model": {"type": "constrained_local_action_optimizer"},
                "by_failure": [{"metric": "dc_gain", "actions": [action]}],
            },
        },
    }

    command = write_tuning_tool_command(state, round_index=1, allowed_priorities=["guarded", "primary"])
    application = execute_tuning_tool_commands(state, round_index=1)

    assert command["args"]["selected_actions"][0]["action_id"] == action["action_id"]
    assert application["applied_actions"]
    applied = application["applied_actions"][0]["applied_knobs"]
    assert {item["knob"]: item["new_initial"] for item in applied} == action["per_knob_values"]
    m3_l = next(item for item in state.design_variables if item.device == "M3" and item.variable == "L")
    assert m3_l.range.max == pytest.approx(1.0e-6)
    assert m3_l.initial == pytest.approx(8.0e-7)


def test_composite_action_copies_symmetric_per_knob_targets():
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"),
        default_environment(),
    )
    action = {
        "action_id": "llm_hypothesis_001_residual_escape_composite_01",
        "metric": "saturation_margin",
        "priority": "guarded",
        "action_class": "llm_residual_escape_schema_patch",
        "knob": "M7.L",
        "apply_to": ["M7.L"],
        "direction": "set",
        "per_knob_values": {"M7.L": 1.5e-6},
        "range_update": {
            "type": "set_range",
            "per_knob": {"M7.L": {"min": 1.3e-7, "max": 1.5e-6}},
        },
        "optimizer_selected": True,
        "action_admissibility": {"passed": True, "objective_delta": -0.1},
        "evidence_gate": {"passed": True, "objective_delta": -0.1},
        "rationale": "Validated residual escape patch should preserve matched output-bias devices.",
    }
    state.diagnostics = {
        "schema_version": "analogrf_ir.state_diagnostics.v0_3",
        "causal_diagnostics": {
            "constrained_action_optimizer": {
                "candidate_actions": [action],
                "selected_actions": [action],
            },
            "attribution_guided_tuning": {
                "decision_model": {"type": "constrained_local_action_optimizer"},
                "by_failure": [{"metric": "saturation_margin", "actions": [action]}],
            },
        },
    }

    command = write_tuning_tool_command(state, round_index=1, allowed_priorities=["guarded", "primary"])
    application = execute_tuning_tool_commands(state, round_index=1)

    assert command["args"]["selected_actions"][0]["action_id"] == action["action_id"]
    assert application["applied_actions"]
    applied = application["applied_actions"][0]["applied_knobs"]
    applied_values = {item["knob"]: item["new_initial"] for item in applied}
    assert set(applied_values) == {"M7.L", "M9.L"}
    assert applied_values["M7.L"] == pytest.approx(1.5e-6)
    assert applied_values["M9.L"] == pytest.approx(1.5e-6)
    m7_l = next(item for item in state.design_variables if item.device == "M7" and item.variable == "L")
    m9_l = next(item for item in state.design_variables if item.device == "M9" and item.variable == "L")
    assert m7_l.initial == pytest.approx(m9_l.initial)
    assert m7_l.range.max == pytest.approx(m9_l.range.max)


def test_composite_action_rejects_missing_per_knob_values_without_fallback():
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"),
        default_environment(),
    )
    action = {
        "action_id": "llm_hypothesis_001_partial_composite",
        "metric": "saturation_margin",
        "priority": "guarded",
        "action_class": "llm_residual_escape_schema_patch",
        "knob": "M6.gm_id",
        "apply_to": ["M6.gm_id", "M3.gm_id", "M4.gm_id"],
        "direction": "increase",
        "per_knob_values": {"M3.gm_id": 8.0, "M4.gm_id": 8.0},
        "optimizer_selected": True,
        "action_admissibility": {"passed": True, "objective_delta": -0.1},
        "evidence_gate": {"passed": True, "objective_delta": -0.1},
    }
    state.diagnostics = {
        "schema_version": "analogrf_ir.state_diagnostics.v0_3",
        "causal_diagnostics": {
            "constrained_action_optimizer": {
                "candidate_actions": [action],
                "selected_actions": [action],
            },
            "attribution_guided_tuning": {
                "decision_model": {"type": "constrained_local_action_optimizer"},
                "by_failure": [{"metric": "saturation_margin", "actions": [action]}],
            },
        },
    }

    write_tuning_tool_command(state, round_index=1, allowed_priorities=["guarded", "primary"])
    application = execute_tuning_tool_commands(state, round_index=1)

    assert not application["applied_actions"]
    assert "per_knob_values missing target values" in application["skipped_actions"][0]["reason"]


def test_causal_diagnostics_rank_testable_root_causes_in_schema(tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    result = SimulationResult(
        success=True,
        return_code=0,
        measurements={
            "dc_gain_db": 80.0,
            "unity_gain_bandwidth": 9.0e7,
            "phase_margin": 18.0,
            "slew_rate": 5.0e7,
            "output_swing": 0.85,
            "saturation_margin": 0.12,
            "icmr_min": 0.8,
            "icmr_max": 0.7,
            "total_power": 2.0e-4,
        },
    )
    best_meta = {
        "performance": {
            "dc_gain": 80.0,
            "unity_gain_bandwidth": 9.0e7,
            "phase_margin": 18.0,
            "slew_rate": 5.0e7,
            "output_swing": 0.85,
            "saturation_margin": 0.12,
            "icmr_min": 0.8,
            "icmr_max": 0.7,
            "power": 2.0e-4,
        },
        "decoded": {"__global__": {}},
        "loss_breakdown": {"pm_deficit": 1.0},
    }

    artifacts = ArtifactWriter(tmp_path).write(
        state=state,
        best_meta=best_meta,
        sim_result=result,
        iteration=2,
        netlist_str="* netlist\n.end\n",
        flow_meta={"source_kind": "schema", "options": {}},
    )

    state_payload = load_yaml_mapping(artifacts.design_state)
    causal = json.loads(artifacts.causal_diagnostics.read_text(encoding="utf-8"))
    compact_causal = state_payload["diagnostics"]["causal_diagnostics"]
    result_view = state_payload["diagnostics"]["result"]

    assert "phase_margin" in result_view["status"]["failed_targets"]
    assert any(item["metric"] == "phase_margin" for item in causal["failure_symptom_analysis"])
    assert any(item["metric"] == "phase_margin" for item in compact_causal["failure_symptom_analysis"])
    assert "suggested_validation_experiments" not in compact_causal
    assert "dependency_graph" not in compact_causal
    assert "agent_failure_attribution" not in compact_causal
    assert "A" not in compact_causal.get("local_intervention_summary", {})
    assert causal["root_cause_attribution"]
    assert causal["agent_failure_attribution"]["by_failure"]
    assert compact_causal["attribution_guided_tuning"]["by_failure"]
    assert any(
        action["knob"] == "global.Rz" and action["target_formula"] == "1/gm(second_stage_gain)"
        for item in compact_causal["attribution_guided_tuning"]["by_failure"]
        for action in item["actions"]
    )
    assert any(
        action["knob"] == "global.Rz" and action["action_class"] == "compensation"
        for item in compact_causal["attribution_guided_tuning"]["by_failure"]
        for action in item["actions"]
    )
    assert causal["counterfactual_predictions"]
    assert causal["suggested_validation_experiments"][0]["sweep"] == ["-10%", "-5%", "+5%", "+10%"]


def test_causal_attribution_keeps_tail_source_out_of_direct_gain_load_path(tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    result = SimulationResult(
        success=True,
        return_code=0,
        measurements={
            "dc_gain_db": 20.0,
            "unity_gain_bandwidth": 2.0e8,
            "phase_margin": 75.0,
            "slew_rate": 5.0e7,
            "output_swing": 0.82,
            "saturation_margin": 0.12,
            "icmr_min": 0.63,
            "icmr_max": 1.26,
            "total_power": 5.0e-5,
        },
    )
    best_meta = {
        "performance": {
            "dc_gain": 20.0,
            "unity_gain_bandwidth": 2.0e8,
            "phase_margin": 75.0,
            "slew_rate": 5.0e7,
            "output_swing": 0.82,
            "saturation_margin": 0.12,
            "icmr_min": 0.63,
            "icmr_max": 1.26,
            "power": 5.0e-5,
        },
        "decoded": {"__global__": {}},
        "loss_breakdown": {"gain_deficit": 1.0},
    }
    apply_optimizer_meta_to_state(
        state,
        {
            "decoded": {"__global__": {}},
            "transistor_params": {
                "M1": {"gm": 3.0e-4, "gds": 5.0e-6, "id": 2.0e-5, "vds": 0.53, "vdsat": 0.08, "region": "saturation"},
                "M2": {"gm": 3.0e-4, "gds": 5.0e-6, "id": 2.0e-5, "vds": 0.53, "vdsat": 0.08, "region": "saturation"},
                "M3": {"gm": 2.0e-4, "gds": 4.0e-5, "id": 2.0e-5, "vds": 0.51, "vdsat": 0.20, "region": "saturation"},
                "M4": {"gm": 2.0e-4, "gds": 3.5e-5, "id": 2.0e-5, "vds": 0.51, "vdsat": 0.20, "region": "saturation"},
                "M5": {"gm": 3.0e-4, "gds": 8.0e-5, "id": 4.0e-5, "vds": 0.15, "vdsat": 0.145, "region": "saturation"},
            },
        },
    )

    artifacts = ArtifactWriter(tmp_path).write(
        state=state,
        best_meta=best_meta,
        sim_result=result,
        iteration=3,
        netlist_str="* netlist\n.end\n",
        flow_meta={"source_kind": "schema", "options": {}},
    )

    causal = load_yaml_mapping(artifacts.design_state)["diagnostics"]["causal_diagnostics"]
    full_causal = json.loads(artifacts.causal_diagnostics.read_text(encoding="utf-8"))
    top = causal["root_cause_attribution"][0]
    gain_path = full_causal["causal_paths"][0]["chain"]
    tuning_actions = causal["attribution_guided_tuning"]["by_failure"][0]["actions"]
    primary_action = tuning_actions[0]

    assert causal["attribution_guided_tuning"]["planning_mode"] in {"coarse", "fine"}
    assert primary_action["tuning_mode"] in {"coarse", "fine"}
    assert top["node"] != "device.M5.ro"
    assert "block.load_stage" in gain_path
    assert "device.M5.ro" not in gain_path
    assert full_causal["agent_failure_attribution"]["by_failure"][0]["minimal_causal_factor_set"]
    assert primary_action["knob"] in {"M3.L", "M4.L"}
    assert primary_action["action_id"]
    assert primary_action["direction"] == "increase"
    assert primary_action["apply_to"] == ["M3.L", "M4.L"]
    assert top["score_components"]["intervention"] > 0
    assert top["propagation_path"]
    assert causal["sensitivity_ranking_comparison"]["decision_rule"] == "causal_graph_ranking"
    assert primary_action["range_update"]["type"] == "expand_upper_bound"
    assert full_causal["agent_failure_attribution"]["by_failure"][0]["tuning_plan"][0]["knob"] in {"M3.L", "M4.L"}

    application = apply_attribution_guided_tuning(state, round_index=1)
    assert application["applied_actions"]
    m3_l = next(dv for dv in state.design_variables if dv.device == "M3" and dv.variable == "L")
    m4_l = next(dv for dv in state.design_variables if dv.device == "M4" and dv.variable == "L")
    m1_gm_id = next(dv for dv in state.design_variables if dv.device == "M1" and dv.variable == "gm_id")
    assert m3_l.range.max > 5.0e-7
    assert m3_l.initial > 5.0e-7
    assert m4_l.initial == m3_l.initial
    assert m1_gm_id.initial == 15.0


def test_gain_length_action_is_guarded_when_bandwidth_also_fails(tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    result = SimulationResult(
        success=True,
        return_code=0,
        measurements={
            "dc_gain_db": 20.0,
            "unity_gain_bandwidth": 1.0e6,
            "phase_margin": 75.0,
            "slew_rate": 5.0e7,
            "output_swing": 0.82,
            "saturation_margin": 0.12,
            "icmr_min": 0.63,
            "icmr_max": 1.26,
            "total_power": 5.0e-5,
        },
    )
    best_meta = {
        "performance": {
            "dc_gain": 20.0,
                "unity_gain_bandwidth": 1.0e6,
            "phase_margin": 75.0,
            "slew_rate": 5.0e7,
            "output_swing": 0.82,
            "saturation_margin": 0.12,
            "icmr_min": 0.63,
            "icmr_max": 1.26,
            "power": 5.0e-5,
        },
        "decoded": {"__global__": {}},
        "loss_breakdown": {"gain_deficit": 1.0, "bw_deficit": 1.0},
    }
    apply_optimizer_meta_to_state(
        state,
        {
            "decoded": {"__global__": {}},
            "transistor_params": {
                "M1": {"gm": 3.0e-4, "gds": 5.0e-6, "id": 2.0e-5, "vds": 0.53, "vdsat": 0.08, "region": "saturation"},
                "M2": {"gm": 3.0e-4, "gds": 5.0e-6, "id": 2.0e-5, "vds": 0.53, "vdsat": 0.08, "region": "saturation"},
                "M3": {"gm": 2.0e-4, "gds": 4.0e-5, "id": 2.0e-5, "vds": 0.51, "vdsat": 0.20, "region": "saturation"},
                "M4": {"gm": 2.0e-4, "gds": 3.5e-5, "id": 2.0e-5, "vds": 0.51, "vdsat": 0.20, "region": "saturation"},
                "M5": {"gm": 3.0e-4, "gds": 8.0e-5, "id": 4.0e-5, "vds": 0.15, "vdsat": 0.145, "region": "saturation"},
            },
        },
    )

    artifacts = ArtifactWriter(tmp_path).write(
        state=state,
        best_meta=best_meta,
        sim_result=result,
        iteration=5,
        netlist_str="* netlist\n.end\n",
        flow_meta={"source_kind": "schema", "options": {}},
    )
    causal = load_yaml_mapping(artifacts.design_state)["diagnostics"]["causal_diagnostics"]
    gain_failure = next(item for item in causal["attribution_guided_tuning"]["by_failure"] if item["metric"] == "dc_gain")
    gain_length_actions = [
        action
        for action in gain_failure["actions"]
        if action["direction"] == "increase" and action["knob"].endswith(".L")
    ]

    assert gain_length_actions
    assert all(action["priority"] == "secondary" for action in gain_length_actions)
    assert all(action["max_step_fraction"] <= 0.10 for action in gain_length_actions)
    assert all(action["multi_objective_guardrail"]["policy"].startswith("small secondary L increase") for action in gain_length_actions)
    assert any(action["knob"] == "M1.gm_id" and action["priority"] == "primary" for action in gain_failure["actions"])


def test_gain_only_failure_prioritizes_rout_length_actions(tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    result = SimulationResult(
        success=True,
        return_code=0,
        measurements={
            "dc_gain_db": 20.0,
            "unity_gain_bandwidth": 8.0e7,
            "phase_margin": 63.0,
            "slew_rate": 5.0e7,
            "output_swing": 0.82,
            "saturation_margin": 0.12,
            "icmr_min": 0.63,
            "icmr_max": 1.26,
            "total_power": 5.0e-5,
        },
    )
    best_meta = {
        "performance": {
            "dc_gain": 20.0,
            "unity_gain_bandwidth": 8.0e7,
            "phase_margin": 63.0,
            "slew_rate": 5.0e7,
            "output_swing": 0.82,
            "saturation_margin": 0.12,
            "icmr_min": 0.63,
            "icmr_max": 1.26,
            "power": 5.0e-5,
        },
        "decoded": {"__global__": {}},
        "loss_breakdown": {"gain_deficit": 1.0},
    }
    apply_optimizer_meta_to_state(
        state,
        {
            "decoded": {"__global__": {}},
            "transistor_params": {
                "M1": {"gm": 3.0e-4, "gds": 5.0e-6, "id": 2.0e-5, "vds": 0.53, "vdsat": 0.08, "region": "saturation"},
                "M2": {"gm": 3.0e-4, "gds": 5.0e-6, "id": 2.0e-5, "vds": 0.53, "vdsat": 0.08, "region": "saturation"},
                "M3": {"gm": 2.0e-4, "gds": 4.0e-5, "id": 2.0e-5, "vds": 0.51, "vdsat": 0.20, "region": "saturation"},
                "M4": {"gm": 2.0e-4, "gds": 3.5e-5, "id": 2.0e-5, "vds": 0.51, "vdsat": 0.20, "region": "saturation"},
                "M5": {"gm": 3.0e-4, "gds": 8.0e-5, "id": 4.0e-5, "vds": 0.15, "vdsat": 0.12, "region": "saturation"},
            },
        },
    )

    artifacts = ArtifactWriter(tmp_path).write(
        state=state,
        best_meta=best_meta,
        sim_result=result,
        iteration=6,
        netlist_str="* netlist\n.end\n",
        flow_meta={"source_kind": "schema", "options": {}},
    )
    causal = load_yaml_mapping(artifacts.design_state)["diagnostics"]["causal_diagnostics"]
    gain_failure = next(item for item in causal["attribution_guided_tuning"]["by_failure"] if item["metric"] == "dc_gain")
    length_actions = [
        action
        for action in gain_failure["actions"]
        if action["direction"] == "increase" and action["knob"].endswith(".L")
    ]

    assert length_actions
    policy_actions = [action for action in length_actions if action.get("gain_only_ro_policy")]
    assert policy_actions
    assert all(action["priority"] == "primary" for action in policy_actions)
    assert all(action["min_step_fraction"] >= 0.14 for action in policy_actions)
    assert all(action["max_step_fraction"] >= 0.30 for action in policy_actions)


def test_optimizer_state_update_synchronizes_design_variable_initials():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/current_mirror/current_mirror_ota_ihp130.yaml"), default_environment())
    m3_l = next(dv for dv in state.design_variables if dv.device == "M3" and dv.variable == "L")
    i_tail = next(dv for dv in state.design_variables if not dv.device and dv.variable == "I_tail")
    best_meta = {
        "decoded": {
            "__global__": {"I_tail": 9.0e-5, "vbias": 0.55},
            "M1": {"gm_id": 16.0, "L": 8.0e-7},
            "M2": {"gm_id": 16.0, "L": 8.0e-7},
            "M3": {"gm_id": 8.0, "L": 1.5e-6},
            "M4": {"gm_id": 8.0, "L": 1.5e-6},
            "M5": {"gm_id": 10.0, "L": 4.0e-7},
        },
        "transistor_params": {
            "M3": {"W": 10e-6, "L": 1.5e-6, "gm": 1e-4, "gds": 1e-6, "id": 4.5e-5, "vds": 0.5, "vdsat": 0.1},
            "M4": {"W": 10e-6, "L": 1.5e-6, "gm": 1e-4, "gds": 1e-6, "id": 4.5e-5, "vds": 0.5, "vdsat": 0.1},
        },
    }

    apply_optimizer_meta_to_state(state, best_meta)

    assert m3_l.initial == 1.5e-6
    assert i_tail.initial == 9.0e-5
    assert state.global_parameters["I_tail"] == 9.0e-5


def test_cascode_gain_plan_exposes_typed_bias_voltage_actions(tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/folded_cascode/folded_cascode_ota_ihp130.yaml"), default_environment())
    result = SimulationResult(
        success=True,
        return_code=0,
        measurements={
            "dc_gain_db": 30.0,
            "unity_gain_bandwidth": 3.0e7,
            "phase_margin": 70.0,
            "slew_rate": 2.0e7,
            "output_swing": 0.65,
            "saturation_margin": 0.12,
            "total_power": 8.0e-6,
        },
    )
    best_meta = {
        "performance": {
            "dc_gain": 30.0,
            "unity_gain_bandwidth": 3.0e7,
            "phase_margin": 70.0,
            "slew_rate": 2.0e7,
            "output_swing": 0.65,
            "saturation_margin": 0.12,
            "power": 8.0e-6,
        },
        "decoded": {"__global__": {}},
        "loss_breakdown": {"gain_deficit": 1.0},
    }
    apply_optimizer_meta_to_state(
        state,
        {
            "decoded": {"__global__": {}},
            "transistor_params": {
                "M1": {"gm": 1.5e-4, "gds": 8.0e-6, "id": 8.0e-6, "vds": 0.20, "vdsat": 0.10, "region": "saturation"},
                "M2": {"gm": 1.5e-4, "gds": 8.0e-6, "id": 8.0e-6, "vds": 0.20, "vdsat": 0.10, "region": "saturation"},
                "M3": {"gm": 1.3e-4, "gds": 5.0e-5, "id": 8.0e-6, "vds": 0.18, "vdsat": 0.16, "region": "saturation"},
                "M4": {"gm": 1.3e-4, "gds": 5.0e-5, "id": 8.0e-6, "vds": 0.18, "vdsat": 0.16, "region": "saturation"},
                "M5": {"gm": 1.2e-4, "gds": 2.0e-5, "id": 1.6e-5, "vds": 0.12, "vdsat": 0.15, "region": "linear"},
                "M6": {"gm": 1.1e-4, "gds": 6.0e-5, "id": 8.0e-6, "vds": 0.08, "vdsat": 0.18, "region": "linear"},
                "M7": {"gm": 1.1e-4, "gds": 6.0e-5, "id": 8.0e-6, "vds": 0.08, "vdsat": 0.18, "region": "linear"},
            },
        },
    )

    artifacts = ArtifactWriter(tmp_path).write(
        state=state,
        best_meta=best_meta,
        sim_result=result,
        iteration=1,
        netlist_str="* netlist\n.end\n",
        flow_meta={"source_kind": "schema", "options": {}},
    )
    causal = load_yaml_mapping(artifacts.design_state)["diagnostics"]["causal_diagnostics"]
    gain_failure = next(item for item in causal["attribution_guided_tuning"]["by_failure"] if item["metric"] == "dc_gain")
    bias_actions = [action for action in gain_failure["actions"] if action["knob"].startswith("global.vbias")]

    assert bias_actions
    assert all(action["direction"] == "set" for action in bias_actions)
    assert all(action["action_class"] == "operating_point_headroom" for action in bias_actions)
    assert all(action["target_formula"] == "folded_cascode_topology_guided_bias_search" for action in bias_actions)


def test_current_mirror_gain_plan_includes_composite_headroom_gain_action():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/current_mirror/current_mirror_ota_ihp130.yaml"), default_environment())
    apply_optimizer_meta_to_state(
        state,
        {
            "decoded": {
                "__global__": {"I_tail": 1.0e-4, "vbias": 0.52},
                "M1": {"gm_id": 10.4, "L": 8.4e-7},
                "M2": {"gm_id": 10.4, "L": 8.4e-7},
                "M3": {"gm_id": 9.2, "L": 1.85e-6},
                "M4": {"gm_id": 9.2, "L": 1.85e-6},
                "M5": {"gm_id": 7.0, "L": 1.85e-7},
            },
            "transistor_params": {
                "M1": {"gm": 6.0e-4, "gds": 2.0e-5, "id": 5.0e-5, "vds": 0.45, "vdsat": 0.20, "region": "saturation"},
                "M2": {"gm": 6.0e-4, "gds": 2.0e-5, "id": 5.0e-5, "vds": 0.45, "vdsat": 0.20, "region": "saturation"},
                "M3": {"gm": 2.0e-4, "gds": 3.0e-5, "id": 5.0e-5, "vds": 0.60, "vdsat": 0.24, "region": "saturation"},
                "M4": {"gm": 2.0e-4, "gds": 3.0e-5, "id": 5.0e-5, "vds": 0.60, "vdsat": 0.24, "region": "saturation"},
                "M5": {"gm": 1.5e-4, "gds": 5.0e-5, "id": 1.0e-4, "vds": 0.14, "vdsat": 0.19, "region": "linear"},
            },
        },
    )
    spec_model = SpecRegistry().select(state)
    result = SimulationResult(
        success=False,
        return_code=1,
        measurements={
            "dc_gain_db": 27.5,
            "unity_gain_bandwidth": 6.0e7,
            "phase_margin": 70.0,
            "slew_rate": 8.0e7,
            "output_swing": 0.65,
            "saturation_margin": -0.04,
            "total_power": 1.2e-4,
        },
    )
    target_status = {
        name: spec_model.target_status(name, target, result.measurements, {})
        for name, target in state.targets.items()
    }

    causal = build_causal_diagnostics(state, {}, result, target_status, spec_model, {"local_intervention_model": {}})
    gain_failure = next(item for item in causal["attribution_guided_tuning"]["by_failure"] if item["metric"] == "dc_gain")
    composite = [action for action in gain_failure["actions"] if action["action_class"] == "operating_point_headroom_gain"]

    assert composite
    action = composite[0]
    assert action["priority"] == "primary"
    assert action["target_formula"] == "single_stage_topology_guided_headroom_gain_search"
    assert {"global.vbias", "global.I_tail", "M1.L", "M2.L", "M3.L", "M4.L", "M5.L"}.issubset(set(action["apply_to"]))
    assert action["per_knob_values"]["global.I_tail"] < state.global_parameters["I_tail"]


def test_spice_intervention_model_builds_local_A_matrix(tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    spec_model = SpecRegistry().select(state)
    base_measurements = {
        "dc_gain_db": 58.0,
        "unity_gain_bandwidth": 2.0e6,
        "phase_margin": 60.0,
        "slew_rate": 3.0e7,
        "output_swing": 0.7,
        "saturation_margin": 0.12,
        "total_power": 2.0e-4,
    }
    target_status = {
        name: spec_model.target_status(name, target, base_measurements, {})
        for name, target in state.targets.items()
    }
    cc = state.global_parameters["Cc"]
    tuning = {
        "by_failure": [
            {
                "metric": "unity_gain_bandwidth",
                "actions": [
                    {
                        "action_id": "ugbw_01_global_Cc_decrease",
                        "metric": "unity_gain_bandwidth",
                        "rank": 1,
                        "priority": "primary",
                        "knob": "global.Cc",
                        "apply_to": ["global.Cc"],
                        "direction": "decrease",
                        "current_value": cc,
                        "suggested_unclipped_value": cc * 0.75,
                        "expected_effect": {
                            "unity_gain_bandwidth": "increase",
                            "slew_rate": "increase",
                            "phase_margin": "decrease",
                        },
                    }
                ],
            }
        ]
    }

    class FakeInterventionSim:
        def run(self, _netlist, work_dir=None, include_transient=False):
            return SimulationResult(
                success=True,
                return_code=0,
                measurements={
                    "dc_gain_db": 58.0,
                    "unity_gain_bandwidth": 1.2e8,
                    "phase_margin": 54.0,
                    "slew_rate": 8.0e7,
                    "output_swing": 0.7,
                    "saturation_margin": 0.12,
                    "total_power": 2.0e-4,
                },
            )

    model = build_spice_intervention_model(
        state=state,
        sim=FakeInterventionSim(),
        work_dir=tmp_path,
        spec_model=spec_model,
        target_status=target_status,
        tuning=tuning,
        max_actions=1,
    )

    assert model["method"] == "spice_small_perturbation"
    assert model["A"]["columns"] == ["ugbw_01_global_Cc_decrease"]
    row = model["A"]["rows"].index("unity_gain_bandwidth")
    assert model["A"]["values"][row][0] < 0
    effect = model["action_effects"][0]
    assert effect["source"] == "spice_small_perturbation"
    assert effect["violation_reduction"] > 0


def test_spice_intervention_model_keeps_usable_warning_measurements(tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    spec_model = SpecRegistry().select(state)
    base_measurements = {
        "dc_gain_db": 20.0,
        "unity_gain_bandwidth": 8.0e7,
        "phase_margin": 62.0,
        "slew_rate": 5.0e7,
        "output_swing": 0.75,
        "saturation_margin": 0.12,
        "total_power": 5.0e-5,
    }
    target_status = {
        name: spec_model.target_status(name, target, base_measurements, {})
        for name, target in state.targets.items()
    }
    tuning = {
        "by_failure": [
            {
                "metric": "dc_gain",
                "actions": [
                    {
                        "action_id": "gain_01_M3_L_increase",
                        "metric": "dc_gain",
                        "rank": 1,
                        "priority": "primary",
                        "knob": "M3.L",
                        "apply_to": ["M3.L", "M4.L"],
                        "direction": "increase",
                        "suggested_unclipped_value": 6.0e-7,
                        "expected_effect": {"dc_gain": "increase", "unity_gain_bandwidth": "may decrease"},
                    }
                ],
            }
        ]
    }

    class WarningInterventionSim:
        def run(self, _netlist, work_dir=None, include_transient=False):
            return SimulationResult(
                success=False,
                return_code=0,
                measurements={
                    "dc_gain_db": 26.0,
                    "unity_gain_bandwidth": 6.0e7,
                    "phase_margin": 62.0,
                    "slew_rate": 4.0e7,
                    "output_swing": 0.75,
                    "saturation_margin": 0.12,
                    "total_power": 5.0e-5,
                },
            )

    model = build_spice_intervention_model(
        state=state,
        sim=WarningInterventionSim(),
        work_dir=tmp_path,
        spec_model=spec_model,
        target_status=target_status,
        tuning=tuning,
        max_actions=1,
    )
    effect = model["action_effects"][0]

    assert effect["status"] == "ok"
    assert effect["uncertainty"] == 0.25
    assert effect["delta_violation_vector"]["dc_gain"] < 0.0


def test_constrained_optimizer_selects_action_with_best_local_model_support():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    target_status = {
        "unity_gain_bandwidth": {
            "status": "fail",
            "value": 5.0e7,
            "min": 1.0e8,
            "max": None,
            "priority": 1,
        },
        "phase_margin": {
            "status": "pass",
            "value": 60.0,
            "min": 50.0,
            "max": None,
            "priority": 1,
        },
    }
    tuning = {
        "author": "unit",
        "by_failure": [
            {
                "metric": "unity_gain_bandwidth",
                "actions": [
                    {
                        "action_id": "bad_Cc_increase",
                        "metric": "unity_gain_bandwidth",
                        "rank": 1,
                        "priority": "primary",
                        "knob": "global.Cc",
                        "apply_to": ["global.Cc"],
                        "direction": "increase",
                        "expected_effect": {"unity_gain_bandwidth": "decrease"},
                    },
                    {
                        "action_id": "good_Cc_decrease",
                        "metric": "unity_gain_bandwidth",
                        "rank": 2,
                        "priority": "primary",
                        "knob": "global.Cc",
                        "apply_to": ["global.Cc"],
                        "direction": "decrease",
                        "expected_effect": {"unity_gain_bandwidth": "increase"},
                    },
                ],
            }
        ],
    }
    intervention_model = {
        "base_violation_vector": {"unity_gain_bandwidth": 0.5, "phase_margin": 0.0},
        "action_effects": [
            {
                "action_id": "bad_Cc_increase",
                "status": "ok",
                "source": "spice_small_perturbation",
                "delta_violation_vector": {"unity_gain_bandwidth": 0.2, "phase_margin": 0.0},
                "uncertainty": 0.1,
            },
            {
                "action_id": "good_Cc_decrease",
                "status": "ok",
                "source": "spice_small_perturbation",
                "delta_violation_vector": {"unity_gain_bandwidth": -0.35, "phase_margin": 0.0},
                "uncertainty": 0.1,
            },
        ],
    }

    optimizer = optimize_tuning_actions(
        tuning=tuning,
        target_status=target_status,
        intervention_model=intervention_model,
    )
    optimized_tuning = apply_optimized_action_plan(tuning, optimizer)
    state.diagnostics["causal_diagnostics"] = {
        "constrained_action_optimizer": optimizer,
        "attribution_guided_tuning": optimized_tuning,
    }
    command = write_tuning_tool_command(state, round_index=1)

    assert optimizer["status"] == "ok"
    assert optimizer["selected_actions"][0]["action_id"] == "good_Cc_decrease"
    assert optimized_tuning["decision_model"]["selected_action_ids"] == ["good_Cc_decrease"]
    assert command["args"]["selected_actions"][0]["action_id"] == "good_Cc_decrease"


def test_constrained_optimizer_downweights_advisory_targets_for_action_authority():
    target_status = {
        "dc_gain": {
            "status": "fail",
            "value": 44.9,
            "min": 45.0,
            "max": None,
            "priority": 1,
            "counts_for_pass": True,
        },
        "advisory_margin": {
            "status": "fail",
            "value": -0.02,
            "min": 0.01,
            "max": None,
            "priority": 3,
            "counts_for_pass": False,
        },
    }
    tuning = {
        "by_failure": [
            {
                "metric": "dc_gain",
                "actions": [
                    {
                        "action_id": "gain_L_probe",
                        "metric": "dc_gain",
                        "rank": 1,
                        "priority": "primary",
                        "knob": "M6.L",
                        "apply_to": ["M6.L", "M7.L"],
                        "direction": "increase",
                        "expected_effect": {"dc_gain": "increase"},
                    }
                ],
            }
        ]
    }
    intervention_model = {
        "base_violation_vector": {"dc_gain": 0.08, "advisory_margin": 3.0},
        "action_effects": [
            {
                "action_id": "gain_L_probe",
                "status": "ok",
                "source": "spice_small_perturbation",
                "delta_violation_vector": {"dc_gain": -0.06, "advisory_margin": 0.5},
                "uncertainty": 0.1,
            }
        ],
    }

    optimizer = optimize_tuning_actions(
        tuning=tuning,
        target_status=target_status,
        intervention_model=intervention_model,
    )

    assert optimizer["status"] == "ok"
    assert optimizer["selected_actions"][0]["action_id"] == "gain_L_probe"
    assert optimizer["selected_actions"][0]["objective_delta"] < 0


def test_formal_objective_delta_excludes_action_selection_penalty():
    target_status = {
        "dc_gain": {
            "status": "fail",
            "value": 44.95,
            "min": 45.0,
            "max": None,
            "priority": 1,
            "counts_for_pass": True,
        },
        "unity_gain_bandwidth": {
            "status": "fail",
            "value": 99.0e6,
            "min": 100.0e6,
            "max": None,
            "priority": 1,
            "counts_for_pass": True,
        }
    }
    tuning = {
        "by_failure": [
            {
                "metric": "dc_gain",
                "actions": [
                    {
                        "action_id": "near_feasible_gain_headroom_probe",
                        "metric": "dc_gain",
                        "rank": 1,
                        "priority": "primary",
                        "action_class": "operating_point_headroom",
                        "knob": "M3.gm_id",
                        "apply_to": ["M3.gm_id", "M4.gm_id"],
                        "direction": "increase",
                        "expected_effect": {"dc_gain": "increase", "headroom": "improve"},
                    }
                ],
            }
        ]
    }
    intervention_model = {
        "base_violation_vector": {"dc_gain": 0.01, "unity_gain_bandwidth": 0.01},
        "action_effects": [
            {
                "action_id": "near_feasible_gain_headroom_probe",
                "status": "ok",
                "source": "spice_small_perturbation",
                "delta_violation_vector": {"dc_gain": -0.006, "unity_gain_bandwidth": 0.001},
                "uncertainty": 0.1,
            }
        ],
    }

    optimizer = optimize_tuning_actions(
        tuning=tuning,
        target_status=target_status,
        intervention_model=intervention_model,
    )
    candidate = optimizer["candidate_actions"][0]

    assert candidate["objective_delta"] < 0
    assert candidate["penalized_objective_delta"] > 0
    assert optimizer["selected_actions"][0]["action_id"] == "near_feasible_gain_headroom_probe"
    assert optimizer["selected_actions"][0]["action_admissibility"]["passed"] is True


def test_surrogate_only_negative_delta_is_not_auto_selected_as_admissible():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    action = {
        "action_id": "dc_gain_01_M3_L_increase",
        "metric": "dc_gain",
        "rank": 1,
        "priority": "primary",
        "action_class": "gain_pole_tradeoff",
        "knob": "M3.L",
        "apply_to": ["M3.L", "M4.L"],
        "direction": "increase",
        "suggested_next_value": 5.0e-7,
        "objective_delta": -0.01,
        "local_model_source": "surrogate_structural_prior",
        "action_admissibility": {"passed": True},
    }
    state.diagnostics["causal_diagnostics"] = {
        "constrained_action_optimizer": {
            "status": "no_improving_combination",
            "selected_actions": [],
            "candidate_actions": [action],
        },
        "attribution_guided_tuning": {"by_failure": [{"metric": "dc_gain", "actions": [action]}]},
    }

    command = write_tuning_tool_command(state, round_index=1)

    assert command["args"]["selected_actions"] == []


def test_local_intervention_budget_keeps_primary_actions_before_guarded_fill():
    tuning = {
        "by_failure": [
            {
                "metric": "dc_gain",
                "actions": [
                    {"action_id": "a1", "priority": "primary", "rank": 1, "knob": "global.vbias", "direction": "set"},
                    {"action_id": "a2", "priority": "primary", "rank": 2, "knob": "M2.L", "direction": "increase"},
                    {"action_id": "a3", "priority": "primary", "rank": 3, "knob": "M1.gm_id", "direction": "increase"},
                    {"action_id": "a4", "priority": "primary", "rank": 4, "knob": "M4.L", "direction": "increase"},
                    {"action_id": "guarded", "priority": "guarded", "rank": 5, "knob": "M5.gm_id", "direction": "increase"},
                ],
            },
            {
                "metric": "saturation_margin",
                "actions": [
                    {"action_id": "sat1", "priority": "primary", "rank": 1, "knob": "global.vbias", "direction": "set"},
                    {"action_id": "sat2", "priority": "primary", "rank": 2, "knob": "M1.gm_id", "direction": "increase"},
                    {"action_id": "sat3", "priority": "primary", "rank": 3, "knob": "M3.gm_id", "direction": "increase"},
                ],
            }
        ]
    }

    selected = _candidate_actions(tuning, limit=4)

    assert [item["action_id"] for item in selected] == ["a1", "a2", "a3", "a4"]


def test_local_intervention_keeps_baseline_status_for_unmeasured_metrics():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/current_mirror/current_mirror_ota_ihp130.yaml"), default_environment())
    spec_model = SpecRegistry().select(state)
    baseline_measurements = {
        "dc_gain_db": 27.5,
        "unity_gain_bandwidth": 6.0e7,
        "phase_margin": 70.0,
        "slew_rate": 8.0e7,
        "output_swing": 0.65,
        "saturation_margin": -0.04,
        "total_power": 1.2e-4,
    }
    baseline_status = {
        name: spec_model.target_status(name, target, baseline_measurements, {})
        for name, target in state.targets.items()
    }
    ac_only_measurements = {
        "dc_gain_db": 28.6,
        "unity_gain_bandwidth": 4.3e7,
        "phase_margin": 69.0,
        "output_swing": 0.64,
        "saturation_margin": -0.09,
        "total_power": 1.1e-4,
    }

    after_status = _target_status_with_measured_updates(
        spec_model=spec_model,
        targets=state.targets,
        measurements=ac_only_measurements,
        baseline_status=baseline_status,
    )

    assert baseline_status["slew_rate"]["status"] == "pass"
    assert after_status["slew_rate"] == baseline_status["slew_rate"]
    assert after_status["dc_gain"]["value"] == 28.6


def test_guarded_action_requires_passing_spice_evidence_gate():
    target_status = {
        "dc_gain": {
            "status": "fail",
            "value": 30.0,
            "min": 60.0,
            "max": None,
            "priority": 1,
        }
    }
    tuning = {
        "by_failure": [
            {
                "metric": "dc_gain",
                "actions": [
                    {
                        "action_id": "guarded_input_gm_id",
                        "metric": "dc_gain",
                        "rank": 1,
                        "priority": "guarded",
                        "knob": "M1.gm_id",
                        "apply_to": ["M1.gm_id", "M2.gm_id"],
                        "direction": "increase",
                        "expected_effect": {"dc_gain": "increase"},
                    }
                ],
            }
        ],
    }

    optimizer = optimize_tuning_actions(tuning=tuning, target_status=target_status)
    candidate = optimizer["candidate_actions"][0]

    assert optimizer["selected_actions"] == []
    assert candidate["evidence_gate"]["required"] is True
    assert candidate["evidence_gate"]["passed"] is False
    assert candidate["evidence_gate"]["conditions"]["spice_local_intervention"] is False


def test_llm_apply_is_rejected_when_optimizer_math_gate_fails():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    m1_gm_id = next(dv for dv in state.design_variables if dv.device == "M1" and dv.variable == "gm_id")
    original = m1_gm_id.initial
    target_status = {
        "dc_gain": {
            "status": "fail",
            "value": 30.0,
            "min": 60.0,
            "max": None,
            "priority": 1,
        }
    }
    tuning = {
        "by_failure": [
            {
                "metric": "dc_gain",
                "actions": [
                    {
                        "action_id": "bad_input_gm_id_decrease",
                        "metric": "dc_gain",
                        "rank": 1,
                        "priority": "primary",
                        "action_class": "transconductance_bias",
                        "knob": "M1.gm_id",
                        "apply_to": ["M1.gm_id", "M2.gm_id"],
                        "direction": "decrease",
                        "suggested_unclipped_value": 12.0,
                        "expected_effect": {"dc_gain": "decrease"},
                    }
                ],
            }
        ],
    }

    optimizer = optimize_tuning_actions(tuning=tuning, target_status=target_status)
    optimized_tuning = apply_optimized_action_plan(tuning, optimizer)
    state.diagnostics["causal_diagnostics"] = {
        "constrained_action_optimizer": optimizer,
        "attribution_guided_tuning": optimized_tuning,
    }
    write_tuning_tool_command(
        state,
        round_index=1,
        selected_actions=[
            {
                "action_id": "bad_input_gm_id_decrease",
                "decision": "apply",
                "reason": "LLM asks for it despite optimizer math.",
                "overrides": {},
            }
        ],
    )
    application = execute_tuning_tool_commands(state, round_index=1)

    assert optimizer["status"] == "no_improving_combination"
    assert not application["applied_actions"]
    assert "formal action admissibility gate rejected" in application["skipped_actions"][0]["reason"]
    assert application["skipped_actions"][0]["action_admissibility"]["passed"] is False
    assert m1_gm_id.initial == original


def test_default_command_uses_negative_objective_optimizer_candidate():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    m1_gm_id = next(dv for dv in state.design_variables if dv.device == "M1" and dv.variable == "gm_id")
    m2_gm_id = next(dv for dv in state.design_variables if dv.device == "M2" and dv.variable == "gm_id")
    next_value = min(float(m1_gm_id.range.max), float(m1_gm_id.initial) + 1.0)
    action = {
        "action_id": "gain_01_M1_gm_id_increase",
        "metric": "dc_gain",
        "rank": 1,
        "priority": "primary",
        "action_class": "transconductance_bias",
        "knob": "M1.gm_id",
        "apply_to": ["M1.gm_id", "M2.gm_id"],
        "direction": "increase",
        "suggested_unclipped_value": next_value,
        "expected_effect": {"dc_gain": "increase"},
        "optimizer": {"objective_delta": -0.01, "local_model_source": "spice_small_perturbation"},
        "optimizer_selected": False,
    }
    tuning = {"by_failure": [{"metric": "dc_gain", "actions": [action]}]}
    optimizer = {
        "status": "no_improving_combination",
        "selected_actions": [],
        "candidate_actions": [
            {
                    **action,
                    "objective_delta": -0.01,
                    "local_model_source": "spice_small_perturbation",
                    "action_admissibility": {"passed": True},
                }
            ],
    }
    state.diagnostics["causal_diagnostics"] = {
        "constrained_action_optimizer": optimizer,
        "attribution_guided_tuning": tuning,
    }

    command = write_tuning_tool_command(state, round_index=1)
    assert command["args"]["selected_actions"][0]["action_id"] == "gain_01_M1_gm_id_increase"

    application = execute_tuning_tool_commands(state, round_index=1)

    assert application["applied_actions"]
    assert m1_gm_id.initial == next_value
    assert m2_gm_id.initial == next_value


def test_passing_spice_evidence_gate_allows_guarded_action_execution():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    target_status = {
        "dc_gain": {
            "status": "fail",
            "value": 30.0,
            "min": 60.0,
            "max": None,
            "priority": 1,
        }
    }
    tuning = {
        "by_failure": [
            {
                "metric": "dc_gain",
                "actions": [
                    {
                        "action_id": "guarded_input_gm_id",
                        "metric": "dc_gain",
                        "rank": 1,
                        "priority": "guarded",
                        "knob": "M1.gm_id",
                        "apply_to": ["M1.gm_id", "M2.gm_id"],
                        "direction": "increase",
                        "suggested_unclipped_value": 18.0,
                        "expected_effect": {"dc_gain": "increase"},
                    }
                ],
            }
        ],
    }
    intervention_model = {
        "base_violation_vector": {"dc_gain": 0.5},
        "action_effects": [
            {
                "action_id": "guarded_input_gm_id",
                "status": "ok",
                "source": "spice_small_perturbation",
                "delta_violation_vector": {"dc_gain": -0.45},
                "uncertainty": 0.1,
            }
        ],
    }

    optimizer = optimize_tuning_actions(
        tuning=tuning,
        target_status=target_status,
        intervention_model=intervention_model,
    )
    optimized_tuning = apply_optimized_action_plan(tuning, optimizer)
    state.diagnostics["causal_diagnostics"] = {
        "constrained_action_optimizer": optimizer,
        "attribution_guided_tuning": optimized_tuning,
    }
    command = write_tuning_tool_command(state, round_index=1)
    application = execute_tuning_tool_commands(state, round_index=1)
    m1_gm_id = next(dv for dv in state.design_variables if dv.device == "M1" and dv.variable == "gm_id")
    m2_gm_id = next(dv for dv in state.design_variables if dv.device == "M2" and dv.variable == "gm_id")

    assert optimizer["selected_actions"][0]["evidence_gate"]["passed"] is True
    assert command["args"]["selected_actions"][0]["action_id"] == "guarded_input_gm_id"
    assert application["applied_actions"][0]["action_id"] == "guarded_input_gm_id"
    assert m1_gm_id.initial == 18.0
    assert m2_gm_id.initial == 18.0


def test_llm_schema_command_can_select_and_override_fine_grained_tuning_action(tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    result = SimulationResult(
        success=True,
        return_code=0,
        measurements={
            "dc_gain_db": 20.0,
            "unity_gain_bandwidth": 2.0e8,
            "phase_margin": 75.0,
            "output_swing": 0.82,
            "saturation_margin": 0.12,
            "icmr_min": 0.63,
            "icmr_max": 1.26,
            "total_power": 5.0e-5,
        },
    )
    best_meta = {
        "performance": {
            "dc_gain": 20.0,
            "unity_gain_bandwidth": 2.0e8,
            "phase_margin": 75.0,
            "output_swing": 0.82,
            "saturation_margin": 0.12,
            "icmr_min": 0.63,
            "icmr_max": 1.26,
            "power": 5.0e-5,
        },
        "decoded": {"__global__": {}},
        "loss_breakdown": {"gain_deficit": 1.0},
    }
    apply_optimizer_meta_to_state(
        state,
        {
            "decoded": {"__global__": {}},
            "transistor_params": {
                "M1": {"gm": 3.0e-4, "gds": 5.0e-6, "id": 2.0e-5, "vds": 0.53, "vdsat": 0.08, "region": "saturation"},
                "M2": {"gm": 3.0e-4, "gds": 5.0e-6, "id": 2.0e-5, "vds": 0.53, "vdsat": 0.08, "region": "saturation"},
                "M3": {"gm": 2.0e-4, "gds": 4.0e-5, "id": 2.0e-5, "vds": 0.51, "vdsat": 0.20, "region": "saturation"},
                "M4": {"gm": 2.0e-4, "gds": 3.5e-5, "id": 2.0e-5, "vds": 0.51, "vdsat": 0.20, "region": "saturation"},
                "M5": {"gm": 3.0e-4, "gds": 8.0e-5, "id": 4.0e-5, "vds": 0.15, "vdsat": 0.145, "region": "saturation"},
            },
        },
    )

    artifacts = ArtifactWriter(tmp_path).write(
        state=state,
        best_meta=best_meta,
        sim_result=result,
        iteration=4,
        netlist_str="* netlist\n.end\n",
        flow_meta={"source_kind": "schema", "options": {}},
    )
    state = build_design_state_from_yaml(load_yaml_mapping(artifacts.design_state), default_environment())
    available = state.diagnostics["causal_diagnostics"]["attribution_guided_tuning"]["by_failure"][0]["actions"]
    load_l_action = next(action for action in available if action["knob"] in {"M3.L", "M4.L"})
    m1_action = next(action for action in available if action["knob"] == "M1.gm_id")
    load_l_action["optimizer_selected"] = True
    load_l_action["optimizer"] = {
        "optimizer_selected": True,
        "objective_delta": 0.0,
        "objective_source": "spice_small_perturbation",
    }

    command = write_tuning_tool_command(
        state,
        round_index=1,
        selected_actions=[
            {
                "action_id": load_l_action["action_id"],
                "decision": "apply",
                "reason": "LLM chooses a smaller gain step to protect bandwidth.",
                "overrides": {
                    "suggested_unclipped_value": 6.0e-7,
                    "range_update": {"type": "expand_upper_bound", "suggested_max": 6.5e-7},
                },
            },
            {
                "action_id": m1_action["action_id"],
                "decision": "skip",
                "reason": "Hold input gm/ID for this round.",
                "overrides": {},
            },
        ],
        custom_actions=[
            {
                "action_id": "manual_M5_gm_id_set",
                "decision": "apply",
                "knob": "M5.gm_id",
                "suggested_unclipped_value": 12.0,
                "range_update": {"type": "set_range", "min": 8.0, "max": 18.0},
                "reason": "LLM directly lowers tail gm/ID after reading attribution evidence.",
            }
        ],
    )
    compact_state_path = tmp_path / "compact_design_state.yaml"
    state.to_yaml(compact_state_path, include_runtime_context=False)
    persisted_command = load_yaml_mapping(compact_state_path)["diagnostics"]["agent_tool_commands"][0]
    application = execute_tuning_tool_commands(state, round_index=1)
    m3_l = next(dv for dv in state.design_variables if dv.device == "M3" and dv.variable == "L")
    m4_l = next(dv for dv in state.design_variables if dv.device == "M4" and dv.variable == "L")
    m1_gm_id = next(dv for dv in state.design_variables if dv.device == "M1" and dv.variable == "gm_id")
    m5_gm_id = next(dv for dv in state.design_variables if dv.device == "M5" and dv.variable == "gm_id")

    assert command["args"]["available_actions"]
    assert command["args"]["custom_actions"][0]["action_id"] == "manual_M5_gm_id_set"
    assert "design_variables[*].initial" in command["write_policy"]["allowed_fields"]
    assert "topology" in command["write_policy"]["forbidden_fields"]
    assert command["llm_editable_fields"]["decision_values"] == ["apply", "skip"]
    assert "custom_actions" in command["llm_editable_fields"]
    assert command["write_policy"]["action_admissibility"]["schema_version"] == "analogrf_ir.formal_action_admissibility.v0_1"
    assert "write_policy" not in persisted_command
    assert "schema_context" not in persisted_command
    assert "llm_editable_fields" not in persisted_command
    assert "available_actions" not in persisted_command["args"]
    assert persisted_command["args"]["available_action_count"] > 0
    assert persisted_command["args"]["selected_actions"][0]["action_id"] == load_l_action["action_id"]
    assert persisted_command["args"]["custom_actions"][0]["action_id"] == "manual_M5_gm_id_set"
    assert application["command_id"] == command["id"]
    assert len(application["applied_actions"]) == 1
    assert m3_l.initial == 6.0e-7
    assert m4_l.initial == 6.0e-7
    assert m3_l.range.max == 6.5e-7
    assert m1_gm_id.initial == 15.0
    assert m5_gm_id.initial == 8.0
    assert m5_gm_id.range.min == 5.0
    assert m5_gm_id.range.max == 9.0
    assert application["skipped_actions"][0]["llm_reason"] == "Hold input gm/ID for this round."
    assert "formal action admissibility gate rejected" in application["skipped_actions"][1]["reason"]
    assert application["skipped_actions"][1]["action_admissibility"]["passed"] is False


def test_agent_write_policy_rejects_non_design_variable_edits():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    original_role = state.topology.devices[0].role
    policy = agent_write_policy()

    application = apply_attribution_guided_tuning(
        state,
        round_index=1,
        custom_actions=[
            {
                "action_id": "illegal_topology_edit",
                "decision": "apply",
                "knob": "topology.devices.0.role",
                "suggested_unclipped_value": 1.0,
                "reason": "This should not be allowed.",
            },
            {
                "action_id": "illegal_range_update",
                "decision": "apply",
                "knob": "M1.gm_id",
                "suggested_unclipped_value": 16.0,
                "range_update": {"type": "replace_topology", "value": 1.0},
                "reason": "Bad range update type.",
            },
        ],
    )

    assert "topology" in policy["forbidden_fields"]
    assert not application["applied_actions"]
    reasons = [item["reason"] for item in application["skipped_actions"]]
    assert any("outside design_variables" in reason for reason in reasons)
    assert any("range_update type" in reason for reason in reasons)
    assert state.topology.devices[0].role == original_role


def test_agent_hard_physical_gate_rejects_symmetry_breaking_action():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    m1_gm_id = next(dv for dv in state.design_variables if dv.device == "M1" and dv.variable == "gm_id")
    m2_gm_id = next(dv for dv in state.design_variables if dv.device == "M2" and dv.variable == "gm_id")

    application = apply_attribution_guided_tuning(
        state,
        round_index=1,
        custom_actions=[
            {
                "action_id": "break_M1_M2_symmetry",
                "decision": "apply",
                "knob": "M1.gm_id",
                "apply_to": ["M1.gm_id"],
                "suggested_unclipped_value": 18.0,
                "reason": "This should be rejected because M1/M2 are symmetric.",
            }
        ],
    )

    assert not application["applied_actions"]
    assert "hard physical gate rejected action" in application["skipped_actions"][0]["reason"]
    assert application["skipped_actions"][0]["hard_physical_gate"]["passed"] is False
    assert m1_gm_id.initial == m2_gm_id.initial


def test_deepseek_schema_planner_writes_fallback_command_without_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    state.diagnostics["causal_diagnostics"] = {
        "attribution_guided_tuning": {
            "by_failure": [
                {
                    "actions": [
                        {
                            "action_id": "dc_gain_01_M3_L_increase",
                            "metric": "dc_gain",
                            "rank": 1,
                            "priority": "primary",
                            "knob": "M3.L",
                            "apply_to": ["M3.L", "M4.L"],
                            "direction": "increase",
                            "suggested_next_value": 5.0e-7,
                            "rationale": "Increase mirror load resistance.",
                        }
                    ]
                }
            ]
        }
    }

    result = DeepSeekSchemaPlanner(
        LLMPlannerConfig(provider="deepseek", model="deepseek-v4-flash", api_key_env="DEEPSEEK_API_KEY")
    ).write_command(state, round_index=1, agent_model={"failed_targets": ["dc_gain"]})

    command = result.command
    assert result.used_llm is False
    assert command["llm_planner"]["status"] == "fallback"
    assert command["args"]["selected_actions"][0]["action_id"] == "dc_gain_01_M3_L_increase"
    assert state.diagnostics["agent_tool_commands"][0]["id"] == command["id"]


def test_agent_fast_path_uses_negative_objective_candidate_when_combo_status_is_not_ok():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    action = {
        "action_id": "dc_gain_01_M3_L_increase",
        "metric": "dc_gain",
        "rank": 1,
        "priority": "primary",
        "knob": "M3.L",
        "apply_to": ["M3.L", "M4.L"],
        "direction": "increase",
        "suggested_next_value": 5.0e-7,
        "objective_delta": -0.001,
        "penalized_objective_delta": 0.010,
        "local_model_source": "spice_small_perturbation",
        "action_admissibility": {"passed": True},
    }
    state.diagnostics["causal_diagnostics"] = {
        "constrained_action_optimizer": {
            "status": "no_improving_combination",
            "selected_actions": [],
            "candidate_actions": [action],
        },
        "attribution_guided_tuning": {"by_failure": [{"metric": "dc_gain", "actions": [action]}]},
    }
    loop = DiagnosticAgentLoop.__new__(DiagnosticAgentLoop)
    loop.llm_config = LLMPlannerConfig(provider="deepseek", model="deepseek-v4-pro")

    command = loop._optimizer_evidence_fast_path(state, round_index=1)

    assert command is not None
    assert command["llm_planner"]["status"] == "skipped"
    assert "negative-objective" in command["llm_planner"]["reason"]
    assert command["args"]["selected_actions"][0]["action_id"] == "dc_gain_01_M3_L_increase"
    assert command["args"]["allowed_priorities"] == ["primary"]


def test_llm_shadow_audit_preserves_optimizer_fast_path(monkeypatch, tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    action = {
        "action_id": "dc_gain_01_M3_L_increase",
        "metric": "dc_gain",
        "rank": 1,
        "priority": "primary",
        "knob": "M3.L",
        "apply_to": ["M3.L", "M4.L"],
        "direction": "increase",
        "suggested_next_value": 5.0e-7,
        "objective_delta": -0.001,
        "local_model_source": "spice_small_perturbation",
        "optimizer_selected": True,
        "action_admissibility": {"passed": True, "objective_delta": -0.001},
    }
    state.diagnostics["causal_diagnostics"] = {
        "constrained_action_optimizer": {
            "status": "ok",
            "selected_actions": [action],
            "candidate_actions": [action],
        },
        "attribution_guided_tuning": {"by_failure": [{"metric": "dc_gain", "actions": [action]}]},
    }
    state_path = tmp_path / "design_state.yaml"
    state.to_yaml(state_path)
    calls = {"count": 0}

    class FakePlanner:
        def __init__(self, _config):
            pass

        def write_command(self, schema_state, *, round_index, agent_model):
            calls["count"] += 1
            command = write_tuning_tool_command(
                schema_state,
                round_index=round_index,
                author="llm:deepseek:shadow-test",
                selected_actions=[
                    {
                        "action_id": "dc_gain_01_M3_L_increase",
                        "decision": "apply",
                        "reason": "Shadow planner agrees with the optimizer-backed action.",
                        "overrides": {},
                    }
                ],
            )
            command["llm_planner"] = {
                "provider": "deepseek",
                "model": "unit-test",
                "status": "ok",
                "reason": "shadow audit",
                "used_llm": True,
            }
            return argparse.Namespace(command=command, used_llm=True, status="ok", reason="ok")

    monkeypatch.setattr("flow.agent_loop.DeepSeekSchemaPlanner", FakePlanner)
    loop = DiagnosticAgentLoop.__new__(DiagnosticAgentLoop)
    loop.config = FlowConfig(llm_policy="shadow")
    loop.llm_config = LLMPlannerConfig(provider="deepseek", model="unit-test")
    loop.emit = lambda _msg: None
    loop._environment = default_environment()

    output = loop._llm_write_schema_command_node(
        {
            "last_design_state": str(state_path),
            "round_index": 1,
            "agent_model": {"failed_targets": ["dc_gain"]},
        }
    )

    assert calls["count"] == 1
    assert output["last_tool_command"]["author"] == "optimizer_evidence_fast_path"
    assert output["last_tool_command"]["llm_planner"]["status"] == "skipped"
    assert output["last_tool_command"]["llm_planner"]["policy"] == "shadow"
    assert output["last_tool_command"]["llm_shadow"]["used_llm"] is True
    assert output["last_tool_command"]["llm_shadow"]["source"] == "optimizer_evidence_fast_path"
    assert output["last_tool_command"]["llm_shadow"]["shadow_selected_action_ids"] == ["dc_gain_01_M3_L_increase"]


def test_residual_llm_audits_before_optimizer_fast_path(monkeypatch, tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    action = {
        "action_id": "dc_gain_01_M3_L_increase",
        "metric": "dc_gain",
        "rank": 1,
        "priority": "primary",
        "knob": "M3.L",
        "apply_to": ["M3.L", "M4.L"],
        "direction": "increase",
        "suggested_next_value": 5.0e-7,
        "objective_delta": -0.001,
        "local_model_source": "spice_small_perturbation",
        "optimizer_selected": True,
        "action_admissibility": {"passed": True, "objective_delta": -0.001},
    }
    state.diagnostics["causal_diagnostics"] = {
        "constrained_action_optimizer": {
            "status": "ok",
            "selected_actions": [action],
            "candidate_actions": [action],
        },
        "attribution_guided_tuning": {"by_failure": [{"metric": "dc_gain", "actions": [action]}]},
    }
    state_path = tmp_path / "design_state.yaml"
    state.to_yaml(state_path)
    calls = {"count": 0}

    class FakePlanner:
        def __init__(self, _config):
            pass

        def write_command(self, schema_state, *, round_index, agent_model):
            calls["count"] += 1
            command = write_tuning_tool_command(
                schema_state,
                round_index=round_index,
                author="llm:deepseek:residual-audit-test",
                selected_actions=[],
                custom_actions=[],
            )
            command["llm_planner"] = {
                "provider": "deepseek",
                "model": "unit-test",
                "status": "ok",
                "reason": "audit found no better action",
                "used_llm": True,
            }
            return argparse.Namespace(command=command, used_llm=True, status="ok", reason="ok")

    monkeypatch.setattr("flow.agent_loop.DeepSeekSchemaPlanner", FakePlanner)
    loop = DiagnosticAgentLoop.__new__(DiagnosticAgentLoop)
    loop.config = FlowConfig(llm_policy="residual")
    loop.llm_config = LLMPlannerConfig(provider="deepseek", model="unit-test")
    loop.emit = lambda _msg: None
    loop._environment = default_environment()

    output = loop._llm_write_schema_command_node(
        {
            "last_design_state": str(state_path),
            "round_index": 1,
            "agent_model": {"failed_targets": ["dc_gain"]},
        }
    )

    assert calls["count"] == 1
    assert output["last_tool_command"]["author"] == "optimizer_evidence_fast_path"
    assert output["last_tool_command"]["llm_increment"]["status"] == "no_validated_llm_hypothesis_use_optimizer_fast_path"
    assert output["last_tool_command"]["llm_increment"]["used_llm"] is True
    assert output["last_tool_command"]["llm_planner"]["policy"] == "residual"
    assert output["last_tool_command"]["args"]["selected_actions"][0]["action_id"] == "dc_gain_01_M3_L_increase"


def test_residual_llm_existing_action_selection_is_audit_only(monkeypatch, tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    action = {
        "action_id": "dc_gain_01_M3_L_increase",
        "metric": "dc_gain",
        "rank": 1,
        "priority": "primary",
        "knob": "M3.L",
        "apply_to": ["M3.L", "M4.L"],
        "direction": "increase",
        "suggested_next_value": 5.0e-7,
        "objective_delta": -0.001,
        "local_model_source": "spice_small_perturbation",
        "optimizer_selected": True,
        "action_admissibility": {"passed": True, "objective_delta": -0.001},
    }
    state.diagnostics["causal_diagnostics"] = {
        "constrained_action_optimizer": {
            "status": "ok",
            "selected_actions": [action],
            "candidate_actions": [action],
        },
        "attribution_guided_tuning": {"by_failure": [{"metric": "dc_gain", "actions": [action]}]},
    }
    state_path = tmp_path / "design_state.yaml"
    state.to_yaml(state_path)

    class FakePlanner:
        def __init__(self, _config):
            pass

        def write_command(self, schema_state, *, round_index, agent_model):
            command = write_tuning_tool_command(
                schema_state,
                round_index=round_index,
                author="llm:deepseek:residual-existing-test",
                selected_actions=[
                    {
                        "action_id": "dc_gain_01_M3_L_increase",
                        "decision": "apply",
                        "reason": "Existing action selected by LLM audit.",
                        "overrides": {},
                    }
                ],
                custom_actions=[],
            )
            command["llm_planner"] = {
                "provider": "deepseek",
                "model": "unit-test",
                "status": "ok",
                "reason": "existing action audit",
                "used_llm": True,
            }
            return argparse.Namespace(command=command, used_llm=True, status="ok", reason="ok")

    monkeypatch.setattr("flow.agent_loop.DeepSeekSchemaPlanner", FakePlanner)
    loop = DiagnosticAgentLoop.__new__(DiagnosticAgentLoop)
    loop.config = FlowConfig(llm_policy="residual")
    loop.llm_config = LLMPlannerConfig(provider="deepseek", model="unit-test")
    loop.emit = lambda _msg: None
    loop._environment = default_environment()

    output = loop._llm_write_schema_command_node(
        {
            "last_design_state": str(state_path),
            "round_index": 1,
            "agent_model": {"failed_targets": ["dc_gain"]},
        }
    )

    command = output["last_tool_command"]
    assert command["author"] == "optimizer_evidence_fast_path"
    assert command["llm_increment"]["ignored_existing_action_ids"] == ["dc_gain_01_M3_L_increase"]
    assert command["args"]["selected_actions"][0]["action_id"] == "dc_gain_01_M3_L_increase"


def test_residual_llm_repair_sensitive_no_hypothesis_prefers_postprocess(monkeypatch, tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    state.diagnostics["causal_diagnostics"] = {
        "constrained_action_optimizer": {
            "status": "no_candidate_actions",
            "selected_actions": [],
            "candidate_actions": [],
        },
        "attribution_guided_tuning": {"by_failure": [{"metric": "saturation_margin", "actions": []}]},
    }
    state_path = tmp_path / "design_state.yaml"
    state.to_yaml(state_path)

    class FakePlanner:
        def __init__(self, _config):
            pass

        def write_command(self, schema_state, *, round_index, agent_model):
            command = write_tuning_tool_command(
                schema_state,
                round_index=round_index,
                author="llm:deepseek:residual-repair-test",
                selected_actions=[],
                custom_actions=[],
            )
            command["llm_planner"] = {
                "provider": "deepseek",
                "model": "unit-test",
                "status": "ok",
                "reason": "audit found no custom hypothesis",
                "used_llm": True,
            }
            return argparse.Namespace(command=command, used_llm=True, status="ok", reason="ok")

    monkeypatch.setattr("flow.agent_loop.DeepSeekSchemaPlanner", FakePlanner)
    loop = DiagnosticAgentLoop.__new__(DiagnosticAgentLoop)
    loop.config = FlowConfig(llm_policy="residual", postprocess_policy="fallback")
    loop.llm_config = LLMPlannerConfig(provider="deepseek", model="unit-test")
    loop.emit = lambda _msg: None
    loop._environment = default_environment()

    output = loop._llm_write_schema_command_node(
        {
            "last_design_state": str(state_path),
            "round_index": 1,
            "max_rounds": 3,
            "rounds": [
                {
                    "spec_pass": False,
                    "failed_targets": ["saturation_margin"],
                    "measured_violation_score": 0.01,
                    "postprocess_event_count": 0,
                }
            ],
            "agent_model": {"failed_targets": ["saturation_margin"]},
        }
    )

    command = output["last_tool_command"]
    assert command["author"] == "llm:deepseek:residual-repair-test"
    assert command["args"]["selected_actions"] == []
    assert command["llm_increment"]["status"] == "no_validated_llm_hypothesis_use_postprocess_rescue"
    assert command["llm_increment"]["used_llm"] is True


def test_residual_llm_selection_runs_before_repair_fast_path(monkeypatch, tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    state_path = tmp_path / "design_state.yaml"
    state.to_yaml(state_path)
    calls = {"execute": 0}

    def fake_execute(_schema_state, *, round_index):
        calls["execute"] += 1
        return {
            "round_index": round_index,
            "command_id": "tuning_round_001",
            "applied_actions": [{"action_id": "llm_hypothesis_001_bias"}],
            "skipped_actions": [],
        }

    monkeypatch.setattr("flow.agent_loop.execute_tuning_tool_commands", fake_execute)
    loop = DiagnosticAgentLoop.__new__(DiagnosticAgentLoop)
    loop.config = FlowConfig(llm_policy="residual", postprocess_policy="fallback")
    loop.emit = lambda _msg: None
    loop._environment = default_environment()

    output = loop._execute_schema_command_node(
        {
            "current_schema": str(state_path),
            "last_design_state": str(state_path),
            "loop_dir": str(tmp_path),
            "round_index": 1,
            "max_rounds": 3,
            "rounds": [
                {
                    "spec_pass": False,
                    "failed_targets": ["dc_gain"],
                    "measured_violation_score": 0.01,
                    "postprocess_event_count": 0,
                }
            ],
            "last_tool_command": {
                "args": {
                    "selected_actions": [
                        {
                            "action_id": "llm_hypothesis_001_bias",
                            "decision": "apply",
                            "reason": "validated residual hypothesis",
                        }
                    ]
                }
            },
        }
    )

    assert calls["execute"] == 1
    assert output["round_index"] == 2
    assert output["force_postprocess_once"] is False
    assert output["last_tuning_application"]["applied_actions"]


def test_residual_llm_validates_custom_hypothesis_before_apply(monkeypatch, tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    state.diagnostics["causal_diagnostics"] = {
        "constrained_action_optimizer": {
            "status": "no_candidate_actions",
            "selected_actions": [],
            "candidate_actions": [],
        },
        "attribution_guided_tuning": {"by_failure": [{"metric": "dc_gain", "actions": []}]},
    }
    state_path = tmp_path / "design_state.yaml"
    state.to_yaml(state_path)
    calls = {"planner": 0, "probe": 0}

    class FakePlanner:
        def __init__(self, _config):
            pass

        def write_command(self, schema_state, *, round_index, agent_model):
            calls["planner"] += 1
            command = write_tuning_tool_command(
                schema_state,
                round_index=round_index,
                author="llm:deepseek:hypothesis-test",
                selected_actions=[],
                custom_actions=[
                    {
                        "action_id": "raise_load_length",
                        "decision": "apply",
                        "knob": "M3.L",
                        "metric": "dc_gain",
                        "direction": "increase",
                        "suggested_unclipped_value": 7.0e-7,
                        "reason": "Increase load length as a gain hypothesis.",
                    }
                ],
            )
            command["llm_planner"] = {
                "provider": "deepseek",
                "model": "unit-test",
                "status": "ok",
                "reason": "custom hypothesis",
                "used_llm": True,
            }
            return argparse.Namespace(command=command, used_llm=True, status="ok", reason="ok")

    def fake_intervention_model(**kwargs):
        calls["probe"] += 1
        tuning = kwargs["tuning"]
        action = tuning["by_failure"][0]["actions"][0]
        return {
            "schema_version": "analogrf_ir.local_intervention_model.v0_1",
            "method": "spice_small_perturbation",
            "status": "ok",
            "base_violation_vector": {"dc_gain": 0.40},
            "action_effects": [
                {
                    "action_id": action["action_id"],
                    "metric": "dc_gain",
                    "knob": action["knob"],
                    "apply_to": action["apply_to"],
                    "direction": action["direction"],
                    "per_knob_values": action["per_knob_values"],
                    "source": "spice_small_perturbation",
                    "status": "ok",
                    "delta_violation_vector": {"dc_gain": -0.25},
                    "violation_reduction": 0.25,
                    "uncertainty": 0.10,
                }
            ],
        }

    monkeypatch.setattr("flow.agent_loop.DeepSeekSchemaPlanner", FakePlanner)
    monkeypatch.setattr("flow.agent_loop.build_spice_intervention_model", fake_intervention_model)
    loop = DiagnosticAgentLoop.__new__(DiagnosticAgentLoop)
    loop.config = FlowConfig(llm_policy="residual", env="environment.yaml", intervention_max_actions=2)
    loop.llm_config = LLMPlannerConfig(provider="deepseek", model="unit-test")
    loop.emit = lambda _msg: None
    loop._environment = default_environment()
    loop._final_result = argparse.Namespace(
        sim_result=SimulationResult(success=True, measurements={"dc_gain_db": 0.0}),
        best_meta={"performance": {}},
    )

    output = loop._llm_write_schema_command_node(
        {
            "last_design_state": str(state_path),
            "last_artifact_dir": str(tmp_path),
            "round_index": 1,
            "agent_model": {"failed_targets": ["dc_gain"]},
        }
    )

    command = output["last_tool_command"]
    assert calls == {"planner": 1, "probe": 1}
    assert command["llm_increment"]["status"] == "applied"
    assert command["args"]["custom_actions"] == []
    assert command["args"]["selected_actions"][0]["action_id"].startswith("llm_hypothesis_")
    assert command["args"]["selected_actions"][0]["decision"] == "apply"


def test_residual_escape_accepts_measured_custom_patch_without_optimizer_preselection(monkeypatch, tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    state.diagnostics["causal_diagnostics"] = {
        "constrained_action_optimizer": {
            "status": "no_improving_combination",
            "selected_actions": [],
            "candidate_actions": [],
        },
        "attribution_guided_tuning": {"by_failure": [{"metric": "dc_gain", "actions": []}]},
    }
    state_path = tmp_path / "design_state.yaml"
    state.to_yaml(state_path)
    calls = {"planner": 0, "probe": 0, "optimizer": 0}

    class FakePlanner:
        def __init__(self, _config):
            pass

        def write_command(self, schema_state, *, round_index, agent_model):
            calls["planner"] += 1
            command = write_tuning_tool_command(
                schema_state,
                round_index=round_index,
                author="llm:deepseek:escape-test",
                selected_actions=[],
                custom_actions=[
                    {
                        "action_id": "escape_load_length",
                        "decision": "apply",
                        "knob": "M3.L",
                        "apply_to": ["M3.L", "M4.L"],
                        "metric": "dc_gain",
                        "direction": "set",
                        "per_knob_values": {"M3.L": 4.5e-7, "M4.L": 4.5e-7},
                        "reason": "Try a coupled load-length escape patch.",
                    }
                ],
            )
            command["llm_planner"] = {
                "provider": "deepseek",
                "model": "unit-test",
                "status": "ok",
                "reason": "escape hypothesis",
                "used_llm": True,
            }
            return argparse.Namespace(command=command, used_llm=True, status="ok", reason="ok")

    def fake_intervention_model(**kwargs):
        calls["probe"] += 1
        tuning = kwargs["tuning"]
        action = tuning["by_failure"][0]["actions"][0]
        return {
            "schema_version": "analogrf_ir.local_intervention_model.v0_1",
            "method": "spice_small_perturbation",
            "status": "ok",
            "base_violation_vector": {"dc_gain": 0.50},
            "action_effects": [
                {
                    "action_id": action["action_id"],
                    "metric": "dc_gain",
                    "knob": action["knob"],
                    "apply_to": action["apply_to"],
                    "direction": action["direction"],
                    "per_knob_values": action["per_knob_values"],
                    "source": "spice_small_perturbation",
                    "status": "ok",
                    "base_violation_vector": {"dc_gain": 0.50},
                    "after_violation_vector": {"dc_gain": 0.10},
                    "delta_violation_vector": {"dc_gain": -0.40},
                    "violation_reduction": 0.24,
                    "uncertainty": 0.10,
                }
            ],
        }

    def fake_optimizer(*_args, **_kwargs):
        calls["optimizer"] += 1
        raise AssertionError("residual_escape must not call the constrained action optimizer")

    monkeypatch.setattr("flow.agent_loop.DeepSeekSchemaPlanner", FakePlanner)
    monkeypatch.setattr("flow.agent_loop.build_spice_intervention_model", fake_intervention_model)
    monkeypatch.setattr("flow.agent_loop.optimize_tuning_actions", fake_optimizer)
    loop = DiagnosticAgentLoop.__new__(DiagnosticAgentLoop)
    loop.config = FlowConfig(llm_policy="residual_escape", env="environment.yaml", intervention_max_actions=2)
    loop.llm_config = LLMPlannerConfig(provider="deepseek", model="unit-test")
    loop.emit = lambda _msg: None
    loop._environment = default_environment()
    loop._final_result = argparse.Namespace(
        sim_result=SimulationResult(success=True, measurements={"dc_gain_db": 0.0}),
        best_meta={"performance": {}},
    )

    output = loop._llm_write_schema_command_node(
        {
            "last_design_state": str(state_path),
            "last_artifact_dir": str(tmp_path),
            "round_index": 1,
            "agent_model": {"llm_policy": "residual_escape", "failed_targets": ["dc_gain"]},
        }
    )

    command = output["last_tool_command"]
    assert calls == {"planner": 1, "probe": 1, "optimizer": 0}
    assert command["llm_increment"]["status"] == "applied_escape"
    selected = command["args"]["selected_actions"][0]
    assert selected["action_id"].startswith("llm_hypothesis_")

    updated_state = loop._load_design_state(state_path)
    application = execute_tuning_tool_commands(updated_state, round_index=1)
    assert application["applied_actions"]
    assert not application["skipped_actions"]
    assert {item["knob"]: item["new_initial"] for item in application["applied_actions"][0]["applied_knobs"]} == {
        "M3.L": 4.5e-7,
        "M4.L": 4.5e-7,
    }


def test_residual_llm_validates_unverified_target_hypothesis(monkeypatch, tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    state.diagnostics["result"] = {
        "status": {
            "spec_pass": False,
            "failed_targets": [],
            "unverified_targets": ["slew_rate"],
            "best_loss": 1.0,
        }
    }
    state.diagnostics["causal_diagnostics"] = {
        "constrained_action_optimizer": {
            "status": "no_candidate_actions",
            "selected_actions": [],
            "candidate_actions": [],
        },
        "attribution_guided_tuning": {"by_failure": [{"metric": "slew_rate", "actions": []}]},
    }
    state_path = tmp_path / "design_state.yaml"
    state.to_yaml(state_path)
    captured = {}

    class FakePlanner:
        def __init__(self, _config):
            pass

        def write_command(self, schema_state, *, round_index, agent_model):
            command = write_tuning_tool_command(
                schema_state,
                round_index=round_index,
                author="llm:deepseek:unverified-test",
                selected_actions=[],
                custom_actions=[
                    {
                        "action_id": "raise_tail_for_slew",
                        "decision": "apply",
                        "knob": "global.I_tail",
                        "metric": "slew_rate",
                        "direction": "increase",
                        "suggested_unclipped_value": 5.0e-5,
                        "reason": "Raise bias current to verify slew-rate recovery.",
                    }
                ],
            )
            command["llm_planner"] = {
                "provider": "deepseek",
                "model": "unit-test",
                "status": "ok",
                "reason": "unverified slew hypothesis",
                "used_llm": True,
            }
            return argparse.Namespace(command=command, used_llm=True, status="ok", reason="ok")

    def fake_intervention_model(**kwargs):
        captured["target_status"] = kwargs["target_status"]
        tuning = kwargs["tuning"]
        action = tuning["by_failure"][0]["actions"][0]
        return {
            "schema_version": "analogrf_ir.local_intervention_model.v0_1",
            "method": "spice_small_perturbation",
            "status": "ok",
            "base_violation_vector": {"slew_rate": 0.15},
            "action_effects": [
                {
                    "action_id": action["action_id"],
                    "metric": "slew_rate",
                    "knob": action["knob"],
                    "apply_to": action["apply_to"],
                    "direction": action["direction"],
                    "per_knob_values": action["per_knob_values"],
                    "source": "spice_small_perturbation",
                    "status": "ok",
                    "delta_violation_vector": {"slew_rate": -0.15},
                    "violation_reduction": 0.15,
                    "uncertainty": 0.10,
                }
            ],
        }

    monkeypatch.setattr("flow.agent_loop.DeepSeekSchemaPlanner", FakePlanner)
    monkeypatch.setattr("flow.agent_loop.build_spice_intervention_model", fake_intervention_model)
    loop = DiagnosticAgentLoop.__new__(DiagnosticAgentLoop)
    loop.config = FlowConfig(llm_policy="residual", env="environment.yaml", intervention_max_actions=2)
    loop.llm_config = LLMPlannerConfig(provider="deepseek", model="unit-test")
    loop.emit = lambda _msg: None
    loop._environment = default_environment()
    loop._final_result = argparse.Namespace(
        sim_result=SimulationResult(success=True, measurements={}),
        best_meta={"performance": {"slew_rate": 2.0e7}},
    )

    output = loop._llm_write_schema_command_node(
        {
            "last_design_state": str(state_path),
            "last_artifact_dir": str(tmp_path),
            "round_index": 1,
            "agent_model": {
                "failed_targets": [],
                "unverified_targets": ["slew_rate"],
            },
        }
    )

    command = output["last_tool_command"]
    assert captured["target_status"]["slew_rate"]["status"] == "unverified"
    assert captured["target_status"]["slew_rate"]["value"] is None
    assert captured["target_status"]["slew_rate"]["optimizer_value"] == 2.0e7
    assert command["llm_increment"]["status"] == "applied"
    assert command["llm_increment"]["model_status"] == "ok"
    assert command["args"]["selected_actions"][0]["action_id"].startswith("llm_hypothesis_")


def test_residual_llm_voltage_hypotheses_are_trust_region_clipped():
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/telescopic/telescopic_ota_ihp130.yaml"),
        default_environment(),
    )
    for dv in state.design_variables:
        if not dv.device and dv.variable == "vbias_ncas":
            dv.initial = 0.90
        if not dv.device and dv.variable == "vbias_pcas":
            dv.initial = 0.35
    state.global_parameters["vbias_ncas"] = 0.90
    state.global_parameters["vbias_pcas"] = 0.35
    tuning = _tuning_from_llm_hypotheses(
        state,
        [
            {
                "action_id": "large_bad_bias_jump",
                "decision": "apply",
                "knob": "global.vbias_ncas",
                "apply_to": ["global.vbias_ncas", "global.vbias_pcas"],
                "direction": "set",
                "metric": "saturation_margin",
                "per_knob_values": {
                    "global.vbias_ncas": 0.70,
                    "global.vbias_pcas": 0.50,
                },
            }
        ],
        {
            "saturation_margin": {
                "status": "fail",
                "counts_for_pass": True,
            }
        },
    )

    action = tuning["by_failure"][0]["actions"][0]
    assert action["per_knob_values"]["global.vbias_ncas"] == pytest.approx(0.87)
    assert action["per_knob_values"]["global.vbias_pcas"] == pytest.approx(0.38)


def test_residual_llm_voltage_hypotheses_anchor_to_current_globals():
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/telescopic/telescopic_ota_ihp130.yaml"),
        default_environment(),
    )
    for dv in state.design_variables:
        if not dv.device and dv.variable == "vbias_tail":
            dv.initial = 0.405
    state.global_parameters["vbias_tail"] = 0.320
    tuning = _tuning_from_llm_hypotheses(
        state,
        [
            {
                "action_id": "tail_bias_jump",
                "decision": "apply",
                "knob": "global.vbias_tail",
                "direction": "set",
                "metric": "saturation_margin",
                "per_knob_values": {"global.vbias_tail": 0.405},
            }
        ],
        {
            "saturation_margin": {
                "status": "fail",
                "counts_for_pass": True,
            }
        },
    )

    action = tuning["by_failure"][0]["actions"][0]
    assert action["per_knob_values"]["global.vbias_tail"] == pytest.approx(0.35)


def test_residual_llm_hypothesis_drops_targets_without_per_knob_values():
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"),
        default_environment(),
    )
    tuning = _tuning_from_llm_hypotheses(
        state,
        [
            {
                "action_id": "partial_composite_gmid",
                "decision": "apply",
                "knob": "M6.gm_id",
                "apply_to": ["M6.gm_id", "M3.gm_id", "M4.gm_id"],
                "direction": "increase",
                "metric": "saturation_margin",
                "per_knob_values": {
                    "M3.gm_id": 8.0,
                    "M4.gm_id": 8.0,
                },
                "range_update": {
                    "type": "set_range",
                    "per_knob": {
                        "M6.gm_id": {"min": 6.0, "max": 12.0},
                        "M3.gm_id": {"min": 6.0, "max": 12.0},
                        "M4.gm_id": {"min": 6.0, "max": 12.0},
                    },
                },
            }
        ],
        {
            "saturation_margin": {
                "status": "fail",
                "counts_for_pass": True,
            }
        },
    )

    action = tuning["by_failure"][0]["actions"][0]
    assert action["knob"] == "M3.gm_id"
    assert action["apply_to"] == ["M3.gm_id", "M4.gm_id"]
    assert set(action["per_knob_values"]) == {"M3.gm_id", "M4.gm_id"}
    assert set(action["range_update"]["per_knob"]) == {"M3.gm_id", "M4.gm_id"}


def test_deepseek_schema_planner_accepts_llm_selected_and_custom_actions(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    state.diagnostics["causal_diagnostics"] = {
        "attribution_guided_tuning": {
            "by_failure": [
                {
                    "actions": [
                        {
                            "action_id": "gbw_01_M1_gm_id_increase",
                            "metric": "unity_gain_bandwidth",
                            "rank": 1,
                            "priority": "primary",
                            "knob": "M1.gm_id",
                            "apply_to": ["M1.gm_id", "M2.gm_id"],
                            "direction": "increase",
                            "suggested_next_value": 18.0,
                            "rationale": "Increase input pair transconductance.",
                        }
                    ]
                }
            ]
        }
    }

    class FakePlanner(DeepSeekSchemaPlanner):
        def _call_planner(self, command, agent_model, api_key):
            return {
                "selected_actions": [
                    {
                        "action_id": "gbw_01_M1_gm_id_increase",
                        "decision": "skip",
                        "reason": "Preserve headroom in this round.",
                        "overrides": {},
                    }
                ],
                "custom_actions": [
                    {
                        "action_id": "manual_I_tail_increase",
                        "decision": "apply",
                        "knob": "global.I_tail",
                        "suggested_unclipped_value": 6.0e-5,
                        "range_update": {"type": "expand_upper_bound", "suggested_max": 3.0e-4},
                        "reason": "Raise bias current to improve bandwidth.",
                    }
                ],
                "rationale": "Use one direct bias move.",
            }

    result = FakePlanner(
        LLMPlannerConfig(
            provider="deepseek",
            model="deepseek-v4-pro",
            api_key_env="DEEPSEEK_API_KEY",
            thinking="enabled",
            reasoning_effort="max",
            temperature=0.2,
            max_tokens=2048,
        )
    ).write_command(state, round_index=2, agent_model={"failed_targets": ["unity_gain_bandwidth"]})

    command = result.command
    assert result.used_llm is True
    assert command["llm_planner"]["status"] == "ok"
    assert command["llm_planner"]["model"] == "deepseek-v4-pro"
    assert command["llm_planner"]["thinking"] == "enabled"
    assert command["llm_planner"]["reasoning_effort"] == "max"
    assert command["llm_planner"]["temperature"] == 0.2
    assert command["llm_planner"]["max_tokens"] == 2048
    assert command["args"]["selected_actions"] == []
    assert command["args"]["custom_actions"][0]["knob"] == "global.I_tail"


def test_residual_planner_context_requires_custom_hypothesis():
    context = _planner_context(
        {
            "args": {"available_actions": []},
            "schema_context": {"writable_variables": {"global.I_tail": {"variable": "I_tail"}}},
            "write_policy": {"action_admissibility": {"formal_rule": "optimizer_selected OR objective_delta < 0"}},
        },
        {
            "llm_policy": "residual",
            "status": {"spec_pass": False},
            "failed_targets": ["unity_gain_bandwidth"],
            "unverified_targets": [],
        },
    )

    assert context["planner_mode"] == "residual"
    assert context["residual_hypothesis_required"] is True
    rules = "\n".join(context["rules"])
    assert "existing available_actions are evidence only" in rules
    assert "return one or two custom_actions" in rules
    assert "Do not return custom_actions: []" in rules


def test_deepseek_schema_planner_retries_empty_thinking_content(monkeypatch):
    planner = DeepSeekSchemaPlanner(
        LLMPlannerConfig(
            provider="deepseek",
            model="deepseek-v4-pro",
            api_key_env="DEEPSEEK_API_KEY",
            thinking="enabled",
            reasoning_effort="max",
            max_tokens=12000,
        )
    )
    responses = [
        {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": "", "reasoning_content": "reasoning consumed the budget"},
                }
            ]
        },
        {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": (
                            '{"selected_actions": [], "custom_actions": [], '
                            '"rationale": "retry produced compact JSON"}'
                        )
                    },
                }
            ]
        },
    ]
    bodies = []

    def fake_post(body, api_key):
        bodies.append(body)
        return responses.pop(0)

    monkeypatch.setattr(planner, "_post_chat_completion", fake_post)

    payload = planner._call_planner({"args": {}, "schema_context": {}}, {}, "unit-test-key")

    assert payload["rationale"] == "retry produced compact JSON"
    assert payload["_planner_transport"]["retry"] is True
    assert payload["_planner_transport"]["first_finish_reason"] == "length"
    assert payload["_planner_transport"]["retry_finish_reason"] == "stop"
    assert bodies[0]["thinking"]["type"] == "enabled"
    assert bodies[1]["thinking"]["type"] == "disabled"
    assert bodies[1]["max_tokens"] == 2048


def test_deepseek_schema_planner_retries_remote_disconnected(monkeypatch):
    import http.client

    planner = DeepSeekSchemaPlanner(
        LLMPlannerConfig(
            provider="deepseek",
            model="deepseek-v4-pro",
            api_key_env="DEEPSEEK_API_KEY",
            max_retries=1,
            retry_backoff_seconds=0.0,
        )
    )
    calls = {"count": 0}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"{}"}}]}'

    def fake_urlopen(_request, *, timeout):
        calls["count"] += 1
        assert timeout == planner.config.timeout_seconds
        if calls["count"] == 1:
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        return FakeResponse()

    monkeypatch.setattr("flow.llm_planner.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("flow.llm_planner.time.sleep", lambda _seconds: None)

    data = planner._post_chat_completion({"messages": []}, "unit-test-key")

    assert calls["count"] == 2
    assert data["choices"][0]["message"]["content"] == "{}"


def test_langgraph_llm_ai_options_are_configurable_from_cli():
    args = _parse_args(
        [
            "--llm-model",
            "deepseek-v4-pro",
            "--llm-thinking",
            "enabled",
            "--llm-reasoning-effort",
            "max",
            "--llm-temperature",
            "0.1",
            "--llm-max-tokens",
            "4096",
            "--llm-timeout",
            "90",
        ]
    )
    config = LLMPlannerConfig.from_env(
        model=args.llm_model or None,
        thinking=args.llm_thinking or None,
        reasoning_effort=args.llm_reasoning_effort or None,
        temperature=args.llm_temperature,
        max_tokens=args.llm_max_tokens,
        timeout_seconds=args.llm_timeout,
    )

    assert config.model == "deepseek-v4-pro"
    assert config.thinking == "enabled"
    assert config.reasoning_effort == "max"
    assert config.temperature == 0.1
    assert config.max_tokens == 4096
    assert config.timeout_seconds == 90


def test_cli_config_file_loads_defaults_and_cli_overrides(tmp_path):
    cfg = tmp_path / "run.yaml"
    cfg.write_text(
        """
        schema_version: analogrf_ir.cli_config.v0_1
        input:
          schema: inputs/ota/two_stage_miller/two_stage_miller_ota.yaml
        optimizer:
          generations: 7
          pop_size: 11
          seed: 123
        agent:
          rounds: 2
          llm_policy: shadow
        features:
          run_asir: false
        postprocess:
          policy: off
        output:
          runs_dir: runs/from_config
        """,
        encoding="utf-8",
    )

    loaded = load_cli_config(cfg)
    args = _parse_args(["--config", str(cfg), "--generations", "9", "--asir"])

    assert loaded["schema"] == "inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"
    assert loaded["no_asir"] is True
    assert args.schema == "inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"
    assert args.generations == 9
    assert args.pop_size == 11
    assert args.agent_rounds == 2
    assert args.llm_policy == "shadow"
    assert args.no_asir is False
    assert args.postprocess_policy == "off"
    assert args.runs_dir == "runs/from_config"


def test_ngspice_run_marks_missing_required_transient_as_failed(monkeypatch):
    sim = NgspiceSimulator(timeout_sec=30)

    monkeypatch.setattr(
        sim,
        "_run_ac_pass",
        lambda _netlist, _work_dir: SimulationResult(
            success=True,
            return_code=0,
            measurements={"dc_gain_db": 40.0, "unity_gain_bandwidth": 2.0e7, "phase_margin": 65.0},
        ),
    )
    monkeypatch.setattr(
        sim,
        "_run_dc_pass",
        lambda _netlist, _work_dir: SimulationResult(
            success=True,
            return_code=0,
            measurements={"total_power": 1.0e-4, "output_swing": 0.8, "saturation_margin": 0.05},
        ),
    )
    monkeypatch.setattr(
        sim,
        "_run_icmr_pass",
        lambda _netlist, _work_dir: SimulationResult(success=True, return_code=0, measurements={}),
    )
    monkeypatch.setattr(
        sim,
        "_run_tran_pass",
        lambda _netlist, _work_dir: SimulationResult(
            success=False,
            return_code=-1,
            raw_stderr="Timeout after 30s",
        ),
    )

    result = sim.run("* ota\n.end", include_transient=True)

    assert result.success is False
    assert result.return_code == 1
    assert result.pass_status["tran"]["missing_measurements"] == ["slew_rate"]
    assert "Timeout after 30s" in result.pass_status["tran"]["stderr_tail"]


def test_ngspice_top_level_success_uses_required_measurements(monkeypatch):
    sim = NgspiceSimulator(timeout_sec=30)

    monkeypatch.setattr(
        sim,
        "_run_ac_pass",
        lambda _netlist, _work_dir: SimulationResult(
            success=False,
            return_code=0,
            raw_stderr="Warning: benign parser warning",
            measurements={"dc_gain_db": 40.0, "unity_gain_bandwidth": 2.0e7, "phase_margin": 65.0},
        ),
    )
    monkeypatch.setattr(
        sim,
        "_run_dc_pass",
        lambda _netlist, _work_dir: SimulationResult(
            success=False,
            return_code=0,
            raw_stderr="Warning: ignored vector",
            measurements={"total_power": 1.0e-4, "output_swing": 0.8, "saturation_margin": 0.05},
        ),
    )
    monkeypatch.setattr(
        sim,
        "_run_icmr_pass",
        lambda _netlist, _work_dir: SimulationResult(success=False, return_code=-1, measurements={}),
    )
    monkeypatch.setattr(
        sim,
        "_run_tran_pass",
        lambda _netlist, _work_dir: SimulationResult(
            success=True,
            return_code=0,
            measurements={"slew_rate": 1.2e7},
        ),
    )

    result = sim.run("* ota\n.end", include_transient=True)

    assert result.success is True
    assert result.return_code == 0
    assert result.pass_status["ac"]["success"] is False
    assert result.pass_status["ac"]["validation_success"] is True
    assert result.pass_status["dc"]["success"] is False
    assert result.pass_status["dc"]["validation_success"] is True
    assert result.pass_status["icmr"]["success"] is False
    assert result.pass_status["icmr"]["validation_success"] is True
    assert result.pass_status["tran"]["validation_success"] is True


def test_ngspice_top_level_success_rejects_nonzero_expected_pass(monkeypatch):
    sim = NgspiceSimulator(timeout_sec=30)

    monkeypatch.setattr(
        sim,
        "_run_ac_pass",
        lambda _netlist, _work_dir: SimulationResult(
            success=False,
            return_code=1,
            raw_stderr="ngspice aborted after writing partial measurements",
            measurements={"dc_gain_db": 40.0, "unity_gain_bandwidth": 2.0e7, "phase_margin": 65.0},
        ),
    )
    monkeypatch.setattr(
        sim,
        "_run_dc_pass",
        lambda _netlist, _work_dir: SimulationResult(
            success=True,
            return_code=0,
            measurements={"total_power": 1.0e-4, "output_swing": 0.8, "saturation_margin": 0.05},
        ),
    )
    monkeypatch.setattr(
        sim,
        "_run_icmr_pass",
        lambda _netlist, _work_dir: SimulationResult(success=False, return_code=-1, measurements={}),
    )
    monkeypatch.setattr(
        sim,
        "_run_tran_pass",
        lambda _netlist, _work_dir: SimulationResult(success=True, return_code=0, measurements={"slew_rate": 1.2e7}),
    )

    result = sim.run("* ota\n.end", include_transient=True)

    assert result.success is False
    assert result.return_code == 1
    assert result.pass_status["ac"]["validation_success"] is True
    assert result.pass_status["ac"]["execution_success"] is False


def test_ngspice_default_exec_has_no_python_timeout(monkeypatch, tmp_path):
    captured = []

    class FakeProcess:
        returncode = 0
        stdout = "simulation executed\n"
        stderr = ""

    def fake_run(_cmd, capture_output, text, timeout, cwd):
        captured.append(timeout)
        return FakeProcess()

    monkeypatch.setattr("simulator.ngspice.subprocess.run", fake_run)

    NgspiceSimulator()._exec_ngspice("* ota\n.end", str(tmp_path), suffix="dc")
    NgspiceSimulator(timeout_sec=12)._exec_ngspice("* ota\n.end", str(tmp_path), suffix="ac")

    assert captured == [None, 12]


def test_runner_retries_missing_required_transient(monkeypatch, tmp_path):
    calls = {}

    def fake_run_tran(self, netlist, work_dir):
        calls["netlist"] = netlist
        calls["work_dir"] = work_dir
        return SimulationResult(
            success=True,
            return_code=0,
            measurements={
                "slew_rate": 6.2e7,
                "slew_rate_pos": 6.8e7,
                "slew_rate_neg": 6.2e7,
            },
            raw_stdout="simulation executed\n",
        )

    monkeypatch.setattr("simulator.ngspice.NgspiceSimulator._run_tran_pass", fake_run_tran)
    runner = AnalogRFIRFlowRunner.__new__(AnalogRFIRFlowRunner)
    sim_result = SimulationResult(
        success=False,
        return_code=1,
        measurements={"dc_gain_db": 29.0},
        pass_status={
            "tran": {
                "success": False,
                "return_code": -1,
                "missing_measurements": ["slew_rate"],
                "required_for_run": True,
                "stderr_tail": "Timeout after 30s",
            }
        },
    )
    flow_meta = {}

    runner._retry_missing_required_transient(
        sim=NgspiceSimulator(ngspice_bin="ngspice", timeout_sec=30),
        netlist_str="* ota\n.tran 1n 10n\n.end",
        output_dir=tmp_path,
        sim_result=sim_result,
        flow_meta=flow_meta,
    )

    assert calls["netlist"].startswith("* ota")
    assert calls["work_dir"].endswith("transient_retry")
    assert sim_result.measurements["slew_rate"] == 6.2e7
    assert sim_result.pass_status["tran"]["missing_measurements"] == []
    assert sim_result.pass_status["tran_initial"]["missing_measurements"] == ["slew_rate"]
    assert flow_meta["transient_retry"]["recovered"] is True


def test_ablation_plan_builds_case_seed_schema_jobs(tmp_path):
    plan = {
        "base_config": "configs/default.yaml",
        "schemas": ["inputs/ota/five_transistor/five_transistor_ota.yaml"],
        "seeds": [3],
        "base_overrides": {"optimizer": {"generations": 5}},
        "cases": [
            {
                "name": "optimizer_only",
                "family": "baseline",
                "description": "unit",
                "overrides": {"agent": {"rounds": 1}, "postprocess": {"policy": "off"}},
            }
        ],
    }

    jobs = build_jobs(plan, output_dir=tmp_path, selected_cases=[], selected_schemas=[], selected_seeds=[])

    assert len(jobs) == 1
    assert jobs[0]["case"] == "optimizer_only"
    assert jobs[0]["config"]["optimizer"]["generations"] == 5
    assert jobs[0]["config"]["optimizer"]["seed"] == 3
    assert jobs[0]["config"]["postprocess"]["policy"] == "off"
    assert jobs[0]["config"]["output"]["runs_dir"].startswith(str(tmp_path))


def test_ablation_plan_merges_local_config_and_key_file(tmp_path):
    plan = {
        "base_config": "configs/default.yaml",
        "schemas": ["inputs/ota/five_transistor/five_transistor_ota.yaml"],
        "seeds": [3],
        "base_overrides": {"llm": {"model": "deepseek-v4-flash"}},
        "cases": [
            {
                "name": "deterministic_case",
                "overrides": {"agent": {"rounds": 2}, "llm": {"provider": "deterministic"}},
            }
        ],
    }

    jobs = build_jobs(
        plan,
        output_dir=tmp_path,
        selected_cases=[],
        selected_schemas=[],
        selected_seeds=[],
        local_overrides=[{"llm": {"provider": "deepseek", "timeout": 180}}],
        llm_api_key_file="/home/user/.config/analogrf-ir/deepseek.key",
    )

    assert jobs[0]["config"]["llm"]["provider"] == "deterministic"
    assert jobs[0]["config"]["llm"]["timeout"] == 180
    assert jobs[0]["config"]["llm"]["api_key_file"] == "/home/user/.config/analogrf-ir/deepseek.key"


def test_configure_llm_api_key_expands_user_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    key_file = home / ".config" / "analogrf-ir" / "deepseek.key"
    key_file.parent.mkdir(parents=True)
    key_file.write_text("secret-from-file\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ANALOGRF_IR_TEST_KEY", raising=False)
    args = argparse.Namespace(
        llm_api_key="",
        llm_api_key_file="~/.config/analogrf-ir/deepseek.key",
        llm_api_key_stdin=False,
        llm_api_key_env="ANALOGRF_IR_TEST_KEY",
    )

    _configure_llm_api_key(args)

    assert os.environ["ANALOGRF_IR_TEST_KEY"] == "secret-from-file"


def test_adaptive_strategy_cli_and_short_reoptimization_budget():
    args = _parse_args(
        [
            "--postprocess-policy",
            "fallback",
            "--postprocess-near-feasible-ratio",
            "0.15",
            "--reopt-generations",
            "3",
            "--reopt-pop-size",
            "12",
            "--action-strategy",
            "combo_coarse_fine",
            "--runtime-profile",
            "ablation_fast",
            "--intervention-transient-policy",
            "targeted",
        ]
    )
    loop = DiagnosticAgentLoop.__new__(DiagnosticAgentLoop)
    loop.config = FlowConfig(
        generations=10,
        pop_size=40,
        reopt_generations=args.reopt_generations,
        reopt_pop_size=args.reopt_pop_size,
        postprocess_policy=args.postprocess_policy,
        postprocess_near_feasible_ratio=args.postprocess_near_feasible_ratio,
        runtime_profile=args.runtime_profile,
    )
    loop.emit = lambda _msg: None

    assert args.postprocess_policy == "fallback"
    assert args.action_strategy == "combo_coarse_fine"
    assert args.runtime_profile == "ablation_fast"
    assert args.intervention_transient_policy == "targeted"
    assert loop._short_reopt_generations() == 3
    assert loop._short_reopt_pop_size() == 12
    assert loop._stagnation_detected([{"best_loss": 1.0}, {"best_loss": 0.99}]) is True


def test_postprocess_fallback_decision_uses_near_feasible_estimate():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    runner = AnalogRFIRFlowRunner(
        config=FlowConfig(postprocess_policy="fallback", postprocess_near_feasible_ratio=0.20)
    )
    spec_model = SpecRegistry().select(state)
    gain_target = float(state.targets["dc_gain"].min or 1.0)

    near = runner._postprocess_decision(
        state,
        {"performance": {"dc_gain": 0.92 * gain_target, "unity_gain_bandwidth": 1.5e8, "phase_margin": 70.0, "output_swing": 0.82, "saturation_margin": 0.06, "power": 5e-5}},
        spec_model,
    )
    far = runner._postprocess_decision(
        state,
            {"performance": {"dc_gain": 0.40 * gain_target, "unity_gain_bandwidth": 1.5e8, "phase_margin": 70.0, "output_swing": 0.82, "saturation_margin": 0.06, "power": 5e-5}},
        spec_model,
    )

    assert near["run"] is True
    assert near["reason"] == "near-feasible optimizer estimate"
    assert far["run"] is False


def test_postprocess_fallback_runs_cascode_op_repair_before_ac_metrics():
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/telescopic/telescopic_ota_ihp130.yaml"),
        load_yaml_mapping("environment_ihp_sg13g2.yaml"),
    )
    runner = AnalogRFIRFlowRunner(
        config=FlowConfig(postprocess_policy="fallback", postprocess_near_feasible_ratio=0.20)
    )
    spec_model = SpecRegistry().select(state)

    decision = runner._postprocess_decision(
        state,
        {
            "performance": {
                "dc_gain": -20.0,
                "unity_gain_bandwidth": 0.0,
                "phase_margin": 0.0,
                "slew_rate": 1.0,
                "output_swing": 0.1,
                "power": 1e-6,
            }
        },
        spec_model,
    )

    assert decision["run"] is True
    assert decision["near_feasible"] is False
    assert decision["cascode_op_repair"] is True
    assert decision["reason"] == "cascode operating-point repair required before AC metrics are reliable"


def test_cascode_op_repair_prioritizes_telescopic_initial_search():
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/telescopic/telescopic_ota_ihp130.yaml"),
        load_yaml_mapping("environment_ihp_sg13g2.yaml"),
    )

    points = _cascode_candidate_points(
        state,
        {"vbias_tail": 0.7281, "vbias_ncas": 0.6559, "vbias_pcas": 0.432},
        1.2,
    )

    guided = [point for point in points[:12] if point["phase"] == "telescopic_initial_search"]

    assert guided
    assert all(0.30 <= point["vbias_tail"] <= 0.75 for point in guided)
    assert all(0.45 <= point["vbias_ncas"] <= 0.90 for point in guided)
    assert all(0.35 <= point["vbias_pcas"] <= 0.80 for point in guided)
    assert any(point["vbias_tail"] != 0.7281 for point in guided)


def test_cascode_candidate_selection_avoids_collapsed_negative_gain_points():
    selected = _select_cascode_candidate(
        [
            {
                "success": True,
                "score": 1.0,
                "spec_pass": False,
                "op_required_margin": -0.40,
                "measurements": {"dc_gain_db": -42.0, "unity_gain_bandwidth": 0.0, "output_swing": 0.58},
                "phase": "collapsed",
            },
            {
                "success": True,
                "score": 25.0,
                "spec_pass": False,
                "op_required_margin": -0.08,
                "measurements": {"dc_gain_db": 39.0, "unity_gain_bandwidth": 2.0e5, "output_swing": 0.78},
                "phase": "recovering",
            },
        ]
    )

    assert selected["phase"] == "recovering"


def test_llm_json_loader_extracts_object_from_non_strict_response():
    data = _loads_json_object(
        "The selected actions are:\n"
        '{"selected_actions": [{"action_id": "a", "decision": "apply"}], "rationale": "ok"}\n'
        "Done."
    )

    assert data["selected_actions"][0]["action_id"] == "a"
    assert data["rationale"] == "ok"


def test_agent_loop_defaults_to_twenty_iterations_and_routes_on_stop_reason():
    args = _parse_args([])
    loop = DiagnosticAgentLoop.__new__(DiagnosticAgentLoop)

    assert DEFAULT_AGENT_MAX_ITERATIONS == 20
    assert args.agent_rounds == 20
    assert loop._route_after_diagnostics({"stop_reason": "spec satisfied"}) == "stop"
    assert loop._route_after_diagnostics({"stop_reason": "maximum iterations reached"}) == "stop"
    assert loop._route_after_diagnostics({"stop_reason": "", "round_index": 19, "max_rounds": 20}) == "write_command"


def test_llm_api_key_sources_configure_selected_env(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    direct_args = _parse_args(["--llm-api-key", "direct-key"])
    _configure_llm_api_key(direct_args)
    assert direct_args.llm_api_key_env == "DEEPSEEK_API_KEY"
    assert direct_args.llm_api_key == "direct-key"
    assert direct_args.llm_api_key_file == ""
    assert direct_args.llm_api_key_stdin is False
    assert os.environ["DEEPSEEK_API_KEY"] == "direct-key"

    key_file = tmp_path / "deepseek.key"
    key_file.write_text("file-key\n", encoding="utf-8")
    file_args = _parse_args(["--llm-api-key-env", "CUSTOM_KEY", "--llm-api-key-file", str(key_file)])
    _configure_llm_api_key(file_args)
    assert os.environ["CUSTOM_KEY"] == "file-key"


def test_llm_api_key_stdin_and_mutual_exclusion(monkeypatch):
    class FakeStdin:
        def read(self):
            return "stdin-key\n"

    monkeypatch.delenv("STDIN_KEY", raising=False)
    monkeypatch.setattr("main.sys.stdin", FakeStdin())
    stdin_args = _parse_args(["--llm-api-key-env", "STDIN_KEY", "--llm-api-key-stdin"])
    _configure_llm_api_key(stdin_args)
    assert os.environ["STDIN_KEY"] == "stdin-key"

    try:
        _parse_args(["--llm-api-key", "one", "--llm-api-key-file", "two"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("mutually exclusive API key sources should fail")


def test_agent_loop_read_diagnostics_sets_stop_reason(tmp_path):
    loop = DiagnosticAgentLoop.__new__(DiagnosticAgentLoop)
    loop.emit = lambda _msg: None
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    state.diagnostics["result"] = {"status": {"spec_pass": True, "failed_targets": []}}
    state_path = tmp_path / "design_state.yaml"
    state.to_yaml(state_path)

    output = loop._read_schema_diagnostics_node(
        {
            "last_design_state": str(state_path),
            "round_index": 4,
            "max_rounds": 20,
        }
    )

    assert output["stop_reason"] == "spec satisfied"
    assert output["last_spec_pass"] is True

    state.diagnostics["result"] = {"status": {"spec_pass": False, "failed_targets": ["dc_gain"]}}
    state.to_yaml(state_path)
    output = loop._read_schema_diagnostics_node(
        {
            "last_design_state": str(state_path),
            "round_index": 20,
            "max_rounds": 20,
        }
    )

    assert output["stop_reason"] == "maximum iterations reached"
    assert output["last_failed_targets"] == ["dc_gain"]


def test_agent_loop_final_log_outputs_result_summary():
    messages = []
    loop = DiagnosticAgentLoop.__new__(DiagnosticAgentLoop)
    loop.emit = messages.append
    loop._best_summary = None

    loop._print_final_result(
        {
            "stop_reason": "spec satisfied",
            "max_rounds": 20,
            "rounds": [
                {
                    "artifact_dir": "runs/iter_003",
                    "spec_pass": True,
                    "failed_targets": [],
                    "best_loss": 0.0,
                }
            ],
        }
    )

    log = "\n".join(messages)
    assert "LangGraph final result" in log
    assert "stop_reason: spec satisfied" in log
    assert "completed_iterations: 1" in log
    assert "spec_pass: True" in log


def test_agent_loop_tracks_best_verified_round():
    loop = DiagnosticAgentLoop.__new__(DiagnosticAgentLoop)

    worse = {"spec_pass": False, "failed_targets": ["dc_gain", "output_swing"], "unverified_targets": [], "best_loss": 120.0}
    better = {"spec_pass": False, "failed_targets": ["phase_margin"], "unverified_targets": [], "best_loss": 95.0}
    unverified = {"spec_pass": False, "failed_targets": [], "unverified_targets": ["slew_rate"], "best_loss": 1.0}
    passing = {"spec_pass": True, "failed_targets": [], "unverified_targets": [], "best_loss": 100.0}

    assert loop._is_better_summary(worse, None) is True
    assert loop._is_better_summary(better, worse) is True
    assert loop._is_better_summary(better, unverified) is True
    assert loop._is_better_summary(passing, better) is True
    assert loop._is_better_summary(worse, passing) is False


def test_ablation_summary_uses_best_verified_result_not_latest(tmp_path):
    run_dir = tmp_path / "job"
    early = run_dir / "iter_001"
    late = run_dir / "iter_002"
    early.mkdir(parents=True)
    late.mkdir(parents=True)
    (early / "result.json").write_text(
        json.dumps(
            {
                "status": {"spec_pass": False, "failed_targets": ["phase_margin"], "best_loss": 90.0},
                "measurements": {"dc_gain_db": 49.0},
            }
        ),
        encoding="utf-8",
    )
    (late / "result.json").write_text(
        json.dumps(
            {
                "status": {"spec_pass": False, "failed_targets": ["dc_gain", "output_swing"], "best_loss": 160.0},
                "measurements": {"dc_gain_db": -40.0},
            }
        ),
        encoding="utf-8",
    )

    summary = _latest_result_summary(run_dir)

    assert summary["result_json"].endswith("iter_001/result.json")
    assert summary["failed_targets"] == ["phase_margin"]


def test_ablation_skip_existing_reruns_failed_result(monkeypatch, tmp_path):
    run_dir = tmp_path / "job"
    failed = run_dir / "iter_001"
    failed.mkdir(parents=True)
    (failed / "result.json").write_text(
        json.dumps(
            {
                "status": {"spec_pass": False, "failed_targets": ["dc_gain"], "best_loss": 100.0},
                "measurements": {"dc_gain_db": 20.0},
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def fake_run(command, *, cwd, check):
        calls.append((command, cwd, check))
        passed = run_dir / "iter_002"
        passed.mkdir()
        (passed / "result.json").write_text(
            json.dumps(
                {
                    "status": {"spec_pass": True, "failed_targets": [], "best_loss": 0.0},
                    "measurements": {"dc_gain_db": 30.0},
                }
            ),
            encoding="utf-8",
        )
        return argparse.Namespace(returncode=0)

    monkeypatch.setattr("scripts.run_ablation.subprocess.run", fake_run)

    record = _execute_job(
        {"name": "job", "runs_dir": run_dir},
        ["python", "main.py"],
        {"name": "job"},
        skip_existing=True,
    )

    assert calls
    assert record["status"] == "passed"
    assert record["summary"]["spec_pass"] is True


def test_optimizer_update_keeps_inversion_region_out_of_spice_region():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    apply_optimizer_meta_to_state(
        state,
        {
            "decoded": {"M1": {"gm_id": 18.0, "L": 500e-9}, "__global__": {}},
            "transistor_params": {
                "M1": {
                    "W": 2e-6,
                    "L": 500e-9,
                    "id": 20e-6,
                    "vds": 0.4,
                    "vdsat": 0.12,
                    "gm_id": 18.0,
                    "region": "moderate",
                    "inversion_region": "moderate",
                }
            },
        },
    )

    assert state.transistors["M1"].parameters.region == "saturation"


def test_validation_checks_explicit_cross_role_symmetry_labels():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    state.transistors["M5"].parameters.W = 1.0e-6
    state.transistors["M8"].parameters.W = 2.0e-6

    report = Validator().validate(state, layers=[4], include_custom=False)
    messages = [item.message for item in report.errors()]

    assert any("M5/M8" in message and "W mismatch" in message for message in messages)


def test_symmetric_design_variable_initial_mismatch_is_error():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    m2_gm_id = next(dv for dv in state.design_variables if dv.device == "M2" and dv.variable == "gm_id")
    m2_gm_id.initial = 19.0

    report = Validator().validate(state, layers=[4])
    messages = [item.message for item in report.errors()]

    assert any("sym_M1_M2" in message and "initial" in message for message in messages)


def test_optimizer_reduces_symmetric_design_variables_and_decodes_copies():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    evaluator = CircuitEvaluator(state, create_pygmid_adapter())

    assert evaluator.symmetry_reduced is True
    assert evaluator.n_vars < len(state.design_variables)
    assert any(
        set(group["members"]) >= {"M1.gm_id", "M2.gm_id"}
        for group in evaluator.encoded_variable_groups
    )

    x = [dv.initial if dv.initial is not None else 0.5 * (dv.range.min + dv.range.max) for dv in evaluator.encoded_design_variables]
    decoded = evaluator.decode_x(x)

    assert decoded["M1"]["gm_id"] == decoded["M2"]["gm_id"]
    assert decoded["M1"]["L"] == decoded["M2"]["L"]
    assert decoded["M3"]["gm_id"] == decoded["M4"]["gm_id"]
    assert decoded["M7"]["L"] == decoded["M9"]["L"]


def test_postprocess_width_updates_copy_to_symmetric_peers():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    state.transistors["M7"].parameters.W = 2.0e-6
    state.transistors["M9"].parameters.W = 5.0e-6

    set_symmetric_width(state, "M7", 3.0e-6)

    assert state.transistors["M7"].parameters.W == 3.0e-6
    assert state.transistors["M9"].parameters.W == 3.0e-6


def test_netlist_realizes_wide_and_long_devices_with_layout_units():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    state.process.device_style = "subckt"
    state.process.max_finger_width = 10e-6
    state.process.max_W = 200e-6
    state.process.max_L = 10e-6
    state.transistors["M6"].parameters.W = 25e-6
    state.transistors["M6"].parameters.L = 12e-6
    state.transistors["M6"].L_strategy = 12e-6

    netlist = generate_netlist(state)
    m6 = state.transistors["M6"].parameters

    assert "layout M6" in netlist
    assert "XM6 vout n1 M6_ser1 vdd" in netlist
    assert "XM6_S2 M6_ser1 n1 vdd vdd" in netlist
    assert "ng=3" in netlist
    assert m6.layout_fingers == 3
    assert m6.layout_series == 2
    assert m6.layout_finger_W <= state.process.max_finger_width
    assert m6.layout_segment_L <= state.process.max_L

    report = Validator().validate(state, layers=[4])
    messages = [item.message for item in report.errors()]
    assert not any("layout realization" in message for message in messages)


def test_optimizer_and_netlist_include_slew_rate():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    evaluator = CircuitEvaluator(state, create_pygmid_adapter())
    x = [dv.initial if dv.initial is not None else 0.5 * (dv.range.min + dv.range.max) for dv in state.design_variables]

    _obj, _violation, meta = evaluator.evaluate(x)
    perf = meta["performance"]
    netlist = generate_netlist(state)

    assert perf["slew_rate"] > 0
    assert perf["slew_rate_pos"] > 0
    assert perf["slew_rate_neg"] > 0
    assert perf["output_swing"] > 0
    assert perf["icmr_max"] >= perf["icmr_min"]
    assert "sr_deficit" in meta["loss_breakdown"]
    assert "swing_deficit" in meta["loss_breakdown"]
    assert "icmr_min_excess" not in meta["loss_breakdown"]
    assert ".tran" in netlist


def test_optimizer_loss_uses_pass_oriented_targets_and_pm_window():
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/current_mirror/current_mirror_ota_ihp130.yaml"),
        load_yaml_mapping("environment_ihp_sg13g2.yaml"),
    )
    evaluator = CircuitEvaluator(state, create_pygmid_adapter())
    x = [
        dv.initial if dv.initial is not None else 0.5 * (dv.range.min + dv.range.max)
        for dv in evaluator.encoded_design_variables
    ]
    _obj, _violation, meta = evaluator.evaluate(x)
    tp = meta["transistor_params"]

    _loss, failing_breakdown = evaluator._compute_loss(
        {
            "dc_gain": 20.0,
            "unity_gain_bandwidth": 1.0e8,
            "phase_margin": 80.0,
            "slew_rate": 6.0e7,
            "output_swing": 0.7,
            "saturation_margin": 0.02,
            "power": 1.0e-4,
        },
        tp,
    )
    _loss_pass, passing_breakdown = evaluator._compute_loss(
        {
            "dc_gain": 35.0,
            "unity_gain_bandwidth": 4.0e7,
            "phase_margin": 62.0,
            "slew_rate": 3.0e7,
            "output_swing": 0.7,
            "saturation_margin": 0.02,
            "power": 1.0e-4,
        },
        tp,
    )

    assert failing_breakdown["target:dc_gain:min"] > 0
    assert failing_breakdown["target_window:phase_margin"] > 0
    assert failing_breakdown["target_tradeoff:gain_shortfall_unity_gain_bandwidth_excess"] > 0
    assert "target:dc_gain:min" not in passing_breakdown
    assert "target_window:phase_margin" not in passing_breakdown
    assert "target_tradeoff:gain_shortfall_unity_gain_bandwidth_excess" not in passing_breakdown


def test_optimizer_loss_prioritizes_hard_target_pass_over_soft_advisory_terms():
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/current_mirror/current_mirror_ota_ihp130.yaml"),
        load_yaml_mapping("environment_ihp_sg13g2.yaml"),
    )
    evaluator = CircuitEvaluator(state, create_pygmid_adapter())
    x = [
        dv.initial if dv.initial is not None else 0.5 * (dv.range.min + dv.range.max)
        for dv in evaluator.encoded_design_variables
    ]
    _obj, _violation, meta = evaluator.evaluate(x)
    tp = meta["transistor_params"]

    _loss, breakdown = evaluator._compute_loss(
        {
            "dc_gain": 35.0,
            "unity_gain_bandwidth": 4.0e7,
            "phase_margin": 62.0,
            "slew_rate": 3.0e7,
            "output_swing": 0.58,
            "saturation_margin": 0.02,
            "power": 1.0e-4,
        },
        tp,
    )

    assert breakdown["target:output_swing:min"] > 0
    assert breakdown["target_gate:hard_fail_count"] >= 250.0
    assert breakdown["target_gate:hard_violation"] > breakdown["target:output_swing:min"]


def test_optimizer_best_index_prefers_hard_target_pass_before_soft_loss():
    state = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/current_mirror/current_mirror_ota_ihp130.yaml"),
        load_yaml_mapping("environment_ihp_sg13g2.yaml"),
    )
    evaluator = CircuitEvaluator(state, create_pygmid_adapter())
    optimizer = NSGA2Optimizer(state, evaluator)
    soft_loss_with_hard_fail = Individual(
        x=np.array([0.0]),
        objectives=np.array([1.0]),
        rank=0,
        feasible=True,
        meta={"hard_target_failures": 1, "hard_target_violation_score": 0.1},
    )
    hard_pass_with_higher_soft_loss = Individual(
        x=np.array([1.0]),
        objectives=np.array([100.0]),
        rank=1,
        feasible=True,
        meta={"hard_target_failures": 0, "hard_target_violation_score": 0.0},
    )

    assert optimizer._get_best_index([soft_loss_with_hard_fail, hard_pass_with_higher_soft_loss]) == 1


def test_comparator_estimator_reports_extended_metrics():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/comparator/strongarm/strongarm_v1.yaml"), default_environment())
    evaluator = CircuitEvaluator(state, create_pygmid_adapter())
    x = [dv.initial if dv.initial is not None else 0.5 * (dv.range.min + dv.range.max) for dv in state.design_variables]

    _obj, _violation, meta = evaluator.evaluate(x)
    perf = meta["performance"]
    netlist = generate_netlist(state)

    for key in (
        "delay",
        "regeneration_time",
        "reset_time",
        "offset",
        "input_referred_noise",
        "kickback_noise",
        "energy_per_comparison",
        "pdp",
        "input_capacitance",
        "output_swing",
        "icmr",
        "metastability_margin",
        "max_sample_rate",
        "area",
        "power",
    ):
        assert key in perf
        assert perf[key] >= 0
    assert perf["output_swing"] > 0
    assert perf["icmr_max"] >= perf["icmr_min"]
    assert "Vclk clk" in netlist
    assert "Vclkb clkb" in netlist
    assert ".tran" in netlist


def test_comparator_validator_accepts_metric_context_and_symmetry_labels():
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/comparator/strongarm/strongarm_v1.yaml"), default_environment())

    report = Validator().validate(state, layers=[3, 4])
    messages = [item.message for item in report.warnings()]

    assert not any("Comparator target set is missing" in message for message in messages)
    assert not any("Comparator dynamic estimates need" in message for message in messages)
    assert not any("should share one symmetry label" in message for message in messages)


def test_ngspice_transient_curve_extracts_slew_rate(tmp_path):
    curve = tmp_path / "tran_sweep.dat"
    curve.write_text(
        "\n".join(
            [
                "0 0.1",
                "1e-9 0.2",
                "2e-9 0.3",
                "3e-9 0.25",
                "4e-9 0.15",
                "5e-9 0.05",
            ]
        ),
        encoding="utf-8",
    )

    perf = NgspiceSimulator()._extract_tran_curve_performance(curve)

    assert perf["slew_rate_pos"] > 0
    assert perf["slew_rate_neg"] > 0
    assert perf["slew_rate"] == min(perf["slew_rate_pos"], perf["slew_rate_neg"])


def test_ngspice_headroom_extracts_swing_and_icmr():
    netlist = """
* VDSAT_headroom_factor: 1.3
XM1 net1 vinn tail gnd sg13_lv_nmos W=1u L=500n
XM2 n1 vinp tail gnd sg13_lv_nmos W=1u L=500n
XM3 net1 net1 vdd vdd sg13_lv_pmos W=1u L=500n
XM4 n1 net1 vdd vdd sg13_lv_pmos W=1u L=500n
XM5 tail vbias_tail gnd gnd sg13_lv_nmos W=1u L=500n
XM6 vout n1 vdd vdd sg13_lv_pmos W=1u L=500n
XM7 vout vbias_stage2 gnd gnd sg13_lv_nmos W=1u L=500n
Vdd vdd 0 DC 1.2
Cload vout 0 200f
.end
"""
    op = {
        "M1": {"vgs": 0.55, "vdsat": 0.12},
        "M3": {"vdsat": 0.10},
        "M5": {"vdsat": 0.15},
        "M6": {"vdsat": 0.16},
        "M7": {"vdsat": 0.14},
    }

    perf = NgspiceSimulator()._extract_headroom_performance(netlist, op)

    assert perf["output_swing"] > 0
    assert perf["output_swing_low"] < perf["output_swing_high"]
    assert perf["icmr_min"] > 0
    assert perf["icmr_max"] >= perf["icmr_min"]


def test_ngspice_saturation_margin_uses_configured_vdsat_factor():
    netlist = """
* VDSAT_headroom_factor: 1.3
XM1 out vin tail gnd sg13_lv_nmos W=1u L=500n
XM2 out bias vdd vdd sg13_lv_pmos W=1u L=500n
XM3 tail vbias gnd gnd sg13_lv_nmos W=1u L=500n
Vdd vdd 0 DC 1.2
.end
"""
    op = {
        "M1": {"vds": 0.22, "vdsat": 0.10},
        "M2": {"vds": 0.50, "vdsat": 0.20},
        "M3": {"vds": 0.245, "vdsat": 0.18},
    }

    perf = NgspiceSimulator()._extract_headroom_performance(netlist, op)

    expected_margin = 0.245 - 1.3 * 0.18
    assert np.isclose(perf["saturation_margin"], expected_margin)
    assert np.isclose(perf["saturation_required_gap"], expected_margin - 0.01)


def test_ngspice_icmr_sweep_extracts_common_mode_range():
    netlist = """
* VDSAT_headroom_factor: 1.3
M1 vout vinp tail 0 nmos W=1u L=500n
M2 n1 vinn tail 0 nmos W=1u L=500n
M3 vout vout vdd vdd pmos W=1u L=500n
M4 tail vbias 0 0 nmos W=1u L=500n
Vdd vdd 0 DC 1.2
Vinp vinp 0 DC 0.6 AC 0.5
Vinn vinn 0 DC 0.6 AC -0.5
* .meas dc icmr_min: computed from operating-point headroom in simulator
* .meas dc icmr_max: computed from operating-point headroom in simulator
.end
"""

    class FakeIcmrSimulator(NgspiceSimulator):
        def __init__(self):
            super().__init__()
            self.common_modes = []

        def _exec_ngspice(self, sample_netlist, work_dir, suffix):
            sources = re.findall(
                r"^\s*(Vinp|Vinn)\s+\S+\s+(\S+)\s+DC\s+(\S+)",
                sample_netlist,
                flags=re.IGNORECASE | re.MULTILINE,
            )
            assert len(sources) == 2
            driven = [float(value) for _name, neg, value in sources if neg == "0"]
            feedback = [(neg, float(value)) for _name, neg, value in sources if neg == "vout"]
            if feedback:
                assert len(driven) == 1
                assert len(feedback) == 1
                assert feedback[0][1] == 0.0
            else:
                assert len(driven) == 2
                assert abs(driven[0] - driven[1]) < 1e-12
            vcm = driven[0]
            self.common_modes.append(vcm)
            valid = 0.35 <= vcm <= 0.85
            vds = 0.30 if valid else 0.08
            op = {
                name: {"gm": 1e-4, "vds": vds, "vdsat": 0.10}
                for name in ("M1", "M2", "M3", "M4")
            }
            return SimulationResult(success=True, return_code=0, operating_points=op)

    sim = FakeIcmrSimulator()
    result = sim._run_icmr_pass(netlist, None)

    assert len(sim.common_modes) == 75
    assert 0.34 <= result.measurements["icmr_min"] <= 0.38
    assert 0.82 <= result.measurements["icmr_max"] <= 0.86
    assert result.measurements["icmr"] > 0.45
    assert result.measurements["icmr_valid_points"] > 0


def test_compensation_tune_stops_after_passing_candidate(tmp_path):
    class PassingSimulator:
        timeout_sec = 30.0

        def __init__(self):
            self.calls = 0

        def run(self, _netlist, work_dir=None, include_transient=False):
            self.calls += 1
            return SimulationResult(
                success=True,
                return_code=0,
                measurements={
                    "dc_gain_db": 80.0,
                    "unity_gain_bandwidth": 1.2e8,
                    "phase_margin": 63.0,
                    "slew_rate": 5.0e7,
                    "output_swing": 0.85,
                    "saturation_margin": 0.12,
                    "icmr_min": 0.7,
                    "icmr_max": 0.9,
                    "total_power": 2e-4,
                },
            )

    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    sim = PassingSimulator()

    result = tune_two_stage_compensation(
        state,
        sim,
        tmp_path,
        max_base_candidates=20,
        max_refine_candidates=0,
        max_load_candidates=0,
    )

    assert sim.calls == 1
    assert result["spec_pass"] is True
    assert result["early_stop_reason"] == "robust_spec_pass"
    assert result["evaluated_candidates"] == 1


def test_compensation_tune_rejects_negative_phase_margin(tmp_path):
    class SequenceSimulator:
        timeout_sec = 30.0

        def __init__(self):
            self.calls = 0

        def run(self, _netlist, work_dir=None, include_transient=False):
            self.calls += 1
            if self.calls == 1:
                measurements = {
                    "dc_gain_db": 60.0,
                    "unity_gain_bandwidth": 1.1e8,
                    "phase_margin": -5.0,
                    "output_swing": 0.7,
                    "icmr_min": 0.7,
                    "icmr_max": 0.9,
                    "total_power": 2e-4,
                }
            else:
                measurements = {
                    "dc_gain_db": 45.0,
                    "unity_gain_bandwidth": 7.0e7,
                    "phase_margin": 55.0,
                    "output_swing": 0.7,
                    "icmr_min": 0.7,
                    "icmr_max": 0.9,
                    "total_power": 2e-4,
                }
            return SimulationResult(success=True, return_code=0, measurements=measurements)

    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    sim = SequenceSimulator()

    result = tune_two_stage_compensation(
        state,
        sim,
        tmp_path,
        max_base_candidates=2,
        max_refine_candidates=0,
        max_load_candidates=0,
        max_current_candidates=0,
        time_budget_sec=20,
    )

    assert sim.calls == 2
    assert result["measurements"]["phase_margin"] > 0


def test_two_stage_feasibility_report_has_required_sections(tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    checker = TwoStageMillerFeasibilityChecker(
        state,
        create_pygmid_adapter(),
        FeasibilityConfig(samples=120, seed=3, top_count=4),
    )

    report = checker.run()
    checker.write_report(tmp_path, report)

    assert report["classification"]["label"] in {
        "roughly feasible",
        "near-feasible",
        "likely infeasible",
        "infeasible due to hard physical bounds",
    }
    assert report["best_candidates"]
    assert report["bottleneck_ranking"]
    assert report["validation_plan"]
    assert (tmp_path / "feasibility_report.json").exists()
    assert (tmp_path / "feasibility_report.md").exists()

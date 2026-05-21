import json

from asir.capabilities import detect_circuit_capabilities
from core.compensation import has_miller_rc_compensation
from asir.profiles import select_circuit_profile
from core.environment import default_environment
from core.rule_registry import list_rules
from core.validator import Validator
from diagnostics import apply_attribution_guided_tuning, execute_tuning_tool_commands, write_tuning_tool_command
from flow.llm_planner import DeepSeekSchemaPlanner, LLMPlannerConfig
from flow.agent_loop import DiagnosticAgentLoop
from feasibility import FeasibilityConfig, TwoStageMillerFeasibilityChecker
from main import DEFAULT_AGENT_MAX_ITERATIONS, _parse_args
from flow.state_update import apply_optimizer_meta_to_state
from frontends.design_input import load_design_input
from frontends.yaml_loader import build_design_state_from_yaml, load_yaml_mapping
from netlist.generator import generate_netlist
from optimizer.nsga2 import CircuitEvaluator
from optimizer.problem import OptimizationProblem
from outputs.artifacts import ArtifactWriter
from postprocess.registry import PostprocessConfig, PostprocessContext, PostprocessRegistry
from postprocess.source_follower import _candidate_points
from postprocess.two_stage import tune_two_stage_compensation
from pygmid.adapter import create_pygmid_adapter
from simulator.ngspice import NgspiceSimulator, SimulationResult
from specs.models import SpecRegistry


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
    two_stage = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"),
        default_environment(),
    )
    source_follower = build_design_state_from_yaml(
        load_yaml_mapping("inputs/ota/source_follower_boosted/source_follower_boosted_ota.yaml"),
        default_environment(),
    )

    two_stage_problem = OptimizationProblem.from_state(two_stage)
    source_follower_problem = OptimizationProblem.from_state(source_follower)
    registry = PostprocessRegistry()

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

    assert two_stage_problem.estimator_key == "ota_two_stage_miller"
    assert source_follower_problem.estimator_key == "ota_compact"
    assert [item.name for item in registry.resolve(two_stage_context)] == ["two_stage"]
    assert [item.name for item in registry.resolve(source_follower_context)] == ["source_follower_operating_point"]


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


def test_artifact_writer_emits_result_json(tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    result = SimulationResult(
        success=True,
        return_code=0,
        measurements={
            "dc_gain_db": 61.0,
            "unity_gain_bandwidth": 5.1e8,
            "phase_margin": 65.0,
            "slew_rate": 6.0e7,
            "output_swing": 0.7,
            "icmr_min": 0.8,
            "icmr_max": 0.7,
            "total_power": 2e-4,
        },
    )
    best_meta = {
        "performance": {
            "dc_gain": 60.0,
            "unity_gain_bandwidth": 5.0e8,
            "phase_margin": 64.0,
            "slew_rate": 5.5e7,
            "output_swing": 0.7,
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
        flow_meta={"options": {}},
    )

    assert artifacts.design_state.exists()
    assert artifacts.netlist.exists()
    assert artifacts.causal_diagnostics.exists()
    assert "!!python" not in artifacts.design_state.read_text(encoding="utf-8")
    payload = json.loads(artifacts.result_json.read_text(encoding="utf-8"))
    assert payload["status"]["spec_pass"] is True
    state_payload = load_yaml_mapping(artifacts.design_state)
    assert "diagnostics" in state_payload
    assert state_payload["diagnostics"]["result"]["status"]["spec_pass"] is True
    assert state_payload["diagnostics"]["causal_diagnostics"]["dependency_graph"]["nodes"]


def test_causal_diagnostics_rank_testable_root_causes_in_schema(tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/two_stage_miller/two_stage_miller_ota.yaml"), default_environment())
    result = SimulationResult(
        success=True,
        return_code=0,
        measurements={
            "dc_gain_db": 62.0,
            "unity_gain_bandwidth": 9.0e7,
            "phase_margin": 18.0,
            "slew_rate": 5.0e7,
            "output_swing": 0.7,
            "icmr_min": 0.8,
            "icmr_max": 0.7,
            "total_power": 2.0e-4,
        },
    )
    best_meta = {
        "performance": {
            "dc_gain": 62.0,
            "unity_gain_bandwidth": 9.0e7,
            "phase_margin": 18.0,
            "slew_rate": 5.0e7,
            "output_swing": 0.7,
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
    causal = state_payload["diagnostics"]["causal_diagnostics"]
    result_view = state_payload["diagnostics"]["result"]

    assert "phase_margin" in result_view["status"]["failed_targets"]
    assert any(item["metric"] == "phase_margin" for item in causal["failure_symptom_analysis"])
    assert causal["root_cause_attribution"]
    assert causal["agent_failure_attribution"]["by_failure"]
    assert causal["attribution_guided_tuning"]["by_failure"]
    assert any(
        action["knob"] == "global.Rz" and action["target_formula"] == "1/gm(second_stage_gain)"
        for item in causal["attribution_guided_tuning"]["by_failure"]
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
            "dc_gain_db": 34.0,
            "unity_gain_bandwidth": 2.0e8,
            "phase_margin": 75.0,
            "output_swing": 0.82,
            "icmr_min": 0.63,
            "icmr_max": 1.26,
            "total_power": 5.0e-5,
        },
    )
    best_meta = {
        "performance": {
            "dc_gain": 34.0,
            "unity_gain_bandwidth": 2.0e8,
            "phase_margin": 75.0,
            "output_swing": 0.82,
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
    top = causal["root_cause_attribution"][0]
    gain_path = causal["causal_paths"][0]["chain"]
    tuning_actions = causal["attribution_guided_tuning"]["by_failure"][0]["actions"]
    primary_action = tuning_actions[0]

    assert top["node"] != "device.M5.ro"
    assert "block.load_stage" in gain_path
    assert "device.M5.ro" not in gain_path
    assert causal["agent_failure_attribution"]["by_failure"][0]["minimal_causal_factor_set"]
    assert primary_action["knob"] == "M3.L"
    assert primary_action["action_id"]
    assert primary_action["direction"] == "increase"
    assert primary_action["apply_to"] == ["M3.L", "M4.L"]
    assert primary_action["range_update"]["type"] == "expand_upper_bound"
    assert causal["agent_failure_attribution"]["by_failure"][0]["tuning_plan"][0]["knob"] == "M3.L"

    application = apply_attribution_guided_tuning(state, round_index=1)
    assert application["applied_actions"]
    m3_l = next(dv for dv in state.design_variables if dv.device == "M3" and dv.variable == "L")
    m4_l = next(dv for dv in state.design_variables if dv.device == "M4" and dv.variable == "L")
    m1_gm_id = next(dv for dv in state.design_variables if dv.device == "M1" and dv.variable == "gm_id")
    assert m3_l.range.max > 5.0e-7
    assert m3_l.initial > 5.0e-7
    assert m4_l.initial == m3_l.initial
    assert m1_gm_id.initial > 15.0


def test_llm_schema_command_can_select_and_override_fine_grained_tuning_action(tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("inputs/ota/five_transistor/five_transistor_ota.yaml"), default_environment())
    result = SimulationResult(
        success=True,
        return_code=0,
        measurements={
            "dc_gain_db": 34.0,
            "unity_gain_bandwidth": 2.0e8,
            "phase_margin": 75.0,
            "output_swing": 0.82,
            "icmr_min": 0.63,
            "icmr_max": 1.26,
            "total_power": 5.0e-5,
        },
    )
    best_meta = {
        "performance": {
            "dc_gain": 34.0,
            "unity_gain_bandwidth": 2.0e8,
            "phase_margin": 75.0,
            "output_swing": 0.82,
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
    m3_action = next(action for action in available if action["knob"] == "M3.L")
    m1_action = next(action for action in available if action["knob"] == "M1.gm_id")

    command = write_tuning_tool_command(
        state,
        round_index=1,
        selected_actions=[
            {
                "action_id": m3_action["action_id"],
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
    application = execute_tuning_tool_commands(state, round_index=1)
    m3_l = next(dv for dv in state.design_variables if dv.device == "M3" and dv.variable == "L")
    m4_l = next(dv for dv in state.design_variables if dv.device == "M4" and dv.variable == "L")
    m1_gm_id = next(dv for dv in state.design_variables if dv.device == "M1" and dv.variable == "gm_id")
    m5_gm_id = next(dv for dv in state.design_variables if dv.device == "M5" and dv.variable == "gm_id")

    assert command["args"]["available_actions"]
    assert command["args"]["custom_actions"][0]["action_id"] == "manual_M5_gm_id_set"
    assert command["llm_editable_fields"]["decision_values"] == ["apply", "skip"]
    assert "custom_actions" in command["llm_editable_fields"]
    assert application["command_id"] == command["id"]
    assert len(application["applied_actions"]) == 2
    assert m3_l.initial == 6.0e-7
    assert m4_l.initial == 6.0e-7
    assert m3_l.range.max == 6.5e-7
    assert m1_gm_id.initial == 15.0
    assert m5_gm_id.initial == 12.0
    assert m5_gm_id.range.min == 8.0
    assert m5_gm_id.range.max == 18.0
    assert application["skipped_actions"][0]["llm_reason"] == "Hold input gm/ID for this round."


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
        LLMPlannerConfig(provider="deepseek", model="deepseek-v4-flash", api_key_env="DEEPSEEK_API_KEY")
    ).write_command(state, round_index=2, agent_model={"failed_targets": ["unity_gain_bandwidth"]})

    command = result.command
    assert result.used_llm is True
    assert command["llm_planner"]["status"] == "ok"
    assert command["args"]["selected_actions"][0]["decision"] == "skip"
    assert command["args"]["custom_actions"][0]["knob"] == "global.I_tail"


def test_agent_loop_defaults_to_twenty_iterations_and_routes_on_stop_reason():
    args = _parse_args([])
    loop = DiagnosticAgentLoop.__new__(DiagnosticAgentLoop)

    assert DEFAULT_AGENT_MAX_ITERATIONS == 20
    assert args.agent_rounds == 20
    assert loop._route_after_diagnostics({"stop_reason": "spec satisfied"}) == "stop"
    assert loop._route_after_diagnostics({"stop_reason": "maximum iterations reached"}) == "stop"
    assert loop._route_after_diagnostics({"stop_reason": "", "round_index": 19, "max_rounds": 20}) == "write_command"


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
    messages = [item.message for item in report.warnings()]

    assert any("M5/M8" in message and "W mismatch" in message for message in messages)


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
    assert "icmr_min_excess" in meta["loss_breakdown"]
    assert ".tran" in netlist


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
                    "dc_gain_db": 55.0,
                    "unity_gain_bandwidth": 1.2e8,
                    "phase_margin": 60.0,
                    "output_swing": 0.7,
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

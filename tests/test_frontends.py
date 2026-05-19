from frontends.spice_parser import parse_spice_text
from frontends.yaml_loader import build_design_state_from_yaml
from main import _default_environment


def test_spice_parser_canonicalizes_generated_mos_ids():
    data = parse_spice_text(
        """
        MM1 net1 vinn tail gnd nmos W=10u L=300n
        MM2 n1 vinp tail gnd nmos W=10u L=300n
        MM3 net1 net1 vdd vdd pmos W=20u L=1u
        MM4 n1 net1 vdd vdd pmos W=20u L=1u
        MM5 tail vbias_tail gnd gnd nmos W=8u L=500n
        MM6 vout n1 vdd vdd pmos W=60u L=500n
        MM7 vout vbias_stage2 gnd gnd nmos W=30u L=500n
        Rz n1 ncc 3.5k
        Cc ncc vout 500f
        """,
        design_name="roundtrip_two_stage",
    )
    ids = [dev["id"] for dev in data["topology"]["devices"] if dev.get("type") in {"nmos", "pmos"}]
    roles = {dev["id"]: dev["role"] for dev in data["topology"]["devices"] if dev.get("type") in {"nmos", "pmos"}}

    assert ids == ["M1", "M2", "M3", "M4", "M5", "M6", "M7"]
    assert roles["M4"] == "current_mirror_load"
    assert roles["M6"] == "second_stage_gain"
    assert data["topology"]["architecture"] == "two-stage"


def test_spice_yaml_can_seed_optimizer_design_state():
    data = parse_spice_text(
        """
        M1 net1 vinn tail gnd nmos W=10u L=300n
        M2 n1 vinp tail gnd nmos W=10u L=300n
        M3 net1 net1 vdd vdd pmos W=20u L=1u
        M4 n1 net1 vdd vdd pmos W=20u L=1u
        M5 tail vbias_tail gnd gnd nmos W=8u L=500n
        M6 vout n1 vdd vdd pmos W=60u L=500n
        M7 vout vbias_stage2 gnd gnd nmos W=30u L=500n
        Rz n1 ncc 3.5k
        Cc ncc vout 500f
        """,
        design_name="parsed_two_stage",
    )

    state = build_design_state_from_yaml(data, _default_environment())

    assert state.design_name == "parsed_two_stage"
    assert len(state.topology.devices) == 7
    assert set(state.transistors) == {"M1", "M2", "M3", "M4", "M5", "M6", "M7"}
    assert state.global_parameters["Cc"] == 500e-15
    assert state.global_parameters["Rz"] == 3500.0
    assert any(variable.variable == "Cc" for variable in state.design_variables)


def test_spice_parser_recognizes_tail_bias_mirror():
    data = parse_spice_text(
        "XM8 vbias_tail vbias_tail gnd gnd sg13_lv_nmos W=1u L=500n",
        design_name="tail_bias_mirror",
    )

    device = data["topology"]["devices"][0]
    assert device["id"] == "M8"
    assert device["role"] == "tail_bias_mirror"


def test_generic_mos_models_map_to_active_process():
    data = parse_spice_text(
        """
        M1 out in 0 0 nmos W=1u L=130n
        M2 out out vdd vdd pmos W=2u L=130n
        """,
        design_name="process_model_mapping",
    )
    env = _default_environment()
    env["process"]["nmos_model"] = "sg13_lv_nmos"
    env["process"]["pmos_model"] = "sg13_lv_pmos"

    state = build_design_state_from_yaml(data, env)

    assert state.topology.devices[0].model == "sg13_lv_nmos"
    assert state.topology.devices[1].model == "sg13_lv_pmos"

import json

from core.environment import default_environment
from frontends.design_input import load_design_input
from frontends.yaml_loader import build_design_state_from_yaml, load_yaml_mapping
from outputs.artifacts import ArtifactWriter
from simulator.ngspice import SimulationResult
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
        schema_path="ir/schema.yaml",
        spice_path=spice,
        spice_yaml_out=out,
    )

    assert bundle.source_kind == "spice"
    assert out.exists()
    assert bundle.state.topology.architecture == "two-stage"
    assert bundle.state.global_parameters["Cc"] == 500e-15


def test_spec_registry_selects_ota_and_comparator():
    registry = SpecRegistry()
    ota = build_design_state_from_yaml(load_yaml_mapping("ir/schema_two_stage.yaml"), default_environment())
    comparator = build_design_state_from_yaml(load_yaml_mapping("inputs/strongarm_v1.yaml"), default_environment())

    assert registry.select(ota).name == "ota"
    assert registry.select(comparator).name == "comparator"
    assert registry.select(ota).measurement_key("dc_gain") == "dc_gain_db"
    assert registry.select(comparator).measurement_key("offset") == "offset"


def test_artifact_writer_emits_result_json(tmp_path):
    state = build_design_state_from_yaml(load_yaml_mapping("ir/schema_two_stage.yaml"), default_environment())
    result = SimulationResult(
        success=True,
        return_code=0,
        measurements={"dc_gain_db": 61.0, "unity_gain_bandwidth": 5.1e8, "phase_margin": 65.0, "total_power": 2e-4},
    )
    best_meta = {
        "performance": {"dc_gain": 60.0, "unity_gain_bandwidth": 5.0e8, "phase_margin": 64.0, "power": 2.1e-4},
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
    payload = json.loads(artifacts.result_json.read_text(encoding="utf-8"))
    assert payload["status"]["spec_pass"] is True

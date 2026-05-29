from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from asir.design import build_design
from asir.io.v1_yaml import build_topology_from_v1_dict, embed_asir_output
from core.environment import resolve_project_path
from frontends.spice_parser import parse_spice_file, write_yaml
from frontends.yaml_loader import build_design_state_from_yaml, load_yaml_mapping, yaml_has_explicit_topology
from schemas.design_state import DesignState


StateBuilder = Callable[[dict[str, Any], Path, str], DesignState]


@dataclass
class DesignInput:
    state: DesignState
    schema_path: Path
    source_kind: str
    source_path: Path | None = None
    generated_yaml_path: Path | None = None
    asir_summary: dict[str, Any] = field(default_factory=dict)


def load_design_input(
    *,
    env: dict[str, Any],
    schema_path: str | Path,
    topology: str = "auto",
    spice_path: str | Path | None = None,
    spice_yaml_out: str | Path | None = None,
    legacy_builder: StateBuilder | None = None,
    run_asir: bool = True,
) -> DesignInput:
    """Normalize SPICE/YAML input into a DesignState.

    Schema remains the source of truth. A SPICE input is first translated to the
    editable YAML schema shape, then ASIR may add semantic annotations to the
    YAML payload before DesignState construction.
    """
    source_kind = "schema"
    source_path: Path | None = None
    generated_yaml_path: Path | None = None

    if spice_path:
        source_kind = "spice"
        source_path = resolve_project_path(spice_path)
        generated_yaml_path = (
            resolve_project_path(spice_yaml_out)
            if spice_yaml_out
            else resolve_project_path(Path("runs") / "tmp_spice_import" / f"{source_path.stem}.yaml")
        )
        generated = parse_spice_file(source_path)
        if run_asir:
            generated = attach_asir(generated)
        write_yaml(generated, generated_yaml_path)
        schema = generated
        final_schema_path = generated_yaml_path
        topology = "yaml"
    else:
        final_schema_path = resolve_project_path(schema_path)
        schema = load_yaml_mapping(final_schema_path)
        if run_asir:
            enriched = attach_asir(schema)
            if enriched is not schema:
                schema = enriched

    if topology == "yaml" or (topology == "auto" and yaml_has_explicit_topology(final_schema_path)):
        state = build_design_state_from_yaml(schema, env)
    elif legacy_builder is not None:
        state = legacy_builder(env, final_schema_path, topology)
    else:
        raise ValueError(
            f"{final_schema_path} does not contain explicit topology; provide a legacy state builder"
        )

    asir_summary = {}
    if isinstance(schema.get("asir_output"), dict):
        asir_summary = _asir_summary(schema["asir_output"])

    return DesignInput(
        state=state,
        schema_path=final_schema_path,
        source_kind=source_kind,
        source_path=source_path or final_schema_path,
        generated_yaml_path=generated_yaml_path,
        asir_summary=asir_summary,
    )


def attach_asir(schema: dict[str, Any]) -> dict[str, Any]:
    """Attach ASIR semantic layers when the input topology is compatible."""
    if schema.get("asir_output"):
        return schema
    topology = schema.get("topology") or {}
    if not isinstance(topology, dict) or not topology.get("devices"):
        return schema
    try:
        design = build_design(build_topology_from_v1_dict(schema))
    except Exception:
        return schema
    return embed_asir_output(schema, design)


def write_schema(path: str | Path, state: DesignState) -> Path:
    output = resolve_project_path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    state.to_yaml(output)
    return output


def load_schema_mapping(path: str | Path) -> dict[str, Any]:
    with resolve_project_path(path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _asir_summary(asir_output: dict[str, Any]) -> dict[str, Any]:
    layers = asir_output.get("layers") or {}
    semantic = layers.get("semantic_primitive_graph") or {}
    primitives = semantic.get("primitives") or semantic.get("nodes") or []
    return {
        "name": asir_output.get("name"),
        "domain": asir_output.get("domain"),
        "circuit_class": asir_output.get("circuit_class"),
        "comparator_family": asir_output.get("comparator_family"),
        "primitive_count": len(primitives) if isinstance(primitives, list) else 0,
        "layers": sorted(layers.keys()),
    }

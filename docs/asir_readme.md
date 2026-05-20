# Analog Semantic IR (ASIR)

ASIR is a research prototype for executable semantic representations of analog comparator circuits.

The project intentionally does not implement layout automation, SPICE simulation, reinforcement learning, generic analog synthesis, or full-chip support. It focuses on graph abstraction, symbolic dependencies, phase semantics, rewrite reasoning, and explainability for three comparator families:

- StrongARM comparator
- Double-tail comparator
- Sense amplifier comparator

## Architecture

ASIR keeps four graph layers separate:

1. `TopologyGraph`: transistor-level connectivity using a `networkx.MultiGraph`.
2. `SemanticPrimitiveGraph`: rule-extracted primitives such as `differential_pair`, `cross_coupled_latch`, `reset_switch`, `tail_current_source`, and `sampling_switch`.
3. `DependencyGraph`: symbolic causal dependencies such as `delay <- gm, CL`, `offset <- mismatch, device_area`, and `noise <- kT/C, gm`.
4. `OperationalPhaseGraph`: temporal activation semantics for `reset`, `amplify`, `regenerate`, and `saturate`.

Each semantic primitive contains:

- `role`
- `equations`
- `constraints`
- `active_phases`
- `state_variables`

## Quick Start

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Export a StrongARM ASIR YAML:

```powershell
python main.py strongarm --out exports --summary
```

Export all comparator examples:

```powershell
python main.py all --out exports
```

Compile an AnalogRF-IR YAML input and embed the layered ASIR output back into the YAML document:

```powershell
python main.py from-yaml inputs/comparator/strongarm/strongarm_v1.yaml --out exports/strongarm_v1_asir.yaml --summary
```

Write only the ASIR bundle:

```powershell
python main.py from-yaml inputs/comparator/strongarm/strongarm_v1.yaml --out exports/strongarm_asir_only.yaml --asir-only
```

Embed `asir_output` directly back into the input YAML:

```powershell
python main.py from-yaml inputs/comparator/strongarm/strongarm_v1.yaml --in-place
```

Trace symbolic causes of delay:

```powershell
python main.py strongarm --trace delay
```

Run sample forward propagation:

```powershell
python main.py strongarm --propagate
```

Compare two comparator topologies at the semantic primitive level:

```powershell
python main.py strongarm --compare sense-amplifier
```

## Project Layout

```text
asir/
  topology.py              Layer 1 topology graph
  semantic.py              Layer 2 semantic primitive graph
  dependency.py            Layer 3 symbolic dependency graph
  phase.py                 Layer 4 operational phase graph
  rewrite.py               topology rewrite reasoning
  extraction/rules.py      rule-based semantic extraction
  examples/comparators.py  StrongARM, double-tail, sense amplifier examples
  io/yaml_export.py        YAML export
  cli.py                   command-line interface
tests/
  test_asir.py             smoke tests for extraction and reasoning
inputs/
  *_v1.yaml                designer-editable topology inputs
```

## Designer Input

Designers do not need to edit Python. The intended input is the AnalogRF-IR YAML shape:

```yaml
schema_version: '0.1'
design_name: strongarm_comparator
topology:
  class: comparator
  architecture: strongarm
  global_nets:
    - {name: vdd, type: supply}
    - {name: vss, type: ground}
  ports:
    - {id: vinp, direction: input}
    - {id: outp, direction: output}
  devices:
    - id: M_INP
      role: input_pair_left
      type: nmos
      connections: {drain: intp, gate: vinp, source: tail, body: vss}
```

The compiler preserves the original YAML and adds:

```yaml
asir_output:
  layers:
    topology_graph: ...
    semantic_primitive_graph: ...
    dependency_graph: ...
    operational_phase_graph: ...
```

## Design Notes

The key invariant is that topology, functionality, symbolic dependency, and temporal activation are not collapsed into one graph. Cross-layer links are expressed by stable IDs and exported together only as a layered bundle.

The first extractor is deliberately rule-based. An LLM annotation layer can be added later as a producer of annotations or candidate primitive labels, but it should not replace the executable graph model.

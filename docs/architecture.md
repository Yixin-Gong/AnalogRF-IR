# Architecture

AnalogRF-IR keeps the flow modular so circuit-family behavior lives in profiles
and schema data instead of being hard-coded into one optimizer path.

## Main Flow

`main.py` builds a `FlowConfig` and runs either one optimization pass or the
diagnosis-guided multi-round loop. The single-pass flow is implemented by
`flow.runner.AnalogRFIRFlowRunner`.

The runner executes these stages:

1. Load environment settings.
2. Load YAML or import SPICE and build `DesignState`.
3. Select the IR profile, capabilities, and spec model.
4. Validate the input schema.
5. Load gm/ID lookup data or compact fallback support.
6. Run the optimizer and compact evaluator.
7. Apply optimizer metadata back to the schema state.
8. Generate SPICE.
9. Run postprocess repair and ngspice verification when available.
10. Write structured artifacts.

## Important Packages

```text
asir/         Semantic IR, profile selection, capabilities, comparator examples
core/         Environment loading, units, rules, regions, and validation
feasibility/  Physics-informed feasibility checks
flow/         End-to-end orchestration and diagnosis-guided loops
frontends/    YAML loader and SPICE importer
netlist/      DesignState-to-SPICE generation
optimizer/    Problem model, evaluator registry, and NSGA-II
outputs/      Run artifact writers
postprocess/  ngspice-guided repair and compensation tuning
pygmid/       gm/ID adapter and lookup-table helpers
schemas/      Dataclasses for the design state schema
simulator/    ngspice execution and measurement extraction
specs/        Circuit-family spec models
```

## Profiles And Capabilities

Profiles are selected in `asir/profiles.py` from the circuit class and
architecture. A profile defines metric aliases, required context, validation
policy, generated objectives, and rule filters.

Capabilities are derived after profile selection. Examples include:

- `two_stage_gain`
- `miller_rc_compensation`
- `source_follower_regulation`
- `dynamic_latch`
- `tail_current_mirror`
- `output_bias_mirror`

The optimizer receives a single `OptimizationProblem`, while postprocess repair
is selected from capabilities. This lets topologies change active objectives,
constraints, estimators, and repair passes without changing the optimizer API.

## Validation Strategy

Validation runs at multiple stages: input, post-optimizer, postprocess, and
post-ngspice. Generic schema checks stay in shared validation code. OTA,
comparator, and future RF behavior should be activated through profiles and
profile-specific rules.

Compact estimates are used to guide the search. Simulator measurements and
operating-point extraction are treated as the higher-authority validation layer
when ngspice and process models are available.

## Agent Loop

When `--agent-rounds` is greater than 1, `flow.agent_loop.DiagnosticAgentLoop`
runs iterative schema tuning. Each round reads the previous `design_state.yaml`,
asks an OpenAI-compatible planner for schema actions when configured, applies
approved actions, and starts the next run. If the LLM key is missing, the flow
records fallback status and keeps deterministic artifacts usable for tests.

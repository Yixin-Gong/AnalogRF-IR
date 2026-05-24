# Schema Guide

Circuit inputs are YAML files under `inputs/`. A schema should make the design
intent explicit: topology, devices, roles, variables, targets, constraints,
losses, and requested evaluations.

## Input Families

```text
inputs/
  ota/
    five_transistor/
    two_stage_miller/
    source_follower_boosted/
  comparator/
    strongarm/
    double_tail/
    sense_amplifier/
```

Run schemas with:

```bash
python main.py --topology yaml --schema <path-to-schema> --agent-rounds 1
```

## Core Sections

Common sections include:

- `topology`: circuit name, class, architecture, devices, ports, roles, and connectivity.
- `targets`: desired metrics such as gain, bandwidth, phase margin, delay, power, or swing.
- `constraints`: process, operating-region, and variable bounds.
- `design_variables`: optimizer variables for devices and global parameters.
- `loss_terms`: explicit objective terms. If omitted, default target-tracking losses are generated where possible.
- `evaluations`: requested measurements for simulator or compact evaluation.
- `transistors`: optional physical seed values from a prior run or imported netlist.

User-authored input schemas should stay concise. They should describe design
intent, editable variables, and constraints. They should not contain generated
diagnostics, simulation logs, dependency graphs, or local intervention matrices.

Generated `runs/iter_###/design_state.yaml` files add compact result and tuning
summaries under `diagnostics`, but full evidence is kept in sibling JSON
artifacts. This keeps the schema readable while preserving traceability.

Avoid hiding topology-specific assumptions in Python when they can be declared
through roles, variables, constraints, targets, and evaluations.

## Generated Diagnostics Contract

Generated schemas use diagnostics as a compact decision interface:

- `diagnostics.result`: pass/fail status and compact measurements.
- `diagnostics.causal_diagnostics`: compact root causes, tuning actions,
  selected action traces, and evidence-gate summaries.
- `diagnostics.agent_tool_commands`: requested schema-level tuning commands.
- `diagnostics.previous_agent_tuning`: compact record of the last applied
  command when a new loop input is generated.

Large evidence stays outside the schema:

- Full dependency graph: `causal_diagnostics.json`.
- Full local intervention model and `A` matrix: `causal_diagnostics.json`.
- Simulation and optimizer logs: `sim_log.json`.
- Agent-facing debug report: `agent_diagnostics.json`.

Agents should read generated schemas, select existing actions or explicit
per-knob custom actions, and let the executor enforce the write policy.

## Physical Constraints

Every schema action must remain physically realizable:

- Matched or mirrored devices with the same `symmetry_label` must keep identical
  encoded variables and physical W/L values.
- Device W/L choices must stay within process limits after layout realization.
- Wide devices may be folded or parallelized; long devices may be segmented in
  series when the process style supports subckt emission.
- Invalid symmetry, range, or layout realization is a validation error, not a
  warning.
- The agent may only edit existing design variables and supported constraints.

## OTA Notes

The two-stage Miller OTA schema models an input OTA, second-stage inverter,
compensation capacitor `Cc`, zero-setting resistor `Rz`, tail bias mirror, and
output-stage bias mirror. For this family:

- Use explicit symmetry labels for matched pairs and mirrors.
- Declare `Cc` and `Rz` as globals only when the topology has Miller compensation.
- Keep target priorities realistic for compact optimization before ngspice signoff.
- Use the feasibility checker before expensive searches.
- Treat postprocess as an optional repair layer. Causal diagnosis and schema
  action planning should remain usable with postprocess disabled for ablations.

The source-follower-boosted OTA has no explicit `Rz-Cc` compensation network.
Treat the source follower as local output-resistance boosting with output
common-mode headroom as a central trade-off.

## Comparator Notes

Comparator schemas should include dynamic context such as clock, load, input
step, and reset/regeneration behavior. Useful targets include delay,
regeneration time, reset time, offset, input-referred noise, kickback, energy,
PDP/EDP, input capacitance, output swing, ICMR, metastability margin, maximum
sample rate, area, and average dynamic power.

The current comparator path is strongest for ASIR semantics and validation
coverage. Dedicated simulator testbenches are still needed before comparator
metrics should be treated as signoff-grade.

## Adding A New Schema

Recommended order:

1. Start from the closest existing family under `inputs/`.
2. Make device ids stable and human-readable.
3. Assign roles that match profile and rule expectations.
4. Add symmetry labels for matched pairs and mirrors.
5. Define bounded design variables for every knob the optimizer may touch.
6. Declare targets and evaluations before adding custom loss terms.
7. Run a small smoke optimization and inspect `runs/iter_###/design_state.yaml`.
8. Inspect `causal_diagnostics.json` when debugging root causes or intervention evidence.
9. Add or update regression tests if the schema exercises new behavior.

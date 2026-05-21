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

The schema should remain the source of truth. Avoid hiding topology-specific
assumptions in Python when they can be declared through roles, variables,
constraints, targets, and evaluations.

## OTA Notes

The two-stage Miller OTA schema models an input OTA, second-stage inverter,
compensation capacitor `Cc`, zero-setting resistor `Rz`, tail bias mirror, and
output-stage bias mirror. For this family:

- Use explicit symmetry labels for matched pairs and mirrors.
- Declare `Cc` and `Rz` as globals only when the topology has Miller compensation.
- Keep target priorities realistic for compact optimization before ngspice signoff.
- Use the feasibility checker before expensive searches.

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
8. Add or update regression tests if the schema exercises new behavior.

# AnalogRF-IR v0.1 Two-Stage OTA Method Report

## Abstract

AnalogRF-IR v0.1 uses a schema-driven flow for two-stage Miller OTA exploration. The designer provides a YAML schema or a SPICE netlist. The frontend builds a `DesignState`, ASIR enriches the topology when possible, the gm/ID plugin maps high-level design variables to compact transistor estimates, NSGA-II searches the design-variable space, and ngspice verifies the final candidate. The schema remains the source of truth throughout the flow.

## Flow

1. Load the environment file and process settings.
2. Load YAML directly or parse SPICE into YAML-like topology data.
3. Build and validate `DesignState`.
4. Select a circuit spec model from topology class and architecture.
5. Load gm/ID lookup tables, or fall back to an analytical compact model when tables are missing.
6. Run the optimizer over high-level variables such as `gm_id`, `L`, `I_tail`, `I_stage2`, `Cc`, and `Rz`.
7. Update the schema state with optimizer estimates.
8. Run two-stage post-processing: DC balance repair, headroom repair, and bounded compensation tuning.
9. Generate SPICE and run ngspice AC, DC, transient, and operating-point extraction.
10. Write structured JSON artifacts and the updated schema.

## Optimization Model

The optimizer searches in a high-level analog design space instead of directly sweeping every transistor width. The two-stage OTA model estimates gain, UGBW, phase margin, slew rate, power, output swing, and ICMR from gm/ID lookup values and topology roles. The generated transistor dimensions are compact-model candidates, not final layout-ready signoff sizes.

Loss terms are derived from schema targets. Hard and soft constraints are derived from process limits, operating-region rules, symmetry labels, current-mirror consistency, headroom, and target priorities. CMRR and PSRR are currently excluded from the optimizer objective until dedicated measurement paths are added.

## Phase Margin Handling

Phase margin is measured from exported ngspice AC sweep data rather than from a single principal-value `.meas` phase point. The simulator unwraps phase, finds unity-gain crossings, and reports the worst relevant crossing. This avoids false high PM values caused by phase wrapping.

Middlebrook return-ratio loop-gain validation is still planned. Until that is added, AC output transfer PM is useful but not a complete loop-stability signoff.

## Feasibility Check

The feasibility checker performs a physics-informed coarse search for two-stage Miller OTAs. It uses gm/ID, inversion coefficient estimates, current allocation, compensation ratios, headroom, ft, gain, slew-rate, and PM lower bounds to classify a target as roughly feasible, near-feasible, likely infeasible, or blocked by hard physical bounds. The checker outputs bottlenecks, candidate tables, relaxation suggestions, and a validation plan.

## Current IHP Result

The latest bounded-compensation IHP two-stage run is:

```text
runs/iter_078/
  dc_gain_db:             64.98 dB
  unity_gain_bandwidth:   14.01 MHz
  phase_margin:           45.95 deg
  slew_rate:              39.30 V/us
  output_swing:           0.690 V
  ICMR:                   0.625 V to 1.132 V
  total_power:            98.8 uW
```

The result shows that stability and low-power operation can be recovered, but the compact optimizer still needs stronger speed and slew-rate modeling for the target UGBW.

## Validation Requirements

Final candidate validation should include:

- ngspice `.op` extraction and saturation/headroom checks
- AC gain, UGBW, and phase margin from exported sweep data
- transient slew-rate measurement
- output swing and ICMR DC sweeps
- Middlebrook return-ratio loop-gain measurement
- residual comparison between compact estimates and ngspice measurements

## Known Limitations

- Compact speed prediction remains optimistic for some IHP two-stage candidates.
- Compensation tuning can improve a near-feasible point, but it cannot recover a structurally weak current allocation.
- Swing and ICMR are still fast headroom estimates.
- Comparator and RF sizing are roadmap items rather than complete optimizer flows.

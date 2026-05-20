# AnalogRF-IR v0.1 Development Progress

This file keeps only the current public development snapshot. Historical debug notes and append-only run history have been removed from the source tree in favor of structured per-run JSON artifacts.

## Current Scope

AnalogRF-IR v0.1 is a schema-driven analog/RF design automation prototype. The current strongest path is the two-stage Miller OTA flow with gm/ID compact sizing, physics-informed feasibility checks, bounded compensation tuning, and ngspice validation. Comparator examples are available through ASIR, but comparator sizing is not yet connected to the full optimizer loop.

## Recent Milestones

1. Added ASIR semantic enrichment for schema and SPICE inputs.
2. Added IHP SG13G2 support with lookup tables and subcircuit-style MOS netlisting.
3. Refactored the two-stage OTA topology to include explicit tail and output current-mirror bias devices.
4. Added slew-rate estimation, transient slew-rate extraction, output swing estimates, and input common-mode headroom estimates.
5. Added a physics-informed feasibility checker for two-stage Miller OTAs.
6. Reworked compensation tuning with bounded candidate budgets, per-candidate timeouts, rescue candidates, and early-stop criteria.
7. Replaced root-level Markdown run history with structured artifacts: `sim_log.json`, `agent_diagnostics.json`, and `result.json`.
8. Renamed the project and generated artifacts to AnalogRF-IR v0.1.

## Latest IHP Two-Stage OTA Snapshot

The most recent bounded compensation run produced:

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

This point passes gain, phase margin, power, output swing, and ICMR for the relaxed two-stage OTA target set. UGBW and positive slew rate remain the main bottlenecks, indicating that the compact optimizer model still needs stronger current allocation and speed constraints before the compensation sweep can reliably recover the final design.

## Current Risks

- The compact model can overestimate speed for IHP two-stage OTA candidates.
- Phase margin is checked from ngspice AC sweep data, but Middlebrook return-ratio validation is still pending.
- Swing and ICMR are fast operating-point headroom estimates, not full DC sweep signoff.
- ASIR comparator support is structural and semantic; sizing objectives are still future work.
- RF blocks are not yet modeled.

## Next Development Tasks

1. Tighten the two-stage compact model around `gm2`, `I_stage2`, `Cc`, UGBW, PM, and slew rate.
2. Add full DC sweep validation for output swing and input common-mode range.
3. Add Middlebrook loop-gain testbenches for compensated OTAs.
4. Add RF-specific schema/spec models for LNA, mixer, oscillator, matching network, and filter use cases.
5. Keep generated logs, comments, and public documentation in English.

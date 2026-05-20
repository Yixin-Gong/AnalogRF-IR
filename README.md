# AnalogRF-IR

Version: v0.1

Schema-driven analog and RF circuit optimization with an intermediate representation, gm/ID sizing, physics-informed feasibility checks, and ngspice validation.

AnalogRF-IR is a reusable Analog/RF IR and optimization flow that can ingest circuit schemas or SPICE netlists, diagnose feasibility, generate design candidates, and verify them with simulator-backed measurements.

## Current Status

The project is an active research prototype. The strongest path today is the two-stage Miller OTA flow on IHP SG13G2 130 nm and PTM 130 nm style schemas. Comparator ASIR examples are available, and the frontend can also parse simple SPICE netlists into schema-like input.

Current capabilities include:

- Schema-first circuit description. The YAML schema is treated as the source of truth.
- ASIR semantic representation for OTA and comparator topology reasoning.
- gm/ID-based compact sizing and NSGA-II optimization.
- IHP SG13G2 and PTM lookup-table support.
- Physics-informed feasibility checking for two-stage Miller OTAs.
- ngspice-backed AC, DC, transient slew-rate, and operating-point headroom validation.
- Structured JSON outputs for agents and downstream tools.
- OP-first post-processing for two-stage DC repair, Miller compensation tuning, and local current recovery.

## Repository Layout

```text
asir/                 Semantic IR prototype and comparator examples
core/                 Validation, design rules, regions, environment models
feasibility/          Physics-informed feasibility estimators
flow/                 End-to-end orchestration
frontends/            YAML and SPICE input frontends
ir/                   Example schema files
netlist/              Schema-to-SPICE netlist generation
optimizer/            NSGA-II and compact circuit evaluator
outputs/              Structured result and diagnostic artifact writers
postprocess/          ngspice-guided repair and compensation tuning
pygmid/               gm/ID lookup-table adapter and generation helpers
schemas/              Dataclasses for the design state schema
simulator/            ngspice execution and measurement extraction
scripts/              CLI helpers for feasibility, Pareto, and SPICE conversion
tables/               Lookup tables for supported process examples
tests/                Regression tests
docs/                 Development notes and method reports
```

## Requirements

- Python 3.10+
- NumPy
- pytest for tests
- ngspice for simulator-backed validation
- IHP SG13G2 PDK files if using `environment_ihp_sg13g2.yaml`

The current development environment uses Ubuntu 26.04 under WSL with ngspice installed in the Linux environment.

Recreate the Python environment and install dependencies:

```bash
rm -rf .venv
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
```

For WSL-based runs from this workspace:

```bash
wsl -d Ubuntu-26.04 -- bash -lc 'cd /mnt/d/AnalogRF-IR; rm -rf .venv; python3 -m venv .venv; . .venv/bin/activate; python3 -m pip install -r requirements.txt'
```

## Quick Start

Run the PTM two-stage OTA flow:

```bash
wsl -d Ubuntu-26.04 -- bash -lc 'cd /mnt/d/AnalogRF-IR; . .venv/bin/activate; python main.py \
  --env environment.yaml \
  --schema ir/schema_two_stage.yaml \
  --topology yaml \
  --generations 16 \
  --pop-size 32 \
  --seed 11'
```

Run a fast smoke test without post-processing:

```bash
wsl -d Ubuntu-26.04 -- bash -lc 'cd /mnt/d/AnalogRF-IR; . .venv/bin/activate; python main.py \
  --env environment.yaml \
  --schema ir/schema_two_stage.yaml \
  --topology yaml \
  --generations 3 \
  --pop-size 10 \
  --seed 31 \
  --skip-dc-repair \
  --skip-comp-tune'
```

Run the feasibility checker:

```bash
wsl -d Ubuntu-26.04 -- bash -lc 'cd /mnt/d/AnalogRF-IR; . .venv/bin/activate; python scripts/run_feasibility_check.py \
  --env environment.yaml \
  --schema ir/schema_two_stage.yaml \
  --topology yaml \
  --samples 300 \
  --seed 41'
```

Run the test suite:

```bash
wsl -d Ubuntu-26.04 -- bash -lc 'cd /mnt/d/AnalogRF-IR; . .venv/bin/activate; python -m pytest -q'
```

## Inputs And Outputs

Input is primarily schema YAML. The schema describes:

- topology and device roles
- process/environment data
- design variables such as `gm_id`, `L`, `I_tail`, `I_stage2`, `Cc`, and `Rz`
- target specs
- loss terms and evaluation requests

For two-stage Miller OTAs, ASIR models the series `Rz` and `Cc` network explicitly. The zero-cancellation target is `Rz ~= 1/gm2`, where `gm2` is the second-stage transconductance. Stability diagnostics first pull the dominant pole lower with `Cc`, then verify second-pole separation and `Rz` placement.

The flow writes one run directory under `runs/iter_###/`:

```text
design_state.yaml          Updated schema and transistor state
netlist.cir                Generated SPICE netlist
sim_log.json               Full structured optimizer and ngspice log
agent_diagnostics.json     Agent-oriented diagnostics and suggestions
result.json                Compact final status and measured specs
```

## Development Progress

Recent milestones:

- Merged the ASIR-style semantic frontend into the schema-driven flow.
- Added IHP SG13G2 support with lookup tables and subcircuit-style MOS netlisting.
- Converted the two-stage OTA topology to explicit tail and output current mirrors.
- Added slew-rate estimation and transient ngspice validation.
- Added output swing and input common-mode range estimates and ngspice headroom extraction.
- Added a physics-informed feasibility checker for two-stage Miller OTAs.
- Reworked two-stage compensation tuning with candidate budgets, per-candidate timeouts, stability rescue points, current recovery, and robust early-stop behavior.
- Added OTA ASIR primitives for input pair, mirror load, tail bias, second-stage inverter, and Miller compensation.
- Added explicit symmetry-label validation for mirror/reference device pairs.

Recent PTM two-stage OTA validation after OP-first repair and robust compensation tuning:

```text
runs/iter_003/
  dc_gain_db:             79.47 dB
  unity_gain_bandwidth:   101.90 MHz
  phase_margin:           48.35 deg
  slew_rate:              81.44 V/us
  output_swing:           0.822 V
  ICMR:                   0.598 V to 1.315 V
  total_power:            190.6 uW
  Cc:                     510.8 fF
  Rz:                     4.298 kohm
```

This point passes gain, unity-gain bandwidth, phase margin, slew rate, output swing, ICMR, and power targets. The final ngspice operating point keeps all nine MOS devices in saturation. Width/length symmetry is preserved for `M1/M2`, `M3/M4`, `M5/M8`, and `M7/M9`; mirror copy/reference devices can still show gm and VDS differences because they sit at different operating voltages.

## Known Limitations

- The compact optimizer can still overestimate speed for some two-stage OTA points, so ngspice remains the final authority.
- PM is now verified from exported ngspice AC sweep data, but loop-gain return-ratio validation is not yet fully integrated.
- Output swing and ICMR are currently operating-point headroom estimates, not full DC sweep signoff.
- Two-stage post-processing can repair bias and compensation locally, but very poor compact-model choices may still need a larger optimizer run.
- The ASIR comparator path is semantic and structural; comparator sizing objectives are not yet connected to the full optimizer loop.
- RF-specific blocks are not yet modeled. RF extensions should add noise figure, S-parameters, impedance matching, linearity, stability factor, and PSS/PAC-style workflows where supported.

## Roadmap

Near-term:

- Strengthen the two-stage optimizer around `I_stage2`, `gm2`, compensation capacitance, PM, UGBW, and slew-rate lower bounds.
- Add stricter swing and ICMR sweep testbenches.
- Add Middlebrook loop-gain validation for compensated OTAs.
- Improve schema diagnostics so feasibility failures directly suggest minimal spec relaxations.

Longer-term:

- Add RF building blocks such as LNAs, mixers, VCOs, RF amplifiers, matching networks, and filters.
- Extend the schema and ASIR layers with RF-specific specs and constraints.
- Add S-parameter, noise, linearity, compression, and stability metrics.
- Make optimizer, simulator, feasibility, and post-processing plugins independently swappable.
- Build a reusable analog/RF design-agent backend around structured JSON diagnostics.

## Project Vision

AnalogRF-IR aims to become a bridge between human-readable circuit intent and simulator-backed analog/RF design automation. The goal is not to replace expert circuit design judgment. The goal is to encode enough topology, physics, feasibility reasoning, and measurement structure that an engineer or agent can iterate faster, diagnose failures more clearly, and reuse optimization logic across circuit families.

The long-term target is a flow where designers edit a schema or provide a SPICE netlist, the system builds a semantic representation, estimates feasibility, proposes initial values, optimizes in a high-level design-variable space, validates with ngspice or other simulators, and writes structured artifacts that can be inspected by both humans and agents.

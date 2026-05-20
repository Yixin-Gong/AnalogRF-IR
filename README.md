# AnalogRF-IR

Version: v0.1

Schema-driven analog and RF circuit optimization with an intermediate representation, gm/ID sizing, physics-informed feasibility checks, and ngspice validation.

AnalogRF-IR is a reusable Analog/RF IR and optimization flow that can ingest circuit schemas or SPICE netlists, diagnose feasibility, generate design candidates, and verify them with simulator-backed measurements.

## Current Status

The project is an active research prototype. The strongest path today is the two-stage Miller OTA flow on IHP SG13G2 130 nm and PTM 130 nm style schemas. Comparator ASIR examples are available, and the frontend can also parse simple SPICE netlists into schema-like input.

Current capabilities include:

- Schema-first circuit description. The YAML schema is treated as the source of truth.
- ASIR semantic representation for topology reasoning and future cross-circuit reuse.
- gm/ID-based compact sizing and NSGA-II optimization.
- IHP SG13G2 and PTM lookup-table support.
- Physics-informed feasibility checking for two-stage Miller OTAs.
- ngspice-backed AC, DC, transient slew-rate, and operating-point headroom validation.
- Structured JSON outputs for agents and downstream tools.
- Post-processing for two-stage DC operating point repair and bounded compensation tuning.

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

The current development environment uses Ubuntu under WSL with ngspice installed in the Linux environment.

Install Python dependencies:

```bash
python3 -m pip install -r requirements.txt
```

For WSL-based runs from this workspace:

```bash
wsl -d Ubuntu-26.04 --cd /mnt/d/AnalogRF-IR -- python3 -m pip install -r requirements.txt
```

## Quick Start

Run the IHP two-stage OTA flow:

```bash
python3 main.py \
  --env environment_ihp_sg13g2.yaml \
  --schema ir/schema_two_stage.yaml \
  --generations 16 \
  --pop-size 36 \
  --seed 42
```

Run a fast smoke test without post-processing:

```bash
python3 main.py \
  --env environment_ihp_sg13g2.yaml \
  --schema ir/schema_two_stage.yaml \
  --generations 3 \
  --pop-size 10 \
  --seed 31 \
  --skip-dc-repair \
  --skip-comp-tune
```

Run the feasibility checker:

```bash
python3 scripts/run_feasibility_check.py \
  --env environment_ihp_sg13g2.yaml \
  --schema ir/schema_two_stage.yaml \
  --samples 3000 \
  --seed 41
```

Run the test suite:

```bash
python3 -m pytest tests/test_frontends.py tests/test_asir.py tests/test_modular_flow.py -q
```

## Inputs And Outputs

Input is primarily schema YAML. The schema describes:

- topology and device roles
- process/environment data
- design variables such as `gm_id`, `L`, `I_tail`, `I_stage2`, `Cc`, and `Rz`
- target specs
- loss terms and evaluation requests

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
- Reworked two-stage compensation tuning with candidate budgets, per-candidate timeouts, stability rescue points, and early-stop behavior.

Recent IHP two-stage OTA validation after bounded compensation tuning:

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

This point passes gain, phase margin, power, output swing, and ICMR. It still misses the current UGBW and positive slew-rate targets, which points to the next optimizer-model improvement rather than a compensation-sweep runtime problem.

## Known Limitations

- The compact optimizer can still overestimate speed for IHP two-stage OTA points.
- PM is now verified from exported ngspice AC sweep data, but loop-gain return-ratio validation is not yet fully integrated.
- Output swing and ICMR are currently operating-point headroom estimates, not full DC sweep signoff.
- Two-stage post-processing can repair bias and compensation locally, but it cannot fully compensate for a weak compact model choice.
- The ASIR comparator path is semantic and structural; comparator sizing objectives are not yet connected to the full optimizer loop.
- RF-specific blocks are not yet modeled. RF extensions should add noise figure, S-parameters, impedance matching, linearity, stability factor, and PSS/PAC-style workflows where supported.

## Roadmap

Near-term:

- Strengthen the IHP two-stage optimizer around `I_stage2`, `gm2`, compensation capacitance, PM, UGBW, and slew-rate lower bounds.
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

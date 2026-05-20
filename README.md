# AnalogRF-IR

Version: v0.1

AnalogRF-IR is a schema-driven analog and RF circuit optimization flow. It uses an intermediate representation (IR), ASIR semantic extraction, gm/ID sizing, physics-informed validation, NSGA-II optimization, and ngspice-backed measurements to iterate on analog circuit designs.

The project is intended for reusable circuit-family workflows: OTA, comparator, sample-and-hold, and future RF blocks should each select their own IR profile, rule checks, constraints, and objectives.

## Core Capabilities

- YAML-first circuit descriptions with structured devices, variables, targets, and simulation requests.
- ASIR semantic extraction for topology roles, symmetry groups, compensation networks, and dynamic comparator structure.
- IR-level circuit profiles that select family-specific metrics, required context, rule checks, constraints, and auto-generated objectives.
- gm/ID-based compact sizing and NSGA-II optimization.
- Physics-informed validation for operating regions, bias feasibility, symmetry, compensation, and dynamic comparator context.
- ngspice-backed AC, DC, transient, slew-rate, headroom, and operating-point checks.
- Structured run artifacts for agents, scripts, and manual review.

## Repository Layout

```text
asir/                 Semantic IR, profile selection, and extraction helpers
core/                 Validation, design rules, regions, and environment models
feasibility/          Physics-informed feasibility estimators
flow/                 End-to-end orchestration
frontends/            YAML and SPICE input frontends
inputs/               Circuit-family schema library
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

- Ubuntu Linux
- Python 3.10 or newer
- `python3-venv` and `pip`
- NumPy and the Python packages listed in `requirements.txt`
- pytest for regression tests
- ngspice for simulator-backed validation
- Process model files or lookup tables for the target technology

Install common Ubuntu packages:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ngspice
```

Create a fresh Python environment:

```bash
cd <path/to/your/AnalogRF-IR>
rm -rf .venv
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

## Inputs

The main inputs are:

- Environment YAML: technology, supply, simulator, model paths, and lookup-table configuration.
- Circuit schema YAML: topology, devices, roles, design variables, targets, loss terms, and evaluation requests.
- Optional SPICE netlist: imported before optimization when `--spice` is provided.

Use explicit paths in production runs. Do not rely on CLI defaults:

```bash
--env <path/to/your/environment.yaml>
--schema <path/to/your/circuit_schema.yaml>
--spice <path/to/your/input_netlist.spice>
```

Example schemas are organized by circuit family:

```text
inputs/
  ota/
    five_transistor/
      five_transistor_ota.yaml
    two_stage_miller/
      two_stage_miller_ota.yaml
    source_follower_boosted/
      source_follower_boosted_ota.yaml
  comparator/
    strongarm/
      strongarm_v1.yaml
    double_tail/
      double_tail_v1.yaml
    sense_amplifier/
      sense_amplifier_v1.yaml
```

`inputs/ota/two_stage_miller/two_stage_miller_ota.yaml` is the more complex OTA example. It models a five-transistor input OTA, a second-stage inverter, Miller capacitor `Cc`, zero-setting resistor `Rz`, and bias mirror devices.

`inputs/ota/source_follower_boosted/source_follower_boosted_ota.yaml` is a source-follower-regulated OTA example. It intentionally has no `Rz-Cc` compensation network; the source follower is treated as local output-resistance boosting with output common-mode headroom as the key trade-off.

## IR Profiles

Circuit-family behavior is selected in the IR layer through `asir/profiles.py`.

Each profile maps circuit class and architecture to:

- Metric aliases and metric groups.
- Required context parameters.
- Dynamic-device role policy.
- Static validation behavior.
- Auto-generated objective terms.
- Rule filters used by the validator.

The validator, spec registry, optimizer, and netlist generator consume the selected profile instead of hard-coding OTA or comparator behavior locally. To add a new circuit family, define a profile first, then attach only the rules, constraints, objectives, estimators, and simulator measurements that belong to that profile.

Example profile-driven behavior:

- OTA profiles use static operating-region checks, symmetry checks, compensation objectives, and two-stage stability diagnostics.
- Comparator profiles use dynamic-role checks, comparator metric coverage, clock/load context checks, and comparator-specific compact metrics.
- Sample-and-hold profiles can add acquisition, hold, droop, charge-injection, and settling objectives without changing OTA or comparator code paths.

## Running Optimization

Run a two-stage OTA optimization:

```bash
cd <path/to/your/AnalogRF-IR>
. .venv/bin/activate
python main.py \
  --env <path/to/your/environment.yaml> \
  --schema <path/to/your/two_stage_ota.yaml> \
  --topology yaml \
  --generations <number_of_generations> \
  --pop-size <population_size> \
  --seed <integer_seed>
```

Run a dynamic comparator optimization:

```bash
cd <path/to/your/AnalogRF-IR>
. .venv/bin/activate
python main.py \
  --env <path/to/your/environment.yaml> \
  --schema <path/to/your/comparator.yaml> \
  --topology yaml \
  --generations <number_of_generations> \
  --pop-size <population_size> \
  --seed <integer_seed> \
  --skip-dc-repair \
  --skip-comp-tune
```

Run a fast smoke test:

```bash
cd <path/to/your/AnalogRF-IR>
. .venv/bin/activate
python main.py \
  --env <path/to/your/environment.yaml> \
  --schema <path/to/your/circuit_schema.yaml> \
  --topology yaml \
  --generations 3 \
  --pop-size 10 \
  --seed <integer_seed> \
  --skip-dc-repair \
  --skip-comp-tune
```

Import a SPICE netlist and write the generated schema:

```bash
cd <path/to/your/AnalogRF-IR>
. .venv/bin/activate
python main.py \
  --env <path/to/your/environment.yaml> \
  --spice <path/to/your/input_netlist.spice> \
  --spice-yaml-out <path/to/your/generated_schema.yaml> \
  --schema <path/to/your/generated_schema.yaml> \
  --topology yaml
```

## Feasibility Check

Run the physics-informed feasibility checker before a full optimization:

```bash
cd <path/to/your/AnalogRF-IR>
. .venv/bin/activate
python scripts/run_feasibility_check.py \
  --env <path/to/your/environment.yaml> \
  --schema <path/to/your/circuit_schema.yaml> \
  --topology yaml \
  --samples <number_of_samples> \
  --seed <integer_seed>
```

## Validation Semantics

The flow validates the operating point before trusting higher-level metrics.

For two-stage Miller OTAs:

- Confirm bias and operating regions first.
- Preserve device symmetry for matched pairs and mirror/reference groups.
- Pull the dominant pole lower with `Cc` when diagnosing two-stage stability failures.
- Place the compensation zero with `Rz ~= 1/gm2`, where `gm2` is the second-stage transconductance.
- Use ngspice AC and operating-point measurements as the final authority after compact optimization.

For OTAs without explicit `Rz-Cc` compensation:

- Do not synthesize `Cc`, `Rz`, or Miller zero objectives from architecture alone.
- Derive stability diagnostics from the declared poles, loads, and local feedback paths.
- Treat source-follower regulation as output-resistance boosting, not compensation.
- Retune source-follower bias voltages after sizing so the regulated load, input pair, and tail source all land in a valid OP region.
- Allow a bounded regulated-source current-source width repair when bias-only retuning cannot satisfy phase margin, bandwidth, and OP headroom together.
- Check output common-mode headroom before trusting gain and bandwidth estimates.

For dynamic comparators:

- Check clock, load, input step, and dynamic role context.
- Measure or estimate delay, regeneration time, reset time, offset, input-referred noise, kickback, energy, PDP/EDP, input capacitance, output swing, ICMR, metastability margin, maximum sample rate, area, and average dynamic power.
- Treat compact estimates as optimizer guidance until dedicated transient, noise, and Monte Carlo testbenches are available.

## Outputs

Each run writes a new directory under:

```text
<path/to/your/AnalogRF-IR>/runs/iter_###/
```

Typical artifacts:

```text
design_state.yaml          Updated schema and transistor state
netlist.cir                Generated SPICE netlist
sim_log.json               Full structured optimizer and ngspice log
agent_diagnostics.json     Agent-oriented diagnostics and suggestions
result.json                Compact final status and measured specs
```

## Tests

Run the regression suite:

```bash
cd <path/to/your/AnalogRF-IR>
. .venv/bin/activate
python -m pytest -q
```

Run a syntax check over the main packages:

```bash
cd <path/to/your/AnalogRF-IR>
. .venv/bin/activate
python -m compileall asir core flow frontends netlist optimizer specs tests
```

## Extending The Flow

Recommended order for adding a new circuit type:

1. Add or update an IR profile in `asir/profiles.py`.
2. Add schema examples with clear device roles, symmetry labels, variables, targets, and evaluations.
3. Register profile-specific rules with `circuit_profiles=(...)`.
4. Add compact estimator support in the optimizer only for metrics that have defensible analytical models.
5. Add simulator measurements for final validation.
6. Add tests that prove the selected profile triggers the expected rules, constraints, and objectives.

Keep profile-specific behavior behind the IR profile boundary. Generic validation should stay generic; OTA, comparator, sample-and-hold, and RF-specific behavior should be activated by the selected profile.

## Known Limitations

- Compact estimates are optimizer guidance, not signoff.
- Output swing and ICMR are currently operating-point headroom estimates unless explicit sweeps are added.
- Comparator offset, noise, kickback, energy, and metastability need dedicated transient, noise, or Monte Carlo testbenches for signoff-grade validation.
- RF-specific blocks still require profile, schema, estimator, and simulator extensions for S-parameters, noise figure, matching, compression, linearity, and stability metrics.

## Project Vision

AnalogRF-IR aims to bridge human-readable circuit intent and simulator-backed analog/RF design automation. The goal is to encode enough topology, physics, feasibility reasoning, and measurement structure that an engineer or agent can iterate faster, diagnose failures more clearly, and reuse optimization logic across circuit families.

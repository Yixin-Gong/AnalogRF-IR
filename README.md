# AnalogRF-IR

AnalogRF-IR is a simulator-backed analog circuit optimization framework built
around explicit causal diagnosis and evidence-gated agent actions. The current
implementation focuses on OTA sizing in IHP SG13G2 130 nm under high-impedance
capacitive loading. Its main design objective is not unrestricted automatic
sizing, but a traceable diagnostic loop from failed metrics to typed causes,
local SPICE probes, optimizer-approved actions, and audited schema edits.

The central design contract is simple: optimizer, human review, deterministic
diagnosis, and LLM planning all operate through the same typed YAML schema, and
an executable edit is admitted only when it is supported by optimizer-side
evidence and physical validation.

> License: source-available, all rights reserved. See [LICENSE](LICENSE).

## Core Capabilities

- YAML design schemas for topology, device roles, variables, constraints,
  targets, evaluations, process setup, and concise diagnostics.
- gm/ID-guided surrogate sizing with bounded NSGA-II exploration.
- ngspice validation for AC gain, UGBW, phase margin, transient slew rate,
  output swing, power, and operating-point/headroom metrics.
- Typed causal diagnosis over device roles, bias paths, symmetry groups,
  compensation networks, pole/gain dependencies, and simulator evidence.
- Local SPICE intervention models for action-to-violation estimates.
- Constrained action optimization with symmetry copying, physical gates, and
  explicit apply/skip records.
- Optional topology-aware postprocess repair for operating-point balance,
  cascode headroom, and Miller compensation.
- DeepSeek-compatible LLM planning as a diagnosis and selection layer, not as
  an unrestricted schema editor.

## Method Overview

![Diagnosis-centered analog optimization architecture](docs/assets/analogdiag_architecture.png)

The flow uses a typed schema as the executable interface between human review,
agent diagnosis, surrogate search, ngspice validation, and repair. The schema
records topology roles, editable variables, targets, dependencies, and compact
simulator evidence. Planner suggestions are never applied directly; they must
map to admissible schema commands.

![Causal diagnosis and evidence-gated action selection](docs/assets/analogdiag_diagnosis_loop.png)

The diagnosis loop is the core of the project. Failed metrics are mapped to
typed causes, tested through local SPICE probes, converted into candidate
actions, and filtered by the evidence gate. Unsupported LLM edits are recorded
as skipped notes rather than executable changes.

## Evidence Gate

For normalized violation vector \(\mathbf{v}\), action response column
\(\mathbf{A}_{:,j}\), and weights \(w_i\), the action objective is

```text
J(v) = sum_i w_i v_i^2
v'_j = [v + A_:,j]_+
```

An action can be applied only if it passes the physical gate and is either
selected by the constrained optimizer or reduces the weighted violation
objective:

```text
admissible(a) := physical_gate(a) AND
                 (optimizer_selected(a) OR delta_J(a) < 0)
```

This rule is the boundary between diagnosis and authority: the LLM can explain
and select, but it cannot bypass simulator-backed optimizer evidence.

## IHP130 OTA Targets

The maintained OTA schemas use high-impedance \(C_L \approx 1\) pF targets.
They are not pad, cable, low-resistance, or 50 ohm load specifications.

| Topology | Gain | UGBW | PM | SR | Swing | Power |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 5T OTA | 25 dB | 25 MHz | 60 deg | 15 V/us | 0.65 V | 200 uW |
| Current-mirror OTA | 28 dB | 30 MHz | 60 deg | 25 V/us | 0.60 V | 300 uW |
| Folded-cascode OTA | 45 dB | 20 MHz | 60 deg | 12 V/us | 0.60 V | 600 uW |
| Telescopic OTA | 48 dB | 7 MHz | 60 deg | 6 V/us | 0.55 V | 300 uW |
| Two-stage Miller OTA | 57 dB | 20 MHz | 60 deg | 10 V/us | 0.49 V | 1000 uW |

Phase margin is treated as a bounded stability window: below 55 deg is a hard
failure, 60-65 deg is preferred, 55-70 deg is acceptable for schematic-level
experiments, and values above 75 deg receive no extra reward. The default
schema records a diagnostic 10 mV \(V_{DS}-V_{DS,sat}\) headroom check without
counting it as a full-spec pass target.

For the IHP two-stage Miller OTA, `Cc` is emitted as the SG13G2 `cap_cmim` MIM
capacitor and `Rz` as the SG13G2 `rhigh` PDK resistor.

## Verified OTA Runs

The following seed-10 full-flow runs were measured with ngspice and checked
against the targets above.

| Topology | Status | Iter | Gain | UGBW | PM | SR | Swing | Power |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 5T OTA | pass | 3 | 27.09 dB | 44.92 MHz | 86.3 deg | 36.20 V/us | 0.778 V | 44.55 uW |
| Current-mirror OTA | pass | 8 | 28.16 dB | 30.84 MHz | 83.3 deg | 31.95 V/us | 0.653 V | 42.71 uW |
| Folded-cascode OTA | pass | 1 | 45.62 dB | 27.33 MHz | 63.9 deg | 19.17 V/us | 0.812 V | 25.01 uW |
| Telescopic OTA | pass | 6 | 51.96 dB | 9.33 MHz | 60.6 deg | 6.09 V/us | 0.836 V | 20.31 uW |
| Two-stage Miller OTA | pass | 7 | 57.75 dB | 26.46 MHz | 62.7 deg | 13.02 V/us | 0.592 V | 140.37 uW |

| Target achievement | Measured metrics |
| --- | --- |
| ![Full-flow OTA target achievement](docs/assets/full_flow_ota_achievement.png) | ![Full-flow OTA measured metrics](docs/assets/full_flow_ota_summary.png) |

Diagnosis artifacts provide more detail than pass/fail status alone: local
SPICE probes record whether proposed actions reduce failed violations, and the
evidence gate records which actions are admitted.

![Diagnosis validation from local SPICE probes and objective-gated actions](docs/assets/diagnosis_validation.png)

## Installation

Requirements:

- Ubuntu Linux or WSL Ubuntu
- Python 3.10+
- `ngspice`
- Python packages in `requirements.txt`
- IHP SG13G2 model files referenced by `environment_ihp_sg13g2.yaml`

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ngspice

cd /path/to/AnalogRF-IR
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

## Running A Flow

Single topology optimization:

```bash
python main.py \
  --env environment_ihp_sg13g2.yaml \
  --schema inputs/ota/folded_cascode/folded_cascode_ota_ihp130.yaml \
  --topology yaml \
  --generations 30 \
  --pop-size 80 \
  --seed 10
```

LLM-assisted diagnosis uses a local config file. The API key can be supplied
through the environment or a gitignored key file.

```bash
mkdir -p ~/.config/analogrf-ir
printf '%s\n' 'YOUR_DEEPSEEK_KEY' > ~/.config/analogrf-ir/deepseek.key
cp configs/local/llm.example.yaml configs/local/llm.yaml
```

```bash
python main.py \
  --config configs/local/llm.yaml \
  --env environment_ihp_sg13g2.yaml \
  --schema inputs/ota/two_stage_miller/two_stage_miller_ota.yaml \
  --topology yaml \
  --agent-rounds 30 \
  --postprocess-policy fallback \
  --seed 10
```

## Regression And Figures

Run the OTA method-comparison matrix:

```bash
python scripts/run_ablation.py \
  --config configs/ablation_ihp130_ota.yaml \
  --local-config configs/local/llm.yaml \
  --run --keep-going
```

Regenerate the README figures:

```bash
python scripts/plot_full_flow_results.py --out-dir docs/assets
python scripts/plot_diagnosis_validation.py --out-dir docs/assets
```

Run tests:

```bash
python -m pytest -q
```

## Repository Layout

```text
asir/          Semantic profile extraction and typed dependencies
core/          Environment, design rules, validation, and regions
diagnostics/   Causal diagnosis, intervention models, and action gating
flow/          Main orchestration and LangGraph agent loop
inputs/        Maintained circuit schemas
layout/        Device folding and physical realization helpers
netlist/       Schema-to-SPICE generation
optimizer/     Surrogate evaluators and NSGA-II search
postprocess/   Explicit repair and compensation tuning
schemas/       Typed design-state dataclasses
simulator/     ngspice execution and measurement extraction
tests/         Regression tests
docs/          Architecture, quickstart, schema, and experiment notes
```

## Artifact Contract

Each run writes a compact schema plus structured evidence:

```text
design_state.yaml        Reviewable schema state and concise diagnostics
causal_diagnostics.json  Full causal graph and local intervention model
agent_diagnostics.json   Agent-facing failure summary
sim_log.json             Optimizer, postprocess, and simulator log
result.json              Final pass/fail and measured metrics
```

## Limitations

- Surrogate estimates guide search but are not signoff measurements.
- Current results are schematic-level TT ngspice runs without PVT, mismatch,
  extracted parasitics, or layout verification.
- ICMR is intentionally excluded from default OTA optimization targets.
- Comparator and RF flows are extensible foundations, not mature signoff flows.
- LLM planning is an optional diagnosis/selection interface over the evidence
  gate, not an independent circuit-edit authority.

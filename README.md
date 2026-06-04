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

For normalized violation vector \(\mathbf{v}\), local SPICE action response
column \(\mathbf{A}_{:,j}\), and weights \(w_i\), the specification objective
is

$$
\begin{aligned}
\Phi(\mathbf{v}) &= \sum_i w_i v_i^2,\\
J_{\mathrm{spec}}(s) &= \Phi(\mathbf{v}(s)).
\end{aligned}
$$

An action can be applied only if it passes the physical gate and is either
part of the constrained optimizer's compatible action set or independently
reduces the action objective. Guarded actions also require a passing local
evidence gate:

$$
\begin{aligned}
a\in\mathcal{A}_{\mathrm{adm}}(s)
\Longleftrightarrow\;&
g_{\mathrm{phys}}(s,a)=1 \\
&\land\left(a\in C^\star \lor \Delta J_{\mathrm{act}}(s,a)<0\right)\\
&\land\left(a\notin\mathcal{A}_{\mathrm{guard}} \lor E(s,a)=1\right).
\end{aligned}
$$

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

Priority 1-2 targets define pass/fail in the reference regressions. Lower
priority quantities, including power budget and saturation-margin diagnostics,
are still measured, reported, and available to the objective/diagnosis layers.

## Verified OTA Runs

The following 10-seed LLM-assisted full-flow runs were measured with ngspice
and checked against the priority 1-2 targets above. Reported values are median
measurements across the passing seeds.

| Topology | Status | Iter | Gain | UGBW | PM | SR | Swing | Power |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 5T OTA | 10/10 | 1 | 26.6 dB | 57.67 MHz | 84.5 deg | 50.15 V/us | 0.745 V | 63.9 uW |
| Current-mirror OTA | 10/10 | 2 | 28.8 dB | 46.11 MHz | 68.1 deg | 53.21 V/us | 0.694 V | 81.0 uW |
| Folded-cascode OTA | 10/10 | 2 | 46.3 dB | 50.15 MHz | 63.8 deg | 29.64 V/us | 0.868 V | 38.6 uW |
| Telescopic OTA | 10/10 | 1 | 48.1 dB | 12.47 MHz | 63.9 deg | 6.91 V/us | 0.871 V | 18.5 uW |
| Two-stage Miller OTA | 10/10 | 2 | 57.0 dB | 36.71 MHz | 71.7 deg | 30.09 V/us | 0.663 V | 372.4 uW |

| Target achievement | Measured metrics |
| --- | --- |
| ![Full-flow OTA target achievement](docs/assets/full_flow_ota_achievement.png) | ![Full-flow OTA measured metrics](docs/assets/full_flow_ota_summary.png) |

Diagnosis artifacts provide more detail than pass/fail status alone: local
SPICE probes record whether proposed actions reduce failed violations, and the
evidence gate records which actions are admitted.

![Diagnosis validation from local SPICE probes and objective-gated actions](docs/assets/diagnosis_validation.png)

## Method Ablation

The current unified ablation manifest contains 5 OTA topologies, 10 seeds, and
4 method variants. The non-LLM methods are run independently from the LLM
reference data and all rows are re-scored with the same priority 1-2 target
definition.

| Method | 5T | Current mirror | Folded cascode | Telescopic | Two-stage | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Optimizer | 3/10 | 0/10 | 0/10 | 0/10 | 0/10 | 3/50 |
| Opt + fallback PP | 5/10 | 1/10 | 1/10 | 3/10 | 1/10 | 11/50 |
| Diagnosis + PP | 10/10 | 7/10 | 5/10 | 10/10 | 9/10 | 41/50 |
| LLM + fallback PP | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 50/50 |

![Ablation pass rate by method and topology](docs/assets/success_rate_by_method_topology.png)

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
python scripts/plot_ablation_results.py \
  --manifest runs/ablations_ihp130_ota_unified_seed1_10/manifest.json \
  --out-dir docs/assets --format png

python scripts/plot_full_flow_results.py \
  --manifest runs/ablations_ihp130_ota_unified_seed1_10/manifest.json \
  --out-dir docs/assets --format png

python scripts/plot_diagnosis_validation.py \
  --manifest runs/ablations_ihp130_ota_unified_seed1_10/manifest.json \
  --out-dir docs/assets --format png
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

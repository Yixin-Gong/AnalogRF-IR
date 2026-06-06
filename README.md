# AnalogRF-IR

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](#installation)
[![Simulator](https://img.shields.io/badge/SPICE-ngspice-lightgrey)](#installation)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

AnalogRF-IR is a simulator-backed analog circuit optimization framework for
topology-aware OTA sizing. It combines a typed YAML design schema, gm/ID-guided
surrogate search, NSGA-II exploration, ngspice validation, causal diagnosis,
and evidence-gated LLM-assisted action selection.

The central contract is that humans, deterministic diagnosis, optimizer logic,
and optional LLM planning all operate through the same typed schema. A proposed
edit is executable only when it is physically valid and supported by
optimizer-side or local SPICE evidence.

> Status: manuscript and release package in preparation. The repository is
> distributed under the MIT License. See [LICENSE](LICENSE).

## Key Features

- Typed YAML schemas for topology, device roles, variables, constraints,
  targets, evaluations, process setup, and compact diagnostics.
- ASIR semantic extraction for roles, symmetry groups, gain stages, bias paths,
  compensation networks, and typed dependencies.
- gm/ID-guided surrogate sizing with bounded NSGA-II exploration.
- ngspice validation for AC gain, UGBW, phase margin, transient slew rate,
  output swing, power, operating point, and headroom.
- Causal diagnosis over failed metrics, topology roles, bias paths, pole/gain
  dependencies, and simulator evidence.
- Local SPICE intervention models for action-to-violation estimates.
- Evidence-gated schema actions with symmetry copying, physical checks, and
  explicit apply/skip records.
- Optional DeepSeek-compatible LLM planning as a diagnosis and selection layer,
  not as unrestricted edit authority.

## Results At A Glance

The maintained IHP SG13G2 OTA regressions use high-impedance
`C_L ~= 1 pF` targets. They are schematic-level TT ngspice experiments, not
pad, cable, low-resistance, extracted-layout, or 50 ohm load signoff.

| Topology | Gain | UGBW | PM | SR | Swing | Power |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 5T OTA | 25 dB | 25 MHz | 60 deg | 15 V/us | 0.65 V | 200 uW |
| Current-mirror OTA | 28 dB | 30 MHz | 60 deg | 25 V/us | 0.60 V | 300 uW |
| Folded-cascode OTA | 45 dB | 20 MHz | 60 deg | 12 V/us | 0.60 V | 600 uW |
| Telescopic OTA | 48 dB | 7 MHz | 60 deg | 6 V/us | 0.55 V | 300 uW |
| Two-stage Miller OTA | 57 dB | 20 MHz | 60 deg | 10 V/us | 0.49 V | 1000 uW |

Ten-seed full-flow ngspice runs pass the priority 1-2 targets for all five
maintained OTA topologies under the LLM-assisted diagnosis flow:

| Topology | Status | Iter | Gain | UGBW | PM | SR | Swing | Power |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 5T OTA | 10/10 | 1 | 26.6 dB | 57.67 MHz | 84.5 deg | 50.15 V/us | 0.745 V | 63.9 uW |
| Current-mirror OTA | 10/10 | 2 | 28.8 dB | 46.11 MHz | 68.1 deg | 53.21 V/us | 0.694 V | 81.0 uW |
| Folded-cascode OTA | 10/10 | 2 | 46.3 dB | 50.15 MHz | 63.8 deg | 29.64 V/us | 0.868 V | 38.6 uW |
| Telescopic OTA | 10/10 | 1 | 48.1 dB | 12.47 MHz | 63.9 deg | 6.91 V/us | 0.871 V | 18.5 uW |
| Two-stage Miller OTA | 10/10 | 2 | 57.0 dB | 36.71 MHz | 71.7 deg | 30.09 V/us | 0.663 V | 372.4 uW |

The current ablation matrix compares five OTA topologies, ten seeds, and four
method variants. The rows are re-scored with the same priority 1-2 target
definition:

| Method | 5T | Current mirror | Folded cascode | Telescopic | Two-stage | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Optimizer | 3/10 | 0/10 | 0/10 | 0/10 | 0/10 | 3/50 |
| Opt + fallback PP | 5/10 | 1/10 | 1/10 | 3/10 | 1/10 | 11/50 |
| Diagnosis + PP | 10/10 | 7/10 | 5/10 | 10/10 | 9/10 | 41/50 |
| LLM + fallback PP | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 50/50 |

| Target achievement | Measured metrics |
| --- | --- |
| ![Full-flow OTA target achievement](docs/assets/full_flow_ota_achievement.png) | ![Full-flow OTA measured metrics](docs/assets/full_flow_ota_summary.png) |

![Ablation pass rate by method and topology](docs/assets/success_rate_by_method_topology.png)

## Method Summary

The flow uses a typed schema as the executable interface between human review,
surrogate search, ngspice validation, causal diagnosis, optional LLM planning,
and postprocess repair.

![Diagnosis-centered analog optimization architecture](docs/assets/analogdiag_architecture.png)

Failed metrics are mapped to typed causes, tested through local SPICE probes,
converted into candidate schema actions, and filtered by an evidence gate.
Unsupported LLM edits are recorded as skipped notes rather than executable
changes.

For normalized violation vector `v`, local action response column `A_j`, and
weights `w_i`, the specification objective is:

```text
Phi(v) = sum_i w_i v_i^2
J_spec(s) = Phi(v(s))
```

An action is admissible only if it passes the physical gate and is either part
of the constrained optimizer's compatible action set or independently reduces
the action objective. Guarded actions also require local evidence:

```text
admissible(a, s) :=
  physical_gate(s, a)
  and (a in optimizer_selected_set or delta_J_act(s, a) < 0)
  and (not guarded(a) or evidence_gate(s, a))
```

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

## Quick Start

Run one IHP130 OTA optimization:

```bash
python main.py \
  --env environment_ihp_sg13g2.yaml \
  --schema inputs/ota/folded_cascode/folded_cascode_ota_ihp130.yaml \
  --topology yaml \
  --generations 30 \
  --pop-size 80 \
  --seed 10
```

Run the LLM-assisted diagnosis flow with a local, gitignored config:

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

If no LLM key is configured, the flow records fallback status and keeps
deterministic artifacts usable for tests and non-LLM experiments.

## Reproducibility

Run tests:

```bash
python -m pytest -q
```

Dry-run the method-comparison matrix:

```bash
python scripts/run_ablation.py --config configs/ablation_ihp130_ota.yaml
```

Run the matrix and keep going after individual failures:

```bash
python scripts/run_ablation.py \
  --config configs/ablation_ihp130_ota.yaml \
  --local-config configs/local/llm.yaml \
  --run --keep-going
```

Regenerate README figures from a completed manifest:

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

Each run writes a compact schema plus structured evidence:

```text
design_state.yaml        Reviewable schema state and concise diagnostics
causal_diagnostics.json  Full causal graph and local intervention model
agent_diagnostics.json   Agent-facing failure summary
sim_log.json             Optimizer, postprocess, and simulator log
result.json              Final pass/fail and measured metrics
```

## Repository Layout

```text
asir/          Semantic profile extraction and typed dependencies
core/          Environment, design rules, validation, and regions
diagnostics/   Causal diagnosis, intervention models, and action gating
flow/          Main orchestration and LangGraph agent loop
frontends/     YAML and SPICE input frontends
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

## Documentation

- [Quick Start](docs/quickstart.md)
- [Architecture](docs/architecture.md)
- [Method Comparisons](docs/ablation_experiments.md)
- [Schema Guide](docs/schema_guide.md)
- [Development Guide](docs/development.md)

## Citation

If you use AnalogRF-IR in academic work, cite the repository for now and update
to the manuscript/preprint citation when it becomes available. GitHub-compatible
metadata is provided in [CITATION.cff](CITATION.cff).

```bibtex
@misc{analogrfir2026,
  title = {AnalogRF-IR: Evidence-Gated Causal Diagnosis for Simulator-Backed OTA Optimization},
  author = {{AnalogRF-IR Authors}},
  year = {2026},
  howpublished = {\url{https://github.com/Yixin-Gong/AnalogRF-IR}},
  note = {Manuscript in preparation}
}
```

## Limitations

- Surrogate estimates guide search but are not signoff measurements.
- Current results are schematic-level TT ngspice runs without PVT, mismatch,
  extracted parasitics, or layout verification.
- ICMR is intentionally excluded from default OTA optimization targets.
- Comparator and RF flows are extensible foundations, not mature signoff flows.
- LLM planning is an optional diagnosis/selection interface over the evidence
  gate, not an independent circuit-edit authority.

## License And Contributions

AnalogRF-IR is distributed under the [MIT License](LICENSE).

External contributions should keep benchmark claims reproducible and preserve
the evidence-gated action model. See [CONTRIBUTING.md](CONTRIBUTING.md).

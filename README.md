# AnalogRF-IR

AnalogRF-IR is a schema-driven analog and RF circuit optimization research flow. It converts circuit intent into a typed intermediate representation, runs gm/ID-aware sizing and multi-objective optimization, validates candidates with ngspice, and records structured diagnostics for engineer-in-the-loop and agent-assisted design iteration.

The current development focus is **structure-aware diagnosis and optimization for OTA-class analog circuits**, including five-transistor OTAs and two-stage Miller OTAs. Comparator and broader RF support are present as extensible foundations, but are not yet signoff-grade flows.

> License notice: this repository is source-available only. All rights are reserved. See [LICENSE](LICENSE).

## Highlights

- YAML-first design state for devices, variables, targets, evaluations, and diagnostics.
- ASIR semantic extraction for circuit roles, symmetry groups, bias paths, gain stages, and compensation networks.
- Profile-driven behavior for OTA, comparator, and future RF circuit families.
- gm/ID-based compact sizing with NSGA-II exploration.
- ngspice-backed AC, DC, transient, operating-point, slew-rate, and headroom measurements.
- Postprocess hooks for bias repair, OP validation, and compensation tuning.
- LangGraph-based multi-round agent loop for diagnosis-guided schema tuning.
- DeepSeek-compatible LLM planner with deterministic fallback behavior.
- Local SPICE intervention modeling for action-to-spec attribution.
- Constrained action optimization over schema-safe tuning moves.
- Structured run artifacts for reproducibility, debugging, and human review.

## Repository Layout

```text
asir/          Semantic IR, profile selection, and extraction helpers
core/          Design rules, validation, regions, and process environment models
diagnostics/   Causal diagnostics, ranking, and agent-readable reports
feasibility/   Physics-informed feasibility estimation
flow/          End-to-end orchestration and agent loop control
frontends/     YAML and SPICE input frontends
inputs/        Maintained circuit-family schema examples
netlist/       Schema-to-SPICE generation
optimizer/     Compact evaluators and NSGA-II optimization
postprocess/   ngspice-guided repair and compensation tuning
schemas/       Typed design-state schema definitions
simulator/     ngspice execution and measurement extraction
specs/         Specification and metric utilities
tests/         Regression tests
docs/          Architecture, quickstart, schema, and development notes
```

## Requirements

- Ubuntu Linux or WSL Ubuntu
- Python 3.10 or newer
- `python3-venv`, `pip`, and `ngspice`
- Python packages in `requirements.txt`
- Process model files and lookup tables for the target technology

Install system dependencies:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ngspice
```

Create a local Python environment:

```bash
cd /path/to/AnalogRF-IR
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

## Quick Start

Run a two-stage OTA optimization:

```bash
cd /path/to/AnalogRF-IR
. .venv/bin/activate

python main.py \
  --env environment_ihp_sg13g2.yaml \
  --schema inputs/ota/two_stage_miller/two_stage_miller_ota.yaml \
  --topology two_stage \
  --generations 4 \
  --pop-size 20 \
  --seed 17
```

Run a five-transistor OTA optimization:

```bash
python main.py \
  --env environment_ihp_sg13g2.yaml \
  --schema inputs/ota/five_transistor/five_transistor_ota.yaml \
  --topology five \
  --generations 4 \
  --pop-size 20 \
  --seed 17
```

Run the regression suite:

```bash
. .venv/bin/activate
python -m pytest -q
```

## LLM-Guided Diagnosis

The multi-round agent flow uses LangGraph to separate simulation, diagnosis, schema-command generation, and command execution. The agent reads generated `design_state.yaml` artifacts as the source of truth and only applies edits through schema-level actions.

Set a DeepSeek API key before running LLM-guided rounds:

```bash
export DEEPSEEK_API_KEY="..."
```

Run a DeepSeek-guided two-stage OTA flow:

```bash
python main.py \
  --env environment_ihp_sg13g2.yaml \
  --schema inputs/ota/two_stage_miller/two_stage_miller_ota.yaml \
  --topology two_stage \
  --generations 4 \
  --pop-size 20 \
  --seed 17 \
  --agent-rounds 20 \
  --llm-provider deepseek \
  --llm-model deepseek-v4-pro \
  --llm-thinking enabled \
  --llm-reasoning-effort max \
  --llm-timeout 180 \
  --llm-max-tokens 12000 \
  --intervention-max-actions 3
```

If the API key is not configured, the flow records an LLM fallback status and continues with deterministic schema commands so local tests and non-LLM experiments remain reproducible.

## Causal Action Planning

The agent action layer is modeled as a local constrained optimization problem rather than a raw candidate-selection heuristic.

Each agent round follows three steps:

1. **Causal graph diagnosis** ranks structural root-cause nodes from the dependency graph, operating-point evidence, propagation paths, and weak sensitivity priors.
2. **Local intervention modeling** perturbs a small number of schema-safe actions in SPICE and builds a local matrix `A`, where each column estimates how one action changes normalized spec violation.
3. **Constrained action optimization** selects the next schema edits by minimizing residual weighted violation plus penalties for uncertainty, duplicate knob writes, guarded actions, and cross-metric regressions.

When SPICE intervention data is available, automatic action selection is restricted to actions that were actually perturbed in the local `A` matrix. Surrogate estimates are retained as fallback evidence and debugging context, not as the preferred decision rule.

The resulting artifacts are written into `diagnostics.causal_diagnostics.local_intervention_model`, `diagnostics.causal_diagnostics.constrained_action_optimizer`, and `diagnostics.causal_diagnostics.attribution_guided_tuning`.

## Design State Contract

AnalogRF-IR treats the generated design state as the canonical interface between optimization, simulation, diagnosis, and agent actions.

- User-authored inputs define the initial circuit, process environment, targets, and editable variables.
- Generated schemas capture the current design state, operating-point data, simulation measurements, diagnostics, and available schema actions.
- Agent actions are restricted to explicit schema-edit commands. The planner should not modify topology, simulator output, or hidden internal state directly.
- Derived JSON files are convenience views; `design_state.yaml` remains the authoritative run artifact.

## Outputs

Each execution writes a run directory under:

```text
runs/iter_###/
```

Typical artifacts:

```text
design_state.yaml          Canonical design state and diagnostics
netlist.cir                Generated SPICE netlist
result.json                Compact metric and pass/fail summary
agent_diagnostics.json     Agent-facing diagnosis and action context
causal_diagnostics.json    Structural causal diagnosis view
sim_log.json               Simulator log view derived from design_state.yaml
```

The causal diagnostics layer records failure symptoms, dependency paths, ranked causal candidates, and tuning recommendations. This layer is under active development toward intervention-calibrated, structure-aware action planning.

## Documentation

Additional project notes are maintained in [docs](docs/):

- [Quick Start](docs/quickstart.md)
- [Architecture](docs/architecture.md)
- [Schema Guide](docs/schema_guide.md)
- [Development Guide](docs/development.md)

## Development

Recommended workflow:

```bash
. .venv/bin/activate
python -m pytest -q
python -m compileall asir core diagnostics flow frontends netlist optimizer postprocess schemas simulator specs tests
```

When adding a new circuit family:

1. Define or update the IR profile in `asir/profiles.py`.
2. Add schema examples with explicit device roles, symmetry labels, variables, targets, and evaluations.
3. Register profile-specific rules and validation behavior.
4. Add compact estimators only where the analytical model is defensible.
5. Add simulator measurements for final validation.
6. Add regression tests for profile selection, constraints, diagnostics, and output artifacts.

## Current Limitations

- Compact models are optimization guidance, not signoff results.
- Output swing and ICMR are primarily OP/headroom estimates unless explicit sweeps are added.
- Comparator delay, offset, kickback, noise, energy, and metastability require dedicated transient, noise, and Monte Carlo testbenches.
- RF-specific flows still need S-parameter, noise figure, compression, matching, linearity, and stability extensions.
- LLM-guided diagnosis is experimental and should be treated as a planner/explainer layer over simulator-backed evidence.

## Roadmap

- Structure-aware causal diagnosis with explicit graph dependencies.
- Intervention-calibrated action ranking from small SPICE perturbations.
- Constrained local optimization for agent action selection.
- Cleaner OP-first and dynamic-performance-first decomposition.
- More complete comparator and RF signoff testbenches.
- Tighter reproducibility metadata for process, simulator, model, and planner configuration.

## License

This project is provided under a proprietary, all-rights-reserved license. No permission is granted to use, copy, modify, distribute, sublicense, publish, host, or create derivative works except by explicit written permission from the copyright holder.

See [LICENSE](LICENSE) for the complete terms.

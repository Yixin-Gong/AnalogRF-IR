# Ablation Experiment Plan

This plan defines the project comparison matrix. The goal is to separate which
part of AnalogRF-IR is doing the work: global optimization, causal diagnosis,
local intervention evidence, LLM planning, and postprocess repair.

## Primary Metrics

Record these for every case, schema, and seed:

- Success rate: fraction of runs with all targets passing.
- Final weighted loss and per-target normalized violation.
- Final measured metrics: gain, UGBW, phase margin, slew rate, swing, power.
- Simulation cost: optimizer evaluations, ngspice calls, local intervention
  calls, postprocess events, and wall time.
- Planner behavior: applied actions, skipped actions, formal admissibility
  rejections, guarded-action rejections, and hard physical gate rejections.
- Postprocess dependence: whether pass required postprocess, and which repair
  event type closed the gap.

## Control Matrix

The current release-report matrix uses the frontier-stress IHP 130 nm OTA
schemas in `inputs/ota_stress/frontier/`. The non-LLM controls are materialized
by `configs/ablation_ihp130_ota_frontier_stress.yaml`; the LLM full-flow cells
use the same config with the LLM case filter. This keeps the matrix small
enough for rapid iteration while separating topology effects from method
effects.

| Case | Purpose | Postprocess | Diagnosis | Intervention | LLM |
| --- | --- | --- | --- | --- | --- |
| `optimizer_only` | lower baseline | off | no | no | no |
| `optimizer_postprocess_fallback` | repair value under the fallback policy | fallback | no | no | no |
| `diagnosis_spice_postprocess_fallback` | causal diagnosis and admissible local actions without LLM planning | fallback | yes | SPICE | deterministic |
| `llm_full_residual_escape_postprocess_fallback` | LLM-guided diagnosis with residual-escape hypotheses, evidence gate, and repair policy | fallback | yes | SPICE | yes |

## Recommended Reporting

Use at least three random seeds for the development matrix and increase the
seed count when locking down a release benchmark. The current frontier-stress
IHP 130 nm topology set is:

- `inputs/ota_stress/frontier/two_stage_miller_ota_frontier_stress.yaml`
- `inputs/ota_stress/frontier/five_transistor_ota_frontier_stress.yaml`
- `inputs/ota_stress/frontier/current_mirror_ota_ihp130_frontier_stress.yaml`
- `inputs/ota_stress/frontier/telescopic_ota_ihp130_frontier_stress.yaml`
- `inputs/ota_stress/frontier/folded_cascode_ota_ihp130_frontier_stress.yaml`

For project reporting, compare the complete method against the optimizer-only,
optimizer-plus-postprocess, and deterministic-diagnosis controls. The intended
shape is that the full method has the highest pass rate, while the controls
leave a small number of failed specs that expose the value of the diagnosis and
planner layers.

## CLI

Dry-run the full matrix and generate per-job configs:

```bash
python scripts/run_ablation.py --config configs/ablation_ihp130_ota_frontier_stress.yaml
```

Run one smoke case:

```bash
python scripts/run_ablation.py --config configs/ablation_ihp130_ota_frontier_stress.yaml --case optimizer_only --seed 1 --limit 1 --run
```

Run the non-LLM frontier-stress control matrix:

```bash
python scripts/run_ablation.py \
  --config configs/ablation_ihp130_ota_frontier_stress.yaml \
  --run --keep-going
```

Run the LLM full-flow frontier-stress matrix:

```bash
python scripts/run_ablation.py \
  --config configs/ablation_ihp130_ota_frontier_stress.yaml \
  --case llm_full_residual_escape_postprocess_fallback \
  --run --keep-going --jobs 1 --skip-existing
```

Build the unified 200-cell manifest and generate comparison tables and plots:

```bash
python scripts/build_200cell_manifest.py \
  --nonllm-manifest runs/ablations_ihp130_ota_frontier_stress_relaxed_other150_20260613/manifest.json \
  --llm-manifest runs/ablations_ihp130_ota_frontier_stress_relaxed_llm_full_50cell_20260614/manifest.json \
  --out-dir runs/ablations_ihp130_ota_frontier_stress_relaxed_200cell_20260614

python scripts/plot_ablation_results.py \
  --manifest runs/ablations_ihp130_ota_frontier_stress_relaxed_200cell_20260614/manifest.json \
  --out-dir docs/assets --format png

python scripts/plot_full_flow_results.py \
  --manifest runs/ablations_ihp130_ota_frontier_stress_relaxed_200cell_20260614/manifest.json \
  --out-dir docs/assets --format png --write-csv

python scripts/plot_diagnosis_validation.py \
  --manifest runs/ablations_ihp130_ota_frontier_stress_relaxed_200cell_20260614/manifest.json \
  --out-dir docs/assets --format png --write-csv
```

After a design passes its baseline targets, run a progressive target ladder to
find the measured frontier. Each level tightens gain, bandwidth, slew rate, and
output swing while recording power as a budget; the script stops a ladder after
repeated failures and writes the non-dominated passing points:

```bash
python scripts/run_progressive_pareto.py \
  --schema inputs/ota/current_mirror/current_mirror_ota_ihp130.yaml \
  --seed 1 --seed 2 \
  --output-dir runs/progressive_pareto_current_mirror_mim_cl1pf \
  --levels 6 \
  --run
```

Outputs:

- `manifest.json`: commands, case metadata, return codes, and latest result summary.
- `summary.csv`: compact comparison table for plotting or spreadsheet import.
- `generated_configs/*.yaml`: fully materialized configs for editor inspection
  and reproducibility.
- `figures/ablation_records.csv`: per-run metrics, pass/fail state, planner
  usage, and postprocess event counts.
- `figures/spec_records.csv`: per-run per-spec values and normalized margins.
- `figures/method_topology_summary.csv`: method-by-topology aggregate table.
- `figures/*.png`: success-rate, spec heatmap, metric
  distribution, gain-bandwidth-power tradeoff, and traceability plots.

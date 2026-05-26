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

The current IHP 130 nm OTA topology-family matrix lives in
`configs/ablation_ihp130_ota.yaml`. It is intentionally small enough for rapid
iteration while still separating topology effects from method effects.

| Case | Purpose | Postprocess | Diagnosis | Intervention | LLM |
| --- | --- | --- | --- | --- | --- |
| `optimizer_only` | lower baseline | off | no | no | no |
| `optimizer_postprocess_fallback` | repair value under the fallback policy | fallback | no | no | no |
| `diagnosis_spice_postprocess_fallback` | causal diagnosis and admissible local actions without LLM planning | fallback | yes | SPICE | deterministic |
| `llm_diagnosis_postprocess_fallback` | full diagnosis system with planner, evidence gate, and fallback repair | fallback | yes | SPICE | yes |

## Recommended Reporting

Use at least three random seeds for the development matrix and increase the
seed count when locking down a release benchmark. The current IHP 130 nm
topology set is:

- `inputs/ota/two_stage_miller/two_stage_miller_ota.yaml`
- `inputs/ota/five_transistor/five_transistor_ota.yaml`
- `inputs/ota/current_mirror/current_mirror_ota_ihp130.yaml`
- `inputs/ota/telescopic/telescopic_ota_ihp130.yaml`
- `inputs/ota/folded_cascode/folded_cascode_ota_ihp130.yaml`

For project reporting, compare the complete method against the optimizer-only,
optimizer-plus-postprocess, and deterministic-diagnosis controls. The intended
shape is that the full method has the highest pass rate, while the controls
leave a small number of failed specs that expose the value of the diagnosis and
planner layers.

## CLI

Dry-run the full matrix and generate per-job configs:

```bash
python scripts/run_ablation.py --config configs/ablation_ihp130_ota.yaml
```

Run one smoke case:

```bash
python scripts/run_ablation.py --config configs/ablation_ihp130_ota.yaml --case optimizer_only --seed 1 --limit 1 --run
```

Run the full matrix:

```bash
python scripts/run_ablation.py --config configs/ablation_ihp130_ota.yaml --run --keep-going
```

Generate comparison tables and plots:

```bash
python scripts/plot_ablation_results.py \
  --manifest runs/ablations_ihp130_ota/manifest.json \
  --out-dir runs/ablations_ihp130_ota/figures
```

After a design passes its baseline targets, run a progressive target ladder to
find the measured frontier. Each level tightens gain, bandwidth, slew rate,
output swing, and power; the script stops a ladder after repeated failures and
writes the non-dominated passing points:

```bash
python scripts/run_progressive_pareto.py \
  --schema inputs/ota/current_mirror/current_mirror_ota_ihp130.yaml \
  --seed 1 --seed 2 \
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
- `figures/*.png` and `figures/*.pdf`: success-rate, spec heatmap, metric
  distribution, gain-bandwidth-power tradeoff, and traceability plots.

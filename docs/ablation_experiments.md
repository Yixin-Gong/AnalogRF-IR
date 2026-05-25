# Ablation Experiment Plan

This plan is the first paper-facing comparison matrix. The goal is to separate
which part of AnalogRF-IR is doing the work: global optimization, causal
diagnosis, local intervention evidence, LLM planning, and postprocess repair.

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
| `optimizer_postprocess_fallback` | repair value under clean fallback policy | fallback | no | no | no |
| `diagnosis_surrogate_no_postprocess` | structure-aware actions without SPICE local A matrix | off | yes | surrogate | deterministic |
| `llm_diagnosis_no_postprocess` | main method cleanliness test | off | yes | SPICE | yes |
| `llm_diagnosis_postprocess_fallback` | robust combined method | fallback | yes | SPICE | yes |

## Recommended Reporting

Use at least three random seeds for the development matrix and at least five
for the final paper matrix. The current IHP 130 nm topology set is:

- `inputs/ota/current_mirror/current_mirror_ota_ihp130.yaml`
- `inputs/ota/telescopic/telescopic_ota_ihp130.yaml`
- `inputs/ota/folded_cascode/folded_cascode_ota_ihp130.yaml`

For the main claim, emphasize `llm_diagnosis_no_postprocess` as the clean
planner-gated method and `llm_diagnosis_postprocess_fallback` as the robustness
variant. Treat postprocess cases as repair controls, not as the core method.

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

Generate paper-facing tables and plots:

```bash
python scripts/plot_ablation_results.py \
  --manifest runs/ablations_ihp130_ota/manifest.json \
  --out-dir runs/ablations_ihp130_ota/figures
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

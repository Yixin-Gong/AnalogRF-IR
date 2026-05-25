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

The canonical matrix lives in `configs/ablation.yaml`.

| Case | Purpose | Postprocess | Diagnosis | Intervention | LLM |
| --- | --- | --- | --- | --- | --- |
| `optimizer_only` | lower baseline | off | no | no | no |
| `optimizer_postprocess_fallback` | repair value under clean fallback policy | fallback | no | no | no |
| `optimizer_postprocess_always` | upper-bound repair benefit and repair cost | always | no | no | no |
| `diagnosis_surrogate_no_postprocess` | structure-aware actions without SPICE local A matrix | off | yes | surrogate | deterministic |
| `diagnosis_spice_intervention_no_postprocess` | main non-LLM causal optimizer | off | yes | SPICE | deterministic |
| `llm_diagnosis_no_postprocess` | main method cleanliness test | off | yes | SPICE | yes |
| `llm_diagnosis_postprocess_fallback` | robust combined method | fallback | yes | SPICE | yes |
| `llm_diagnosis_postprocess_always` | quantify hidden repair dependence | always | yes | SPICE | yes |

## Recommended Reporting

Use at least five random seeds on each maintained OTA schema:

- `inputs/ota/five_transistor/five_transistor_ota.yaml`
- `inputs/ota/two_stage_miller/two_stage_miller_ota.yaml`

For the main claim, emphasize `diagnosis_spice_intervention_no_postprocess` and
`llm_diagnosis_no_postprocess`. Treat postprocess cases as robustness or repair
controls, not as the core method.

## CLI

Dry-run the full matrix and generate per-job configs:

```bash
python scripts/run_ablation.py --config configs/ablation.yaml
```

Run one smoke case:

```bash
python scripts/run_ablation.py --config configs/ablation.yaml --case optimizer_only --seed 1 --limit 1 --run
```

Run the full matrix:

```bash
python scripts/run_ablation.py --config configs/ablation.yaml --run --keep-going
```

Outputs:

- `manifest.json`: commands, case metadata, return codes, and latest result summary.
- `summary.csv`: compact comparison table for plotting or spreadsheet import.
- `generated_configs/*.yaml`: fully materialized configs for editor inspection
  and reproducibility.

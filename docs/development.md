# Development Guide

## Testing

Run the full suite:

```bash
python -m pytest -q
```

Run focused suites:

```bash
python -m pytest tests/test_frontends.py tests/test_asir.py -q
python -m pytest tests/test_modular_flow.py -q
```

Run a syntax check:

```bash
python -m compileall asir core diagnostics flow frontends layout netlist optimizer outputs postprocess schemas simulator specs tests
```

## Extension Workflow

For a new circuit family or major topology:

1. Add or update the IR profile in `asir/profiles.py`.
2. Add schema examples under `inputs/<family>/`.
3. Register profile-specific validation rules with `circuit_profiles=(...)`.
4. Add compact estimator support only for metrics with defensible models.
5. Add simulator measurements for validation.
6. Add postprocess repair only when the topology needs simulator-guided tuning.
7. Add regression tests for profile selection, generated objectives, validation rules, and artifacts.

Keep generic infrastructure generic. Family-specific behavior should enter
through profiles, capabilities, rule filters, spec models, estimators, and
postprocess registries.

## Module Boundaries

Keep the main treatment paths decoupled:

- Optimizers propose candidate states from schema variables and compact models.
- Validators enforce schema, symmetry, OP, and physical-layout constraints.
- Diagnostics explain failures and build action evidence.
- The constrained action optimizer selects schema-safe edits.
- The LLM planner may choose among existing actions, but the executor owns
  write-policy enforcement.
- Postprocess is an optional repair layer and should be easy to disable for
  ablation.

Do not hide permanent tuning logic inside postprocess if it belongs in schema
variables, constraints, diagnostics, or the action optimizer.

The preferred research method is:

```text
small-budget global optimization
  -> causal diagnosis and local intervention evidence
  -> combo coarse-fine constrained action optimization
  -> short re-optimization
  -> optional postprocess fallback
```

Keep this flow explicit in code and artifacts so postprocess can be ablated
without changing the diagnosis or action optimizer.

## Artifact Discipline

Generated run data belongs under `runs/`. Do not commit large generated logs,
temporary netlists, simulator scratch files, or external manuals. The maintained
documentation in this directory should describe current behavior, not preserve
append-only debugging history.

After a run, prefer reading `runs/iter_###/design_state.yaml` first. The JSON
files in the same directory are derived views for narrower consumers.

`design_state.yaml` should remain compact. Do not add full simulation logs,
dependency graphs, validation transcripts, or local intervention matrices to the
schema. Store heavy evidence in JSON artifacts and expose only compact summaries
needed by the next agent decision.

## Physical Safety Rules

New tuning strategies must be validation-gated before SPICE:

- Preserve explicit symmetry groups by construction.
- Encode only one optimizer variable for symmetric design variables and copy the
  value to peers on decode.
- Reject symmetry mismatches as errors.
- Keep W/L process limits layout-realizable through folding, parallel devices,
  or series segmentation.
- Reject agent edits outside known design variables and supported constraints.
- Add tests that prove invalid physical states are rejected before simulation.

The executor should validate a cloned schema state before committing agent
edits. This avoids writing a broken symmetry group, invalid range, or
layout-unrealizable W/L into the next optimization input.

## Diagnosis And Evidence Rules

Structure-aware causal diagnosis is the decision rule. Sensitivity may be used
as a weak prior or debugging comparison, but not as the final ranking signal.

Guarded actions must remain evidence-gated:

- They require local SPICE intervention evidence when applied automatically.
- The evidence gate must show objective improvement, failed-metric reduction,
  bounded tradeoff worsening, and bounded uncertainty.
- LLM prompts and deterministic fallbacks should follow the same executor
  policy.

All LLM-requested applies must also pass the formal action admissibility gate:

```text
apply_allowed := optimizer_selected OR objective_delta < 0
```

If constrained optimizer evidence is present, custom LLM edits are not an escape
hatch. They should be skipped or recorded as notes unless represented as an
optimizer candidate with admissible objective math. Keep OP/headroom/balance and
compensation moves typed with `action_class` and visible to the constrained
optimizer.

When changing action planning, add tests for both the selected action and the
rejected unsafe path.

Typed graph data is part of the public artifact contract. Causal diagnostic
edges should include node types, relation type, polarity, and mechanism; ASIR
dependency rules and edges should include dependency type and input/output
quantity types.

## Ablation Discipline

When evaluating a new tuning strategy, compare against at least:

```text
optimizer only
optimizer + postprocess
optimizer + diagnosis
LLM + optimizer + diagnosis
LLM + optimizer + diagnosis + postprocess
```

Record success rate, total ngspice calls, wall time, final loss, final metrics,
invalid-action count, intervention count, and postprocess trigger count. This
keeps speed and accuracy tradeoffs visible.

## Current Limitations

- Compact estimates guide optimization but are not signoff.
- Output swing is an operating-point headroom extraction. ICMR is measured with an explicit ngspice common-mode operating-point sweep, including valid-point count and the limiting headroom margin.
- Middlebrook return-ratio validation for compensated OTAs is still pending.
- Comparator offset, noise, kickback, energy, and metastability need dedicated transient, noise, or Monte Carlo paths.
- RF block support still needs profiles, schemas, estimators, simulator measurements, and validation rules.
- The SPICE importer handles common flat netlists and does not parse arbitrary hierarchy, expressions, or all PDK-specific passive models.

## Documentation Rules

Keep docs concise and current. Prefer durable explanations over historical run
notes. If a result is tied to a particular run directory, document what the run
shows and where the artifact lives, but avoid making one run look like a general
project guarantee.

The canonical project overview is `docs/README.md`; the repository root
`README.md` mirrors it for GitHub's homepage display. Update both together.

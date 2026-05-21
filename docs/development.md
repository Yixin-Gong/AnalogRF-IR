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
python -m compileall asir core flow frontends netlist optimizer specs tests
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

## Artifact Discipline

Generated run data belongs under `runs/`. Do not commit large generated logs,
temporary netlists, simulator scratch files, or external manuals. The maintained
documentation in this directory should describe current behavior, not preserve
append-only debugging history.

After a run, prefer reading `runs/iter_###/design_state.yaml` first. The JSON
files in the same directory are derived views for narrower consumers.

## Current Limitations

- Compact estimates guide optimization but are not signoff.
- Output swing and ICMR are mostly operating-point headroom estimates unless explicit sweeps are added.
- Middlebrook return-ratio validation for compensated OTAs is still pending.
- Comparator offset, noise, kickback, energy, and metastability need dedicated transient, noise, or Monte Carlo paths.
- RF block support still needs profiles, schemas, estimators, simulator measurements, and validation rules.
- The SPICE importer handles common flat netlists and does not parse arbitrary hierarchy, expressions, or all PDK-specific passive models.

## Documentation Rules

Keep docs concise and current. Prefer durable explanations over historical run
notes. If a result is tied to a particular run directory, document what the run
shows and where the artifact lives, but avoid making one run look like a general
project guarantee.

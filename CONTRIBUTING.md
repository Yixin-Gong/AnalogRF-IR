# Contributing

AnalogRF-IR is distributed under the MIT License. External contributions are
welcome when they preserve the repository's reproducibility and evidence-gated
optimization model.

## Before Contributing

- Do not submit proprietary PDK files, confidential process collateral, private
  model decks, or third-party code/data unless you have written permission to
  do so.
- Do not submit patches that rely on unpublished local files outside this
  repository.
- Keep benchmark claims reproducible: include the schema, config, seed,
  environment file, and generated artifact path needed to verify the result.
- Keep LLM-related behavior evidence-gated. Planner output should remain a
  diagnosis/selection signal and must not bypass schema validation, physical
  checks, or optimizer-side evidence.

## Preferred Change Shape

- Small bug fixes should include a focused regression test where practical.
- New circuit-family support should add or update schema examples, profile
  rules, simulator measurements, diagnostics, and tests together.
- Method-comparison changes should update the ablation config and explain how
  success rate, SPICE call count, wall time, and postprocess dependence are
  affected.
- Documentation changes should avoid new benchmark claims unless the supporting
  manifest and artifacts are available.

## Paper And Private Artifacts

The `paper/` directory is intentionally ignored by Git. Keep local manuscript
drafts, private submission metadata, and camera-ready paper sources there unless
the maintainers explicitly decide to publish a separate paper package.

## Licensing Of Contributions

Unless stated otherwise in writing, contributions submitted to this repository
are provided under the same MIT License that governs the project.

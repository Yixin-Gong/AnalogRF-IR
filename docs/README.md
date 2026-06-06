# AnalogRF-IR Documentation

This directory contains the technical notes behind the root project overview.
The root [README](../README.md) is the canonical GitHub entry point; this file
is a documentation index rather than a mirrored copy.

## Start Here

- [Quick Start](quickstart.md): install dependencies, run YAML designs, configure
  DeepSeek, import SPICE, run feasibility checks, and inspect run artifacts.
- [Architecture](architecture.md): execution flow, profile/capability model,
  diagnosis and action planning, validation strategy, artifact boundaries, and
  the agent loop.
- [Schema Guide](schema_guide.md): YAML schema structure, generated diagnostic
  contract, physical constraints, OTA notes, comparator notes, and new-schema
  checklist.
- [Method Comparisons](ablation_experiments.md): ablation matrix, metrics,
  reporting guidance, CLI commands, and plotting outputs.
- [Development Guide](development.md): repository conventions and extension
  workflow for new circuit families.

## Figures

The `assets/` directory contains the architecture and result figures used by
the root README and the local manuscript draft:

- `analogdiag_architecture.png`
- `analogdiag_diagnosis_loop.png`
- `full_flow_ota_achievement.png`
- `full_flow_ota_summary.png`
- `diagnosis_validation.png`
- `success_rate_by_method_topology.png`

Regenerate figures from a completed ablation manifest with the plotting scripts
described in [Method Comparisons](ablation_experiments.md).

## Publication Package Notes

The local manuscript draft lives under `paper/`, which is intentionally ignored
by Git. Keep camera-ready paper sources, private submission metadata, and local
draft figures there unless the maintainers deliberately decide to publish a
separate paper artifact.

GitHub-facing citation metadata is kept at the repository root in
[`CITATION.cff`](../CITATION.cff). Update it when a preprint, DOI, or accepted
paper citation becomes available.

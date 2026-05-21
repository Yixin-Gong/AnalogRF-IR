# AnalogRF-IR Documentation

This directory contains the maintained project notes for AnalogRF-IR. The main
repository README gives the broad overview; these documents focus on practical
usage, architecture, schema authoring, and development rules.

## Documents

- [Quick Start](quickstart.md): environment setup, common commands, and run outputs.
- [Architecture](architecture.md): module boundaries and the end-to-end data flow.
- [Schema Guide](schema_guide.md): how YAML inputs describe circuits, targets, variables, and evaluations.
- [Development Guide](development.md): testing, extension workflow, and current limitations.

## Project Snapshot

AnalogRF-IR is a schema-driven analog/RF optimization prototype. A design starts
as YAML or an imported SPICE netlist, becomes a typed design state, is enriched
with IR and ASIR semantics, is optimized with gm/ID-aware compact models, and is
finally checked with ngspice-backed measurements when available.

The current strongest path is OTA exploration, especially the two-stage Miller
OTA example. Comparator schemas and ASIR semantic extraction are present, but
comparator signoff still needs dedicated transient, noise, offset, kickback, and
Monte Carlo testbenches.

## Source Of Truth

After each run, `design_state.yaml` in the generated run directory is the
canonical state artifact. JSON files such as `sim_log.json`,
`agent_diagnostics.json`, `causal_diagnostics.json`, and `result.json` are
derived views for scripts and agents.

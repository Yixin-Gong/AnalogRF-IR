# Quick Start

## Requirements

- Python 3.10 or newer.
- Packages from `requirements.txt`.
- `ngspice` for simulator-backed validation.
- Process model files or gm/ID lookup tables for the target process.

On Ubuntu or WSL:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip ngspice
```

Create the Python environment:

```bash
cd <path-to-AnalogRF-IR>
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

## Run A YAML Design

Use explicit input paths for normal work:

```bash
python main.py \
  --env environment.yaml \
  --schema inputs/ota/two_stage_miller/two_stage_miller_ota.yaml \
  --topology yaml \
  --generations 50 \
  --pop-size 100 \
  --seed 23 \
  --agent-rounds 1
```

For a fast smoke run:

```bash
python main.py \
  --env environment.yaml \
  --schema inputs/ota/five_transistor/five_transistor_ota.yaml \
  --topology yaml \
  --generations 3 \
  --pop-size 10 \
  --seed 1 \
  --skip-dc-repair \
  --skip-comp-tune \
  --agent-rounds 1
```

## Configure DeepSeek

LLM-guided agent rounds use `DEEPSEEK_API_KEY` by default. You can provide the
key through an environment variable:

```bash
export DEEPSEEK_API_KEY=<your-key>
python main.py --agent-rounds 20 --llm-provider deepseek --llm-model deepseek-v4-pro
```

For one-off runs, the CLI can also load the key from a file, standard input, or
an explicit argument:

```bash
python main.py --llm-api-key-file ~/.config/analogrf-ir/deepseek.key --agent-rounds 20
printf '%s\n' "$DEEPSEEK_API_KEY" | python main.py --llm-api-key-stdin --agent-rounds 20
python main.py --llm-api-key <your-key> --agent-rounds 20
```

`--llm-api-key-file` and `--llm-api-key-stdin` are preferred for regular use
because command-line arguments can be captured by shell history or process
inspection.

## Import SPICE

Convert a supported flat MOS netlist to YAML:

```bash
python scripts/spice_to_yaml.py input.cir --out runs/imported.yaml --name imported_design
```

Run the imported schema:

```bash
python main.py \
  --env environment.yaml \
  --spice input.cir \
  --spice-yaml-out runs/imported.yaml \
  --schema runs/imported.yaml \
  --topology yaml \
  --agent-rounds 1
```

The SPICE parser is intentionally conservative. It handles common flat
MOS/resistor/capacitor netlists and canonicalizes generated MOS names such as
`MM1` back to schema device id `M1`.

## Feasibility Check

Run the two-stage Miller OTA feasibility checker before a long optimization:

```bash
python scripts/run_feasibility_check.py \
  --env environment.yaml \
  --schema inputs/ota/two_stage_miller/two_stage_miller_ota.yaml \
  --topology yaml \
  --samples 6000 \
  --seed 19
```

The checker writes a report under `runs/feasibility_###/` and prints a JSON
summary with the classification, output directory, evaluated candidate count,
and best candidate.

## Run Artifacts

Each optimization run writes `runs/iter_###/` with:

```text
design_state.yaml          Canonical state with compact result/tuning summary
netlist.cir                Generated SPICE netlist
sim_log.json               Simulation-focused diagnostic view
agent_diagnostics.json     Agent-facing pass/fail and tuning context
causal_diagnostics.json    Root-cause graph and suggested schema moves
result.json                Compact final result view
```

If `ngspice` is unavailable, the flow still writes structured outputs, but the
simulation result is marked unsuccessful.

`design_state.yaml` intentionally omits environment-derived process/simulation
settings, the full simulation log, agent diagnostics, dependency graph, and
validation transcript so the schema remains human-readable. Use the active
environment file for process context and the JSON artifacts in the same
directory when you need the full diagnostic detail.

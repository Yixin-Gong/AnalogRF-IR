# Development Progress Log

## 2026-05-19

### Scope

完成 two-stage OTA 优化流程的调试、重构、500 MHz 规格验证、Pareto 分析和方法报告整理。

### Major Changes

1. Added robust AC-curve based phase margin extraction in `simulator/ngspice.py`.
   - Export full Bode data to `ac_sweep.dat`.
   - Parse gain/phase in Python.
   - Unwrap phase before PM calculation.
   - Detect unity-gain crossings and use the worst crossing.

2. Fixed two-stage compensation tuning in `main.py`.
   - Corrected `_unique_sorted()` tolerance for femto/pico-scale capacitance values.
   - Expanded `Cc/Rz` tuning grid around the useful 500 MHz compensation region.
   - Prevented stale `ac_sweep.dat` data from contaminating repeated ngspice runs.

3. Added deterministic physical repair steps.
   - Stage-2 DC output balancing by scaling M6 width.
   - Tail current-source headroom repair by scaling M1/M2 width when M5 margin is low.

4. Updated current working spec in `ir/schema_two_stage.yaml`.
   - `unity_gain_bandwidth.min` increased to `500000000 Hz`.

5. Added Pareto analysis scripts.
   - `scripts/run_spec_pareto.py`: estimated full-variable Pareto exploration with optional ngspice checks.
   - `scripts/run_compensation_pareto.py`: ngspice Cc/Rz sweep and local compensation Pareto export.

6. Added method report.
   - `docs/two_stage_ota_method_report.md` documents method, algorithm, data flow, PM theory, experiment setup, final results, limitations, and future work.

### Key Debug Findings

The earlier `PM=166 deg` result was not a valid phase margin. It came from using ngspice principal phase directly at the 0 dB crossing. After phase unwrap, the same unstable point had approximately:

```text
unwrapped phase at unity ~= -193.6 deg
phase margin ~= -13.6 deg
```

This explained why the previous two-stage optimization did not converge reliably.

### Current Best 500 MHz Result

Latest verified design:

```text
runs/iter_024/
```

ngspice measurements:

```text
dc_gain_db              68.84 dB
unity_gain_bandwidth    532.43 MHz
phase_margin            100.86 deg
total_power             251.68 uW
unity_gain_crossings    1
```

Compensation:

```text
Cc = 500 fF
Rz = 3.5 kohm
```

Validation:

```text
PASSED: 0 errors, 3 warnings
```

Remaining warnings are conservative voltage-stack warnings, M5 inversion-region preference, and power-density notice.

### Pareto Results

Local ngspice compensation Pareto:

```text
runs/pareto_comp_001/
```

Summary:

```text
252 Cc/Rz sweep points
43 Pareto points
32 points pass the 500 MHz spec
```

Representative feasible tradeoff points:

```text
Cc=2.50pF  Rz=3.5k  UGBW=530.1MHz  PM=112.8deg
Cc=0.50pF  Rz=3.5k  UGBW=532.4MHz  PM=100.9deg
Cc=0.30pF  Rz=3.5k  UGBW=557.2MHz  PM=89.9deg
Cc=2.50pF  Rz=4.0k  UGBW=756.6MHz  PM=86.0deg
Cc=2.50pF  Rz=4.5k  UGBW=889.6MHz  PM=68.8deg
```

### Reproduction Commands

Run final 500 MHz optimization:

```bash
python3 main.py --schema ir/schema_two_stage.yaml --generations 60 --pop-size 120 --seed 23
```

Run estimated full-variable Pareto:

```bash
python3 scripts/run_spec_pareto.py --schema ir/schema_two_stage.yaml --pop-size 180 --generations 80 --seed 31 --verify 16
```

Run ngspice compensation Pareto:

```bash
python3 scripts/run_compensation_pareto.py --netlist runs/iter_024/netlist.cir --out runs/pareto_comp_001
```

### Next Recommended Work

1. Add a true Middlebrook/Tian loop-gain testbench for final PM signoff.
2. Improve compact PM model calibration against ngspice.
3. Add PVT, load sweep, noise, slew rate, input/output swing, and Monte Carlo checks.
4. Decide whether M5 should be forced into stronger inversion by schema constraints.

## 2026-05-19 IHP SG13G2 Update

### Scope

Added an IHP SG13G2 130 nm PDK flow while keeping the PTM flow intact.

### Major Changes

1. Added `environment_ihp_sg13g2.yaml`.
   - Uses IHP `cornerMOSlv.lib` with `mos_tt`.
   - Loads `psp103.osdi` through an auto-generated local `.spiceinit`.
   - Uses subckt devices `sg13_lv_nmos` and `sg13_lv_pmos`.

2. Generalized netlist generation.
   - Supports `.lib <path> <corner>`.
   - Supports subckt MOS instances (`XM1 ... model W=... L=...`).
   - Keeps PTM `.include` / `M...` behavior for the original environment.

3. Generalized ngspice operating-point parsing.
   - Maps IHP PSP paths like `@n.xm1.nsg13_lv_nmos[gm]` back to `M1`.
   - Maps PSP `ids` to `id` and `vdss` to `vdsat`.

4. Extended gm/id table generation.
   - Added CLI flags for model library, corner, subckt device style, OSDI, and output prefix.
   - Generated `tables/ihp_sg13g2_nmos.npz` and `tables/ihp_sg13g2_pmos.npz`.

5. Improved two-stage DC repair.
   - M6 and M7 can now both be adjusted during output balancing.
   - Repair width changes are clamped to process `min_W` / `max_W`.

### Validation

IHP lookup table generation completed successfully:

```text
NMOS GM_ID range: 1.6 ... 31.7 S/A
PMOS GM_ID range: 2.0 ... 31.9 S/A
```

IHP full-flow run:

```text
runs/iter_026/
gain   ~= 38.6 dB
UGBW   ~= 312 MHz
PM     ~= 78.6 deg
power  ~= 190 uW
```

Best IHP high-bandwidth compensation diagnostic:

```text
runs/pareto_comp_ihp_verify03/
gain   ~= 47.7 dB
UGBW   ~= 593 MHz
PM     ~= 65.9 deg
power  ~= 322 uW
```

### Diagnosis

The PDK connection is working, but the original `60 dB / 500 MHz / 60 deg` two-stage spec is not yet solved under IHP SG13G2. The IHP runs show a real tradeoff:

- Short M6/M7 lengths can reach the 500 MHz and PM targets, but DC gain stays around 48 dB.
- Longer M7 / higher output resistance can raise gain toward 58 dB with stable PM, but UGBW drops below 200 MHz.
- The compact optimizer still mispredicts IHP two-stage gain and second-stage DC bias after repair, so the next step should be ngspice-in-the-loop or surrogate-corrected optimization for the second-stage bias and compensation variables.

## 2026-05-19 ASIR Merge And YAML/SPICE Frontend

### Scope

Merged the `obj_irv2.0` ASIR project into `objective_ir` and added a new frontend path so the optimizer can be driven by explicit YAML topology/spec files or by SPICE netlists converted into YAML.

### Major Changes

1. Added the `asir/` package, comparator YAML examples under `inputs/`, and ASIR tests.
2. Added `frontends/yaml_loader.py`.
   - Builds `DesignState` directly from YAML topology, targets, constraints, variables, losses, evaluations, and transistor seed values.
   - Keeps process and simulation ownership in `environment.yaml`.
   - Synthesizes missing initial values from device roles and constraints.
   - Auto-generates default loss terms from targets when YAML omits `loss_terms`.
3. Added `frontends/spice_parser.py`.
   - Parses flat MOS, resistor, capacitor, and voltage source lines.
   - Infers OTA roles, ports, architecture, constraints, design variables, compensation globals, and default objectives.
   - Canonicalizes generated instance names such as `MM1` back to stable YAML ids such as `M1`.
4. Added `scripts/spice_to_yaml.py`.
   - Converts a SPICE netlist into the new YAML format.
   - Can be executed directly from WSL or Windows because it inserts the repo root into `sys.path`.
5. Extended `main.py`.
   - `--topology yaml` forces YAML-driven construction.
   - `--topology auto` now chooses the YAML frontend when the schema has explicit devices.
   - `--spice` plus `--spice-yaml-out` runs SPICE -> YAML -> diagnosis/initialization -> optimization.
6. Added `docs/yaml_spice_frontend_method.md` with the method, algorithm, flow, commands, and limitations.
7. Updated `requirements.txt` with `networkx` and `pytest` for ASIR and tests.

### Validation

Windows Python:

```text
python -m pytest tests/test_frontends.py tests/test_asir.py -q
9 passed
```

Ubuntu-26.04 WSL:

```text
python3 -m pytest tests/test_frontends.py tests/test_asir.py -q
9 passed
```

Main CLI smoke test:

```bash
python3 main.py --spice runs/iter_024/netlist.cir \
  --spice-yaml-out runs/tmp_spice_import/main_cli.yaml \
  --generations 1 --pop-size 4 --seed 3 --ngspice-bin /no/such/ngspice
```

Result:

```text
SPICE parsed to YAML
DesignState built with 7 MOS devices and 18 variables
4 spec-derived loss terms loaded
NSGA-II completed 1 smoke generation
SPICE netlist generated successfully
```

### Environment Notes

Ubuntu-26.04 WSL already had `numpy` and `pyyaml`. Installed missing frontend/test dependencies:

```bash
python3 -m pip install --user networkx pytest --break-system-packages
```

## 2026-05-19 IHP Run Through YAML/SPICE Frontend

### Scope

Ran the new SPICE -> YAML -> optimizer -> ngspice flow with `environment_ihp_sg13g2.yaml`.

### Interface Fix

SPICE-imported YAML can contain generic model names such as `nmos` and `pmos`. The YAML frontend now maps those generic names to the active process models:

```text
IHP NMOS: sg13_lv_nmos
IHP PMOS: sg13_lv_pmos
```

This prevents a PTM-origin netlist import from accidentally generating IHP subckt instances with stale `nmos/pmos` model names.

### Command

```bash
python3 main.py --env environment_ihp_sg13g2.yaml \
  --spice runs/iter_024/netlist.cir \
  --spice-yaml-out runs/tmp_spice_import/flow_test_ihp_from_spice.yaml \
  --generations 45 --pop-size 80 --seed 23
```

### Result

Run directory:

```text
runs/iter_031/
```

ngspice measurements:

```text
dc_gain_db              49.56 dB
unity_gain_bandwidth    377.90 MHz
phase_margin            69.76 deg
total_power             280.78 uW
```

The IHP flow is wired correctly and produces a stable verified result, but this imported/default objective setup still does not meet the original `60 dB / 500 MHz / 60 deg` target. It primarily misses gain and UGBW.

## 2026-05-19 Structured Agent Diagnostics

### Scope

Removed the append-only root `history.md` write path from `main.py` and replaced it with per-run structured diagnostics.

### Major Changes

1. `main.py` no longer appends Markdown history to the repository root.
2. Each run now writes:
   - `design_state.yaml`
   - `netlist.cir`
   - `sim_log.json`
   - `agent_diagnostics.json`
3. `agent_diagnostics.json` is designed for agent parsing and includes:
   - run metadata
   - ngspice success/return code
   - spec pass/fail status
   - per-target margins
   - dominant loss contributors
   - optimizer/ngspice mismatch
   - per-device operating status
   - machine-readable diagnosis items
4. The no-ngspice early-exit path now also writes `sim_log.json` and `agent_diagnostics.json`, so missing simulator failures are structured instead of only printed.

### Validation

```text
python -m py_compile main.py
python3 -m pytest tests/test_frontends.py tests/test_asir.py -q
10 passed
```

Smoke output:

```text
runs/iter_033/agent_diagnostics.json
```

## 2026-05-19 Strict Spec Verification And CMRR/PSRR Removal

### Scope

Made spec pass/fail stricter and removed CMRR/PSRR from the active optimization problem until proper measurement testbenches are added.

### Major Changes

1. `agent_diagnostics.json` now marks priority-1 targets as `unverified` when they are not backed by ngspice measurements.
2. `status.spec_pass` is true only when there are no failed targets and no unverified priority-1 targets.
3. Each target now includes:
   - `source`
   - `requires_ngspice`
   - `model_status`
   - absolute and relative margins
4. Removed CMRR and PSRR targets/losses from:
   - default five-transistor OTA builder
   - default two-stage OTA builder
   - `ir/schema.yaml`
   - `ir/schema_two_stage.yaml`
   - tracked schema backup
5. Removed CMRR/PSRR estimates from optimizer performance dictionaries so they do not appear as optimization metrics.

### Validation

```text
python -m py_compile main.py optimizer/nsga2.py
python3 -m pytest tests/test_frontends.py tests/test_asir.py -q
10 passed
```

No-ngspice smoke output:

```text
runs/iter_034/agent_diagnostics.json
```

The smoke run correctly reported:

```text
spec_pass: false
unverified_targets: [dc_gain, unity_gain_bandwidth, phase_margin]
```

## 2026-05-19 Full Current-Mirror Two-Stage Model

### Scope

Converted the IHP two-stage OTA biasing to explicit current mirrors on both the tail and second-stage sink branches, then added compact-model diagnostics for the new bias topology.

### Major Changes

1. Added an output bias mirror device `M9` to `ir/schema_two_stage.yaml`.
2. Updated the netlist generator so `tail_bias_mirror` and `output_bias_mirror` emit reference current sources instead of ideal bias-voltage sources.
3. Updated the SPICE parser and YAML frontend so imported diode-connected tail/output bias transistors keep their semantic roles.
4. Added optimizer-side current mirror modeling:
   - tail and output mirror copy factors
   - stage-2 current demand/capacity/effective current
   - `stage2_balance_factor` loss term
   - PMOS load gate capacitance diagnostic
5. Tightened the high-gain two-stage search region by raising selected channel-length lower bounds and gain-deficit weight.
6. Fixed the DC repair flow so stage-2 re-balance first evaluates the current operating point and does not destructively resize M6 when `vout` is already in a valid output window.

### Validation

```text
python3 -m py_compile main.py netlist/generator.py optimizer/nsga2.py frontends/yaml_loader.py frontends/spice_parser.py tests/test_frontends.py
manual tests/test_frontends.py checks: passed
```

IHP all-current-mirror runs:

```text
runs/iter_050  optimizer sizing only:
  gain 31.7 dB, UGBW 100.5 MHz, PM 41.6 deg, power 270.3 uW

runs/iter_058  full flow after repair fix:
  gain 59.3 dB, UGBW 62.6 MHz, PM 81.4 deg, power 304.4 uW
```

### Current Diagnosis

The all-current-mirror model fixes the earlier output-collapse failure mode and can find ngspice-verified high-gain points. The remaining IHP two-stage bottleneck is bandwidth: reaching 60 dB and stable PM currently drives long-channel/high-capacitance PMOS mirror loads, which limits UGBW far below the 500 MHz target. A simultaneous 60 dB / 500 MHz / 60 deg IHP point likely needs a SPICE-aware outer-loop search, topology changes, or a looser gain/bandwidth tradeoff.

## 2026-05-20 Modular Flow Refactor

### Scope

Refactored the execution path so schema/SPICE input, ASIR enrichment, environment loading, gm/ID lookup, optimization, post-processing, ngspice simulation, spec interpretation, and artifact writing are separated behind explicit modules. The CLI still preserves the original command shape, but now delegates to a modular runner.

### Major Changes

1. Added `core.environment` as the single environment/process/simulation builder. `frontends.yaml_loader` no longer imports `main.py`.
2. Added `frontends.design_input` to normalize schema or SPICE input into a `DesignState`, with optional ASIR semantic annotation.
3. Added `pygmid.plugin.GmIdPlugin` so gm/ID lookup tables are treated as a service; optional table auto-generation is controlled by environment `tools.auto_generate_tables`.
4. Added `optimizer.registry` to decouple algorithm selection from the flow. NSGA-II remains the active implementation.
5. Added `specs.models` with OTA, comparator, sample/hold, and generic spec models. The flow selects a spec model from `topology.class` and `topology.architecture`.
6. Added `postprocess` modules for ngspice backfill and two-stage OTA repair/tuning.
7. Added `outputs.artifacts` to own `design_state.yaml`, `netlist.cir`, `sim_log.json`, `agent_diagnostics.json`, and the new compact `result.json`.
8. Added `flow.runner.ObjectiveIRFlowRunner` as the single orchestrator. It validates schema at input, post-optimizer, post-process, and post-ngspice update points before writing artifacts.

### Validation

```text
python3 -m py_compile main.py core/environment.py frontends/design_input.py specs/models.py pygmid/plugin.py optimizer/registry.py flow/runner.py flow/state_update.py outputs/artifacts.py postprocess/common.py postprocess/two_stage.py tests/test_modular_flow.py tests/test_frontends.py tests/test_asir.py
python3 -m pytest tests/test_frontends.py tests/test_asir.py tests/test_modular_flow.py -q
15 passed
```

Smoke flow:

```text
python3 main.py --env environment_ihp_sg13g2.yaml --schema ir/schema_two_stage.yaml --generations 2 --pop-size 8 --seed 7 --skip-dc-repair --skip-comp-tune

runs/iter_059/
  design_state.yaml
  netlist.cir
  sim_log.json
  agent_diagnostics.json
  result.json
```

This smoke run was intentionally tiny and not performance-oriented. It verified the full data chain: schema/env input, ASIR-safe frontend, gm/ID plugin, optimizer update, schema validation, netlist emission, ngspice simulation, state backfill, and JSON artifact output.

## 2026-05-20 Legacy Migration and Main Slimming

### Scope

Completed the migration that the previous modular refactor prepared: `main.py` is now only a CLI adapter, legacy topology synthesis lives outside the runner, and external scripts/tests no longer import implementation helpers from `main.py`.

### Major Changes

1. Replaced the old monolithic `main.py` with a thin CLI that builds `FlowConfig` and delegates to `ObjectiveIRFlowRunner`.
2. Added `topologies.legacy` for compatibility with old five-transistor and two-stage schema files that do not yet carry explicit `topology.devices`.
3. Updated Pareto and frontend tests to depend on stable modules (`core.environment`, `netlist.generator`, `postprocess`, `topologies.legacy`) instead of `main.py`.
4. Added `core.regions` so compact-model inversion labels (`weak`, `moderate`, `strong`) are not written into the SPICE operating-region field (`saturation`, `linear`, `off`, etc.).
5. Added a regression test covering that region/inversion split during optimizer-to-schema update.
6. Updated stale legacy schema comments to point at the YAML frontend and compatibility builder.

### Validation

```text
python3 -m py_compile main.py core/regions.py core/design_rules.py optimizer/nsga2.py flow/state_update.py topologies/legacy.py scripts/run_spec_pareto.py tests/test_modular_flow.py tests/test_frontends.py
python3 -m pytest tests/test_frontends.py tests/test_asir.py tests/test_modular_flow.py -q
16 passed
```

Legacy five-transistor smoke:

```text
python3 main.py --generations 1 --pop-size 4 --seed 5 --skip-dc-repair --skip-comp-tune --no-asir

runs/iter_060/
  ngspice_success: true
  dc_gain_db: 33.9 dB
  unity_gain_bandwidth: 220.8 MHz
  phase_margin: 77.3 deg
  total_power: 49.3 uW
```

IHP two-stage explicit-YAML smoke:

```text
python3 main.py --env environment_ihp_sg13g2.yaml --schema ir/schema_two_stage.yaml --generations 1 --pop-size 4 --seed 9 --skip-dc-repair --skip-comp-tune

runs/iter_061/
  ngspice_success: true
  dc_gain_db: 25.6 dB
  unity_gain_bandwidth: 12.7 MHz
  phase_margin: 86.0 deg
  total_power: 182.2 uW
```

The IHP smoke is intentionally tiny, so it is a wiring/data-chain check rather than a performance run. The structured diagnostics correctly report remaining gain/bandwidth target failures and the output/tail current-source headroom warnings.

## 2026-05-20 Flow Recheck and Repository Cleanup

### Scope

Re-ran the modular IHP two-stage flow after migration, then removed files that are generated, temporary, or superseded by the new schema/topology path.

### Validation

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests/test_frontends.py tests/test_asir.py tests/test_modular_flow.py -q
16 passed

python3 main.py --env environment_ihp_sg13g2.yaml --schema ir/schema_two_stage.yaml --generations 2 --pop-size 8 --seed 13

runs/iter_062/
  ngspice_success: true
  dc_gain_db: 59.3 dB
  unity_gain_bandwidth: 67.2 MHz
  phase_margin: 63.9 deg
  total_power: 107.9 uW
```

### Cleanup

1. Removed Python bytecode caches and `.pytest_cache`.
2. Removed `runs/debug_*` and `runs/tmp_*` directories.
3. Removed `ir/schema.yaml.bak`; the active schema path and `topologies.legacy` compatibility builder now cover that role.
4. Kept historical `runs/iter_*` and Pareto result directories because they contain useful performance evidence and diagnostics.

## 2026-05-20 Lowered IHP Two-Stage OTA Spec

### Scope

Relaxed the IHP two-stage OTA target specification to 50 dB gain, 100 MHz unity-gain bandwidth, and 45 degree phase margin, then re-ran the full optimizer plus ngspice verification flow.

### Changes

1. Updated `ir/schema_two_stage.yaml` targets:
   - `dc_gain.min`: 50 dB
   - `unity_gain_bandwidth.min`: 100 MHz
   - `phase_margin.min`: 45 deg
2. Added a guarded post-process refinement in `postprocess.two_stage`: if the standard compensation sweep still misses spec, the flow now tries a small grid-aligned W/L scale on the first-stage PMOS current-mirror load and re-checks a compact compensation grid.
3. Snapped post-process width/length scaling to the process W/L grid before validation and netlist emission.

### Validation

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests/test_frontends.py tests/test_asir.py tests/test_modular_flow.py -q
16 passed

python3 main.py --env environment_ihp_sg13g2.yaml --schema ir/schema_two_stage.yaml --generations 16 --pop-size 36 --seed 31

runs/iter_066/
  spec_pass: true
  dc_gain_db: 50.110 dB
  unity_gain_bandwidth: 104.932 MHz
  phase_margin: 68.335 deg
  total_power: 112.030 uW
```

### Notes

The lowered spec is now reachable with the all-current-mirror IHP topology. Remaining warnings are mainly conservative stack/headroom and inversion preference diagnostics on the tail branch; they do not block the current target pass.

## 2026-05-20 Feasibility Check Flow

### Scope

Added a standalone physics-informed feasibility checker. Its purpose is not final W/L optimization; it estimates whether a topology/process/spec combination has a plausible high-level design region before launching expensive optimizer/ngspice loops.

### Major Changes

1. Added `feasibility.two_stage_miller.TwoStageMillerFeasibilityChecker`.
2. Added CLI entrypoint `scripts/run_feasibility_check.py`.
3. The checker searches high-level variables only:
   - `gmID_in`, `gmID_stage2`, `gmID_load`
   - `L_in`, `L_load`, `L_stage2`
   - `Cc/CL`
   - `Rz_factor`, where `Rz = Rz_factor / gm2`
4. The model uses gm/ID lookup tables when present and falls back to the analytical EKV-like adapter when tables are missing. Reports explicitly mark the source.
5. The two-stage Miller model checks GBW-gm1, SR, PM/second-pole separation, power lower bound, gm/gds gain, headroom, estimated parasitics, ft reserve, and validation risk.
6. Reports are written as:
   - `feasibility_report.json`
   - `feasibility_report.md`
   - `best_candidates.csv`

### Validation

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests/test_frontends.py tests/test_asir.py tests/test_modular_flow.py -q
17 passed

python3 scripts/run_feasibility_check.py --env environment_ihp_sg13g2.yaml --schema ir/schema_two_stage.yaml --samples 6000 --seed 41

runs/feasibility_005/
  classification: roughly feasible
  evaluated: 6000
  best candidate:
    gmID_in: 18
    gmID_stage2: 10
    gmID_load: 10
    IC_in / IC_stage2: 2.36 / 7.64
    L_in/L_load/L_stage2: 0.65um / 0.65um / 0.50um
    Cc: 150 fF
    Itail: 10.47 uA
    I2: 28.98 uA
    predicted gain: 54.98 dB
    predicted GBW: 100.0 MHz
    predicted PM: 65.56 deg
    predicted SR+/SR-: 69.81 / 138.23 V/us
    predicted power: 57.76 uW
```

### Notes

The feasibility report correctly classifies the relaxed IHP two-stage OTA spec as roughly feasible, matching the later ngspice-passing optimizer result. The report still flags Middlebrook return-ratio loop-gain verification as mandatory because analytical PM is only a screening metric.

## 2026-05-20 Slew-Rate Optimization And Ngspice Validation

### Scope

Added slew-rate as a first-class OTA metric in the optimizer, schema, structured diagnostics, and ngspice validation flow.

### Major Changes

1. Added `slew_rate` target to `ir/schema_two_stage.yaml`:
   - `slew_rate.min`: 50 V/us (`5.0e7 V/s`)
2. Added optimizer estimates:
   - Five-transistor OTA: `SR ~= Itail / Cout`
   - Two-stage Miller OTA: `SR+ ~= Itail / Cc`, `SR- ~= Istage2 / (CL + Cpar_out)`, and `slew_rate = min(SR+, SR-)`
3. Added `sr_deficit` loss term so SR participates in optimization.
4. Added transient analysis generation when SR targets/evaluations are present.
5. Added ngspice transient pass:
   - Keeps AC/DC testbenches unchanged.
   - Injects a differential pulse only in the transient pass.
   - Exports `vout(t)` and computes `slew_rate`, `slew_rate_pos`, and `slew_rate_neg` from the waveform.
6. Postprocess compensation/DC repair sweeps explicitly disable transient evaluation to avoid multiplying runtime during internal candidate sweeps.

### Validation

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider tests/test_frontends.py tests/test_asir.py tests/test_modular_flow.py -q
19 passed

python3 scripts/run_feasibility_check.py --env environment_ihp_sg13g2.yaml --schema ir/schema_two_stage.yaml --samples 3000 --seed 41

runs/feasibility_006/
  classification: roughly feasible
  best predicted SR+/SR-: 69.81 / 138.23 V/us

python3 main.py --env environment_ihp_sg13g2.yaml --schema ir/schema_two_stage.yaml --generations 3 --pop-size 10 --seed 31 --skip-dc-repair --skip-comp-tune

runs/iter_068/
  ngspice slew_rate: 133.43 V/us
  ngspice slew_rate_pos: 133.43 V/us
  ngspice slew_rate_neg: 136.34 V/us
  SR target status: pass

Direct transient validation on the prior passing IHP sizing:
runs/iter_066/netlist.cir
  dc_gain_db: 50.110 dB
  unity_gain_bandwidth: 104.932 MHz
  phase_margin: 68.335 deg
  total_power: 112.030 uW
  slew_rate: 53.132 V/us
  slew_rate_pos: 91.514 V/us
  slew_rate_neg: 53.132 V/us
```

### Notes

The short optimizer run intentionally verifies the new SR data chain rather than seeking a final optimum. A full `16 x 36` run hit the external command timeout while in the existing ngspice-heavy postprocess loop; the new transient pass itself takes about 2.7 seconds on the prior passing netlist. Further runtime reduction should focus on pruning the compensation sweep candidate count or adding an early-stop condition once AC/DC specs are already met.

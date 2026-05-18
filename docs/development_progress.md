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

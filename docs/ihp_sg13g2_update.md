# IHP SG13G2 PDK Update

## Scope

This update adds an IHP SG13G2 130 nm ngspice flow while keeping the existing PTM 130 nm flow available.

## Implemented Changes

- Added `environment_ihp_sg13g2.yaml` for IHP SG13G2 LV MOS TT corner.
- Added support for `.lib <model> <corner>` and OSDI-backed local `.spiceinit` generation.
- Added subcircuit MOS instantiation: `XM1 ... sg13_lv_nmos` / `sg13_lv_pmos`.
- Added IHP-compatible operating-point parsing for PSP subckt paths such as `@n.xm1.nsg13_lv_nmos[gm]`.
- Added IHP gm/id table generation options and generated:
  - `tables/ihp_sg13g2_nmos.npz`
  - `tables/ihp_sg13g2_pmos.npz`
- Updated Pareto script to accept `--env`.
- Improved two-stage DC repair so both M6 and M7 can be adjusted, with `max_W` respected.

## IHP PDK Paths

```bash
/mnt/d/IHP-Open-PDK-0.3.0/ihp-sg13g2/libs.tech/ngspice/models/cornerMOSlv.lib
/mnt/d/IHP-Open-PDK-0.3.0/ihp-sg13g2/libs.tech/ngspice/osdi/psp103.osdi
```

## Reproduction

Generate IHP lookup tables:

```bash
python3 pygmid/generate_tables.py --device both \
  --model-lib /mnt/d/IHP-Open-PDK-0.3.0/ihp-sg13g2/libs.tech/ngspice/models/cornerMOSlv.lib \
  --model-corner mos_tt \
  --nmos-model sg13_lv_nmos \
  --pmos-model sg13_lv_pmos \
  --device-style subckt \
  --osdi /mnt/d/IHP-Open-PDK-0.3.0/ihp-sg13g2/libs.tech/ngspice/osdi/psp103.osdi \
  --output tables --output-prefix ihp_sg13g2 \
  --L-min 130e-9 --L-max 2e-6 --L-points 11 --vgs-step 0.05 --vds 0.6
```

Run IHP two-stage optimization:

```bash
python3 main.py --env environment_ihp_sg13g2.yaml --schema ir/schema_two_stage.yaml --generations 80 --pop-size 160 --seed 41
```

Run IHP spec Pareto:

```bash
python3 scripts/run_spec_pareto.py --env environment_ihp_sg13g2.yaml --schema ir/schema_two_stage.yaml --pop-size 160 --generations 80 --seed 41 --verify 8
```

## Current IHP Findings

The IHP model integration is functional, but the original 60 dB / 500 MHz / 60 deg spec has not yet been met by the present two-stage optimizer.

Useful ngspice-verified points found during diagnosis:

```text
runs/iter_058:
  gain ~= 59.3 dB, UGBW ~= 62.6 MHz, PM ~= 81.4 deg, power ~= 304 uW

runs/iter_056:
  optimizer-only sizing, gain ~= 60.9 dB, UGBW ~= 71.6 MHz, PM ~= 0 deg, power ~= 299 uW

runs/pareto_comp_ihp_verify03:
  gain ~= 47.7 dB, UGBW ~= 593 MHz, PM ~= 65.9 deg, power ~= 322 uW

runs/ihp_verify03_local_001/m7_800:
  gain ~= 58.3 dB, UGBW ~= 149 MHz, PM ~= 65.7 deg, power ~= 156 uW

runs/iter_026:
  gain ~= 38.6 dB, UGBW ~= 312 MHz, PM ~= 78.6 deg, power ~= 190 uW
```

Interpretation: IHP SG13G2 can produce either a stable high-bandwidth point or a higher-gain stable point in this topology, but the current compact optimizer and repair flow do not yet find a simultaneous 60 dB / 500 MHz / 60 deg solution. After adding explicit tail and output current mirrors, the high-gain point is now close to 60 dB with stable PM, but bandwidth falls to tens of MHz because the high-ro PMOS load mirror becomes very capacitive. The main remaining work is a true ngspice-in-the-loop or surrogate-corrected outer search for second-stage bias, output resistance, and compensation together, or a topology-level bandwidth improvement.

# Iteration 008
**Timestamp**: 2026-05-16T13:58:55.843435
**Design**: five_transistor_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 0.363596

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.5 | [12, 25] | 15 |
| M1.L | 5e-07 | [1.3e-07, 5e-07] | 2e-07 |
| M2.gm_id | 15.5 | [12, 25] | 15 |
| M2.L | 5e-07 | [1.3e-07, 5e-07] | 2e-07 |
| M3.gm_id | 7.476 | [5, 12] | 8 |
| M3.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M4.gm_id | 7.476 | [5, 12] | 8 |
| M4.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M5.gm_id | 13.11 | [8, 20] | 12 |
| M5.L | 4.75e-07 | [1.3e-07, 5e-07] | 2e-07 |
| I_tail | 2.091e-05 | [1e-06, 0.00025] | 5e-05 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.338508 |
| bw_deficit | 2.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 0.5 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.000000 |
| power_ratio | 0.3 | `realized.power/max(targets.power.max, 1e-9)` | 0.025088 |
| cmrr_deficit | 0.2 | `relu(targets.cmrr.min - realized.cmrr)/max(targets` | 0.000000 |
| psrr_deficit | 0.2 | `relu(targets.psrr_plus.min - realized.psrr_plus)/m` | 0.000000 |

## Correction Factors
- gm_factor: 0.78
- gds_factor: 1.0
- c_factor: 1.0
- description: gm VDS/body-effect correction from iter_003 ngspice back-calibration

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 41.2 | 42.9 | +1.67 |
| unity_gain_bandwidth | 1.01e+08 | 1.11e+08 | +1e+07 |
| phase_margin | 85 | 82.4 | -2.6 |
| power | 2.51e-05 | 2.23e-05 | -2.76e-06 |
| cmrr | 20.6 | N/A | N/A |
| psrr_plus | 12.4 | N/A | N/A |


---

# Iteration 009
**Timestamp**: 2026-05-16T14:00:48.049685
**Design**: five_transistor_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 0.363596

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.5 | [12, 25] | 15 |
| M1.L | 5e-07 | [1.3e-07, 5e-07] | 2e-07 |
| M2.gm_id | 15.5 | [12, 25] | 15 |
| M2.L | 5e-07 | [1.3e-07, 5e-07] | 2e-07 |
| M3.gm_id | 7.476 | [5, 12] | 8 |
| M3.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M4.gm_id | 7.476 | [5, 12] | 8 |
| M4.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M5.gm_id | 13.11 | [8, 20] | 12 |
| M5.L | 4.75e-07 | [1.3e-07, 5e-07] | 2e-07 |
| I_tail | 2.091e-05 | [1e-06, 0.00025] | 5e-05 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.338508 |
| bw_deficit | 2.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 0.5 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.000000 |
| power_ratio | 0.3 | `realized.power/max(targets.power.max, 1e-9)` | 0.025088 |
| cmrr_deficit | 0.2 | `relu(targets.cmrr.min - realized.cmrr)/max(targets` | 0.000000 |
| psrr_deficit | 0.2 | `relu(targets.psrr_plus.min - realized.psrr_plus)/m` | 0.000000 |

## Correction Factors
- gm_factor: 0.78
- gds_factor: 1.0
- c_factor: 1.0
- description: gm VDS/body-effect correction from iter_003 ngspice back-calibration

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 41.2 | 42.9 | +1.67 |
| unity_gain_bandwidth | 1.01e+08 | 1.11e+08 | +1e+07 |
| phase_margin | 85 | 82.4 | -2.6 |
| power | 2.51e-05 | 2.23e-05 | -2.76e-06 |
| cmrr | 20.6 | N/A | N/A |
| psrr_plus | 12.4 | N/A | N/A |


---

# Iteration 010
**Timestamp**: 2026-05-16T15:03:11.621904
**Design**: five_transistor_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 0.363596

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.5 | [12, 25] | 15 |
| M1.L | 5e-07 | [1.3e-07, 5e-07] | 2e-07 |
| M2.gm_id | 15.5 | [12, 25] | 15 |
| M2.L | 5e-07 | [1.3e-07, 5e-07] | 2e-07 |
| M3.gm_id | 7.476 | [5, 12] | 8 |
| M3.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M4.gm_id | 7.476 | [5, 12] | 8 |
| M4.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M5.gm_id | 13.11 | [8, 20] | 12 |
| M5.L | 4.75e-07 | [1.3e-07, 5e-07] | 2e-07 |
| I_tail | 2.091e-05 | [1e-06, 0.00025] | 5e-05 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.338508 |
| bw_deficit | 2.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 0.5 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.000000 |
| power_ratio | 0.3 | `realized.power/max(targets.power.max, 1e-9)` | 0.025088 |
| cmrr_deficit | 0.2 | `relu(targets.cmrr.min - realized.cmrr)/max(targets` | 0.000000 |
| psrr_deficit | 0.2 | `relu(targets.psrr_plus.min - realized.psrr_plus)/m` | 0.000000 |

## Correction Factors
- gm_factor: 0.78
- gds_factor: 1.0
- c_factor: 1.0
- description: gm VDS/body-effect correction from iter_003 ngspice back-calibration

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 41.2 | 42.9 | +1.67 |
| unity_gain_bandwidth | 1.01e+08 | 1.11e+08 | +1e+07 |
| phase_margin | 85 | 82.4 | -2.6 |
| power | 2.51e-05 | 2.23e-05 | -2.76e-06 |
| cmrr | 20.6 | N/A | N/A |
| psrr_plus | 12.4 | N/A | N/A |


---

# Iteration 011
**Timestamp**: 2026-05-16T15:07:31.628919
**Design**: five_transistor_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 0.363332

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.51 | [12, 25] | 15 |
| M1.L | 5e-07 | [1.3e-07, 5e-07] | 2e-07 |
| M2.gm_id | 15.51 | [12, 25] | 15 |
| M2.L | 5e-07 | [1.3e-07, 5e-07] | 2e-07 |
| M3.gm_id | 7.557 | [5, 12] | 8 |
| M3.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M4.gm_id | 7.557 | [5, 12] | 8 |
| M4.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M5.gm_id | 6.93 | [5, 9] | 8 |
| M5.L | 2.148e-07 | [1.3e-07, 5e-07] | 2e-07 |
| I_tail | 2.085e-05 | [1e-06, 0.00025] | 5e-05 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.338306 |
| bw_deficit | 2.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 0.5 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.000000 |
| power_ratio | 0.3 | `realized.power/max(targets.power.max, 1e-9)` | 0.025026 |
| cmrr_deficit | 0.2 | `relu(targets.cmrr.min - realized.cmrr)/max(targets` | 0.000000 |
| psrr_deficit | 0.2 | `relu(targets.psrr_plus.min - realized.psrr_plus)/m` | 0.000000 |

## Correction Factors
- gm_factor: 0.78
- gds_factor: 1.0
- c_factor: 1.0
- description: gm VDS/body-effect correction from iter_003 ngspice back-calibration

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 41.2 | 43 | +1.83 |
| unity_gain_bandwidth | 1e+08 | 9.96e+07 | -7.68e+05 |
| phase_margin | 83.6 | 82.7 | -0.856 |
| power | 2.5e-05 | 1.94e-05 | -5.62e-06 |
| cmrr | 20.6 | N/A | N/A |
| psrr_plus | 12.4 | N/A | N/A |


---

# Iteration 001
**Timestamp**: 2026-05-16T15:12:29.286923
**Design**: five_transistor_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 0.363332

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.51 | [12, 25] | 15 |
| M1.L | 5e-07 | [1.3e-07, 5e-07] | 2e-07 |
| M2.gm_id | 15.51 | [12, 25] | 15 |
| M2.L | 5e-07 | [1.3e-07, 5e-07] | 2e-07 |
| M3.gm_id | 7.557 | [5, 12] | 8 |
| M3.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M4.gm_id | 7.557 | [5, 12] | 8 |
| M4.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M5.gm_id | 6.93 | [5, 9] | 8 |
| M5.L | 2.148e-07 | [1.3e-07, 5e-07] | 2e-07 |
| I_tail | 2.085e-05 | [1e-06, 0.00025] | 5e-05 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.338306 |
| bw_deficit | 2.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 0.5 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.000000 |
| power_ratio | 0.3 | `realized.power/max(targets.power.max, 1e-9)` | 0.025026 |
| cmrr_deficit | 0.2 | `relu(targets.cmrr.min - realized.cmrr)/max(targets` | 0.000000 |
| psrr_deficit | 0.2 | `relu(targets.psrr_plus.min - realized.psrr_plus)/m` | 0.000000 |

## Correction Factors
- gm_factor: 0.78
- gds_factor: 1.0
- c_factor: 1.0
- description: gm VDS/body-effect correction from iter_003 ngspice back-calibration

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 41.2 | 43 | +1.83 |
| unity_gain_bandwidth | 1e+08 | 9.96e+07 | -7.68e+05 |
| phase_margin | 83.6 | 82.7 | -0.856 |
| power | 2.5e-05 | 1.94e-05 | -5.62e-06 |
| cmrr | 20.6 | N/A | N/A |
| psrr_plus | 12.4 | N/A | N/A |


---

# Iteration 002
**Timestamp**: 2026-05-16T15:21:40.828756
**Design**: five_transistor_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 0.087817

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.74 | [12, 25] | 15 |
| M1.L | 1e-06 | [1.3e-07, 1e-06] | 2e-07 |
| M2.gm_id | 15.74 | [12, 25] | 15 |
| M2.L | 1e-06 | [1.3e-07, 1e-06] | 2e-07 |
| M3.gm_id | 7.464 | [5, 12] | 8 |
| M3.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M4.gm_id | 7.464 | [5, 12] | 8 |
| M4.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M5.gm_id | 7.686 | [5, 9] | 8 |
| M5.L | 2.869e-07 | [1.3e-07, 5e-07] | 2e-07 |
| I_tail | 2.047e-05 | [1e-06, 0.00025] | 5e-05 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.063255 |
| bw_deficit | 2.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 0.5 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.000000 |
| power_ratio | 0.3 | `realized.power/max(targets.power.max, 1e-9)` | 0.024562 |
| cmrr_deficit | 0.2 | `relu(targets.cmrr.min - realized.cmrr)/max(targets` | 0.000000 |
| psrr_deficit | 0.2 | `relu(targets.psrr_plus.min - realized.psrr_plus)/m` | 0.000000 |

## Correction Factors
- gm_factor: 0.78
- gds_factor: 0.85
- c_factor: 1.0
- description: iter_001: gm_factor=0.78 (UGBW<1%), gds_factor→0.85 (dc_gain +4.4% bias)

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 44.3 | 44.6 | +0.276 |
| unity_gain_bandwidth | 1e+08 | 1.02e+08 | +2.23e+06 |
| phase_margin | 74.5 | 80.3 | +5.83 |
| power | 2.46e-05 | 2.01e-05 | -4.43e-06 |
| cmrr | 22.1 | N/A | N/A |
| psrr_plus | 13.3 | N/A | N/A |


---

# Iteration 003
**Timestamp**: 2026-05-16T15:27:48.423089
**Design**: five_transistor_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 0.039018

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.8 | [12, 25] | 15 |
| M1.L | 1.5e-06 | [1.3e-07, 1.5e-06] | 2e-07 |
| M2.gm_id | 15.8 | [12, 25] | 15 |
| M2.L | 1.5e-06 | [1.3e-07, 1.5e-06] | 2e-07 |
| M3.gm_id | 7.669 | [5, 12] | 8 |
| M3.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M4.gm_id | 7.669 | [5, 12] | 8 |
| M4.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M5.gm_id | 8.128 | [5, 9] | 8 |
| M5.L | 3.939e-07 | [1.3e-07, 5e-07] | 2e-07 |
| I_tail | 2.066e-05 | [1e-06, 0.00025] | 5e-05 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.014230 |
| bw_deficit | 2.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 0.5 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.000000 |
| power_ratio | 0.3 | `realized.power/max(targets.power.max, 1e-9)` | 0.024788 |
| cmrr_deficit | 0.2 | `relu(targets.cmrr.min - realized.cmrr)/max(targets` | 0.000000 |
| psrr_deficit | 0.2 | `relu(targets.psrr_plus.min - realized.psrr_plus)/m` | 0.000000 |

## Correction Factors
- gm_factor: 0.78
- gds_factor: 0.85
- c_factor: 1.0
- description: iter_001: gm_factor=0.78 (UGBW<1%), gds_factor→0.85 (dc_gain +4.4% bias)

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 44.8 | 45 | +0.198 |
| unity_gain_bandwidth | 1.01e+08 | 1.06e+08 | +4.39e+06 |
| phase_margin | 60.6 | 76.4 | +15.8 |
| power | 2.48e-05 | 2.1e-05 | -3.78e-06 |
| cmrr | 22.4 | N/A | N/A |
| psrr_plus | 13.5 | N/A | N/A |


---

# Iteration 004
**Timestamp**: 2026-05-18T23:43:14.013132
**Design**: five_transistor_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 0.039018

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.8 | [12, 25] | 15 |
| M1.L | 1.5e-06 | [1.3e-07, 1.5e-06] | 2e-07 |
| M2.gm_id | 15.8 | [12, 25] | 15 |
| M2.L | 1.5e-06 | [1.3e-07, 1.5e-06] | 2e-07 |
| M3.gm_id | 7.669 | [5, 12] | 8 |
| M3.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M4.gm_id | 7.669 | [5, 12] | 8 |
| M4.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M5.gm_id | 8.128 | [5, 9] | 8 |
| M5.L | 3.939e-07 | [1.3e-07, 5e-07] | 2e-07 |
| I_tail | 2.066e-05 | [1e-06, 0.00025] | 5e-05 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.014230 |
| bw_deficit | 2.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 0.5 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.000000 |
| power_ratio | 0.3 | `realized.power/max(targets.power.max, 1e-9)` | 0.024788 |
| cmrr_deficit | 0.2 | `relu(targets.cmrr.min - realized.cmrr)/max(targets` | 0.000000 |
| psrr_deficit | 0.2 | `relu(targets.psrr_plus.min - realized.psrr_plus)/m` | 0.000000 |

## Correction Factors
- gm_factor: 0.78
- gds_factor: 0.85
- c_factor: 1.0
- description: iter_001: gm_factor=0.78 (UGBW<1%), gds_factor→0.85 (dc_gain +4.4% bias)

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 44.8 | 45 | +0.198 |
| unity_gain_bandwidth | 1.01e+08 | 1.06e+08 | +4.39e+06 |
| phase_margin | 60.6 | 76.4 | +15.8 |
| power | 2.48e-05 | 2.1e-05 | -3.78e-06 |
| cmrr | 22.4 | N/A | N/A |
| psrr_plus | 13.5 | N/A | N/A |


---

# Iteration 005
**Timestamp**: 2026-05-18T23:52:01.164415
**Design**: five_transistor_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 0.042100

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.79 | [12, 25] | 15 |
| M1.L | 1.489e-06 | [1.3e-07, 1.5e-06] | 2e-07 |
| M2.gm_id | 15.79 | [12, 25] | 15 |
| M2.L | 1.489e-06 | [1.3e-07, 1.5e-06] | 2e-07 |
| M3.gm_id | 7.679 | [5, 12] | 8 |
| M3.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M4.gm_id | 7.679 | [5, 12] | 8 |
| M4.L | 5e-07 | [1.3e-07, 5e-07] | 5e-07 |
| M5.gm_id | 8.989 | [5, 9] | 8 |
| M5.L | 4.598e-07 | [1.3e-07, 5e-07] | 2e-07 |
| I_tail | 2.181e-05 | [1e-06, 0.00025] | 5e-05 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.015930 |
| bw_deficit | 2.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 0.5 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.000000 |
| power_ratio | 0.3 | `realized.power/max(targets.power.max, 1e-9)` | 0.026169 |
| cmrr_deficit | 0.2 | `relu(targets.cmrr.min - realized.cmrr)/max(targets` | 0.000000 |
| psrr_deficit | 0.2 | `relu(targets.psrr_plus.min - realized.psrr_plus)/m` | 0.000000 |

## Correction Factors
- gm_factor: 0.78
- gds_factor: 0.85
- c_factor: 1.0
- description: iter_001: gm_factor=0.78 (UGBW<1%), gds_factor→0.85 (dc_gain +4.4% bias)

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 44.8 | 45 | +0.19 |
| unity_gain_bandwidth | 1.05e+08 | 1.12e+08 | +7.24e+06 |
| phase_margin | 60.5 | 75.8 | +15.2 |
| power | 2.62e-05 | 2.24e-05 | -3.72e-06 |
| cmrr | 22.4 | N/A | N/A |
| psrr_plus | 13.4 | N/A | N/A |


---

# Iteration 006
**Timestamp**: 2026-05-18T23:52:47.829657
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 0.398263

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 12.84 | [10, 22] | 16 |
| M1.L | 3.005e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 12.84 | [10, 22] | 16 |
| M2.L | 3.005e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 5.464 | [5, 12] | 7 |
| M3.L | 3e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 5.464 | [5, 12] | 7 |
| M4.L | 3e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 12.91 | [6, 14] | 9 |
| M5.L | 4.005e-07 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 6.67 | [6, 18] | 10 |
| M6.L | 2.363e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 7.401 | [6, 14] | 9 |
| M7.L | 8.35e-07 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 4.532e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 7.032e-05 | [1e-05, 0.00025] | 8e-05 |
| Cc | 4.895e-13 | [1e-13, 2.5e-12] | 4e-13 |
| Rz | 1.151e+04 | [1e+02, 2e+04] | 1000.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 7.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.000000 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.111017 |
| zero_alignment | 0.08 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 0.287245 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 65.8 | 42.6 | -23.2 |
| unity_gain_bandwidth | 8.04e+07 | 3.74e+08 | +2.93e+08 |
| phase_margin | 60.9 | 98.6 | +37.7 |
| power | 0.000139 | 0.000132 | -7.01e-06 |
| cmrr | 36.2 | N/A | N/A |
| psrr_plus | 23 | N/A | N/A |
| Cc | 4.9e-13 | N/A | N/A |
| Rz | 1.15e+04 | N/A | N/A |
| zero_target_rz | 2.51e+03 | N/A | N/A |


---

# Iteration 007
**Timestamp**: 2026-05-18T23:54:50.635050
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 0.335457

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 10.95 | [10, 22] | 16 |
| M1.L | 3e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 10.95 | [10, 22] | 16 |
| M2.L | 3e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 6.15 | [5, 12] | 7 |
| M3.L | 3e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 6.15 | [5, 12] | 7 |
| M4.L | 3e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 11.98 | [6, 14] | 9 |
| M5.L | 1.066e-06 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 14.25 | [6, 18] | 10 |
| M6.L | 2.231e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 7.58 | [6, 14] | 9 |
| M7.L | 1.39e-06 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 5.825e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 5.763e-05 | [1e-05, 0.00025] | 8e-05 |
| Cc | 5.375e-13 | [1e-13, 2.5e-12] | 4e-13 |
| Rz | 1.298e+04 | [1e+02, 2e+04] | 1000.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 7.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.000000 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.111240 |
| zero_alignment | 0.08 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 0.224217 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 63.7 | 43.9 | -19.8 |
| unity_gain_bandwidth | 8.03e+07 | 5.19e+08 | +4.38e+08 |
| phase_margin | 60.1 | 83.3 | +23.2 |
| power | 0.000139 | 0.000116 | -2.33e-05 |
| cmrr | 35 | N/A | N/A |
| psrr_plus | 22.3 | N/A | N/A |
| Cc | 5.38e-13 | N/A | N/A |
| Rz | 1.3e+04 | N/A | N/A |
| zero_target_rz | 3.41e+03 | N/A | N/A |


---

# Iteration 008
**Timestamp**: 2026-05-18T23:57:29.292052
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 0.550463

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 14.55 | [10, 22] | 16 |
| M1.L | 1.803e-06 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 14.55 | [10, 22] | 16 |
| M2.L | 1.803e-06 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 8.333 | [5, 12] | 7 |
| M3.L | 4.671e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 8.333 | [5, 12] | 7 |
| M4.L | 4.671e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 8.829 | [6, 14] | 9 |
| M5.L | 9.365e-07 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 9.344 | [6, 18] | 10 |
| M6.L | 2.158e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 9.088 | [6, 14] | 9 |
| M7.L | 4.746e-07 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 7.681e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 9.138e-05 | [1e-05, 0.00025] | 8e-05 |
| Cc | 9.048e-13 | [1e-13, 2.5e-12] | 4e-13 |
| Rz | 9527 | [1e+02, 2e+04] | 1000.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 7.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.000000 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.161459 |
| zero_alignment | 0.08 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 0.389004 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 72.1 | 76 | +3.91 |
| unity_gain_bandwidth | 8.35e+07 | 9.01e+08 | +8.17e+08 |
| phase_margin | 60.8 | 141 | +80.2 |
| power | 0.000202 | 0.000184 | -1.77e-05 |
| cmrr | 39.7 | N/A | N/A |
| psrr_plus | 25.2 | N/A | N/A |
| Cc | 9.05e-13 | N/A | N/A |
| Rz | 9.53e+03 | N/A | N/A |
| zero_target_rz | 1.63e+03 | N/A | N/A |


---

# Iteration 009
**Timestamp**: 2026-05-18T23:58:46.332491
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 0.554682

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.26 | [10, 22] | 16 |
| M1.L | 3.613e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 15.26 | [10, 22] | 16 |
| M2.L | 3.613e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 9.011 | [5, 12] | 7 |
| M3.L | 7.912e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 9.011 | [5, 12] | 7 |
| M4.L | 7.912e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 12.16 | [6, 14] | 9 |
| M5.L | 4.72e-07 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 8.208 | [6, 18] | 10 |
| M6.L | 2.163e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 11.67 | [6, 14] | 9 |
| M7.L | 9.416e-07 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 4.282e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 7.137e-05 | [1e-05, 0.00025] | 8e-05 |
| Cc | 5.448e-13 | [1e-13, 2.5e-12] | 4e-13 |
| Rz | 1.271e+04 | [1e+02, 2e+04] | 1000.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 7.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.000000 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.109619 |
| zero_alignment | 0.08 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 0.445063 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 70.8 | 74.6 | +3.82 |
| unity_gain_bandwidth | 8.11e+07 | 5.33e+08 | +4.52e+08 |
| phase_margin | 60.8 | 49.7 | -11.1 |
| power | 0.000137 | 0.00013 | -6.57e-06 |
| cmrr | 38.9 | N/A | N/A |
| psrr_plus | 24.8 | N/A | N/A |
| Cc | 5.45e-13 | N/A | N/A |
| Rz | 1.27e+04 | N/A | N/A |
| zero_target_rz | 1.94e+03 | N/A | N/A |


---

# Iteration 010
**Timestamp**: 2026-05-19T00:00:19.945891
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 2.638952

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.25 | [10, 22] | 16 |
| M1.L | 3.642e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 15.25 | [10, 22] | 16 |
| M2.L | 3.642e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 8.512 | [5, 12] | 7 |
| M3.L | 3e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 8.512 | [5, 12] | 7 |
| M4.L | 3e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 12.02 | [6, 14] | 9 |
| M5.L | 2.254e-07 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 9.371 | [6, 18] | 10 |
| M6.L | 2.252e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 6.03 | [6, 14] | 9 |
| M7.L | 7.895e-07 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 7.645e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 0.000118 | [1e-05, 0.00025] | 8e-05 |
| Cc | 1.017e-12 | [8e-13, 3e-12] | 1e-12 |
| Rz | 8629 | [1e+02, 1e+04] | 1500.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.091375 |
| pm_deficit | 9.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.814324 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.186670 |
| zero_alignment | 0.25 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 1.546582 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 68.7 | 71.5 | +2.78 |
| unity_gain_bandwidth | 7.76e+07 | 1.08e+09 | +1.01e+09 |
| phase_margin | 59.1 | 12.1 | -47 |
| power | 0.000233 | 0.00022 | -1.34e-05 |
| cmrr | 37.8 | N/A | N/A |
| psrr_plus | 24 | N/A | N/A |
| Cc | 1.02e-12 | N/A | N/A |
| Rz | 8.63e+03 | N/A | N/A |
| zero_target_rz | 1.2e+03 | N/A | N/A |


---

# Iteration 011
**Timestamp**: 2026-05-19T00:01:53.987373
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 5.205146

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 14.82 | [10, 22] | 16 |
| M1.L | 3.179e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 14.82 | [10, 22] | 16 |
| M2.L | 3.179e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 8.453 | [5, 12] | 7 |
| M3.L | 3e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 8.453 | [5, 12] | 7 |
| M4.L | 3e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 14 | [6, 14] | 9 |
| M5.L | 9.865e-07 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 6 | [6, 18] | 10 |
| M6.L | 2.892e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 6.701 | [6, 14] | 9 |
| M7.L | 1.181e-06 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 7.915e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 8.222e-05 | [1e-05, 0.00025] | 8e-05 |
| Cc | 4.531e-12 | [2e-12, 8e-12] | 3e-12 |
| Rz | 4926 | [1e+02, 5e+03] | 1500.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 2.343260 |
| pm_deficit | 9.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.804556 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.154918 |
| zero_alignment | 1.0 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 1.902412 |
| bw_excess | 2.0 | `relu(realized.unity_gain_bandwidth - targets.unity` | 0.000000 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 71.1 | 74 | +2.92 |
| unity_gain_bandwidth | 1.75e+07 | 6.9e+08 | +6.73e+08 |
| phase_margin | 59.2 | 54.4 | -4.76 |
| power | 0.000194 | 0.000186 | -8.07e-06 |
| cmrr | 39.1 | N/A | N/A |
| psrr_plus | 24.9 | N/A | N/A |
| Cc | 4.53e-12 | N/A | N/A |
| Rz | 4.93e+03 | N/A | N/A |
| zero_target_rz | 1.7e+03 | N/A | N/A |


---

# Iteration 012
**Timestamp**: 2026-05-19T00:04:07.195422
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 8.788844

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.7 | [10, 22] | 16 |
| M1.L | 1.965e-06 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 15.7 | [10, 22] | 16 |
| M2.L | 1.965e-06 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 9.448 | [5, 12] | 7 |
| M3.L | 1.848e-06 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 9.448 | [5, 12] | 7 |
| M4.L | 1.848e-06 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 14 | [6, 14] | 9 |
| M5.L | 1.34e-06 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 17.55 | [6, 18] | 10 |
| M6.L | 1.563e-06 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 9.481 | [6, 14] | 9 |
| M7.L | 1.27e-06 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 8e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 8.852e-05 | [1e-05, 0.00025] | 8e-05 |
| Cc | 2e-12 | [2e-12, 8e-12] | 3e-12 |
| Rz | 4726 | [1e+02, 5e+03] | 1500.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 1.406629 |
| pm_deficit | 9.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 4.862699 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.161777 |
| zero_alignment | 1.0 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 2.357738 |
| bw_excess | 2.0 | `relu(realized.unity_gain_bandwidth - targets.unity` | 0.000000 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 91.1 | 93.4 | +2.25 |
| unity_gain_bandwidth | 4.25e+07 | 3.75e+07 | -5.01e+06 |
| phase_margin | 29.9 | 50.4 | +20.6 |
| power | 0.000202 | 0.000193 | -9.34e-06 |
| cmrr | 50.1 | N/A | N/A |
| psrr_plus | 31.9 | N/A | N/A |
| Cc | 2e-12 | N/A | N/A |
| Rz | 4.73e+03 | N/A | N/A |
| zero_target_rz | 1.41e+03 | N/A | N/A |


---

# Iteration 013
**Timestamp**: 2026-05-19T00:05:09.053543
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 1.267900

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.24 | [10, 22] | 16 |
| M1.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 15.24 | [10, 22] | 16 |
| M2.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 10.51 | [5, 12] | 7 |
| M3.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 10.51 | [5, 12] | 7 |
| M4.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 14 | [6, 14] | 9 |
| M5.L | 6.842e-07 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 14.69 | [6, 18] | 10 |
| M6.L | 2e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 11.02 | [6, 14] | 9 |
| M7.L | 4.428e-07 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 7.996e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 0.0001316 | [1e-05, 0.00025] | 8e-05 |
| Cc | 1.028e-12 | [1e-13, 2.5e-12] | 4e-13 |
| Rz | 1.133e+04 | [1e+02, 2e+04] | 1000.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 7.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.145023 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.203082 |
| zero_alignment | 0.08 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 0.919795 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 70.3 | 73.9 | +3.68 |
| unity_gain_bandwidth | 8.02e+07 | 9.79e+08 | +8.99e+08 |
| phase_margin | 58.8 | 166 | +108 |
| power | 0.000254 | 0.000244 | -1.01e-05 |
| cmrr | 38.6 | N/A | N/A |
| psrr_plus | 24.6 | N/A | N/A |
| Cc | 1.03e-12 | N/A | N/A |
| Rz | 1.13e+04 | N/A | N/A |
| zero_target_rz | 907 | N/A | N/A |


---

# Iteration 014
**Timestamp**: 2026-05-19T00:06:08.531468
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 0.905870

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 13.89 | [10, 22] | 16 |
| M1.L | 3e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 13.89 | [10, 22] | 16 |
| M2.L | 3e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 7.129 | [5, 12] | 7 |
| M3.L | 6.209e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 7.129 | [5, 12] | 7 |
| M4.L | 6.209e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 13.81 | [6, 18] | 12 |
| M5.L | 1.015e-06 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 14.61 | [6, 18] | 10 |
| M6.L | 2.259e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 9.083 | [6, 14] | 9 |
| M7.L | 8.492e-07 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 7.989e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 0.0001412 | [1e-05, 0.00025] | 8e-05 |
| Cc | 9.2e-13 | [1e-13, 2.5e-12] | 4e-13 |
| Rz | 1.171e+04 | [1e+02, 2e+04] | 1000.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 7.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.009549 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.212241 |
| zero_alignment | 0.08 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 0.684080 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 68.5 | 71.5 | +2.91 |
| unity_gain_bandwidth | 8.16e+07 | 4.37e+08 | +3.55e+08 |
| phase_margin | 59.9 | 35.6 | -24.4 |
| power | 0.000265 | 0.000255 | -9.86e-06 |
| cmrr | 37.7 | N/A | N/A |
| psrr_plus | 24 | N/A | N/A |
| Cc | 9.2e-13 | N/A | N/A |
| Rz | 1.17e+04 | N/A | N/A |
| zero_target_rz | 1.23e+03 | N/A | N/A |


---

# Iteration 015
**Timestamp**: 2026-05-19T00:13:57.537763
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 1.267900

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.24 | [10, 22] | 16 |
| M1.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 15.24 | [10, 22] | 16 |
| M2.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 10.51 | [5, 12] | 7 |
| M3.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 10.51 | [5, 12] | 7 |
| M4.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 14 | [6, 14] | 9 |
| M5.L | 6.842e-07 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 14.69 | [6, 18] | 10 |
| M6.L | 2e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 11.02 | [6, 14] | 9 |
| M7.L | 4.428e-07 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 7.996e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 0.0001316 | [1e-05, 0.00025] | 8e-05 |
| Cc | 1e-13 | [1e-13, 2.5e-12] | 4e-13 |
| Rz | 1.133e+04 | [1e+02, 2e+04] | 1000.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 7.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.145023 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.203082 |
| zero_alignment | 0.08 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 0.919795 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 70.3 | 73.9 | +3.68 |
| unity_gain_bandwidth | 8.02e+07 | 9.79e+08 | +8.99e+08 |
| phase_margin | 58.8 | 166 | +108 |
| power | 0.000254 | 0.000244 | -1.01e-05 |
| cmrr | 38.6 | N/A | N/A |
| psrr_plus | 24.6 | N/A | N/A |
| Cc | 1e-13 | N/A | N/A |
| Rz | 1.13e+04 | N/A | N/A |
| zero_target_rz | 907 | N/A | N/A |


---

# Iteration 016
**Timestamp**: 2026-05-19T00:15:19.150251
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 1.267900

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.24 | [10, 22] | 16 |
| M1.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 15.24 | [10, 22] | 16 |
| M2.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 10.51 | [5, 12] | 7 |
| M3.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 10.51 | [5, 12] | 7 |
| M4.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 14 | [6, 14] | 9 |
| M5.L | 6.842e-07 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 14.69 | [6, 18] | 10 |
| M6.L | 2e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 11.02 | [6, 14] | 9 |
| M7.L | 4.428e-07 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 7.996e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 0.0001316 | [1e-05, 0.00025] | 8e-05 |
| Cc | 1e-13 | [1e-13, 2.5e-12] | 4e-13 |
| Rz | 1.133e+04 | [1e+02, 2e+04] | 1000.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 7.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.145023 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.203082 |
| zero_alignment | 0.08 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 0.919795 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 70.3 | 73.9 | +3.68 |
| unity_gain_bandwidth | 8.02e+07 | 9.79e+08 | +8.99e+08 |
| phase_margin | 58.8 | 166 | +108 |
| power | 0.000254 | 0.000244 | -1.01e-05 |
| cmrr | 38.6 | N/A | N/A |
| psrr_plus | 24.6 | N/A | N/A |
| Cc | 1e-13 | N/A | N/A |
| Rz | 1.13e+04 | N/A | N/A |
| zero_target_rz | 907 | N/A | N/A |


---

# Iteration 017
**Timestamp**: 2026-05-19T00:16:51.342364
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 1.267900

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.24 | [10, 22] | 16 |
| M1.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 15.24 | [10, 22] | 16 |
| M2.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 10.51 | [5, 12] | 7 |
| M3.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 10.51 | [5, 12] | 7 |
| M4.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 14 | [6, 14] | 9 |
| M5.L | 6.842e-07 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 14.69 | [6, 18] | 10 |
| M6.L | 2e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 11.02 | [6, 14] | 9 |
| M7.L | 4.428e-07 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 7.996e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 0.0001316 | [1e-05, 0.00025] | 8e-05 |
| Cc | 1e-13 | [1e-13, 2.5e-12] | 4e-13 |
| Rz | 1.133e+04 | [1e+02, 2e+04] | 1000.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 7.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.145023 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.203082 |
| zero_alignment | 0.08 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 0.919795 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 70.3 | 73.9 | +3.68 |
| unity_gain_bandwidth | 8.02e+07 | 9.79e+08 | +8.99e+08 |
| phase_margin | 58.8 | 166 | +108 |
| power | 0.000254 | 0.000244 | -1.01e-05 |
| cmrr | 38.6 | N/A | N/A |
| psrr_plus | 24.6 | N/A | N/A |
| Cc | 1e-13 | N/A | N/A |
| Rz | 1.13e+04 | N/A | N/A |
| zero_target_rz | 907 | N/A | N/A |


---

# Iteration 018
**Timestamp**: 2026-05-19T00:19:51.788084
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 1.267900

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.24 | [10, 22] | 16 |
| M1.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 15.24 | [10, 22] | 16 |
| M2.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 10.51 | [5, 12] | 7 |
| M3.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 10.51 | [5, 12] | 7 |
| M4.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 14 | [6, 14] | 9 |
| M5.L | 6.842e-07 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 14.69 | [6, 18] | 10 |
| M6.L | 2e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 11.02 | [6, 14] | 9 |
| M7.L | 4.428e-07 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 7.996e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 0.0001316 | [1e-05, 0.00025] | 8e-05 |
| Cc | 1e-13 | [1e-13, 2.5e-12] | 4e-13 |
| Rz | 1.133e+04 | [1e+02, 2e+04] | 1000.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 7.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.145023 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.203082 |
| zero_alignment | 0.08 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 0.919795 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 70.3 | 73.9 | +3.68 |
| unity_gain_bandwidth | 8.02e+07 | 9.79e+08 | +8.99e+08 |
| phase_margin | 58.8 | 166 | +108 |
| power | 0.000254 | 0.000244 | -1.01e-05 |
| cmrr | 38.6 | N/A | N/A |
| psrr_plus | 24.6 | N/A | N/A |
| Cc | 1e-13 | N/A | N/A |
| Rz | 1.13e+04 | N/A | N/A |
| zero_target_rz | 907 | N/A | N/A |


---

# Iteration 019
**Timestamp**: 2026-05-19T00:30:14.480763
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 1.267900

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.24 | [10, 22] | 16 |
| M1.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 15.24 | [10, 22] | 16 |
| M2.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 10.51 | [5, 12] | 7 |
| M3.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 10.51 | [5, 12] | 7 |
| M4.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 14 | [6, 14] | 9 |
| M5.L | 6.842e-07 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 14.69 | [6, 18] | 10 |
| M6.L | 2e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 11.02 | [6, 14] | 9 |
| M7.L | 4.428e-07 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 7.996e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 0.0001316 | [1e-05, 0.00025] | 8e-05 |
| Cc | 1e-13 | [1e-13, 2.5e-12] | 4e-13 |
| Rz | 1541 | [1e+02, 2e+04] | 1000.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 7.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.145023 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.203082 |
| zero_alignment | 0.08 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 0.919795 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 70.3 | 73.9 | +3.68 |
| unity_gain_bandwidth | 8.02e+07 | 5.17e+08 | +4.37e+08 |
| phase_margin | 58.8 | 23.3 | -35.5 |
| power | 0.000254 | 0.000244 | -1.01e-05 |
| cmrr | 38.6 | N/A | N/A |
| psrr_plus | 24.6 | N/A | N/A |
| Cc | 1e-13 | N/A | N/A |
| Rz | 1.54e+03 | N/A | N/A |
| zero_target_rz | 907 | N/A | N/A |


---

# Iteration 020
**Timestamp**: 2026-05-19T00:31:55.967312
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 1.267900

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.24 | [10, 22] | 16 |
| M1.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 15.24 | [10, 22] | 16 |
| M2.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 10.51 | [5, 12] | 7 |
| M3.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 10.51 | [5, 12] | 7 |
| M4.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 14 | [6, 14] | 9 |
| M5.L | 6.842e-07 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 14.69 | [6, 18] | 10 |
| M6.L | 2e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 11.02 | [6, 14] | 9 |
| M7.L | 4.428e-07 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 7.996e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 0.0001316 | [1e-05, 0.00025] | 8e-05 |
| Cc | 1e-13 | [1e-13, 2.5e-12] | 4e-13 |
| Rz | 1541 | [1e+02, 2e+04] | 1000.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 7.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.145023 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.203082 |
| zero_alignment | 0.08 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 0.919795 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 70.3 | 73.9 | +3.68 |
| unity_gain_bandwidth | 8.02e+07 | 5.17e+08 | +4.37e+08 |
| phase_margin | 58.8 | 23.3 | -35.5 |
| power | 0.000254 | 0.000244 | -1.01e-05 |
| cmrr | 38.6 | N/A | N/A |
| psrr_plus | 24.6 | N/A | N/A |
| Cc | 1e-13 | N/A | N/A |
| Rz | 1.54e+03 | N/A | N/A |
| zero_target_rz | 907 | N/A | N/A |


---

# Iteration 021
**Timestamp**: 2026-05-19T00:34:09.145765
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 1.267900

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.24 | [10, 22] | 16 |
| M1.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 15.24 | [10, 22] | 16 |
| M2.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 10.51 | [5, 12] | 7 |
| M3.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 10.51 | [5, 12] | 7 |
| M4.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 14 | [6, 14] | 9 |
| M5.L | 6.842e-07 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 14.69 | [6, 18] | 10 |
| M6.L | 2e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 11.02 | [6, 14] | 9 |
| M7.L | 4.428e-07 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 7.996e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 0.0001316 | [1e-05, 0.00025] | 8e-05 |
| Cc | 1.028e-12 | [1e-13, 2.5e-12] | 4e-13 |
| Rz | 1541 | [1e+02, 2e+04] | 1000.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 7.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.145023 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.203082 |
| zero_alignment | 0.08 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 0.919795 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 70.3 | 73.9 | +3.68 |
| unity_gain_bandwidth | 8.02e+07 | 9.12e+07 | +1.11e+07 |
| phase_margin | 58.8 | 99.1 | +40.4 |
| power | 0.000254 | 0.000244 | -1.01e-05 |
| cmrr | 38.6 | N/A | N/A |
| psrr_plus | 24.6 | N/A | N/A |
| Cc | 1.03e-12 | N/A | N/A |
| Rz | 1.54e+03 | N/A | N/A |
| zero_target_rz | 907 | N/A | N/A |


---

# Iteration 022
**Timestamp**: 2026-05-19T00:37:48.849415
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 1.267900

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 15.24 | [10, 22] | 16 |
| M1.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 15.24 | [10, 22] | 16 |
| M2.L | 9.185e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 10.51 | [5, 12] | 7 |
| M3.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 10.51 | [5, 12] | 7 |
| M4.L | 4.27e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 14 | [6, 14] | 9 |
| M5.L | 6.842e-07 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 14.69 | [6, 18] | 10 |
| M6.L | 2e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 11.02 | [6, 14] | 9 |
| M7.L | 4.428e-07 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 7.996e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 0.0001316 | [1e-05, 0.00025] | 8e-05 |
| Cc | 1.028e-12 | [1e-13, 2.5e-12] | 4e-13 |
| Rz | 1541 | [1e+02, 2e+04] | 1000.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 0.000000 |
| pm_deficit | 7.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.145023 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.203082 |
| zero_alignment | 0.08 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 0.919795 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 70.3 | 74.9 | +4.59 |
| unity_gain_bandwidth | 8.02e+07 | 1.11e+08 | +3.11e+07 |
| phase_margin | 58.8 | 99 | +40.3 |
| power | 0.000254 | 0.000246 | -8.11e-06 |
| cmrr | 38.6 | N/A | N/A |
| psrr_plus | 24.6 | N/A | N/A |
| Cc | 1.03e-12 | N/A | N/A |
| Rz | 1.54e+03 | N/A | N/A |
| zero_target_rz | 907 | N/A | N/A |


---

# Iteration 023
**Timestamp**: 2026-05-19T00:44:02.245309
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 3.244591

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 14.55 | [10, 22] | 16 |
| M1.L | 3.001e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 14.55 | [10, 22] | 16 |
| M2.L | 3.001e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 5.89 | [5, 12] | 7 |
| M3.L | 3e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 5.89 | [5, 12] | 7 |
| M4.L | 3e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 12.97 | [6, 14] | 9 |
| M5.L | 2.001e-07 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 11.29 | [6, 18] | 10 |
| M6.L | 2.259e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 6.042 | [6, 14] | 9 |
| M7.L | 7.579e-07 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 7.98e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 0.0001464 | [1e-05, 0.00025] | 8e-05 |
| Cc | 1e-13 | [1e-13, 2.5e-12] | 4e-13 |
| Rz | 2380 | [1e+02, 2e+04] | 1000.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 2.547814 |
| pm_deficit | 7.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.007036 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.217167 |
| zero_alignment | 0.08 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 0.472574 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 65.1 | 68.8 | +3.77 |
| unity_gain_bandwidth | 7.54e+07 | 5.78e+08 | +5.03e+08 |
| phase_margin | 59.9 | 46.4 | -13.6 |
| power | 0.000271 | 0.000252 | -1.98e-05 |
| cmrr | 35.8 | N/A | N/A |
| psrr_plus | 22.8 | N/A | N/A |
| Cc | 1e-13 | N/A | N/A |
| Rz | 2.38e+03 | N/A | N/A |
| zero_target_rz | 1.4e+03 | N/A | N/A |


---

# Iteration 024
**Timestamp**: 2026-05-19T00:50:38.638962
**Design**: two_stage_ota
**Process**: PTM_130nm (0.13um)
**Best loss**: 3.244591

## Design Variables
| Variable | Value | Range | Initial |
|----------|-------|-------|---------|
| M1.gm_id | 14.55 | [10, 22] | 16 |
| M1.L | 3.001e-07 | [3e-07, 2e-06] | 1e-06 |
| M2.gm_id | 14.55 | [10, 22] | 16 |
| M2.L | 3.001e-07 | [3e-07, 2e-06] | 1e-06 |
| M3.gm_id | 5.89 | [5, 12] | 7 |
| M3.L | 3e-07 | [3e-07, 2e-06] | 1e-06 |
| M4.gm_id | 5.89 | [5, 12] | 7 |
| M4.L | 3e-07 | [3e-07, 2e-06] | 1e-06 |
| M5.gm_id | 12.97 | [6, 14] | 9 |
| M5.L | 2.001e-07 | [2e-07, 1.5e-06] | 5e-07 |
| M6.gm_id | 11.29 | [6, 18] | 10 |
| M6.L | 2.259e-07 | [2e-07, 2e-06] | 8e-07 |
| M7.gm_id | 6.042 | [6, 14] | 9 |
| M7.L | 7.579e-07 | [2e-07, 1.5e-06] | 5e-07 |
| I_tail | 7.98e-05 | [5e-06, 8e-05] | 2e-05 |
| I_stage2 | 0.0001464 | [1e-05, 0.00025] | 8e-05 |
| Cc | 5e-13 | [1e-13, 2.5e-12] | 4e-13 |
| Rz | 3500 | [1e+02, 2e+04] | 1000.0 |

## Loss Terms
| ID | Weight | Formula | Contribution |
|----|--------|---------|-------------|
| gain_deficit | 4.0 | `relu(targets.dc_gain.min - realized.dc_gain)/max(t` | 0.000000 |
| bw_deficit | 3.0 | `relu(targets.unity_gain_bandwidth.min - realized.u` | 2.547814 |
| pm_deficit | 7.0 | `relu(targets.phase_margin.min - realized.phase_mar` | 0.007036 |
| power_ratio | 0.4 | `realized.power/max(targets.power.max, 1e-9)` | 0.217167 |
| zero_alignment | 0.08 | `abs(realized.Rz - realized.zero_target_rz)/max(rea` | 0.472574 |

## Correction Factors
- gm_factor: 0.85
- gds_factor: 1.0
- c_factor: 1.15
- description: Conservative two-stage estimator; ngspice remains final authority

## Performance
| Metric | Optimizer | ngspice | Δ |
|--------|-----------|---------|---|
| dc_gain | 65.1 | 68.8 | +3.77 |
| unity_gain_bandwidth | 7.54e+07 | 5.32e+08 | +4.57e+08 |
| phase_margin | 59.9 | 101 | +40.9 |
| power | 0.000271 | 0.000252 | -1.98e-05 |
| cmrr | 35.8 | N/A | N/A |
| psrr_plus | 22.8 | N/A | N/A |
| Cc | 5e-13 | N/A | N/A |
| Rz | 3.5e+03 | N/A | N/A |
| zero_target_rz | 1.4e+03 | N/A | N/A |


---


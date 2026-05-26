# Circuit Input Library

Circuit schemas are grouped by circuit family so profile-specific rules, constraints, objectives, and simulator checks remain easy to find.

```text
inputs/
  ota/
    five_transistor/
    two_stage_miller/
    source_follower_boosted/
  comparator/
    strongarm/
    double_tail/
    sense_amplifier/
```

Use these schemas with `python main.py --topology yaml --schema <path/to/schema.yaml>`.

## OTA Schemas

- `ota/five_transistor/five_transistor_ota.yaml`: single-stage five-transistor OTA baseline.
- `ota/two_stage_miller/two_stage_miller_ota.yaml`: two-stage Miller OTA with input OTA, second-stage inverter, `Cc`, `Rz`, tail bias mirror, and output-stage bias mirror. In IHP SG13G2 netlists, `Cc` is emitted as a `cap_cmim` MIM capacitor.
- `ota/source_follower_boosted/source_follower_boosted_ota.yaml`: source-follower-regulated OTA with boosted output resistance and no explicit `Rz-Cc` compensation network.

## Comparator Schemas

- `comparator/strongarm/strongarm_v1.yaml`: StrongARM-style dynamic latch comparator.
- `comparator/double_tail/double_tail_v1.yaml`: double-tail dynamic comparator example.
- `comparator/sense_amplifier/sense_amplifier_v1.yaml`: sense-amplifier-style comparator example.

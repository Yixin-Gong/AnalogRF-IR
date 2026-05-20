# OTA Schemas

This folder contains OTA and op-amp input schemas.

- `five_transistor/five_transistor_ota.yaml` is the compact single-stage baseline.
- `two_stage_miller/two_stage_miller_ota.yaml` is the more complex two-stage Miller OTA path. It models the input five-transistor OTA, second-stage inverter, compensation capacitor `Cc`, zero-setting resistor `Rz`, tail bias mirror, and output-stage bias mirror.
- `source_follower_boosted/source_follower_boosted_ota.yaml` is a source-follower-regulated OTA inspired by DOI `10.1109/JSSC.1969.1049994`. It has no explicit `Rz-Cc` compensation; the source follower holds the small-signal voltage across the regulated load device nearly constant to boost output resistance, with reduced output common-mode headroom as the main trade-off.

Add future OTA families here, for example folded-cascode, telescopic, gain-boosted, rail-to-rail, or fully differential OTAs. Keep device roles, symmetry labels, compensation elements, design variables, targets, and evaluations explicit in the YAML so the IR profile can select the right checks and objectives.

# Comparator Schemas

This folder contains dynamic comparator input schemas.

- `strongarm/strongarm_v1.yaml` is the main StrongARM-style latch comparator example.
- `double_tail/double_tail_v1.yaml` is a double-tail dynamic comparator example.
- `sense_amplifier/sense_amplifier_v1.yaml` is a sense-amplifier-style comparator example.

Comparator schemas should include clock/load context and the metric targets needed by the comparator IR profile: delay, regeneration/reset time, offset, noise, kickback, energy, PDP/EDP, input capacitance, output swing, ICMR, metastability margin, sample rate, area, and average power where applicable.

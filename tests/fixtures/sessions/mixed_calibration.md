# mixed_calibration

Baseline session for the bootstrap and recalibration corpus. The agent
investigates a failing test, reads a handful of related files, runs one
grep, applies one targeted edit, and verifies the result. Read:edit
ratio is 8.0 across ten classified tool calls so the metric exits
warming-up cleanly. No stop phrases, no self-correction markers, no
parser drift. This is the "median" fixture the recalibration script
draws its centre-of-distribution from.

Expected severities:

- `read_edit_ratio`: OK
- `stop_phrase`: OK
- `reasoning_loop`: OK
- `parse_health`: OK

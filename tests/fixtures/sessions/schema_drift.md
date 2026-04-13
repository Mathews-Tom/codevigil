# schema_drift

Forty-five well-formed assistant text events followed by ten lines of an
assistant shape with the required `message` object stripped out. Total
lines fifty-five, parse confidence 0.82, which is well below the 0.9
critical floor used by `parse_health` once its fifty-line window is full.
The fixture is intentionally larger than the others because the drift
collector cannot judge confidence until it has seen at least fifty raw
lines.

Expected severities:

- `read_edit_ratio`: OK (warming up, no tool calls)
- `stop_phrase`: OK
- `reasoning_loop`: OK
- `parse_health`: CRITICAL

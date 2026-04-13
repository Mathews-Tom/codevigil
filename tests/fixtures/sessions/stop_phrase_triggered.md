# stop_phrase_triggered

Four assistant turns each containing a default stop phrase: "should I
continue", "good stopping point", "would you like me to", and a final
turn that hits both "out of scope" and "future work". Five total hits is
well over the critical threshold of three. Read:edit ratio is dominated
by a lone read so the ratio collector stays in warming-up OK.

Expected severities:

- `read_edit_ratio`: OK (warming up)
- `stop_phrase`: CRITICAL
- `reasoning_loop`: OK
- `parse_health`: OK

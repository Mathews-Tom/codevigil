# degraded_re

The agent jumps straight to mutation: only two reads, then a long sequence
of edits and writes across multiple files. Read:edit ratio lands at 0.22
(2 reads / 9 mutations), well below the 2.0 critical threshold. Stop
phrases and reasoning-loop signals are absent so this fixture isolates
the read-edit collector.

Expected severities:

- `read_edit_ratio`: CRITICAL
- `stop_phrase`: OK
- `reasoning_loop`: OK
- `parse_health`: OK

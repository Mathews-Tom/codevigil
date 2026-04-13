# Getting started

A first-run walkthrough. Assumes you have already installed codevigil via `uv tool install codevigil` and have at least one Claude Code session under `~/.claude/projects`.

## Step 1: Confirm the install

```bash
codevigil --version
```

If this prints `codevigil 0.1.0` (or whatever you installed), you are ready. Otherwise see [installation.md](installation.md).

## Step 2: Look at the resolved configuration

```bash
codevigil config check
```

This prints every config key codevigil knows about, alongside the **source** that resolved each value. On a fresh install with no config file, env vars, or CLI flags, every value will read `(default)`:

```text
codevigil config check
  bootstrap.sessions = 10  (default)
  bootstrap.state_path = '~/.local/state/codevigil/bootstrap.json'  (default)
  collectors.enabled = ['read_edit_ratio', 'stop_phrase', 'reasoning_loop']  (default)
  collectors.read_edit_ratio.warn_threshold = 4.0  (default)
  collectors.read_edit_ratio.critical_threshold = 2.0  (default)
  ...
```

Any time you change a TOML key, an environment variable, or pass a CLI flag, the corresponding row will switch from `(default)` to `(file:...)`, `(env:...)`, or `(cli:...)`. This is how codevigil tells you exactly where every effective value came from.

## Step 3: Run watch mode

```bash
codevigil watch
```

codevigil will scan `~/.claude/projects` for session JSONL files, classify them as `ACTIVE` / `STALE` / `EVICTED` based on their last event timestamp, and start rendering one frame per second. Active sessions appear at the top.

A typical frame (after a few ticks, with the session store warm):

```text
codevigil [experimental thresholds] | sessions=3 crit=0 warn=1 ok=2 projects=2 updated=2026-04-14T10:22:00 | parse_confidence: 1.00
session: a3f7c2d | project: my-project | 2m 34s ACTIVE
──────────────────────────────────────────────────────────────
  read_edit_ratio    5.2   OK    [R:E 5.2 | research:mut 7.1] [↗3.1→4.2→5.2] [p68 of your baseline]
  stop_phrase        0     OK    [0 hits]
  reasoning_loop     6.4   OK    [6.4/1K tool calls | burst: 2] [↘8.1→7.2→6.4] [n/a]
──────────────────────────────────────────────────────────────
```

### Reading the fleet summary line

The first line of every frame summarises the entire fleet:

| Field                           | Meaning                                                                                                                                             |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `codevigil`                     | The tool name.                                                                                                                                      |
| `[experimental thresholds]`     | Shown while any enabled collector still has `experimental = true`. Disappears after bootstrap completes or you flip the flag in config.             |
| `sessions=N crit=C warn=W ok=O` | Total sessions in the active set and their worst-severity breakdown.                                                                                |
| `projects=P`                    | Number of distinct projects represented in the active set.                                                                                          |
| `updated=TS`                    | ISO timestamp of the last tick. Useful for confirming the watcher is running when no session activity is visible.                                   |
| `parse_confidence: 1.00`        | Fraction of input lines successfully parsed as events in the current 50-line drift window. Drops below `0.9` → CRITICAL banner from `parse_health`. |

### Reading the session line

| Field                          | Meaning                                                                                                                                           |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `session: a3f7c2d`             | Short disambiguated label for the session id (unique within the active set).                                                                      |
| `project: my-project`          | Resolved via `~/.config/codevigil/projects.toml` → first observed `cwd` in a SYSTEM event → raw hash prefix fallback.                             |
| `2m 34s`                       | Wall-clock duration from first event to last event in the session.                                                                                |
| `ACTIVE` / `STALE` / `EVICTED` | Lifecycle state. ACTIVE = recent activity. STALE = silent ≥ 5 min, collector state preserved. EVICTED = silent ≥ 35 min, collector state cleared. |

Sessions are sorted by worst severity first (CRITICAL → WARN → OK), then by most-recently-active. The sort is stable across ticks so you don't have to track movement to spot changes.

### Reading metric lines

```text
  read_edit_ratio    5.2   OK    [R:E 5.2 | research:mut 7.1] [↗3.1→4.2→5.2] [p68 of your baseline]
  ─────name─────    val   sev   ────────label────────────────  ──mini-trend──  ─────percentile──────
```

- **Name** identifies the collector.
- **Value** is the primary scalar — every collector emits exactly one scalar per snapshot.
- **Severity** is `OK` / `WARN` / `CRIT` and is colored when output is going to a TTY.
- **Label** is a short human-readable breakdown chosen by the collector. Some labels include secondary metrics like `research:mut`.
- **Mini-trend** (`[↗3.1→4.2→5.2]`) shows the last three snapshot values with a direction arrow. Appears after the second tick for a session. `↗` = rising; `↘` = falling.
- **Percentile anchor** (`[p68 of your baseline]`) shows where the current value falls in your own session history from the `SessionStore`. Shows `[n/a]` when persistence is disabled or too few sessions have been stored to compute a stable percentile.

The full meaning of each metric, threshold behaviour, and what to do when one flips to WARN or CRITICAL is documented in [collectors.md](collectors.md).

### Stopping watch mode

`Ctrl-C` flips an internal shutdown flag, the next tick exits cleanly, the aggregator and renderer are closed, and you see:

```text
codevigil shutdown
```

The exit code is `0`. No buffered log entries are lost.

## Step 4: Generate a batch report

Watch mode is for live monitoring. For after-the-fact analysis use report mode:

```bash
codevigil report ~/.claude/projects
```

This walks the tree, parses every `*.jsonl` file it finds, runs every v0.1 collector against each session, and writes a JSON report to `~/.local/share/codevigil/reports/`. The default format is JSON; pass `--format markdown` to get a human-readable version instead:

```bash
codevigil report ~/.claude/projects --format markdown --from 2026-04-01
```

The `--from` and `--to` flags filter sessions by their first event timestamp. Report output is **deterministic** under identical input — sessions sort by id, metric rows sort by name, no wall-clock timestamps are embedded — so you can diff two reports across time and see exactly which sessions changed.

If any session's parse confidence drops below `0.9` during the run, report exits with status `2`. This is intentional: it signals that the data integrity gate tripped and you should investigate before trusting the derived metrics. See [collectors.md#parse_health](collectors.md#parse_health) for what to look for.

### Cohort trend report

Instead of one block per session, you can aggregate across many sessions grouped by a dimension:

```bash
codevigil report ~/.claude/projects --group-by week
codevigil report ~/.claude/projects --group-by project
```

The output is a Markdown table where rows are dimension values (e.g. ISO weeks like `2026-W14`), columns are metrics, and each cell shows `mean ± stdev (n)`. Cells with fewer than 5 sessions show `n<5` instead of a number. The report also includes `## Methodology` and `## Appendix` sections documenting the source corpus, date range, and behavioral catalog.

### Period-over-period comparison

To compare two time windows directly:

```bash
codevigil report ~/.claude/projects \
  --compare-periods 2026-03-01:2026-03-31,2026-04-01:2026-04-30
```

This filters sessions into the two date ranges, runs Welch's t-test per metric, and produces a signed delta table plus a prose one-liner per metric. Useful for quantifying before/after changes across a deployment, model update, or workflow change.

## Step 5: Browse session history

If you enable persistence, codevigil stores a finalised JSON report for each session at eviction time. Add this to `~/.config/codevigil/config.toml`:

```toml
[storage]
enable_persistence = true
```

On first write, codevigil logs a one-line notice naming the target directory. After that, the `history` subcommand family lets you browse stored sessions without re-parsing the raw JSONL files:

```bash
codevigil history list                                    # all stored sessions
codevigil history list --since 2026-04-01 --severity warn # filter by date and severity
codevigil history list --project my-project               # filter by project
```

To inspect a single session in detail:

```bash
codevigil history SESSION_ID
```

This renders the session header (project, model, duration, event count, parse confidence), a metric table, and any stop-phrase context snippets using rich colored panels.

To compare two sessions side-by-side:

```bash
codevigil history diff SESSION_A SESSION_B
```

To render a tool × severity heatmap:

```bash
codevigil history heatmap SESSION_ID
```

Full flag reference: [cli.md#history](cli.md#history).

## Step 6: Pipe events to `jq`

For ad-hoc analysis that doesn't fit any of the built-in collectors or the history viewer:

```bash
codevigil export session.jsonl | jq '.kind' | sort | uniq -c
```

`codevigil export` parses one or more session files and emits the parsed event stream as NDJSON on stdout. Each line is one event:

```json
{"timestamp": "2026-04-13T10:11:23+00:00", "session_id": "...", "kind": "tool_call", "payload": {"tool_name": "read", "tool_use_id": "...", "input": {...}}}
```

Pipe this through `jq` to compute anything you want — tool-call histograms, file-edit frequency, thinking-block sizes, anything the parser surfaces. The shape is documented in [cli.md#export](cli.md#export).

## Step 7: Personalise the thresholds

The shipped defaults are conservative starting points. They are marked `experimental = true` and the watch header shows `[experimental thresholds]` to remind you they are not calibrated for your specific workflow.

The fastest way to get personalised thresholds is to **just run watch mode**. The aggregator's bootstrap manager will silently observe your first 10 sessions (configurable via `bootstrap.sessions`) with all severities pinned to `OK`, then derive WARN at p80 and CRITICAL at p95 of _your_ local distribution. After bootstrap completes the experimental badge disappears.

If you want to inspect the calibrated thresholds before they take effect, use the offline recalibration helper against a fixture corpus:

```bash
python -m scripts.recalibrate_thresholds --fixtures-dir tests/fixtures/sessions
```

This emits a TOML snippet you can paste into `~/.config/codevigil/config.toml`. See [collectors.md#experimental-thresholds-and-bootstrap](collectors.md#experimental-thresholds-and-bootstrap) for the full mechanism.

## Where to go next

- [cli.md](cli.md) — every flag for every subcommand
- [configuration.md](configuration.md) — every TOML key and env binding
- [collectors.md](collectors.md) — what each metric measures and why
- [privacy.md](privacy.md) — the privacy model in detail
- [design.md](design.md) — architecture, plugin boundaries, error taxonomy

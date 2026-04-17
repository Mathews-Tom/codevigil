# codevigil - Session Quality, Observed

Local, privacy-preserving observability for Claude Code sessions.

codevigil tails `~/.claude/projects/**/*.jsonl` on disk, computes signal metrics about reasoning and tool-use patterns, and surfaces them in a rich terminal dashboard or as JSON / markdown reports. **Zero network egress, no data ever leaves your machine.**

[![Status](https://img.shields.io/badge/status-beta-blue.svg)](https://github.com/Mathews-Tom/codevigil)
[![Version](https://img.shields.io/badge/version-0.4.0-informational.svg)](CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)
[![CI](https://github.com/Mathews-Tom/codevigil/actions/workflows/ci.yml/badge.svg)](https://github.com/Mathews-Tom/codevigil/actions/workflows/ci.yml)
[![Privacy](https://img.shields.io/badge/network%20egress-zero-success.svg)](docs/privacy.md)

## Install

```bash
uv tool install codevigil
```

`uv tool install` places the `codevigil` executable on your `PATH` inside an isolated environment that does not conflict with project virtualenvs. All subcommands, including the full `history` suite with colored panels and formatted tables, work out of the box.

Upgrade and uninstall:

```bash
uv tool upgrade codevigil
uv tool uninstall codevigil        # leaves config and session data untouched
```

No `uv`? Install it from <https://docs.astral.sh/uv/getting-started/installation/>. Fallbacks: `pipx install codevigil` and `pip install --user codevigil` both work. See [docs/installation.md](docs/installation.md) for all supported paths and from-source installs.

## First run

```bash
codevigil ingest        # one-shot cold-ingest into persistent memory (first run only)
codevigil watch         # project roll-up dashboard, resumes every file from its cached cursor
```

`codevigil ingest` walks every JSONL under `watch.roots`, parses them end-to-end, and writes a durable record (root-aware session key, raw session id, file id, cursor offset, collector state, metric summary) to the local SQLite store under `~/.local/state/codevigil/`. You run it once after install. Subsequent `codevigil watch` ticks seek past the saved cursor on every file, so the hot path only processes newly-appended events. If the store is absent on startup, `watch` will bootstrap it for you.

`codevigil watch` then prints a live **project-row** dashboard: one row per Claude Code project, with the fleet-worst severity, the active session count, and the aggregate metric summary. The top line shows fleet totals (session count, CRIT/WARN/OK tallies, project count, last-updated wall-clock tick). Every session's rolling-window collector state is restored from the store so restart does not erase your percentile baselines.

```text
codevigil [experimental thresholds] | sessions=3 crit=0 warn=1 ok=2 projects=2 updated=2026-04-16T10:22:00 | parse_confidence: 1.00
project: my-project      | 2 active | WARN   read_edit_ratio 3.1 | stop_phrase 1 | reasoning_loop 8.4 | thinking 0.82 | prompts 14
project: another-project | 1 active | OK     read_edit_ratio 5.6 | stop_phrase 0 | reasoning_loop 4.1 | thinking 0.91 | prompts  7
```

Pass `--by-session` (or set `watch.display_mode = "session"`) to fall back to the 0.2.x one-block-per-session layout:

```text
session: a3f7c2d | project: my-project | 2m 34s ACTIVE [task: debug_loop] [experimental]
──────────────────────────────────────────────────────────────
  read_edit_ratio    5.2   OK    [R:E 5.2 | research:mut 7.1] [↗3.1→4.2→5.2] [p68 of your baseline]
  stop_phrase        0     OK    [0 hits]
  reasoning_loop     6.4   OK    [6.4/1K tool calls | burst: 2] [↘8.1→7.2→6.4] [n/a]
  thinking           0.87  OK    [0.87 visible | chars med: 342 | sig med: 118]
  prompts            11    OK    [11 user turns]
──────────────────────────────────────────────────────────────
```

`Ctrl-C` exits cleanly. Walk through what every column means and how to interpret it in [docs/getting-started.md](docs/getting-started.md).

## What else can it do

```bash
codevigil config check                                                   # show the resolved config and where each value came from
codevigil ingest                                                         # cold-ingest every session into local persistent memory
codevigil ingest --force                                                 # rebuild the store from scratch, ignoring existing rows
codevigil watch --by-session                                             # one block per session (0.2.x layout)
codevigil report ~/.claude/projects                                      # default: stacked today / 7d / 30d panels
codevigil report sessions/ --format markdown --from 2026-04-01           # explicit window → single-period mode
codevigil report ~/.claude/projects --group-by week                      # cohort trend table by ISO week
codevigil report ~/.claude/projects --group-by week --format csv         # flat CSV for notebook consumption
codevigil report sessions/ --compare-periods 2026-03-01:2026-03-31,2026-04-01:2026-04-30
codevigil report sessions/ --pivot-date 2026-04-01                       # before/after delta at a change point
codevigil report sessions/ --group-by week --experimental-correlations   # append Pearson appendix [experimental]
codevigil report sessions/ --output-file ~/reports/april.md              # write to an exact file path
codevigil export session.jsonl                                           # NDJSON event stream on stdout, jq-friendly
codevigil export session.jsonl | jq 'select(.kind == "tool_call") | .payload.tool_name'
codevigil history list                                                   # list stored sessions
codevigil history list --task-type debug_loop --since 2026-04-01 --severity warn
codevigil history SESSION_ID                                             # event, metric, and per-turn task-type timeline
codevigil history diff SESSION_A SESSION_B                               # side-by-side Markdown diff of two sessions
codevigil history heatmap SESSION_ID                                     # tool × severity heatmap with proportional gradient bars
codevigil history heatmap --axis task_type                               # cross-tab metrics against experimental task labels
```

`codevigil report` with no date flags now renders three stacked windows — **today**, **7d**, and **30d** — in one invocation. Pass `--from` or `--to` to fall back to the original single-period mode. Scripts that depend on the old no-flag single-period output should pass `--from 1970-01-01` (or any open lower bound) to preserve the previous shape.

Full flag reference for every subcommand: [docs/cli.md](docs/cli.md).

## Configuration

codevigil resolves its configuration from a layered precedence chain: built-in defaults → `~/.config/codevigil/config.toml` → `CODEVIGIL_*` environment variables → CLI flags. `watch.roots` is the canonical multi-root setting; `watch.root` and `CODEVIGIL_WATCH_ROOT` remain supported as deprecated single-root aliases. Run `codevigil config check` to see every resolved key with its source and any deprecation notices.

A minimal `~/.config/codevigil/config.toml`:

```toml
[watch]
roots = ["~/.claude/projects"]
poll_interval = 1.0

[collectors.read_edit_ratio]
warn_threshold = 5.0
critical_threshold = 2.5
```

The complete key reference, env-var bindings, and validation rules live in [docs/configuration.md](docs/configuration.md).

## What gets measured

Five user-facing collectors plus an always-on integrity gate:

| Collector         | Signal                                                                                                                                |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `read_edit_ratio` | Reads vs. mutations, blind-edit detection, file-tracking confidence                                                                   |
| `stop_phrase`     | Hits against ownership-dodging, permission-seeking, premature-stopping, and known-limitation phrase categories                        |
| `reasoning_loop`  | Self-correction phrase rate per 1K tool calls plus longest consecutive burst                                                          |
| `thinking`        | Visible-vs-redacted thinking-block ratio plus median visible / signature character lengths (headline signal for #42796 depth decline) |
| `prompts`         | Cumulative user-turn count per session; feeds the #42796 "prompts per session" cohort mean                                            |
| `parse_health`    | Always-on. Flips to CRITICAL when parse confidence drops below 0.9 in any 50-line window                                              |

`thinking` and `prompts` are descriptive counters — severity stays at OK by design. They exist to feed cohort trend reports, not to alarm. Threshold semantics for the three gated collectors, what each metric is sensitive to, and how to interpret CRITICAL signals live in [docs/collectors.md](docs/collectors.md).

## Persistent memory

0.4.0 adds first-class multi-root support on top of the local SQLite-backed processed-session store under `~/.local/state/codevigil/processed/`. Every finalised session now writes a root-aware identity (`session_key`, raw `session_id`, cursor byte offset, collector state snapshot, and derived metric summary), and the watcher seeds each polled file from the cached cursor on startup instead of re-parsing JSONL from byte 0. Rolling-window collector state (the `read_edit_ratio` 50-event deque, the `reasoning_loop` burst counter) is restored verbatim across restarts, even when different roots contain the same `session_id`. Run `codevigil ingest` once after install; after that, `codevigil watch` only processes newly-appended events on the hot path. Disable the cursor cache for reproducible cold-start benchmarks with `watch.cursor_cache_enabled = false`. Schema, migration policy, and the invariants the store upholds live in [docs/design.md](docs/design.md).

## Cohort trend reports

`codevigil report --group-by {day,week,project,model,permission_mode}` aggregates every session in the store into cohort cells and emits a Markdown trend table with a methodology header, Δ-vs-prior-row annotations on chronological dimensions, and threshold highlighting for cells crossing warn / critical. Cells with `n<5` are redacted with an `n<5` sentinel. Additional cohort-only flags:

- `--pivot-date YYYY-MM-DD` — split the corpus at a change point and emit a Before/After delta table.
- `--compare-periods A_START:A_END,B_START:B_END` — signed delta table + prose summary per metric.
- `--experimental-correlations` — Pearson appendix across per-session metric columns; pairs below `n=30` are dropped. Exploratory only — correlation is not causation, and the rendered output says so explicitly.
- `--format csv` — flat `dimension_value,metric_name,mean,stdev,n,min,max` for notebook consumption.
- `--format json` — versioned JSON cohort document (`schema_version=1`) for downstream pipelines.
- `--output-file PATH` — write to an exact file path (parent dirs created, must resolve under `$HOME`).

Both new default collectors (`thinking`, `prompts`) surface in cohort reports as `thinking_visible_ratio`, `thinking_visible_chars_median`, `thinking_signature_chars_median`, and `user_turns`. Full reference: [docs/cli.md](docs/cli.md).

## Task classifier `[experimental]`

The experimental task classifier labels each Claude Code turn as `exploration`, `mutation_heavy`, `debug_loop`, `planning`, or `mixed` using a two-stage cascade (tool-presence heuristic → keyword regex on the user message, stdlib `re` only, zero network, zero new dependencies). Session-level labels aggregate turn labels by majority vote. Labels surface in four places:

- **`history list`** — new `task_type` column and `--task-type <label>` filter
- **`history heatmap --axis task_type`** — cross-tab metrics against task labels
- **`history SESSION_ID`** — per-turn task-type headings in the event timeline
- **`codevigil watch`** — right-aligned task tag in each session header

Every surface is marked `[experimental]`. The classifier is opt-out via `[classifier]` in `~/.config/codevigil/config.toml`:

```toml
[classifier]
enabled = false
```

When disabled, the four surfaces degrade cleanly: no task column in list, no task tag in watch, no per-turn headings in detail, and `history heatmap --axis task_type` exits with a clear error. Category definitions, the cascade algorithm, and the calibration gate (≥85% agreement on a labeled corpus) are documented in [docs/classifier.md](docs/classifier.md).

## Privacy

Three independent enforcement layers ensure session data never leaves your machine:

- **Runtime import allowlist hook** installed at package init refuses any import of `socket`, `urllib`, `http.client`, `httpx`, `requests`, `aiohttp`, `ftplib`, `smtplib`, `ssl`, `subprocess`, or related transports from inside a `codevigil` module.
- **CI grep gate** re-checks the source tree for the same banned names on every push as a belt-and-suspenders second layer.
- **Filesystem scope check** refuses any read or write path outside `$HOME` via a `Path.resolve().is_relative_to(home)` check.

The full privacy model and threat boundary: [docs/privacy.md](docs/privacy.md).

## Documentation

| Doc                                                | What it covers                                        |
| -------------------------------------------------- | ----------------------------------------------------- |
| [docs/installation.md](docs/installation.md)       | Install, upgrade, uninstall, from-source builds       |
| [docs/getting-started.md](docs/getting-started.md) | First-run walkthrough and interpreting the output     |
| [docs/cli.md](docs/cli.md)                         | Exhaustive CLI reference: every subcommand and flag   |
| [docs/configuration.md](docs/configuration.md)     | Every config key, env binding, and validation rule    |
| [docs/collectors.md](docs/collectors.md)           | What each metric measures and how to interpret it     |
| [docs/classifier.md](docs/classifier.md)           | Experimental task classifier: categories and surfaces |
| [docs/privacy.md](docs/privacy.md)                 | Privacy guarantees and the threat model               |
| [docs/design.md](docs/design.md)                   | Architecture, plugin boundaries, error taxonomy       |
| [CHANGELOG.md](CHANGELOG.md)                       | Release notes                                         |

## Experimental thresholds

The shipped default thresholds were derived from a single user's session window — one user is not a population baseline. Every default ships with `experimental = true` and the watch header shows `[experimental thresholds]` until you either flip the flag in config or let bootstrap mode personalise the thresholds for your own workflow.

Bootstrap mode observes the first 10 sessions (configurable) with all severities pinned to `OK`, records the per-collector value distributions, then derives WARN at p80 and CRITICAL at p95 of _your_ local data, clamped by the literal-value hard caps. No manual tuning required. See [docs/collectors.md#experimental-thresholds-and-bootstrap](docs/collectors.md#experimental-thresholds-and-bootstrap).

## Contributing

```bash
git clone https://github.com/Mathews-Tom/codevigil
cd codevigil
uv sync --dev
uv run pytest
uv run mypy --strict codevigil
uv run ruff check .
uv run ruff format --check .
bash scripts/ci_privacy_grep.sh
```

All five gates must pass before a commit lands. The privacy grep runs as a separate CI job alongside the typecheck-and-test matrix on every PR.

## License

Apache License 2.0. See [LICENSE](LICENSE).

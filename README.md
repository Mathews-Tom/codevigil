# codevigil

Local, privacy-preserving observability for Claude Code sessions.

codevigil tails `~/.claude/projects/**/*.jsonl` on disk, computes signal metrics
about reasoning and tool-use patterns, and surfaces them in a terminal dashboard
or as JSON/markdown reports. Stdlib-only runtime, zero network egress, no data
ever leaves your machine.

Status: alpha (Development Status :: 3). Python 3.11 and 3.12.

## Quickstart

Install from a local checkout with `uv`:

```
uv tool install --from . codevigil
```

Or from PyPI once published:

```
pip install codevigil
```

Then:

```
codevigil config check            # show the resolved config and its sources
codevigil watch                   # live terminal dashboard
codevigil report ~/.claude/projects --format markdown
codevigil export session.jsonl    # NDJSON on stdout for piping to jq
```

## CLI reference

Global flags apply to every subcommand:

| flag | description |
| --- | --- |
| `--config PATH` | Path to a TOML config file. Overrides `~/.config/codevigil/config.toml`. |
| `--explain` | Surface `stop_phrase` intent annotations in watch/report/export output. |
| `--version` | Print `codevigil <version>` and exit. |

### `codevigil config check`

Resolve the effective config and print each value with its source (default,
file, env, or CLI). Exit code is `0` on success, `2` on a critical config
error.

### `codevigil watch`

Live tick loop over `~/.claude/projects` session files. Polls at the
configured interval, parses new events, runs every enabled collector, and
renders a terminal frame per tick. `Ctrl-C` triggers a clean shutdown.

When any enabled collector is still marked `experimental = true`, the header
shows `[experimental thresholds]` until bootstrap completes or the user sets
`experimental = false` in config.

### `codevigil report PATH`

Batch analysis over one or more session files.

| flag | description |
| --- | --- |
| `PATH` | File, directory (walked recursively for `*.jsonl`), or shell glob. |
| `--from YYYY-MM-DD` | Filter sessions whose first event is on/after this date. |
| `--to YYYY-MM-DD` | Filter sessions whose first event is on/before this date. |
| `--format {json,markdown}` | Output format (default: `json`). |
| `--output DIR` | Override report output directory. Must live under `$HOME`. |

Output is deterministic under identical input: sessions sort by id, metric
rows sort by name, no wall-clock timestamps are embedded. Reports are written
to `~/.local/share/codevigil/reports/` by default. Exit code is `2` if any
session's parse confidence drops below `0.9`.

### `codevigil export PATH`

Stream parsed events as NDJSON on stdout. Each line is one event with
`timestamp`, `session_id`, `kind`, and `payload`. Pipe into `jq` to compute
ad-hoc aggregates. Without `--explain`, `intent` fields are stripped from the
payload for symmetry with the non-explain watch output.

## Configuration

Config is resolved in this precedence order (highest first):

1. CLI flags
2. Environment variables (`CODEVIGIL_*`)
3. Config file (`~/.config/codevigil/config.toml` or `--config PATH`)
4. Built-in defaults

Environment overrides:

| env var | maps to |
| --- | --- |
| `CODEVIGIL_LOG_PATH` | `logging.log_path` |
| `CODEVIGIL_WATCH_ROOT` | `watch.root` |
| `CODEVIGIL_WATCH_POLL_INTERVAL` | `watch.poll_interval` |
| `CODEVIGIL_WATCH_TICK_INTERVAL` | `watch.tick_interval` |
| `CODEVIGIL_REPORT_OUTPUT_DIR` | `report.output_dir` |
| `CODEVIGIL_REPORT_OUTPUT_FORMAT` | `report.output_format` |
| `CODEVIGIL_BOOTSTRAP_SESSIONS` | `bootstrap.sessions` |

Run `codevigil config check` to see every resolved key with its source.

## Privacy guarantees

- Zero network egress. A runtime import allowlist hook installed at package
  init raises `PrivacyViolationError` if any codevigil module imports
  `socket`, `urllib`, `http.client`, `httpx`, `requests`, `aiohttp`,
  `ftplib`, `smtplib`, `ssl`, `subprocess`, or related transports.
- A CI grep gate (`scripts/ci_privacy_grep.sh`) re-checks the tree for the
  same banned names as a belt-and-suspenders second layer.
- The watcher and the report writer both refuse any path outside `$HOME` via
  a `Path.resolve().is_relative_to(home)` check on every read and write.

No data ever leaves your machine.

## Experimental thresholds

Default collector thresholds in v0.1 are derived from a single user's post-hoc
session window. One user's sample is not a population baseline, so every
default is marked `experimental = true` and the watch header displays an
`[experimental thresholds]` badge until you either run bootstrap mode or set
`experimental = false` in config.

Bootstrap mode observes the first N sessions (default `bootstrap.sessions =
10`) with severity pinned to `OK`, records per-collector value distributions,
then shifts defaults to local-percentile thresholds (WARN at p80, CRITICAL at
p95) clamped by the literal-value hard caps. This personalises signal to your
actual workflow without manual tuning.

## Complexity ceiling

Collectors have honest per-ingest cost:

- `read_edit_ratio` is O(1): deque append plus counter update.
- `stop_phrase` and `reasoning_loop` are O(P·L) with P = phrase count and
  L = message length. Both escalate to an Aho–Corasick automaton once P > 32
  to bound cost to O(L + matches).
- `blind_edit_rate` is O(W) with W = lookback window size (default 20).

At P=50 and L=2000 the naive scan is roughly 5M character compares per
session of 50 assistant messages — well under a second on any modern laptop,
but not O(1). The Aho–Corasick escalation is the upgrade path if user phrase
lists grow large.

## Development

```
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict codevigil
uv run pytest
bash scripts/ci_privacy_grep.sh
```

All five gates must pass before a commit lands. The privacy grep runs
separately in CI as a second layer against the runtime import allowlist.

## License

MIT. See [LICENSE](LICENSE).

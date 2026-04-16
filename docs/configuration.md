# Configuration reference

codevigil resolves its effective configuration through a layered precedence chain:

1. **Built-in defaults** — hardcoded in `codevigil/config.py::CONFIG_DEFAULTS`.
2. **Config file** — `~/.config/codevigil/config.toml`, or any path passed via `--config PATH`.
3. **Environment variables** — `CODEVIGIL_*` bindings (a deliberately small set).
4. **CLI flags** — highest precedence, override everything else.

Every leaf value carries a provenance string. Run `codevigil config check` to see the resolved value and source for every key.

Validation is fail-loud: unknown keys, wrong types, out-of-range values, unknown collector or renderer names, and bad output formats all abort startup with a descriptive error message that names the offending key, source layer, and expected type or range.

## Sections

The default config tree has these top-level sections:

- [`[watch]`](#watch) — file polling, lifecycle, tick cadence
- [`[collectors]`](#collectors) — per-collector configuration and the `enabled` allow-list
- [`[renderers]`](#renderers) — output renderers
- [`[report]`](#report) — batch report output
- [`[logging]`](#logging) — error log file path
- [`[bootstrap]`](#bootstrap) — threshold calibration window
- [`[storage]`](#storage) — opt-in session-report persistence
- [`[classifier]`](#classifier) — turn-level task classifier (experimental)

## `[watch]`

| Key                     | Type    | Default                    | Description                                                                                                                                                                                                                                                                                              |
| ----------------------- | ------- | -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `root`                  | `str`   | `~/.claude/projects`       | Directory to walk for session JSONL files. Must resolve under `$HOME`.                                                                                                                                                                                                                                   |
| `poll_interval`         | `float` | `2.0`                      | Seconds between filesystem polls. Range: `[0.05, 3600]`.                                                                                                                                                                                                                                                 |
| `tick_interval`         | `float` | `1.0`                      | Seconds between aggregator ticks (and terminal frames). Range: `[0.05, 3600]`.                                                                                                                                                                                                                           |
| `max_files`             | `int`   | `2000`                     | Cap on the number of session files walked per poll. Overflow logs one WARN per run and processes the first N deterministically. Range: `[1, 1_000_000]`.                                                                                                                                                 |
| `large_file_warn_bytes` | `int`   | `10 * 1024 * 1024`         | Per-poll growth above this triggers a single WARN per file per run. Range: `[1024, 10**12]`.                                                                                                                                                                                                             |
| `stale_after_seconds`   | `int`   | `300`                      | A session silent for this long transitions to STALE. Collector state is preserved. Range: `[1, 86400]`.                                                                                                                                                                                                  |
| `evict_after_seconds`   | `int`   | `2100`                     | A session silent for this long is EVICTED. `reset()` is called on every collector and the cursor is dropped. Must be strictly greater than `stale_after_seconds`. Range: `[1, 86400]`.                                                                                                                   |
| `display_mode`          | `str`   | `"project"`                | Watch dashboard layout. `"project"` rolls every active session in a project into a single row with fleet-worst severity, active count, and aggregate metric summary. `"session"` renders the 0.2.x one-block-per-session layout. The `--by-session` CLI flag forces `"session"` for a single invocation. |
| `display_limit`         | `int`   | `20`                       | Max session blocks rendered per frame in `"session"` mode. Ranked by severity then recency. When the active set exceeds the cap, a footer line reports the omitted count. Range: `[1, 500]`. Env: `CODEVIGIL_WATCH_DISPLAY_LIMIT`.                                                                       |
| `display_project_limit` | `int`   | `10`                       | Max project rows rendered per frame in `"project"` mode. Ranked by fleet-worst severity then most-recent activity.                                                                                                                                                                                       |
| `cursor_cache_enabled`  | `bool`  | `true`                     | Seed each polled file from its last saved byte offset on startup instead of re-reading from byte 0. Disable for fully reproducible cold-start benchmarks.                                                                                                                                                |
| `cursor_cache_dir`      | `str`   | `~/.local/state/codevigil` | Directory that holds the persistent cursor cache. Must resolve under `$HOME`.                                                                                                                                                                                                                            |

### Watch lifecycle

A session moves through three states: `ACTIVE` → `STALE` → `EVICTED`.

- `ACTIVE` — receiving events. The terminal renderer shows the session at the top of the dashboard.
- `STALE` — silent for at least `stale_after_seconds`. Collector state is **preserved** so a quick coffee break does not erase your metric history. A new APPEND flips the session back to ACTIVE.
- `EVICTED` — silent for at least `evict_after_seconds`. Every collector's `reset()` method is called and the session context is dropped from the aggregator. A new APPEND on the same file id starts a fresh session.

## `[collectors]`

| Key       | Type        | Default                                                                       | Description                                                                                                                                                                                                                                                                                                           |
| --------- | ----------- | ----------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `enabled` | `list[str]` | `["read_edit_ratio", "stop_phrase", "reasoning_loop", "thinking", "prompts"]` | The user-facing collectors that ingest events. Must contain only names that exist in the registry. Duplicates are rejected. `parse_health` is **always on** and not part of this list. 0.3.0 adds `thinking` and `prompts` to the default list — users with an explicit override in their config file are unaffected. |

Each enabled collector has its own subsection. The shipped subsections are documented below.

### `[collectors.parse_health]` (always on)

| Key                  | Type    | Default | Description                                                                                                                                                                                                                                                         |
| -------------------- | ------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `enabled`            | `bool`  | `true`  | **Cannot be set to false.** Any layer that attempts to disable `parse_health` raises `ConfigError("config.parse_health_undisableable")`. The integrity gate is required, not optional.                                                                              |
| `critical_threshold` | `float` | `0.9`   | Rolling `parse_confidence` below this value flips the collector to CRITICAL once the internal 50-line window has accumulated enough lines. Relax it for projects with known-noisy wire formats that do not need the default 90 % fidelity bar. Range: `[0.0, 1.0]`. |

### `[collectors.read_edit_ratio]`

| Key                           | Type    | Default | Description                                                                                                                                                                                            |
| ----------------------------- | ------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `window_size`                 | `int`   | `50`    | Rolling deque size for tool call classification. Range: `[1, 100_000]`.                                                                                                                                |
| `warn_threshold`              | `float` | `4.0`   | `read_edit_ratio < warn_threshold` raises severity to WARN.                                                                                                                                            |
| `critical_threshold`          | `float` | `2.0`   | `read_edit_ratio < critical_threshold` raises severity to CRITICAL.                                                                                                                                    |
| `blind_edit_window`           | `int`   | `20`    | Lookback window for blind-edit detection (mutations without a preceding read or research on the same file). Range: `[1, 10_000]`.                                                                      |
| `blind_edit_confidence_floor` | `float` | `0.95`  | When the fraction of mutation events with a populated `file_path` falls below this floor, the blind-edit metric is relabeled `"insufficient data"` and severity is clamped to OK. Range: `[0.0, 1.0]`. |
| `min_events_for_severity`     | `int`   | `10`    | Number of classified events (reads + research + mutations) the collector must see before it emits anything other than `OK`. While below this gate the label is `"warming up"`. Range: `[0, 100_000]`.  |
| `experimental`                | `bool`  | `true`  | Surfaces the `[experimental thresholds]` badge in the watch header. Flip to `false` after bootstrap or after manual calibration.                                                                       |

### `[collectors.stop_phrase]`

| Key                  | Type                | Default | Description                                                                                                   |
| -------------------- | ------------------- | ------- | ------------------------------------------------------------------------------------------------------------- |
| `custom_phrases`     | `list[str \| dict]` | `[]`    | User-supplied phrases on top of the built-in categories. See [phrase forms](#stop-phrase-custom-forms) below. |
| `warn_threshold`     | `float`             | `1.0`   | Total hits ≥ this threshold raises severity to WARN.                                                          |
| `critical_threshold` | `float`             | `3.0`   | Total hits ≥ this threshold raises severity to CRITICAL.                                                      |
| `experimental`       | `bool`              | `true`  | As above.                                                                                                     |

#### Stop phrase custom forms

`custom_phrases` accepts a mixed list of plain strings and TOML inline tables.

```toml
[collectors.stop_phrase]
custom_phrases = [
    "should I continue",                                                    # plain string → word-boundary match
    { text = "leave that for now", mode = "word", category = "premature_stopping" },
    { text = "as an AI", mode = "substring", category = "known_limitation" },
    { text = "\\bsome[Pp]attern\\b", mode = "regex", category = "ownership_dodging", intent = "deflects ownership" },
]
```

Each entry has the following fields:

- `text` (required, `str`) — the phrase or pattern.
- `mode` (optional, one of `"word"` / `"substring"` / `"regex"`, default `"word"`) — match strategy.
- `category` (optional, `str`) — bucket for hit aggregation. Default categories: `ownership_dodging`, `permission_seeking`, `premature_stopping`, `known_limitation`.
- `intent` (optional, `str`) — short annotation surfaced via the `--explain` CLI flag.

Unknown keys in the table form raise `ConfigError("config.unknown_key")`. Bad mode values raise `ConfigError("config.out_of_range")`. Missing `text` raises `ConfigError("config.type_mismatch")`.

### `[collectors.reasoning_loop]`

| Key                           | Type    | Default | Description                                                                                                                                                                                               |
| ----------------------------- | ------- | ------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `warn_threshold`              | `float` | `10.0`  | `loop_rate ≥ warn_threshold` raises severity to WARN. Loop rate is matches per 1000 tool calls.                                                                                                           |
| `critical_threshold`          | `float` | `20.0`  | `loop_rate ≥ critical_threshold` raises severity to CRITICAL.                                                                                                                                             |
| `min_tool_calls_for_severity` | `int`   | `20`    | Number of tool calls the collector must see before it emits anything other than `OK`. Short sessions that happen to open with a self-correction phrase do not trigger false WARNs. Range: `[0, 100_000]`. |
| `experimental`                | `bool`  | `true`  | As above.                                                                                                                                                                                                 |

### `[collectors.thinking]`

| Key            | Type   | Default | Description                                                                                                                                                                     |
| -------------- | ------ | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `experimental` | `bool` | `true`  | Marks the collector output as experimental in watch headers and cohort report column headers. The collector itself has no tunable thresholds — severity is always OK by design. |

The `thinking` collector observes `EventKind.THINKING` events and exposes the visible-vs-redacted ratio as its primary scalar, plus median visible-block and median signature-block character lengths in `detail`. Headline signal for the #42796 thinking-depth-decline cohort analysis. No threshold, no severity gate — this is a descriptive counter feeding cohort trends, not an alarm. Disable by removing `"thinking"` from `collectors.enabled`.

### `[collectors.prompts]`

| Key            | Type   | Default | Description                                                                                                                                                                     |
| -------------- | ------ | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `experimental` | `bool` | `true`  | Marks the collector output as experimental in watch headers and cohort report column headers. The collector itself has no tunable thresholds — severity is always OK by design. |

The `prompts` collector counts `EventKind.USER_MESSAGE` events per session. Primary scalar is the cumulative user-turn count; it feeds the #42796 "prompts per session" per-week cohort mean. Like `thinking`, it is a descriptive counter — no threshold, no severity gate. Disable by removing `"prompts"` from `collectors.enabled`.

## `[renderers]`

| Key       | Type        | Default        | Description                                                                                                                               |
| --------- | ----------- | -------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `enabled` | `list[str]` | `["terminal"]` | Renderers attached to the aggregator's tick output. Must contain only names that exist in the renderer registry. Duplicates are rejected. |

Renderer names recognised by the v0.1 registry: `terminal`, `json_file`. Watch mode defaults to `terminal`. To stream snapshots to a JSONL file in addition to the terminal, set `enabled = ["terminal", "json_file"]`.

## `[report]`

| Key             | Type  | Default                            | Description                                                                                                                                           |
| --------------- | ----- | ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `output_format` | `str` | `"json"`                           | Default output format for `codevigil report`. One of `"json"` or `"markdown"`. CLI `--format` overrides.                                              |
| `output_dir`    | `str` | `~/.local/share/codevigil/reports` | Default output directory. CLI `--output` overrides. The resolved path must lie under `$HOME` — codevigil refuses to write outside the home directory. |

## `[logging]`

| Key        | Type  | Default                                  | Description                                                                                                                                                 |
| ---------- | ----- | ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `log_path` | `str` | `~/.local/state/codevigil/codevigil.log` | Path to the rotating JSONL error log. Rotation is hand-implemented at 10 MiB × 3 archived files. The log path is also overridable via `CODEVIGIL_LOG_PATH`. |

## `[bootstrap]`

| Key          | Type  | Default                                   | Description                                                                                                                                                                                       |
| ------------ | ----- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sessions`   | `int` | `10`                                      | Number of sessions the bootstrap manager observes before computing personalised thresholds. While inside the window, every collector snapshot has its severity clamped to OK. Range: `[1, 1000]`. |
| `state_path` | `str` | `~/.local/state/codevigil/bootstrap.json` | On-disk persistence path for the bootstrap manager. Survives process restarts; corrupt files trigger a re-bootstrap with a single WARN.                                                           |

### How user-supplied thresholds interact with bootstrap

When you set `warn_threshold` or `critical_threshold` in your config file, those values act as **calibration bounds**, not as unconditional preserved values.

For **high-is-worse** metrics (`stop_phrase`, `reasoning_loop`): the hard cap is a **strictness ceiling**. Bootstrap picks the p80 (WARN) and p95 (CRITICAL) of your local session distribution, then applies `min(p80, warn_cap)` and `max(p95, critical_cap)`. Bootstrap can tighten a threshold toward your observed normal, but can never loosen it past your configured cap.

For **low-is-worse** metrics (`read_edit_ratio`): the hard cap is a **looseness floor**. Bootstrap picks the p20 (WARN) and p5 (CRITICAL) of your local distribution, then applies `max(p20, warn_cap)` and `min(p5, critical_cap)`. Bootstrap can relax toward your observed normal, but can never strictify past your configured cap.

The practical implication: a user who sets `warn_threshold = 5.0` for `read_edit_ratio` will never see bootstrap move the warning trigger above `5.0`, even if their sessions routinely produce ratios of `3.0`. The explicit threshold acts as the looseness floor. This is intentional — user-supplied values signal intent, and bootstrap must respect the intent's direction.

## `[classifier]`

Controls the turn-level task classifier. This is an **experimental** feature; the category labels and cascade rules may change between minor releases.

| Key            | Type   | Default | Description                                                                                                                                                                                                                                       |
| -------------- | ------ | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `enabled`      | `bool` | `true`  | When `true`, each completed turn is classified via the two-stage cascade and `session_task_type` / `turn_task_types` are written to the session report. Set to `false` to skip classification entirely; both fields will be `null` in the report. |
| `experimental` | `bool` | `true`  | Marks the feature as experimental. Flip to `false` after you have validated the category labels against your own session corpus. Has no effect on runtime behaviour; it is metadata for dashboards and report consumers.                          |

### Disabling the classifier

```toml
[classifier]
enabled = false
```

When disabled, `session_task_type` and `turn_task_types` are `null` in every session report written by `codevigil watch`. No CPU is spent on classification.

### Category labels

The classifier assigns one of five labels to each turn, then aggregates to session level by majority vote:

| Label            | Meaning                                                  |
| ---------------- | -------------------------------------------------------- |
| `exploration`    | Read-heavy investigation with minimal or no mutations.   |
| `mutation_heavy` | Three or more file-write operations with no bash calls.  |
| `debug_loop`     | Bash execution co-present with file mutations (fix/run). |
| `planning`       | Pure text turn — no tool calls at all.                   |
| `mixed`          | No single category exceeds 50 % of classified turns.     |

See [`docs/classifier.md`](classifier.md) for the full cascade algorithm and rule specifications.

## `[storage]`

Controls whether `codevigil watch` writes finalised session reports to disk for retrospective analysis.

| Key                    | Type   | Default | Description                                                                                                                                                                                                                                  |
| ---------------------- | ------ | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `enable_persistence`   | `bool` | `false` | When `false` (the default), `codevigil watch` writes nothing under `~/.local/state/codevigil/sessions/`. Set to `true` to enable the session-report store. The first write logs a single-line activation notice naming the target directory. |
| `min_observation_days` | `int`  | `1`     | Minimum number of calendar days a period must span to be included in cohort output. Periods shorter than this are dropped with a logged reason. Range: `[1, 365]`.                                                                           |

### Enabling persistence

```toml
[storage]
enable_persistence = true
```

When persistence is first enabled, codevigil logs a one-time activation notice at INFO level:

```
persistence enabled, writing to /home/user/.local/state/codevigil/sessions/
```

The session directory is resolved as `$XDG_STATE_HOME/codevigil/sessions/` when `XDG_STATE_HOME` is set, falling back to `~/.local/state/codevigil/sessions/`.

Session reports are written at session eviction time (when a session has been silent for `evict_after_seconds`). Each report is one JSON file named `<session_id>.json`. See `docs/design.md §Session Report Schema` for the full field reference and migration policy.

### Default behaviour (persistence disabled)

With the default `enable_persistence = false`, `codevigil watch` creates no files under `~/.local/state/codevigil/sessions/`. The existing log file at `~/.local/state/codevigil/codevigil.log` and bootstrap state at `~/.local/state/codevigil/bootstrap.json` are unaffected.

## Environment variables

Only the keys in this map can be overridden via the environment. Every other key must be set in TOML or on the CLI. The bindings are kept small on purpose — a typo in a `CODEVIGIL_*` variable that is not in this list is a no-op, not a silent override.

| Environment variable             | Maps to                |
| -------------------------------- | ---------------------- |
| `CODEVIGIL_LOG_PATH`             | `logging.log_path`     |
| `CODEVIGIL_WATCH_ROOT`           | `watch.root`           |
| `CODEVIGIL_WATCH_POLL_INTERVAL`  | `watch.poll_interval`  |
| `CODEVIGIL_WATCH_TICK_INTERVAL`  | `watch.tick_interval`  |
| `CODEVIGIL_WATCH_DISPLAY_LIMIT`  | `watch.display_limit`  |
| `CODEVIGIL_REPORT_OUTPUT_DIR`    | `report.output_dir`    |
| `CODEVIGIL_REPORT_OUTPUT_FORMAT` | `report.output_format` |
| `CODEVIGIL_BOOTSTRAP_SESSIONS`   | `bootstrap.sessions`   |

Environment values arrive as strings and are coerced against the default's declared type. `CODEVIGIL_WATCH_POLL_INTERVAL=0.5` parses as `float`; `CODEVIGIL_BOOTSTRAP_SESSIONS=20` parses as `int`. Coercion failures raise `ConfigError("config.type_mismatch")`.

## Validation rules

Every layer is validated against the same rules. A failure aborts startup with an exit code of 2 (critical) or 1 (non-critical) and a stderr message naming the key, source, and expected shape.

### Errors you can hit

| Code                                                       | When                                                                                      | Example                                            |
| ---------------------------------------------------------- | ----------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `config.unknown_key`                                       | A key not in `CONFIG_DEFAULTS` appears in any layer.                                      | `[mystery] x = 1`                                  |
| `config.type_mismatch`                                     | A value has the wrong type for its declared default.                                      | `poll_interval = "fast"`                           |
| `config.out_of_range`                                      | A scalar is outside its allowed range.                                                    | `poll_interval = -1.0`                             |
| `config.unknown_collector`                                 | An entry in `collectors.enabled` is not a registered collector name.                      | `enabled = ["does_not_exist"]`                     |
| `config.unknown_renderer`                                  | An entry in `renderers.enabled` is not a registered renderer name.                        | `enabled = ["projector"]`                          |
| `config.duplicate_collector` / `config.duplicate_renderer` | A name appears more than once in an `enabled` list.                                       | `enabled = ["read_edit_ratio", "read_edit_ratio"]` |
| `config.invalid_output_format`                             | `report.output_format` is not `"json"` or `"markdown"`.                                   | `output_format = "pdf"`                            |
| `config.parse_health_undisableable`                        | Any layer sets `collectors.parse_health.enabled = false`.                                 | `parse_health.enabled = false`                     |
| `config.file_not_found`                                    | An explicit `--config` path does not exist. (A missing default file is **not** an error.) | `--config /nope.toml`                              |
| `config.toml_parse_error`                                  | The TOML file fails to parse.                                                             | malformed TOML                                     |

## Worked examples

### Minimal personalisation

```toml
[watch]
poll_interval = 1.0

[collectors.read_edit_ratio]
warn_threshold = 5.0
critical_threshold = 2.5
```

### Aggressive stop-phrase scanning

```toml
[collectors.stop_phrase]
warn_threshold = 1
critical_threshold = 2
custom_phrases = [
    "I'll leave that for now",
    "this should be sufficient",
    { text = "tell me what you'd like", mode = "word", category = "ownership_dodging", intent = "deflects ownership" },
]
```

### Both renderers active

```toml
[renderers]
enabled = ["terminal", "json_file"]

[report]
output_dir = "~/codevigil-reports"
```

### Override everything via the environment

```bash
export CODEVIGIL_WATCH_ROOT=~/work/.claude/projects
export CODEVIGIL_WATCH_POLL_INTERVAL=0.5
export CODEVIGIL_REPORT_OUTPUT_DIR=~/work/codevigil-reports
codevigil watch
```

### Confirm what you set actually applied

```bash
codevigil config check
```

Look for `(env:CODEVIGIL_*)` or `(file:...)` annotations next to the keys you changed. If they still read `(default)`, the layer did not resolve.

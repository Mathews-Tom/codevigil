# CLI reference

Exhaustive reference for every codevigil subcommand and flag. For a quick first-run walkthrough see [getting-started.md](getting-started.md).

## Top-level flags

These apply to every subcommand and must appear before the subcommand name on the command line.

| Flag | Description |
| --- | --- |
| `-h, --help` | Show help for the top-level command and exit. |
| `--version` | Print `codevigil <version>` and exit. |
| `--config PATH` | Path to a TOML config file. Overrides `~/.config/codevigil/config.toml`. |
| `--explain` | Surface `stop_phrase` collector `intent` annotations in `watch`, `report`, and `export` output. |

## Subcommands

| Subcommand | Purpose |
| --- | --- |
| [`config check`](#config-check) | Resolve the effective config and print each value with its source. |
| [`watch`](#watch) | Live tick loop over `~/.claude/projects` with a terminal dashboard. |
| [`report`](#report) | Batch analysis over one or more session files. |
| [`export`](#export) | Stream parsed events as NDJSON on stdout. |

---

## `config check`

```text
codevigil config check
```

Resolves the effective configuration through the precedence chain (defaults → config file → environment → CLI flags) and prints every leaf key with its provenance. Useful for debugging "why is this value what it is".

### Output format

```text
codevigil config check
  bootstrap.sessions = 10  (default)
  bootstrap.state_path = '~/.local/state/codevigil/bootstrap.json'  (default)
  collectors.enabled = ['read_edit_ratio', 'stop_phrase', 'reasoning_loop']  (default)
  ...
  watch.poll_interval = 5.0  (file:/Users/me/.config/codevigil/config.toml)
  watch.tick_interval = 1.5  (env:CODEVIGIL_WATCH_TICK_INTERVAL)
```

Each line follows the format `<dotted.key> = <value>  (<source>)`. The source is one of:

- `default` — the built-in default in `codevigil/config.py`
- `file:<path>` — set by the resolved TOML config file
- `env:<VAR>` — set by an environment variable
- `cli:--<flag>` — set by a CLI flag (not user-facing in v0.1)

### Exit codes

- `0` — success
- `1` — non-critical config error
- `2` — critical config error (unknown key, type mismatch, out of range, unknown collector / renderer name)

### Examples

```bash
codevigil config check
codevigil --config ./project-codevigil.toml config check
CODEVIGIL_WATCH_POLL_INTERVAL=0.5 codevigil config check
```

---

## `watch`

```text
codevigil watch
```

Starts the live tick loop. Polls `watch.root` at `watch.poll_interval`, parses new events, runs every enabled collector, and renders a frame to the terminal at `watch.tick_interval`.

### What it does

1. Resolves config.
2. Constructs a `PollingSource` rooted at `watch.root` (default `~/.claude/projects`). Refuses to start if the resolved root is outside `$HOME`.
3. Loads `~/.config/codevigil/projects.toml` if present, for friendly project name resolution.
4. Constructs a `SessionAggregator` with the bootstrap manager loaded from `bootstrap.state_path`.
5. Runs the tick loop:
   - `renderer.begin_tick()`
   - For each `(meta, snapshots)` pair from `aggregator.tick()`:
     - Apply `--explain` annotation rewrites if the flag is set.
     - `renderer.render(snapshots, meta)`
   - `renderer.end_tick()` — flushes the buffered frame in one write.
   - `time.sleep(watch.tick_interval)`
6. On `SIGINT` (`Ctrl-C`), the next tick exits the loop, calls `aggregator.close()` and `renderer.close()` via `try/finally`, prints `codevigil shutdown`, and returns 0.

### Output

Multi-session terminal dashboard. Most-recently-active sessions appear first. See [getting-started.md](getting-started.md) for a fully annotated example frame.

### Exit codes

- `0` — clean shutdown via `Ctrl-C`
- `1` — non-critical config or runtime error
- `2` — critical error (unknown config, path scope violation, etc.)

### Examples

```bash
codevigil watch
codevigil --explain watch
CODEVIGIL_WATCH_POLL_INTERVAL=1.0 codevigil watch
codevigil --config ./local.toml watch
```

---

## `report`

```text
codevigil report PATH [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                      [--format {json,markdown}] [--output DIR]
```

Batch analysis over one or more session files. Walks the input, parses each file, runs every v0.1 collector, and writes a deterministic report to `report.output_dir` (or `--output`).

### Positional argument

| Argument | Description |
| --- | --- |
| `PATH` | A file, a directory (recursively walked for `*.jsonl`), or a shell glob pattern. Globs are evaluated relative to the parent directory of the pattern. |

### Flags

| Flag | Description |
| --- | --- |
| `--from YYYY-MM-DD` | Drop sessions whose first event timestamp is strictly before this date. |
| `--to YYYY-MM-DD` | Drop sessions whose first event timestamp is strictly after this date. |
| `--format {json,markdown}` | Output format. Default `json`. |
| `--output DIR` | Override the report output directory. Must resolve under `$HOME`. |

### JSON output shape

```json
{
  "kind": "report",
  "generated_at_session_count": 12,
  "sessions": [
    {
      "session_id": "abc123",
      "project_hash": "fixture-1",
      "project_name": "my-project",
      "metrics": [
        {
          "name": "read_edit_ratio",
          "value": 5.2,
          "label": "R:E 5.2 | research:mut 7.1",
          "severity": "ok",
          "detail": {
            "research_mutation_ratio": 7.1,
            "blind_edit_rate": 0.0,
            "tracking_confidence": 1.0,
            "experimental": true
          }
        }
      ]
    }
  ]
}
```

Sessions are sorted by `session_id`; metrics within each session are sorted by `name`. Top-level keys are emitted via `json.dumps(..., sort_keys=True)`. The output is byte-identical across runs on identical input — diffable under git.

### Markdown output shape

A short summary header followed by one section per session, each containing a metric table:

```markdown
# codevigil report

Sessions: 12

## session: abc123

project: my-project

| metric          | value | severity | label                         |
| --------------- | ----- | -------- | ----------------------------- |
| read_edit_ratio | 5.2   | OK       | R:E 5.2 \| research:mut 7.1   |
| reasoning_loop  | 6.4   | OK       | 6.4/1K tool calls \| burst: 2 |
| stop_phrase     | 0     | OK       | 0 hits                        |
```

### Output directory

The default output directory is `~/.local/share/codevigil/reports/`. Override via `--output DIR` or `report.output_dir` in config. The resolved path **must** be under `$HOME` — codevigil refuses to write outside the home directory and exits 2 with `PrivacyViolationError` if you point `--output` elsewhere.

### Exit codes

- `0` — success, no integrity issues
- `2` — at least one session had `parse_confidence < 0.9` (parse_health CRITICAL), OR `--output` resolved outside `$HOME`, OR a config error

The non-zero exit on parse_health degradation is intentional: it lets shell scripts and CI jobs detect data integrity failures without parsing the report content.

### Examples

```bash
codevigil report ~/.claude/projects
codevigil report ~/.claude/projects --format markdown
codevigil report sessions/ --from 2026-04-01 --to 2026-04-30
codevigil report 'sessions/*.jsonl' --format json --output ~/reports
codevigil --explain report sessions/ --format markdown
```

---

## `export`

```text
codevigil export PATH
```

Streams the parsed event stream as NDJSON on stdout, one JSON object per line. Designed for piping into `jq`, loading into notebooks, or feeding ad-hoc analysis pipelines.

### Positional argument

| Argument | Description |
| --- | --- |
| `PATH` | A file, a directory (recursively walked for `*.jsonl`), or a shell glob pattern. |

### Output shape

```json
{
  "timestamp": "2026-04-13T10:11:23+00:00",
  "session_id": "abc123",
  "kind": "tool_call",
  "payload": {
    "tool_name": "read",
    "tool_use_id": "tool-1",
    "input": { "path": "/home/user/code.py" }
  }
}
```

Each line is one parsed `Event`. The `kind` field is one of:

- `tool_call`
- `tool_result`
- `assistant`
- `user`
- `thinking`
- `system`

The `payload` shape varies per kind. See [design.md §Payload Schemas by EventKind](design.md#payload-schemas-by-eventkind) for the authoritative table.

### Exit codes

- `0` — success
- `1` — file read error
- `2` — config error or file not found

### Examples

```bash
# Count events by kind
codevigil export session.jsonl | jq '.kind' | sort | uniq -c

# Find every tool call
codevigil export session.jsonl | jq 'select(.kind == "tool_call") | .payload.tool_name'

# Dump every assistant message that contains "actually"
codevigil export session.jsonl | jq 'select(.kind == "assistant" and (.payload.text | contains("actually")))'

# Export everything in a project to a single file
codevigil export ~/.claude/projects/abc/sessions/ > all-events.ndjson
```

The `--explain` flag is plumbed through `export` for forward compatibility but currently does not change the output — the parser does not surface `intent` annotations on raw events. A future parser change can flow into export without re-wiring the dispatcher.

---

## Configuration interaction

Every subcommand reads the same effective config (see [configuration.md](configuration.md)). The precedence chain is:

1. CLI flags (highest)
2. `CODEVIGIL_*` environment variables
3. TOML config file (`--config` or `~/.config/codevigil/config.toml`)
4. Built-in defaults (lowest)

`codevigil config check` shows the resolved value and source for every key. Use it as the first step when debugging "why does codevigil think X is set to Y".

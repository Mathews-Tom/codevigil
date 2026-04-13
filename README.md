# codevigil - Session Quality, Observed

Local, privacy-preserving observability for Claude Code sessions.

codevigil tails `~/.claude/projects/**/*.jsonl` on disk, computes signal metrics about reasoning and tool-use patterns, and surfaces them in a terminal dashboard or as JSON / markdown reports. **Stdlib-only runtime, zero network egress, no data ever leaves your machine.**

Status: alpha. Python 3.11 and 3.12.

## Install

```bash
uv tool install codevigil
```

That's it. `uv tool install` puts a `codevigil` executable on your `PATH` in an isolated environment so it does not interfere with your project virtualenvs. To upgrade later, run `uv tool upgrade codevigil`. To remove it, `uv tool uninstall codevigil`.

If you don't have `uv`, install it from <https://docs.astral.sh/uv/getting-started/installation/>, or fall back to `pipx install codevigil` / `pip install --user codevigil`. See [docs/installation.md](docs/installation.md) for every supported path including from-source installs.

## First run

```bash
codevigil watch
```

Tails every active session under `~/.claude/projects` and prints a live multi-session dashboard at one frame per second. Each session shows three metrics — read/edit ratio, stop-phrase hit count, reasoning loop rate — plus a header line with parse confidence and an `[experimental thresholds]` badge while you're still inside the bootstrap window.

```text
codevigil [experimental thresholds] | parse_confidence: 1.00
session: a3f7c2d | project: my-project | 2m 34s ACTIVE
──────────────────────────────────────────────────────────────
  read_edit_ratio    5.2  OK    [R:E 5.2 | research:mut 7.1]
  stop_phrase        0    OK    [0 hits]
  reasoning_loop     6.4  OK    [6.4/1K tool calls | burst: 2]
──────────────────────────────────────────────────────────────
```

`Ctrl-C` exits cleanly. Walk through what every column means and how to interpret it in [docs/getting-started.md](docs/getting-started.md).

## What else can it do

```bash
codevigil config check               # show the resolved config and where each value came from
codevigil report ~/.claude/projects  # batch report over a tree of session files
codevigil report sessions/ --format markdown --from 2026-04-01
codevigil export session.jsonl       # NDJSON event stream on stdout, jq-friendly
codevigil export session.jsonl | jq 'select(.kind == "tool_call") | .payload.tool_name'
```

Full flag reference for every subcommand: [docs/cli.md](docs/cli.md).

## Configuration

codevigil resolves its configuration from a layered precedence chain: built-in defaults → `~/.config/codevigil/config.toml` → `CODEVIGIL_*` environment variables → CLI flags. Run `codevigil config check` to see every resolved key with its source.

A minimal `~/.config/codevigil/config.toml`:

```toml
[watch]
poll_interval = 1.0

[collectors.read_edit_ratio]
warn_threshold = 5.0
critical_threshold = 2.5
```

The complete key reference, env-var bindings, and validation rules live in [docs/configuration.md](docs/configuration.md).

## What gets measured

Three user-facing collectors plus an always-on integrity gate:

| Collector         | Signal                                                                                                         |
| ----------------- | -------------------------------------------------------------------------------------------------------------- |
| `read_edit_ratio` | Reads vs. mutations, blind-edit detection, file-tracking confidence                                            |
| `stop_phrase`     | Hits against ownership-dodging, permission-seeking, premature-stopping, and known-limitation phrase categories |
| `reasoning_loop`  | Self-correction phrase rate per 1K tool calls plus longest consecutive burst                                   |
| `parse_health`    | Always-on. Flips to CRITICAL when parse confidence drops below 0.9 in any 50-line window                       |

Threshold semantics, what each metric is sensitive to, and how to interpret CRITICAL signals: [docs/collectors.md](docs/collectors.md).

## Privacy

Three independent enforcement layers ensure session data never leaves your machine:

- **Runtime import allowlist hook** installed at package init refuses any import of `socket`, `urllib`, `http.client`, `httpx`, `requests`, `aiohttp`, `ftplib`, `smtplib`, `ssl`, `subprocess`, or related transports from inside a `codevigil` module.
- **CI grep gate** re-checks the source tree for the same banned names on every push as a belt-and-suspenders second layer.
- **Filesystem scope check** refuses any read or write path outside `$HOME` via a `Path.resolve().is_relative_to(home)` check.

The full privacy model and threat boundary: [docs/privacy.md](docs/privacy.md).

## Documentation

| Doc                                                | What it covers                                      |
| -------------------------------------------------- | --------------------------------------------------- |
| [docs/installation.md](docs/installation.md)       | Install, upgrade, uninstall, from-source builds     |
| [docs/getting-started.md](docs/getting-started.md) | First-run walkthrough and interpreting the output   |
| [docs/cli.md](docs/cli.md)                         | Exhaustive CLI reference: every subcommand and flag |
| [docs/configuration.md](docs/configuration.md)     | Every config key, env binding, and validation rule  |
| [docs/collectors.md](docs/collectors.md)           | What each metric measures and how to interpret it   |
| [docs/privacy.md](docs/privacy.md)                 | Privacy guarantees and the threat model             |
| [docs/design.md](docs/design.md)                   | Architecture, plugin boundaries, error taxonomy     |
| [CHANGELOG.md](CHANGELOG.md)                       | Release notes                                       |

## Experimental thresholds

The default v0.1 thresholds were derived from a single user's session window — one user is not a population baseline. Every default ships with `experimental = true` and the watch header shows `[experimental thresholds]` until you either flip the flag in config or let bootstrap mode personalise the thresholds for your own workflow.

Bootstrap mode observes the first 10 sessions (configurable) with all severities pinned to `OK`, records the per-collector value distributions, then derives WARN at p80 and CRITICAL at p95 of _your_ local data, clamped by the literal-value hard caps. No manual tuning required. See [docs/collectors.md#experimental-thresholds-and-bootstrap](docs/collectors.md#experimental-thresholds-and-bootstrap).

## Contributing

```bash
git clone https://github.com/Mathews-Tom/codevigil
cd codevigil
uv sync --dev
uv run pytest
uv run mypy --strict codevigil
uv run ruff check .
bash scripts/ci_privacy_grep.sh
```

All five gates must pass before a commit lands. The privacy grep runs as a separate CI job alongside the typecheck-and-test matrix on every PR.

## License

MIT. See [LICENSE](LICENSE).

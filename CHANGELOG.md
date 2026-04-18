# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`watch.allow_roots_outside_home`** (bool, default `false`). Opt-in flag that lets `watch.roots` include paths outside `$HOME`. Required for cross-environment setups such as a Windows host watching WSL's ext4 `/home/<user>/.claude/projects/` through `\\wsl.localhost\...`, a workstation observing a mounted remote dev box (SSHFS/NFS), or a multi-profile layout where two user homes sit under different parents. Settable via TOML or `CODEVIGIL_ALLOW_ROOTS_OUTSIDE_HOME` env var. The flag is threaded through both scope gates that admit watch roots — `resolve_watch_roots()` at config time and `PollingSource(...)` at runtime — so `codevigil config check`, `codevigil ingest`, and `codevigil watch` all honor the opt-in consistently. **Scoped to reads only**: `report.output_dir` and the renderer output-dir gates keep enforcing `$HOME` unconditionally, so writes cannot escape regardless of this flag. The default remains strict and both scope-violation error messages now name the flag so users discover the opt-in rather than guessing. Resolves the cross-host gap left open by #44 on top of v0.4.0's `watch.roots` plumbing.

### Fixed

- **`codevigil config check` now surfaces watch-root scope and overlap violations.** Previously `config check` returned 0 for configs that `ingest` / `watch` / `report` would subsequently reject with `config.watch_root_scope_violation` or `config.overlapping_watch_roots`. `config check` now calls `resolve_watch_roots()` after the loader so users discover a bad config at check time, not at first real invocation. Exit code is `2` on violation, matching the behaviour of the commands that consume the resolved roots.

## [0.3.0] - 2026-04-16

This release graduates codevigil from a live-only watch dashboard to a persistent-memory tool with cold-ingest, cohort trend analysis at parity with anthropics/claude-code#42796, and a project-level watch roll-up. The 0.2.x watch command is preserved behind `--by-session`; the new default collapses multi-session projects into a single row.

### Added

- **Persistent processed-session store (SQLite)** (`analysis/processed_store.py`). `codevigil watch` now writes a durable record for every finalised session under `~/.local/state/codevigil/processed/` — session id, file id, cursor offset, collector state snapshot, and derived metric summary. The watcher seeds each polled file from the stored cursor so restarts no longer re-parse JSONL from byte 0. The collector state is restored verbatim so rolling windows survive across process restarts. See `docs/design.md §Persistent Memory` for schema, migration policy, and the key invariants the store upholds.
- **`codevigil ingest` subcommand**. One-shot cold-ingest of every JSONL session under `watch.root` into the persistent store. Intended to be run once after install (or once after any `--force` rebuild) so subsequent `codevigil watch` ticks only process newly-appended events. Flags: `--db PATH` (override the store path) and `--force` (re-ingest every session, ignoring existing rows). Missing-store bootstrap is automatic: `codevigil watch` will invoke the ingest flow on first run if the store is absent.
- **Per-file cursor cache** (`watcher_cache.py`). Opt-in-by-default persistent cache under `~/.local/state/codevigil/` that records `(inode, mtime, size, byte_offset)` per watched file. The watcher consults the cache on startup and seeks past already-processed bytes. Disable with `watch.cursor_cache_enabled = false` for fully reproducible cold-start benchmarks. Cache location is configurable via `watch.cursor_cache_dir`.
- **Project-row watch view** (`renderers/terminal.py`). The default `codevigil watch` layout now rolls up every active session belonging to the same project into a single row showing the fleet-worst severity, the active session count, and the aggregate metric summary. The previous one-block-per-session layout is available via the new `--by-session` CLI flag or by setting `watch.display_mode = "session"` in config. `watch.display_project_limit` (default `10`) caps the number of project rows rendered per frame and is analogous to `watch.display_limit` for session mode.
- **`thinking` collector** (`collectors/thinking.py`). Observes `EventKind.THINKING` events and exposes the visible-vs-redacted ratio plus median visible-block and signature-block character lengths. Headline signal for the #42796 "thinking depth decline" cohort analysis. Severity is always OK by design — the collector is descriptive, not a quality gate.
- **`prompts` collector** (`collectors/prompts.py`). Counts user-message turns per session. Primary scalar feeds the #42796 "prompts per session" per-week cohort mean. Severity is always OK.
- **Both new collectors are enabled by default.** `collectors.enabled` now ships as `["read_edit_ratio", "stop_phrase", "reasoning_loop", "thinking", "prompts"]` so cohort reports surface the #42796 signals out of the box. Each has its own `[collectors.thinking]` / `[collectors.prompts]` config section with an `experimental` flag.
- **`codevigil report --pivot-date YYYY-MM-DD`** splits the corpus into `before` / `after` buckets (after-bucket inclusive) and emits a Before/After delta table. Incompatible with `--group-by` and `--compare-periods`.
- **`codevigil report --experimental-correlations`** appends a Pearson correlation matrix across per-session metrics to the cohort report. Pairs below `MIN_PAIRS=30` are dropped; zero-variance columns are skipped. Surfaces with an unavoidable "exploratory signal, not causal evidence" disclaimer and is marked `[experimental]` in output.
- **`codevigil report --output-file PATH`** writes the rendered report to an exact file path instead of the computed `<output_dir>/report*.{json,md,txt}` default. Parent directories are created if missing; the path must resolve under `$HOME`. Mutually exclusive with `--output`.
- **`codevigil report --format csv`** on the cohort path. Emits a flat `dimension_value,metric_name,mean,stdev,n,min,max` schema for notebook consumption. Per-session JSON / Markdown remain unchanged.
- **Cohort report methodology header** on every `--group-by` report. Shows corpus size, date range, cell count, and the cohort JSON schema version so readers know the data shape before reading the table.
- **Δ-vs-prior-row annotations** on chronological dimensions (`day`, `week`) for each cell with `n≥5`, plus **threshold highlighting** that renders cells crossing the warn / critical thresholds in bold (critical cells carry a leading asterisk). Direction is per-metric (`parse_health` low-is-bad; `reasoning_loop` / `stop_phrase` high-is-bad).
- **Cohort column injectors** expose four new metric columns: `blind_edit_rate`, `research_mutation_ratio`, `thinking_visible_ratio` (+ two chars_median companions), and `user_turns`. Column headers are decoupled from collector internals so naming stays self-explanatory.
- **Claim-discipline renderer gate** (`tests/report/test_claim_discipline.py`). Every public cohort renderer is walked and any output containing banned causal language (`because of`, `due to`, `results in`, `responsible for`, etc.) fails the build. Correlation-not-causation enforced at test time.

### Changed

- **Default watch layout is now the project roll-up.** `codevigil watch` with no flags renders one row per project; pass `--by-session` for the 0.2.x-style per-session blocks. Scripts screen-scraping the watch output must either pin `--by-session` or migrate to the new layout.
- **`collectors.enabled` default now includes `thinking` and `prompts`.** Users with an explicit `collectors.enabled` in their config file are unaffected — their override wins. Users relying on the default will see two new collector blocks in watch output and two new metric columns in cohort reports after upgrading.

### Fixed

- **Stale cursor seeds on file replacement** (`watcher.py`, `watcher_cache.py`). When a session JSONL was replaced in place (e.g., by Claude Code compaction writing a new file with the same path but a different inode / smaller size), the cursor cache previously seeded the new file from the old byte offset, silently skipping the early events. The cache loader now refuses any seed whose `(inode, size)` fingerprint does not match the current file on disk and falls back to byte 0 with a single-line WARN naming the replaced path. A regression test at `tests/test_watcher_cursor_cache.py` covers the inode-change and shrink-below-offset cases.

### Developer experience

- Large-scale complexity reduction across `cli.py`, `config.py`, `parser.py`, `report/`, `history/`, `analysis/`, `collectors/`, and `renderers/`. No behavioural change — pure extraction of helpers, defaultdict adoption, nesting flattening. `mypy --strict` still clean; every pre-existing test passes unchanged.
- New integration and unit tests: `tests/test_cli_ingest.py`, `tests/test_processed_session_store.py`, `tests/test_watcher_cursor_cache.py`, `tests/test_watch_resume_collector_state.py`, `tests/test_watcher_timing_instrumentation.py`, `tests/test_watcher_unresolved_timestamp.py`, `tests/test_collector_state_serialization.py`, `tests/test_parser_structural_kinds.py`, `tests/renderers/test_terminal_project_view.py`, `tests/renderers/test_terminal_store_overlay.py`, `tests/cli/test_report_output_file.py`, `tests/cli/test_watch_instant_shutdown.py`.

## [0.2.1] - 2026-04-15

### Added

- **`watch.display_limit` config key** (default `20`, range `[1, 500]`). Caps the `codevigil watch` dashboard to the top-N session blocks per frame, ranked by severity then recency. When the active set exceeds the cap, a footer line reports how many sessions were omitted and reminds you how to raise the limit. Env binding: `CODEVIGIL_WATCH_DISPLAY_LIMIT`.

### Changed

- **Fleet header `updated=` now reflects render wall-clock tick time.** Previously the timestamp was frozen at the newest `last_event_time` across all session files — a historical-event peak that never advanced. It now shows the time the most recent frame was rendered, giving a true liveness indicator.

### Fixed

- **Cold-start lifecycle classification.** `codevigil watch` no longer flags historical replayed sessions as ACTIVE. On the first lifecycle tick after startup, sessions are classified using their actual last-event age against the existing 5-min / 35-min stale / evict thresholds. Sessions from hours or days ago immediately appear as STALE or EVICTED rather than filling the active-set count with ghost sessions.

- **`parse_confidence` on older Claude Code JSONL files.** The parser now recognises additional historical JSONL shapes that were missing from the original fingerprint table: `ts`/`session` timestamp aliases, top-level `role` kind promotion, and flat-content records carrying `text`, `tool`, `tool_input`, or `tool_result` keys without the `message` wrapper. Session stores that previously reported `parse_confidence ≈ 0.31` on older files should now report `≥ 0.9`.

- **Non-determinism in `test_20_session_fixture_renders_deterministically`.** The wall-clock `updated=` header change caused this test to fail intermittently because two `TerminalRenderer` instances produced frames at different wall-clock instants. The test now injects a pinned fixed clock on both renderer instances so the comparison is stable.

## [0.2.0] - 2026-04-14

### Added

- **Turn abstraction and TurnGrouper sidecar** (`aggregator.py`). The aggregator now groups raw events into completed `Turn` dataclass instances inside each `_SessionContext`. A turn spans one user message and the assistant's complete response. Completed turns are accumulated in `_SessionContext.completed_turns` and serialised into `SessionReport.turns` at eviction.
- **Turn-level task classifier** (`classifier.py`). A two-stage cascade classifier assigns one of five category labels — `exploration`, `mutation_heavy`, `debug_loop`, `planning`, `mixed` — to each completed turn. Stage 1 applies tool-presence heuristics (mutation count, bash count, read/glob dominance). Stage 2 applies keyword regex against the user message text when Stage 1 is ambiguous. Session-level label is the majority-vote aggregate across all classified turns.
- **Classifier calibration gate** (`tests/test_classifier_calibration.py`). Asserts ≥ 85% agreement between the classifier and a hand-labeled fixture corpus. Fails the build if not met. Calibration script at `scripts/calibrate_classifier.py` writes a confusion matrix to `.docs/classifier-calibration.md`.
- **`session_task_type` and `turn_task_types` fields on `SessionReport`**. Additive optional fields; pre-0.2.0 records read back as `None` without error. No migration required.
- **`history list --task-type NAME` filter**. Filters stored sessions by classifier-derived task type label. Sessions with no task type are excluded. Requires `classifier.enabled = true`.
- **`task_type [experimental]` column in `history list`**. Hidden entirely when no session in the result set carries a task type, preserving backward compatibility with stores created before the classifier was enabled.
- **`history heatmap --axis task_type`** [experimental]. Cross-tabulates metric means across all stored sessions grouped by classifier task type. Sessions with no task type appear under `(unclassified)`. Exits 1 with a descriptive error if the classifier is disabled.
- **Task type in `history <SESSION_ID>` detail view**. A "Turn Task Types" panel shows per-turn labels. Session-level label appears in the header as `task_type: <label> [experimental]`. Both surfaces are absent when the classifier was disabled at capture time.
- **Task type tag in `codevigil watch` session header**. `[task: <label>] [experimental]` appears right-aligned when a task type has been derived from the session's completed turns.
- **Proportional gradient bars in `history heatmap` cells**. Each cell now renders a 9-glyph smooth gradient bar scaled to the cell value relative to the column maximum, replacing the raw scalar with a visual magnitude signal.
- **Message-ID deduplication in `SessionParser`**. The parser now tracks seen message IDs and skips duplicate entries. This is an additive correctness fix — see the "Correctness fix callout" below.
- **`[classifier]` configuration section**. Two new config keys: `classifier.enabled` (bool, default `true`) and `classifier.experimental` (bool, default `true`). See [docs/configuration.md](docs/configuration.md#classifier) and [docs/classifier.md](docs/classifier.md) for full documentation.

### Changed

- **`codevigil report` default behavior changed**. When invoked with no `--from` or `--to` flags, `report` now runs in **multi-period mode**: it computes three windows relative to now — `today`, `7d`, and `30d` — and renders three stacked summaries. JSON output is `{"today": [...], "7d": [...], "30d": [...]}`. The output file is `report_multi_period.json` (or `report_multi_period.txt` for markdown).

  > **Migration note for scripts and CI pipelines.** Any script that relied on the previous default behavior (a flat per-session report with no date filter) must add `--from 1970-01-01` to explicitly request single-period mode. The single-period output format and file names (`report.json` / `report.md`) are unchanged when `--from` or `--to` is supplied.

- **`--from`/`--to` filtering is now event-level, not session-level**. Events are filtered individually by timestamp. Sessions that straddle a date boundary contribute only their in-window events; `started_at`/`ended_at` are clamped to the first/last in-window event. Sessions with zero in-window events are omitted entirely.

  > **Correctness fix callout.** Previous versions dropped or kept entire sessions based on the session's first event timestamp. The new per-event filtering means that reports generated with narrow date windows over sessions that straddle those windows will show different (lower) event counts and metric values. This is the correct behaviour.

### Fixed

- **Message-ID deduplication** (`parser.py`). The parser previously emitted duplicate `Event` objects when Claude Code compaction rewrote the session JSONL with overlapping entries. Seen message IDs are now tracked per-file; duplicates are silently discarded.

  > **Correctness fix callout.** Sessions that underwent compaction will show lower event counts and potentially different metric values (particularly `parse_health.duplicate_count`) after upgrading to 0.2.0. This is the correct behaviour — the prior metric values were inflated by duplicates.

- **Entry-level date filtering** (`report.py`). The previous implementation filtered sessions by the session's first event timestamp rather than filtering individual events. The fix aligns filtering with documented semantics.

  > **Correctness fix callout.** Reports generated over sessions that straddle a `--from`/`--to` window will show lower event counts after upgrading. This is the correct behaviour. Scripts that used narrow date windows and compared output against a pre-0.2.0 baseline should re-generate their baselines.

## [0.1.1] - 2026-04-13

License-metadata correction. The `0.1.0` wheel published to PyPI declared
`License :: OSI Approved :: MIT License` in its classifiers but the bundled
`LICENSE` file was Apache License 2.0 — the file content has been Apache 2.0
since the first commit of the repository and the MIT references in the docs
and packaging metadata were a mistake. `0.1.1` reconciles every reference to
Apache License 2.0 to match the actual `LICENSE` file. No runtime behaviour
changed.

### Fixed

- `pyproject.toml` classifier: `MIT License` → `Apache Software License`.
- `README.md` license section: MIT → Apache License 2.0.
- `docs/design.md` repo notes: MIT → Apache License 2.0.

### Deprecated

- `0.1.0` should not be installed. The wheel is functional but its declared
  license metadata is internally inconsistent. Users should pin `>=0.1.1`.

## [0.1.0] - 2026-04-13

Initial alpha release. Stdlib-only runtime, Python 3.11+, zero network egress.

### Added

- Session parser with schema fingerprints and a `ParseHealthCollector` that
  surfaces per-file parse confidence and degradation.
- `PollingSource` watcher with rotation, truncate, and delete handling plus a
  filesystem scope gate that refuses any root outside `$HOME`.
- `SessionAggregator` managing per-session lifecycle, collector instances,
  and structured error routing (no silent failures).
- `ReadEditRatioCollector`, `StopPhraseCollector`, and `ReasoningLoopCollector`
  as the v0.1 signal set.
- `TerminalRenderer` for the live watch dashboard and `JsonFileRenderer` for
  batch report output.
- CLI surface: `codevigil watch`, `codevigil report`, `codevigil export`, and
  `codevigil config check`, plus the global `--config` and `--explain` flags.
- `BootstrapManager` that observes the first N sessions to derive personalised
  percentile-based thresholds clamped by literal-value hard caps.
- v0.1 fixture corpus and integration tests running every collector end to
  end against the corpus.
- Deterministic session anonymiser so fixtures can be re-derived without
  leaking operator-specific content.

### Privacy

- Runtime import allowlist hook installed at package init blocks any codevigil
  module from importing `socket`, `urllib`, `http.client`, `httpx`, `requests`,
  `aiohttp`, `ftplib`, `smtplib`, `ssl`, `subprocess`, or related transports.
- CI grep gate (`scripts/ci_privacy_grep.sh`) re-checks the tree for the same
  banned names as a belt-and-suspenders second layer against the runtime hook.
- Filesystem scope check refuses any read or write path outside the user's
  home directory via `Path.resolve().is_relative_to(Path.home())`.

### Documented limitations

- Terminal renderer does full screen redraws on every tick, not diffed
  updates. Flicker is possible on slow SSH or high-latency tmux links. A
  `rich`-based diff renderer is the v0.2 upgrade path.
- All collector thresholds are experimental until bootstrap mode completes.
  The watch header shows `[experimental thresholds]` until the user sets
  `experimental = false` or bootstrap has observed enough sessions.
- No `inotify` / `fsevents` integration; the watcher is polling-only.
- Single-process tick loop. No concurrent rendering or multi-host fan-in.

[Unreleased]: https://github.com/Mathews-Tom/codevigil/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Mathews-Tom/codevigil/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/Mathews-Tom/codevigil/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Mathews-Tom/codevigil/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/Mathews-Tom/codevigil/releases/tag/v0.1.1
[0.1.0]: https://github.com/Mathews-Tom/codevigil/releases/tag/v0.1.0

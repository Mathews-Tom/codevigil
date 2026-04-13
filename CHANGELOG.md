# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

no changes yet.

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

[Unreleased]: https://github.com/Mathews-Tom/codevigil/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Mathews-Tom/codevigil/releases/tag/v0.1.1
[0.1.0]: https://github.com/Mathews-Tom/codevigil/releases/tag/v0.1.0

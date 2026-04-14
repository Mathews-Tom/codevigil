# codevigil — System Design

## Problem

Claude Code session quality degrades silently. Users have no instrumentation to detect, measure, or respond to quality regressions in real-time. The only existing approach (stellaraccident's manual JSONL analysis) required months of logs and significant engineering effort to produce after-the-fact.

codevigil makes session quality observable.

## Non-Goals (v0.1)

- Auto-remediation (hooks that inject corrections)
- Convention drift scoring (requires CLAUDE.md parsing + edit diffing)
- GUI / web dashboard
- Cloud telemetry or any network calls
- Multi-user or team features

## Architecture

```mermaid
flowchart TD
    CLI["CLI<br/>(watch | report | export | history)"]

    CLI --> Watcher["Watcher<br/>PollingSource → inotify/fsevents"]
    CLI --> Batch["BatchReader<br/>glob + read"]
    CLI --> HistoryDispatch["history/<br/>list | detail | diff | heatmap"]

    Watcher --> Parser
    Batch --> Parser

    Parser["Parser<br/>JSONL → EventStream<br/>schema fingerprint + parse_confidence"]
    Parser -->|Event| CollectorBoundary

    subgraph CollectorBoundary["Collectors — plugin boundary"]
        direction LR
        C1["ReadEditRatio"]
        C2["StopPhrase"]
        C3["ReasoningLoop"]
        C4["ParseHealth<br/>built-in, always on"]
    end

    CollectorBoundary -->|MetricSnapshot| Aggregator

    Aggregator["Aggregator<br/>session lifecycle<br/>tick loop + error routing"]

    Aggregator -->|snapshots + SessionMeta| RendererBoundary
    Aggregator -.->|"opt-in persistence<br/>[storage] enable_persistence"| SessionStore

    subgraph RendererBoundary["Renderers — plugin boundary"]
        direction LR
        R1["Terminal"]
        R2["JSON file"]
        R3["Future:<br/>MCP / Hook / Dashboard"]
    end

    SessionStore[("SessionStore<br/>~/.local/state/codevigil/sessions/")]
    SessionStore --> AnalysisPkg["analysis/<br/>cohort · compare · guards"]
    AnalysisPkg --> CohortReport["Cohort Report<br/>--group-by | --compare-periods"]
    HistoryDispatch --> SessionStore

    Aggregator -.->|CodevigilError| ErrorChannel[("Error channel<br/>log file + stderr")]

    classDef boundary fill:#1f2937,stroke:#60a5fa,stroke-width:2px,color:#f8fafc;
    classDef core fill:#0f172a,stroke:#94a3b8,color:#f8fafc;
    classDef err fill:#450a0a,stroke:#f87171,color:#fecaca;
    classDef store fill:#14532d,stroke:#4ade80,color:#f8fafc;
    class CollectorBoundary,RendererBoundary boundary;
    class CLI,Watcher,Batch,Parser,Aggregator,HistoryDispatch,AnalysisPkg,CohortReport core;
    class ErrorChannel err;
    class SessionStore store;
```

### Why This Shape

Three extension axes. Collectors and renderers are the two axes of real-time expansion. New metric = new collector, no existing code touched. New output target = new renderer, same deal. The third axis — retrospective analysis — runs off the `SessionStore`: new group-by dimensions and new report renderers layer onto `analysis/` without touching the live pipeline. The parser-to-collector interface (`Event`) and the collector-to-renderer interface (`MetricSnapshot`) are the two contracts that need to stay stable across all three axes.

## Core Abstractions

### Event

The parser's output. Every JSONL entry becomes one or more typed Events. This is the internal lingua franca — collectors never touch raw JSONL.

```python
@dataclass(frozen=True, slots=True)
class Event:
    timestamp: datetime
    session_id: str
    kind: EventKind
    payload: dict[str, Any]

class EventKind(Enum):
    TOOL_CALL = "tool_call"          # any tool invocation
    TOOL_RESULT = "tool_result"      # tool response
    ASSISTANT_MESSAGE = "assistant"  # model text output
    USER_MESSAGE = "user"            # user prompt
    THINKING = "thinking"            # thinking block (content or redacted)
    SYSTEM = "system"                # system/meta events
```

Payload is intentionally unstructured at the type level — each EventKind has an **explicit documented schema** (below) but we don't enforce it with dataclasses to avoid a type explosion as kinds grow. Collectors never reach into `payload` directly; they use the `safe_get` helper in `types.py`:

```python
def safe_get(payload: dict, key: str, default: Any, expected: type | None = None) -> Any:
    """Returns payload[key] if present and type-matches, else default. Logs a WARN
    to the error channel on missing-expected or type-mismatch so drift is observable."""
```

This turns every silent `KeyError` or type mismatch into a counted, reportable event (see **Error Taxonomy**). A collector that starts seeing >5% `safe_get` miss-rate on a required field is a parse-drift signal — surfaced via the `parse_confidence` meta-metric.

#### Payload Schemas by EventKind

| EventKind           | Required keys                                       | Optional keys                                                                                                                                               |
| ------------------- | --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `TOOL_CALL`         | `tool_name: str`, `tool_use_id: str`, `input: dict` | `file_path: str` (extracted from input when applicable)                                                                                                     |
| `TOOL_RESULT`       | `tool_use_id: str`, `is_error: bool`                | `output: str`, `truncated: bool`                                                                                                                            |
| `ASSISTANT_MESSAGE` | `text: str`                                         | `token_count: int`                                                                                                                                          |
| `USER_MESSAGE`      | `text: str`                                         | —                                                                                                                                                           |
| `THINKING`          | `length: int`                                       | `signature: str`, `redacted: bool`, `text: str` (reserved for v0.2 `thinking_depth` collector; always populated when available, ignored by v0.1 collectors) |
| `SYSTEM`            | `subkind: str`                                      | arbitrary                                                                                                                                                   |

Reserving the `THINKING` payload now (even though v0.1 ships no collector that reads it) avoids a schema migration when `thinking_depth` lands in v0.2.

Trade-off: we lose compile-time payload validation. Acceptable at this scale. If payload diversity grows past ~10 kinds, introduce typed payload dataclasses behind a discriminated union.

### Collector (Protocol)

```python
class Collector(Protocol):
    name: str
    complexity: str  # documented big-O per ingest, e.g. "O(1)" or "O(phrases * text_len)"

    def ingest(self, event: Event) -> None:
        """Process a single event. Must not raise; must not block."""
        ...

    def snapshot(self) -> MetricSnapshot:
        """Return current state. Idempotent and cheap; safe to call at any frequency."""
        ...

    def reset(self) -> None:
        """Clear state. Called ONLY on session boundary transitions (see lifecycle below)."""
        ...
```

Collectors are stateful, single-threaded, and own their windowing logic. The aggregator calls `snapshot()` on a timer or on-demand — collectors don't decide when to report.

#### Lifecycle Contract

`reset()` is called by the aggregator **only** at session boundaries:

1. When a new session file is first observed (fresh collector instance, `reset()` is a no-op but defined for symmetry).
2. When a session is evicted from the active set (see **Stale Session Policy**).
3. Never mid-session. Rolling windows (e.g., `read_edit_ratio`'s 50-event deque) MUST NOT be cleared by `snapshot()` or by tick cadence — doing so would mask the degradation the metric is designed to detect.

Collectors that need per-session state use one instance per session; the aggregator manages the `dict[session_id, dict[collector_name, Collector]]` map.

#### Complexity Honesty

The earlier draft claimed "O(1) amortized" for all collectors. That's false for text-scanning collectors. The real contract:

| Collector         | Per-ingest cost                                                                                                                                     |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `read_edit_ratio` | O(1) — deque append + counter update                                                                                                                |
| `stop_phrase`     | O(P·L) with P = phrase count, L = message length. Switches to Aho–Corasick automaton (stdlib-implementable) once P > 32 to bound to O(L + matches). |
| `reasoning_loop`  | O(P·L) with same escalation rule                                                                                                                    |
| `blind_edit_rate` | O(W) with W = lookback window size (default 20)                                                                                                     |

Document the throughput ceiling in the README: at P=50, L=2000, the naive scan is ~5M char-compares per session of 50 assistant messages — well under a second, but not O(1). The Aho–Corasick escalation is the upgrade path if user phrase lists grow large.

Each collector declares its `name` as a string key. Snapshots are keyed by this name in the aggregated output. Collision is caught at registry load time (see **Registry Validation**), not runtime.

### MetricSnapshot

```python
@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    name: str
    value: float                           # primary scalar (for threshold checks)
    label: str                             # human-readable summary, e.g. "R:E 3.2"
    detail: dict[str, Any] | None = None   # optional structured breakdown
    severity: Severity = Severity.OK

class Severity(Enum):
    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"
```

`value` is always a float. This is a deliberate constraint — it forces every metric to have a single primary scalar that can be thresholded, trended, and compared. Rich data goes in `detail`.

`severity` is computed by the collector against its own configured thresholds. The renderer uses it for coloring/alerting but doesn't interpret the value itself.

### SessionMeta

```python
@dataclass(frozen=True, slots=True)
class SessionMeta:
    session_id: str          # file stem (see Session Identification)
    project_hash: str        # parent dir name under ~/.claude/projects
    project_name: str | None # resolved via ProjectRegistry; None if unmapped
    file_path: Path
    start_time: datetime     # first event timestamp observed
    last_event_time: datetime
    event_count: int
    parse_confidence: float  # 0.0–1.0, emitted by parser (see Parser Design)
    state: SessionState      # ACTIVE | STALE | EVICTED
```

SessionMeta is produced by the aggregator, not by collectors. It accompanies every render call so renderers never need to reach back into the event stream or filesystem.

### Renderer (Protocol)

```python
class Renderer(Protocol):
    name: str

    def render(self, snapshots: list[MetricSnapshot], meta: SessionMeta) -> None:
        """Output the current state. Called on aggregator tick. Must not raise."""
        ...

    def render_error(self, err: CodevigilError, meta: SessionMeta | None) -> None:
        """Surface an error from parser/watcher/collector. See Error Taxonomy."""
        ...

    def close(self) -> None:
        """Flush any buffered output. Called on CLI exit or session eviction."""
        ...
```

Renderers are stateless w.r.t. metric values but may hold output handles (file descriptors, terminal state). They receive the full snapshot list for a single session on every tick. Multi-session composition is the aggregator's job, not the renderer's — this keeps renderers simple and composable.

Terminal renderer clears and redraws (see **Watch Mode UX Limitations**). JSON renderer appends NDJSON to a rotating file. Future MCP renderer pushes to a local server.

## Module Layout

```text
codevigil/
├── __init__.py              # installs privacy import hook
├── __main__.py              # CLI entrypoint
├── cli.py                   # argparse, mode dispatch
├── parser.py                # JSONL → Event stream, schema fingerprints
├── watcher.py               # Source protocol + PollingSource
├── aggregator.py            # collector orchestration, session lifecycle, error routing
├── errors.py                # CodevigilError hierarchy, ErrorLevel, log writer
├── privacy.py               # import allowlist hook, path scope checks
├── registry.py              # shared collector/renderer registry validation
├── projects.py              # ProjectRegistry (hash → name resolution)
├── types.py                 # Event, EventKind, MetricSnapshot, Severity,
│                            #   SessionMeta, SessionState, safe_get helper
├── collectors/
│   ├── __init__.py           # collector registry
│   ├── parse_health.py       # built-in, always enabled
│   ├── read_edit_ratio.py
│   ├── stop_phrase.py
│   └── reasoning_loop.py
├── renderers/
│   ├── __init__.py           # renderer registry
│   ├── terminal.py
│   └── json_file.py
└── config.py                # TOML loader, precedence resolution, validation
```

### Why Flat Packages

`collectors/` and `renderers/` are the only subdirectories. Everything else is top-level. This keeps import paths short and avoids premature layering. If we later need `sources/` (for non-JSONL inputs) or `hooks/` (for Claude Code hook integration), they slot in at the same level.

### Registry Pattern

Both `collectors/__init__.py` and `renderers/__init__.py` export a registry dict built by scanning the package. Adding a new collector = add a file + add one entry to the registry. No wiring code elsewhere.

```python
# collectors/__init__.py
COLLECTORS: dict[str, type[Collector]] = {
    "read_edit_ratio": ReadEditRatioCollector,
    "stop_phrase": StopPhraseCollector,
    "reasoning_loop": ReasoningLoopCollector,
}
```

Config enables/disables collectors by name. Unknown names in config are errors, not silently ignored.

## v0.1 Collectors

### Thresholds Are Experimental

All default thresholds in this section come from a single data point: stellaraccident's post-hoc analysis (6.6 R:E healthy → 2.0 degraded; 8.2 loop-rate healthy → 21.0 degraded). **One user's session window is not a population baseline.** Shipping these as authoritative would produce false positives in legitimate contexts: tight debugging loops genuinely invert R:E, careful reasoning genuinely uses "actually", refactor sprints are edit-heavy by design.

v0.1 addresses this in three ways:

1. **Thresholds are marked `experimental = true` in config** and the watch-mode header shows a `[experimental thresholds]` badge until the user explicitly sets `experimental = false`.
2. **Bootstrap mode.** On first run, codevigil observes N sessions (default N=10, configurable) with severity pinned to `OK` and records per-collector value distributions. After bootstrap, defaults shift to percentile-based thresholds: WARN at p80, CRITICAL at p95 of the local distribution, clamped by hard caps from the literal-value defaults below. This personalizes signal to the actual workflow without requiring manual tuning.
3. **Calibration dataset.** The repo ships with an anonymized fixture set (see **Testing Strategy → Fixture Sourcing**) so thresholds can be re-derived as more data becomes available.

The literal defaults below are the hard caps and the fallback when bootstrap is disabled.

### 1. ReadEditRatioCollector

Tracks file-level tool calls. Classifies each tool call as:

| Tool                           | Classification |
| ------------------------------ | -------------- |
| `Read` / `View`                | read           |
| `Grep` / `Glob` / `LS`         | research       |
| `Edit` / `Write` / `MultiEdit` | mutation       |
| Everything else                | other          |

Computes:

- **read_edit_ratio**: reads / edits (rolling window, default 50 tool calls)
- **research_mutation_ratio**: (reads + research) / mutations
- **blind_edit_rate**: edits where the target file was not read in the last N tool calls
- **blind_edit_tracking_confidence**: fraction of edit events for which the collector could resolve the target file path from the tool input payload. If this drops below 0.95, the `blind_edit_rate` snapshot is emitted with `severity=OK` and `label="insufficient data"` — a low-confidence metric must not fire a CRITICAL. The confidence itself is surfaced as `detail["tracking_confidence"]` so renderers can show a dim indicator when the collector has gone partially blind.

Thresholds (configurable):

- OK: R:E ≥ 4.0
- WARN: 2.0 ≤ R:E < 4.0
- CRITICAL: R:E < 2.0

These defaults come directly from stellaraccident's data: 6.6 was healthy, 2.0 was degraded.

### 2. StopPhraseCollector

Pattern-matches against assistant message text. Default phrase list (from the issue's stop-phrase-guard.sh categories):

```text
ownership_dodging:
  - "not caused by my changes"
  - "existing issue"
  - "pre-existing"
  - "outside the scope"

permission_seeking:
  - "should I continue"
  - "want me to keep going"
  - "shall I proceed"
  - "would you like me to"

premature_stopping:
  - "good stopping point"
  - "natural checkpoint"
  - "let's pause here"

known_limitation:
  - "known limitation"
  - "future work"
  - "out of scope"
  - "beyond the scope"
```

Users add custom phrases via config. Matching is case-insensitive with **word-boundary anchoring**: internally each phrase compiles to `re.compile(r'(?<!\w)' + re.escape(phrase) + r'(?!\w)', re.IGNORECASE)`. This prevents the classic substring trap where `"should I"` matches `"shoulder inflammation"`. Users who want true substring behaviour can opt in per phrase via a `{phrase = "...", mode = "substring"}` table form in config.

Each phrase entry carries an intent annotation so custom additions don't drift from the categories' original meaning:

```toml
[[collectors.stop_phrase.phrases]]
text = "actually,"
category = "reasoning_loop"
intent = "self-correction after a clause boundary; the trailing comma is load-bearing"
```

The `intent` field is documentation, not logic — it surfaces in `--explain` output so users can audit why a phrase matched.

Computes:

- **hit_rate**: matches per 1K tool calls
- **hits_by_category**: breakdown by category
- **recent_hits**: last 5 matches with timestamps and matched phrase

Thresholds:

- OK: 0 hits in current session
- WARN: 1-5 hits
- CRITICAL: >5 hits

### 3. ReasoningLoopCollector

Counts self-correction patterns in assistant messages:

```text
patterns:
  - "oh wait"
  - "actually,"
  - "let me reconsider"
  - "hmm, actually"
  - "no wait"
  - "I was wrong"
  - "let me rethink"
  - "on second thought"
```

Computes:

- **loop_rate**: matches per 1K tool calls
- **max_burst**: highest count in a single message

Thresholds:

- OK: < 10 per 1K
- WARN: 10-20 per 1K
- CRITICAL: > 20 per 1K

Baseline from the issue: 8.2 (good) → 21.0 (degraded). These are experimental defaults — see **Thresholds Are Experimental** above. The reasoning-loop patterns use the same word-boundary matching as stop phrases to avoid false positives on `"actually"` inside `"the actually-correct answer"`.

## Parser Design

### JSONL Structure

Claude Code session files live at `~/.claude/projects/<project-hash>/sessions/<session-id>.jsonl`. Each line is a JSON object. The parser needs to handle:

- **assistant turns**: `{"type": "assistant", "message": {...}}` — contains tool_use blocks and text blocks
- **user turns**: `{"type": "user", "message": {...}}`
- **thinking blocks**: nested inside assistant messages, `{"type": "thinking", "thinking": "..." | "[redacted]"}`
- **tool results**: `{"type": "tool_result", ...}`
- **system events**: session start/end markers

### Parsing Strategy

The parser is a generator that yields `Event` objects. It handles malformed lines gracefully (log + skip, never crash). It tracks enough state to associate tool results with their originating tool calls via `tool_use_id`.

```python
def parse_session(lines: Iterable[str]) -> Iterator[Event]:
    ...
```

This signature works for both batch (read file) and streaming (tail file). The caller decides the source; the parser doesn't care.

### Schema Evolution and Drift Detection

Claude Code's JSONL schema has changed before and will change again. "Defensive parsing" alone is not enough — a silently missing field turns a collector blind without any user-visible symptom. The parser implements **active drift detection**:

1. **Parse confidence.** For each expected-but-missing field, the parser increments a per-session counter. `parse_confidence = 1.0 - (missing / expected)` is attached to every emitted Event (via `SessionMeta.parse_confidence`) and is itself exposed as a meta-metric via the built-in `ParseHealthCollector` (always enabled, cannot be disabled).
2. **Drift thresholds.** If parse_confidence drops below 0.90 in any 50-event window, `ParseHealthCollector` emits a `CRITICAL` snapshot with `label="schema drift detected"` and `detail={"missing_fields": {...counts...}}`. This is the loud signal a silent break would otherwise eat.
3. **Schema fingerprint.** At session start the parser samples the first 10 events and records a fingerprint `(set of observed top-level keys, set of observed type values)`. A fingerprint change across session starts is logged at WARN via the error channel with the diff, so users see schema evolution between Claude Code versions without grepping logs.
4. **Version epoch.** Until Claude Code ships an explicit format version field, codevigil maintains a `KNOWN_FINGERPRINTS: dict[str, SchemaEpoch]` table in `parser.py`. Fingerprints observed in the wild are committed with a date stamp. Unknown fingerprints trigger a one-time WARN per-run ("new Claude Code session schema observed — please open an issue with fingerprint X").

Defensive parsing still handles the per-event case (log + skip malformed line, never crash), but drift is treated as a first-class observable, not a hope.

## Watcher Design

### Source Protocol

Watcher is itself a protocol, not just an implementation, so v0.2 inotify/fsevents backends and test-time fake sources drop in without touching the aggregator.

```python
class Source(Protocol):
    def poll(self) -> Iterator[SourceEvent]:
        """Yield SourceEvents since the last call. Must not block."""
        ...

    def close(self) -> None: ...

@dataclass(frozen=True, slots=True)
class SourceEvent:
    kind: SourceEventKind   # NEW_SESSION | APPEND | ROTATE | TRUNCATE | DELETE
    session_id: str
    file_path: Path
    inode: int
    lines: list[str]        # complete JSONL lines only; never partial
```

### v0.1: Poll-Based PollingSource

```python
class PollingSource:
    def __init__(self, root: Path, interval: float = 2.0):
        # state: dict[session_id, FileCursor]
        ...
```

Each tracked file has a `FileCursor`:

```python
@dataclass
class FileCursor:
    path: Path
    inode: int          # identity across rotation
    offset: int         # byte offset of last fully-consumed newline
    pending: bytes      # bytes read past last newline, not yet emitted
```

On each poll cycle the watcher:

1. Enumerates `root/**/sessions/*.jsonl` with a bounded walk (hard cap 2000 files; overflow WARNs once per run).
2. For each file, `os.stat()` and compare `(st_ino, st_size)` to the cursor. Five cases:

   | Transition              | Action                                                                                                                      |
   | ----------------------- | --------------------------------------------------------------------------------------------------------------------------- |
   | unknown path            | create cursor at offset 0, emit `NEW_SESSION`                                                                               |
   | same inode, size grew   | read delta from `offset` to EOF, split on `\n`, retain tail after last `\n` as `pending`, emit `APPEND` with complete lines |
   | same inode, size shrank | emit `TRUNCATE`; reset cursor to 0; read from start                                                                         |
   | inode changed           | emit `ROTATE`; close old cursor; open new at offset 0                                                                       |
   | path vanished           | emit `DELETE`; mark cursor evicted                                                                                          |

3. **Partial-line safety.** A line is only emitted once it terminates in `\n`. An incomplete trailing fragment stays in `pending` and is prepended on the next read. This is what prevents the "JSON appears partway through a poll cycle" loss.
4. **Large-file safety.** Delta reads are chunked at 1 MiB. Files that grow more than 10 MiB between polls emit a single WARN and still process the delta — we trust the filesystem.

2-second poll interval is fast enough for human observation without burning CPU. On `~/.claude/projects` with hundreds of subdirs, enumeration is O(dirs) with a single `os.scandir` per directory — benchmarked acceptable for 2000 files at 2s cadence.

### Symlinks

Symlinks inside `~/.claude/projects` are followed once (via `Path.resolve()`), then the resolved inode is tracked. Symlink loops are bounded by `os.stat` failure.

### Stale Session Policy

A session is `ACTIVE` while new lines arrive. After 5 minutes with no APPEND events it transitions to `STALE` — the aggregator stops emitting it to renderers but keeps its collector state in memory. After a further 30 minutes in STALE with no activity it becomes `EVICTED`: collectors are `reset()` and dropped, cursor is closed. Both timeouts are configurable under `[watch]`.

A STALE session that receives a new APPEND returns to ACTIVE with collector state intact — a coffee break should not reset your metrics. Only EVICTED triggers state loss, and only after 35 minutes of silence.

### Cold-Start Replay

When `codevigil watch` launches against an existing session store, `PollingSource` discovers every pre-existing JSONL file on its first `poll()` call. To ensure the 5-min / 35-min thresholds classify these files correctly on the very first lifecycle tick — without waiting for real wall-clock time to pass — the watcher and aggregator cooperate to back-date the monotonic lifecycle cursor:

1. `PollingSource._handle_new` converts `stat.st_mtime` to a UTC datetime and stamps it on the `NEW_SESSION` `SourceEvent.timestamp`. For a file that just appeared, `st_mtime ≈ now` so behaviour is unchanged. For a pre-existing file from hours or days ago, `st_mtime` carries the true last-write time.

2. `SessionAggregator._ensure_session` derives the event's age (`now - source_event.timestamp`) and subtracts it from the current monotonic clock to produce a back-dated `last_monotonic`:

   ```
   age_seconds = max(0.0, (datetime.now(UTC) - source_event.timestamp).total_seconds())
   last_monotonic = now_clock - age_seconds
   ```

   This means the first `_run_lifecycle_pass()` computes `silence = now_clock - last_monotonic = age_seconds`, which is the file's true age. A file last written 2 hours ago immediately satisfies the 35-minute eviction threshold and is dropped on the first tick. A file last written 3 minutes ago stays ACTIVE.

3. `SessionAggregator._ingest_line` applies the same back-dating to each ingested JSONL event's timestamp, but only advances `last_monotonic` forward — it never moves it backward relative to its current value. Old replayed events converge toward the most recent JSONL event's timestamp; genuinely fresh live APPEND events advance `last_monotonic` to approximately the current clock, keeping live sessions ACTIVE.

The `_stale_after_seconds` (300 s) and `_evict_after_seconds` (2100 s) defaults are not changed by this mechanism — the existing thresholds are correct, and the back-dating makes them apply to cold-replayed sessions that previously appeared ACTIVE regardless of their true age.

### v0.2+: inotify / fsevents

Drop in an `InotifySource` or `FSEventsSource` implementing the same `Source` protocol. The aggregator doesn't know or care which source is upstream. Cross-platform selection lives behind a factory function in `watcher.py`. The `PollingSource` remains as a universal fallback.

### Turn Sidecar

The aggregator maintains a `TurnGrouper` inside each `_SessionContext` as a sidecar to collector ingestion. A **turn** is one user message plus the assistant's complete response (thinking blocks, tool calls, tool results, and the final assistant message), up to the next user message or session close. The grouper is a state machine: it opens a turn on each `USER_MESSAGE` event and closes it when the next `USER_MESSAGE` arrives or when `finalize()` is called at eviction time.

Completed turns are accumulated in `_SessionContext.completed_turns` as immutable `Turn` dataclass instances. Each `Turn` records the session ID, start/end timestamps, the user message text, the ordered sequence of canonical tool names called within the turn, and the total event count. A `task_type: str | None` field is reserved for the Phase 5 classifier; it is always `None` until classification runs.

Collectors continue to receive raw `Event` objects — they do not consume `Turn`. The Turn sidecar is exposed only to the classifier and to `history detail` turn-level display. At session eviction the completed turn list is serialised into `SessionReport.turns` (an optional field, `None` for pre-v0.2.0 records) alongside the existing collector metrics.

### Classifier Layering

The task classifier (`codevigil/classifier.py`) is a pure function of completed `Turn` snapshots. It runs inside the aggregator, invoked by `_SessionContext` immediately after `TurnGrouper` closes a turn (on arrival of the next user message or at eviction). The classifier is never called from the parser.

**Layer assignment rationale.** The parser is a line-level processor with no notion of turn boundaries — a turn spans multiple events across potentially multiple JSONL lines and files. Threading turn-boundary logic into the parser would require it to maintain conversational state, violating its single responsibility: event extraction and deduplication. The aggregator already accumulates events into `_SessionContext` objects where temporal ordering is visible and session lifecycle is managed. Placing classification there keeps the parser stateless with respect to conversation structure.

**Two-stage cascade.** Stage 1 applies tool-presence heuristics (mutation count, bash count, read/glob dominance) and is deterministic. Stage 2 applies keyword regex against the user message text and runs only when Stage 1 is ambiguous. A Stage 1 match is never overridden by Stage 2. See [docs/classifier.md](classifier.md) for the full rule table.

**Isolation from collectors.** Collectors (`read_edit_ratio`, `reasoning_loop`, `stop_phrase`, `parse_health`) continue to receive raw `Event` streams and do not consume `task_type`. Task-aware thresholds — e.g., treating a `mutation_heavy` session differently in `read_edit_ratio` — are a future concern, deferred until calibration establishes sufficient trust in the classifier labels. Coupling task-aware thresholds to classifier output before calibration is established would make it impossible to isolate regressions in either subsystem.

**Opt-in surface.** The classifier is controlled by two config keys: `classifier.enabled` (gates all classification; default `true`) and `classifier.experimental` (controls the `[experimental]` badge on all user-facing surfaces; default `true`). When disabled, `classify_turn` is never called and `session_task_type` / `turn_task_types` remain `null` in session reports. All user-visible surfaces (history list column, history detail panel, watch header tag, heatmap axis) degrade cleanly when the classifier is disabled.

## Configuration

TOML file at `~/.config/codevigil/config.toml` or passed via `--config`. Falls back to built-in defaults if absent.

```toml
[watch]
root = "~/.claude/projects"
poll_interval = 2.0

[collectors]
enabled = ["read_edit_ratio", "stop_phrase", "reasoning_loop"]

[collectors.read_edit_ratio]
window_size = 50
warn_threshold = 4.0
critical_threshold = 2.0

[collectors.stop_phrase]
custom_phrases = [
    "I'll leave that for now",
    "that should be sufficient",
]

[collectors.reasoning_loop]
warn_threshold = 10.0
critical_threshold = 20.0

[renderers]
enabled = ["terminal"]

[report]
output_format = "json"     # json | markdown
output_dir = "~/.local/share/codevigil/reports"
```

### Config Loading Order

Precedence, lowest to highest (later wins for any given key):

1. Built-in defaults (hardcoded in `config.py`)
2. Config file (`~/.config/codevigil/config.toml` or `--config <path>`)
3. Environment variables `CODEVIGIL_*`
4. CLI flags

CLI flags are the highest precedence so a one-off invocation can always override automation-set env vars. This reverses a draft-version ambiguity where env was documented as highest.

### Config Validation

Config loading is fail-loud:

- **Type errors** (e.g. `CODEVIGIL_WARN_THRESHOLD=invalid`) abort startup with a message naming the key, source (env/file/CLI), and expected type. No silent fallback.
- **Unknown keys** abort with `unknown config key '<name>' at <source>`. Typos must not be eaten.
- **Unknown collector/renderer names** in `enabled` lists abort with the list of known names. Registry is the source of truth.
- **Out-of-range values** (e.g. `poll_interval = -1`) abort with the allowed range.

A dry-run `codevigil config check` command prints the fully-resolved effective config with each value's source annotated, so users can audit precedence conflicts.

### Registry Validation

Both `collectors/__init__.py` and `renderers/__init__.py` run a validation pass at import time:

- **Duplicate names.** If two collector classes declare `name = "foo"`, registry construction raises `RegistryCollisionError`. No silent shadow.
- **Namespacing guidance.** Built-in collectors use bare names (`read_edit_ratio`). Third-party collectors installed via pip-entry-points must use dotted namespaces (`acme.quality`, `astral.lint_compliance`). Registry validation rejects unnamespaced third-party registrations.
- **Protocol conformance.** Each registered class is checked for `name`, `complexity`, `ingest`, `snapshot`, `reset` at load time, not at first event.

## CLI Modes

### `codevigil watch`

Live monitoring. Tails active sessions, refreshes terminal output every tick.

```bash
$ codevigil watch

codevigil [experimental thresholds] | sessions=2 crit=0 warn=1 ok=1 projects=2 updated=2026-04-14T10:22:00 | parse_confidence: 1.00
session: a3f7c2d | project: iree-loom | 2m 34s ACTIVE
──────────────────────────────────────────────────────────────────────
  read_edit_ratio     5.2   OK     [R:E 5.2 | research:mut 7.1] [↗3.1→4.2→5.2] [p68 of your baseline]
  stop_phrase         0     OK     [0 hits]
  reasoning_loop      6.4   OK     [6.4/1K tool calls | burst: 2] [↘8.1→7.2→6.4] [n/a]
──────────────────────────────────────────────────────────────────────

session: b8e1f9a | project: iree-amdgpu | 14m 12s ACTIVE
──────────────────────────────────────────────────────────────────────
  read_edit_ratio     1.8   CRIT   [R:E 1.8 | research:mut 2.3] [↘4.2→3.1→1.8] [p5 of your baseline]
  stop_phrase         3     WARN   [3 hits | last: "should I continue"] [↗0→1→3] [p94 of your baseline]
  reasoning_loop     18.2   WARN   [18.2/1K tool calls | burst: 7] [↗6.4→12.1→18.2] [p89 of your baseline]
──────────────────────────────────────────────────────────────────────
```

Multi-session display, sorted by worst severity first. The top line shows the fleet summary: session count, CRIT/WARN/OK tallies, project count, and ISO timestamp of the last tick. Each metric row appends a mini-trend (`[↗v1→v2→v3]`, last three snapshots) and a percentile anchor against the user's own session history from the `SessionStore` — `[n/a]` when the store is cold or persistence is disabled. Session state follows the ACTIVE → STALE → EVICTED lifecycle documented under **Watcher Design → Stale Session Policy** (defaults: 5 min to STALE, 35 min total to EVICTED).

### Project Name Resolution

Session files live at `~/.claude/projects/<project-hash>/sessions/<session-id>.jsonl`. Project hashes are not human-readable. The aggregator resolves them via a `ProjectRegistry` that merges three sources in precedence order:

1. User-maintained `~/.config/codevigil/projects.toml` (`{hash = "name"}` pairs).
2. The first `cwd` field observed in a session's SYSTEM event, stripped to the last path component.
3. The raw hash (fallback, always available).

The registry is cached per run. Users see a friendly name where possible and can always override via the TOML file. Unresolved hashes surface in watch mode as `project: <hash[:8]>` without triggering a WARN — this is expected state, not an error.

### Experimental Threshold Badge

The `[experimental thresholds]` marker in the header is present whenever any enabled collector has `experimental = true` in its effective config. Users who have explicitly calibrated thresholds (or completed bootstrap) set `experimental = false` and the badge disappears. This keeps the "these are not validated" signal visible without being annoying for users who have tuned their setup.

### `codevigil report <path>`

Batch analysis. Accepts a session file, directory of sessions, or glob pattern. Two distinct output modes:

**Per-session report** — one JSON or Markdown block per session, deterministic and diffable:

```bash
$ codevigil report ~/.claude/projects/*/sessions/*.jsonl \
    --from 2026-03-01 --to 2026-03-31 --format markdown
```

**Cohort trend table** — aggregates sessions into cells grouped by a dimension; cells with `n < 5` are replaced by the `n<5` sentinel:

```bash
$ codevigil report ~/.claude/projects --group-by week
$ codevigil report sessions/ --group-by project
```

**Period-over-period comparison** — filters sessions into two date ranges, runs Welch's t-test per metric, emits a signed delta table and prose one-liner:

```bash
$ codevigil report ~/.claude/projects \
    --compare-periods 2026-03-01:2026-03-31,2026-04-01:2026-04-30
```

Both cohort modes emit a `## Methodology` section (source size, date range, correlation-not-causation language) and a `## Appendix` (behavioral catalog, threshold table, sample-size distribution). `--group-by` and `--compare-periods` are mutually exclusive.

### `codevigil history`

Retrospective, read-only view of the `SessionStore`. Reads session reports written at eviction time when `[storage] enable_persistence = true`.

```bash
$ codevigil history list
$ codevigil history list --project my-project --since 2026-04-01 --severity warn
$ codevigil history agent-abc123def456       # detail view for one session
$ codevigil history diff agent-abc123 agent-def456
$ codevigil history heatmap agent-abc123
```

`history list` renders a rich formatted table. `history <SESSION_ID>` renders the session header, metric table, and stop-phrase context snippets with colored panels. `history diff` aligns two sessions by metric name using `difflib.SequenceMatcher` and renders a signed delta table. `history heatmap` renders a metric × severity matrix using `rich.table.Table`.

### `codevigil export <path>`

Dumps the parsed event stream as newline-delimited JSON for external tools. Useful for piping into `jq`, loading into notebooks, or feeding into future visualization layers.

```bash
$ codevigil export ~/.claude/projects/abc123/sessions/xyz.jsonl \
    | jq '.kind == "tool_call"'
```

## Error Taxonomy

Parser, watcher, collectors, and renderers all produce errors. Without a taxonomy, errors silently route to stderr or get swallowed. v0.1 defines a single `CodevigilError` hierarchy and a single **error channel** the aggregator owns:

```python
class CodevigilError(Exception):
    level: ErrorLevel        # INFO | WARN | ERROR | CRITICAL
    source: ErrorSource      # PARSER | WATCHER | COLLECTOR | RENDERER | CONFIG
    code: str                # stable identifier, e.g. "parser.malformed_line"
    context: dict[str, Any]  # structured detail for logs and --explain
```

### Levels and Routes

| Level    | Meaning                                                                                              | Route in `watch` mode                          | Route in `report`/`export`      |
| -------- | ---------------------------------------------------------------------------------------------------- | ---------------------------------------------- | ------------------------------- |
| INFO     | Lifecycle event (session start, rotation)                                                            | Log file only                                  | Log file only                   |
| WARN     | Recoverable drift (single malformed line, unknown schema fingerprint)                                | Dim footer line + log file                     | stderr + log file               |
| ERROR    | Subsystem degraded (collector stopped ingesting, renderer close failed)                              | Bright footer + log file + exit code remains 0 | stderr + log file + exit code 0 |
| CRITICAL | Metric integrity compromised (parse_confidence < 0.9, schema fingerprint unknown + parse miss > 20%) | Red banner over affected session + log file    | stderr + log file + exit code 2 |

CRITICAL errors are **always user-visible**. They are not suppressible by log-level config. A user who disables INFO/WARN noise must still see integrity failures.

### Log File

`~/.local/state/codevigil/codevigil.log`, JSON-lines, rotated at 10 MiB × 3 files. Each line is a serialised `CodevigilError` plus a timestamp. The log path is configurable under `[logging]`.

### Error Non-Swallowing Rule

No subsystem catches `CodevigilError` except the aggregator's top-level loop and the renderer's `render_error()` dispatch. Collectors that encounter bad data raise; the aggregator routes. This keeps error flow linear and auditable.

## Privacy Enforcement

Open Question 4 previously said "enforce via code review." A README promise is not enforcement. v0.1 adds a **technical network gate**:

1. **Import allowlist.** `codevigil/__init__.py` installs an import hook that raises `PrivacyViolationError` if any codevigil module (or any module it imports via the codevigil entry points) imports `socket`, `urllib`, `urllib3`, `http.client`, `httpx`, `requests`, `aiohttp`, `ftplib`, `smtplib`, `asyncio` transports, or any `ssl` module. The hook is active in all execution modes.
2. **Filesystem scope.** The watcher refuses to walk any root outside the user's home directory. The report command refuses to write outside `~/.local/share/codevigil/` or a path explicitly passed via `--output`. A path-traversal check (`Path.resolve().is_relative_to(allowed_root)`) runs on every write.
3. **CI gate.** A grep-based CI check greps the tree for the same module names and fails the build on any match. Belt-and-suspenders against future contributors unaware of the runtime hook.
4. **Subprocess audit.** codevigil invokes no subprocesses in v0.1. The CI gate also blocks imports of `subprocess`, `os.system`, `multiprocessing.popen_*`, and `pty`. If a future feature needs a subprocess, the contributor must remove the gate entry in a commit that reviewers will see.
5. **MCP mode caveat.** `codevigil serve` (v0.2+) necessarily opens a local socket. That feature lives in a separate package (`codevigil-serve`) outside the core import allowlist, so v0.1 users who don't install the serve extra retain the hard no-network guarantee.

This is not paranoia — session JSONL contains verbatim code, prompts, file paths, and sometimes secrets pasted during debugging. The blast radius of a regression that exfiltrates this data is large enough to justify technical enforcement.

## Watch Mode UX Limitations

Known limitations of the current terminal renderer:

1. **Full redraw, not diffed.** The terminal renderer clears the screen and redraws on every aggregator tick (default 1 Hz). On fast terminals this is fine; on slow SSH or tmux-over-high-latency links users will see flicker. Documented as a known limitation, not a bug. Diff rendering via `rich.live.Live` is the v0.2 upgrade path.
2. **No resize handling in v0.1.** If the terminal is resized mid-session, the next redraw adapts but the previous frame may leave artifacts. `SIGWINCH` handling is v0.2.
3. **One renderer focus.** The v0.1 watch-mode default enables exactly one renderer: `terminal`. Report-mode defaults to `json_file`. Users who want both in watch mode opt in explicitly via config — composing live terminal output with file-append is valid but users should know they're doing it.
4. **Scope narrowing.** v0.1 ships with `terminal` and `json_file` renderers. Any "dashboard" or "markdown" renderer is v0.2.

## Extension Points (Post v0.1)

These are not designed now, but the architecture must not block them.

### New Collectors

Adding a collector requires:

1. Create `collectors/new_metric.py` implementing `Collector` protocol
2. Add to registry in `collectors/__init__.py`
3. Add default config in `config.py`

No changes to parser, aggregator, or renderers. This is the primary extension axis.

**Planned collectors (v0.2+):**

| Collector          | Signal                                     | Depends On                             |
| ------------------ | ------------------------------------------ | -------------------------------------- |
| `thinking_depth`   | Signature length proxy for thinking tokens | Thinking block parsing                 |
| `convention_drift` | Edit compliance against CLAUDE.md rules    | CLAUDE.md parser + edit diffing        |
| `time_of_day`      | Quality correlation with hour/load         | Aggregation over historical data       |
| `file_churn`       | Same-file edit count (thrashing detection) | Tool call file path tracking           |
| `sentiment`        | User frustration indicators in prompts     | Keyword matching on user messages      |
| `token_efficiency` | Useful output per token consumed           | Token count parsing from API responses |
| `blind_edit_depth` | How many edits deep without any read       | Sequential tool call analysis          |

### Claude Code Hooks Integration

Claude Code hooks fire on specific lifecycle events. codevigil could register hooks that:

- Block a response if stop-phrase is detected (replaces the bash script approach)
- Inject "read the file first" when blind-edit-rate exceeds threshold
- Force `/compact` when context-related quality signals degrade

This requires a `hooks/` module that bridges collector snapshots to hook actions. The aggregator already produces snapshots on a tick — hooks subscribe to snapshots and emit actions.

**Key design constraint:** hooks must be opt-in and clearly separated from passive monitoring. Users should be able to run codevigil as pure instrumentation without it modifying Claude Code behavior.

### MCP Server Mode

`codevigil serve` exposes metrics as an MCP tool server. Claude Code itself (or other agents) can query session quality mid-run.

```
Tool: get_session_quality
Returns: { read_edit_ratio: 5.2, stop_phrase_hits: 0, ... }
```

This enables self-aware agents — Claude Code could check its own quality metrics and self-correct. The MCP server is a renderer that speaks the MCP protocol instead of writing to terminal/file.

### Multi-Session Aggregation and Trending

Cross-session analysis is implemented in the `codevigil/analysis/` package (shipped). The aggregator writes finalised session reports to `SessionStore` at eviction time when `[storage] enable_persistence = true`. The analysis layer provides:

- `analysis/store.py` — append-only JSON store with schema migration
- `analysis/cohort.py` — reducer over many session reports with group-by: `day`, `week`, `project`, `model`, `permission_mode`
- `analysis/compare.py` — Welch's t-test period-over-period comparison
- `analysis/guards.py` — sample-size guard (`n < 5` → `"n<5"` sentinel, enforced in all output paths)

The `codevigil history` subcommand family and the `report --group-by` / `report --compare-periods` flags both read from this store. Future work on this axis (additional group-by dimensions, cohort export to CSV, trend-alert hooks) layers onto `analysis/` without touching the live pipeline.

## Historical Analytics Substrate

The `codevigil/analysis/` package provides offline cohort reduction, period-over-period comparison, and sample-size guards for retrospective session analysis. All components are stdlib-only.

### Session Report Schema (stable, schema_version = 1)

Every finalised session writes one JSON file to `$XDG_STATE_HOME/codevigil/sessions/<session_id>.json` (falling back to `~/.local/state/codevigil/sessions/` when `XDG_STATE_HOME` is not set). The schema is pinned and stable. Any future field addition increments `schema_version` and ships a one-way migrator in `analysis/store.py::_migrate_record`.

```json
{
  "schema_version": 1,
  "session_id": "agent-abc123",
  "project_hash": "abc12345",
  "project_name": null,
  "model": null,
  "permission_mode": null,
  "started_at": "2026-04-14T10:00:00+00:00",
  "ended_at": "2026-04-14T10:30:00+00:00",
  "duration_seconds": 1800.0,
  "event_count": 120,
  "parse_confidence": 0.98,
  "metrics": {
    "read_edit_ratio": 5.2,
    "stop_phrase": 0.0,
    "reasoning_loop": 8.3
  },
  "eviction_churn": 0,
  "cohort_size": 3
}
```

Field semantics:

| Field              | Type               | Notes                                                                                                                                        |
| ------------------ | ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema_version`   | `int`              | Always present. Starts at 1. Increment on any schema change.                                                                                 |
| `session_id`       | `str`              | The JSONL file stem, stable within a session's lifetime.                                                                                     |
| `project_hash`     | `str`              | Parent directory name under `~/.claude/projects`. Non-empty; falls back to a 16-hex SHA-256 prefix of the raw path for unrecognised layouts. |
| `project_name`     | `str \| null`      | Human-readable name resolved via `ProjectRegistry`; `null` when unresolved.                                                                  |
| `model`            | `str \| null`      | Model identifier from session metadata. `null` until Phase 5 wires this field. Cohort group-by on `model` silently excludes null records.    |
| `permission_mode`  | `str \| null`      | Permission mode from session metadata. Same exclusion policy as `model`.                                                                     |
| `started_at`       | ISO 8601 `str`     | Wall-clock timestamp of the first observed event. Timezone-aware.                                                                            |
| `ended_at`         | ISO 8601 `str`     | Wall-clock timestamp of the last observed event before eviction.                                                                             |
| `duration_seconds` | `float`            | `(ended_at - started_at).total_seconds()`. May be `0.0` for single-event sessions.                                                           |
| `event_count`      | `int`              | Total events processed by the aggregator for this session.                                                                                   |
| `parse_confidence` | `float`            | `0.0`–`1.0` from `SessionParser.stats.parse_confidence` at eviction time.                                                                    |
| `metrics`          | `dict[str, float]` | Metric name → scalar value from `MetricSnapshot.value` at eviction time. One entry per active collector.                                     |
| `eviction_churn`   | `int`              | Number of sessions evicted during the tick that evicted this session. Fleet-level observability; not used by the cohort reducer.             |
| `cohort_size`      | `int`              | Number of live sessions at the end of the tick that evicted this session.                                                                    |

### Schema Migration Policy

Because Phase 3 ships before Phase 1 (the validation substrate), the store will hold real user data before any future phase can extend the schema. Migrations must be one-way and forward-compatible.

Rules enforced in `analysis/store.py::_migrate_record`:

1. **Adding a nullable field:** set the new field to `None` for all old records in the migrator. Never assume the field is present when reading a record of any version.
2. **Removing a field:** silently drop it. Old records with the removed field are valid; the extra key is ignored.
3. **Renaming a field:** add the new name (populated from the old value), drop the old name.
4. **Changing a field type:** coerce the old value to the new type in the migrator.
5. **Forwards:** code at version N must refuse to read records at version N+1 (raised as `MigrationError`). Never silently interpret a newer schema.

When a phase bumps `schema_version`, it must ship a corresponding `if version < N:` block in `_migrate_record`. A version increment without a migration block is a bug.

### Opt-In Persistence

Persistence is opt-in. The `[storage] enable_persistence` config flag defaults to `false`. When `false`, `codevigil watch` writes nothing under `~/.local/state/codevigil/` beyond the existing log file.

When `enable_persistence = true`, the aggregator writes one JSON file per finalised session (at eviction time). The first write logs a single-line activation notice at INFO level:

```
persistence enabled, writing to /home/user/.local/state/codevigil/sessions/
```

This notice fires once per process, on the first successful write, regardless of how many sessions are written subsequently.

### Group-By Dimensions (Closed Set)

The cohort reducer in `analysis/cohort.py` supports exactly five group-by dimensions. This set is deliberately closed — new dimensions are added only when a future phase's feature explicitly needs them:

| Dimension         | Key extracted                                  |
| ----------------- | ---------------------------------------------- |
| `day`             | `started_at.date().isoformat()` → `YYYY-MM-DD` |
| `week`            | ISO 8601 week → `YYYY-Www` (Monday-anchored)   |
| `project`         | `project_hash`                                 |
| `model`           | `model` (null records excluded)                |
| `permission_mode` | `permission_mode` (null records excluded)      |

### Sample-Size Guard

Any cohort cell with `n < 5` observations must never be rendered as a headline number. The guard is enforced by `analysis/guards.py::guard_cell` which raises `CellTooSmall` for cells below the threshold. Renderers and the compare path are responsible for catching `CellTooSmall` and substituting the sentinel string `"n<5"` in all headline-number positions.

This guard applies in every output path: cohort tables, period-over-period comparison, and the Phase 4 report renderer. It cannot be overridden by configuration.

### Period-over-Period Comparison

`analysis/compare.py` implements Welch's t-test via the stdlib `statistics` module and a manual continued-fraction approximation of the regularised incomplete beta function (no `scipy` dependency). The test is two-tailed with a default significance threshold of `p < 0.05`.

When either period has fewer than 2 observations for a given metric, the t-test is skipped and `significant = False` with `p_value = None`. Renderers must always display the raw delta and the p-value alongside the significance flag — the `significant` boolean is a convenience, not a substitute for the underlying numbers.

## Dependency Policy

### Runtime dependency: `rich>=13`

One declared runtime dependency: `rich>=13`. It provides the terminal dashboard (`watch`), all history command rendering, and ANSI styling throughout. No click, no watchdog, no tomllib backport (Python 3.11+ has `tomllib`). File watching via `os.stat()` polling.

`rich` brings its own minimal transitive tree (markdown-it-py, mdurl, pygments). The installed footprint is approximately 2–3 MB.

Minimum Python version: **3.11** (for `tomllib`, `StrEnum`, `slots=True` on dataclasses).

`watchdog` (inotify/fsevents) remains a potential future addition for lower-latency file watching, but is not yet included.

### When to add a new dependency

A dependency is justified when:

1. It replaces >200 lines of hand-rolled code, AND
2. It's well-maintained (>1K stars, recent commits), AND
3. It has zero transitive dependencies (or all transitive deps are also justified)

## Testing Strategy

### Unit Tests

Each collector gets a test file that feeds it a sequence of Events and asserts snapshot values. These are the core correctness tests.

```python
def test_read_edit_ratio_healthy():
    c = ReadEditRatioCollector(Config.defaults())
    for _ in range(6):
        c.ingest(make_event(EventKind.TOOL_CALL, tool="Read"))
    c.ingest(make_event(EventKind.TOOL_CALL, tool="Edit"))
    snap = c.snapshot()
    assert snap.value == 6.0
    assert snap.severity == Severity.OK
```

### Integration Tests

Feed real (anonymized) JSONL snippets through the full pipeline and assert report output. Keep 3-5 representative session fragments as test fixtures under `tests/fixtures/sessions/`.

### Fixture Sourcing

Fixtures are generated, not fabricated. The process:

1. **Source.** The contributor runs a real Claude Code session and copies the raw JSONL from `~/.claude/projects/<hash>/sessions/<id>.jsonl`.
2. **Anonymize.** A `tests/tools/anonymize_session.py` script performs: path stripping (replace home dir with `/home/user`), content redaction of any string matching common secret patterns (`sk-`, `ghp_`, AWS key prefixes), project-hash rewrite to a stable `fixture-<n>` token, and timestamp normalisation to a fixed base date. The anonymizer's output is deterministic given the same input.
3. **Review.** The anonymized file is manually reviewed before check-in. Each fixture has a sibling `fixture.md` that documents what scenario it represents (e.g., "healthy refactor session with high R:E and one legitimate 'actually' self-correction").
4. **License.** Contributors must confirm they own the session or have permission to redistribute the anonymized form. The fixture PR template includes this checkbox.

The initial v0.1 fixture set targets: one healthy session, one degraded R:E session, one stop-phrase-triggered session, one schema-drift session (hand-crafted to test `ParseHealthCollector`), and one mixed session used for the threshold-calibration baseline.

### Calibration Dataset

The anonymized fixture set doubles as the calibration corpus for threshold tuning. A `scripts/recalibrate_thresholds.py` tool runs all fixtures through each collector and emits a suggested threshold table based on the observed value distribution. Defaults in `config.py` are updated from this tool's output, not hand-picked, as the fixture corpus grows.

### Regression Tests

Once we have real users, capture session files that exhibited known quality issues. These become golden-file tests: "this session should produce R:E < 2.0 and CRITICAL severity." Regression fixtures go through the same anonymization pipeline as integration fixtures.

## Distribution

### Package

```
pip install codevigil
```

Published to PyPI. `codevigil` console script entry point. Runtime dependency: `rich>=13`. See **Dependency Policy** for rationale.

### Repo

Under `Mathews-Tom` on GitHub (`Mathews-Tom/codevigil`). Apache License 2.0.

## Open Questions

1. **Session identification.** Claude Code session files don't have a clean "session start" marker. **Resolved for v0.1:** one file = one session, session_id = file stem. The parser additionally records the first observed SYSTEM event's `cwd` and any session-start payload into SessionMeta for future use.

2. **Tool name normalization.** Tool names in JSONL vary across Claude Code versions (`Read` vs `read` vs `file_read`). **Resolved for v0.1:** a `TOOL_ALIASES: dict[str, str]` table in `parser.py` normalises to canonical lowercase snake_case names before Events are emitted. Unknown tool names pass through unchanged and are logged once per run at INFO. The table is small, committed, and updated as Claude Code evolves.

3. **Thinking block parsing.** The signature-to-thinking-length correlation (r=0.971) from the issue is powerful but relies on an undocumented field. **Resolved: defer the `thinking_depth` collector to v0.2, but reserve the THINKING payload schema now** (see Core Abstractions → Payload Schemas) so no migration is needed when the collector lands.

4. **Privacy enforcement.** **Resolved:** see the **Privacy Enforcement** section. Runtime import hook, filesystem scope check, CI grep gate, and subprocess ban. No-network is a technical guarantee, not a README promise.

5. **Remaining open.** The bootstrap window size (N=10 default) is a guess. After the first calibration dataset lands, revisit based on observed distributional stability per collector — collectors with high variance may need larger N.

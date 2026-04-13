# Privacy

codevigil's reason for being is to give you instrumentation over Claude Code sessions **without ever sending session data anywhere**. The privacy guarantee is not a README promise — it is technically enforced by three independent layers, any one of which would block a regression on its own.

This document explains what those layers are, what they protect against, and what is explicitly out of scope.

## What you put on the line by running codevigil

Claude Code session JSONL files are sensitive. They contain:

- Verbatim user prompts and the model's full responses
- Verbatim file contents read by tool calls (sometimes including secrets pasted during debugging)
- Resolved file paths from your filesystem
- Full transcripts of thinking blocks
- Project hashes that identify your work directories

The blast radius of a regression that exfiltrates this data is large enough to justify technical enforcement, not just a promise.

## Layer 1: runtime import allowlist hook

`codevigil/__init__.py` installs an `importlib` meta-path finder before any other codevigil submodule loads. The hook inspects `sys._getframe()` to find the **direct caller** of any banned import, skipping `importlib` machinery frames, and raises `PrivacyViolationError` if the caller is inside the `codevigil` package.

### What is blocked

| Module / package                                                 | Why blocked                                     |
| ---------------------------------------------------------------- | ----------------------------------------------- |
| `socket`, `socket.*`                                             | Raw network sockets                             |
| `ssl`                                                            | TLS — no encrypted transport that we don't have |
| `urllib`, `urllib.*`                                             | URL fetching                                    |
| `urllib3`                                                        | Same                                            |
| `http`, `http.client`, `http.server`                             | HTTP transport                                  |
| `httpx`, `requests`, `aiohttp`                                   | High-level HTTP libraries                       |
| `ftplib`, `smtplib`, `poplib`, `imaplib`, `nntplib`, `telnetlib` | Other transports                                |
| `xmlrpc`, `xmlrpc.*`                                             | RPC over HTTP                                   |
| `subprocess`                                                     | Process spawning — could shell out to `curl`    |
| `pty`                                                            | Pseudo-terminals — could spawn a shell          |
| `multiprocessing.popen_*`                                        | Process spawning under another name             |

The hook is active **in every execution mode**: `watch`, `report`, `export`, `config check`, library imports from third-party code, and the test suite.

### Why "direct caller", not "any caller"

A naive "block any import of `socket` once the hook is installed" would crash codevigil itself, because `logging.handlers` (a stdlib module that codevigil could legitimately want to use) statically imports `socket`. The hook needs to allow transitive imports from permitted stdlib modules while blocking direct imports from codevigil.

The implementation walks `sys._getframe()` outward, skips internal `importlib` frames, and looks at the **first** real Python frame. If that frame's module name starts with `codevigil`, the import is blocked. If it does not — for example if the frame is inside `logging.handlers` and codevigil is further down the stack — the import is allowed.

This means:

- `codevigil/foo.py: import socket` → blocked, raises `PrivacyViolationError`.
- `codevigil/foo.py: import logging` → allowed (codevigil doesn't actually do this — see below).
- A user's own code outside codevigil doing `import socket` → allowed (the hook scopes itself to codevigil callers).

### Even the log writer is hand-rolled

To avoid relying on the "transitive imports are allowed" exemption at all, codevigil's rotating JSONL log writer in `codevigil/errors.py` is **hand-implemented**. It does not use `logging.handlers.RotatingFileHandler` (which would pull in `socket`). The writer is ~30 lines of pure Python: rename ladder for archives, `open(..., "ab")` for the active file, byte counter for rotation. Zero stdlib paths that touch the network surface.

This is belt-and-suspenders against a future change to `logging` itself. If the standard library ever started importing more transports inside `logging` (e.g., for a syslog backend), codevigil would be unaffected.

### What `PrivacyViolationError` looks like

```text
PrivacyViolationError: codevigil module 'codevigil.foo' attempted to import banned module 'socket'; network and subprocess modules are disallowed by the privacy gate (see docs/design.md §Privacy Enforcement).
```

The error names the offending module, the banned target, and where to read about the policy. It is a subclass of `ImportError`, so existing `try/except ImportError` blocks catch it correctly.

## Layer 2: CI grep gate

`scripts/ci_privacy_grep.sh` runs as a separate CI job on every push and every PR. It re-checks the entire `codevigil/` source tree for `import X` and `from X import` statements against the same banned-module list as the runtime hook.

This is **belt-and-suspenders** against the runtime hook. The runtime hook protects users at install time; the CI gate protects against a contributor landing a regression that the test suite happens not to exercise. A banned import that slips through code review fails the CI job before merge, regardless of whether any test would have triggered the runtime hook on the same import path.

The grep is word-anchored (`^[[:space:]]*(import X|from X import)`) so a benign identifier like `mock_socket` cannot accidentally trip it from the wrong direction. The script explicitly excludes `codevigil/privacy.py` itself, where the banned-name list lives as string literals — without that exclusion the gate would block its own definition file.

The CI workflow at `.github/workflows/ci.yml` runs the gate as a separate `privacy-gate` job alongside the `quality (3.11)` and `quality (3.12)` jobs. All three must pass before a PR can merge.

## Layer 3: filesystem scope check

The watcher and the report writer both refuse to operate outside `$HOME` via a `Path.resolve().is_relative_to(Path.home().resolve())` check. Specifically:

- **`PollingSource(root)`** validates `root.resolve()` against `Path.home().resolve()` at construction time. Any root outside `$HOME` raises `PrivacyViolationError` and records a CRITICAL `CodevigilError` on the channel before the exception propagates.
- **`JsonFileRenderer(output_dir)`** applies the same check at construction time. Same error path.
- **`codevigil report --output DIR`** applies the same check before writing any file.

This protects against accidentally pointing codevigil at a system directory or shared mount, and against accidentally writing reports to a path that you didn't intend.

The check uses `Path.resolve()` so symlinks are followed once at construction time — a symlink inside `$HOME` that points outside `$HOME` is also blocked.

## What is **not** in scope

Honest about the boundary:

- **codevigil does not encrypt data at rest.** Session JSONL files, error logs, reports, and bootstrap state all live in plain text under your home directory. If your home directory is on an unencrypted disk and someone else has read access to it, codevigil's privacy guarantees do nothing for you. Use full-disk encryption.
- **codevigil does not run as a different user.** It runs as you, with your user's filesystem permissions. If a process running as you can read a file, codevigil can too.
- **codevigil does not isolate against other processes on the same host.** A privileged process or a process running as the same user can read codevigil's state and outputs the same way it can read everything else you own.
- **codevigil does not protect against an attacker with code-execution on your machine.** Nothing claimed here matters once an attacker can run arbitrary Python in your shell.
- **codevigil does not cover the "MCP serve" mode that is post-v0.1.** A future `codevigil serve` mode would necessarily open a local socket. The design defers that to a separate package (`codevigil-serve`) outside the core import allowlist so v0.1 users who never install the serve extra retain the hard no-network guarantee.

## Auditing the privacy claim yourself

You don't have to take this document's word for it.

### Read the runtime hook

```bash
less codevigil/privacy.py
```

It is ~150 lines and does exactly what this document describes. The banned-name list is in `_BANNED_EXACT` and `_BANNED_ROOTS`.

### Run the CI grep yourself

```bash
bash scripts/ci_privacy_grep.sh
```

It exits 0 when there are no banned imports, exits 1 with a list otherwise. You can run it against any clone, any branch, any git history.

### Run a network capture against codevigil

If you want empirical evidence:

```bash
sudo tcpdump -i any -w codevigil-traffic.pcap &
codevigil watch &
sleep 60
kill %2
sudo kill %1
```

Open `codevigil-traffic.pcap` in Wireshark. There will be no codevigil-originated packets. (You will see traffic from other processes running on your machine; filter by PID if you want only codevigil's, but the simpler check is "no traffic to non-loopback addresses originated by Python during the watch session.")

### Confirm the runtime dependency footprint

```bash
pip show codevigil
```

The `Requires` field lists only `rich>=13`. codevigil's runtime imports are stdlib modules plus `rich` — no networking libraries, no subprocess launchers, nothing that could exfiltrate data. `rich` itself makes no network calls.

## What to do if you find a violation

A privacy gate failure is a **release-blocker** bug, not a feature request. If you find a way to make codevigil emit a network packet or shell out to a subprocess from a default code path, file an issue at <https://github.com/Mathews-Tom/codevigil/issues> with the reproduction steps. We will treat it as a security-class bug — fix on the default branch, ship a patch release, document in the changelog.

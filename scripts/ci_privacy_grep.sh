#!/usr/bin/env bash
# Privacy gate — static grep for banned network and subprocess imports.
#
# This is the belt-and-suspenders companion to codevigil/privacy.py. The
# runtime hook blocks imports at interpreter load time; this script blocks
# them at CI time so a banned import can never land on main in the first
# place, even behind a feature flag or in code paths the runtime test
# corpus does not exercise.
#
# Scope: codevigil/**/*.py only. Tests are excluded so they can reference
# banned module names in string literals and assertion setup without
# tripping the gate.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Each pattern is an egrep alternation. Word boundaries ensure `socket` does
# not match `mocksocket`; anchoring on `import ` catches both `import X` and
# `from X import Y` after a leading whitespace trim.
BANNED=(
  "socket"
  "ssl"
  "urllib"
  "urllib3"
  "http\\.client"
  "http\\.server"
  "httpx"
  "requests"
  "aiohttp"
  "ftplib"
  "smtplib"
  "poplib"
  "imaplib"
  "nntplib"
  "telnetlib"
  "xmlrpc"
  "subprocess"
  "pty"
  "os\\.system"
  "multiprocessing\\.popen_"
)

status=0
for pattern in "${BANNED[@]}"; do
  # Match `import <pattern>` or `from <pattern> import ...` at line start
  # (after optional leading whitespace). Excludes the privacy module itself
  # which contains the banned-name *list* as string literals.
  if matches=$(grep -R -nE "^[[:space:]]*(import[[:space:]]+${pattern}|from[[:space:]]+${pattern}[[:space:]]+import)" \
      --include="*.py" codevigil 2>/dev/null); then
    while IFS= read -r line; do
      file="${line%%:*}"
      if [[ "$file" == "codevigil/privacy.py" ]]; then
        continue
      fi
      echo "PRIVACY GATE: banned import matched: $line"
      status=1
    done <<< "$matches"
  fi
done

if [[ $status -ne 0 ]]; then
  echo ""
  echo "One or more banned imports were found in codevigil/. The privacy"
  echo "gate forbids direct imports of network, TLS, and subprocess modules."
  echo "If you truly need one, discuss the design-doc exemption first."
  exit 1
fi

echo "privacy gate: no banned imports found"

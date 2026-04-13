"""Package entry point: prints the version and exits cleanly."""

from __future__ import annotations

import sys

from codevigil import __version__


def main() -> int:
    sys.stdout.write(f"codevigil {__version__}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

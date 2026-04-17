# Installation

codevigil ships as a single Python package. The only runtime dependency is `rich>=13`, which provides the terminal dashboard and all history command formatting. Any installer that can resolve a wheel from PyPI works. Python 3.11 or 3.12 is required.

## Recommended: `uv tool`

```bash
uv tool install codevigil
```

`uv tool install` puts a `codevigil` executable on your `PATH` inside an isolated environment that does not interfere with project virtualenvs or your system Python. This is the recommended path because it is the fastest install, the cleanest uninstall, and the easiest upgrade.

Verify the install:

```bash
codevigil --version
codevigil config check
```

`codevigil --version` prints the package version. `codevigil config check` prints the resolved configuration with each value's source — see the output to confirm the install reached the package metadata correctly.
The first thing to verify is `watch.roots`; that is the canonical watch setting as of multi-root support. If your setup still uses `watch.root` or `CODEVIGIL_WATCH_ROOT`, `config check` will print a deprecation notice while continuing to honor the value.

### Upgrade

```bash
uv tool upgrade codevigil
```

### Uninstall

```bash
uv tool uninstall codevigil
```

This removes both the executable and the isolated environment. It does not touch any session data, config files, or logs in `~/.config/`, `~/.local/share/`, or `~/.local/state/`.

### Don't have `uv`?

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Or follow the official guide at <https://docs.astral.sh/uv/getting-started/installation/>.

## Alternative: `pipx`

```bash
pipx install codevigil
pipx upgrade codevigil
pipx uninstall codevigil
```

`pipx` provides the same isolation guarantees as `uv tool` and works identically from codevigil's perspective. Choose whichever you already have.

## Alternative: `pip --user`

```bash
python3 -m pip install --user codevigil
```

This installs into your user site-packages. The `codevigil` executable lands under `~/.local/bin/` (Linux) or `~/Library/Python/3.x/bin/` (macOS). Make sure that directory is on your `PATH`. To upgrade or uninstall, use `pip install --user --upgrade codevigil` and `pip uninstall codevigil`.

## Alternative: from source

For development or to install from a local checkout:

```bash
git clone https://github.com/Mathews-Tom/codevigil
cd codevigil
uv tool install --from . codevigil
```

Or with pip:

```bash
git clone https://github.com/Mathews-Tom/codevigil
cd codevigil
pip install .
```

For a full development environment with the lint, type-check, and test toolchain, use `uv sync --dev` instead — see the [Contributing](#contributing) section below.

## Verifying the install is genuine

The wheel install is exactly two distribution files plus a sdist tarball:

```text
codevigil-0.4.0-py3-none-any.whl
codevigil-0.4.0.tar.gz
```

After installing, confirm the package and its single declared dependency:

```bash
uv tool list
# or
pip show codevigil
```

The `Requires` field should list `rich>=13` and nothing else. If you see additional packages, you have a different package, not codevigil.

## What gets created on first run

codevigil writes to three locations under your home directory. None of them are created at install time — they appear lazily on first run:

| Path                                      | Purpose                                      |
| ----------------------------------------- | -------------------------------------------- |
| `~/.config/codevigil/config.toml`         | Optional. User config, you create it.        |
| `~/.local/share/codevigil/reports/`       | Default report output directory.             |
| `~/.local/state/codevigil/codevigil.log`  | Rotating JSONL error log (10 MiB × 3 files). |
| `~/.local/state/codevigil/bootstrap.json` | Bootstrap calibration state.                 |

All four are inside `$HOME` and respect the filesystem scope gate. None of them are created until codevigil actually writes to them.

## Contributing

If you want to hack on codevigil rather than just use it:

```bash
git clone https://github.com/Mathews-Tom/codevigil
cd codevigil
uv sync --dev
```

This installs the dev tooling: ruff, mypy, pytest. Then run the gate:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict codevigil
uv run pytest
bash scripts/ci_privacy_grep.sh
```

All five must pass before a commit lands. CI re-runs them on every PR.

## Troubleshooting

**`codevigil: command not found`** — your installer put the executable somewhere that is not on your `PATH`. With `uv tool` and `pipx` this should not happen; with `pip --user`, add `~/.local/bin` (Linux) or the relevant Python user-base bin directory to your `PATH`.

**`PrivacyViolationError: ... attempted to import banned module`** — you are running a development build that imports a module not on the allowlist. This is a build defect, not a user error. Report it as a bug.

**`ConfigError: config.unknown_key`** — your `~/.config/codevigil/config.toml` contains a key codevigil does not recognise. The error message names the key. See [docs/configuration.md](configuration.md) for the full key list.

**`unable to find PEP 517 build backend`** — you tried `pip install` against a checkout that does not have `pyproject.toml` or that has a broken `pyproject.toml`. Use a clean checkout.

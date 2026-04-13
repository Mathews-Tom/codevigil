"""Wire-up test: ``codevigil watch`` must pass a ``SessionStore`` to the
terminal renderer when ``[storage] enable_persistence`` is True, and must
pass ``None`` when it is False (the default).

The percentile-anchor feature in the renderer degrades to ``[n/a]`` when
the store is ``None``. Before this wire-up, ``_run_watch`` always passed
``None``, so percentile anchors were cosmetic-only regardless of user
config. These tests lock in the positive and negative cases.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codevigil import cli as cli_module
from codevigil.analysis.store import SessionStore
from codevigil.cli import main
from codevigil.renderers.terminal import TerminalRenderer


def _write_config(cfg_path: Path, *, enable_persistence: bool) -> None:
    """Write a minimal TOML config that toggles persistence.

    Everything else falls back to ``CONFIG_DEFAULTS`` so the test stays
    small and survives unrelated schema changes.
    """
    cfg_path.write_text(
        f"""\
[storage]
enable_persistence = {"true" if enable_persistence else "false"}
""",
        encoding="utf-8",
    )


def _prime_env(monkeypatch: pytest.MonkeyPatch, home: Path, *, tick: str = "0.05") -> None:
    (home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEVIGIL_LOG_PATH", str(home / "codevigil.log"))
    monkeypatch.setenv("CODEVIGIL_WATCH_ROOT", str(home / ".claude" / "projects"))
    monkeypatch.setenv("CODEVIGIL_WATCH_TICK_INTERVAL", tick)
    # Redirect the store's default base_dir into tmp_path so we never write
    # to the real ``~/.local/state/codevigil/sessions/``.
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))


def _install_single_tick_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the watch loop run exactly one iteration and then exit."""

    def _fake_install() -> None:
        cli_module._shutdown_requested = True

    monkeypatch.setattr(cli_module, "_install_sigint_handler", _fake_install)
    # Use dotted-string target so mypy --strict does not require ``time`` to
    # be an explicit re-export of the ``codevigil.cli`` module.
    monkeypatch.setattr("codevigil.cli.time.sleep", lambda _s: None)


def _capture_baseline_store_from_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Replace ``TerminalRenderer`` with a spy that records ``baseline_store``.

    Returns a dict that the test asserts against after ``main`` returns.
    """
    captured: dict[str, Any] = {}

    class _Spy(TerminalRenderer):
        def __init__(
            self,
            *,
            show_experimental_badge: bool = False,
            baseline_store: SessionStore | None = None,
            **kwargs: Any,
        ) -> None:
            captured["baseline_store"] = baseline_store
            captured["show_experimental_badge"] = show_experimental_badge
            super().__init__(
                show_experimental_badge=show_experimental_badge,
                baseline_store=baseline_store,
                **kwargs,
            )

    monkeypatch.setattr(cli_module, "TerminalRenderer", _Spy)
    return captured


def test_watch_passes_session_store_when_persistence_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _prime_env(monkeypatch, home)
    _install_single_tick_shutdown(monkeypatch)
    captured = _capture_baseline_store_from_renderer(monkeypatch)

    cfg_path = tmp_path / "codevigil.toml"
    _write_config(cfg_path, enable_persistence=True)

    exit_code = main(["--config", str(cfg_path), "watch"])
    assert exit_code == 0

    store = captured["baseline_store"]
    assert isinstance(store, SessionStore), (
        f"expected SessionStore when persistence is enabled, got {type(store).__name__}"
    )
    # The store must point at the redirected XDG path — not the real user dir.
    assert str(store.base_dir).startswith(str(home / ".local" / "state"))


def test_watch_passes_none_when_persistence_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _prime_env(monkeypatch, home)
    _install_single_tick_shutdown(monkeypatch)
    captured = _capture_baseline_store_from_renderer(monkeypatch)

    cfg_path = tmp_path / "codevigil.toml"
    _write_config(cfg_path, enable_persistence=False)

    exit_code = main(["--config", str(cfg_path), "watch"])
    assert exit_code == 0

    assert captured["baseline_store"] is None, (
        "expected baseline_store=None when persistence is disabled"
    )


def test_watch_passes_none_when_storage_section_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No [storage] section → default False → renderer receives ``None``.

    This guards against a regression where ``cfg.get("storage", {})`` is
    replaced with a direct key access that would KeyError on default configs.
    """
    home = tmp_path / "home"
    _prime_env(monkeypatch, home)
    _install_single_tick_shutdown(monkeypatch)
    captured = _capture_baseline_store_from_renderer(monkeypatch)

    # Config file exists but has no [storage] section at all.
    cfg_path = tmp_path / "codevigil.toml"
    cfg_path.write_text("# intentionally empty\n", encoding="utf-8")

    exit_code = main(["--config", str(cfg_path), "watch"])
    assert exit_code == 0

    assert captured["baseline_store"] is None

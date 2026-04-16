"""Shared Rich progress helpers for long-running bounded commands."""

from __future__ import annotations

import sys
import time
from contextlib import AbstractContextManager
from typing import Protocol

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)


class ProgressReporter(Protocol):
    def start(
        self,
        *,
        phase: str,
        total: int | None = None,
        message: str = "",
        unit: str = "items",
        target: str = "",
    ) -> None: ...

    def set_total(self, total: int | None) -> None: ...

    def advance(
        self,
        delta: int = 1,
        *,
        message: str | None = None,
        bytes_delta: int = 0,
        target: str | None = None,
    ) -> None: ...

    def set_message(self, message: str) -> None: ...

    def update(
        self,
        *,
        phase: str | None = None,
        message: str | None = None,
        target: str | None = None,
        total: int | None = None,
    ) -> None: ...

    def finish(self, message: str | None = None) -> None: ...


class NullProgressReporter:
    def start(
        self,
        *,
        phase: str,
        total: int | None = None,
        message: str = "",
        unit: str = "items",
        target: str = "",
    ) -> None:
        return None

    def set_total(self, total: int | None) -> None:
        return None

    def advance(
        self,
        delta: int = 1,
        *,
        message: str | None = None,
        bytes_delta: int = 0,
        target: str | None = None,
    ) -> None:
        return None

    def set_message(self, message: str) -> None:
        return None

    def update(
        self,
        *,
        phase: str | None = None,
        message: str | None = None,
        target: str | None = None,
        total: int | None = None,
    ) -> None:
        return None

    def finish(self, message: str | None = None) -> None:
        return None


def should_enable_progress(*, total_items: int | None = None, minimum_items: int = 10) -> bool:
    if not sys.stderr.isatty():
        return False
    if total_items is None:
        return True
    return total_items >= minimum_items


def stderr_console() -> Console:
    return Console(file=sys.stderr)


class RichProgressReporter(AbstractContextManager[ProgressReporter]):
    """Single-task progress reporter for CLI commands."""

    def __init__(self, *, console: Console, enabled: bool = True) -> None:
        self.console = console
        self.enabled = enabled
        self._progress: Progress | None = None
        self._task_id: TaskID | None = None
        self._bytes_processed: int = 0
        self._unit: str = "items"
        self._started_at: float = 0.0
        if not self.enabled:
            return
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.fields[phase]}", justify="left"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("{task.fields[message]}", justify="left"),
            TextColumn("{task.fields[target]}", justify="left"),
            TextColumn("{task.fields[bytes]}", justify="right"),
            TextColumn("{task.fields[rate]}", justify="right"),
            TimeElapsedColumn(),
            console=self.console,
            transient=False,
        )
        self._progress.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._progress is not None:
            self._progress.stop()

    def start(
        self,
        *,
        phase: str,
        total: int | None = None,
        message: str = "",
        unit: str = "items",
        target: str = "",
    ) -> None:
        if self._progress is None:
            return
        self._bytes_processed = 0
        self._unit = unit
        self._started_at = time.monotonic()
        self._task_id = self._progress.add_task(
            description=phase,
            total=total,
            phase=phase,
            message=message,
            target=target,
            bytes="",
            rate=self._format_rate(completed=0.0),
        )

    def set_total(self, total: int | None) -> None:
        if self._progress is None or self._task_id is None:
            return
        self._progress.update(self._task_id, total=total)

    def advance(
        self,
        delta: int = 1,
        *,
        message: str | None = None,
        bytes_delta: int = 0,
        target: str | None = None,
    ) -> None:
        if self._progress is None or self._task_id is None:
            return
        if bytes_delta:
            self._bytes_processed += bytes_delta
        task = self._progress.tasks[self._task_id]
        current_message = task.fields["message"]
        current_target = task.fields["target"]
        completed = task.completed + delta
        self._progress.update(
            self._task_id,
            advance=delta,
            message=message if message is not None else current_message,
            target=target if target is not None else current_target,
            bytes=self._format_bytes(self._bytes_processed),
            rate=self._format_rate(completed=completed),
        )

    def set_message(self, message: str) -> None:
        self.update(message=message)

    def update(
        self,
        *,
        phase: str | None = None,
        message: str | None = None,
        target: str | None = None,
        total: int | None = None,
    ) -> None:
        if self._progress is None or self._task_id is None:
            return
        task = self._progress.tasks[self._task_id]
        fields = self._progress.tasks[self._task_id].fields
        self._progress.update(
            self._task_id,
            total=total if total is not None else task.total,
            phase=phase if phase is not None else fields["phase"],
            message=message if message is not None else fields["message"],
            target=target if target is not None else fields["target"],
            bytes=self._format_bytes(self._bytes_processed),
            rate=self._format_rate(completed=task.completed),
        )

    def finish(self, message: str | None = None) -> None:
        if self._progress is None or self._task_id is None:
            return
        fields = self._progress.tasks[self._task_id].fields
        task = self._progress.tasks[self._task_id]
        self._progress.update(
            self._task_id,
            completed=task.total or task.completed,
            phase="done",
            message=message if message is not None else fields["message"],
            target=fields["target"],
            bytes=self._format_bytes(self._bytes_processed),
            rate=self._format_rate(completed=task.total or task.completed),
        )
        self._progress.stop()
        self._progress = None
        self._task_id = None

    @staticmethod
    def _format_bytes(value: int) -> str:
        if value <= 0:
            return ""
        suffixes = ("B", "KB", "MB", "GB")
        size = float(value)
        suffix = suffixes[0]
        for suffix in suffixes:
            if size < 1024.0 or suffix == suffixes[-1]:
                break
            size /= 1024.0
        if suffix == "B":
            return f"{int(size)} {suffix}"
        return f"{size:.1f} {suffix}"

    def _format_rate(self, *, completed: float) -> str:
        elapsed = max(time.monotonic() - self._started_at, 0.0)
        if elapsed <= 0:
            return ""
        if self._bytes_processed > 0:
            return f"{self._format_bytes(int(self._bytes_processed / elapsed))}/s"
        return f"{completed / elapsed:.1f} {self._unit}/s"


def progress_reporter(
    *,
    total_items: int | None = None,
    minimum_items: int = 10,
) -> ProgressReporter:
    enabled = should_enable_progress(total_items=total_items, minimum_items=minimum_items)
    if not enabled:
        return NullProgressReporter()
    return RichProgressReporter(console=stderr_console(), enabled=True)


__all__ = [
    "NullProgressReporter",
    "ProgressReporter",
    "RichProgressReporter",
    "progress_reporter",
    "should_enable_progress",
]

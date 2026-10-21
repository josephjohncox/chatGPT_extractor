"""Rich terminal progress with deterministic, credential-safe redirected logs."""

import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Column
from rich.text import Text


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def size_progress(completed: int, total: int | None) -> str:
    return f"{human_bytes(completed)}/{human_bytes(total) if total is not None else '?'}"


def safe_terminal_text(message: str) -> str:
    return "".join(char if char >= " " and not 127 <= ord(char) <= 159 else " " for char in message)


class CountsColumn(MofNCompleteColumn):
    def render(self, task: Task) -> Text:
        if task.fields.get("unit") == "bytes":
            return Text(
                size_progress(
                    int(task.completed), int(task.total) if task.total is not None else None
                )
            )
        return super().render(task)


class RateColumn(ProgressColumn):
    def render(self, task: Task) -> Text:
        if not task.speed or task.completed <= 1:
            return Text("--/s")
        rate = (
            human_bytes(int(task.speed))
            if task.fields.get("unit") == "bytes"
            else f"{task.speed:.1f}"
        )
        return Text(f"{rate}/s")


class DetailedProgress(Progress):
    def get_renderables(self) -> Iterable[RenderableType]:
        tasks = [task for task in self.tasks if task.visible]
        main = [task for task in tasks if not task.fields.get("group_key")]
        if main:
            yield self.make_tasks_table(main)
        active = [
            task
            for task in main
            if task.fields.get("state") == "working" and task.fields.get("detail")
        ]
        for task in active[-2:]:
            yield Text(f"{task.description}: {task.fields['detail']}")
        keys = dict.fromkeys(
            task.fields["group_key"] for task in tasks if task.fields.get("group_key")
        )
        for key in keys:
            grouped = [task for task in tasks if task.fields.get("group_key") == key]
            details = [
                Text(f"{task.description}: {task.fields['detail']}")
                for task in grouped
                if task.fields.get("detail")
            ]
            yield Panel(
                Group(self.make_tasks_table(grouped), *details),
                title=Text(grouped[0].fields.get("group_title", "Progress")),
                padding=(0, 1),
            )


class ProgressReporter:
    def __init__(
        self, *, enabled: bool = True, console: Console | None = None, interval: float = 1.0
    ):
        self.enabled = enabled
        self.console = console if console is not None else Console(stderr=True)
        self.interval = interval
        self.started = time.monotonic()
        self.live = enabled and self.console.is_terminal
        self.results: list[str] = []
        self.warnings = 0
        self._group_stack: list[ProgressGroup] = []
        self._group_serial = 0
        columns: list[ProgressColumn] = [
            SpinnerColumn(),
            TextColumn(
                "{task.description}",
                markup=False,
                table_column=Column(
                    max_width=28 if self.console.width >= 140 else 22,
                    no_wrap=True,
                    overflow="ellipsis",
                ),
            ),
            TextColumn(
                "{task.fields[state]}",
                markup=False,
                table_column=Column(width=9, no_wrap=True, overflow="ellipsis"),
            ),
            BarColumn(),
            CountsColumn(table_column=Column(no_wrap=True)),
        ]
        if self.console.width >= 140:
            columns.extend([TaskProgressColumn(), RateColumn()])
        columns.extend([TimeElapsedColumn(), TextColumn("ETA"), TimeRemainingColumn()])
        self.renderer = DetailedProgress(
            *columns,
            console=self.console,
            refresh_per_second=4,
            expand=False,
            redirect_stdout=False,
            redirect_stderr=False,
        )

    def __enter__(self) -> "ProgressReporter":
        self.started = time.monotonic()
        if self.live:
            self.renderer.start()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.live:
            self.renderer.stop()
        for result in self.results:
            print(result)
        self.results.clear()

    def result(self, message: str) -> None:
        # Do not let live terminal redraws or Rich redirection consume JSON stdout.
        if self.live:
            self.results.append(message)
        else:
            print(message)

    def error(self, message: str) -> None:
        self.console.print(Text(message))

    def notice(self, message: str, *, persistent: bool = False) -> None:
        if self._group_stack and not persistent:
            self._group_stack[-1].note(safe_terminal_text(message))
            return
        self._emit(message)

    def _emit(self, message: str) -> None:
        if self.enabled:
            # Text disables Rich markup; strip terminal controls without ever logging payloads.
            safe = safe_terminal_text(message)
            self.console.print(Text(f"[{time.monotonic() - self.started:7.1f}s] {safe}"))

    def warning(self, message: str) -> None:
        self.warnings += 1
        self.notice("WARNING: " + message, persistent=True)

    def stage(
        self,
        label: str,
        *,
        total: int | None = None,
        detail: str = "",
        transient: bool = False,
        unit: str = "items",
    ) -> "ProgressTask":
        group = self._group_stack[-1] if self._group_stack else None
        return ProgressTask(
            self, label, total=total, detail=detail, transient=transient, group=group, unit=unit
        )

    def group(self, label: str) -> "ProgressGroup":
        self._group_serial += 1
        return ProgressGroup(self, label, f"group-{self._group_serial}")


class ProgressTask:
    def __init__(
        self,
        reporter: ProgressReporter,
        label: str,
        *,
        total: int | None,
        detail: str,
        transient: bool,
        group: "ProgressGroup | None" = None,
        unit: str = "items",
    ):
        self.reporter = reporter
        self.group = group
        self.unit = unit
        self.label = label
        self.total = total
        self.detail = detail
        self.transient = transient
        self.completed = 0
        self.started = time.monotonic()
        self.last_log = self.started
        self.finished = False
        self.silent = False
        self.task_id: TaskID | None = None
        if group is not None:
            group.enter_task(self)
        elif reporter.live:
            self.task_id = reporter.renderer.add_task(
                label, total=total, detail=detail, state="working", unit=unit
            )
        else:
            reporter.notice(f"{label}: started" + (f"; {detail}" if detail else ""))

    def __enter__(self) -> "ProgressTask":
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if not self.finished:
            state = (
                "interrupted"
                if kind is not None and issubclass(kind, KeyboardInterrupt)
                else "deferred"
                if getattr(exc, "temporary_stop", False)
                else "failed"
                if kind
                else "done"
            )
            self.finish(state=state)

    def update(
        self,
        *,
        completed: int | None = None,
        total: int | None = None,
        detail: str | None = None,
        force: bool = False,
    ) -> None:
        if completed is not None:
            self.completed = completed
        if total is not None:
            self.total = total
        if detail is not None:
            self.detail = detail
        if self.group is not None:
            self.group.task_changed(self, force=force)
        elif self.task_id is not None:
            self.reporter.renderer.update(
                self.task_id, completed=self.completed, total=self.total, detail=self.detail
            )
        elif force or time.monotonic() - self.last_log >= self.reporter.interval:
            self._log("working")

    def advance(self, amount: int = 1, *, detail: str | None = None, force: bool = False) -> None:
        self.update(completed=self.completed + amount, detail=detail, force=force)

    def finish(
        self, detail: str | None = None, *, state: str = "done", finalize_total: bool = True
    ) -> None:
        if self.finished:
            return
        self.finished = True
        if detail is not None:
            self.detail = detail
        if self.group is not None:
            self.group.exit_task(self, state)
        elif self.task_id is not None:
            self.reporter.renderer.update(
                self.task_id,
                completed=self.completed,
                total=self.total
                if self.total is not None or not finalize_total
                else self.completed,
                detail=self.detail,
                state=state,
                refresh=not self.transient,
            )
            self.reporter.renderer.stop_task(self.task_id)
            if self.transient:
                self.reporter.renderer.remove_task(self.task_id)
            else:
                self._log(state)
        else:
            self._log(state)

    def _log(self, state: str) -> None:
        if self.silent:
            return
        elapsed = max(0.0, time.monotonic() - self.started)
        counts = (
            f"{self.completed}/{self.total}"
            if self.total is not None
            else f"{self.completed} processed (total unknown)"
        )
        if self.unit == "bytes":
            counts = size_progress(self.completed, self.total)
        eta = ""
        if (
            self.total is not None
            and self.completed > 0
            and self.completed < self.total
            and state == "working"
        ):
            eta = f"; ETA ~{elapsed * (self.total - self.completed) / self.completed:.1f}s"
        rate = ""
        if self.completed > 1 and elapsed >= 0.1:
            speed = (
                human_bytes(int(self.completed / elapsed))
                if self.unit == "bytes"
                else f"{self.completed / elapsed:.1f}"
            )
            rate = f"; {speed}/s"
        self.reporter.notice(
            f"{self.label}: {state}; {counts}; {elapsed:.1f}s{rate}{eta}"
            + (f"; {self.detail}" if self.detail else "")
        )
        self.last_log = time.monotonic()


class ProgressGroup:
    """One aggregate row plus a single reusable activity slot, regardless of file count."""

    def __init__(self, reporter: ProgressReporter, label: str, key: str):
        self.reporter = reporter
        self.label = label
        self.key = key
        self.root = ProgressTask(reporter, label, total=None, detail="", transient=False)
        self.root.silent = True
        self.activity_id: TaskID | None = None
        self.tasks: list[ProgressTask] = []
        self.current_item = ""
        self.last_notice = ""
        self.last_log = time.monotonic()
        self.active_task: ProgressTask | None = None
        if self.root.task_id is not None:
            reporter.renderer.update(
                self.root.task_id, description="Processed", group_key=key, group_title=label
            )

    @property
    def finished(self) -> bool:
        return self.root.finished

    @contextmanager
    def activate(self) -> Iterator["ProgressGroup"]:
        if self.finished:
            self.root.finished = False
            self.root.total = None
            if self.root.task_id is not None:
                self.reporter.renderer.reset(
                    self.root.task_id,
                    total=None,
                    completed=self.root.completed,
                    state="working",
                    group_key=self.key,
                    group_title=self.label,
                )
        depth = len(self.tasks)
        self.reporter._group_stack.append(self)
        state = "done"
        try:
            yield self
        except BaseException as exc:
            state = (
                "interrupted"
                if isinstance(exc, KeyboardInterrupt)
                else "deferred"
                if getattr(exc, "temporary_stop", False)
                else "failed"
            )
            raise
        finally:
            for task in list(reversed(self.tasks[depth:])):
                task.finish(state=state)
            self.reporter._group_stack.pop()
            self._render()

    def current(self, label: str) -> None:
        if label != self.current_item:
            self.current_item = label
            self.last_notice = ""
        self._render()

    def update(self, *, completed: int, detail: str) -> None:
        self.root.update(completed=completed, detail=detail)
        self._render()
        self._log()

    def enter_task(self, task: ProgressTask) -> None:
        self.tasks.append(task)
        self._render()

    def task_changed(self, task: ProgressTask, *, force: bool = False) -> None:
        self._render()
        self._log(force=force)

    def exit_task(self, task: ProgressTask, state: str) -> None:
        if task in self.tasks:
            self.tasks.remove(task)
        if state not in {"done", "received"}:
            self.last_notice = f"{task.label}: {state}"
        self._render()
        self._log()

    def note(self, message: str) -> None:
        self.last_notice = message
        self._render()
        important = message.startswith(
            ("Retry:", "Retry stopped:", "Recovered HTTP:", "Network cleanup:", "Browser challenge")
        )
        if not self.reporter.live and important:
            self.reporter._emit(message)
        else:
            self._log()

    def _render(self) -> None:
        if not self.reporter.live:
            return
        current = self.tasks[-1] if self.tasks else None
        if current is None:
            if self.activity_id is not None:
                self.reporter.renderer.update(self.activity_id, visible=False)
            self.active_task = None
            return
        detail = "; ".join(
            part for part in (self.current_item, current.detail, self.last_notice) if part
        )
        fields: dict[str, Any] = {
            "detail": detail,
            "state": "working",
            "unit": current.unit,
            "group_key": self.key,
            "group_title": self.label,
        }
        if self.activity_id is None:
            self.activity_id = self.reporter.renderer.add_task(
                "↳ " + current.label, total=current.total, completed=current.completed, **fields
            )
        elif self.active_task is not current:
            self.reporter.renderer.reset(
                self.activity_id,
                description="↳ " + current.label,
                total=current.total,
                completed=current.completed,
                visible=True,
                **fields,
            )
        else:
            self.reporter.renderer.update(
                self.activity_id,
                completed=current.completed,
                total=current.total,
                visible=True,
                **fields,
            )
        self.active_task = current

    def _log(self, *, force: bool = False) -> None:
        if self.reporter.live or not self.reporter.enabled:
            return
        if not force and time.monotonic() - self.last_log < self.reporter.interval:
            return
        activity = self.tasks[-1] if self.tasks else None
        transfer = ""
        if activity is not None and activity.unit == "bytes":
            transfer = size_progress(activity.completed, activity.total)
            elapsed = time.monotonic() - activity.started
            if activity.completed > 1 and elapsed >= 0.1:
                transfer += f"; {human_bytes(int(activity.completed / elapsed))}/s"
        details = "; ".join(
            part
            for part in (
                self.root.detail,
                self.current_item,
                activity.label if activity else "",
                transfer,
                activity.detail if activity else "",
                self.last_notice,
            )
            if part
        )
        self.reporter._emit(
            f"{self.label}: {self.root.completed} processed (total unknown until discovery finishes); {details}"
        )
        self.last_log = time.monotonic()

    def finish(self, detail: str, *, state: str = "done") -> None:
        if self.finished:
            return
        for task in list(reversed(self.tasks)):
            task.finish(state=state)
        if self.activity_id is not None:
            self.reporter.renderer.remove_task(self.activity_id)
            self.activity_id = None
        self.root.finish(detail, state=state, finalize_total=state in {"done", "partial"})
        self.reporter._emit(f"{self.label}: {state}; {self.root.completed} processed; {detail}")


NO_PROGRESS = ProgressReporter(enabled=False)

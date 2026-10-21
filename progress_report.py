"""Rich terminal progress with deterministic, credential-safe redirected logs."""

import time
from collections.abc import Iterable
from types import TracebackType

from rich.console import Console, RenderableType
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


class RateColumn(ProgressColumn):
    def render(self, task: Task) -> Text:
        return Text(f"{task.speed:.1f}/s" if task.speed and task.completed > 1 else "--/s")


class DetailedProgress(Progress):
    def get_renderables(self) -> Iterable[RenderableType]:
        tasks = self.tasks
        yield self.make_tasks_table(tasks)
        # Put details on full-width lines so 80-column terminals keep bars/counts visible.
        active = [
            task
            for task in tasks
            if task.fields.get("state") == "working" and task.fields.get("detail")
        ]
        for task in active[-2:]:
            yield Text(f"{task.description}: {task.fields['detail']}")


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
            MofNCompleteColumn(),
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

    def notice(self, message: str) -> None:
        if self.enabled:
            # Text disables Rich markup; strip terminal controls without ever logging payloads.
            safe = "".join(
                char if char >= " " and not 127 <= ord(char) <= 159 else " " for char in message
            )
            self.console.print(Text(f"[{time.monotonic() - self.started:7.1f}s] {safe}"))

    def warning(self, message: str) -> None:
        self.warnings += 1
        self.notice("WARNING: " + message)

    def stage(
        self, label: str, *, total: int | None = None, detail: str = "", transient: bool = False
    ) -> "ProgressTask":
        return ProgressTask(self, label, total=total, detail=detail, transient=transient)


class ProgressTask:
    def __init__(
        self,
        reporter: ProgressReporter,
        label: str,
        *,
        total: int | None,
        detail: str,
        transient: bool,
    ):
        self.reporter = reporter
        self.label = label
        self.total = total
        self.detail = detail
        self.transient = transient
        self.completed = 0
        self.started = time.monotonic()
        self.last_log = self.started
        self.finished = False
        self.task_id: TaskID | None = None
        if reporter.live:
            self.task_id = reporter.renderer.add_task(
                label, total=total, detail=detail, state="working"
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
        if self.task_id is not None:
            self.reporter.renderer.update(
                self.task_id, completed=self.completed, total=self.total, detail=self.detail
            )
        elif force or time.monotonic() - self.last_log >= self.reporter.interval:
            self._log("working")

    def advance(self, amount: int = 1, *, detail: str | None = None, force: bool = False) -> None:
        self.update(completed=self.completed + amount, detail=detail, force=force)

    def finish(self, detail: str | None = None, *, state: str = "done") -> None:
        if self.finished:
            return
        self.finished = True
        if detail is not None:
            self.detail = detail
        if self.task_id is not None:
            self.reporter.renderer.update(
                self.task_id,
                completed=self.completed,
                total=self.total if self.total is not None else self.completed,
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
        elapsed = max(0.0, time.monotonic() - self.started)
        counts = (
            f"{self.completed}/{self.total}"
            if self.total is not None
            else f"{self.completed} processed (total unknown)"
        )
        eta = ""
        if (
            self.total is not None
            and self.completed > 0
            and self.completed < self.total
            and state == "working"
        ):
            eta = f"; ETA ~{elapsed * (self.total - self.completed) / self.completed:.1f}s"
        rate = (
            f"; {self.completed / elapsed:.1f}/s" if self.completed > 1 and elapsed >= 0.1 else ""
        )
        self.reporter.notice(
            f"{self.label}: {state}; {counts}; {elapsed:.1f}s{rate}{eta}"
            + (f"; {self.detail}" if self.detail else "")
        )
        self.last_log = time.monotonic()


NO_PROGRESS = ProgressReporter(enabled=False)

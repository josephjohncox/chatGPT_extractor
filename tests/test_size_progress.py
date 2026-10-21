import io
from contextlib import nullcontext

import pytest
from rich.console import Console

from attachment_transport import save_chunks
from progress_report import ProgressReporter


@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("total", [None, 2 * 1024 * 1024])
def test_redirected_transfer_counts_and_rates_are_readable(monkeypatch, grouped, total):
    clock = [0.0]
    monkeypatch.setattr("progress_report.time.monotonic", lambda: clock[0])
    stream = io.StringIO()
    progress = ProgressReporter(
        console=Console(file=stream, force_terminal=False, width=160), interval=0
    )
    group = progress.group("Attachments") if grouped else None
    with group.activate() if group else nullcontext():
        with progress.stage("Save attachment", total=total, unit="bytes") as work:
            clock[0] = 2.0
            work.advance(1024 * 1024)
    if group:
        group.finish("saved=1")
    text = stream.getvalue()
    assert "1.0 MiB/2.0 MiB" in text if total is not None else "1.0 MiB/?" in text
    assert "512.0 KiB/s" in text
    assert "1048576" not in text and "2097152" not in text
    assert "Attachment bytes" not in text


def test_actual_transfer_uses_save_label_and_readable_size_in_live_panel(tmp_path):
    progress = ProgressReporter(console=Console(file=io.StringIO(), force_terminal=True, width=100))
    group = progress.group("Attachments")
    captured = io.StringIO()

    def chunks():
        yield b"a" * (1024 * 1024)
        Console(file=captured, width=100, color_system=None).print(progress.renderer)
        yield b"b" * (1024 * 1024)

    with group.activate():
        save_chunks(
            chunks(),
            tmp_path / "synthetic.bin",
            total=2 * 1024 * 1024,
            limit=4 * 1024 * 1024,
            progress=progress,
        )
    group.finish("saved=1")
    text = captured.getvalue()
    assert "Save attachment" in text and "1.0 MiB/2.0 MiB" in text
    assert "Attachment bytes" not in text and "1048576" not in text

import io

import pytest
import requests
from rich.console import Console

from attachment_transport import save_chunks
from attachments import AttachmentManager
from chatgpt_client import ChatGPTClient, RetryStopped
from export_config import Config
from progress_report import CountsColumn, ProgressReporter


def reporter(*, terminal=True, width=100, interval=1):
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=terminal, color_system=None, width=width)
    return ProgressReporter(console=console, interval=interval), stream


def test_group_reuses_one_activity_slot_for_hundreds_of_downloads():
    progress, _ = reporter()
    group = progress.group("Attachments")
    slots = set()
    with progress.stage("Export conversations", total=300) as export:
        for index in range(300):
            with group.activate():
                group.current(f"item {index + 1}/300")
                with progress.stage("Conversation attachments", total=1) as files:
                    with progress.stage("Resolver", total=1, transient=True) as resolve:
                        resolve.advance()
                        slots.add(group.activity_id)
                        assert len(progress.renderer.tasks) == 3
                    with progress.stage("Attachment bytes", total=1024, unit="bytes") as data:
                        data.advance(1024)
                        slots.add(group.activity_id)
                        assert len(progress.renderer.tasks) == 3
                    files.advance()
            group.update(completed=index + 1, detail=f"saved={index + 1}")
            export.advance()
            assert progress.renderer.tasks[0].total is None
            assert not progress.renderer.tasks[-1].visible
        group.finish("saved=300")
    assert len(slots) == 1
    assert len(progress.renderer.tasks) == 2
    root = next(task for task in progress.renderer.tasks if task.id == group.root.task_id)
    assert root.completed == root.total == 300 and root.fields["state"] == "done"
    assert not progress._group_stack and not group.tasks


@pytest.mark.parametrize("width", [80, 120, 180])
def test_panel_has_readable_byte_units_and_separate_summary(width):
    progress, _ = reporter(width=width)
    group = progress.group("Attachments")
    group.update(completed=12, detail="saved=10; skipped=2; failed=0; refs so far=15")
    with group.activate():
        group.current("item 3/5 in this conversation")
        with progress.stage("Attachment bytes", total=2 * 1024 * 1024, unit="bytes") as data:
            data.advance(1024 * 1024, detail="receiving content")
            snapshot = io.StringIO()
            Console(file=snapshot, width=width, color_system=None).print(progress.renderer)
            text = snapshot.getvalue()
            assert "Attachments" in text and "saved=10" in text
            assert "1.0 MiB/2.0 MiB" in text and "item 3/5" in text
            assert "1048576" not in text
            assert all(len(line) <= width for line in text.splitlines())
    group.finish("saved=10; skipped=2")


def test_routine_live_notices_update_the_group_without_appending_console_history():
    progress, stream = reporter()
    group = progress.group("Attachments")
    with group.activate():
        with progress.stage("Resolve", total=1):
            for index in range(100):
                progress.notice(f"Attachment transfer [request #{index}]: attempt 1/3")
            assert stream.getvalue() == ""
            progress.warning("Important warning")
            assert "Important warning" in stream.getvalue()
    assert "request #99" in group.last_notice
    group.finish("saved=0")


def test_redirected_logs_are_throttled_but_retry_diagnostics_are_preserved():
    progress, stream = reporter(terminal=False, interval=1000)
    group = progress.group("Attachments")
    with group.activate():
        for _ in range(100):
            with progress.stage("Attachment bytes", total=1) as data:
                data.advance()
            progress.notice("Attachment transfer: attempt 1/3")
        progress.notice("Retry: resolver [request #1]; HTTP 429; backoff 2.0s")
        progress.notice("Recovered HTTP: resolver [request #1]")
    group.finish("saved=100")
    text = stream.getvalue()
    assert text.count("Attachment bytes") == 0
    assert "HTTP 429" in text and "Recovered HTTP" in text
    assert "Attachments: done" in text and len(text.splitlines()) < 8


def test_nested_group_scope_restores_its_parent_and_does_not_capture_later_phases():
    progress, _ = reporter()
    group = progress.group("Attachments")
    with group.activate():
        with progress.stage("Files", total=2) as files:
            with group.activate():
                with progress.stage("Checksum", total=8) as verify:
                    verify.advance(8)
            assert group.tasks == [files]
            assert progress._group_stack == [group]
    with progress.stage("Conversation HTTP", total=1) as request:
        task = next(task for task in progress.renderer.tasks if task.id == request.task_id)
        assert not task.fields.get("group_key")
    group.finish("saved=0")


@pytest.mark.parametrize(
    "error,state",
    [
        (KeyboardInterrupt(), "interrupted"),
        (RuntimeError("PRIVATE_ERROR"), "failed"),
        (
            RetryStopped(
                429, "resolver", request_id=1, reason="single_wait_limit", retry_after=59, spent=0
            ),
            "deferred",
        ),
    ],
)
def test_failed_groups_keep_unknown_totals_and_clean_up_activity(error, state):
    progress, stream = reporter()
    group = progress.group("Attachments")
    with pytest.raises(type(error)):
        with group.activate():
            with progress.stage("Attachment bytes", total=999) as data:
                data.advance(10)
                raise error
    group.finish("saved=0", state=state)
    assert not progress._group_stack and not group.tasks
    assert len(progress.renderer.tasks) == 1
    task = progress.renderer.tasks[0]
    assert task.fields["state"] == state and task.total is None
    assert "PRIVATE_ERROR" not in stream.getvalue()


def test_actual_manager_groups_saved_cached_and_missing_assets_without_private_names(tmp_path):
    progress, stream = reporter()
    ref_ids = [f"file-{i}" for i in range(20)]
    detail = {
        "id": "PRIVATE_CHAT_ID",
        "mapping": {
            "m": {
                "message": {
                    "metadata": {
                        "attachments": [
                            {"id": ident, "name": "PRIVATE_FILENAME.txt"} for ident in ref_ids
                        ]
                    }
                }
            }
        },
    }
    source = tmp_path / "source.json"
    source.write_text("{}")
    (tmp_path / "files").mkdir()
    for ident in ref_ids[:10]:
        (tmp_path / "files" / (ident + "-data.txt")).write_text("synthetic content")
    with AttachmentManager(
        tmp_path / "export", offline_source=source, progress=progress
    ) as manager:
        manager.process(detail)
        manager.process(detail)
        assert len(progress.renderer.tasks) == 2
        assert manager.saved == manager.skipped == 10
        assert manager.unavailable == 20
        assert manager._progress_group is not None
        assert manager._progress_group.root.completed == 40
        assert manager.finish() == 1
    assert len(progress.renderer.tasks) == 1
    assert progress.renderer.tasks[0].fields["state"] == "partial"
    assert (
        "PRIVATE_FILENAME" not in stream.getvalue() and "PRIVATE_CHAT_ID" not in stream.getvalue()
    )


def test_actual_byte_writer_uses_group_slot_and_human_readable_units(tmp_path):
    progress, _ = reporter()
    group = progress.group("Attachments")

    def chunks():
        yield b"data"
        task = next(task for task in progress.renderer.tasks if task.id == group.activity_id)
        assert task.fields["unit"] == "bytes"
        assert "4.0 B/8.0 B" == CountsColumn().render(task).plain
        yield b"more"

    with group.activate():
        result = save_chunks(chunks(), tmp_path / "bytes", total=8, limit=16, progress=progress)
    assert result.size == 8 and len(progress.renderer.tasks) == 2
    group.finish("saved=1")
    assert len(progress.renderer.tasks) == 1


def test_manager_deferred_stop_removes_current_slot_and_leaves_no_active_group(tmp_path):
    class Session(requests.Session):
        def get(self, *args, **kwargs):
            response = requests.Response()
            response.status_code = 429
            response.headers["Retry-After"] = "59"
            response._content = b"{}"
            return response

    progress, _ = reporter()
    client = ChatGPTClient(
        Config(access_token="fake", request_delay=0), Session(), progress=progress
    )
    with pytest.raises(RetryStopped):
        with AttachmentManager(tmp_path, client=client, progress=progress) as manager:
            manager.process(
                {
                    "id": "c",
                    "mapping": {
                        "m": {"message": {"metadata": {"attachments": [{"id": "file-one"}]}}}
                    },
                }
            )
    assert len(progress.renderer.tasks) == 1
    assert progress.renderer.tasks[0].fields["state"] == "deferred"
    assert progress.renderer.tasks[0].total is None
    assert not progress._group_stack


def test_markdown_cache_verification_is_grouped_and_counted_as_skipped(tmp_path):
    source = tmp_path / "source.json"
    source.write_text("{}")
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "file-one-data.txt").write_text("synthetic content")
    detail = {
        "id": "c",
        "update_time": 1,
        "mapping": {"m": {"message": {"metadata": {"attachments": [{"id": "file-one"}]}}}},
    }
    output = tmp_path / "export"
    with AttachmentManager(output, offline_source=source) as original:
        original.process(detail)
        assert original.finish() == 0
    progress, _ = reporter()
    with AttachmentManager(output, progress=progress) as manager:
        assert manager.conversation_complete("c", 1)
        assert manager.conversation_complete("c", 1)
        assert manager.skipped == 1
        assert len(progress.renderer.tasks) == 2
        assert manager.finish() == 0
    assert len(progress.renderer.tasks) == 1
    assert progress.renderer.tasks[0].completed == 1

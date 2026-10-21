import io
import json
from dataclasses import replace

import pytest
import requests
from rich.console import Console

from chatgpt_client import ChatGPTClient, HTTPError
from chatgpt_exporter import export_auxiliary_data, export_conversations, main
from export_config import Config
from local_index import build_index
from progress_report import ProgressReporter, human_bytes


def reporter(*, enabled=True, terminal=False):
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=terminal, color_system=None, width=180)
    return ProgressReporter(enabled=enabled, console=console, interval=0), stream


def chat(ident="a"):
    return {
        "id": ident,
        "title": "PRIVATE_CHAT_TITLE",
        "create_time": 0,
        "update_time": 1,
        "_project_id": "g-p-abc",
        "_folder_name": "PRIVATE_PROJECT_NAME",
        "mapping": {
            "root": {
                "parent": None,
                "children": [],
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["PRIVATE_MESSAGE_CONTENT"]},
                },
            }
        },
        "current_node": "root",
    }


def test_indeterminate_progress_does_not_invent_a_total_or_eta():
    progress, stream = reporter()
    with progress:
        with progress.stage("Discover") as work:
            work.advance(5, detail="page 1")
    text = stream.getvalue()
    assert "total unknown" in text and "5 processed" in text
    assert "ETA" not in text and "page 1" in text
    assert "\x1b" not in text


def test_determinate_partial_progress_keeps_honest_counts():
    progress, stream = reporter()
    with progress.stage("Downloads", total=3) as work:
        work.advance(detail="saved=1")
        work.advance(detail="saved=1; failed=1")
        work.finish(state="partial")
    text = stream.getvalue()
    assert "2/3" in text and "partial" in text
    assert "ETA ~" in text and "failed=1" in text


def test_disabled_progress_preserves_results_and_errors(capsys):
    progress, stream = reporter(enabled=False)
    with progress:
        progress.notice("hidden notice")
        with progress.stage("hidden stage", total=1) as work:
            work.advance()
        progress.result("visible result")
        progress.error("visible error")
    assert stream.getvalue() == "visible error\n"
    assert capsys.readouterr().out == "visible result\n"


def test_failed_stage_does_not_log_arbitrary_exception_details():
    progress, stream = reporter()
    with pytest.raises(RuntimeError):
        with progress.stage("Read data"):
            raise RuntimeError("PRIVATE_EXCEPTION_BODY")
    assert "failed" in stream.getvalue()
    assert "PRIVATE_EXCEPTION_BODY" not in stream.getvalue()


def test_live_progress_never_redirects_or_corrupts_json_stdout(capsys):
    progress, stream = reporter(terminal=True)
    with progress:
        with progress.stage("Probe", total=1) as work:
            work.advance()
            progress.result(json.dumps({"authentication": "ok"}))
    assert json.loads(capsys.readouterr().out) == {"authentication": "ok"}
    assert "Probe" in stream.getvalue()
    assert progress.results == []


def test_live_display_is_stopped_after_keyboard_interrupt():
    progress, stream = reporter(terminal=True)
    with pytest.raises(KeyboardInterrupt):
        with progress:
            with progress.stage("Interrupted operation", total=10) as work:
                work.advance()
                raise KeyboardInterrupt
    assert not progress.renderer.live.is_started
    assert "interrupted" in stream.getvalue()


def test_terminal_control_characters_are_not_interpreted():
    progress, stream = reporter()
    progress.notice("label\x1b[31m\n\x9b31m")
    text = stream.getvalue()
    assert "\x1b" not in text and "\x9b" not in text
    assert "label" in text


def test_retry_and_backoff_are_reported_without_headers_or_body(monkeypatch):
    sleeps = []
    monkeypatch.setattr("chatgpt_client.time.sleep", sleeps.append)

    class Session(requests.Session):
        def __init__(self):
            self.attempts = 0

        def get(self, url, params=None, **kwargs):
            self.attempts += 1
            response = requests.Response()
            response.status_code = 429 if self.attempts == 1 else 200
            response.headers["Retry-After"] = "2"
            response._content = b'{"items": [], "private": "PRIVATE_HTTP_BODY"}'
            return response

    progress, stream = reporter()
    client = ChatGPTClient(
        replace(Config(), request_delay=0, max_retries=1), Session(), progress=progress
    )
    with progress:
        client.json("conversations", "PRIVATE_ACCESS_TOKEN", label="conversation listing")
    text = stream.getvalue()
    assert "attempt 1/2" in text and "attempt 2/2" in text
    assert "HTTP 429" in text and "backoff 2.0s" in text and "Waiting" in text
    assert sleeps == [2]
    assert "PRIVATE_ACCESS_TOKEN" not in text and "PRIVATE_HTTP_BODY" not in text


def test_export_saved_skipped_failed_and_duplicate_counts_are_visible(tmp_path, capsys):
    progress, stream = reporter()
    config = replace(Config(), output_dir=tmp_path)

    def fetch(ident):
        if ident == "b":
            raise HTTPError(404, "conversation detail")
        return chat(ident)

    items = [chat("a"), chat("a"), chat("b"), chat("c")]
    assert export_conversations(items, fetch, config, progress=progress) == 1
    assert "saved=2" in stream.getvalue() and "failed=1" in stream.getvalue()
    assert "1 duplicate entries removed" in stream.getvalue()
    assert "3/3" in stream.getvalue()
    stream.truncate(0)
    stream.seek(0)
    assert export_conversations(items, fetch, config, progress=progress) == 1
    text = stream.getvalue() + capsys.readouterr().out
    assert "skipped=2" in text and "failed=1" in text
    for private in ("PRIVATE_CHAT_TITLE", "PRIVATE_PROJECT_NAME", "PRIVATE_MESSAGE_CONTENT"):
        assert private not in text


def test_checkpoint_failure_is_counted_without_logging_filesystem_details(tmp_path, monkeypatch):
    progress, stream = reporter()

    def fail(*args):
        raise OSError("PRIVATE_FILESYSTEM_DETAIL")

    monkeypatch.setattr("chatgpt_exporter.write_json_file", fail)
    with pytest.raises(OSError):
        export_conversations(
            [chat()], lambda _: chat(), replace(Config(), output_dir=tmp_path), progress=progress
        )
    text = stream.getvalue()
    assert "checkpoint failed" in text and "saved=0" in text and "failed=1" in text
    assert "PRIVATE_FILESYSTEM_DETAIL" not in text


def test_auxiliary_progress_distinguishes_unavailable_failed_and_not_attempted(tmp_path):
    progress, stream = reporter()
    old = tmp_path / "settings/settings.json"
    old.parent.mkdir()
    old.write_text('{"old": true}')

    class Client(ChatGPTClient):
        def json(self, path, token, **kwargs):
            if path == "memories":
                return {"items": ["PRIVATE_MEMORY_CONTENT"]}
            if path == "user_system_messages":
                raise HTTPError(404, path)
            raise HTTPError(403, path)

    with pytest.raises(HTTPError):
        export_auxiliary_data(Client(Config()), "PRIVATE_TOKEN", tmp_path, progress=progress)
    statuses = json.loads((tmp_path / "auxiliary-status.json").read_text())
    assert statuses["memories/memories.json"]["outcome"] == "saved"
    assert statuses["settings/custom_instructions.json"]["outcome"] == "unavailable"
    assert statuses["settings/settings.json"]["outcome"] == "failed"
    assert statuses["settings/models.json"]["attempted"] is False
    assert old.read_text() == '{"old": true}'
    text = stream.getvalue()
    assert "saved=1" in text and "unavailable=1" in text and "failed=1" in text
    assert "not_attempted=5" in text
    assert "PRIVATE_TOKEN" not in text and "PRIVATE_MEMORY_CONTENT" not in text


def test_auxiliary_interrupt_is_checkpointed_as_interrupted(tmp_path):
    progress, stream = reporter()

    class Client(ChatGPTClient):
        def json(self, path, token, **kwargs):
            if path == "memories":
                return {"items": []}
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        export_auxiliary_data(Client(Config()), "fake", tmp_path, progress=progress)
    statuses = json.loads((tmp_path / "auxiliary-status.json").read_text())
    assert statuses["settings/custom_instructions.json"]["outcome"] == "interrupted"
    assert "interrupted=1" in stream.getvalue() and "not_attempted=6" in stream.getvalue()


def test_index_scan_render_and_catalog_phases_are_reported(tmp_path):
    (tmp_path / "chat.json").write_text(json.dumps(chat()))
    progress, stream = reporter()
    result = build_index(tmp_path, progress=progress)
    assert result.chats == 1
    text = stream.getvalue()
    assert "Scan export directory" in text and "candidate files" in text
    assert "Build readable chat pages" in text and "1/1" in text
    assert "Write HTML catalog" in text
    for private in ("PRIVATE_CHAT_TITLE", "PRIVATE_PROJECT_NAME", "PRIVATE_MESSAGE_CONTENT"):
        assert private not in text


@pytest.mark.parametrize("args,env", [(["probe", "--no-progress"], None), (["probe"], "false")])
def test_cli_can_disable_progress_while_stdout_remains_json(args, env, monkeypatch, capsys):
    class Client(ChatGPTClient):
        def access_token(self):
            return "fake"

        def json(self, path, token, **kwargs):
            return {"items": []}

    monkeypatch.setattr("chatgpt_exporter.ChatGPTClient", Client)
    if env is not None:
        monkeypatch.setenv("CHATGPT_PROGRESS", env)
    assert main(args) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["authentication"] == "ok"


def test_full_cli_export_reports_discovery_auxiliary_and_index_without_private_text(
    tmp_path, monkeypatch, capsys
):
    class Client(ChatGPTClient):
        def json(self, path, token, *, params=None, label):
            if path == "conversations":
                return {"items": [{"id": "a"}], "total": 1}
            if path == "gizmos/snorlax/sidebar":
                return {
                    "items": [
                        {
                            "gizmo": {
                                "gizmo": {
                                    "id": "g-p-abc",
                                    "display": {"name": "PRIVATE_PROJECT_NAME"},
                                }
                            }
                        }
                    ],
                    "cursor": None,
                }
            if path == "gizmos/g-p-abc/conversations":
                return {"items": [{"id": "a"}], "cursor": None}
            if path == "conversation/a":
                return chat()
            if path == "memories":
                return {"items": ["PRIVATE_MEMORY_CONTENT"]}
            raise HTTPError(404, path)

    monkeypatch.setattr("chatgpt_exporter.ChatGPTClient", Client)
    monkeypatch.setenv("CHATGPT_ACCESS_TOKEN", "PRIVATE_ACCESS_TOKEN")
    assert main(["export", "--auxiliary", "--output", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    logs = captured.err
    for phase in (
        "Authentication",
        "Regular conversations",
        "Project discovery",
        "Project conversation listings",
        "Conversation discovery",
        "Export conversations",
        "Auxiliary snapshots",
        "Scan export directory",
        "Build readable chat pages",
        "Write HTML catalog",
        "Run finished",
    ):
        assert phase in logs
    assert "1/1" in logs and "8/8" in logs and "saved=1" in logs
    assert (tmp_path / "index.html").is_file()
    for private in (
        "PRIVATE_CHAT_TITLE",
        "PRIVATE_PROJECT_NAME",
        "PRIVATE_MESSAGE_CONTENT",
        "PRIVATE_ACCESS_TOKEN",
        "PRIVATE_MEMORY_CONTENT",
    ):
        assert private not in captured.err + captured.out


def test_cli_interrupt_exits_130_and_restores_terminal(monkeypatch, capsys):
    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr("chatgpt_exporter.run_command", interrupt)
    assert main(["index", "--no-progress"]) == 130
    assert "saved files and checkpoints are retained" in capsys.readouterr().err


@pytest.mark.parametrize("size,expected", [(0, "0.0 B"), (1024, "1.0 KiB"), (1024**2, "1.0 MiB")])
def test_byte_counts_are_readable(size, expected):
    assert human_bytes(size) == expected

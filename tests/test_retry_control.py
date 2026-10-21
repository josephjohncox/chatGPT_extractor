"""Deterministic repro for minute-long waits repeated across otherwise successful requests."""

import io
import json

import pytest
import requests
from rich.console import Console

from attachments import AttachmentManager
from chatgpt_client import ChatGPTClient, ExportError, RetryStopped
from chatgpt_exporter import export_auxiliary_data, main
from export_config import Config
from progress_report import ProgressReporter
from retry_control import retry_delay, server_retry_after


class Response(requests.Response):
    def __init__(self, status=200, retry_after=None, payload=None):
        super().__init__()
        self.status_code = status
        self._content = json.dumps(payload if payload is not None else {"items": []}).encode()
        self._content_consumed = True
        self.closed = False
        if retry_after is not None:
            self.headers["Retry-After"] = str(retry_after)

    def close(self):
        self.closed = True
        super().close()


class Session(requests.Session):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.calls = 0
        self.last = None

    def get(self, url, params=None, **kwargs):
        self.calls += 1
        self.last = self.responses.pop(0)
        if isinstance(self.last, Exception):
            raise self.last
        return self.last


def make_client(responses, **env):
    stream = io.StringIO()
    progress = ProgressReporter(console=Console(file=stream, force_terminal=False), interval=0)
    session = Session(responses)
    config = Config.from_env({"CHATGPT_REQUEST_DELAY": "0", **env})
    return ChatGPTClient(config, session, progress=progress), session, stream


def test_long_retry_after_does_not_silently_sleep_for_minutes(monkeypatch):
    waits = []
    monkeypatch.setattr("chatgpt_client.time.sleep", waits.append)
    client, _, _ = make_client([Response(429, 59) for _ in range(8)])
    with pytest.raises(ExportError):
        client.json("conversations", "fake", label="conversation detail")
    assert sum(waits) <= 30 and max(waits, default=0) <= 10


def test_retry_sleep_budget_does_not_reset_after_each_successful_request(monkeypatch):
    waits = []
    monkeypatch.setattr("chatgpt_client.time.sleep", waits.append)
    responses = [response for _ in range(20) for response in (Response(429, 2), Response())]
    client, _, _ = make_client(responses)
    stopped = False
    for _ in range(20):
        try:
            client.json("conversations", "fake", label="conversation detail")
        except ExportError:
            stopped = True
            break
    assert stopped, "A per-request reset must not allow an effectively unbounded run of cooldowns"
    assert sum(waits) <= 30


def test_retry_response_is_closed_before_waiting(monkeypatch):
    response = Response(429, 1)
    client, _, _ = make_client([response, Response()])

    def check_closed(_):
        assert response.closed, "The retry must release its response before sleeping"

    monkeypatch.setattr("chatgpt_client.time.sleep", check_closed)
    assert client.json("conversations", "fake", label="conversation detail") == {"items": []}


def test_stop_is_latched_and_does_not_send_more_items(monkeypatch):
    monkeypatch.setattr("chatgpt_client.time.sleep", lambda _: None)
    client, session, _ = make_client([Response(429, 59), Response()])
    for _ in range(3):
        with pytest.raises(RetryStopped):
            client.json("conversations", "fake", label="conversation detail")
    assert session.calls == 1


def test_persistent_429_stops_after_request_retry_limit_without_advancing_items(monkeypatch):
    waits = []
    monkeypatch.setattr("chatgpt_client.time.sleep", waits.append)
    client, session, _ = make_client([Response(429, 1) for _ in range(10)])
    with pytest.raises(RetryStopped) as exc:
        client.json("conversations", "fake", label="conversation detail")
    assert session.calls == 3 and waits == [1, 1]
    assert exc.value.status == 429 and exc.value.reason == "attempts_exhausted"


def test_explicit_larger_budget_respects_server_delay_without_shortening(monkeypatch):
    waits = []
    monkeypatch.setattr("chatgpt_client.time.sleep", waits.append)
    client, _, _ = make_client(
        [Response(429, 59), Response()],
        CHATGPT_MAX_RETRY_WAIT="60",
        CHATGPT_MAX_TOTAL_RETRY_WAIT="60",
    )
    assert client.json("conversations", "fake", label="test") == {"items": []}
    assert waits == [59]


def test_connection_pools_are_cleared_on_network_failure_without_losing_cookies(monkeypatch):
    monkeypatch.setattr("chatgpt_client.time.sleep", lambda _: None)
    client, session, _ = make_client(
        [requests.ConnectionError("PRIVATE_NETWORK_DETAIL"), Response()]
    )
    closed = []
    session.cookies.set("session", "PRIVATE_COOKIE")
    for adapter in session.adapters.values():
        monkeypatch.setattr(adapter, "close", lambda: closed.append(True))
    assert client.json("conversations", "PRIVATE_TOKEN", label="test") == {"items": []}
    assert len(closed) == 2 and session.cookies.get("session") == "PRIVATE_COOKIE"


def test_export_defers_75_and_preserves_completed_conversation_checkpoint(
    tmp_path, monkeypatch, capsys
):
    session = Session(
        [Response(payload={"id": "one", "mapping": {}, "update_time": 1}), Response(429, 59)]
    )

    class Client(ChatGPTClient):
        def __init__(self, config, *, progress):
            super().__init__(config, session, progress=progress)

        def access_token(self):
            return "fake"

        def all_conversations(self, token):
            return [
                {"id": "one", "title": "First", "update_time": 1},
                {"id": "two", "title": "Second", "update_time": 1},
            ]

    monkeypatch.setattr("chatgpt_exporter.ChatGPTClient", Client)
    assert (
        main(
            [
                "export",
                "--output",
                str(tmp_path),
                "--no-attachments",
                "--no-auxiliary",
                "--no-index",
                "--no-progress",
            ]
        )
        == 75
    )
    index = json.loads((tmp_path / ".export-index.json").read_text())
    assert "json:one" in index and "json:two" not in index
    assert session.calls == 2
    assert "59.0s" in capsys.readouterr().err


def test_attachment_retry_stop_is_not_swallowed_as_one_missing_file(tmp_path):
    client, session, _ = make_client([Response(429, 59), Response()])
    client.config = Config(access_token="fake", request_delay=0)
    detail = {
        "id": "c",
        "mapping": {
            "m": {
                "message": {"metadata": {"attachments": [{"id": "file-one"}, {"id": "file-two"}]}}
            }
        },
    }
    with AttachmentManager(tmp_path, client=client) as assets:
        with pytest.raises(RetryStopped):
            assets.process(detail)
        assert session.calls == 1
        assert {item["outcome"] for item in assets.data["assets"].values()} == {
            "failed",
            "not_attempted",
        }


def test_auxiliary_retry_stop_leaves_remaining_snapshots_unattempted(tmp_path):
    client, session, _ = make_client([Response(429, 59), Response()])
    with pytest.raises(RetryStopped):
        export_auxiliary_data(client, "fake", tmp_path)
    statuses = json.loads((tmp_path / "auxiliary-status.json").read_text())
    assert session.calls == 1
    assert statuses["memories/memories.json"]["outcome"] == "failed"
    assert statuses["settings/settings.json"]["outcome"] == "not_attempted"


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "invalid", ""])
def test_invalid_retry_after_uses_short_local_backoff(value):
    assert server_retry_after(value) is None
    assert retry_delay(value, 0) == 1


def test_retry_logs_identify_different_requests_and_recovery(monkeypatch):
    monkeypatch.setattr("chatgpt_client.time.sleep", lambda _: None)
    client, _, stream = make_client([Response(429, 1), Response(), Response(429, 1), Response()])
    for _ in range(2):
        client.json("conversations", "fake", label="conversation detail")
    text = stream.getvalue()
    assert "request #1" in text and "request #2" in text
    assert text.lower().count("recovered") >= 2

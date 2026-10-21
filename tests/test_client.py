import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
import requests

from chatgpt_client import ChatGPTClient, ExportError, HTTPError, retry_delay
from export_config import Config


def response(payload=None, status=200, headers=None, raw=None):
    result = requests.Response()
    result.status_code = status
    result._content = raw if raw is not None else json.dumps(payload or {}).encode()
    result.headers.update(headers or {})
    return result


class Transport(requests.Session):
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, {**kwargs, "params": params}))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def client(transport, **overrides):
    return ChatGPTClient(replace(Config(), request_delay=0, max_retries=0, **overrides), transport)


def test_auth_exchanges_split_cookie_but_never_logs_it(capsys):
    transport = Transport(
        response({"accessToken": "fake-access-secret", "user": {"email": "private"}})
    )
    instance = client(
        transport,
        session_token_0="fake-part0",
        session_token_1="fake-part1",
        workspace_id="account-1",
    )
    assert instance.access_token() == "fake-access-secret"
    url, call = transport.calls[0]
    assert url.endswith("/api/auth/session")
    assert (
        call["headers"]["Cookie"]
        == "__Secure-next-auth.session-token.0=fake-part0; __Secure-next-auth.session-token.1=fake-part1"
    )
    assert call["timeout"] == 30
    assert call["allow_redirects"] is False
    assert not capsys.readouterr().out
    assert "Cookie" not in instance.headers("fake-access-secret")
    assert instance.headers("fake-access-secret")["ChatGPT-Account-ID"] == "account-1"


def test_session_cloudflare_challenge_is_explicit_and_body_stays_private(capsys):
    transport = Transport(
        response(
            status=403,
            headers={"cf-mitigated": "challenge", "Content-Type": "text/html"},
            raw=b"<html>fake-cookie-secret and fake-access-secret</html>",
        )
    )
    instance = client(transport, session_token_0="fake-part0", session_token_1="fake-part1")
    with pytest.raises(HTTPError, match="Cloudflare browser challenge") as exc:
        instance.access_token()
    message = str(exc.value)
    assert "scripts/probe-chatgpt.js" in message
    assert "CHATGPT_ACCESS_TOKEN" in message
    assert "not a verdict" in message
    assert "fake-cookie-secret" not in message and "fake-access-secret" not in message
    assert capsys.readouterr().out == ""


def test_default_user_agent_is_desktop_chrome_not_generic():
    headers = client(Transport()).headers()
    assert "Chrome/" in headers["User-Agent"]
    assert headers["User-Agent"] != "Mozilla/5.0"


def test_configured_user_agent_is_used_by_session_and_backend_headers():
    custom = "Mozilla/5.0 CustomBrowser/123"
    transport = Transport(response({"accessToken": "fake-access"}), response({"items": []}))
    config = Config.from_env(
        {
            "CHATGPT_USER_AGENT": custom,
            "CHATGPT_SESSION_TOKEN": "fake-cookie",
            "CHATGPT_REQUEST_DELAY": "0",
        }
    )
    instance = ChatGPTClient(config, transport)
    token = instance.access_token()
    assert instance.json("conversations", token, label="test") == {"items": []}
    assert len(transport.calls) == 2
    assert all(call[1]["headers"]["User-Agent"] == custom for call in transport.calls)


def test_access_token_does_not_exchange_session():
    transport = Transport()
    assert client(transport, access_token="fake-access").access_token() == "fake-access"
    assert transport.calls == []


@pytest.mark.parametrize("payload", [{}, {"accessToken": None}, {"accessToken": []}])
def test_missing_access_token(payload):
    with pytest.raises(ExportError, match="no accessToken"):
        client(Transport(response(payload)), session_token="fake").access_token()


@pytest.mark.parametrize("status", [200, 403, 429])
def test_explicit_challenge_header_is_not_treated_as_token_or_json_success(status):
    instance = client(
        Transport(
            response(
                {"accessToken": "fake-access-secret"},
                status=status,
                headers={"CF-Mitigated": "challenge"},
            )
        )
    )
    with pytest.raises(HTTPError, match="Cloudflare browser challenge") as exc:
        instance.json("conversations", "fake-access", label="conversation listing")
    assert exc.value.browser_challenge
    assert "backend request is browser-gated" in str(exc.value)
    assert "fake-access-secret" not in str(exc.value)


def test_cloudflare_server_header_alone_is_not_proof_of_challenge():
    instance = client(
        Transport(
            response(status=403, headers={"Server": "cloudflare"}, raw=b"<html>private</html>")
        )
    )
    with pytest.raises(HTTPError) as exc:
        instance.json("conversations", "fake-access", label="conversation listing")
    assert not exc.value.browser_challenge
    assert "challenge detected" not in str(exc.value)


def test_explicit_429_challenge_is_not_retried_as_rate_limiting(monkeypatch):
    monkeypatch.setattr(
        "chatgpt_client.time.sleep",
        lambda _: pytest.fail("A browser challenge must not be retried"),
    )
    transport = Transport(response(status=429, headers={"cf-mitigated": "challenge"}))
    instance = ChatGPTClient(replace(Config(), request_delay=0, max_retries=3), transport)
    with pytest.raises(HTTPError, match="Cloudflare browser challenge"):
        instance.json("conversations", "fake-access", label="conversation listing")
    assert len(transport.calls) == 1


def test_missing_credentials_fails_before_network():
    with pytest.raises(ExportError, match="mise.local.toml"):
        client(Transport()).access_token()


@pytest.mark.parametrize("status", [401, 403, 404, 302, 429, 503])
def test_http_failures_omit_body(status):
    with pytest.raises(HTTPError) as exc:
        client(Transport(response(status=status, raw=b"fake-access-secret"))).json(
            "conversations", "token", label="test"
        )
    assert exc.value.status == status
    assert "fake-access-secret" not in str(exc.value)


@pytest.mark.parametrize("raw", [b"<html>fake-secret</html>", b"", b"[]", b"null"])
def test_bad_json_shape_does_not_leak(raw):
    with pytest.raises(ExportError) as exc:
        client(Transport(response(raw=raw))).json("conversations", "token", label="test")
    assert "fake-secret" not in str(exc.value)


def test_retry_transient_and_rate_limit(monkeypatch):
    sleeps = []
    monkeypatch.setattr("chatgpt_client.time.sleep", sleeps.append)
    transport = Transport(
        response(status=429, headers={"Retry-After": "2"}),
        response(status=503),
        response({"items": []}),
    )
    instance = ChatGPTClient(replace(Config(), request_delay=0, max_retries=2), transport)
    assert instance.json("conversations", "token", label="test") == {"items": []}
    assert len(transport.calls) == 3
    assert 2 in sleeps and 8 in sleeps


def test_network_error_details_are_withheld(monkeypatch):
    monkeypatch.setattr("chatgpt_client.time.sleep", lambda _: None)
    transport = Transport(requests.ConnectionError("fake-secret in URL"))
    with pytest.raises(ExportError, match="network request failed") as exc:
        client(transport).json("conversations", "token", label="test")
    assert "fake-secret" not in str(exc.value)


def test_retry_after_date_and_bounds():
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=30))
    assert 25 <= retry_delay(future, 0) <= 31
    assert retry_delay("9999999", 0) == 120
    assert retry_delay("-3", 0) == 1
    assert retry_delay("garbage", 2) == 16


def test_credentials_never_sent_off_origin():
    with pytest.raises(ExportError, match="another origin"):
        client(Transport()).get(
            "https://chatgpt.com.evil.invalid/", headers={"Authorization": "secret"}, label="test"
        )


def test_offset_pagination_without_total_keeps_short_pages():
    transport = Transport(
        response({"items": [{"id": "a"}]}),
        response({"items": [{"id": "b"}]}),
        response({"items": []}),
    )
    result = client(transport).conversations("token")
    assert [item["id"] for item in result] == ["a", "b"]
    assert [call[1]["params"]["offset"] for call in transport.calls] == [0, 1, 2]


def test_total_stops_without_extra_request():
    transport = Transport(response({"items": [{"id": "a"}], "total": 1}))
    assert len(client(transport).conversations("token")) == 1
    assert len(transport.calls) == 1


def test_offset_loop_and_premature_empty_page_fail():
    for payloads in [
        [{"items": [{"id": "a"}]}, {"items": [{"id": "a"}]}],
        [{"items": [], "total": 9}],
        [{"items": [], "has_more": True}],
    ]:
        with pytest.raises(ExportError):
            client(Transport(*(response(p) for p in payloads))).conversations("token")


def test_list_failures_are_not_silent_partial_success():
    transport = Transport(response({"items": [{"id": "a"}], "total": 2}), response(status=500))
    with pytest.raises(HTTPError):
        client(transport).conversations("token")


def test_cursor_pagination_normalizes_project_slug_and_tags_folder():
    transport = Transport(
        response({"items": [{"id": "a"}], "cursor": "next"}),
        response({"conversations": [{"id": "b"}], "cursor": None}),
    )
    result = client(transport).conversations("token", "g-p-abc123-research", "Research")
    assert [item["id"] for item in result] == ["a", "b"]
    assert all(item["_folder_name"] == "Research" for item in result)
    assert "/gizmos/g-p-abc123/conversations" in transport.calls[0][0]
    assert [call[1]["params"]["cursor"] for call in transport.calls] == ["0", "next"]


@pytest.mark.parametrize(
    "payload",
    [
        {"items": [{"id": "a"}], "cursor": "0"},
        {"items": [], "cursor": "next"},
        {"items": [{"id": "a"}], "has_more": True},
        {"items": "changed"},
        {"items": [{}]},
    ],
)
def test_bad_project_pagination_fails(payload):
    with pytest.raises(ExportError):
        client(Transport(response(payload))).conversations("token", "g-p-abc")


def test_project_discovery_nested_shapes_pagination_overrides():
    transport = Transport(
        response(
            {
                "items": [{"gizmo": {"gizmo": {"id": "g-p-abc", "display": {"name": "Original"}}}}],
                "cursor": "next",
            }
        ),
        response(
            {
                "items": [
                    {"gizmo": {"id": "g-p-def", "display": {"name": "Shared"}}},
                    {"id": "g-custom-gpt"},
                ]
            }
        ),
    )
    instance = client(transport, projects=(("g-p-abc", "Override"),))
    assert instance.projects("token") == {"g-p-abc": "Override", "g-p-def": "Shared"}
    assert transport.calls[0][1]["params"]["owned_only"] == "false"
    assert transport.calls[1][1]["params"]["cursor"] == "next"


def test_all_conversations_deduplicates_and_preserves_project_tag(monkeypatch):
    instance = client(Transport(), include_archived=True)
    calls = []

    def conversations(token, project_id="", folder_name="", *, archived=False):
        calls.append((project_id, archived))
        return (
            [{"id": "same", "_folder_name": folder_name}]
            if project_id
            else [{"id": "same"}, {"id": "archived" if archived else "normal"}]
        )

    monkeypatch.setattr(instance, "conversations", conversations)
    monkeypatch.setattr(instance, "projects", lambda token: {"g-p-abc": "Research"})
    result = {item["id"]: item for item in instance.all_conversations("token")}
    assert set(result) == {"same", "normal", "archived"}
    assert result["same"]["_folder_name"] == "Research"
    assert calls == [("", False), ("", True), ("g-p-abc", False)]


def test_bearer_probe_does_not_claim_verified_auth_when_backend_is_challenged():
    transport = Transport(
        *(response(status=403, headers={"cf-mitigated": "challenge"}) for _ in range(3))
    )
    report = client(transport, access_token="fake-configured-bearer").probe()
    assert report["authentication"] == "unverified"
    assert report["token_source"] == "configured_bearer"
    assert all(
        item["status"] == 403 and item["browser_challenge"] for item in report["endpoints"].values()
    )
    assert "fake-configured-bearer" not in json.dumps(report)


def test_probe_prints_no_payload_values():
    transport = Transport(
        *(
            response({"items": [{"title": "private title"}], "accessToken": "private-token"})
            for _ in range(3)
        )
    )
    report = client(transport, access_token="fake-access").probe()
    serialized = json.dumps(report)
    assert "private title" not in serialized and "private-token" not in serialized
    assert report["authentication"] == "ok"


@pytest.mark.parametrize(
    "item", [{"unexpected": {}}, {"gizmo": {"id": "g-p-abc", "display": "changed"}}]
)
def test_project_discovery_unrecognized_shape_fails(item):
    with pytest.raises(ExportError, match="changed shape"):
        client(Transport(response({"items": [item]}))).projects("token")


def test_contradictory_total_and_end_flag_fails():
    with pytest.raises(ExportError, match="reported total"):
        client(
            Transport(response({"items": [{"id": "a"}], "total": 2, "has_more": False}))
        ).conversations("token")


def test_project_pagination_new_cursors_with_identical_items_fails():
    transport = Transport(
        response({"items": [{"id": "a"}], "cursor": "next"}),
        response({"items": [{"id": "a"}], "cursor": "another"}),
    )
    with pytest.raises(ExportError, match="repeated a page"):
        client(transport).conversations("token", "g-p-abc")

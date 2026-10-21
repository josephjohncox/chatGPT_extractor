import hashlib

import pytest
import requests

from attachment_transport import AttachmentHTTP, LibraryCookieError, checked_download_url
from chatgpt_client import ChatGPTClient, ExportError, HTTPError
from export_config import Config


class StreamResponse(requests.Response):
    def __init__(self, chunks=None, status=200, headers=None):
        super().__init__()
        self.status_code = status
        self.headers.update(headers or {})
        self.chunks = chunks if chunks is not None else [b"data"]
        self.visited = self.closed = False

    def iter_content(self, chunk_size=1, decode_unicode=False):
        self.visited = True
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class Session(requests.Session):
    def __init__(self, responses, calls):
        super().__init__()
        self.responses = responses
        self.calls = calls
        self.headers["Authorization"] = "PRIVATE_DEFAULT_AUTH"
        self.headers["Cookie"] = "PRIVATE_DEFAULT_COOKIE"
        self.cookies.set("private-cookie", "PRIVATE_JAR_COOKIE")
        self.auth = ("PRIVATE_NETRC_USER", "PRIVATE_NETRC_PASSWORD")

    def get(self, url, params=None, **kwargs):
        self.calls.append(
            {
                "url": url,
                "headers": {**self.headers, **kwargs.get("headers", {})},
                "cookies": self.cookies.get_dict(),
                "auth": self.auth,
                "trust_env": self.trust_env,
                **{key: value for key, value in kwargs.items() if key != "headers"},
            }
        )
        return self.responses.pop(0)


def transport(responses, monkeypatch):
    monkeypatch.setattr("chatgpt_client.time.sleep", lambda _: None)
    calls = []
    own = Session(responses, calls)
    client = ChatGPTClient(Config(request_delay=0, max_retries=0), own)
    downloader = AttachmentHTTP(client, session_factory=lambda: Session(responses, calls))
    return downloader, calls


@pytest.mark.parametrize(
    "url",
    [
        "http://files.oaiusercontent.com/a",
        "file:///etc/passwd",
        "https://127.0.0.1/a",
        "https://localhost/a",
        "https://files.oaiusercontent.com.evil.invalid/a",
        "https://evil.invalid/a",
        "https://user:password@files.oaiusercontent.com/a",
        "https://files.oaiusercontent.com:8443/a",
        "https://chatgpt.com/api/auth/session",
        "https://chatgpt.com/backend-api/files/../accounts",
        "https://chatgpt.com/backend-api/files/%252e%252e/a",
        "https://files.oaiusercontent.com/a#fragment",
        "https://evil.blob.core.windows.net/a",
    ],
)
def test_disallowed_urls_are_rejected_before_requests(url):
    with pytest.raises(ExportError):
        checked_download_url(url)


def test_signed_storage_redirects_never_receive_chatgpt_credentials(tmp_path, monkeypatch):
    responses = [
        StreamResponse(
            status=302,
            headers={"Location": "https://files.oaiusercontent.com/blob?sig=PRIVATE_SIGNED_URL"},
        ),
        StreamResponse(
            [b"abc", b"def"], headers={"Content-Length": "6", "Content-Type": "image/png"}
        ),
    ]
    downloader, calls = transport(responses, monkeypatch)
    target = tmp_path / "asset.bin"
    result = downloader.download(
        "https://chatgpt.com/backend-api/estuary/content?id=file-test&sig=PRIVATE",
        target,
        "PRIVATE_BEARER",
        max_bytes=32,
    )
    assert target.read_bytes() == b"abcdef"
    assert result.sha256 == hashlib.sha256(b"abcdef").hexdigest()
    assert calls[0]["headers"]["Authorization"] == "Bearer PRIVATE_BEARER"
    storage = calls[1]
    assert "Authorization" not in storage["headers"] and "Cookie" not in storage["headers"]
    assert storage["cookies"] == {} and storage["auth"] is None and storage["trust_env"] is False
    assert all(call["allow_redirects"] is False and call["stream"] is True for call in calls)
    assert target.stat().st_mode & 0o777 == 0o600


def test_library_content_gets_session_cookies_but_redirected_storage_does_not(
    tmp_path, monkeypatch
):
    responses = [
        StreamResponse(
            status=302,
            headers={"Location": "https://files.oaiusercontent.com/blob?sig=PRIVATE_SIGNATURE"},
        ),
        StreamResponse([b"ok"]),
    ]
    downloader, calls = transport(responses, monkeypatch)
    downloader.client.config = Config(
        request_delay=0,
        max_retries=0,
        session_token_0="PRIVATE_PART0",
        session_token_1="PRIVATE_PART1",
    )
    downloader.download(
        "/api/library/files/file-test/content?token=PRIVATE",
        tmp_path / "file",
        "PRIVATE_BEARER",
        max_bytes=32,
    )
    assert (
        calls[0]["headers"]["Cookie"]
        == "__Secure-next-auth.session-token.0=PRIVATE_PART0; __Secure-next-auth.session-token.1=PRIVATE_PART1"
    )
    assert "Cookie" not in calls[1]["headers"] and "Authorization" not in calls[1]["headers"]


def test_library_401_explains_cookie_requirement_without_token_values(tmp_path, monkeypatch):
    downloader, _ = transport([StreamResponse(status=401)], monkeypatch)
    with pytest.raises(LibraryCookieError) as exc:
        downloader.download(
            "/api/library/files/file-test/content?sig=PRIVATE_SIGNATURE",
            tmp_path / "file",
            "PRIVATE_BEARER",
            max_bytes=32,
        )
    assert "CHATGPT_SESSION_TOKEN" in str(exc.value)
    assert "bearer token may remain configured" in str(exc.value)
    assert "PRIVATE_" not in str(exc.value)


def test_redirect_to_unapproved_host_is_not_followed(tmp_path, monkeypatch):
    downloader, calls = transport(
        [StreamResponse(status=302, headers={"Location": "https://evil.invalid/?secret=PRIVATE"})],
        monkeypatch,
    )
    with pytest.raises(ExportError) as exc:
        downloader.download(
            "https://files.oaiusercontent.com/a", tmp_path / "file", "PRIVATE_BEARER", max_bytes=32
        )
    assert len(calls) == 1 and "PRIVATE" not in str(exc.value)


def test_declared_and_streamed_size_caps_preserve_existing_file(tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_bytes(b"old")
    for response in (
        StreamResponse([b"abc"], headers={"Content-Length": "100"}),
        StreamResponse([b"ab", b"cd"]),
        StreamResponse([b"abcdef"], headers={"Content-Length": "2", "Content-Encoding": "gzip"}),
    ):
        downloader, _ = transport([response], monkeypatch)
        with pytest.raises(ExportError, match="size limit"):
            downloader.download("https://files.oaiusercontent.com/a", target, "fake", max_bytes=3)
        assert target.read_bytes() == b"old"
        assert not list(tmp_path.glob(".download-*"))


def test_truncated_stream_never_commits_partial_file(tmp_path, monkeypatch):
    response = StreamResponse([b"abc"], headers={"Content-Length": "5"})
    downloader, _ = transport([response], monkeypatch)
    with pytest.raises(ExportError, match="truncated"):
        downloader.download(
            "https://files.oaiusercontent.com/a", tmp_path / "file", "fake", max_bytes=32
        )
    assert not (tmp_path / "file").exists() and response.closed
    assert not list(tmp_path.glob(".download-*"))


def test_challenge_body_is_not_downloaded_or_logged(tmp_path, monkeypatch):
    response = StreamResponse(
        [b"PRIVATE_RESPONSE_BODY"], status=403, headers={"cf-mitigated": "challenge"}
    )
    downloader, calls = transport([response], monkeypatch)
    with pytest.raises(HTTPError) as exc:
        downloader.download(
            "https://files.oaiusercontent.com/a?sig=PRIVATE_SIGNATURE",
            tmp_path / "file",
            "PRIVATE_BEARER",
            max_bytes=32,
        )
    assert exc.value.browser_challenge and not response.visited
    assert "PRIVATE" not in str(exc.value)
    assert len(calls) == 1 and not (tmp_path / "file").exists()


def test_binary_retry_restarts_cleanly_after_stream_failure(tmp_path, monkeypatch):
    responses = [
        StreamResponse([b"partial", requests.ConnectionError("PRIVATE_SIGNATURE")]),
        StreamResponse([b"complete"]),
    ]
    downloader, calls = transport(responses, monkeypatch)
    downloader.client.config = Config(request_delay=0, max_retries=1)
    target = tmp_path / "file"
    downloader.download("https://files.oaiusercontent.com/a", target, "fake", max_bytes=32)
    assert target.read_bytes() == b"complete" and len(calls) == 2
    assert not list(tmp_path.glob(".download-*"))


def test_redirect_loops_are_bounded(tmp_path, monkeypatch):
    responses = [
        StreamResponse(status=302, headers={"Location": "https://files.oaiusercontent.com/a"})
        for _ in range(5)
    ]
    downloader, calls = transport(responses, monkeypatch)
    with pytest.raises(ExportError, match="redirect limit"):
        downloader.download(
            "https://files.oaiusercontent.com/a", tmp_path / "file", "fake", max_bytes=32
        )
    assert len(calls) == 4

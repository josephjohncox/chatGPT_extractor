"""Bounded, atomic binary transfers with explicit redirect and credential boundaries."""

import hashlib
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import requests

from chatgpt_client import ChatGPTClient, ExportError, HTTPError
from progress_report import ProgressReporter, human_bytes

AZURE_HOSTS = {"oaidalleapiprodscus.blob.core.windows.net", "oaiusercontent.blob.core.windows.net"}


def checked_download_url(value: str) -> str:
    url = urljoin("https://chatgpt.com/", value)
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ExportError("Invalid attachment URL (details withheld)") from None
    decoded_path = unquote(parsed.path)
    if (
        any(part in {".", ".."} for part in decoded_path.split("/"))
        or "\\" in decoded_path
        or re.search(r"%[0-9a-fA-F]{2}", decoded_path)
    ):
        raise ExportError("Attachment URL has an unsafe encoded path")
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ExportError("Unsafe attachment URL (details withheld)")
    if host == "chatgpt.com":
        if parsed.path != "/backend-api/estuary/content" and not parsed.path.startswith(
            ("/backend-api/estuary/content/", "/api/library/files/")
        ):
            raise ExportError("Attachment URL is outside approved content paths")
    elif not (
        host == "oaiusercontent.com" or host.endswith(".oaiusercontent.com") or host in AZURE_HOSTS
    ):
        raise ExportError("Attachment URL host is not an approved OpenAI content host")
    if "\\" in url or any(ord(c) < 32 for c in url):
        raise ExportError("Unsafe attachment URL characters")
    return url


class LibraryCookieError(HTTPError):
    def __init__(self, *, configured: bool):
        super().__init__(401, "attachment library download")
        state = (
            "The library request was not authorized with the configured session cookies"
            if configured
            else "No session cookies are configured"
        )
        self.args = (
            f"Attachment library download: HTTP 401. {state}. Some /api/library/files downloads require browser session cookies in addition to a bearer token. Copy the single cookie into CHATGPT_SESSION_TOKEN or the .0/.1 pair into CHATGPT_SESSION_TOKEN_0/_1 from the same signed-in tab. The bearer token may remain configured. See docs/authentication.md; do not share cookie values.",
        )


@dataclass(frozen=True)
class DownloadedFile:
    size: int
    sha256: str
    mime: str


class AttachmentHTTP:
    def __init__(
        self,
        client: ChatGPTClient,
        *,
        session_factory: Callable[[], requests.Session] | None = None,
    ):
        self.client = client
        self.session_factory = session_factory or requests.Session

    def download(
        self, url: str, destination: Path, token: str, *, max_bytes: int
    ) -> DownloadedFile:
        origin = checked_download_url(url)
        report = self.client.progress
        request_id = self.client.next_request_id()
        for attempt in range(self.client.config.max_retries + 1):
            current = origin
            redirects = 0
            same_origin = urlsplit(current).hostname == "chatgpt.com"
            try:
                while True:
                    current = checked_download_url(current)
                    same_origin = urlsplit(current).hostname == "chatgpt.com"
                    session = self.client.session if same_origin else self.session_factory()
                    if not same_origin:
                        # Signed storage URLs authenticate themselves. Never inherit netrc, auth, or cookies.
                        session.headers.clear()
                        session.cookies.clear()
                        session.auth = None
                        session.cert = None
                        session.proxies.clear()
                        session.verify = True
                        session.trust_env = False
                    response = None
                    try:
                        if self.client._last_request is not None:
                            self.client._wait(
                                max(
                                    0,
                                    self.client.config.request_delay
                                    - (time.monotonic() - self.client._last_request),
                                ),
                                "attachment request pacing",
                            )
                        headers = (
                            self.client.headers(token)
                            if same_origin
                            else {"User-Agent": self.client.config.user_agent}
                        )
                        headers["Accept"] = "*/*"
                        if same_origin and self.client.config.cookie():
                            headers["Cookie"] = self.client.config.cookie()
                        report.notice(
                            f"Attachment transfer [request #{request_id}]: attempt {attempt + 1}/{self.client.config.max_retries + 1}; redirect {redirects}/3; timeout {self.client.config.timeout:.1f}s"
                        )
                        self.client._last_request = time.monotonic()
                        response = session.get(
                            current,
                            headers=headers,
                            stream=True,
                            allow_redirects=False,
                            timeout=self.client.config.timeout,
                        )
                        challenge = (
                            response.headers.get("cf-mitigated", "").strip().lower() == "challenge"
                        )
                        if challenge:
                            raise HTTPError(
                                response.status_code, "attachment transfer", browser_challenge=True
                            )
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if redirects >= 3 or not response.headers.get("Location"):
                                raise ExportError("Attachment redirect limit or missing target")
                            current = checked_download_url(
                                urljoin(current, response.headers["Location"])
                            )
                            redirects += 1
                            continue
                        if response.status_code in {429, 500, 502, 503, 504} and (
                            response.status_code == 429 or attempt < self.client.config.max_retries
                        ):
                            status = response.status_code
                            header = response.headers.get("Retry-After")
                            # A streaming response must not retain its connection through cooldown.
                            response.close()
                            response = None
                            if not same_origin:
                                session.close()
                            self.client.wait_for_retry(
                                status=status,
                                label="attachment transfer",
                                request_id=request_id,
                                attempt=attempt,
                                header=header,
                            )
                            break
                        if (
                            response.status_code == 401
                            and same_origin
                            and urlsplit(current).path.startswith("/api/library/files/")
                        ):
                            raise LibraryCookieError(configured=bool(self.client.config.cookie()))
                        if response.status_code != 200:
                            raise HTTPError(response.status_code, "attachment transfer")
                        result = self._save(response, destination, max_bytes)
                        if attempt:
                            report.notice(
                                f"Recovered HTTP: attachment transfer [request #{request_id}]; attempt {attempt + 1}/{self.client.config.max_retries + 1}; saved {human_bytes(result.size)}"
                            )
                        return result
                    finally:
                        if response is not None:
                            response.close()
                        if not same_origin:
                            session.close()
            except requests.RequestException:
                if same_origin:
                    self.client.reset_connections()
                self.client.wait_for_retry(
                    status=0,
                    label="attachment transfer",
                    request_id=request_id,
                    attempt=attempt,
                    header=None,
                )
        raise ExportError("Attachment transfer exhausted retries")

    def _save(self, response: requests.Response, destination: Path, limit: int) -> DownloadedFile:
        total = None
        length = response.headers.get("Content-Length")
        if length is not None and not response.headers.get("Content-Encoding"):
            try:
                total = int(length)
            except ValueError:
                raise ExportError("Attachment has an invalid response length") from None
        if total is not None and (total < 0 or total > limit):
            raise ExportError("Attachment exceeds configured size limit or has invalid length")
        mime = (
            response.headers.get("Content-Type", "application/octet-stream")
            .split(";", 1)[0]
            .strip()
            .lower()
        )
        return save_chunks(
            response.iter_content(chunk_size=256 * 1024),
            destination,
            total=total,
            limit=limit,
            progress=self.client.progress,
            mime=mime,
        )


def save_chunks(
    chunks: Iterable[bytes],
    destination: Path,
    *,
    total: int | None,
    limit: int,
    progress: ProgressReporter,
    mime: str = "application/octet-stream",
) -> DownloadedFile:
    if total is not None and (total < 0 or total > limit):
        raise ExportError("Attachment exceeds configured size limit or has invalid length")
    if not re.fullmatch(r"[a-z0-9.+_-]+/[a-z0-9.+_-]+", mime):
        mime = "application/octet-stream"
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=destination.parent)
    size = 0
    digest = hashlib.sha256()
    try:
        with (
            os.fdopen(fd, "wb") as stream,
            progress.stage("Attachment bytes", total=total, unit="bytes") as work,
        ):
            for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > limit:
                    raise ExportError("Attachment exceeds configured size limit")
                stream.write(chunk)
                digest.update(chunk)
                work.advance(
                    len(chunk), detail=f"received {human_bytes(size)}; limit {human_bytes(limit)}"
                )
            if total is not None and size != total:
                raise ExportError("Attachment response was truncated or length changed")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return DownloadedFile(size, digest.hexdigest(), mime)

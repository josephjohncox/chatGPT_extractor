"""Read-only ChatGPT web transport. These endpoints are not a supported public API."""

import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import requests

from export_config import Config, normalize_project_id
from progress_report import NO_PROGRESS, ProgressReporter, human_bytes

BASE_URL = "https://chatgpt.com/backend-api"


class ExportError(RuntimeError):
    """An actionable, credential-safe export failure."""


class HTTPError(ExportError):
    def __init__(self, status: int, label: str, *, browser_challenge: bool = False):
        self.status = status
        self.browser_challenge = browser_challenge
        if browser_challenge:
            explanation = "Cloudflare browser challenge detected (cf-mitigated: challenge). This is not a verdict on cookie validity."
            if label == "session authentication":
                action = (
                    " Run scripts/probe-chatgpt.js in your normal signed-in ChatGPT tab and match its "
                    "CHATGPT_USER_AGENT. Its CHATGPT_ACCESS_TOKEN can skip this cookie-exchange request. Retry mise run probe. "
                    "Backend requests may still be challenged."
                )
            else:
                action = (
                    " This backend request is browser-gated even if a bearer token was supplied. "
                    "Match CHATGPT_USER_AGENT to navigator.userAgent using scripts/probe-chatgpt.js, then retry. "
                    "Use that helper in your normal signed-in tab for a browser-side comparison if challenges persist. "
                    "Offline import/index remain available; bearer tokens do not solve browser challenges."
                )
            super().__init__(f"{label}: HTTP {status}. {explanation}{action}")
            return
        hint = (
            " Check browser sign-in, credentials, and workspace ID. HTTP 403 can also be browser/Cloudflare gating; use offline import if needed."
            if status in {401, 403}
            else ""
        )
        super().__init__(f"{label}: HTTP {status}.{hint}")


def retry_delay(value: str | None, attempt: int) -> float:
    try:
        delay = float(value) if value else 4 * 2**attempt
    except ValueError:
        try:
            when = parsedate_to_datetime(value or "")
            delay = (when - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            delay = 4 * 2**attempt
    return min(120.0, max(1.0, delay))


class ChatGPTClient:
    def __init__(
        self,
        config: Config,
        session: requests.Session | None = None,
        *,
        progress: ProgressReporter | None = None,
    ):
        self.config = config
        self.session = session if session is not None else requests.Session()
        self.progress = progress if progress is not None else NO_PROGRESS
        self._last_request: float | None = None
        self.project_files: dict[str, list[dict]] = {}

    def headers(self, token: str = "") -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.config.user_agent,
            "Referer": "https://chatgpt.com/",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self.config.workspace_id:
            headers["ChatGPT-Account-ID"] = self.config.workspace_id
        return headers

    def _wait(self, seconds: float, reason: str) -> None:
        if seconds > 0:
            with self.progress.stage(
                "Waiting", total=1, detail=f"{reason}; {seconds:.1f}s", transient=True
            ) as work:
                time.sleep(seconds)
                work.advance()

    def get(
        self, url: str, *, headers: dict[str, str], params: dict[str, Any] | None = None, label: str
    ) -> requests.Response:
        if not url.startswith("https://chatgpt.com/"):
            raise ExportError("Refusing to send ChatGPT credentials to another origin")
        for attempt in range(self.config.max_retries + 1):
            if self._last_request is not None:
                delay = max(0, self.config.request_delay - (time.monotonic() - self._last_request))
                self._wait(delay, "request pacing")
            try:
                with self.progress.stage(
                    f"HTTP {label.split(' (', 1)[0]}",
                    total=1,
                    transient=True,
                    detail=f"attempt {attempt + 1}/{self.config.max_retries + 1}; timeout {self.config.timeout:.1f}s",
                ) as request:
                    self._last_request = time.monotonic()
                    response = self.session.get(
                        url,
                        headers=headers,
                        params=params,
                        timeout=self.config.timeout,
                        allow_redirects=False,
                    )
                    request.advance(
                        detail=f"HTTP {response.status_code}; {human_bytes(len(response.content))}"
                    )
                    request.finish(state="received")
            except requests.RequestException:
                if attempt == self.config.max_retries:
                    self.progress.notice("Network retries exhausted; request details withheld")
                    raise ExportError(
                        f"{label}: network request failed after retries (details withheld)"
                    ) from None
                delay = retry_delay(None, attempt)
                self.progress.notice(
                    f"Retry: network failure; next attempt {attempt + 2}/{self.config.max_retries + 1}; backoff {delay:.1f}s"
                )
                self._wait(delay, "network retry backoff")
                continue
            if response.headers.get("cf-mitigated", "").strip().lower() == "challenge":
                self.progress.notice("Browser challenge detected; not retrying as rate limiting")
                return response
            if (
                response.status_code not in {429, 500, 502, 503, 504}
                or attempt == self.config.max_retries
            ):
                return response
            delay = retry_delay(response.headers.get("Retry-After"), attempt)
            self.progress.notice(
                f"Retry: HTTP {response.status_code}; next attempt {attempt + 2}/{self.config.max_retries + 1}; backoff {delay:.1f}s"
            )
            self._wait(delay, "HTTP retry backoff")
        raise ExportError(f"{label}: no response")

    def json(
        self, path: str, token: str, *, params: dict[str, Any] | None = None, label: str
    ) -> dict:
        response = self.get(
            f"{BASE_URL}/{path}", headers=self.headers(token), params=params, label=label
        )
        return self.decode(response, label)

    @staticmethod
    def decode(response: requests.Response, label: str) -> dict:
        browser_challenge = response.headers.get("cf-mitigated", "").strip().lower() == "challenge"
        if response.status_code != 200 or browser_challenge:
            raise HTTPError(response.status_code, label, browser_challenge=browser_challenge)
        try:
            payload = response.json()
        except ValueError:
            raise ExportError(
                f"{label}: expected JSON; the server may require browser sign-in"
            ) from None
        if not isinstance(payload, dict):
            raise ExportError(f"{label}: unexpected response shape (expected object)")
        return payload

    def access_token(self) -> str:
        with self.progress.stage("Authentication", total=1) as work:
            if self.config.access_token:
                work.advance(detail="configured bearer selected; backend verification follows")
                return self.config.access_token
            cookie = self.config.cookie()
            if not cookie:
                raise ExportError(
                    "Set CHATGPT_ACCESS_TOKEN or CHATGPT_SESSION_TOKEN[_0/_1] in mise.local.toml, then run through mise"
                )
            headers = self.headers()
            headers["Cookie"] = cookie
            response = self.get(
                "https://chatgpt.com/api/auth/session",
                headers=headers,
                label="session authentication",
            )
            payload = self.decode(response, "session authentication")
            token = payload.get("accessToken")
            if not isinstance(token, str) or not token:
                raise ExportError(
                    "Session has no accessToken; refresh the ChatGPT session cookie or set CHATGPT_ACCESS_TOKEN"
                )
            work.advance(detail="cookie exchange completed; access token received")
            return token

    @staticmethod
    def items(payload: dict, *keys: str) -> list[dict]:
        for key in keys:
            if key in payload:
                value = payload[key]
                if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                    return value
                break
        raise ExportError("List endpoint changed shape; expected a list of objects")

    def conversations(
        self, token: str, project_id: str = "", folder_name: str = "", *, archived: bool = False
    ) -> list[dict]:
        label = (
            "Project conversations"
            if project_id
            else "Archived conversations"
            if archived
            else "Regular conversations"
        )
        with self.progress.stage(label, transient=bool(project_id)) as work:
            output: dict[str, dict] = {}
            cursor = "0"
            seen_cursors = {cursor}
            offset = 0
            page_number = 0
            project_id = normalize_project_id(project_id) if project_id else ""
            while True:
                page_number += 1
                if project_id:
                    path = f"gizmos/{quote(project_id, safe='')}/conversations"
                    params: dict[str, Any] = {"cursor": cursor}
                else:
                    path = "conversations"
                    params = {
                        "offset": offset,
                        "limit": self.config.page_size,
                        "order": "updated",
                        "is_archived": str(archived).lower(),
                    }
                payload = self.json(path, token, params=params, label="conversation listing")
                items = self.items(payload, "items", "conversations")
                new = 0
                for item in items:
                    ident = item.get("id") or item.get("conversation_id")
                    if not isinstance(ident, str) or not ident:
                        raise ExportError("Conversation listing has an item without an ID")
                    new += ident not in output
                    item = dict(item, id=ident)
                    if folder_name:
                        item["_folder_name"] = folder_name
                        item["_project_id"] = project_id
                    output[ident] = item
                reported_total = payload.get("total")
                work.update(
                    completed=len(output) if project_id else offset + len(items),
                    total=reported_total
                    if isinstance(reported_total, int) and reported_total >= 0
                    else None,
                    detail=f"page {page_number}; {len(items)} on page; {len(output)} unique conversations",
                )
                if project_id:
                    if items and not new:
                        raise ExportError("Project pagination repeated a page")
                    if payload.get("has_more") is False:
                        break
                    next_cursor = payload.get("cursor") or payload.get("next_cursor")
                    if not next_cursor:
                        if payload.get("has_more"):
                            raise ExportError("Project pagination has_more without a cursor")
                        break
                    if not isinstance(next_cursor, str) or next_cursor in seen_cursors or not items:
                        raise ExportError("Project pagination did not advance")
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor
                else:
                    offset += len(items)
                    total = payload.get("total")
                    if not items:
                        if payload.get("has_more") or (isinstance(total, int) and offset < total):
                            raise ExportError(
                                "Conversation listing ended before its reported total"
                            )
                        break
                    if not new:
                        raise ExportError("Conversation pagination repeated a page")
                    if payload.get("has_more") is False:
                        if isinstance(total, int) and offset < total:
                            raise ExportError(
                                "Conversation listing ended before its reported total"
                            )
                        break
                    if isinstance(total, int) and offset >= total and not payload.get("has_more"):
                        break
                    # Without a total, keep going to an empty page. A short page is not proof of completion.
            work.finish(f"{page_number} pages; {len(output)} unique conversations")
            return list(output.values())

    def projects(self, token: str) -> dict[str, str]:
        with self.progress.stage("Project discovery") as work:
            projects: dict[str, str] = {}
            cursor = None
            seen = set()
            page_number = 0
            while True:
                page_number += 1
                params = {"owned_only": "false", "conversations_per_gizmo": "0"}
                if cursor:
                    params["cursor"] = cursor
                payload = self.json(
                    "gizmos/snorlax/sidebar",
                    token,
                    params=params,
                    label="project discovery (set CHATGPT_AUTO_PROJECTS=false to opt out)",
                )
                items = self.items(payload, "items", "gizmos")
                for item in items:
                    gizmo = item
                    files: list[dict] = []
                    for _ in range(3):
                        if isinstance(gizmo.get("files"), list):
                            files.extend(
                                record if isinstance(record, dict) else {"id": record}
                                for record in gizmo["files"]
                            )
                        if not isinstance(gizmo.get("gizmo"), dict):
                            break
                        gizmo = gizmo["gizmo"]
                    ident = gizmo.get("id")
                    if not isinstance(ident, str) or not ident:
                        raise ExportError("Project discovery changed shape: item has no gizmo ID")
                    if ident.startswith("g-p-"):
                        display = gizmo.get("display") or {}
                        if not isinstance(display, dict):
                            raise ExportError(
                                "Project discovery changed shape: display is not an object"
                            )
                        name = display.get("name") or gizmo.get("name") or ident
                        projects[normalize_project_id(ident)] = str(name)
                        self.project_files[normalize_project_id(ident)] = files
                work.update(
                    completed=len(projects),
                    detail=f"page {page_number}; {len(projects)} projects discovered",
                )
                next_cursor = payload.get("cursor") or payload.get("next_cursor")
                if not next_cursor:
                    if payload.get("has_more"):
                        raise ExportError("Project discovery has_more without a cursor")
                    break
                if not isinstance(next_cursor, str) or next_cursor in seen or not items:
                    raise ExportError("Project discovery pagination did not advance")
                seen.add(next_cursor)
                cursor = next_cursor
            projects.update(self.config.projects)
            work.update(completed=len(projects))
            work.finish(
                f"{page_number} pages; {len(projects)} projects; {len(self.config.projects)} manual entries"
            )
            return projects

    def all_conversations(self, token: str) -> list[dict]:
        with self.progress.stage("Conversation discovery") as discovery:
            regular = self.conversations(token)
            output = {item["id"]: item for item in regular}
            self.progress.notice(f"Regular listing: {len(output)} unique conversations")
            if self.config.include_archived:
                archived = self.conversations(token, archived=True)
                before = len(output)
                output.update({item["id"]: item for item in archived})
                self.progress.notice(
                    f"Archived listing: {len(archived)} listed; {len(output) - before} additional unique"
                )
            projects = (
                self.projects(token) if self.config.auto_projects else dict(self.config.projects)
            )
            self.progress.notice(
                f"Project scope: {len(projects)} projects; automatic discovery {'enabled' if self.config.auto_projects else 'disabled'}"
            )
            with self.progress.stage("Project conversation listings", total=len(projects)) as work:
                for i, (ident, name) in enumerate(projects.items(), 1):
                    work.update(
                        detail=f"project {i}/{len(projects)}; {len(output)} unique conversations so far"
                    )
                    items = self.conversations(token, ident, name)
                    before = len(output)
                    output.update({item["id"]: item for item in items})
                    work.advance(
                        detail=f"project {i}: {len(items)} listed; {len(output) - before} additional unique; {len(output)} total"
                    )
            discovery.update(completed=len(output))
            discovery.finish(
                f"{len(output)} unique conversations across regular/archived/project listings"
            )
            return list(output.values())

    def detail(self, ident: str, token: str) -> dict:
        return self.json(
            f"conversation/{quote(ident, safe='')}", token, label="conversation detail"
        )

    def probe(self) -> dict:
        """Only request metadata endpoints; return no credentials or conversation content."""
        token = self.access_token()
        report: dict[str, Any] = {
            "authentication": "unverified",
            "token_source": "configured_bearer" if self.config.access_token else "session_exchange",
            "endpoints": {},
        }
        with self.progress.stage("Probe endpoints", total=3) as work:
            for path, params in (
                ("conversations", {"offset": 0, "limit": 1, "order": "updated"}),
                ("gizmos/snorlax/sidebar", {"conversations_per_gizmo": 0}),
                ("settings/user", None),
            ):
                try:
                    data = self.json(path, token, params=params, label=path)
                    report["endpoints"][path] = {
                        "status": 200,
                        "keys": sorted(data),
                        "items_on_page": len(data.get("items", []))
                        if isinstance(data.get("items"), list)
                        else None,
                    }
                except HTTPError as exc:
                    report["endpoints"][path] = {
                        "status": exc.status,
                        "browser_challenge": exc.browser_challenge,
                        "error": str(exc),
                    }
                except ExportError as exc:
                    report["endpoints"][path] = {"error": str(exc)}
                item = report["endpoints"][path]
                work.advance(
                    detail=f"{path}: HTTP {item.get('status', 'unknown')}; {'failed' if 'error' in item else 'ok'}"
                )
        if any(
            item.get("status") == 200 and "error" not in item
            for item in report["endpoints"].values()
        ):
            report["authentication"] = "ok"
        elif not self.config.access_token:
            report["authentication"] = "session_ok_backend_unverified"
        return report

"""Read-only ChatGPT web transport. These endpoints are not a supported public API."""

import time
from typing import Any
from urllib.parse import quote

import requests

from export_config import Config, normalize_project_id
from progress_report import NO_PROGRESS, ProgressReporter, human_bytes
from retry_control import RetryBudget, server_retry_after
from retry_control import retry_delay as retry_delay

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


class RetryStopped(HTTPError):
    """Stop the whole run rather than resetting the cooldown budget for the next item."""

    temporary_stop = True

    def __init__(
        self,
        status: int,
        label: str,
        *,
        request_id: int,
        reason: str,
        retry_after: float | None,
        spent: float,
        max_wait: float = 10,
        max_total_wait: float = 30,
    ):
        super().__init__(status, label)
        self.reason = reason
        self.operation = label
        self.request_id = request_id
        self.retry_after = retry_after
        self.spent = spent
        cause = f"HTTP {status}" if status else "network request failed"
        cooldown = (
            f" Server requests a {retry_after:.1f}s cooldown; wait at least that long before resuming."
            if retry_after is not None
            else " Retry later; no trusted server cooldown was provided."
        )
        self.args = (
            f"{label} [request #{request_id}]: {cause}; retry stopped ({reason}); single-wait limit {max_wait:.1f}s, cumulative retry sleep {spent:.1f}/{max_total_wait:.1f}s. Saved checkpoints are retained; no further items will be requested.{cooldown} Adjust CHATGPT_MAX_RETRY_WAIT / CHATGPT_MAX_TOTAL_RETRY_WAIT only if longer autonomous waits are intentional.",
        )


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
        self.retry_budget = RetryBudget(config.max_retry_wait, config.max_total_retry_wait)
        self._request_serial = 0
        self.retry_stopped: RetryStopped | None = None

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

    def ensure_can_request(self) -> None:
        if self.retry_stopped is not None:
            raise self.retry_stopped.with_traceback(None) from None

    def next_request_id(self) -> int:
        self.ensure_can_request()
        self._request_serial += 1
        return self._request_serial

    def reset_connections(self) -> None:
        # Close pools, not the session: account cookies and configured headers must survive.
        reset = 0
        for adapter in getattr(self.session, "adapters", {}).values():
            try:
                adapter.close()
                reset += 1
            except Exception:
                self.progress.warning(
                    "A failed transport pool could not be cleared; details withheld"
                )
        self.progress.notice(
            f"Network cleanup: cleared {reset} connection pools; session cookies retained"
        )

    def stop_retry(
        self, *, status: int, label: str, request_id: int, reason: str, retry_after: float | None
    ) -> None:
        self.progress.notice(
            f"Retry stopped: {label} [request #{request_id}]; reason={reason}; run retry sleep={self.retry_budget.spent:.1f}/{self.config.max_total_retry_wait:.1f}s"
        )
        self.retry_stopped = RetryStopped(
            status,
            label,
            request_id=request_id,
            reason=reason,
            retry_after=retry_after,
            spent=self.retry_budget.spent,
            max_wait=self.config.max_retry_wait,
            max_total_wait=self.config.max_total_retry_wait,
        )
        raise self.retry_stopped from None

    def wait_for_retry(
        self, *, status: int, label: str, request_id: int, attempt: int, header: str | None
    ) -> None:
        requested = server_retry_after(header)
        if attempt >= self.config.max_retries:
            self.stop_retry(
                status=status,
                label=label,
                request_id=request_id,
                reason="attempts_exhausted",
                retry_after=requested,
            )
        delay = retry_delay(header, attempt)
        rejected = self.retry_budget.rejection(delay)
        if rejected:
            self.stop_retry(
                status=status,
                label=label,
                request_id=request_id,
                reason=rejected,
                retry_after=requested,
            )
        self.retry_budget.reserve(delay)
        source = "server Retry-After" if requested is not None else "local exponential"
        cause = f"HTTP {status}" if status else "network failure"
        self.progress.notice(
            f"Retry: {label} [request #{request_id}]; {cause}; next attempt {attempt + 2}/{self.config.max_retries + 1}; backoff {delay:.1f}s ({source}); run retry sleep={self.retry_budget.spent:.1f}/{self.config.max_total_retry_wait:.1f}s"
        )
        self._wait(delay, f"{label}; bounded retry backoff")

    def get(
        self, url: str, *, headers: dict[str, str], params: dict[str, Any] | None = None, label: str
    ) -> requests.Response:
        if not url.startswith("https://chatgpt.com/"):
            raise ExportError("Refusing to send ChatGPT credentials to another origin")
        request_id = self.next_request_id()
        for attempt in range(self.config.max_retries + 1):
            if self._last_request is not None:
                self._wait(
                    max(0, self.config.request_delay - (time.monotonic() - self._last_request)),
                    "request pacing",
                )
            try:
                with self.progress.stage(
                    f"HTTP {label.split(' (', 1)[0]}",
                    total=1,
                    transient=True,
                    detail=f"request #{request_id}; attempt {attempt + 1}/{self.config.max_retries + 1}; connect/read timeout {self.config.timeout:.1f}s",
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
                        detail=f"request #{request_id}; HTTP {response.status_code}; {human_bytes(len(response.content))}"
                    )
                    request.finish(state="received")
            except requests.RequestException as exc:
                if exc.response is not None:
                    exc.response.close()
                self.reset_connections()
                self.wait_for_retry(
                    status=0, label=label, request_id=request_id, attempt=attempt, header=None
                )
                continue
            if response.headers.get("cf-mitigated", "").strip().lower() == "challenge":
                self.progress.notice("Browser challenge detected; not retrying as rate limiting")
                return response
            if response.status_code in {429, 500, 502, 503, 504}:
                status = response.status_code
                header = response.headers.get("Retry-After")
                if status == 429 or attempt < self.config.max_retries:
                    response.close()
                    self.wait_for_retry(
                        status=status,
                        label=label,
                        request_id=request_id,
                        attempt=attempt,
                        header=header,
                    )
                    continue
            if attempt and response.status_code == 200:
                self.progress.notice(
                    f"Recovered HTTP: {label} [request #{request_id}]; attempt {attempt + 1}/{self.config.max_retries + 1}; HTTP {response.status_code}"
                )
            return response
        raise ExportError(f"{label}: no response")

    def json(
        self, path: str, token: str, *, params: dict[str, Any] | None = None, label: str
    ) -> dict:
        response = self.get(
            f"{BASE_URL}/{path}", headers=self.headers(token), params=params, label=label
        )
        try:
            return self.decode(response, label)
        finally:
            response.close()

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
            try:
                payload = self.decode(response, "session authentication")
            finally:
                response.close()
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
                except RetryStopped:
                    raise
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

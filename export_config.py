"""Environment-only configuration; mise owns loading the ignored local file."""

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.8059.85 Safari/537.36"
)


class ConfigError(ValueError):
    """Invalid configuration without including credential values in diagnostics."""


def normalize_project_id(value: str) -> str:
    match = re.search(r"(?:^|/)(g-p-[a-f0-9]+)(?:-|/|$)", value)
    if not match:
        raise ConfigError("Project IDs must be g-p-<hex> IDs or ChatGPT project URLs")
    return match.group(1)


def number(env: Mapping[str, str], name: str, default: float, minimum: float) -> float:
    try:
        value = float(env.get(name, str(default)))
        if not math.isfinite(value) or value < minimum:
            raise ValueError
        return value
    except ValueError:
        raise ConfigError(f"{name} must be a finite number >= {minimum}") from None


def boolean(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(name, str(default)).strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise ConfigError(f"{name} must be true or false")
    return value in {"true", "1"}


def output_directory(value: str) -> Path:
    """Relative paths are repository-relative under mise; support explicit home paths."""
    if not value.strip() or "\x00" in value:
        raise ConfigError("CHATGPT_OUTPUT_DIR must be a nonempty directory path")
    return Path(value).expanduser()


@dataclass(frozen=True)
class Config:
    session_token: str = field(default="", repr=False)
    session_token_0: str = field(default="", repr=False)
    session_token_1: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    workspace_id: str = ""
    user_agent: str = DEFAULT_USER_AGENT
    projects: tuple[tuple[str, str], ...] = ()
    output_dir: Path = Path("chatgpt_export")
    output_format: str = "json"
    page_size: int = 100
    request_delay: float = 1.0
    timeout: float = 30.0
    max_retries: int = 5
    auto_projects: bool = True
    include_archived: bool = False
    build_index: bool = True
    progress: bool = True
    auxiliary: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        output_format = env.get("CHATGPT_EXPORT_FORMAT", "json").strip().lower()
        if output_format not in {"json", "markdown"}:
            raise ConfigError("CHATGPT_EXPORT_FORMAT must be json or markdown")
        try:
            raw = json.loads(env.get("CHATGPT_PROJECTS", "[]"))
            if not isinstance(raw, list):
                raise ValueError
            projects = []
            for entry in raw:
                if (
                    not isinstance(entry, list)
                    or len(entry) != 2
                    or not all(isinstance(v, str) and v.strip() for v in entry)
                ):
                    raise ValueError
                projects.append((normalize_project_id(entry[0]), entry[1]))
        except (ValueError, TypeError):
            raise ConfigError(
                "CHATGPT_PROJECTS must be a JSON array of [project_id, folder_name] pairs"
            ) from None
        page_size = number(env, "CHATGPT_PAGE_SIZE", 100, 1)
        retries = number(env, "CHATGPT_MAX_RETRIES", 5, 0)
        if (
            not page_size.is_integer()
            or page_size > 100
            or not retries.is_integer()
            or retries > 10
        ):
            raise ConfigError(
                "CHATGPT_PAGE_SIZE must be 1..100 and CHATGPT_MAX_RETRIES 0..10 (integers)"
            )
        secrets = {}
        for name in ("SESSION_TOKEN", "SESSION_TOKEN_0", "SESSION_TOKEN_1", "ACCESS_TOKEN"):
            value = env.get(f"CHATGPT_{name}", "").strip()
            if any(c in value for c in "\r\n;") or value.startswith("PASTE_"):
                raise ConfigError(f"CHATGPT_{name} contains a placeholder or invalid characters")
            secrets[name.lower()] = value
        if secrets["session_token_1"] and not secrets["session_token_0"]:
            raise ConfigError("CHATGPT_SESSION_TOKEN_1 requires CHATGPT_SESSION_TOKEN_0")
        workspace = env.get("CHATGPT_WORKSPACE_ID", "").strip()
        if workspace and not re.fullmatch(r"[a-zA-Z0-9_-]+", workspace):
            raise ConfigError("CHATGPT_WORKSPACE_ID must be an account ID, not a URL or header")
        user_agent = env.get("CHATGPT_USER_AGENT", DEFAULT_USER_AGENT).strip()
        if not user_agent or any(ord(char) < 32 or ord(char) == 127 for char in user_agent):
            raise ConfigError(
                "CHATGPT_USER_AGENT must be a nonempty HTTP header value without control characters"
            )
        try:
            user_agent.encode("latin-1")
        except UnicodeEncodeError:
            raise ConfigError(
                "CHATGPT_USER_AGENT must be an HTTP header value; copy navigator.userAgent from your browser"
            ) from None
        return cls(
            **secrets,
            workspace_id=workspace,
            user_agent=user_agent,
            projects=tuple(projects),
            output_dir=output_directory(env.get("CHATGPT_OUTPUT_DIR", "chatgpt_export")),
            output_format=output_format,
            page_size=int(page_size),
            max_retries=int(retries),
            request_delay=number(env, "CHATGPT_REQUEST_DELAY", 1, 0),
            timeout=number(env, "CHATGPT_TIMEOUT", 30, 0.1),
            auto_projects=boolean(env, "CHATGPT_AUTO_PROJECTS", True),
            include_archived=boolean(env, "CHATGPT_INCLUDE_ARCHIVED", False),
            build_index=boolean(env, "CHATGPT_BUILD_INDEX", True),
            progress=boolean(env, "CHATGPT_PROGRESS", True),
            auxiliary=boolean(env, "CHATGPT_AUXILIARY", True),
        )

    def cookie(self) -> str:
        if self.session_token:
            return f"__Secure-next-auth.session-token={self.session_token}"
        if self.session_token_0 and self.session_token_1:
            return (
                f"__Secure-next-auth.session-token.0={self.session_token_0}; "
                f"__Secure-next-auth.session-token.1={self.session_token_1}"
            )
        if self.session_token_0:
            return f"__Secure-next-auth.session-token={self.session_token_0}"
        return ""

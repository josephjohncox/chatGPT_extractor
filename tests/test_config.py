import json
import tomllib
from dataclasses import fields, replace
from pathlib import Path

import pytest

from export_config import Config, ConfigError, normalize_project_id


@pytest.mark.parametrize(
    ("env", "cookie"),
    [
        ({"CHATGPT_SESSION_TOKEN": "single"}, "__Secure-next-auth.session-token=single"),
        ({"CHATGPT_SESSION_TOKEN_0": "single"}, "__Secure-next-auth.session-token=single"),
        (
            {"CHATGPT_SESSION_TOKEN_0": "part0", "CHATGPT_SESSION_TOKEN_1": "part1"},
            "__Secure-next-auth.session-token.0=part0; __Secure-next-auth.session-token.1=part1",
        ),
        ({}, ""),
    ],
)
def test_cookie_shapes(env, cookie):
    assert Config.from_env(env).cookie() == cookie


@pytest.mark.parametrize(
    "env",
    [
        {"CHATGPT_SESSION_TOKEN_1": "orphan"},
        {"CHATGPT_SESSION_TOKEN": "PASTE_TOKEN_HERE"},
        {"CHATGPT_ACCESS_TOKEN": "fake\r\ninjected"},
        {"CHATGPT_SESSION_TOKEN": "fake;another=cookie"},
        {"CHATGPT_PAGE_SIZE": "101"},
        {"CHATGPT_PAGE_SIZE": "1.5"},
        {"CHATGPT_MAX_RETRIES": "11"},
        {"CHATGPT_MAX_RETRIES": "-1"},
        {"CHATGPT_MAX_RETRY_WAIT": "-1"},
        {"CHATGPT_MAX_RETRY_WAIT": "nan"},
        {"CHATGPT_MAX_TOTAL_RETRY_WAIT": "-1"},
        {"CHATGPT_MAX_TOTAL_RETRY_WAIT": "inf"},
        {"CHATGPT_TIMEOUT": "0"},
        {"CHATGPT_TIMEOUT": "inf"},
        {"CHATGPT_REQUEST_DELAY": "nan"},
        {"CHATGPT_REQUEST_DELAY": "-1"},
        {"CHATGPT_AUTO_PROJECTS": "maybe"},
        {"CHATGPT_PROGRESS": "maybe"},
        {"CHATGPT_AUXILIARY": "maybe"},
        {"CHATGPT_ATTACHMENTS": "maybe"},
        {"CHATGPT_MAX_ATTACHMENT_BYTES": "0"},
        {"CHATGPT_MAX_ATTACHMENT_BYTES": "1.5"},
        {"CHATGPT_MAX_ATTACHMENT_BYTES": "nan"},
        {"CHATGPT_MAX_ATTACHMENT_BYTES": "2147483649"},
        {"CHATGPT_INCLUDE_ARCHIVED": "yes"},
        {"CHATGPT_EXPORT_FORMAT": "xml"},
        {"CHATGPT_WORKSPACE_ID": "bad\r\nHeader"},
        {"CHATGPT_USER_AGENT": ""},
        {"CHATGPT_USER_AGENT": "fake\r\nHeader"},
        {"CHATGPT_USER_AGENT": "fake\x00header"},
        {"CHATGPT_USER_AGENT": "Snowman \u2603"},
        {"CHATGPT_PROJECTS": "invalid"},
        {"CHATGPT_PROJECTS": "{}"},
        {"CHATGPT_PROJECTS": "[1]"},
        {"CHATGPT_PROJECTS": '[["g-p-abc", ""]]'},
        {"CHATGPT_PROJECTS": '[["workspace-id", "Work"]]'},
    ],
)
def test_invalid_config_is_rejected_without_values(env):
    with pytest.raises(ConfigError):
        Config.from_env(env)


def test_project_urls_and_explicit_settings():
    config = Config.from_env(
        {
            "CHATGPT_PROJECTS": json.dumps(
                [["https://chatgpt.com/g/g-p-abc123-research/project", "Research"]]
            ),
            "CHATGPT_AUTO_PROJECTS": "false",
            "CHATGPT_INCLUDE_ARCHIVED": "true",
            "CHATGPT_PAGE_SIZE": "20",
        }
    )
    assert config.projects == (("g-p-abc123", "Research"),)
    assert not config.auto_projects and config.include_archived
    assert config.page_size == 20
    assert normalize_project_id("g-p-abc123") == "g-p-abc123"


def test_credentials_not_in_config_repr():
    config = replace(
        Config(), access_token="fake-access-secret", session_token="fake-cookie-secret"
    )
    assert "fake-access-secret" not in repr(config)
    assert "fake-cookie-secret" not in repr(config)


def test_example_documents_every_config_field_with_matching_defaults():
    env = tomllib.loads((Path(__file__).parents[1] / "mise.local.toml.example").read_text())["env"]
    expected = {
        "CHATGPT_EXPORT_FORMAT" if item.name == "output_format" else f"CHATGPT_{item.name.upper()}"
        for item in fields(Config)
    }
    assert set(env) == expected
    assert Config.from_env(env) == Config()

"""Keep the token how-to linked and its configuration examples executable."""

import re
import tomllib
from pathlib import Path

import pytest

from export_config import Config

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "path", ["README.md", "mise.local.toml.example", "scripts/probe-chatgpt.js"]
)
def test_authentication_guide_is_linked_from_setup_and_generated_config(path):
    assert "docs/authentication.md" in (ROOT / path).read_text()
    assert (ROOT / "docs/authentication.md").is_file()


def test_how_to_snippets_select_one_authentication_method_and_clear_the_others():
    guide = (ROOT / "docs/authentication.md").read_text()
    examples = re.findall(r"```toml\n(.*?)\n```", guide, re.DOTALL)
    assert len(examples) == 3
    configs = []
    for example in examples:
        env = tomllib.loads(example)
        assert set(env) == {
            "CHATGPT_ACCESS_TOKEN",
            "CHATGPT_SESSION_TOKEN",
            "CHATGPT_SESSION_TOKEN_0",
            "CHATGPT_SESSION_TOKEN_1",
        }
        configs.append(Config.from_env(env))
    single, split, bearer = configs
    assert single.cookie() == "__Secure-next-auth.session-token=single-cookie-value"
    assert single.access_token == "" and single.session_token_0 == single.session_token_1 == ""
    assert split.cookie() == (
        "__Secure-next-auth.session-token.0=value-from-cookie-ending-.0; "
        "__Secure-next-auth.session-token.1=value-from-cookie-ending-.1"
    )
    assert split.access_token == split.session_token == ""
    assert bearer.access_token == "accessToken-field-value"
    assert bearer.cookie() == ""

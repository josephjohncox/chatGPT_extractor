"""Run the console script in a sandboxed Node VM, then parse its TOML with Python.

Node is optional for contributors; GitHub's hosted runner provides it. No browser,
network, real credential, or additional Python dependency is needed.
"""

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from export_config import Config

PROBE = Path(__file__).resolve().parents[1] / "scripts" / "probe-chatgpt.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Browser-script sandbox tests need Node.js")
HARNESS = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const options = JSON.parse(process.argv[1]);
const source = fs.readFileSync(0, 'utf8');
const output = { downloads: [], requests: [], confirmations: 0, removed: 0, revoked: 0 };
const context = {
  location: { origin: 'https://chatgpt.com' },
  navigator: { userAgent: 'Mozilla/5.0 FixtureChrome/123' },
  prompt: message => message.startsWith('Export output directory') ? options.outputDir : options.workspace,
  confirm: () => { output.confirmations++; return options.download; },
  fetch: async (path, config) => {
    output.requests.push(path);
    const session = path === '/api/auth/session';
    const status = session ? options.status : 200;
    return {
      ok: status === 200,
      status,
      headers: { get: name => name.toLowerCase() === 'cf-mitigated' && options.challenge ? 'challenge' : null },
      json: async () => session ? { accessToken: options.token } : { items: [] },
    };
  },
  Blob: class { constructor(parts) { this.text = parts.join(''); } },
  URL: {
    createObjectURL: blob => { output.downloads.push(blob.text); return 'blob:test'; },
    revokeObjectURL: () => { output.revoked++; },
  },
  document: {
    body: { appendChild() {} },
    createElement: () => ({
      click() { if (options.failClick) throw new Error('Simulated browser download failure'); },
      remove() { output.removed++; },
    }),
  },
  setTimeout: fn => fn(),
};
(async () => {
  try { output.report = await vm.runInNewContext(source, context, { timeout: 1000 }); }
  catch (error) { output.error = error.message; }
  process.stdout.write(JSON.stringify(output));
})();
"""


def probe(**overrides):
    assert NODE is not None
    options = {
        "workspace": "workspace-test",
        "download": True,
        "outputDir": "chatgpt_export",
        "status": 200,
        "token": 'fake-"token\\path\x7f',
        **overrides,
    }
    result = subprocess.run(
        [NODE, "-e", HARNESS, json.dumps(options)],
        input=PROBE.read_text(),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    return json.loads(result.stdout), options


@pytest.mark.parametrize("workspace", ["", "workspace-test"])
def test_download_is_valid_mise_toml_and_report_stays_redacted(workspace):
    result, options = probe(workspace=workspace)
    assert result["report"]["credential_file"] == "download_requested"
    assert len(result["downloads"]) == 1
    env = tomllib.loads(result["downloads"][0])["env"]
    assert env == {
        "CHATGPT_ACCESS_TOKEN": options["token"],
        "CHATGPT_WORKSPACE_ID": workspace,
        "CHATGPT_OUTPUT_DIR": options["outputDir"],
        "CHATGPT_BUILD_INDEX": "true",
        "CHATGPT_AUXILIARY": "true",
        "CHATGPT_ATTACHMENTS": "true",
        "CHATGPT_USER_AGENT": "Mozilla/5.0 FixtureChrome/123",
    }
    config = Config.from_env(env)
    assert config.access_token == options["token"]
    assert config.workspace_id == workspace
    assert config.output_dir == Path(options["outputDir"])
    assert config.build_index
    assert config.auxiliary
    assert config.attachments
    assert config.user_agent == "Mozilla/5.0 FixtureChrome/123"
    assert options["token"] not in json.dumps(result["report"])
    assert result["removed"] == result["revoked"] == 1
    assert len(result["requests"]) == 4


def test_download_documents_output_index_projects_and_cookie_alternatives():
    result, _ = probe()
    text = result["downloads"][0]
    for expected in (
        "mise run index -- /path/to/previous/export",
        "index.html",
        ".chatgpt-index/chats/",
        "CHATGPT_AUTO_PROJECTS",
        "CHATGPT_PROGRESS",
        "CHATGPT_AUXILIARY",
        "--no-auxiliary",
        "CHATGPT_ATTACHMENTS",
        "CHATGPT_MAX_ATTACHMENT_BYTES",
        "mise run attachments",
        "--no-progress",
        "CHATGPT_MAX_RETRY_WAIT",
        "CHATGPT_MAX_TOTAL_RETRY_WAIT",
        "CHATGPT_PROJECTS",
        "CHATGPT_SESSION_TOKEN_0",
        "CHATGPT_SESSION_TOKEN_1",
        "HttpOnly",
        "no automatic fallback",
        "docs/authentication.md",
        "Application > Cookies > https://chatgpt.com",
        "__Secure-next-auth.session-token.0 / .1",
        "copy the complete Value",
    ):
        assert expected in text
    example = tomllib.loads((PROBE.parents[1] / "mise.local.toml.example").read_text())["env"]
    generated = tomllib.loads(text)["env"]
    assert set(generated) <= set(example)


def test_declining_download_still_returns_redacted_probe():
    result, options = probe(download=False)
    assert result["report"]["credential_file"] == "declined"
    assert result["downloads"] == []
    assert options["token"] not in json.dumps(result)


@pytest.mark.parametrize("options", [{"status": 403}, {"token": ""}, {"token": None}])
def test_no_credential_download_without_successful_session(options):
    result, _ = probe(**options)
    assert result["downloads"] == []
    assert result["confirmations"] == 0
    assert result["requests"] == ["/api/auth/session"]


def test_browser_probe_reports_challenge_without_tokens_or_download():
    result, options = probe(status=403, challenge=True)
    assert result["report"]["authentication"] == {
        "status": 403,
        "browser_challenge": True,
        "has_access_token": False,
    }
    assert result["downloads"] == []
    assert options["token"] not in json.dumps(result["report"])


def test_cancelling_workspace_prompt_never_downloads():
    result, _ = probe(workspace=None)
    assert result["report"] == {"cancelled": True}
    assert result["downloads"] == []
    assert result["confirmations"] == 0


def test_invalid_workspace_does_not_download_or_probe_endpoints():
    result, _ = probe(workspace="bad\r\nHeader")
    assert "Expected an account ID" in result["error"]
    assert result["downloads"] == []
    assert result["requests"] == ["/api/auth/session"]


@pytest.mark.parametrize("directory", ["", " ", "bad\x00path"])
def test_invalid_output_directory_never_downloads(directory):
    result, _ = probe(outputDir=directory)
    assert result["report"]["credential_file"] == "invalid_output_directory"
    assert result["downloads"] == []


def test_cancelling_directory_prompt_never_downloads():
    result, _ = probe(outputDir=None)
    assert result["report"]["credential_file"] == "cancelled"
    assert result["downloads"] == []


def test_directory_with_spaces_and_quotes_is_valid_toml():
    directory = '/tmp/ChatGPT "private" archives'
    result, _ = probe(outputDir=directory)
    assert tomllib.loads(result["downloads"][0])["env"]["CHATGPT_OUTPUT_DIR"] == directory


def test_failed_download_is_redacted_and_blob_is_released():
    result, options = probe(failClick=True)
    assert result["report"]["credential_file"] == "download_failed"
    assert options["token"] not in json.dumps(result["report"])
    assert result["removed"] == result["revoked"] == 1

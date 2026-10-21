import hashlib
import json
import stat
from html.parser import HTMLParser
from urllib.parse import unquote

import pytest

from chatgpt_client import ChatGPTClient, ExportError
from chatgpt_exporter import main
from export_config import Config, ConfigError
from local_index import MARKER, SITE, build_index, local_link, scan_directory


def detail(ident="test-chat", title="Test conversation", updated=1):
    return {
        "id": ident,
        "title": title,
        "create_time": 0,
        "update_time": updated,
        "current_node": "selected",
        "mapping": {
            "question": {
                "parent": None,
                "children": ["old", "selected"],
                "message": {"author": {"role": "user"}, "content": {"parts": ["Question"]}},
            },
            "old": {
                "parent": "question",
                "children": [],
                "message": {"author": {"role": "assistant"}, "content": {"parts": ["Old answer"]}},
            },
            "selected": {
                "parent": "question",
                "children": [],
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"parts": ["Selected answer"]},
                },
            },
        },
    }


def save(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


class Links(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.links = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs)["href"])


def assert_links_exist(page):
    for href in Links(page.read_text()).links:
        assert href.startswith(("./", "../"))
        assert (page.parent / unquote(href)).is_file(), href


def test_builds_existing_json_tree_without_network_or_changing_sources(tmp_path):
    source = save(tmp_path, "Old project/Original title.json", detail())
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    result = build_index(tmp_path)
    assert result.path == tmp_path / "index.html"
    assert (result.chats, result.projects, result.warnings) == (1, 1, [])
    assert "Test conversation" in result.path.read_text()
    page = next((tmp_path / SITE / "chats").glob("*.html"))
    content = page.read_text()
    assert "Selected answer" in content and "Old answer" not in content
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    assert_links_exist(result.path)
    assert_links_exist(page)
    assert stat.S_IMODE(page.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / SITE).stat().st_mode) == 0o700


def test_legacy_markdown_only_header_dates_and_fallback_filename(tmp_path):
    folder = tmp_path / "Legacy"
    folder.mkdir()
    (folder / "first.md").write_text(
        "# Original Markdown title\n\n**Created:** 2024-01-01 10:00\n**Last updated:** 2024-02-01 10:00\n\nHello **world**"
    )
    (tmp_path / "no header.markdown").write_text("Plain Markdown body")
    chats, ignored, warnings = scan_directory(tmp_path.resolve())
    assert {chat.title for chat in chats} == {"Original Markdown title", "no header"}
    assert chats[0].updated is not None
    result = build_index(tmp_path)
    assert (result.chats, result.projects, ignored, warnings) == (2, 2, 0, [])
    pages = "\n".join(path.read_text() for path in (tmp_path / SITE / "chats").glob("*.html"))
    assert "Hello **world**" in pages and "Plain Markdown body" in pages


def test_json_markdown_pair_and_renamed_same_id_are_one_chat(tmp_path):
    save(tmp_path, "Work/old.json", detail(title="Old title", updated=1))
    save(tmp_path, "Work/new.json", detail(title="New title", updated=2))
    (tmp_path / "Work/new.md").write_text("# New title\n\nText")
    chats, _, _ = scan_directory(tmp_path.resolve())
    assert len(chats) == 1
    assert chats[0].title == "New title"
    assert len(chats[0].raw) == 2 and len(chats[0].markdown) == 1
    assert build_index(tmp_path).chats == 1


@pytest.mark.parametrize("wrapper", [False, True])
def test_bulk_official_conversations_json_without_extraction(tmp_path, wrapper):
    data = [detail("a", "First chat"), detail("b", "Second chat")]
    save(tmp_path, "conversations.json", {"conversations": data} if wrapper else data)
    result = build_index(tmp_path)
    assert result.chats == 2 and not result.warnings
    for page in (tmp_path / SITE / "chats").glob("*.html"):
        assert "Selected answer" in page.read_text()
        assert_links_exist(page)


def test_auxiliary_hidden_nonchat_json_and_generated_html_are_not_chats(tmp_path):
    for folder in ("settings", "memories", ".private", "node_modules"):
        save(tmp_path, f"{folder}/not-a-chat.json", detail(folder))
    save(tmp_path, "auxiliary-status.json", {"available": True})
    save(tmp_path, ".export-index.json", {"not": "a conversation"})
    save(tmp_path, "_Uncategorized/chat.json", detail())
    result = build_index(tmp_path)
    assert result.chats == 1 and result.ignored == 1 and result.projects == 1
    assert build_index(tmp_path).chats == 1


def test_same_title_distinct_ids_and_same_named_project_hashes_stay_distinct(tmp_path):
    save(tmp_path, "Research--aaaaaaaaaaaa/chat.json", detail("a"))
    save(tmp_path, "Research--bbbbbbbbbbbb/chat.json", detail("b"))
    result = build_index(tmp_path)
    assert result.chats == 2 and result.projects == 2
    assert len(list((tmp_path / SITE / "chats").glob("*.html"))) == 2


def test_title_message_folder_and_path_html_are_escaped(tmp_path):
    payload = '<img src="https://evil.invalid/image" onerror="alert(1)"> & {{ROWS}}'
    data = detail(title=payload)
    data["mapping"]["selected"]["message"]["content"]["parts"] = [
        '</pre><script>alert("secret")</script>'
    ]
    save(tmp_path, 'Project & "quotes"/chat # ü.json', data)
    result = build_index(tmp_path)
    content = result.path.read_text()
    page = next((tmp_path / SITE / "chats").glob("*.html"))
    assert "<img src=" not in content
    assert "&lt;img src=" in content and "{{ROWS}}" in content
    assert '<script>alert("secret")</script>' not in page.read_text()
    assert "&lt;script&gt;" in page.read_text()
    assert "Content-Security-Policy" in content
    assert_links_exist(result.path)
    assert_links_exist(page)


def test_invalid_json_and_broken_mapping_keep_partial_index_with_warnings(tmp_path):
    (tmp_path / "invalid.json").write_text("not json")
    data = detail()
    data["current_node"] = "missing"
    save(tmp_path, "bad-mapping.json", data)
    result = build_index(tmp_path)
    assert result.chats == 1 and len(result.warnings) == 2
    assert "indexing warnings" in result.path.read_text()
    page = next((tmp_path / SITE / "chats").glob("*.html"))
    assert "Unable to render" in page.read_text()
    assert_links_exist(page)


def test_source_symlinks_do_not_read_outside_root(tmp_path):
    outside = tmp_path / "outside"
    save(outside, "secret.json", detail())
    root = tmp_path / "root"
    root.mkdir()
    (root / "outside-directory").symlink_to(outside, target_is_directory=True)
    (root / "outside-file.json").symlink_to(outside / "secret.json")
    result = build_index(root)
    assert result.chats == 0 and len(result.warnings) == 1
    assert "Selected answer" not in result.path.read_text()


@pytest.mark.parametrize("target", [SITE, f"{SITE}/chats"])
def test_generated_directory_symlinks_are_rejected(tmp_path, target):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    path = root / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ExportError, match="symlink"):
        build_index(root)
    assert not list(outside.iterdir())


def test_existing_unowned_index_or_site_is_not_overwritten(tmp_path):
    index = tmp_path / "index.html"
    index.write_text("User's own page")
    with pytest.raises(ExportError, match="already exists"):
        build_index(tmp_path)
    assert index.read_text() == "User's own page"
    index.unlink()
    (tmp_path / SITE).mkdir()
    with pytest.raises(ExportError, match="not owned"):
        build_index(tmp_path)


def test_missing_directory_not_created_and_empty_directory_works(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(ExportError, match="existing export directory"):
        build_index(missing)
    assert not missing.exists()
    assert build_index(tmp_path).chats == 0


def test_large_files_are_reported_and_not_read(tmp_path, monkeypatch):
    save(tmp_path, "large.json", detail())
    monkeypatch.setattr("local_index.MAX_FILE_BYTES", 1)
    result = build_index(tmp_path)
    assert result.chats == 0 and len(result.warnings) == 1


def test_index_cli_uses_config_directory_and_ignores_bad_api_credentials(tmp_path, monkeypatch):
    save(tmp_path, "legacy.json", detail())
    monkeypatch.setenv("CHATGPT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("CHATGPT_ACCESS_TOKEN", "PASTE_INVALID_TOKEN")
    monkeypatch.setenv("CHATGPT_PROJECTS", "invalid API configuration")
    assert main(["index"]) == 0
    assert (tmp_path / "index.html").read_text().startswith(MARKER)


def test_index_cli_explicit_directory_overrides_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATGPT_OUTPUT_DIR", "unrelated")
    for args in (["index", str(tmp_path)], ["index", "--output", str(tmp_path)]):
        assert main(args) == 0
    with pytest.raises(SystemExit):
        main(["index", str(tmp_path), "--output", "other"])


def test_index_cli_warning_is_nonzero_and_index_remains_usable(tmp_path):
    (tmp_path / "bad.json").write_text("invalid")
    assert main(["index", str(tmp_path)]) == 1
    assert (tmp_path / "index.html").is_file()


def test_import_builds_html_and_explicit_opt_out(tmp_path, monkeypatch):
    source = save(tmp_path, "source.json", [detail()])
    output = tmp_path / "output"
    assert main(["import", str(source), "--output", str(output)]) == 0
    assert (output / "index.html").is_file()
    for option in ("flag", "env"):
        other = tmp_path / option
        args = ["import", str(source), "--output", str(other)]
        if option == "flag":
            args.append("--no-index")
        else:
            monkeypatch.setenv("CHATGPT_BUILD_INDEX", "false")
        assert main(args) == 0
        assert not (other / "index.html").exists()


def test_output_directory_expands_home_and_build_index_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = Config.from_env(
        {"CHATGPT_OUTPUT_DIR": "~/ChatGPT/private", "CHATGPT_BUILD_INDEX": "false"}
    )
    assert config.output_dir == tmp_path / "ChatGPT/private"
    assert not config.build_index
    for value in ("", " ", "bad\x00path"):
        with pytest.raises(ConfigError, match="nonempty directory"):
            Config.from_env({"CHATGPT_OUTPUT_DIR": value})


def test_export_builds_index_in_overridden_output_directory(tmp_path, monkeypatch):
    class Client(ChatGPTClient):
        def access_token(self):
            return "fake-access"

        def all_conversations(self, token):
            return [detail()]

        def detail(self, ident, token):
            return detail(ident)

    monkeypatch.setattr("chatgpt_exporter.ChatGPTClient", Client)
    monkeypatch.setenv("CHATGPT_OUTPUT_DIR", str(tmp_path / "default"))
    chosen = tmp_path / "chosen"
    assert main(["export", "--no-auxiliary", "--output", str(chosen)]) == 0
    assert (chosen / "index.html").is_file()
    assert not (tmp_path / "default").exists()


def test_rebuild_rereads_changed_source_without_stale_cache(tmp_path):
    save(tmp_path, "chat.json", detail(title="Old title"))
    build_index(tmp_path)
    save(tmp_path, "chat.json", detail(title="New title", updated=2))
    result = build_index(tmp_path)
    assert "New title" in result.path.read_text()
    assert "Old title" not in result.path.read_text()


def test_generated_page_symlink_does_not_overwrite_target(tmp_path):
    root = tmp_path / "root"
    save(root, "chat.json", detail())
    build_index(root)
    page = next((root / SITE / "chats").glob("*.html"))
    page.unlink()
    target = tmp_path / "unrelated.html"
    target.write_text("Private unrelated page")
    page.symlink_to(target)
    with pytest.raises(ExportError, match="escapes"):
        build_index(root)
    assert target.read_text() == "Private unrelated page"


def test_local_link_is_relative_and_encodes_special_characters(tmp_path):
    assert (
        local_link(tmp_path / "javascript:chat # ü.json", tmp_path)
        == "./javascript%3Achat%20%23%20%C3%BC.json"
    )

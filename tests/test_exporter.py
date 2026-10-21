import json
import stat
import zipfile
from dataclasses import replace

import pytest

from chatgpt_client import ChatGPTClient, ExportError, HTTPError
from chatgpt_exporter import (
    export_auxiliary_data,
    export_conversations,
    extract_messages,
    load_archive,
    main,
    parse_timestamp,
    project_folder,
    safe_output_path,
    sanitize_filename,
    write_json_file,
)
from export_config import Config


def node(parent, children, text, *, role="assistant", hidden=False):
    return {
        "parent": parent,
        "children": children,
        "message": {
            "author": {"role": role},
            "content": {"content_type": "text", "parts": [text]},
            "metadata": {"is_visually_hidden_from_conversation": hidden},
            "create_time": 0,
        },
    }


def conversation(ident="conversation-a", title="Same title", updated: int | None = 1):
    return {
        "id": ident,
        "title": title,
        "update_time": updated,
        "create_time": 0,
        "current_node": "selected",
        "mapping": {
            "root": node(None, ["old", "selected"], "Question", role="user"),
            "old": node("root", [], "Old answer"),
            "selected": node("root", [], "Selected answer"),
        },
    }


def test_selected_branch_not_first_child_and_timestamp_epoch():
    messages = extract_messages(conversation())
    assert [m["content"] for m in messages] == ["Question", "Selected answer"]
    assert messages[0]["timestamp"] == "1970-01-01 00:00 UTC"


def test_old_archive_fallback_and_hidden_nodes():
    detail = conversation()
    detail.pop("current_node")
    detail["mapping"]["root"]["message"]["metadata"]["is_visually_hidden_from_conversation"] = True
    assert [m["content"] for m in extract_messages(detail)] == ["Old answer"]


@pytest.mark.parametrize("current_node", ["missing", "selected"])
def test_broken_branch_fails_instead_of_hanging(current_node):
    detail = conversation()
    detail["current_node"] = current_node
    detail["mapping"]["root"]["parent"] = "selected"
    with pytest.raises(ExportError):
        extract_messages(detail)


def test_multimodal_markdown_notes_nontext_and_raw_json_retains_all():
    detail = conversation()
    detail["mapping"]["selected"]["message"]["content"]["parts"].append(
        {"content_type": "image_asset_pointer", "asset_pointer": "sediment://fake-image"}
    )
    assert "Non-text content" in extract_messages(detail)[1]["content"]
    assert "fake-image" in json.dumps(detail)


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "1970-01-01 00:00 UTC"),
        ("1970-01-01T02:00:00+02:00", "1970-01-01 00:00 UTC"),
        (None, "Unknown"),
        ("not-time", "Unknown"),
        (1e100, "Unknown"),
    ],
)
def test_timestamps(value, expected):
    assert parse_timestamp(value) == expected


def test_workspace_is_not_a_project_and_folders_are_sanitized_unique():
    assert project_folder({"workspace_id": "team-account"}) == "_Uncategorized"
    assert project_folder({"gizmo_id": "g-custom-gpt"}) == "_Uncategorized"
    folder = project_folder({"_project_id": "g-p-abc", "_folder_name": "../../outside"})
    assert "/" not in folder and not folder.startswith(".")
    assert folder != project_folder({"_project_id": "g-p-def", "_folder_name": "../../outside"})
    assert len(sanitize_filename("😀" * 100).encode()) <= 100


def test_no_path_traversal_or_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ExportError, match="escapes"):
        safe_output_path(root, "../outside")
    (root / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ExportError, match="escapes"):
        safe_output_path(root, "link/outside")


def test_atomic_outputs_have_private_permissions(tmp_path):
    path = tmp_path / "nested" / "test.json"
    write_json_file(path, {"x": 1})
    write_json_file(path, {"x": 2})
    assert json.loads(path.read_text()) == {"x": 2}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.rglob(".export-*"))


def test_duplicate_titles_id_stability_incremental_update_and_damage(tmp_path):
    config = replace(Config(), output_dir=tmp_path)
    details = {name: conversation(name) for name in ("a", "b", "c")}
    calls = []

    def fetch(ident):
        calls.append(ident)
        return details[ident]

    assert export_conversations(list(details.values()), fetch, config) == 0
    files = list((tmp_path / "_Uncategorized").glob("*.json"))
    assert len(files) == 3
    assert set(calls) == {"a", "b", "c"}
    calls.clear()
    assert export_conversations(list(reversed(list(details.values()))), fetch, config) == 0
    assert calls == []
    details["a"]["update_time"] = 2
    assert export_conversations(list(details.values()), fetch, config) == 0
    assert calls == ["a"]
    calls.clear()
    files[0].write_text("damaged")
    assert export_conversations(list(details.values()), fetch, config) == 0
    assert len(calls) == 1
    calls.clear()
    assert export_conversations(list(details.values()), fetch, config, refresh=True) == 0
    assert set(calls) == {"a", "b", "c"}


def test_format_switch_preserves_raw_json(tmp_path):
    detail = conversation()
    config = replace(Config(), output_dir=tmp_path)
    assert export_conversations([detail], lambda _: detail, config) == 0
    assert (
        export_conversations([detail], lambda _: detail, replace(config, output_format="markdown"))
        == 0
    )
    assert len(list(tmp_path.rglob("*.md"))) == 1
    raw = next((tmp_path / "_Uncategorized").glob("*.json"))
    assert json.loads(raw.read_text()) == detail
    markdown = next(tmp_path.rglob("*.md")).read_text()
    assert "Selected answer" in markdown and "Old answer" not in markdown


def test_no_timestamp_does_not_skip(tmp_path):
    detail = conversation(updated=None)
    config = replace(Config(), output_dir=tmp_path)
    calls = []
    for _ in range(2):
        assert (
            export_conversations([detail], lambda ident: calls.append(ident) or detail, config) == 0
        )
    assert len(calls) == 2


def test_detail_failure_nonzero_no_index_success_and_auth_stops(tmp_path):
    def missing(_):
        raise HTTPError(404, "conversation")

    config = replace(Config(), output_dir=tmp_path)
    assert export_conversations([conversation()], missing, config) == 1
    assert not (tmp_path / ".export-index.json").exists()

    def expired(_):
        raise HTTPError(401, "conversation")

    with pytest.raises(HTTPError):
        export_conversations([conversation()], expired, config)


@pytest.mark.parametrize(
    "data", [[], [conversation()], {"conversations": [conversation()]}, conversation()]
)
def test_import_json_shapes(tmp_path, data):
    source = tmp_path / "archive.json"
    source.write_text(json.dumps(data))
    assert load_archive(source) == ([] if data == [] else [conversation()])


def test_import_zip_reads_without_extracting_paths(tmp_path):
    source = tmp_path / "archive.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("../../conversations.json", json.dumps([conversation()]))
        archive.writestr("../../must-not-extract.txt", "anything")
    assert load_archive(source) == [conversation()]
    assert not (tmp_path / "must-not-extract.txt").exists()


@pytest.mark.parametrize("names", [[], ["a/conversations.json", "b/conversations.json"]])
def test_zip_missing_or_ambiguous_conversations_fails(tmp_path, names):
    path = tmp_path / "archive.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, "[]")
    with pytest.raises(ExportError):
        load_archive(path)


def test_cli_import_without_credentials_and_no_network(tmp_path, capsys):
    source = tmp_path / "conversations.json"
    source.write_text(json.dumps([conversation()]))
    output = tmp_path / "out"
    assert main(["import", str(source), "--format", "markdown", "--output", str(output)]) == 0
    assert "Selected answer" in next(output.rglob("*.md")).read_text()
    assert "failed: 0" in capsys.readouterr().out


def test_cli_error_is_nonzero_and_does_not_echo_input(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("fake-secret-not-json")
    assert main(["import", str(path)]) == 1
    assert "fake-secret" not in capsys.readouterr().err
    assert main(["probe"]) == 1


def test_import_duplicate_or_missing_ids_is_not_success(tmp_path):
    for items in [[conversation(), conversation()], [dict(conversation(), id=None)]]:
        path = tmp_path / "conversations.json"
        path.write_text(json.dumps(items))
        assert main(["import", str(path), "--output", str(tmp_path / "out")]) == 1


def test_optional_auxiliary_404_records_unavailable_without_overwriting_backup(tmp_path):
    original = tmp_path / "memories" / "memories.json"
    write_json_file(original, {"saved": "older snapshot"})

    class Client(ChatGPTClient):
        def json(self, path, token, **kwargs):
            if path == "settings/user":
                return {"setting": True}
            raise HTTPError(404, path)

    export_auxiliary_data(Client(Config()), "fake", tmp_path)
    statuses = json.loads((tmp_path / "auxiliary-status.json").read_text())
    assert statuses["memories/memories.json"]["available"] is False
    assert statuses["settings/settings.json"]["available"] is True
    assert json.loads(original.read_text()) == {"saved": "older snapshot"}


def test_detail_id_mismatch_is_not_recorded_as_success(tmp_path):
    config = replace(Config(), output_dir=tmp_path)
    assert export_conversations([conversation("a")], lambda _: conversation("b"), config) == 1
    assert not (tmp_path / ".export-index.json").exists()


@pytest.mark.parametrize("index", [[], {"json:conversation-a": "invalid-entry"}])
def test_invalid_index_fails_safely(tmp_path, index):
    write_json_file(tmp_path / ".export-index.json", index)
    with pytest.raises(ExportError, match="Invalid export index"):
        export_conversations(
            [conversation()], lambda _: conversation(), replace(Config(), output_dir=tmp_path)
        )

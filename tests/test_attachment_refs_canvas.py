import json

import pytest

from attachment_refs import AttachmentRef, discover_references, sandbox_path
from canvas_artifacts import canvas_artifacts, patch_text


def message_node(parent, recipient="all", data=None, *, author="assistant", name=None):
    return {
        "parent": parent,
        "children": [],
        "message": {
            "author": {"role": author, "name": name},
            "recipient": recipient,
            "content": {
                "content_type": "code",
                "text": json.dumps(data) if data is not None else "",
            },
        },
    }


def canvas_chat(updates=None):
    mapping = {
        "create": message_node(
            None,
            "canmore.create_textdoc",
            {"name": "Draft", "type": "document", "content": "Hello world"},
        ),
        "created": message_node(
            "create",
            data={"name": "Draft", "result": "created", "textdoc_id": "doc-1"},
            author="tool",
            name="canmore.create_textdoc",
        ),
        "update": message_node(
            "created",
            "canmore.update_textdoc",
            {
                "updates": updates
                if updates is not None
                else [{"pattern": "world", "replacement": "reader"}]
            },
        ),
        "reply": message_node("update"),
    }
    return {"id": "chat-a", "mapping": mapping, "current_node": "reply", "title": "Canvas chat"}


def test_discovers_uploads_images_audio_and_sandbox_across_branches_without_arbitrary_urls():
    detail = {
        "id": "chat",
        "current_node": "one",
        "mapping": {
            "one": {
                "message": {
                    "metadata": {
                        "attachments": [
                            {
                                "id": "file-upload",
                                "name": "report.pdf",
                                "mime_type": "application/pdf",
                            }
                        ]
                    },
                    "content": {
                        "parts": [
                            {"asset_pointer": "sediment://file_image"},
                            {"audio_asset_pointer": "file-service://file-audio"},
                            "[download](sandbox:/mnt/data/result%20file.csv)",
                            "https://evil.invalid/image.png",
                        ]
                    },
                }
            },
            "other-branch": {
                "message": {
                    "content": {
                        "parts": [
                            {"asset_pointer": "sediment://file_other"},
                            "[same path, other version](sandbox:/mnt/data/result%20file.csv)",
                        ]
                    }
                }
            },
        },
    }
    refs = discover_references(detail)
    assert {ref.locator for ref in refs} == {
        "file-upload",
        "file_image",
        "file-audio",
        "file_other",
        "/mnt/data/result file.csv",
    }
    assert len([ref for ref in refs if ref.kind == "sandbox"]) == 2
    assert next(ref for ref in refs if ref.locator == "file-upload").name == "report.pdf"
    assert "evil.invalid" not in json.dumps([ref.record() for ref in refs])


def test_rendered_sediment_fragment_uses_canonical_file_id_without_context_suffix():
    detail = {
        "id": "c",
        "mapping": {
            "m": {
                "message": {
                    "content": {
                        "parts": [
                            {
                                "asset_pointer": "sediment://opaque-render-context#file-image#frame.png"
                            }
                        ]
                    }
                }
            }
        },
    }
    refs = discover_references(detail)
    assert len(refs) == 1 and refs[0].kind == "file" and refs[0].locator == "file-image"


def test_same_file_reference_deduplicates_but_sandbox_versions_do_not():
    assert (
        AttachmentRef("file", "c", "file-x", "m1").key
        == AttachmentRef("file", "c", "file-x", "m2", project_id="g-p-abc").key
    )
    assert (
        AttachmentRef("sandbox", "c", "/mnt/data/x", "m1").key
        != AttachmentRef("sandbox", "c", "/mnt/data/x", "m2").key
    )
    assert (
        AttachmentRef("file", "", "file-x", project_id="g-p-a").key
        != AttachmentRef("file", "", "file-x", project_id="g-p-b").key
    )


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/mnt/data/../secret",
        "/mnt/data/%2e%2e/secret",
        "/mnt/data/%252e%252e/secret",
        "/mnt/data/x\\y",
        "/mnt/data/bad%00name",
    ],
)
def test_unsafe_sandbox_paths_are_not_resolved(path):
    assert sandbox_path(path) is None


def test_unrecognized_asset_pointer_is_explicit_not_silently_omitted():
    detail = {
        "id": "c",
        "mapping": {
            "m": {
                "message": {
                    "content": {
                        "parts": [{"asset_pointer": "sediment://unknown-format?sig=PRIVATE"}]
                    }
                }
            }
        },
    }
    refs = discover_references(detail)
    assert refs[0].kind == "unsupported"
    assert "PRIVATE" not in refs[0].locator


def test_canvas_create_update_and_confirmation_id_are_reconstructed():
    artifacts = canvas_artifacts(canvas_chat())
    assert len(artifacts) == 1
    assert artifacts[0].content == "Hello reader"
    assert artifacts[0].complete and artifacts[0].ref.locator == "doc-1"
    assert len(artifacts[0].history) == 2


def test_canvas_whole_document_update_does_not_duplicate_replacement_or_consume_backslashes():
    value = patch_text(
        "old\ncontent", [{"pattern": ".*", "multiple": True, "replacement": r"new \d+"}]
    )
    assert value == r"new \d+"


def test_unmatched_canvas_update_retains_last_good_content_and_marks_partial():
    artifact = canvas_artifacts(canvas_chat([{"pattern": "missing", "replacement": "x"}]))[0]
    assert artifact.content == "Hello world"
    assert not artifact.complete and artifact.reason == "canvas_patch_not_applied"
    assert artifact.history[-1]["payload"]["updates"][0]["pattern"] == "missing"


def test_full_canvas_rewrite_recovers_after_an_unapplied_patch():
    detail = canvas_chat([{"pattern": "missing", "replacement": "x"}])
    detail["mapping"]["rewrite"] = message_node(
        "update",
        "canmore.update_textdoc",
        {"updates": [{"pattern": ".*", "replacement": "Known final text"}]},
    )
    detail["current_node"] = "rewrite"
    artifact = canvas_artifacts(detail)[0]
    assert artifact.complete and artifact.content == "Known final text"
    assert len(artifact.history) == 3


def test_canvas_selected_branch_does_not_mix_regenerations():
    detail = canvas_chat()
    detail["mapping"]["abandoned"] = message_node(
        "created",
        "canmore.update_textdoc",
        {"updates": [{"pattern": ".*", "replacement": "WRONG BRANCH"}]},
    )
    assert canvas_artifacts(detail)[0].content == "Hello reader"


def test_ambiguous_multi_canvas_update_is_not_guessed():
    detail = canvas_chat()
    detail["mapping"]["second"] = message_node(
        "created",
        "canmore.create_textdoc",
        {"name": "Other", "type": "code/python", "content": "print(1)"},
    )
    detail["mapping"]["update"]["parent"] = "second"
    artifacts = canvas_artifacts(detail)
    assert len(artifacts) == 3 and all(not artifact.complete for artifact in artifacts)


def test_regex_patch_resource_budget_rejects_oversized_and_pathological_work():
    with pytest.raises(ValueError):
        patch_text("abc", [{"pattern": "x" * 17000, "replacement": "x"}])
    with pytest.raises((TimeoutError, ValueError)):
        patch_text("a" * 200000 + "!", [{"pattern": "(a+)+$", "replacement": "x"}])


def test_broken_canvas_branch_is_recorded_as_partial():
    detail = canvas_chat()
    detail["current_node"] = "missing"
    artifact = canvas_artifacts(detail)[0]
    assert not artifact.complete and artifact.reason == "broken_selected_branch"

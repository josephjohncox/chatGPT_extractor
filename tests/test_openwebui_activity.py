import html
import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
import requests

from openwebui_activity import PREVIEW_LIMIT
from openwebui_artifacts import enrich_chat
from openwebui_import import convert, main


def node(parent, role, text, *, recipient="all", kind="text", channel=None, end_turn=None):
    content = (
        {"content_type": kind, "text": text}
        if kind == "code"
        else {"content_type": kind, "parts": [text]}
    )
    return {
        "parent": parent,
        "message": {
            "id": "message-" + str(parent),
            "author": {"role": role, "name": "api_tool" if role == "tool" else None},
            "content": content,
            "recipient": recipient,
            "channel": channel,
            "end_turn": end_turn,
            "create_time": 1700000001,
        },
    }


def conversation():
    return {
        "id": "source",
        "title": "Historical tool use",
        "create_time": 1700000000,
        "update_time": 1700000010,
        "current_node": "final",
        "mapping": {
            "user": node(None, "user", "Review the document"),
            "call": node(
                "user",
                "assistant",
                json.dumps({"path": "/Legacy/getPage", "args": {"pageId": "123"}}),
                recipient="api_tool.call_tool",
                kind="code",
                end_turn=False,
            ),
            "result": node("call", "tool", '{"body":"Source document"}', kind="code"),
            "thought1": {
                "parent": "result",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {
                        "content_type": "thoughts",
                        "thoughts": [{"summary": "Review source", "content": "Recorded summary"}],
                    },
                    "create_time": 1700000002,
                },
            },
            "thought2": {
                "parent": "thought1",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"content_type": "reasoning_recap", "content": "Recap"},
                    "metadata": {"finished_duration_sec": 12},
                    "create_time": 1700000003,
                },
            },
            "create": node(
                "thought2",
                "assistant",
                json.dumps(
                    {"path": "/Legacy/createPage", "args": {"body": "<h1>Recorded draft</h1>"}}
                ),
                recipient="api_tool.call_tool",
                kind="code",
                end_turn=False,
            ),
            "created": node("create", "tool", '{"id":"456"}', kind="code"),
            "final": node(
                "created",
                "assistant",
                "Final review with findings.",
                channel="final",
                end_turn=True,
            ),
        },
    }


def test_tool_json_and_reasoning_are_activity_not_separate_answer_bubbles():
    source = conversation()
    before = deepcopy(source)
    result = convert(source)
    history = result["chat"]["history"]
    assert set(history["messages"]) == {"user", "final"}
    final = history["messages"]["final"]
    assert final["content"].startswith('<details type="imported_activity"')
    assert final["content"].endswith("Final review with findings.")
    assert final["parentId"] == "user"
    assert len(final["chatgpt_activity"]) == 6
    decoded = html.unescape(final["content"])
    assert "Historical tool call" in decoded
    assert "getPage" in decoded and "createPage" in decoded
    assert "Historical tool result" in decoded
    assert 'type="reasoning"' in decoded
    assert source == before == result["chat"]["chatgpt_source"]
    assert [m["id"] for m in result["chat"]["messages"]] == ["user", "final"]
    assert all("output" not in m and "tool_calls" not in m for m in history["messages"].values())
    assert final["timestamp"] == 1700000001
    assert (
        next(m for m in final["chatgpt_activity"] if m["id"] == "thought2")["timestamp"]
        == 1700000003
    )


def test_branch_activities_do_not_cross_contaminate():
    source = conversation()
    source["mapping"]["other-call"] = node(
        "thought2",
        "assistant",
        '{"path":"/Other/search","args":{}}',
        recipient="api_tool.call_tool",
        kind="code",
    )
    source["mapping"]["other-final"] = node(
        "other-call", "assistant", "Other answer", end_turn=True
    )
    result = convert(source)
    messages = result["chat"]["history"]["messages"]
    assert set(messages) == {"user", "final", "other-final"}
    assert messages["user"]["childrenIds"] == ["final", "other-final"]
    assert "createPage" not in messages["other-final"]["content"]
    assert "/Other/search" not in messages["final"]["content"]
    assert (
        "getPage" in messages["final"]["content"]
        and "getPage" in messages["other-final"]["content"]
    )
    assert result["chat"]["history"]["currentId"] == "final"


def test_artifact_on_collapsed_activity_links_to_each_branch_answer():
    source = conversation()
    source["mapping"]["other-final"] = node("thought2", "assistant", "Other answer", end_turn=True)
    chat = convert(source)["chat"]
    link = {
        "asset_id": "asset",
        "name": "source.pdf",
        "url": "https://example.test/file",
        "outcome": "saved",
        "reference": {"kind": "file", "message_id": "call"},
    }
    enriched = enrich_chat(chat, [link])
    for ident in ["final", "other-final"]:
        assert "Imported artifact" in enriched["history"]["messages"][ident]["content"]
    assert "Imported artifact" not in enriched["history"]["messages"]["user"]["content"]
    assert enrich_chat(enriched, [link]) == enriched


def test_unfinished_selected_turn_and_later_user_keep_activity():
    source = conversation()
    del source["mapping"]["final"]
    source["current_node"] = "created"
    result = convert(source)
    assert result["chat"]["history"]["currentId"] == "created"
    assert set(result["chat"]["history"]["messages"]) == {"user", "created"}
    assert (
        "No final assistant answer was recorded"
        in result["chat"]["history"]["messages"]["created"]["content"]
    )
    source["mapping"]["next-user"] = node("created", "user", "Continue")
    source["current_node"] = "next-user"
    messages = convert(source)["chat"]["history"]["messages"]
    assert messages["next-user"]["parentId"] == "created"


def test_legitimate_json_answer_and_user_code_are_not_tool_calls():
    source = conversation()
    source["mapping"] = {
        "user": node(None, "user", '{"path":"example","args":{}}'),
        "final": node(
            "user", "assistant", '{"path":"example","args":{}}', kind="code", end_turn=True
        ),
    }
    result = convert(source)
    assert (
        result["chat"]["history"]["messages"]["final"]["content"] == '{"path":"example","args":{}}'
    )


def test_node_mode_keeps_per_node_bubbles_but_tool_calls_are_collapsed():
    result = convert(conversation(), activity_mode="nodes")
    messages = result["chat"]["history"]["messages"]
    assert set(messages) == set(conversation()["mapping"])
    assert messages["call"]["content"].startswith('<details type="imported_tool"')


def test_historical_mermaid_fences_and_confluence_macros_get_inert_diagram_previews():
    source = conversation()
    fenced = "```mermaid\nflowchart LR\n A[Start] --> B[Done]\n```"
    macro = (
        '<div data-type="extension" data-parameters="{&quot;guestParams&quot;:{&quot;syntax&quot;:&quot;Mermaid&quot;,'
        '&quot;__bodyContent&quot;:&quot;sequenceDiagram\n A-&gt;&gt;B: Recorded&quot;}}">Diagram</div>'
    )
    call = json.dumps({"path": "/Legacy/createPage", "args": {"body": fenced + macro}})
    source["mapping"]["create"]["message"]["content"]["text"] = call
    result = convert(source)
    text = html.unescape(result["chat"]["history"]["messages"]["final"]["content"])
    assert text.count('type="imported_diagram"') == 2
    assert "```mermaid" in text
    assert "flowchart LR" in text and "sequenceDiagram" in text
    assert "Historical record only; not executed during import." in text
    assert (
        result["chat"]["chatgpt_source"]["mapping"]["create"]["message"]["content"]["text"] == call
    )


def test_non_mermaid_or_malformed_confluence_macro_has_no_diagram_preview():
    source = conversation()
    body = '<div data-parameters="{&quot;guestParams&quot;:{&quot;syntax&quot;:&quot;PlantUML&quot;,&quot;__bodyContent&quot;:&quot;A--B&quot;}}"></div>'
    source["mapping"]["create"]["message"]["content"]["text"] = json.dumps(
        {"path": "/Legacy/createPage", "args": {"body": body}}
    )
    result = convert(source)
    text = html.unescape(result["chat"]["history"]["messages"]["final"]["content"])
    assert 'type="imported_diagram"' not in text


def test_tool_preview_is_bounded_and_original_payload_remains_complete():
    source = conversation()
    body = "</details><script>never_execute()</script>```" + "x" * (PREVIEW_LIMIT + 100)
    original = json.dumps({"path": "/Legacy/createPage", "args": {"body": body}})
    source["mapping"]["create"]["message"]["content"]["text"] = original
    result = convert(source)
    text = result["chat"]["history"]["messages"]["final"]["content"]
    assert "Preview truncated" in text
    assert "<script>" not in text
    assert text.count("</details>") == 1  # Only the generated outer wrapper is literal.
    assert (
        result["chat"]["chatgpt_source"]["mapping"]["create"]["message"]["content"]["text"]
        == original
    )


def test_selected_mid_turn_anchor_and_hidden_results_are_preserved_correctly():
    source = conversation()
    source["current_node"] = "thought2"
    source["mapping"]["result"]["message"]["metadata"] = {
        "is_visually_hidden_from_conversation": True
    }
    result = convert(source)
    history = result["chat"]["history"]
    assert history["currentId"] == "thought2"
    assert set(history["messages"]) == {"user", "thought2", "final"}
    assert history["messages"]["final"]["parentId"] == "thought2"
    assert "result" not in history["messages"]["thought2"]["chatgpt_source_node_ids"]
    assert "result" in result["chat"]["chatgpt_source"]["mapping"]


@pytest.mark.parametrize("mode", ["compact", "nodes"])
def test_cli_applies_activity_mode(tmp_path, monkeypatch, mode):
    path = tmp_path / "chat.json"
    path.write_text(json.dumps(conversation()))
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    uploaded = []

    def respond(method, url, **kwargs):
        response = Mock(status_code=200)
        if method == "GET":
            response.json.return_value = {"id": "user"}
        else:
            assert url.endswith("/chats/import")
            form = kwargs["json"]["chats"][0]
            uploaded.append(form)
            response.json.return_value = [{"id": "remote", **form}]
        return response

    session.request.side_effect = respond
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setenv("OPENWEBUI_TOKEN", "test")
    assert (
        main(
            [
                str(path),
                "--activity-rendering",
                mode,
                "--upload",
                "--url",
                "https://example.test",
                "--state",
                str(tmp_path / "state.sqlite3"),
            ]
        )
        == 0
    )
    messages = uploaded[0]["chat"]["history"]["messages"]
    assert len(messages) == (2 if mode == "compact" else 8)
    assert all(not m["content"].lstrip().startswith('{"path":') for m in messages.values())

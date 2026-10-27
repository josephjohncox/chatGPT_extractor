from copy import deepcopy

import pytest

from openwebui_import import convert, render_content
from openwebui_reasoning import TAG_NAMES, convert_inline_reasoning, render_reasoning


def message(content, **kwargs):
    return {"author": {"role": "assistant"}, "content": content, **kwargs}


def test_structured_thoughts_become_saved_reasoning_not_json_reply():
    source = message(
        {
            "content_type": "thoughts",
            "source_analysis_msg_id": "original-analysis",
            "thoughts": [
                {
                    "summary": "Check the units",
                    "content": "Compare GB and GiB.",
                    "chunks": [],
                    "finished": True,
                }
            ],
        }
    )
    result = render_reasoning(source, render_content(source["content"]))
    assert result.startswith('<details type="reasoning" done="true">\n')
    assert "<summary>Imported ChatGPT reasoning summary</summary>\n" in result
    assert "> Check the units" in result
    assert "> Compare GB and GiB." in result
    assert "original-analysis" not in result
    assert "```json" not in result
    assert result.endswith("</details>")
    assert "duration=" not in result


def test_recap_uses_exported_duration_and_marks_cancelled_state():
    source = message(
        {"content_type": "reasoning_recap", "content": "Exported recap"},
        metadata={"finished_duration_sec": 42, "reasoning_status": "reasoning_cancelled"},
    )
    result = render_reasoning(source, "unused")
    assert 'duration="42"' in result
    assert "recap (cancelled)" in result
    assert "> Exported recap" in result


@pytest.mark.parametrize("duration", [None, "42", True, -1, float("nan"), float("inf")])
def test_missing_or_invalid_duration_not_invented(duration):
    source = message(
        {"content_type": "reasoning_recap", "content": "Recap"},
        metadata={"finished_duration_sec": duration},
    )
    assert "duration=" not in render_reasoning(source, "unused")


@pytest.mark.parametrize(
    "fields",
    [
        {"channel": "analysis"},
        {"metadata": {"is_thinking_preamble_message": True}},
        {"metadata": {"reasoning_status": "is_reasoning"}},
    ],
)
def test_explicit_reasoning_metadata_is_respected(fields):
    source = message({"content_type": "text", "parts": ["Checking sources"]}, **fields)
    assert 'type="reasoning"' in render_reasoning(source, "Checking sources")


@pytest.mark.parametrize(
    "fields",
    [
        {"channel": "commentary"},
        {"metadata": {"reasoning_status": "reasoning_ended"}},
        {"recipient": "python", "metadata": {"reasoning_status": "is_reasoning"}},
    ],
)
def test_ordinary_final_commentary_and_tool_invocations_are_not_relabelled(fields):
    source = message({"content_type": "text", "parts": ["Ordinary message"]}, **fields)
    assert render_reasoning(source, "Ordinary message") == "Ordinary message"


def test_code_content_with_example_tags_is_untouched():
    source = message({"content_type": "code", "text": "<think>example</think>"})
    assert render_reasoning(source, source["content"]["text"]) == "<think>example</think>"


@pytest.mark.parametrize("tag", [*TAG_NAMES, "|begin_of_thought|"])
def test_complete_inline_reasoning_becomes_details_and_final_answer_stays_outside(tag):
    closing = "<|end_of_thought|>" if tag.startswith("|") else f"</{tag}>"
    text = f"<{tag}>Exported reasoning{closing}\n\nFinal answer."
    result = convert_inline_reasoning(text)
    assert 'type="reasoning"' in result
    assert result.endswith("Final answer.")
    assert "Final answer." not in result.split("</details>")[0]
    assert convert_inline_reasoning(result) == result


@pytest.mark.parametrize(
    "text",
    [
        "```xml\n<think>example</think>\n```\nAnswer",
        '~~~mermaid\nflowchart LR\n A["<think>example</think>"]\n~~~\n',
        "Use `<think>example</think>` tags.",
        "    <think>Indented example</think>\n",
        "\t<think>Tab-indented example</think>\n",
        "```\n<think>Unclosed fenced example</think>",
        "<think>Unclosed reasoning tag",
    ],
)
def test_code_and_incomplete_tags_are_not_reinterpreted(text):
    assert convert_inline_reasoning(text) == text


def test_exported_closing_tags_cannot_break_outer_details_block():
    source = message(
        {
            "content_type": "thoughts",
            "thoughts": [{"summary": "</summary>", "content": "</details><script>bad()</script>"}],
        }
    )
    result = render_reasoning(source, "unused")
    assert result.count("</details>") == 1
    assert result.count("</summary>") == 1
    assert "<script>" not in result
    assert "&lt;/details&gt;" in result


def test_unknown_or_empty_reasoning_stays_explicit():
    source = message({"content_type": "thoughts", "thoughts": []})
    result = render_reasoning(source, "unused")
    assert "No readable reasoning text was included" in result
    assert 'type="reasoning"' in result


def test_import_keeps_source_nodes_dates_and_final_branch():
    thought = message(
        {
            "content_type": "thoughts",
            "thoughts": [{"summary": "Summary", "content": "Exported detail"}],
        },
        create_time=1700000002,
    )
    recap = message(
        {"content_type": "reasoning_recap", "content": "Recap"},
        metadata={"finished_duration_sec": 12},
        create_time=1700000003,
    )
    final = message(
        {"content_type": "text", "parts": ["Final answer"]},
        metadata={"reasoning_status": "reasoning_ended"},
        create_time=1700000004,
    )
    source: dict = {
        "id": "source",
        "create_time": 1700000000,
        "update_time": 1700000010,
        "current_node": "final",
        "mapping": {
            "question": {
                "parent": None,
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["<think>user example</think>"]},
                },
            },
            "thought": {"parent": "question", "message": thought},
            "recap": {"parent": "thought", "message": recap},
            "final": {"parent": "recap", "message": final},
            "alternate": {"parent": "thought", "message": deepcopy(final)},
        },
    }
    before = deepcopy(source)
    result = convert(source, activity_mode="nodes")
    history = result["chat"]["history"]
    assert source == before == result["chat"]["chatgpt_source"]
    assert set(history["messages"]) == set(source["mapping"])
    assert history["messages"]["thought"]["childrenIds"] == ["recap", "alternate"]
    assert history["messages"]["thought"]["timestamp"] == 1700000002
    assert history["messages"]["final"]["content"] == "Final answer"
    assert history["messages"]["question"]["content"] == "<think>user example</think>"
    assert history["currentId"] == "final"
    assert [m["id"] for m in result["chat"]["messages"]] == [
        "question",
        "thought",
        "recap",
        "final",
    ]
    assert result["created_at"] == 1700000000 and result["updated_at"] == 1700000010
    assert 'type="reasoning"' in history["messages"]["thought"]["content"]
    assert 'duration="12"' in history["messages"]["recap"]["content"]
    assert all("output" not in m for m in history["messages"].values())


def test_hidden_reasoning_is_retained_only_in_source():
    source = {
        "id": "source",
        "create_time": 1700000000,
        "update_time": 1700000010,
        "current_node": "final",
        "mapping": {
            "hidden": {
                "parent": None,
                "message": message(
                    {"content_type": "thoughts", "thoughts": []},
                    metadata={"is_visually_hidden_from_conversation": True},
                ),
            },
            "final": {"parent": "hidden", "message": message({"parts": ["Answer"]})},
        },
    }
    result = convert(source)
    assert "hidden" not in result["chat"]["history"]["messages"]
    assert "hidden" in result["chat"]["chatgpt_source"]["mapping"]

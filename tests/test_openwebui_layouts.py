import json
from copy import deepcopy
from unittest.mock import Mock

import pytest
import requests

from openwebui_import import convert, load_conversations, main, render_content
from openwebui_layouts import MAX_DEPTH, MAX_NODES, render_layouts

EXAMPLE = (
    "<box gap={2}> <grid columns={2} gap={2}> "
    '<box background="surface-secondary" radius="lg" padding={3} gap={1}> '
    '<text color="secondary" size="xs">Sum of visible entries</text> '
    '<title size="xl" tabularNums>67.30 GB</title> </box> '
    '<box background="surface-secondary" radius="lg" padding={3} gap={1}> '
    '<text color="secondary" size="xs">Excluding Photos, Music, Messages</text> '
    '<title size="xl" tabularNums>64.75 GB</title> </box> </grid> </box>'
)
PROSE = (
    "That 64.75 GB is already close to the roughly 66–67 GB indicated by the red "
    "Applications bar. The remaining smaller applications can plausibly account for the difference."
)
MERMAID = '```mermaid\nflowchart LR\n  A["<box>literal label</box>"] --> B[Done]\n```\n'


def test_entities_and_citations_become_readable_inline_text():
    text = (
        '<Entity category="people" value="Simon Willison"/>: empirical work '
        '<Cite refs={["turn133202search2","turn133202search5"]}/>; '
        '<Entity category="book" value="Category Theory in Context"/> '
        '<Cite ref="turn411585search2"/>.'
    )
    assert render_layouts(text) == (
        "Simon Willison: empirical work "
        "*(citation: turn133202search2, turn133202search5)*; "
        "Category Theory in Context *(citation: turn411585search2)*."
    )


@pytest.mark.parametrize(
    "component",
    [
        '<Entity category="people"/>',
        '<Cite refs={["turn good"]}/>',
        "<Cite refs={dynamicRefs}/>",
        "<Cite/>",
    ],
)
def test_dynamic_or_incomplete_entity_and_citation_stay_visible_source(component):
    result = render_layouts(component)
    assert "Unsupported layout (original source):" in result
    assert component in result


def test_literal_svelte_svg_loop_becomes_accessible_diagram_text():
    text = """<box border radius="lg" padding={3} gap={2}>
  <svg viewBox="0 0 330 73" width="100%">
    {#each [{x:5,t:"Original work"},{x:116,t:"Clear exposition"},{x:227,t:"Community use"}] as x}
      <rect x={x.x} y="14" width="98" height="38"/>
      <text x={x.x+49} y="37">{x.t}</text>
    {/each}
    <path d="M103 33 H115"/>
    <text x="165" y="67">Feedback and collaboration improve the work</text>
  </svg>
</box>"""
    result = render_layouts(text)
    assert result.startswith("\n\n```mermaid\nflowchart TB")
    for label in [
        "Original work",
        "Clear exposition",
        "Community use",
        "Feedback and collaboration improve the work",
    ]:
        assert label in result
    assert "N1 --> N2" in result
    assert "<svg" not in result and "Unsupported layout" not in result
    assert "{#each" not in result


def test_static_svg_becomes_native_mermaid_without_executing_code():
    result = render_layouts("<box><svg><text>Static SVG label</text></svg></box>")
    assert "```mermaid" in result and "Static SVG label" in result
    assert "<svg" not in result


def test_dynamic_svg_stays_visible_source():
    svg = "<box><svg>{#each dynamicItems as x}<text>{x.t}</text>{/each}</svg></box>"
    result = render_layouts(svg)
    assert "Unsupported layout (original source):" in result
    assert svg in result


def test_text_link_component_becomes_safe_markdown_link():
    text = (
        '<text color="secondary" size="xs">Reviewed: '
        '<Link url="https://hadrian-automation.atlassian.net/wiki/spaces/EPD/pages/3791816594" '
        'title="FSpec: Generic Requisitions"/> · Michael Gallagher · October 19, 2026</text>'
    )
    assert render_layouts(text).strip() == (
        "Reviewed: [FSpec: Generic Requisitions]("
        "<https://hadrian-automation.atlassian.net/wiki/spaces/EPD/pages/3791816594>) "
        "· Michael Gallagher · October 19, 2026"
    )
    html_result = render_layouts(text, "html")
    assert (
        'href="https://hadrian-automation.atlassian.net/wiki/spaces/EPD/pages/3791816594"'
        in html_result
    )
    assert 'rel="noreferrer"' in html_result


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "file:///etc/passwd", "//example.test", "https://bad example"]
)
def test_unsafe_layout_link_stays_visible_source(url):
    text = f'<box><Link url="{url}" title="Example"/></box>'
    result = render_layouts(text)
    assert "Unsupported layout (original source):" in result
    assert text in result


def test_link_title_cannot_inject_an_extra_markdown_destination():
    text = '<text><Link url="https://safe.example" title="x](https://evil.example)"/></text>'
    result = render_layouts(text)
    assert result.count("](<") == 1
    assert "x\\]\\(" in result
    assert "<https://safe.example>" in result


def test_user_example_becomes_readable_table_with_prose_unchanged():
    result = render_layouts(EXAMPLE + "\n" + PROSE)
    assert "| Measure | Value |" in result
    assert "| Sum of visible entries | **67.30 GB** |" in result
    assert "| Excluding Photos, Music, Messages | **64.75 GB** |" in result
    assert result.endswith(PROSE)
    assert "<box" not in result and "<grid" not in result
    assert render_layouts(result) == result


@pytest.mark.parametrize("mode", ["markdown", "html", "source"])
@pytest.mark.parametrize(
    "code",
    [
        MERMAID,
        '~~~mermaid\r\ngraph TD\r\n A["<box>literal</box>"]\r\n~~~\r\n',
        "````jsx\n<box><text>example with ``` nested fence</text></box>\n````\n",
        '```python\nexample = "<box>source</box>"\n```\n',
        "Use `<box gap={2}>` and ``<grid> `backtick` </grid>`` as examples.",
        "    <box>Indented code</box>\n    <grid>Also code</grid>\n",
        "\t<box>Tab-indented code</box>\n",
        r"Escaped \<box>literal example</box>",
        "```mermaid\nUnclosed fence with <box>example</box>",
    ],
)
def test_existing_code_is_byte_for_byte_unchanged(mode, code):
    assert render_layouts(code, mode) == code


def test_mermaid_and_code_around_layout_are_not_modified():
    suffix = '\n```json\n{"example": "<box>literal</box>"}\n```\n'
    result = render_layouts(MERMAID + EXAMPLE + suffix)
    assert result.startswith(MERMAID)
    assert result.endswith(suffix)
    assert "| Sum of visible entries |" in result


def test_html_mode_adds_static_artifact_with_fixed_styles_and_no_script():
    result = render_layouts(EXAMPLE, "html")
    assert "| Sum of visible entries |" in result  # Inline fallback remains available.
    assert "```html\n<!doctype html>" in result
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in result
    assert "font-variant-numeric:tabular-nums" in result
    assert "default-src 'none'" in result
    assert "<script" not in result
    assert "<box" not in result
    assert render_layouts(result, "html") == result


@pytest.mark.parametrize(
    "markup",
    [
        "<box><UnknownWidget value={2}/></box>",
        "<box gap={computeGap()}><text>Text</text></box>",
        '<box onClick="alert(1)"><text>Text</text></box>',
        '<box style={{background: "red"}}><text>Text</text></box>',
        "<box><text>Mismatch</title></box>",
        "<box><script>alert(1)</script></box>",
        "<box><divider>Do not discard this content</divider></box>",
        "<box><text>Unclosed",
    ],
)
def test_unknown_dynamic_and_malformed_markup_remains_visible_source(markup):
    result = render_layouts(markup, "html")
    assert "Unsupported layout (original source):" in result
    assert "```text\n" in result
    assert markup in result
    assert "<!doctype html>" not in result


def test_unknown_layout_does_not_swallow_surrounding_prose():
    result = render_layouts("Before. <box><Unknown/></box> After.")
    assert result.startswith("Before. ")
    assert result.endswith(" After.")


def test_literals_do_not_become_html_attributes_or_tags():
    text = '<box background="url(https://bad.example)"><text>&lt;img src=x onerror=alert(1)&gt;</text></box>'
    result = render_layouts(text, "html")
    assert "background:url" not in result
    assert "https://bad.example" not in result
    assert "<img" not in result
    assert "&lt;img" in result


def test_rows_badges_icons_and_grid_items_keep_content():
    text = '<box><row><icon name="check"/><text weight="bold">Status</text><badge>Ready</badge></row><divider/><grid><grid-item><caption>Detail</caption><text>More</text></grid-item></grid></box>'
    result = render_layouts(text)
    for expected in ["[check]", "**Status**", "**Ready**", "---", "Detail", "More"]:
        assert expected in result
    assert "Unsupported" not in result


def test_table_escapes_pipes_and_does_not_drop_extra_content():
    text = "<box><table><table-row><table-cell>A | B</table-cell><table-cell>C</table-cell></table-row></table></box>"
    assert "| A \\| B | C |" in render_layouts(text)
    extra = text.replace("<table-row>", "<table-row>Important stray text")
    assert "Important stray text" in render_layouts(extra)


@pytest.mark.parametrize(
    "markup",
    [
        "<box>" * (MAX_DEPTH + 1) + "Deep text" + "</box>" * (MAX_DEPTH + 1),
        "<box>" + "<text>x</text>" * MAX_NODES + "</box>",
    ],
)
def test_complexity_limits_preserve_source(markup):
    result = render_layouts(markup)
    assert "Unsupported layout" in result
    assert markup in result


def detail(text):
    return {
        "id": "source",
        "create_time": 1700000000,
        "update_time": 1700000010,
        "current_node": "a",
        "mapping": {
            "u": {
                "parent": None,
                "message": {"author": {"role": "user"}, "content": {"parts": [EXAMPLE]}},
            },
            "a": {
                "parent": "u",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": [text]},
                },
            },
        },
    }


def test_converter_preserves_source_user_messages_branches_and_dates():
    source = detail(EXAMPLE + "\n" + MERMAID)
    before = deepcopy(source)
    form = convert(source)
    assert source == before == form["chat"]["chatgpt_source"]
    messages = form["chat"]["history"]["messages"]
    assert messages["u"]["content"] == EXAMPLE
    assert "| Measure |" in messages["a"]["content"]
    assert messages["a"]["content"].endswith(MERMAID)
    assert messages["a"]["parentId"] == "u"
    assert form["chat"]["messages"][-1] == messages["a"]
    assert (form["created_at"], form["updated_at"]) == (1700000000, 1700000010)
    assert (
        convert(source, "source")["chat"]["history"]["messages"]["a"]["content"]
        == EXAMPLE + "\n" + MERMAID
    )


def test_structured_code_content_is_not_reinterpreted():
    assert render_content({"content_type": "code", "text": EXAMPLE}) == EXAMPLE


def test_changed_rendering_has_distinct_digest_for_explicit_reimport(tmp_path):
    source = tmp_path / "chat.json"
    source.write_text(json.dumps(detail(EXAMPLE)))
    original = next(load_conversations(source, layout_mode="source"))
    markdown = next(load_conversations(source))
    html = next(load_conversations(source, layout_mode="html"))
    assert len({original[1], markdown[1], html[1]}) == 3
    assert original[2]["chat"]["chatgpt_source"] == markdown[2]["chat"]["chatgpt_source"]


@pytest.mark.parametrize("mode", ["markdown", "html", "source"])
def test_cli_upload_applies_requested_rendering(tmp_path, monkeypatch, mode):
    source = tmp_path / "chat.json"
    source.write_text(json.dumps(detail(EXAMPLE + "\n" + MERMAID)))
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    uploaded = []

    def respond(method, url, **kwargs):
        result = Mock(status_code=200)
        if method == "GET":
            result.json.return_value = {"id": "user"}
        else:
            assert url.endswith("/chats/import")
            form = kwargs["json"]["chats"][0]
            uploaded.append(form)
            result.json.return_value = [{"id": "remote", **form}]
        return result

    session.request.side_effect = respond
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setenv("OPENWEBUI_TOKEN", "test")
    assert (
        main(
            [
                str(source),
                "--layout-rendering",
                mode,
                "--upload",
                "--url",
                "https://example.test",
                "--state",
                str(tmp_path / "journal.sqlite3"),
            ]
        )
        == 0
    )
    content = uploaded[0]["chat"]["history"]["messages"]["a"]["content"]
    assert content.endswith(MERMAID)
    if mode == "source":
        assert content.startswith(EXAMPLE)
    else:
        assert "| Sum of visible entries | **67.30 GB** |" in content
        assert ("<!doctype html>" in content) == (mode == "html")

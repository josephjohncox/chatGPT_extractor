from copy import deepcopy

from openwebui_citations import enrich_citations
from openwebui_import import convert


def metadata():
    return {
        "search_result_groups": [
            {
                "entries": [
                    {
                        "ref_id": {"turn_index": 1001, "ref_type": "search", "ref_index": 2},
                        "url": "https://example.test/simon",
                        "title": "Simon source",
                        "snippet": "Concrete work.",
                    },
                    {
                        "ref_id": {"turn_index": 1001, "ref_type": "search", "ref_index": 5},
                        "url": "https://example.test/julia",
                        "title": "Julia source",
                        "snippet": "Clear explanations.",
                    },
                ]
            }
        ]
    }


def test_resolved_archival_markers_become_openwebui_numeric_citations():
    content, sources = enrich_citations(
        "Statement *(citation: turn1001search2, turn1001search5)*.", metadata()
    )
    assert content == "Statement [1,2]."
    assert [source["source"]["id"] for source in sources] == [
        "turn1001search2",
        "turn1001search5",
    ]
    assert sources[0] == {
        "source": {
            "id": "turn1001search2",
            "name": "Simon source",
            "url": "https://example.test/simon",
        },
        "document": ["Concrete work."],
        "metadata": [
            {
                "source": "https://example.test/simon",
                "name": "Simon source",
                "external": True,
                "chatgpt_ref_id": "turn1001search2",
            }
        ],
        "distances": [],
    }


def test_raw_chatgpt_citation_markup_becomes_rich_citations():
    content, sources = enrich_citations(
        "Use this. citeturn1001search2turn1001search5", metadata()
    )
    assert content == "Use this. [1,2]"
    assert [source["source"]["id"] for source in sources] == [
        "turn1001search2",
        "turn1001search5",
    ]


def test_content_reference_groups_resolve_raw_view_ids():
    metadata_with_group = {
        "content_references": [
            {
                "matched_text": "citeturn2002view2turn3003view6",
                "items": [
                    {
                        "title": "Corporate Finance Class",
                        "url": "https://pages.stern.nyu.edu/corpfin.html",
                        "snippet": "Lectures and projects.",
                    }
                ],
            }
        ]
    }
    content, sources = enrich_citations(
        "Study this. citeturn2002view2turn3003view6", metadata_with_group
    )
    assert content == "Study this. [1,2]"
    assert [source["source"]["id"] for source in sources] == ["turn2002view2", "turn3003view6"]
    assert all(
        source["source"]["url"] == "https://pages.stern.nyu.edu/corpfin.html" for source in sources
    )


def test_unresolved_refs_remain_visible_and_resolved_refs_still_work():
    content, sources = enrich_citations(
        "Statement *(citation: turn1001search2, turn9999search0)*.", metadata()
    )
    assert content == "Statement [1] *(citation: turn9999search0)*."
    assert len(sources) == 1


def test_duplicates_reuse_one_source_and_stable_number():
    content, sources = enrich_citations(
        "One *(citation: turn1001search2)*. Two *(citation: turn1001search2)*.", metadata()
    )
    assert content == "One [1]. Two [1]."
    assert len(sources) == 1


def test_bad_urls_never_become_source_objects():
    unsafe = deepcopy(metadata())
    unsafe["search_result_groups"][0]["entries"][0]["url"] = "javascript:alert(1)"
    content, sources = enrich_citations("X *(citation: turn1001search2)*", unsafe)
    assert content == "X *(citation: turn1001search2)*"
    assert sources == []


def test_import_attaches_sources_and_numeric_citation_to_message():
    source = {
        "id": "source",
        "create_time": 1700000000,
        "update_time": 1700000001,
        "current_node": "answer",
        "mapping": {
            "question": {
                "parent": None,
                "message": {"author": {"role": "user"}, "content": {"parts": ["Who?"]}},
            },
            "answer": {
                "parent": "question",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {
                        "parts": [
                            '<Entity category="people" value="Simon Willison"/> <Cite ref="turn1001search2"/>'
                        ]
                    },
                    "metadata": metadata(),
                },
            },
        },
    }
    before = deepcopy(source)
    result = convert(source)
    answer = result["chat"]["history"]["messages"]["answer"]
    assert answer["content"] == "Simon Willison [1]"
    assert answer["sources"][0]["source"]["url"] == "https://example.test/simon"
    assert source == before == result["chat"]["chatgpt_source"]


def test_existing_sources_keep_their_indices():
    existing = [
        {
            "source": {"id": "prior", "name": "Prior", "url": "https://prior.test"},
            "document": ["Prior"],
            "metadata": [{"source": "https://prior.test"}],
        }
    ]
    content, sources = enrich_citations("X *(citation: turn1001search2)*", metadata(), existing)
    assert content == "X [2]"
    assert sources[0] == existing[0]
    assert sources[1]["source"]["id"] == "turn1001search2"

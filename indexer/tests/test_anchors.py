"""The heading anchor is one function, shared by chunks and the reader."""

from urllib.parse import quote

from src.anchors import assign_heading_anchors, encode_fragment, heading_anchor
from src.chunker import chunk_document
from src.document_loader import uri_to_slug
from src.models import Document
from src.snapshot_schema import COPY_COLUMNS
from src.transcript_export import project_transcript

SPACED = "俄军更换指挥官 美欧援助重装备"
ANCHOR = "俄军更换指挥官-美欧援助重装备"
URI = "CanKaoXinXi/0001-0100/0017.md"


def test_whitespace_collapses_to_one_hyphen():
    assert heading_anchor(SPACED) == ANCHOR
    assert heading_anchor(f"## {SPACED}") == ANCHOR
    assert heading_anchor("甲  乙\t丙") == "甲-乙-丙"
    assert heading_anchor("  -两边-  ") == "两边"


def test_link_is_unwrapped_and_footnote_is_removed():
    assert heading_anchor("[标题](https://example.com/a)") == "标题"
    assert heading_anchor("[[标题](https://a.example)](https://b.example)") == "标题"
    assert heading_anchor("话题[^1]") == "话题"
    assert heading_anchor("话题[^注]") == "话题"


def test_wrapping_emphasis_is_stripped_once():
    assert heading_anchor("**加粗标题**") == "加粗标题"
    assert heading_anchor("__下划线__") == "下划线"
    assert heading_anchor("`代码标题`") == "代码标题"
    # Two separate wraps are not one wrapping pair.
    assert heading_anchor("**甲** **乙**") == "**甲**-**乙**"


def test_duplicate_heading_gains_a_numeric_suffix():
    assert assign_heading_anchors(["同题", "同题", "同题"]) == [
        "同题",
        "同题-2",
        "同题-3",
    ]
    # A heading that is already the -2 form keeps it; the duplicate skips ahead.
    assert assign_heading_anchors(["同题", "同题-2", "同题"]) == [
        "同题",
        "同题-2",
        "同题-3",
    ]


def test_empty_anchor_takes_no_dedupe_slot():
    assert heading_anchor("---") == ""
    assert assign_heading_anchors(["---", "话题", "   "]) == ["", "话题", ""]


def test_html_id_is_raw_and_only_the_fragment_is_encoded():
    assert encode_fragment(ANCHOR) == quote(ANCHOR, safe="")
    assert "-" in encode_fragment(ANCHOR)
    assert encode_fragment(ANCHOR) != ANCHOR

    raw = _transcript(f"## {SPACED}\n\n" + "正" * 20).encode()
    html = project_transcript(URI, raw, "参考信息17").body_html
    assert f'id="{ANCHOR}"' in html
    assert f'id="{encode_fragment(ANCHOR)}"' not in html


def test_chunker_anchor_matches_exported_h2_id():
    body = "\n\n".join(
        [
            "开" * 60,
            f"## {SPACED}",
            "甲" * 60,
            f"## {SPACED}",
            "乙" * 60,
        ]
    )
    markdown = f"# 标题\n\n**发布日期** 2024-01-02\n\n## 正文\n\n{body}\n\n## 附录\n"
    chunks = chunk_document(
        Document(
            file_path=URI,
            doc_id=URI,
            slug=uri_to_slug(URI),
            text=body,
        ),
        min_chunk_size=1,
    )
    html = project_transcript(URI, markdown.encode(), "参考信息17").body_html

    assert [c.heading_anchor for c in chunks] == ["", ANCHOR, f"{ANCHOR}-2"]
    assert chunks[1].heading == SPACED
    assert " " not in ANCHOR
    assert f'id="{ANCHOR}"' in html
    assert f'id="{ANCHOR}-2"' in html
    assert 'id="正文"' not in html
    assert 'id="附录"' not in html


def test_copy_columns_list_heading_anchor_after_heading():
    columns = COPY_COLUMNS["document_chunks"]
    assert columns[columns.index("heading") + 1] == "heading_anchor"


def _transcript(body: str) -> str:
    return f"# 标题\n\n**发布日期** 2024-01-02\n\n## 正文\n\n{body}\n\n## 附录\n"

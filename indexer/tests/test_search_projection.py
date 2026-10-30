"""Complete-body search coverage and visible-text/anchor contracts."""

from pathlib import Path

from src.document_loader import load_indexable_source
from src.search_projection import normalize, plain_text, project_search
from src.search_text import analyze, lexeme, query_terms


def project(body):
    raw = (
        "# 非正文标题\n发布日期 2026-10-18\n## 正文\n"
        + body
        + "\n## 附录\n附录独占词\n"
    ).encode()
    return project_search(load_indexable_source("sample.md", raw))


def test_full_body_includes_short_preamble_and_topics_without_appendix():
    result = project("短开场\n\n## 话题\n贵州\n\n## 话题\n债务")
    assert result.body_text == "短开场\n\n话题\n\n贵州\n\n话题\n\n债务"
    assert [s["heading_anchor"] for s in result.sections] == ["", "话题", "话题-2"]
    for section in result.sections:
        assert result.body_text[section["start"] : section["end"]]
    assert "附录" not in result.body_text
    assert "非正文标题" not in result.body_text


def test_projection_keeps_visible_labels_and_code_but_not_link_destinations():
    result = project(
        "[可见标签](https://example.com/隐藏地址) **粗体** `Code`\n\n"
        "| 表头 |\n| --- |\n| 表格内容 |\n\n"
        "<script>不可见脚本</script>\n\n段落[^1]\n\n```txt\n代码文本\n```"
    )
    for visible in ["可见标签", "粗体", "Code", "表头", "表格内容", "段落", "代码文本"]:
        assert visible in result.body_text
    for hidden in ["隐藏地址", "example.com", "**", "[^1]", "不可见脚本", "```"]:
        assert hidden not in result.body_text


def test_nfc_and_ascii_case_keep_normalized_offsets_aligned():
    result = project("Cafe\u0301 OpenAI 中文𠀀")
    assert "Café OpenAI 中文𠀀" in result.body_text
    assert "café openai 中文𠀀" in result.normalized_text
    assert len(result.body_text) == len(result.normalized_text)
    assert normalize("ＡＢＣ Ä ß") == "ＡＢＣ Ä ß"


def test_word_analysis_keeps_frequencies_and_complete_offsets():
    result = project("贵州的债务问题。贵州债务。")
    assert result.token_data["贵州"] == [0, 8]
    assert result.token_data["债务"] == [3, 10]
    assert "的" not in result.token_data
    assert result.token_count == sum(map(len, result.token_data.values()))
    assert lexeme("贵州") in result.terms
    assert query_terms("贵州的债务问题现状及其成因") == [
        "贵州",
        "债务",
        "问题",
        "现状",
        "成因",
    ]


def test_word_offsets_are_not_truncated_by_tsvector_limits():
    text = "贵州 " * 17000
    positions = analyze(text)["贵州"]
    assert len(positions) == 17000
    assert positions[-1] == 16999 * 3


def test_separately_built_components_share_the_same_analyzer():
    root = Path(__file__).resolve().parents[2]
    for name in ("search_text.py", "search_words.txt"):
        assert (root / "indexer/src" / name).read_bytes() == (
            root / "agent/src" / name
        ).read_bytes()


def test_discovered_words_stay_whole():
    for word in ("阿尔忒弥斯", "中芯国际", "宁德时代", "钟薛高"):
        assert query_terms(word) == [word]
        # Search segmentation still indexes the dictionary words inside.
    assert {"宁德", "时代", "宁德时代"} <= set(analyze("宁德时代的电池"))
    # A discovered word never splits an existing longer reading.
    assert query_terms("火星舰队") == ["火星", "舰队"]


def test_plain_text_preserves_comparison_operators_and_character_entities():
    assert plain_text("甲 &amp; 乙，ΔP<H，条件 A > B") == "甲 & 乙，ΔP<H，条件 A > B"

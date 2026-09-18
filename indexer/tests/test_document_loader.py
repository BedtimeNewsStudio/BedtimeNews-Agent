"""Unit tests for 正文 extraction and document loading."""

import pytest
from src import document_loader
from src.document_loader import (
    clean_text,
    extract_body,
    load_document,
    uri_to_slug,
)

# Shaped like a real transcript: title, publication-date line, the 正文 section
# whose own sub-headings are also level 2, then the 附录 section.
TRANSCRIPT = """# 【睡前消息588】城投债四大网红，柳州第一

**发布日期** 2023-05-12 | [YouTube](https://example.invalid/x)

## 正文

## 四大网红的柳州

大家好，欢迎收看588期睡前消息。

## 地方债与政信债

柳州的情况不太好。

## 附录

### 事实订正

[^1]: 「柳州东投」→「柳州东城」：这是附录里的订正内容。

### 已核对

- 史实核实：这段也属于附录。
"""


class TestExtractBody:
    def test_keeps_body_prose(self):
        body = extract_body(TRANSCRIPT)
        assert "大家好，欢迎收看588期睡前消息。" in body
        assert "柳州的情况不太好。" in body

    def test_keeps_body_subheadings(self):
        # The body's own `##` sub-headings must survive: the chunker uses them
        # as section boundaries and as each chunk's heading.
        body = extract_body(TRANSCRIPT)
        assert "## 四大网红的柳州" in body
        assert "## 地方债与政信债" in body

    def test_drops_title_and_publication_date(self):
        body = extract_body(TRANSCRIPT)
        assert "【睡前消息588】" not in body
        assert "发布日期" not in body
        assert "2023-05-12" not in body

    def test_drops_appendix(self):
        body = extract_body(TRANSCRIPT)
        assert "附录" not in body
        assert "事实订正" not in body
        assert "已核对" not in body
        assert "柳州东投" not in body
        assert "这段也属于附录" not in body

    def test_drops_the_body_heading_itself(self):
        assert "## 正文" not in extract_body(TRANSCRIPT)

    def test_runs_to_end_when_there_is_no_appendix(self):
        body = extract_body("# T\n\n## 正文\n\n只有正文。\n")
        assert "只有正文。" in body

    def test_missing_body_section_yields_empty_string(self):
        # A malformed upstream document is skipped, not fatal: one bad file must
        # not fail a whole scheduled run.
        assert extract_body("# T\n\n## 附录\n\n只有附录。\n") == ""


class TestUriToSlug:
    @pytest.mark.parametrize(
        ("uri", "slug"),
        [
            ("ShuiQianXiaoXi/0501-0600/0588.md", "ShuiQianXiaoXi_0501-0600_0588"),
            ("ShuiQianXiaoXi/0001-0100/0013.5.md", "ShuiQianXiaoXi_0001-0100_0013.5"),
            ("ChanJingPoBiJi/misc/biz-001.md", "ChanJingPoBiJi_misc_biz-001"),
        ],
    )
    def test_strips_suffix_and_flattens_separators(self, uri, slug):
        assert uri_to_slug(uri) == slug


class TestLoadDocument:
    def test_loads_by_uri_with_body_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(document_loader, "CONTENTS_DIR", tmp_path)
        uri = "ShuiQianXiaoXi/0501-0600/0588.md"
        path = tmp_path / uri
        path.parent.mkdir(parents=True)
        path.write_text(TRANSCRIPT, encoding="utf-8")

        doc = load_document(uri)

        # doc_id is the URI verbatim, .md included.
        assert doc.doc_id == uri
        assert doc.slug == "ShuiQianXiaoXi_0501-0600_0588"
        assert "大家好" in doc.text
        assert "事实订正" not in doc.text
        assert "发布日期" not in doc.text


class TestCleanText:
    def test_strips_inline_formatting_tags_but_keeps_their_text(self):
        assert clean_text("强调<u>重点</u>内容") == "强调重点内容"
        assert clean_text("换行<br/>后面") == "换行后面"
        assert clean_text("<b>加粗</b>与<center>表名</center>") == "加粗与表名"

    def test_preserves_comparison_operators_in_prose(self):
        # A catch-all `<[^>]+>` reads everything between the `<` and the next
        # `>` as a tag and deletes the clause in between. This line is real
        # transcript text (ShuiQianXiaoXi/0301-0400/0321.md).
        line = "抗力为H，如果ΔP<H，目标生存；如果ΔP>H，目标摧毁"
        assert clean_text(line) == line

    def test_preserves_numeric_comparisons(self):
        assert clean_text("温度<100度，压力>2兆帕") == "温度<100度，压力>2兆帕"

    def test_collapses_runs_of_blank_lines(self):
        assert clean_text("一\n\n\n\n二") == "一\n\n二"


class TestIndexableFingerprint:
    def test_hashes_empty_body_consistently(self, tmp_path, monkeypatch):
        import hashlib

        monkeypatch.setattr(document_loader, "CONTENTS_DIR", tmp_path)
        uri = "malformed.md"
        raw = b"# title\n\n## Appendix only\n"
        (tmp_path / uri).write_bytes(raw)

        loaded = document_loader.load_indexable_source(uri)

        assert loaded.document.text == ""
        assert loaded.source_hash == hashlib.sha256(raw).hexdigest()
        assert loaded.body_hash == hashlib.sha256(b"").hexdigest()
        assert (
            loaded.body_normalization_version
            == document_loader.BODY_NORMALIZATION_VERSION
        )

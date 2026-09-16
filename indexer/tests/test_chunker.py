"""Unit tests for the document chunker (pure text-processing logic)."""

from src.chunker import (
    _extract_headings,
    _extract_last_words,
    _split_by_paragraphs,
    _split_into_sections,
    chunk_document,
    count_words,
)
from src.document_loader import uri_to_slug
from src.models import Document


class TestCountWords:
    def test_english_words(self):
        assert count_words("hello world foo") == 3

    def test_chinese_chars_count_individually(self):
        assert count_words("你好世界") == 4

    def test_mixed_chinese_and_english(self):
        # 2 Chinese chars + 1 English word ("Hello")
        assert count_words("Hello 世界") == 3

    def test_punctuation_and_digits_ignored(self):
        assert count_words("123 !!! ,.;") == 0

    def test_empty_string(self):
        assert count_words("") == 0


class TestExtractHeadings:
    def test_no_headings(self):
        assert _extract_headings("just some text\nmore text") == []

    def test_levels_and_text(self):
        headings = _extract_headings("# Title\n\nbody\n## Sub\n### Deep")
        levels = [(level, text) for _pos, level, text in headings]
        assert levels == [(1, "Title"), (2, "Sub"), (3, "Deep")]

    def test_positions_are_increasing(self):
        headings = _extract_headings("# A\n## B\n# C")
        positions = [pos for pos, _level, _text in headings]
        assert positions == sorted(positions)
        assert len(positions) == 3


class TestSplitIntoSections:
    def test_no_headings_returns_single_section(self):
        sections = _split_into_sections("plain text without headings")
        assert len(sections) == 1
        assert sections[0]["heading"] is None
        assert sections[0]["level"] == 0
        assert sections[0]["breadcrumb"] == []

    def test_breadcrumb_nesting(self):
        # A (h1) -> B (h2 under A) -> C (h1, resets stack)
        sections = _split_into_sections("# A\n## B\n# C")
        crumbs = [s["breadcrumb"] for s in sections]
        assert crumbs == [["A"], ["A", "B"], ["C"]]

    def test_section_content_spans_to_next_heading(self):
        sections = _split_into_sections("# A\nalpha\n# B\nbeta")
        assert "alpha" in sections[0]["content"]
        assert "beta" not in sections[0]["content"]


class TestSplitByParagraphs:
    def test_splits_on_blank_lines_and_strips(self):
        assert _split_by_paragraphs("para1\n\npara2") == ["para1", "para2"]

    def test_collapses_multiple_blank_lines_and_drops_empties(self):
        assert _split_by_paragraphs("a\n\n\n  \n\nb") == ["a", "b"]

    def test_single_paragraph(self):
        assert _split_by_paragraphs("only one") == ["only one"]


class TestExtractLastWords:
    def test_non_positive_count_returns_empty(self):
        assert _extract_last_words("anything", 0) == ""
        assert _extract_last_words("anything", -5) == ""

    def test_fewer_tokens_than_requested_returns_full_text(self):
        text = "你好世界"  # 4 tokens
        assert _extract_last_words(text, 10) == text

    def test_returns_suffix_for_long_text(self):
        text = "alpha beta gamma delta epsilon zeta eta theta"
        result = _extract_last_words(text, 2)
        # Result is a trailing slice of the original text, shorter than it.
        assert result and result in text
        assert len(result) < len(text)


def _doc(text: str, uri: str = "ShuiQianXiaoXi/0001-0100/0123.md") -> Document:
    slug = uri_to_slug(uri)
    return Document(id=f"doc_{slug}", file_path=uri, doc_id=uri, slug=slug, text=text)


class TestChunkDocument:
    def test_small_document_single_chunk(self):
        chunks = chunk_document(_doc("hello world foo bar"), min_chunk_size=1)
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.chunk_index == 0
        assert chunk.id == "ShuiQianXiaoXi_0001-0100_0123_chunk_000"
        assert chunk.doc_id == "ShuiQianXiaoXi/0001-0100/0123.md"
        assert chunk.word_count == count_words(chunk.text)

    def test_chunk_id_is_the_uri_slug_plus_index(self):
        chunks = chunk_document(
            _doc("hello world", uri="ShuiQianXiaoXi/0501-0600/0588.md"),
            min_chunk_size=1,
        )
        # The .md suffix is dropped and "/" becomes "_", so the chunk id stays a
        # plain identifier while doc_id keeps the URI verbatim.
        assert chunks[0].id == "ShuiQianXiaoXi_0501-0600_0588_chunk_000"
        assert chunks[0].doc_id == "ShuiQianXiaoXi/0501-0600/0588.md"

    def test_chunks_below_min_size_are_filtered_out(self):
        # 2 words, min is 5 -> dropped entirely
        chunks = chunk_document(_doc("你好"), min_chunk_size=5)
        assert chunks == []

    def test_large_document_splits_into_multiple_sequential_chunks(self):
        # 12 paragraphs of 3 words each = 36 words; small sizes force splitting.
        text = "\n\n".join("alpha beta gamma" for _ in range(12))
        chunks = chunk_document(
            _doc(text),
            target_chunk_size=10,
            max_chunk_size=20,
            min_chunk_size=1,
            overlap_size=2,
        )
        assert len(chunks) > 1
        # chunk_index is contiguous starting at 0
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
        # every retained chunk meets the minimum
        assert all(c.word_count >= 1 for c in chunks)

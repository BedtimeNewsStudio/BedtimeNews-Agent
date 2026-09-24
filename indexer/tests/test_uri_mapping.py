"""Unit tests for URI -> 标准化标题 resolution."""

import pytest
from src import uri_mapping
from src.uri_mapping import derive_title, load_uri_titles, resolve_title

# The real file's shape: a 3-column 例外表 and 2-column per-channel tables.
MAPPING_DOC = """# URI 映射（URI ↔ 标准化标题）

## 方案

通则覆盖 1783 篇；例外 29 篇。

## 例外表（通则之外的全部 29 篇）

| URI                                    | 标准化标题      | 备注           |
| -------------------------------------- | --------------- | -------------- |
| ShuiQianXiaoXi/misc/motiaonews1.md     | 末条新闻1       | 特辑/番外      |
| ChanJingPoBiJi/misc/biz-001.md         | 产经破壁机-001  | 负数期         |
| CanKaoXinXi/0301-0400/0396-1.md        | 参考信息396-1（下架版） | 下架原版 |

## 睡前消息（889 篇）

| URI                                | 标准化标题   |
| ---------------------------------- | ------------ |
| ShuiQianXiaoXi/0501-0600/0588.md   | 睡前消息588  |
| ShuiQianXiaoXi/0001-0100/0013.5.md | 睡前消息13.5 |
| ShuiQianXiaoXi/misc/motiaonews1.md | 末条新闻1    |
"""


class TestLoadUriTitles:
    @pytest.fixture
    def titles(self, tmp_path, monkeypatch):
        path = tmp_path / "URI映射.md"
        path.write_text(MAPPING_DOC, encoding="utf-8")
        monkeypatch.setattr(uri_mapping, "URI_MAPPING_FILE", path)
        return load_uri_titles()

    def test_parses_per_channel_tables(self, titles):
        assert titles["ShuiQianXiaoXi/0501-0600/0588.md"] == "睡前消息588"

    def test_parses_three_column_exception_table(self, titles):
        assert titles["ChanJingPoBiJi/misc/biz-001.md"] == "产经破壁机-001"
        assert titles["CanKaoXinXi/0301-0400/0396-1.md"] == "参考信息396-1（下架版）"

    def test_skips_header_and_separator_rows(self, titles):
        assert "URI" not in titles
        assert all(uri.endswith(".md") for uri in titles)

    def test_missing_file_degrades_to_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(uri_mapping, "URI_MAPPING_FILE", tmp_path / "absent.md")
        assert load_uri_titles() == {}


class TestDeriveTitle:
    @pytest.mark.parametrize(
        ("uri", "title"),
        [
            ("ShuiQianXiaoXi/0501-0600/0588.md", "睡前消息588"),
            ("ShuiQianXiaoXi/0001-0100/0001.md", "睡前消息1"),
            ("ShuiQianXiaoXi/0001-0100/0013.5.md", "睡前消息13.5"),
            ("CanKaoXinXi/0401-0500/0439.5.md", "参考信息439.5"),
            ("GaoJian/0001-0100/0012.md", "高见12"),
            ("JiangDianHeiHua/0001-0100/0005.md", "讲点黑话5"),
            ("ChanJingPoBiJi/0001-0100/0000.md", "产经破壁机0"),
        ],
    )
    def test_general_rule(self, uri, title):
        assert derive_title(uri) == title

    @pytest.mark.parametrize(
        "uri",
        [
            # The documented exceptions: no rule derives these.
            "ShuiQianXiaoXi/misc/motiaonews1.md",
            "ShuiQianXiaoXi/misc/speech2019.md",
            "ChanJingPoBiJi/misc/biz-001.md",
            "CanKaoXinXi/0301-0400/0396-1.md",
            "ChanJingPoBiJi/0101-0200/0119-2.md",
            # Not a transcript path at all.
            "ShuiQianXiaoXi/INDEX.md",
            "Unknown/0001-0100/0001.md",
        ],
    )
    def test_returns_none_for_exceptions(self, uri):
        assert derive_title(uri) is None


class TestResolveTitle:
    def test_prefers_the_upstream_table(self):
        # The table wins even where the general rule would also produce a title,
        # which is what makes 0119-2 and friends come out right.
        mapped = {"ShuiQianXiaoXi/0501-0600/0588.md": "睡前消息588"}
        assert (
            resolve_title("ShuiQianXiaoXi/0501-0600/0588.md", mapped) == "睡前消息588"
        )

    def test_exception_resolves_only_via_the_table(self):
        mapped = {"ShuiQianXiaoXi/misc/motiaonews1.md": "末条新闻1"}
        assert (
            resolve_title("ShuiQianXiaoXi/misc/motiaonews1.md", mapped) == "末条新闻1"
        )

    def test_falls_back_to_the_general_rule(self):
        assert resolve_title("GaoJian/0001-0100/0012.md", {}) == "高见12"

    def test_falls_back_to_the_uri_when_nothing_else_works(self):
        assert (
            resolve_title("ShuiQianXiaoXi/misc/motiaonews1.md", {})
            == "ShuiQianXiaoXi/misc/motiaonews1.md"
        )

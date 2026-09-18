"""Unit tests for config-based file filtering."""

import pathlib

import pytest
from src import file_scanner
from src.file_scanner import _should_include_file


def _make_file(base, rel_path, size):
    path = base / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return rel_path


class TestShouldIncludeFile:
    def test_included_when_matches_include_and_size_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(file_scanner, "CONTENTS_DIR", tmp_path)
        _make_file(tmp_path, "episode.md", size=100)
        config = {"include": ["*.md"]}
        assert _should_include_file("episode.md", config) is True

    def test_excluded_when_not_matching_include(self, tmp_path, monkeypatch):
        monkeypatch.setattr(file_scanner, "CONTENTS_DIR", tmp_path)
        config = {"include": ["*.txt"]}  # .md does not match -> excluded early
        assert _should_include_file("episode.md", config) is False

    def test_excluded_when_matches_exclude(self, tmp_path, monkeypatch):
        monkeypatch.setattr(file_scanner, "CONTENTS_DIR", tmp_path)
        config = {"exclude": ["draft_*.md"]}
        assert _should_include_file("draft_episode.md", config) is False

    def test_rejected_when_below_min_size(self, tmp_path, monkeypatch):
        monkeypatch.setattr(file_scanner, "CONTENTS_DIR", tmp_path)
        _make_file(tmp_path, "tiny.md", size=5)
        config = {"validation": {"min_file_size": 100}}
        assert _should_include_file("tiny.md", config) is False

    def test_rejected_when_above_max_size(self, tmp_path, monkeypatch):
        monkeypatch.setattr(file_scanner, "CONTENTS_DIR", tmp_path)
        _make_file(tmp_path, "huge.md", size=200)
        config = {"validation": {"max_file_size": 100}}
        assert _should_include_file("huge.md", config) is False

    def test_empty_config_includes_any_sized_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(file_scanner, "CONTENTS_DIR", tmp_path)
        _make_file(tmp_path, "anything.md", size=50)
        assert _should_include_file("anything.md", {}) is True


class TestShippedConfigPatterns:
    """The real index_config.yml against the real upstream layout."""

    CONFIG = {
        "include": [
            "ShuiQianXiaoXi/*/*.md",
            "CanKaoXinXi/*/*.md",
            "GaoJian/*/*.md",
            "JiangDianHeiHua/*/*.md",
            "ChanJingPoBiJi/*/*.md",
        ],
        "exclude": ["*/INDEX.md"],
    }

    def test_shipped_config_matches_the_file_on_disk(self):
        import yaml
        from src.paths import INDEX_CONFIG_FILE

        shipped = yaml.safe_load(
            (pathlib.Path(__file__).parents[1] / INDEX_CONFIG_FILE.name).read_text(
                encoding="utf-8"
            )
        )
        assert shipped["include"] == self.CONFIG["include"]
        assert shipped["exclude"] == self.CONFIG["exclude"]

    @pytest.mark.parametrize(
        "uri",
        [
            "ShuiQianXiaoXi/0501-0600/0588.md",
            "ShuiQianXiaoXi/0001-0100/0013.5.md",
            "ShuiQianXiaoXi/misc/motiaonews1.md",
            "CanKaoXinXi/0301-0400/0396-1.md",
            "ChanJingPoBiJi/misc/biz-001.md",
            "GaoJian/0001-0100/0001.md",
            "JiangDianHeiHua/0001-0100/0001.md",
        ],
    )
    def test_transcripts_are_included(self, uri, tmp_path, monkeypatch):
        monkeypatch.setattr(file_scanner, "CONTENTS_DIR", tmp_path)
        _make_file(tmp_path, uri, size=500)
        assert _should_include_file(uri, self.CONFIG) is True

    @pytest.mark.parametrize(
        "uri",
        [
            "ShuiQianXiaoXi/INDEX.md",
            "CanKaoXinXi/INDEX.md",
            # Repo-root docs live outside contents/, but make sure a stray
            # top-level file could never be picked up either.
            "README.md",
        ],
    )
    def test_navigation_and_stray_files_are_excluded(self, uri, tmp_path, monkeypatch):
        monkeypatch.setattr(file_scanner, "CONTENTS_DIR", tmp_path)
        _make_file(tmp_path, uri, size=500)
        assert _should_include_file(uri, self.CONFIG) is False


def test_scan_fails_when_a_required_sample_uri_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(file_scanner, "CONTENTS_DIR", tmp_path)
    monkeypatch.setattr(
        file_scanner,
        "_load_config",
        lambda: {"required_uris": ["missing.md"], "include": ["*.md"]},
    )

    with pytest.raises(RuntimeError, match="missing.md"):
        file_scanner.scan_files()

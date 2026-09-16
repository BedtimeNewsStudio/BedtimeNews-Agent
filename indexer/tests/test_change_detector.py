"""Unit tests for change detection logic."""

import hashlib

from src import change_detector
from src.change_detector import calculate_file_hash, detect_changes, get_doc_id


class TestGetDocId:
    """The doc_id is the URI verbatim — including .md — so it stays byte-for-byte
    identical to the keys of the upstream URI映射.md."""

    def test_keeps_md_suffix(self):
        assert (
            get_doc_id("ShuiQianXiaoXi/0501-0600/0588.md")
            == "ShuiQianXiaoXi/0501-0600/0588.md"
        )

    def test_keeps_fractional_episode_suffix(self):
        assert (
            get_doc_id("ShuiQianXiaoXi/0001-0100/0013.5.md")
            == "ShuiQianXiaoXi/0001-0100/0013.5.md"
        )

    def test_keeps_misc_special_path(self):
        assert (
            get_doc_id("ChanJingPoBiJi/misc/biz-001.md")
            == "ChanJingPoBiJi/misc/biz-001.md"
        )


class TestDetectChanges:
    def test_classifies_added_modified_deleted_unchanged(self, monkeypatch):
        indexed = ["a.md", "b.md", "c.md", "gone.md"]
        current = {"a.md", "b.md", "c.md", "new.md"}

        # a.md unchanged (hash matches), b.md modified (hash differs),
        # c.md indexed but has no history -> treated as added.
        history = {
            "a.md": {"content_hash": "HASH_A"},
            "b.md": {"content_hash": "OLD_B"},
            "c.md": None,
        }
        hashes = {"a.md": "HASH_A", "b.md": "NEW_B"}

        monkeypatch.setattr(change_detector, "get_indexed_files", lambda: indexed)
        monkeypatch.setattr(
            change_detector, "get_indexing_history", lambda f: history.get(f)
        )
        monkeypatch.setattr(change_detector, "calculate_file_hash", lambda f: hashes[f])

        added, modified, deleted = detect_changes(current)

        assert added == {"new.md", "c.md"}
        assert modified == {"b.md"}
        assert deleted == {"gone.md"}

    def test_all_new_when_nothing_indexed(self, monkeypatch):
        monkeypatch.setattr(change_detector, "get_indexed_files", lambda: [])
        monkeypatch.setattr(change_detector, "get_indexing_history", lambda f: None)
        added, modified, deleted = detect_changes({"x.md", "y.md"})
        assert added == {"x.md", "y.md"}
        assert modified == set()
        assert deleted == set()


class TestCalculateFileHash:
    def test_matches_sha256_of_content(self, tmp_path, monkeypatch):
        monkeypatch.setattr(change_detector, "CONTENTS_DIR", tmp_path)
        content = b"hello bedtime news"
        (tmp_path / "doc.md").write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert calculate_file_hash("doc.md") == expected

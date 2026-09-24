"""Unit tests for source/body-aware change detection."""

import hashlib

from src import change_detector, document_loader
from src.change_detector import detect_changes
from src.document_loader import BODY_NORMALIZATION_VERSION, clean_text, extract_body


def _transcript(body="正文内容", *, title="标题", date="2026-01-01", appendix="订正"):
    return (
        f"# {title}\n\n**发布日期** {date}\n\n## 正文\n\n{body}"
        f"\n\n## 附录\n\n{appendix}\n"
    )


def _write_source(root, uri, text):
    path = root / uri
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    raw = path.read_bytes()
    normalized = clean_text(extract_body(text, uri))
    return {
        "source_hash": hashlib.sha256(raw).hexdigest(),
        "body_hash": hashlib.sha256(normalized.encode()).hexdigest(),
        "body_normalization_version": BODY_NORMALIZATION_VERSION,
    }


def _set_contents_dir(monkeypatch, root):
    monkeypatch.setattr(change_detector, "CONTENTS_DIR", root)
    monkeypatch.setattr(document_loader, "CONTENTS_DIR", root)


class TestDetectChanges:
    def test_classifies_all_change_kinds(self, tmp_path, monkeypatch):
        _set_contents_dir(monkeypatch, tmp_path)
        same = _write_source(tmp_path, "same.md", _transcript())
        source_only_old = _write_source(
            tmp_path, "source-only.md", _transcript(title="新标题")
        )
        source_only_body = source_only_old["body_hash"]
        _write_source(tmp_path, "body.md", _transcript(body="新的正文内容"))
        legacy = _write_source(tmp_path, "legacy.md", _transcript())
        _write_source(tmp_path, "added.md", _transcript())

        histories = {
            "same.md": same,
            "source-only.md": {
                "source_hash": "old-source",
                "body_hash": source_only_body,
                "body_normalization_version": BODY_NORMALIZATION_VERSION,
            },
            "body.md": {
                "source_hash": "old-source",
                "body_hash": "old-body",
                "body_normalization_version": BODY_NORMALIZATION_VERSION,
            },
            "legacy.md": {
                "source_hash": legacy["source_hash"],
                "body_hash": None,
                "body_normalization_version": None,
            },
            "gone.md": {
                "source_hash": "gone",
                "body_hash": "gone",
                "body_normalization_version": BODY_NORMALIZATION_VERSION,
            },
        }
        monkeypatch.setattr(
            change_detector, "get_indexing_histories", lambda: histories
        )

        changes = detect_changes(
            {"same.md", "source-only.md", "body.md", "legacy.md", "added.md"}
        )

        assert changes.added == {"added.md"}
        assert changes.body_modified == {"body.md"}
        assert changes.source_only == {"source-only.md"}
        assert changes.legacy_requires_reindex == {"legacy.md"}
        assert changes.deleted == {"gone.md"}
        assert "same.md" not in changes.loaded_sources
        assert set(changes.loaded_sources) == {
            "added.md",
            "source-only.md",
            "body.md",
            "legacy.md",
        }

    def test_normalization_version_change_forces_reindex(self, tmp_path, monkeypatch):
        _set_contents_dir(monkeypatch, tmp_path)
        current = _write_source(tmp_path, "doc.md", _transcript())
        history = current | {
            "body_normalization_version": BODY_NORMALIZATION_VERSION - 1
        }
        monkeypatch.setattr(
            change_detector, "get_indexing_histories", lambda: {"doc.md": history}
        )

        changes = detect_changes({"doc.md"})

        assert changes.body_modified == {"doc.md"}


class TestHashes:
    def test_source_hash_covers_raw_bytes_and_body_hash_matches_document_text(
        self, tmp_path, monkeypatch
    ):
        _set_contents_dir(monkeypatch, tmp_path)
        uri = "doc.md"
        text = _transcript(body="一\n\n\n<strong>二</strong>")
        _write_source(tmp_path, uri, text)

        source = document_loader.load_indexable_source(uri)

        assert source.source_hash == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert (
            source.body_hash
            == hashlib.sha256(source.document.text.encode("utf-8")).hexdigest()
        )

    def test_non_body_edits_change_source_but_not_body(self, tmp_path, monkeypatch):
        _set_contents_dir(monkeypatch, tmp_path)
        uri = "doc.md"
        first = _write_source(tmp_path, uri, _transcript())
        second = _write_source(
            tmp_path,
            uri,
            _transcript(title="改标题", date="2026-02-02", appendix="新订正"),
        )

        assert first["source_hash"] != second["source_hash"]
        assert first["body_hash"] == second["body_hash"]

    def test_removed_inline_formatting_does_not_change_body_hash(
        self, tmp_path, monkeypatch
    ):
        _set_contents_dir(monkeypatch, tmp_path)
        uri = "doc.md"
        first = _write_source(tmp_path, uri, _transcript(body="<strong>正文</strong>"))
        second = _write_source(tmp_path, uri, _transcript(body="<u>正文</u>"))
        assert first["source_hash"] != second["source_hash"]
        assert first["body_hash"] == second["body_hash"]

    def test_body_heading_or_prose_edit_changes_body_hash(self, tmp_path, monkeypatch):
        _set_contents_dir(monkeypatch, tmp_path)
        uri = "doc.md"
        first = _write_source(tmp_path, uri, _transcript(body="## 小节\n\n正文"))
        second = _write_source(tmp_path, uri, _transcript(body="## 新小节\n\n正文"))
        assert first["body_hash"] != second["body_hash"]

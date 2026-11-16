"""The indexing cache retains documents, not the separate attachment archive."""

import subprocess

import pytest
from src import git_sync


def git(cwd, *args):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


@pytest.mark.parametrize("existing_full_clone", [False, True])
def test_sync_keeps_documents_current_without_attachment_checkout(
    tmp_path, monkeypatch, existing_full_clone
):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    (source / "contents").mkdir()
    (source / "contents" / "episode.md").write_text("original")
    (source / "URI映射.md").write_text("metadata")
    (source / "snapshots").mkdir()
    (source / "snapshots" / "report.pdf").write_bytes(b"%PDF-1.7\n" + b"\0" * 4096)
    git(source, "add", ".")
    git(source, "commit", "-m", "initial")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "--bare", str(source), str(remote))
    git(remote, "config", "uploadpack.allowFilter", "true")
    cache = tmp_path / "cache"
    if existing_full_clone:
        git(tmp_path, "clone", remote.as_uri(), str(cache))
        assert (cache / "snapshots" / "report.pdf").exists()
    (source / "contents" / "episode.md").write_text("updated")
    (source / "snapshots" / "another.pdf").write_bytes(b"%PDF-1.7\n" + b"\0" * 8192)
    git(source, "add", ".")
    git(source, "commit", "-m", "update")
    git(source, "push", str(remote), "main")
    monkeypatch.setattr(git_sync, "BEDTIMENEWS_TRANSCRIPTS_DIR", cache)
    monkeypatch.setattr(git_sync, "BEDTIMENEWS_TRANSCRIPTS_REPO_URL", remote.as_uri())

    git_sync.sync_repository()

    assert (cache / "contents" / "episode.md").read_text() == "updated"
    assert (cache / "URI映射.md").read_text() == "metadata"
    assert not (cache / "snapshots").exists()
    assert git(cache, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
    assert git(cache, "config", "remote.origin.partialclonefilter") == "blob:none"

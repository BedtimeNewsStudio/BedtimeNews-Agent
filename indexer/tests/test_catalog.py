"""Retention rules (design 6.8), run lock and snapshot identity, no database."""

from datetime import UTC, datetime, timedelta

import pytest
from src import catalog, snapshot_schema
from src.catalog import SnapshotInfo, gc_candidates
from src.run_lock import RunLockBusy, run_lock

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
CURRENT = ("fp-current", "model@2560")
OTHER = ("fp-old", "model@2560")


def snap(
    sid,
    minutes_ago,
    lineage=CURRENT,
    *,
    status="published",
    pinned=False,
    retired_minutes_ago=None,
):
    return SnapshotInfo(
        snapshot_id=sid,
        schema_name=f"rag_{sid}",
        format_version=1,
        pipeline_fingerprint=lineage[0],
        embedding_space=lineage[1],
        embedding_model="model",
        embedding_dim=2560,
        status=status,
        pinned=pinned,
        published_at=NOW - timedelta(minutes=minutes_ago),
        retired_at=(
            NOW - timedelta(minutes=retired_minutes_ago)
            if retired_minutes_ago is not None
            else None
        ),
        source_commit=None,
        base_snapshot_id=None,
        stats=None,
    )


def doomed(snapshots):
    return [s.snapshot_id for s, _ in gc_candidates(snapshots, CURRENT, NOW)]


def test_current_lineage_keeps_latest_two():
    assert doomed([snap("c", 20), snap("b", 40), snap("a", 60)]) == ["a"]


def test_grace_period_protects_recently_superseded():
    # "a" was superseded by "b" only 5 minutes ago (b published 5 min ago)
    # and fell out of the keep-2 window when "c" arrived 1 minute ago.
    assert doomed([snap("c", 1), snap("b", 5), snap("a", 60)]) == []


def test_pinned_snapshots_are_never_collected():
    assert doomed([snap("c", 20), snap("b", 40), snap("a", 60, pinned=True)]) == []


def test_retired_kept_for_24_hours():
    assert (
        doomed(
            [snap("b", 100), snap("a", 200, status="retired", retired_minutes_ago=60)]
        )
        == []
    )
    assert doomed(
        [
            snap("b", 100),
            snap("a", 3000, status="retired", retired_minutes_ago=24 * 60 + 1),
        ]
    ) == ["a"]


def test_other_lineage_latest_kept_seven_days_after_superseded():
    recent = [snap("new", 60 * 24 * 6), snap("legacy", 60 * 24 * 7, OTHER)]
    assert doomed(recent) == []
    old = [snap("new", 60 * 24 * 8), snap("legacy", 60 * 24 * 9, OTHER)]
    assert doomed(old) == ["legacy"]


def test_other_lineage_kept_while_current_has_nothing_newer():
    assert doomed([snap("legacy", 60 * 24 * 30, OTHER)]) == []


def test_other_lineage_older_members_go_after_grace():
    assert doomed([snap("o2", 30, OTHER), snap("o1", 60, OTHER)]) == ["o1"]


def test_only_snapshot_schemas_are_droppable():
    assert catalog._is_snapshot_schema("rag")
    assert catalog._is_snapshot_schema("rag_s20261007t091512z_a1b2c3d")
    assert not catalog._is_snapshot_schema("rag_state")
    assert not catalog._is_snapshot_schema("rag_meta")
    assert not catalog._is_snapshot_schema("public")


def test_snapshot_id_shape():
    sid = snapshot_schema.new_snapshot_id(
        "A1B2C3D4E5", datetime(2026, 10, 7, 9, 15, 12, tzinfo=UTC)
    )
    assert sid == "s20261007t091512z_a1b2c3d"
    assert len(snapshot_schema.schema_for(sid)) < 63


def test_fingerprint_tracks_every_version():
    base = snapshot_schema.pipeline_fingerprint(1, 3, 1)
    assert snapshot_schema.pipeline_fingerprint(1, 3, 1) == base
    assert snapshot_schema.pipeline_fingerprint(2, 3, 1) != base
    assert snapshot_schema.pipeline_fingerprint(1, 4, 1) != base
    assert snapshot_schema.pipeline_fingerprint(1, 3, 2) != base


def test_embedding_space_override(monkeypatch):
    monkeypatch.setattr(snapshot_schema.settings.embedding, "model", "m")
    monkeypatch.setattr(snapshot_schema.settings, "embedding_dim", 1024)
    monkeypatch.setattr(snapshot_schema.settings.embedding, "space_id", "")
    assert snapshot_schema.embedding_space() == "m@1024"
    monkeypatch.setattr(snapshot_schema.settings.embedding, "space_id", "m@vendor-b")
    assert snapshot_schema.embedding_space() == "m@vendor-b"


def test_run_lock_is_exclusive_and_non_blocking(tmp_path):
    lock = tmp_path / ".indexer.lock"
    with run_lock(lock):
        with pytest.raises(RunLockBusy):
            with run_lock(lock):
                pass
    with run_lock(lock):  # released again
        pass

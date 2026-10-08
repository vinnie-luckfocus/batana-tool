"""SessionStore 测试：索引落盘、三段式标记、修剪、删除、崩溃重建。"""

from __future__ import annotations

import json

import pytest

from app.session import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_REVIEW,
    SessionStore,
    new_session_id,
)


def make_clip_dir(store: SessionStore, session_id: str, frame_count: int = 30) -> str:
    d = store.sessions_dir / session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "capture_meta.json").write_text(
        json.dumps({"frame_count": frame_count, "fps": 120.0}), encoding="utf-8"
    )
    (d / "left.mkv").write_bytes(b"fake")
    return f"sessions/{session_id}"


def test_session_id_format():
    import re

    sid = new_session_id()
    assert re.match(r"^sess_[0-9A-HJKMNP-TV-Z]{26}$", sid)
    assert new_session_id() != new_session_id()


def test_add_clip_appends_and_persists(tmp_path):
    store = SessionStore(tmp_path)
    sid = new_session_id()
    clip_dir = make_clip_dir(store, sid)
    record = store.add_clip(sid, clip_dir, frame_count=30, fps=120.0, trigger_idx=40)
    assert record["seq"] == 1
    assert record["status"] == STATUS_REVIEW
    assert record["trim"] == {"start_frame": 0, "end_frame": 29}
    # index.json 已落盘且可被新实例读回（崩溃恢复）
    store2 = SessionStore(tmp_path)
    assert store2.get(sid)["session_id"] == sid
    assert store2.counts()["total"] == 1


def test_mark_three_way_status(tmp_path):
    store = SessionStore(tmp_path)
    sid = new_session_id()
    store.add_clip(sid, make_clip_dir(store, sid), 30, 120.0)
    store.mark(sid, STATUS_PASS)
    assert store.get(sid)["status"] == STATUS_PASS
    store.mark(sid, STATUS_FAIL)
    assert store.list(status=STATUS_FAIL)[0]["session_id"] == sid
    with pytest.raises(ValueError, match="非法标记"):
        store.mark(sid, "随便")


def test_set_trim_bounds(tmp_path):
    store = SessionStore(tmp_path)
    sid = new_session_id()
    store.add_clip(sid, make_clip_dir(store, sid, frame_count=100), 100, 120.0)
    store.set_trim(sid, 10, 89)
    assert store.get(sid)["trim"] == {"start_frame": 10, "end_frame": 89}
    with pytest.raises(ValueError):
        store.set_trim(sid, 50, 40)
    with pytest.raises(ValueError):
        store.set_trim(sid, 0, 100)


def test_delete_removes_record_and_files(tmp_path):
    store = SessionStore(tmp_path)
    sid = new_session_id()
    make_clip_dir(store, sid)
    store.add_clip(sid, f"sessions/{sid}", 30, 120.0)
    store.delete(sid)
    assert store.counts()["total"] == 0
    assert not (store.sessions_dir / sid).exists()
    with pytest.raises(KeyError):
        store.get(sid)


def test_counts(tmp_path):
    store = SessionStore(tmp_path)
    ids = [new_session_id() for _ in range(3)]
    for sid in ids:
        store.add_clip(sid, make_clip_dir(store, sid), 30, 120.0)
    store.mark(ids[0], STATUS_PASS)
    store.mark(ids[1], STATUS_FAIL)
    c = store.counts()
    assert c == {"total": 3, STATUS_PASS: 1, STATUS_REVIEW: 1, STATUS_FAIL: 1}


def test_rebuild_recovers_missing_index(tmp_path):
    store = SessionStore(tmp_path)
    ids = [new_session_id() for _ in range(2)]
    for sid in ids:
        store.add_clip(sid, make_clip_dir(store, sid, frame_count=42), 42, 120.0)
    # 模拟崩溃：索引丢失，素材目录还在
    (tmp_path / "index.json").unlink()
    store2 = SessionStore(tmp_path)
    assert store2.counts()["total"] == 0
    added = store2.rebuild()
    assert added == 2
    assert store2.counts()["total"] == 2
    rec = store2.get(ids[0])
    assert rec["frame_count"] == 42
    assert rec["status"] == STATUS_REVIEW
    assert rec["rebuilt"] is True
    # 重复 rebuild 不重复登记
    assert store2.rebuild() == 0

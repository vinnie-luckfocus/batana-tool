"""素材索引与审核管理（PRD F6/F10）：index.json 每段追加落盘、崩溃可重建。"""

from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 三段式审核标记（PRD F10）
STATUS_PASS = "合格"
STATUS_FAIL = "不合格"
STATUS_REVIEW = "待复核"
STATUSES = (STATUS_PASS, STATUS_FAIL, STATUS_REVIEW)


class SessionStore:
    """采集会话素材库。

    目录布局：
        <root>/index.json          # 素材索引（每段保存即原子更新，崩溃后可重建）
        <root>/sessions/<session_id>/  # 每段素材目录（视频/时间戳/session.json 等）
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.json"
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        if self._index_path.is_file():
            self._records = json.loads(self._index_path.read_text(encoding="utf-8"))
        else:
            self._flush()

    # ---- 写入 ----

    def add_clip(
        self,
        session_id: str,
        clip_dir: str | Path,
        frame_count: int,
        fps: float,
        trigger_idx: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """登记一段新素材并立即落盘。序号 = 当日已登记段数 + 1。"""
        with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            seq = sum(1 for r in self._records if r.get("date") == today) + 1
            record: dict[str, Any] = {
                "session_id": session_id,
                "date": today,
                "seq": seq,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "clip_dir": str(Path(clip_dir).relative_to(self.root)
                              if Path(clip_dir).is_absolute() and Path(clip_dir).is_relative_to(self.root)
                              else clip_dir),
                "frame_count": int(frame_count),
                "fps": float(fps),
                "trigger_idx": trigger_idx,
                "status": STATUS_REVIEW,
                "trim": {"start_frame": 0, "end_frame": int(frame_count) - 1},
            }
            if extra:
                record.update(extra)
            self._records.append(record)
            self._flush()
            return record

    def mark(self, session_id: str, status: str) -> None:
        """三段式标记：合格 / 不合格 / 待复核。"""
        if status not in STATUSES:
            raise ValueError(f"非法标记 {status!r}，取值 {STATUSES}")
        with self._lock:
            self._find(session_id)["status"] = status
            self._flush()

    def set_trim(self, session_id: str, start_frame: int, end_frame: int) -> None:
        """设置片段修剪起止帧（闭区间）。"""
        with self._lock:
            record = self._find(session_id)
            if not (0 <= start_frame <= end_frame < record["frame_count"]):
                raise ValueError(
                    f"修剪区间非法: [{start_frame}, {end_frame}]，段长 {record['frame_count']}"
                )
            record["trim"] = {"start_frame": int(start_frame), "end_frame": int(end_frame)}
            self._flush()

    def delete(self, session_id: str, remove_files: bool = True) -> None:
        """删除素材（重拍场景）：移除索引记录，默认连同素材目录一并删除。"""
        with self._lock:
            record = self._find(session_id)
            self._records.remove(record)
            if remove_files:
                shutil.rmtree(self.root / record["clip_dir"], ignore_errors=True)
            self._flush()

    # ---- 查询 ----

    def get(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._find(session_id))

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._records if status is None or r["status"] == status]

    def counts(self) -> dict[str, int]:
        """实时计数：已采集 / 合格 / 待复核 / 不合格。"""
        with self._lock:
            return {
                "total": len(self._records),
                STATUS_PASS: sum(1 for r in self._records if r["status"] == STATUS_PASS),
                STATUS_REVIEW: sum(1 for r in self._records if r["status"] == STATUS_REVIEW),
                STATUS_FAIL: sum(1 for r in self._records if r["status"] == STATUS_FAIL),
            }

    # ---- 崩溃恢复 ----

    def rebuild(self) -> int:
        """扫描 sessions/ 目录重建索引：保留已有记录，补登索引缺失的素材目录。

        返回补登段数。补登记录标记为待复核，帧数从 capture_meta.json 读取（缺失记 0）。
        """
        with self._lock:
            known = {r["session_id"] for r in self._records}
            added = 0
            for d in sorted(self.sessions_dir.iterdir()):
                if not d.is_dir() or d.name in known:
                    continue
                meta_path = d / "capture_meta.json"
                frame_count = 0
                fps = 0.0
                if meta_path.is_file():
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    frame_count = int(meta.get("frame_count", 0))
                    fps = float(meta.get("fps", 0.0))
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                seq = sum(1 for r in self._records if r.get("date") == today) + 1
                self._records.append({
                    "session_id": d.name,
                    "date": today,
                    "seq": seq,
                    "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "clip_dir": f"sessions/{d.name}",
                    "frame_count": frame_count,
                    "fps": fps,
                    "trigger_idx": None,
                    "status": STATUS_REVIEW,
                    "trim": {"start_frame": 0, "end_frame": max(0, frame_count - 1)},
                    "rebuilt": True,
                })
                added += 1
            if added:
                self._flush()
            return added

    # ---- 内部 ----

    def _find(self, session_id: str) -> dict[str, Any]:
        for r in self._records:
            if r["session_id"] == session_id:
                return r
        raise KeyError(f"素材不存在: {session_id}")

    def _flush(self) -> None:
        """原子落盘：先写临时文件再 rename，避免崩溃留下半截 JSON。"""
        tmp = self._index_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self._index_path)

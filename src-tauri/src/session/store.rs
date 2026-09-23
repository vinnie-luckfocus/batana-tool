//! 素材索引与审核管理（PRD F6/F10）：index.json 每段追加落盘、崩溃可重建。
//! 语义对齐 Python 版 app/session/store.py。
//!
//! 目录布局：
//!     <root>/index.json               # 素材索引（每段保存即原子更新）
//!     <root>/sessions/<session_id>/   # 每段素材目录（视频/时间戳/session.json 等）

use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use super::export::{utc_now_iso, utc_today};

// 三段式审核标记（PRD F10）
pub const STATUS_PASS: &str = "合格";
pub const STATUS_FAIL: &str = "不合格";
pub const STATUS_REVIEW: &str = "待复核";
pub const STATUSES: [&str; 3] = [STATUS_PASS, STATUS_FAIL, STATUS_REVIEW];

pub struct SessionStore {
    pub root: PathBuf,
    pub sessions_dir: PathBuf,
    index_path: PathBuf,
    records: Vec<Value>,
}

impl SessionStore {
    pub fn new(root: impl AsRef<Path>) -> Result<Self, String> {
        let root = root.as_ref().to_path_buf();
        let sessions_dir = root.join("sessions");
        std::fs::create_dir_all(&sessions_dir).map_err(|e| format!("创建素材库目录失败: {e}"))?;
        let index_path = root.join("index.json");
        let records = if index_path.is_file() {
            let text = std::fs::read_to_string(&index_path)
                .map_err(|e| format!("读 index.json 失败: {e}"))?;
            serde_json::from_str(&text).map_err(|e| format!("解析 index.json 失败: {e}"))?
        } else {
            Vec::new()
        };
        let store = Self {
            root,
            sessions_dir,
            index_path,
            records,
        };
        if !store.index_path.is_file() {
            store.flush()?;
        }
        Ok(store)
    }

    // ---- 写入 ----

    /// 登记一段新素材并立即落盘。序号 = 当日已登记段数 + 1。
    pub fn add_clip(
        &mut self,
        session_id: &str,
        clip_dir: &Path,
        frame_count: u64,
        fps: f64,
        trigger_idx: Option<u64>,
        extra: Option<Value>,
    ) -> Result<Value, String> {
        let today = utc_today();
        let seq = self
            .records
            .iter()
            .filter(|r| r.get("date").and_then(Value::as_str) == Some(&today))
            .count() as u64
            + 1;
        let clip_dir_str = match clip_dir.strip_prefix(&self.root) {
            Ok(rel) => rel.to_string_lossy().into_owned(),
            Err(_) => clip_dir.to_string_lossy().into_owned(),
        };
        let mut record = json!({
            "session_id": session_id,
            "date": today,
            "seq": seq,
            "created_at": utc_now_iso(),
            "clip_dir": clip_dir_str,
            "frame_count": frame_count,
            "fps": fps,
            "trigger_idx": trigger_idx,
            "status": STATUS_REVIEW,
            "trim": {"start_frame": 0, "end_frame": frame_count.saturating_sub(1)},
        });
        if let Some(Value::Object(extra)) = extra {
            for (k, v) in extra {
                record[k] = v;
            }
        }
        self.records.push(record.clone());
        self.flush()?;
        Ok(record)
    }

    /// 三段式标记：合格 / 不合格 / 待复核。
    pub fn mark(&mut self, session_id: &str, status: &str) -> Result<(), String> {
        if !STATUSES.contains(&status) {
            return Err(format!("非法标记 {status:?}，取值 {STATUSES:?}"));
        }
        self.find_mut(session_id)?["status"] = json!(status);
        self.flush()
    }

    /// 设置片段修剪起止帧（闭区间）。
    pub fn set_trim(&mut self, session_id: &str, start_frame: u64, end_frame: u64) -> Result<(), String> {
        let record = self.find_mut(session_id)?;
        let frame_count = record["frame_count"].as_u64().unwrap_or(0);
        if !(start_frame <= end_frame && end_frame < frame_count) {
            return Err(format!(
                "修剪区间非法: [{start_frame}, {end_frame}]，段长 {frame_count}"
            ));
        }
        record["trim"] = json!({"start_frame": start_frame, "end_frame": end_frame});
        self.flush()
    }

    /// 删除素材（重拍场景）：移除索引记录，默认连同素材目录一并删除。
    pub fn delete(&mut self, session_id: &str, remove_files: bool) -> Result<(), String> {
        let idx = self.index_of(session_id)?;
        let record = self.records.remove(idx);
        if remove_files {
            let dir = self.root.join(record["clip_dir"].as_str().unwrap_or(""));
            let _ = std::fs::remove_dir_all(dir);
        }
        self.flush()
    }

    // ---- 查询 ----

    pub fn get(&self, session_id: &str) -> Result<Value, String> {
        self.records
            .iter()
            .find(|r| r["session_id"].as_str() == Some(session_id))
            .cloned()
            .ok_or_else(|| format!("素材不存在: {session_id}"))
    }

    pub fn list(&self, status: Option<&str>) -> Vec<Value> {
        self.records
            .iter()
            .filter(|r| status.is_none_or(|s| r["status"].as_str() == Some(s)))
            .cloned()
            .collect()
    }

    /// 实时计数：已采集 / 合格 / 待复核 / 不合格。
    pub fn counts(&self) -> Value {
        let count = |s: &str| self.records.iter().filter(|r| r["status"].as_str() == Some(s)).count();
        json!({
            "total": self.records.len(),
            STATUS_PASS: count(STATUS_PASS),
            STATUS_REVIEW: count(STATUS_REVIEW),
            STATUS_FAIL: count(STATUS_FAIL),
        })
    }

    // ---- 崩溃恢复 ----

    /// 扫描 sessions/ 目录重建索引：保留已有记录，补登索引缺失的素材目录。
    /// 返回补登段数。补登记录标记为待复核，帧数从 capture_meta.json 读取（缺失记 0）。
    pub fn rebuild(&mut self) -> Result<u64, String> {
        let known: Vec<String> = self
            .records
            .iter()
            .filter_map(|r| r["session_id"].as_str().map(str::to_string))
            .collect();
        let mut added = 0u64;
        let mut dirs: Vec<PathBuf> = std::fs::read_dir(&self.sessions_dir)
            .map_err(|e| format!("扫描 sessions/ 失败: {e}"))?
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| p.is_dir())
            .collect();
        dirs.sort();
        for d in dirs {
            let name = d.file_name().unwrap().to_string_lossy().into_owned();
            if known.contains(&name) {
                continue;
            }
            let mut frame_count = 0u64;
            let mut fps = 0.0f64;
            let meta_path = d.join("capture_meta.json");
            if meta_path.is_file() {
                if let Ok(text) = std::fs::read_to_string(&meta_path) {
                    if let Ok(meta) = serde_json::from_str::<Value>(&text) {
                        frame_count = meta["frame_count"].as_u64().unwrap_or(0);
                        fps = meta["fps"].as_f64().unwrap_or(0.0);
                    }
                }
            }
            let today = utc_today();
            let seq = self
                .records
                .iter()
                .filter(|r| r.get("date").and_then(Value::as_str) == Some(&today))
                .count() as u64
                + 1;
            self.records.push(json!({
                "session_id": name,
                "date": today,
                "seq": seq,
                "created_at": utc_now_iso(),
                "clip_dir": format!("sessions/{name}"),
                "frame_count": frame_count,
                "fps": fps,
                "trigger_idx": Value::Null,
                "status": STATUS_REVIEW,
                "trim": {"start_frame": 0, "end_frame": frame_count.saturating_sub(1)},
                "rebuilt": true,
            }));
            added += 1;
        }
        if added > 0 {
            self.flush()?;
        }
        Ok(added)
    }

    // ---- 内部 ----

    fn index_of(&self, session_id: &str) -> Result<usize, String> {
        self.records
            .iter()
            .position(|r| r["session_id"].as_str() == Some(session_id))
            .ok_or_else(|| format!("素材不存在: {session_id}"))
    }

    fn find_mut(&mut self, session_id: &str) -> Result<&mut Value, String> {
        let idx = self.index_of(session_id)?;
        Ok(&mut self.records[idx])
    }

    /// 原子落盘：先写临时文件再 rename，避免崩溃留下半截 JSON。
    fn flush(&self) -> Result<(), String> {
        let tmp = self.index_path.with_extension("json.tmp");
        let text = serde_json::to_string_pretty(&self.records).map_err(|e| e.to_string())?;
        std::fs::write(&tmp, text).map_err(|e| format!("写 index.json 失败: {e}"))?;
        std::fs::rename(&tmp, &self.index_path).map_err(|e| format!("替换 index.json 失败: {e}"))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::session::ulid::new_session_id;

    fn tmp_root(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "batana-store-{tag}-{}-{}",
            std::process::id(),
            new_session_id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        dir
    }

    fn make_clip_dir(store: &SessionStore, session_id: &str, frame_count: u64) -> String {
        let d = store.sessions_dir.join(session_id);
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(
            d.join("capture_meta.json"),
            json!({"frame_count": frame_count, "fps": 120.0}).to_string(),
        )
        .unwrap();
        std::fs::write(d.join("left.mkv"), b"fake").unwrap();
        format!("sessions/{session_id}")
    }

    #[test]
    fn add_clip_appends_and_persists() {
        let root = tmp_root("add");
        let mut store = SessionStore::new(&root).unwrap();
        let sid = new_session_id();
        let clip_dir = make_clip_dir(&store, &sid, 30);
        let record = store
            .add_clip(&sid, Path::new(&clip_dir), 30, 120.0, Some(40), None)
            .unwrap();
        assert_eq!(record["seq"], 1);
        assert_eq!(record["status"], STATUS_REVIEW);
        assert_eq!(record["trim"], json!({"start_frame": 0, "end_frame": 29}));
        // index.json 已落盘且可被新实例读回（崩溃恢复）
        let store2 = SessionStore::new(&root).unwrap();
        assert_eq!(store2.get(&sid).unwrap()["session_id"], sid);
        assert_eq!(store2.counts()["total"], 1);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn mark_three_way_status() {
        let root = tmp_root("mark");
        let mut store = SessionStore::new(&root).unwrap();
        let sid = new_session_id();
        let dir = make_clip_dir(&store, &sid, 30);
        store.add_clip(&sid, Path::new(&dir), 30, 120.0, None, None).unwrap();
        store.mark(&sid, STATUS_PASS).unwrap();
        assert_eq!(store.get(&sid).unwrap()["status"], STATUS_PASS);
        store.mark(&sid, STATUS_FAIL).unwrap();
        assert_eq!(store.list(Some(STATUS_FAIL))[0]["session_id"], sid);
        assert!(store.mark(&sid, "随便").unwrap_err().contains("非法标记"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn set_trim_bounds() {
        let root = tmp_root("trim");
        let mut store = SessionStore::new(&root).unwrap();
        let sid = new_session_id();
        let dir = make_clip_dir(&store, &sid, 100);
        store.add_clip(&sid, Path::new(&dir), 100, 120.0, None, None).unwrap();
        store.set_trim(&sid, 10, 89).unwrap();
        assert_eq!(store.get(&sid).unwrap()["trim"], json!({"start_frame": 10, "end_frame": 89}));
        assert!(store.set_trim(&sid, 50, 40).is_err());
        assert!(store.set_trim(&sid, 0, 100).is_err());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn delete_removes_record_and_files() {
        let root = tmp_root("delete");
        let mut store = SessionStore::new(&root).unwrap();
        let sid = new_session_id();
        make_clip_dir(&store, &sid, 30);
        store
            .add_clip(&sid, Path::new(&format!("sessions/{sid}")), 30, 120.0, None, None)
            .unwrap();
        store.delete(&sid, true).unwrap();
        assert_eq!(store.counts()["total"], 0);
        assert!(!store.sessions_dir.join(&sid).exists());
        assert!(store.get(&sid).is_err());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn counts_by_status() {
        let root = tmp_root("counts");
        let mut store = SessionStore::new(&root).unwrap();
        let ids: Vec<String> = (0..3).map(|_| new_session_id()).collect();
        for sid in &ids {
            let dir = make_clip_dir(&store, sid, 30);
            store.add_clip(sid, Path::new(&dir), 30, 120.0, None, None).unwrap();
        }
        store.mark(&ids[0], STATUS_PASS).unwrap();
        store.mark(&ids[1], STATUS_FAIL).unwrap();
        let c = store.counts();
        assert_eq!(c["total"], 3);
        assert_eq!(c[STATUS_PASS], 1);
        assert_eq!(c[STATUS_REVIEW], 1);
        assert_eq!(c[STATUS_FAIL], 1);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn rebuild_recovers_missing_index() {
        let root = tmp_root("rebuild");
        let mut store = SessionStore::new(&root).unwrap();
        let ids: Vec<String> = (0..2).map(|_| new_session_id()).collect();
        for sid in &ids {
            let dir = make_clip_dir(&store, sid, 42);
            store.add_clip(sid, Path::new(&dir), 42, 120.0, None, None).unwrap();
        }
        // 模拟崩溃：索引丢失，素材目录还在
        std::fs::remove_file(root.join("index.json")).unwrap();
        let mut store2 = SessionStore::new(&root).unwrap();
        assert_eq!(store2.counts()["total"], 0);
        let added = store2.rebuild().unwrap();
        assert_eq!(added, 2);
        assert_eq!(store2.counts()["total"], 2);
        let rec = store2.get(&ids[0]).unwrap();
        assert_eq!(rec["frame_count"], 42);
        assert_eq!(rec["status"], STATUS_REVIEW);
        assert_eq!(rec["rebuilt"], true);
        // 重复 rebuild 不重复登记
        assert_eq!(store2.rebuild().unwrap(), 0);
        let _ = std::fs::remove_dir_all(&root);
    }
}

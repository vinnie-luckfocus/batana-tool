//! 姿态数据模型与 pose2d.json 读写：MediaPipe BlazePose 33 关键点。
//! 点序与命名严格对齐 batana-core session-schema 契约 pose2d 字段约定。
//! 语义对齐 Python 版 app/pose/model.py + app/pose/io.py。

use std::path::Path;

use serde::{Deserialize, Serialize};

/// 固定 33 点（MediaPipe BlazePose 拓扑，顺序与契约一致，不可改动）
pub const KEYPOINT_NAMES: [&str; 33] = [
    "nose", "left_eye_inner", "left_eye", "left_eye_outer",
    "right_eye_inner", "right_eye", "right_eye_outer",
    "left_ear", "right_ear", "mouth_left", "mouth_right",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_pinky", "right_pinky",
    "left_index", "right_index", "left_thumb", "right_thumb",
    "left_hip", "right_hip", "left_knee", "right_knee",
    "left_ankle", "right_ankle", "left_heel", "right_heel",
    "left_foot_index", "right_foot_index",
];

fn is_false(b: &bool) -> bool {
    !*b
}

/// 单个关键点：归一化坐标 [0,1]（原点左上）+ 可见性。
/// manual=true 表示经人工拖动修正；auto 保留修正前的自动原值。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Keypoint {
    pub name: String,
    pub x: f64,
    pub y: f64,
    pub visibility: f64,
    #[serde(default, skip_serializing_if = "is_false")]
    pub manual: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub auto: Option<AutoValue>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct AutoValue {
    pub x: f64,
    pub y: f64,
    pub visibility: f64,
}

/// 单帧姿态：33 点 + 整帧置信度。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PoseFrame {
    pub frame_index: u64,
    pub timestamp_ms: f64,
    pub confidence: f64,
    pub keypoints: Vec<Keypoint>,
}

impl PoseFrame {
    pub fn new(frame_index: u64, timestamp_ms: f64, confidence: f64, keypoints: Vec<Keypoint>) -> Result<Self, String> {
        if keypoints.len() != 33 {
            return Err(format!("PoseFrame 必须含 33 个关键点，实际 {}", keypoints.len()));
        }
        Ok(Self {
            frame_index,
            timestamp_ms,
            confidence,
            keypoints,
        })
    }

    /// 手动修正关键点：标记 manual 并保留自动原值（重复修正保留最早原值）。
    pub fn correct(&mut self, keypoint: usize, x: f64, y: f64, visibility: Option<f64>) -> Result<(), String> {
        let kp = self
            .keypoints
            .get_mut(keypoint)
            .ok_or_else(|| format!("关键点序号越界: {keypoint}"))?;
        if !kp.manual {
            kp.auto = Some(AutoValue {
                x: kp.x,
                y: kp.y,
                visibility: kp.visibility,
            });
        }
        kp.x = x;
        kp.y = y;
        kp.visibility = visibility.unwrap_or(1.0);
        kp.manual = true;
        Ok(())
    }
}

/// 写 pose2d.json（结构对齐契约 pose2d 字段）。
pub fn write_pose2d(
    path: &Path,
    model: &str,
    frame_rate: f64,
    frames: &[PoseFrame],
) -> Result<(), String> {
    let data = serde_json::json!({
        "model": model,
        "frame_rate": frame_rate,
        "frames": frames,
    });
    let text = serde_json::to_string_pretty(&data).map_err(|e| e.to_string())?;
    std::fs::write(path, text).map_err(|e| format!("写 pose2d.json 失败: {e}"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stub_frame(idx: u64) -> PoseFrame {
        let keypoints = KEYPOINT_NAMES
            .iter()
            .map(|n| Keypoint {
                name: n.to_string(),
                x: 0.5,
                y: 0.5,
                visibility: 0.9,
                manual: false,
                auto: None,
            })
            .collect();
        PoseFrame::new(idx, idx as f64 / 120.0 * 1000.0, 0.9, keypoints).unwrap()
    }

    #[test]
    fn pose_frame_requires_33_keypoints() {
        assert!(PoseFrame::new(0, 0.0, 0.9, vec![]).is_err());
        let f = stub_frame(0);
        assert_eq!(f.keypoints.len(), 33);
        assert_eq!(f.keypoints[0].name, "nose");
        assert_eq!(f.keypoints[32].name, "right_foot_index");
    }

    #[test]
    fn pose_correct_keeps_original_auto() {
        let mut f = stub_frame(0);
        f.correct(0, 0.6, 0.6, None).unwrap();
        f.correct(0, 0.7, 0.7, None).unwrap();
        let kp = &f.keypoints[0];
        assert!(kp.manual);
        assert_eq!((kp.x, kp.y), (0.7, 0.7));
        let auto = kp.auto.unwrap();
        assert_eq!((auto.x, auto.y), (0.5, 0.5)); // 保留最早原值
    }

    #[test]
    fn write_pose2d_roundtrip_json() {
        let dir = std::env::temp_dir().join(format!("batana-pose-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("pose2d.json");
        write_pose2d(&path, "batana-pose-v0.1", 120.0, &[stub_frame(0), stub_frame(1)]).unwrap();
        let data: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(data["model"], "batana-pose-v0.1");
        assert_eq!(data["frame_rate"], 120.0);
        assert_eq!(data["frames"].as_array().unwrap().len(), 2);
        assert_eq!(data["frames"][0]["keypoints"].as_array().unwrap().len(), 33);
        // manual=false 不落盘（对齐 Python to_dict 仅在 manual 时输出）
        assert!(data["frames"][0]["keypoints"][0].get("manual").is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }
}

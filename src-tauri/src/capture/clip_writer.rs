//! 片段落盘写出器：左右目无损视频（ffmpeg 子进程 FFV1/MKV）+ 逐帧时间戳 + 采集参数。
//! 语义对齐 Python 版 app/capture/clip_writer.py；编码由 ffmpeg 子进程完成
//! （Python 版用 cv2.VideoWriter，此处改为 ffmpeg stdin 管道，参数等价）。

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use super::{find_in_path, BufferItem};

/// 一段素材落盘后的文件清单。
#[derive(Debug, Clone)]
pub struct ClipPaths {
    pub out_dir: PathBuf,
    pub left_video: PathBuf,
    pub right_video: PathBuf,
    pub timestamps_csv: PathBuf,
    pub capture_meta: PathBuf,
    pub codec: String,
    pub frame_count: usize,
}

/// 单目灰度序列 → FFV1/MKV（无损）。帧经 stdin 以 rawvideo 喂入。
fn encode_eye(frames: &[&[u8]], path: &Path, fps: f64, width: usize, height: usize) -> Result<(), String> {
    let ffmpeg = find_in_path("ffmpeg").ok_or("未找到 ffmpeg（brew install ffmpeg）")?;
    let mut child = Command::new(ffmpeg)
        .args([
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-s",
            &format!("{width}x{height}"),
            "-r",
            &format!("{fps}"),
            "-i",
            "pipe:0",
            "-c:v",
            "ffv1",
            "-level",
            "3",
        ])
        .arg(path)
        .stdin(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("启动 ffmpeg 编码失败: {e}"))?;
    {
        let mut stdin = child.stdin.take().expect("stdin 已管道化");
        for f in frames {
            if stdin.write_all(f).is_err() {
                break; // 编码器中途退出，错误以退出码为准
            }
        } // drop stdin → EOF，ffmpeg 收尾写索引
    }
    let out = child
        .wait_with_output()
        .map_err(|e| format!("等待 ffmpeg 编码结束失败: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "ffmpeg FFV1 编码失败（{}）: {}",
            path.display(),
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(())
}

/// 把 (idx, ts_ns, left, right) 帧序列写为一段素材目录；frames 至少 1 帧。
pub fn write_clip(
    items: &[BufferItem],
    out_dir: &Path,
    fps: f64,
    meta: serde_json::Value,
) -> Result<ClipPaths, String> {
    if items.is_empty() {
        return Err("空片段：至少需要 1 帧".into());
    }
    std::fs::create_dir_all(out_dir).map_err(|e| format!("创建素材目录失败: {e}"))?;
    let (w, h) = (items[0].2.width, items[0].2.height);

    let left_path = out_dir.join("left.mkv");
    let right_path = out_dir.join("right.mkv");
    let left_frames: Vec<&[u8]> = items.iter().map(|it| &it.2.data[..]).collect();
    let right_frames: Vec<&[u8]> = items.iter().map(|it| &it.3.data[..]).collect();
    encode_eye(&left_frames, &left_path, fps, w, h)?;
    encode_eye(&right_frames, &right_path, fps, w, h)?;

    let ts_path = out_dir.join("timestamps.csv");
    let mut csv = String::from("frame_idx,ts_ns\n");
    for (idx, ts_ns, _, _) in items {
        csv.push_str(&format!("{idx},{ts_ns}\n"));
    }
    std::fs::write(&ts_path, csv).map_err(|e| format!("写 timestamps.csv 失败: {e}"))?;

    let meta_path = out_dir.join("capture_meta.json");
    let mut capture_meta = serde_json::json!({
        "fps": fps,
        "codec": "FFV1",
        "lossless": true,
        "frame_count": items.len(),
        "per_eye_resolution": [w, h],
        "pixel_format": "MONO8",
    });
    // 调用方附加字段（对齐 Python 版 **meta 展开语义）
    if let serde_json::Value::Object(extra) = meta {
        for (k, v) in extra {
            capture_meta[k] = v;
        }
    }
    let text = serde_json::to_string_pretty(&capture_meta).map_err(|e| e.to_string())?;
    std::fs::write(&meta_path, text).map_err(|e| format!("写 capture_meta.json 失败: {e}"))?;

    Ok(ClipPaths {
        out_dir: out_dir.to_path_buf(),
        left_video: left_path,
        right_video: right_path,
        timestamps_csv: ts_path,
        capture_meta: meta_path,
        codec: "FFV1".into(),
        frame_count: items.len(),
    })
}

/// 读回视频统计帧数（完整性校验用；ffmpeg 缺失返回 None）。
pub fn count_video_frames(path: &Path) -> Option<usize> {
    let ffprobe = find_in_path("ffprobe")?;
    let out = Command::new(ffprobe)
        .args([
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "csv=p=0",
        ])
        .arg(path)
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    String::from_utf8_lossy(&out.stdout).trim().parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::capture::GrayFrame;

    fn make_items(n: u64, w: usize, h: usize) -> Vec<BufferItem> {
        // 确定性伪随机（简单 LCG），避免全零帧被编码器特殊处理
        let mut seed = 7u32;
        let mut next = move || {
            seed = seed.wrapping_mul(1103515245).wrapping_add(12345);
            (seed >> 16) as u8
        };
        (0..n)
            .map(|i| {
                let l: Vec<u8> = (0..w * h).map(|_| next()).collect();
                let r: Vec<u8> = (0..w * h).map(|_| next()).collect();
                (i, i * 33_000_000, GrayFrame::new(w, h, l), GrayFrame::new(w, h, r))
            })
            .collect()
    }

    #[test]
    fn write_clip_produces_files() {
        if find_in_path("ffmpeg").is_none() {
            eprintln!("跳过：无 ffmpeg");
            return;
        }
        let dir = std::env::temp_dir().join(format!("batana-clip-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let items = make_items(10, 96, 64);
        let paths = write_clip(&items, &dir, 30.0, serde_json::json!({"source": "test"})).unwrap();
        assert!(paths.left_video.is_file());
        assert!(paths.right_video.is_file());
        let ts = std::fs::read_to_string(&paths.timestamps_csv).unwrap();
        assert_eq!(ts.trim().lines().count(), 11); // 表头 + 10 帧
        let meta: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&paths.capture_meta).unwrap()).unwrap();
        assert_eq!(meta["codec"], "FFV1");
        assert_eq!(meta["lossless"], true);
        assert_eq!(meta["frame_count"], 10);
        assert_eq!(meta["source"], "test");
        if find_in_path("ffprobe").is_some() {
            assert_eq!(count_video_frames(&paths.left_video), Some(10));
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn write_clip_empty_rejected() {
        let dir = std::env::temp_dir().join("batana-clip-empty");
        let err = write_clip(&[], &dir, 30.0, serde_json::Value::Null).unwrap_err();
        assert!(err.contains("空片段"));
    }
}

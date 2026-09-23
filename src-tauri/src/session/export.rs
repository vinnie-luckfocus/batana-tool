//! 素材导出（PRD F10）：按 session-schema v1.0 契约产出素材目录 + 内置校验 + 可选全量校验。
//! 语义对齐 Python 版 app/session/export.py。
//!
//! 产出（sessions/<session_id>/）：
//!     session.json / left.mkv / right.mkv / timestamps.csv / capture_meta.json
//!     （提供 pose_frames 时另有 pose2d.json）

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::capture::{clip_writer, BufferItem};

use super::pose::{write_pose2d, PoseFrame, KEYPOINT_NAMES};
use super::ulid::new_session_id;

pub const SCHEMA_VERSION: &str = "1.0";
pub const PIPELINES: [&str; 5] = ["standard-vision", "standard-imu", "pro-fusion", "pro-stereo", "max"];

/// batana-core 仓默认路径（存在时可调用其全量校验器）
pub const DEFAULT_CORE_REPO: &str = "/Users/vinniechow/Projects/private/batana-core";

// ---- UTC 时间工具（无外部依赖的 civil 换算） ----

/// 当前 UTC 时间，ISO 8601（Z 后缀，秒精度，对齐 Python isoformat(timespec="seconds")）。
pub fn utc_now_iso() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0) as i64;
    iso_from_unix(secs)
}

/// 当日 UTC 日期 YYYY-MM-DD（SessionStore 序号分组用）。
pub fn utc_today() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0) as i64;
    iso_from_unix(secs)[..10].to_string()
}

/// Unix 秒 → "YYYY-MM-DDTHH:MM:SSZ"（Howard Hinnant civil_from_days 算法）。
fn iso_from_unix(secs: i64) -> String {
    let days = secs.div_euclid(86400);
    let sod = secs.rem_euclid(86400);
    let (h, m, s) = (sod / 3600, (sod % 3600) / 60, sod % 60);
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let mo = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if mo <= 2 { y + 1 } else { y };
    format!("{y:04}-{mo:02}-{d:02}T{h:02}:{m:02}:{s:02}Z")
}

/// ISO 8601 UTC（Z 后缀）格式检查，对齐契约正则 ^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$。
fn is_iso_utc(s: &str) -> bool {
    let Some(body) = s.strip_suffix('Z') else {
        return false;
    };
    let (main, frac) = match body.split_once('.') {
        Some((m, f)) => (m, Some(f)),
        None => (body, None),
    };
    const PAT: &[u8; 19] = b"0000-00-00T00:00:00";
    if main.len() != 19 {
        return false;
    }
    let main_ok = main.bytes().zip(PAT.iter()).all(|(c, p)| match p {
        b'0' => c.is_ascii_digit(),
        other => c == *other,
    });
    main_ok
        && frac.is_none_or(|f| !f.is_empty() && f.bytes().all(|c| c.is_ascii_digit()))
}

/// session_id 格式检查：sess_ + 26 位 Crockford Base32（无 ILOU）。
fn is_session_id(s: &str) -> bool {
    let Some(body) = s.strip_prefix("sess_") else {
        return false;
    };
    body.len() == 26
        && body
            .bytes()
            .all(|b| b"0123456789ABCDEFGHJKMNPQRSTVWXYZ".contains(&b))
}

// ---- 文件工具 ----

fn sha256_file(path: &Path) -> Result<String, String> {
    let mut f = std::fs::File::open(path).map_err(|e| format!("打开文件失败 {}: {e}", path.display()))?;
    let mut h = Sha256::new();
    std::io::copy(&mut f, &mut h).map_err(|e| format!("读文件失败 {}: {e}", path.display()))?;
    Ok(format!("{:x}", h.finalize()))
}

/// 本地绝对路径 → file:// URI（对齐 Python Path.resolve().as_uri()）。
pub fn file_uri(path: &Path) -> String {
    let abs = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let bytes = abs.to_string_lossy().as_bytes().to_vec();
    let mut out = String::from("file://");
    for b in bytes {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'/' | b'-' | b'.' | b'_' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}

/// macOS 版本号（device.os 默认值用）。
fn macos_version() -> String {
    Command::new("sw_vers")
        .arg("-productVersion")
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "unknown".into())
}

// ---- session.json 组装 ----

pub struct SessionJsonSpec<'a> {
    pub session_id: &'a str,
    pub left_video: &'a Path,
    pub frame_count: u64,
    pub fps: f64,
    pub resolution: (usize, usize),
    pub handedness: &'a str,
    pub pipeline: &'a str,
    pub device: Option<Value>,
    pub subject: Option<Value>,
    pub calibration: Option<Value>,
    pub pose_model: Option<&'a str>,
    pub pose_frames: Option<&'a [PoseFrame]>,
    pub video_sha256: bool,
}

/// 组装 session.json 数据（字段严格对齐 session-schema v1.0）。
pub fn build_session_json(spec: &SessionJsonSpec) -> Result<Value, String> {
    let duration_ms = ((spec.frame_count as f64 / spec.fps) * 1000.0).round().max(1.0) as u64;
    let device = spec.device.clone().unwrap_or_else(|| {
        json!({
            "model": "OV9281 双目模组（2560x800 SBS UVC）",
            "os": format!("macOS {}", macos_version()),
        })
    });
    let mut data = json!({
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "session_id": spec.session_id,
            "created_at": utc_now_iso(),
            "duration_ms": duration_ms,
            "device": device,
            "app": {"name": "batana-tool", "version": env!("CARGO_PKG_VERSION")},
            "pipeline": spec.pipeline,
            "handedness": spec.handedness,
        },
        "video": {
            "uri": file_uri(spec.left_video),
            "fps": spec.fps,
            "resolution": [spec.resolution.0, spec.resolution.1],
            "duration_ms": duration_ms,
            "view": "side",
        },
    });
    if spec.video_sha256 {
        data["video"]["sha256"] = json!(sha256_file(spec.left_video)?);
    }
    if let Some(subject) = &spec.subject {
        data["meta"]["subject"] = subject.clone();
    }
    if let Some(calibration) = &spec.calibration {
        data["calibration"] = calibration.clone();
    }
    if let (Some(model), Some(frames)) = (spec.pose_model, spec.pose_frames) {
        data["pose2d"] = json!({
            "model": model,
            "frame_rate": spec.fps,
            "frames": frames,
        });
    }
    Ok(data)
}

// ---- 内置契约校验 ----

/// 内置轻量必填校验：对齐 session-schema 契约必填项，返回错误列表（空 = 通过）。
/// 全量校验请用 validate_with_core（需本机存在 batana-core 仓）。
pub fn validate_session_builtin(data: &Value) -> Vec<String> {
    let mut errors: Vec<String> = Vec::new();
    let mut err = |path: &str, msg: &str| errors.push(format!("{path}: {msg}"));

    if !data.is_object() {
        return vec!["根节点: 应为对象".into()];
    }
    match data.get("schema_version").and_then(Value::as_str) {
        Some(sv) if sv.starts_with("1.") => {}
        _ => err("schema_version", "必填，主版本 1.x"),
    }

    match data.get("meta") {
        Some(meta) if meta.is_object() => {
            let sid_ok = meta
                .get("session_id")
                .and_then(Value::as_str)
                .is_some_and(is_session_id);
            if !sid_ok {
                err("meta.session_id", "必填，格式 sess_ + 26 位 Crockford Base32");
            }
            let created_ok = meta
                .get("created_at")
                .and_then(Value::as_str)
                .is_some_and(is_iso_utc);
            if !created_ok {
                err("meta.created_at", "必填，ISO 8601 UTC（Z 后缀）");
            }
            let dur_ok = meta
                .get("duration_ms")
                .and_then(Value::as_u64)
                .is_some_and(|d| d > 0);
            if !dur_ok {
                err("meta.duration_ms", "必填，正整数（毫秒）");
            }
            for (section, keys) in [("device", ["model", "os"]), ("app", ["name", "version"])] {
                let ok = meta.get(section).is_some_and(|sec| {
                    sec.is_object()
                        && keys
                            .iter()
                            .all(|k| sec.get(k).and_then(Value::as_str).is_some_and(|s| !s.is_empty()))
                });
                if !ok {
                    err(
                        &format!("meta.{section}"),
                        &format!("必填，需含非空字符串字段 {keys:?}"),
                    );
                }
            }
            let pipeline_ok = meta
                .get("pipeline")
                .and_then(Value::as_str)
                .is_some_and(|p| PIPELINES.contains(&p));
            if !pipeline_ok {
                err("meta.pipeline", &format!("必填，取值 {PIPELINES:?}"));
            }
            let handed_ok = meta
                .get("handedness")
                .and_then(Value::as_str)
                .is_some_and(|h| h == "right" || h == "left");
            if !handed_ok {
                err("meta.handedness", "必填，right / left");
            }
        }
        _ => err("meta", "必填，应为对象"),
    }

    if let Some(video) = data.get("video") {
        if !video.is_object() {
            err("video", "应为对象");
        } else {
            if !video
                .get("uri")
                .and_then(Value::as_str)
                .is_some_and(|u| !u.is_empty())
            {
                err("video.uri", "必填，非空字符串");
            }
            if !video
                .get("fps")
                .and_then(Value::as_f64)
                .is_some_and(|f| f > 0.0)
            {
                err("video.fps", "必填，正数");
            }
            let res_ok = video.get("resolution").and_then(Value::as_array).is_some_and(|r| {
                r.len() == 2 && r.iter().all(|x| x.as_u64().is_some_and(|v| v > 0))
            });
            if !res_ok {
                err("video.resolution", "必填，[宽, 高] 正整数");
            }
        }
    }

    if let Some(pose) = data.get("pose2d") {
        if !pose.is_object() {
            err("pose2d", "应为对象");
        } else {
            if !pose
                .get("model")
                .and_then(Value::as_str)
                .is_some_and(|m| !m.is_empty())
            {
                err("pose2d.model", "必填，非空字符串");
            }
            if !pose
                .get("frame_rate")
                .and_then(Value::as_f64)
                .is_some_and(|f| f > 0.0)
            {
                err("pose2d.frame_rate", "必填，正数");
            }
            match pose.get("frames").and_then(Value::as_array) {
                None => err("pose2d.frames", "必填，应为数组"),
                Some(frames) => {
                    for (i, f) in frames.iter().enumerate() {
                        let Some(kps) = f.get("keypoints").and_then(Value::as_array) else {
                            err(&format!("pose2d.frames[{i}].keypoints"), "应为 33 点数组");
                            continue;
                        };
                        if kps.len() != 33 {
                            err(&format!("pose2d.frames[{i}].keypoints"), "应为 33 点数组");
                            continue;
                        }
                        let names_ok = kps.iter().enumerate().all(|(j, k)| {
                            k.get("name").and_then(Value::as_str) == Some(KEYPOINT_NAMES[j])
                        });
                        if !names_ok {
                            err(
                                &format!("pose2d.frames[{i}].keypoints"),
                                "关键点名称或顺序与契约 33 点枚举不符",
                            );
                            continue;
                        }
                        for (j, k) in kps.iter().enumerate() {
                            for field in ["x", "y", "visibility"] {
                                let ok = k
                                    .get(field)
                                    .and_then(Value::as_f64)
                                    .is_some_and(|v| (0.0..=1.0).contains(&v));
                                if !ok {
                                    err(
                                        &format!("pose2d.frames[{i}].keypoints[{j}].{field}"),
                                        "应在 [0,1]",
                                    );
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    errors
}

// ---- batana-core 全量校验（可选） ----

/// 本机是否存在可调用的 batana-core 全量校验器。
pub fn core_validator_available(core_repo: &Path) -> bool {
    core_repo.join("tools").join("validate_session.py").is_file()
}

/// 解析 batana-core 仓路径：配置优先，否则探测常见位置；不可用返回 None。
pub fn resolve_core_repo(configured: &str) -> Option<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if !configured.is_empty() {
        candidates.push(PathBuf::from(configured));
    }
    candidates.push(PathBuf::from(DEFAULT_CORE_REPO));
    if let Some(home) = std::env::var_os("HOME") {
        candidates.push(Path::new(&home).join("Projects/private/batana-core"));
        candidates.push(Path::new(&home).join("Projects/batana-core"));
    }
    candidates.into_iter().find(|c| core_validator_available(c))
}

/// 调用 batana-core tools/validate_session.py 做契约全量校验。
/// 返回错误列表（空 = 通过）；batana-core 仓不存在时返回 None。
/// 解释器优先用 core 仓自带 .venv（系统 python3 可能缺 numpy 等依赖）。
pub fn validate_with_core(session_json: &Path, core_repo: &Path) -> Option<Vec<String>> {
    if !core_validator_available(core_repo) {
        return None;
    }
    let venv_py = core_repo.join(".venv").join("bin").join("python3");
    let python = if venv_py.is_file() {
        venv_py.to_string_lossy().into_owned()
    } else {
        "python3".to_string()
    };
    let out = Command::new(python)
        .args(["-m", "tools.validate_session"])
        .arg(session_json)
        .current_dir(core_repo)
        .output()
        .ok()?;
    if out.status.success() {
        return Some(Vec::new());
    }
    let stdout = String::from_utf8_lossy(&out.stdout);
    let lines: Vec<String> = stdout
        .lines()
        .map(str::trim)
        .filter(|l| l.starts_with("- "))
        .map(|l| l[2..].to_string())
        .collect();
    Some(if lines.is_empty() {
        let text = stdout.trim();
        vec![if text.is_empty() {
            format!("validate_session 退出码 {}", out.status.code().unwrap_or(-1))
        } else {
            text.to_string()
        }]
    } else {
        lines
    })
}

// ---- 导出入口 ----

#[derive(Default)]
pub struct ExportOptions<'a> {
    pub handedness: Option<&'a str>,
    pub pipeline: Option<&'a str>,
    pub device: Option<Value>,
    pub subject: Option<Value>,
    pub calibration: Option<Value>,
    pub capture_extra: Option<Value>,
    pub pose_model: Option<&'a str>,
    pub pose_frames: Option<Vec<PoseFrame>>,
    pub session_id: Option<String>,
}

/// 把一段素材导出为契约目录，返回素材目录路径。
/// 导出前执行内置必填校验，失败返回 Err。
pub fn export_session(
    items: &[BufferItem],
    out_root: &Path,
    fps: f64,
    options: ExportOptions,
) -> Result<PathBuf, String> {
    if items.is_empty() {
        return Err("片段帧序列为空（环形缓冲可能容量不足，旧帧已被覆盖）".into());
    }
    let sid = options.session_id.clone().unwrap_or_else(new_session_id);
    let out_dir = out_root.join("sessions").join(&sid);

    let meta = options.capture_extra.clone().unwrap_or(Value::Null);
    let paths = clip_writer::write_clip(items, &out_dir, fps, meta)?;
    if let (Some(model), Some(frames)) = (options.pose_model, options.pose_frames.as_deref()) {
        write_pose2d(&out_dir.join("pose2d.json"), model, fps, frames)?;
    }

    let (w, h) = (items[0].2.width, items[0].2.height);
    let data = build_session_json(&SessionJsonSpec {
        session_id: &sid,
        left_video: &paths.left_video,
        frame_count: paths.frame_count as u64,
        fps,
        resolution: (w, h),
        handedness: options.handedness.unwrap_or("right"),
        pipeline: options.pipeline.unwrap_or("pro-stereo"),
        device: options.device,
        subject: options.subject,
        calibration: options.calibration,
        pose_model: options.pose_model,
        pose_frames: options.pose_frames.as_deref(),
        video_sha256: true,
    })?;
    let errors = validate_session_builtin(&data);
    if !errors.is_empty() {
        let detail = errors.iter().map(|e| format!("  - {e}")).collect::<Vec<_>>().join("\n");
        return Err(format!("导出未通过内置契约校验:\n{detail}"));
    }
    let text = serde_json::to_string_pretty(&data).map_err(|e| e.to_string())?;
    std::fs::write(out_dir.join("session.json"), text).map_err(|e| format!("写 session.json 失败: {e}"))?;
    Ok(out_dir)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::capture::{find_in_path, GrayFrame};
    use crate::session::pose::PoseFrame;
    use crate::session::pose::Keypoint;

    const FPS: f64 = 30.0;
    const SIZE: (usize, usize) = (96, 64);

    fn make_items(n: u64) -> Vec<BufferItem> {
        let mut seed = 7u32;
        let mut next = move || {
            seed = seed.wrapping_mul(1103515245).wrapping_add(12345);
            (seed >> 16) as u8
        };
        (0..n)
            .map(|i| {
                let l: Vec<u8> = (0..SIZE.0 * SIZE.1).map(|_| next()).collect();
                let r: Vec<u8> = (0..SIZE.0 * SIZE.1).map(|_| next()).collect();
                (
                    i,
                    i * 33_000_000,
                    GrayFrame::new(SIZE.0, SIZE.1, l),
                    GrayFrame::new(SIZE.0, SIZE.1, r),
                )
            })
            .collect()
    }

    fn stub_pose(n: u64) -> Vec<PoseFrame> {
        (0..n)
            .map(|i| {
                let keypoints = KEYPOINT_NAMES
                    .iter()
                    .map(|name| Keypoint {
                        name: name.to_string(),
                        x: 0.5,
                        y: 0.5,
                        visibility: 0.9,
                        manual: false,
                        auto: None,
                    })
                    .collect();
                PoseFrame::new(i, i as f64 / FPS * 1000.0, 0.9, keypoints).unwrap()
            })
            .collect()
    }

    fn tmp_root(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "batana-export-{tag}-{}-{}",
            std::process::id(),
            new_session_id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        dir
    }

    #[test]
    fn iso_utc_format_check() {
        assert!(is_iso_utc("2026-09-17T02:30:00Z"));
        assert!(is_iso_utc("2026-09-17T02:30:00.123Z"));
        assert!(!is_iso_utc("2026-09-17 02:30:00Z"));
        assert!(!is_iso_utc("2026-09-17T02:30:00+08:00"));
        assert!(!is_iso_utc("2026-09-17T02:30:00"));
        // 自家生成的时间戳必须过自家校验
        assert!(is_iso_utc(&utc_now_iso()));
    }

    #[test]
    fn builtin_validation_catches_errors() {
        let root = tmp_root("validate");
        std::fs::create_dir_all(&root).unwrap();
        let video = root.join("left.mkv");
        std::fs::write(&video, b"fake").unwrap();
        let good = build_session_json(&SessionJsonSpec {
            session_id: &new_session_id(),
            left_video: &video,
            frame_count: 30,
            fps: FPS,
            resolution: SIZE,
            handedness: "right",
            pipeline: "pro-stereo",
            device: None,
            subject: None,
            calibration: None,
            pose_model: None,
            pose_frames: None,
            video_sha256: false,
        })
        .unwrap();
        assert!(validate_session_builtin(&good).is_empty());

        let mut bad = good.clone();
        bad["meta"]["session_id"] = json!("bad-id");
        bad["meta"]["duration_ms"] = json!(-1);
        bad["meta"]["handedness"] = json!("both");
        bad["video"]["resolution"] = json!([0, 0]);
        let errors = validate_session_builtin(&bad);
        assert!(errors.iter().any(|e| e.contains("session_id")));
        assert!(errors.iter().any(|e| e.contains("duration_ms")));
        assert!(errors.iter().any(|e| e.contains("handedness")));
        assert!(errors.iter().any(|e| e.contains("resolution")));

        // pose2d 点序错乱被检出
        let mut bad2 = good.clone();
        let mut frame = stub_pose(1).remove(0);
        frame.keypoints.swap(0, 1);
        bad2["pose2d"] = json!({"model": "m", "frame_rate": FPS, "frames": [frame]});
        let errors2 = validate_session_builtin(&bad2);
        assert!(errors2.iter().any(|e| e.contains("keypoints")));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn export_produces_contract_directory() {
        if find_in_path("ffmpeg").is_none() {
            eprintln!("跳过：无 ffmpeg");
            return;
        }
        let root = tmp_root("export");
        let items = make_items(30);
        let pose = stub_pose(30);
        let out_dir = export_session(
            &items,
            &root,
            FPS,
            ExportOptions {
                pose_model: Some("batana-pose-stub-v0.1"),
                pose_frames: Some(pose),
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(out_dir.parent().unwrap(), root.join("sessions"));
        assert!(is_session_id(out_dir.file_name().unwrap().to_str().unwrap()));
        for name in ["session.json", "timestamps.csv", "capture_meta.json", "pose2d.json", "left.mkv", "right.mkv"] {
            assert!(out_dir.join(name).is_file(), "缺少 {name}");
        }
        let session: Value =
            serde_json::from_str(&std::fs::read_to_string(out_dir.join("session.json")).unwrap()).unwrap();
        assert!(validate_session_builtin(&session).is_empty());
        assert_eq!(session["schema_version"], "1.0");
        assert_eq!(session["meta"]["session_id"], out_dir.file_name().unwrap().to_str().unwrap());
        assert_eq!(session["meta"]["pipeline"], "pro-stereo");
        assert_eq!(session["meta"]["duration_ms"], 1000); // 30 帧 / 30fps
        assert_eq!(session["video"]["fps"], 30.0);
        assert_eq!(session["video"]["resolution"], json!([96, 64]));
        let sha = session["video"]["sha256"].as_str().unwrap();
        assert_eq!(sha.len(), 64);
        assert!(sha.bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()));
        assert_eq!(session["pose2d"]["frame_rate"], 30.0);
        assert_eq!(session["pose2d"]["frames"].as_array().unwrap().len(), 30);
        assert_eq!(
            session["pose2d"]["frames"][0]["keypoints"].as_array().unwrap().len(),
            33
        );
        let ts = std::fs::read_to_string(out_dir.join("timestamps.csv")).unwrap();
        assert_eq!(ts.trim().lines().count(), 31);
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn export_empty_clip_rejected() {
        let root = tmp_root("empty");
        let err = export_session(&[], &root, FPS, ExportOptions::default()).unwrap_err();
        assert!(err.contains("为空"));
        let _ = std::fs::remove_dir_all(&root);
    }

    /// batana-core 全量校验集成测试：core 仓不在本机时条件跳过。
    #[test]
    fn core_validate_session_full_pass() {
        let Some(core) = resolve_core_repo("") else {
            eprintln!("跳过：本机不存在 batana-core 仓");
            return;
        };
        if find_in_path("ffmpeg").is_none() {
            eprintln!("跳过：无 ffmpeg");
            return;
        }
        let root = tmp_root("core-valid");
        let items = make_items(30);
        let out_dir = export_session(&items, &root, FPS, ExportOptions::default()).unwrap();
        let errors = validate_with_core(&out_dir.join("session.json"), &core).unwrap();
        assert!(errors.is_empty(), "batana-core 全量校验失败: {errors:?}");

        // 坏文件必须被检出
        let bad = root.join("bad.json");
        std::fs::write(&bad, json!({"schema_version": "1.0", "meta": {}}).to_string()).unwrap();
        let errors = validate_with_core(&bad, &core).unwrap();
        assert!(!errors.is_empty());
        let _ = std::fs::remove_dir_all(&root);
    }
}

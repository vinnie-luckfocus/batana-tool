// IPC 契约层：字段名以 src-tauri/src/ 源码为准（snake_case，serde 内部标签）。
import { invoke, Channel, convertFileSrc } from "@tauri-apps/api/core";

export { Channel, convertFileSrc };

// ---- 设备 ----
export interface VideoDevice {
  index: number;
  name: string;
  is_stereo_module: boolean;
}

// ---- 采集事件（capture/mod.rs CaptureEvent，serde tag="type" snake_case） ----
export type CaptureEvent =
  | { type: "preview"; width: number; height: number; rgba: number[] | ArrayBuffer }
  | { type: "telemetry"; fps: number; presence_ratio: number; motion_ratio: number; state: SmStateName }
  | { type: "state_changed"; prev: SmStateName; next: SmStateName; reason: string; seq: number | null }
  | { type: "clip_saved"; session_id: string; seq: number; dir: string; frame_count: number }
  | { type: "error"; message: string };

export type SmStateName = "IDLE" | "READY" | "ARMED" | "SWING" | "SAVING" | "ERROR";

// ---- 素材摘要（session/mod.rs SessionSummary） ----
export interface SessionSummary {
  id: string;
  created_at: string; // ISO 8601 UTC
  date: string;
  seq: number;
  verdict: string; // 合格 / 不合格 / 待复核
  frame_count: number;
  fps: number;
  clip_dir: string;
}

export const STATUS_PASS = "合格";
export const STATUS_FAIL = "不合格";
export const STATUS_REVIEW = "待复核";

// ---- 设置（session/settings.rs AppSettings，容器级 serde(default)） ----
export interface AppSettings {
  roi: [number, number, number, number] | null; // 左目像素坐标 [x, y, w, h]
  capture_width: number;
  capture_height: number;
  capture_fps: number;
  pixel_format: string;
  camera_index: number;
  camera_name: string;
  capture_backend: string;
  presence_ratio: number;
  motion_trigger_pct: number;
  motion_release_pct: number;
  motion_pix_thresh: number;
  pre_roll_seconds: number;
  post_roll_seconds: number;
  countdown_seconds: number;
  buffer_seconds: number;
  voice_enabled: boolean;
  voice_rate: number;
  storage_root: string;
  pose_model_path: string;
  core_repo_path: string;
  env_brightness_fail: number;
  env_brightness_warn: number;
  env_flicker_warn_pct: number;
  env_flicker_fail_pct: number;
  env_sharpness_warn: number;
  env_level_warn_deg: number;
  env_level_fail_deg: number;
  env_planned_clips: number;
  env_est_mb_per_clip: number;
}

// ---- 骨架（session/pose.rs pose2d.json） ----
export interface PoseKeypoint {
  name: string;
  x: number; // 归一化 [0,1]
  y: number;
  visibility: number;
  manual?: boolean;
}
export interface PoseFrame {
  frame_index: number;
  timestamp_ms: number;
  confidence: number;
  keypoints: PoseKeypoint[];
}
export interface Pose2dFile {
  model: string;
  frame_rate: number;
  frames: PoseFrame[];
}

// ---- 命令封装 ----
export const listDevices = () => invoke<VideoDevice[]>("list_devices");
export const startCapture = (channel: Channel<unknown>) => invoke<void>("start_capture", { channel });
export const stopCapture = () => invoke<void>("stop_capture");
export const discardClip = () => invoke<void>("discard_clip");
export const manualToggle = () => invoke<string>("manual_toggle");
export const setMuted = (muted: boolean) => invoke<void>("set_muted", { muted });
export const listSessions = () => invoke<SessionSummary[]>("list_sessions");
export const getSettings = () => invoke<AppSettings>("get_settings");
export const saveSettings = (settings: AppSettings) => invoke<void>("save_settings", { settings });
export const markSession = (sessionId: string, status: string) =>
  invoke<void>("mark_session", { sessionId, status });
export const deleteSession = (sessionId: string) => invoke<void>("delete_session", { sessionId });
export const readPose2d = (sessionId: string) => invoke<Pose2dFile | null>("read_pose2d", { sessionId });
export const ensurePreviewVideo = (sessionId: string, eye: "left" | "right") =>
  invoke<string>("ensure_preview_video", { sessionId, eye });

/** 全局共享设置（启动时加载一次，各页读写同一对象，保存后仍指向最新）。 */
export const store: { settings: AppSettings | null } = { settings: null };

export async function loadSettings(): Promise<AppSettings> {
  store.settings = await getSettings();
  return store.settings;
}

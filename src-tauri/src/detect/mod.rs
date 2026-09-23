//! 检测层：ROI 就位检测（简化背景建模，无 OpenCV）与挥棒检测（显著运动像素占比）。
//! 语义对齐 Python 版 app/detect/（presence.py / swing.py）。

pub mod presence;
pub mod swing;

pub use presence::{PresenceDetector, PresenceMethod};
pub use swing::{SwingDetector, SwingEvent};

use crate::capture::{GrayFrame, Roi};

/// ROI 裁剪（x, y, w, h 像素坐标）；越界部分裁剪到帧内。
pub fn crop_roi(frame: &GrayFrame, roi: Roi) -> Vec<u8> {
    let (x, y, w, h) = roi;
    let x_end = (x + w).min(frame.width);
    let y_end = (y + h).min(frame.height);
    let mut out = Vec::with_capacity((x_end - x) * (y_end - y));
    for row in y..y_end {
        let start = row * frame.width + x;
        out.extend_from_slice(&frame.data[start..start + (x_end - x)]);
    }
    out
}

/// 就位检测抽象（状态机依赖；测试可注入可控假实现，对齐 Python conftest FakePresence）。
pub trait PresenceDetect: Send {
    /// 喂入一帧（左目灰度整帧），返回稳定判定后的是否就位。
    fn update(&mut self, frame: &GrayFrame) -> bool;
    fn present(&self) -> bool;
    fn reset(&mut self);
    fn last_ratio(&self) -> f64;
}

/// 挥棒检测抽象（状态机依赖；测试可注入可控假实现，对齐 Python conftest FakeSwing）。
pub trait SwingDetect: Send {
    /// 喂入一帧，返回 Started / Ended / None。
    fn update(&mut self, frame: &GrayFrame, frame_idx: u64) -> SwingEvent;
    fn active(&self) -> bool;
    fn trigger_idx(&self) -> i64;
    fn pre_roll_frames(&self) -> u64;
    /// 手动模式：由 manual_start / manual_stop 直接置位（对齐 Python 直接写 _active）。
    fn set_manual_active(&mut self, active: bool, trigger_idx: i64);
    fn reset(&mut self);
    fn last_ratio(&self) -> f64;
    fn last_energy(&self) -> f64;
}

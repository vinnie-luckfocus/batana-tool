//! ROI 挥棒检测：显著运动像素占比阈值触发 + 回落结束。
//! 语义对齐 Python 版 app/detect/swing.py。
//!
//! 判据 = **显著运动像素占比**（motion ratio）：ROI 内帧间差分超过
//! pix_thresh 的像素数 / ROI 总像素数。
//!
//! 为什么不用平均帧差（mean abs diff）：它对运动覆盖面积极度敏感——
//! 真实挥棒中手臂+球棒只占 ROI 的 2–10%，平均到全 ROI 后能量只有 1–5
//! （0–255 量纲），与传感器底噪（~1–3）拉不开差距，阈值没法定。
//! 占比指标先按像素阈值滤掉底噪再计数，静止底噪 ≈0%，挥棒 ≈2–20%，
//! 区分度两个数量级。

use crate::capture::{GrayFrame, Roi};

use super::{crop_roi, SwingDetect};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SwingEvent {
    None,
    Started,
    Ended,
}

pub struct SwingDetector {
    pub roi: Roi,
    pub fps: f64,
    pub pix_thresh: f64,
    pub trigger_ratio: f64,
    pub release_ratio: f64,
    pre_roll_frames: u64,
    post_roll_frames: u64,
    prev: Option<Vec<u8>>,
    active: bool,
    trigger_idx: i64,
    quiet: u64,
    pub last_ratio: f64,  // 显著运动像素占比（0–1），触发判据
    pub last_energy: f64, // 平均帧差（0–255），仅遥测参考
}

impl SwingDetector {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        roi: Roi,
        fps: f64,
        pix_thresh: f64,
        trigger_ratio: f64,
        release_ratio: f64,
        pre_roll_seconds: f64,
        post_roll_seconds: f64,
    ) -> Self {
        Self {
            roi,
            fps,
            pix_thresh,
            trigger_ratio,
            release_ratio,
            pre_roll_frames: ((pre_roll_seconds * fps) + 0.5).max(0.0) as u64,
            post_roll_frames: ((post_roll_seconds * fps) + 0.5).max(1.0) as u64,
            prev: None,
            active: false,
            trigger_idx: -1,
            quiet: 0,
            last_ratio: 0.0,
            last_energy: 0.0,
        }
    }

    pub fn pre_roll_frames(&self) -> u64 {
        self.pre_roll_frames
    }

    /// 返回 (motion_ratio, mean_energy)。
    fn metrics(&mut self, frame_gray: &GrayFrame) -> (f64, f64) {
        let roi_frame = crop_roi(frame_gray, self.roi);
        let prev = match self.prev.take() {
            None => {
                self.prev = Some(roi_frame);
                return (0.0, 0.0);
            }
            Some(p) => p,
        };
        let mut count = 0usize;
        let mut sum = 0u64;
        for (&a, &b) in roi_frame.iter().zip(prev.iter()) {
            let d = a.abs_diff(b);
            sum += d as u64;
            if d as f64 > self.pix_thresh {
                count += 1;
            }
        }
        self.prev = Some(roi_frame.clone());
        let n = roi_frame.len() as f64;
        (count as f64 / n, sum as f64 / n)
    }
}

impl SwingDetect for SwingDetector {
    fn update(&mut self, frame_gray: &GrayFrame, frame_idx: u64) -> SwingEvent {
        let (ratio, energy) = self.metrics(frame_gray);
        self.last_ratio = ratio;
        self.last_energy = energy;
        if !self.active {
            if ratio > self.trigger_ratio {
                self.active = true;
                self.trigger_idx = frame_idx as i64;
                self.quiet = 0;
                return SwingEvent::Started;
            }
            return SwingEvent::None;
        }
        // 挥棒进行中：运动占比回落且持续 post_roll 帧判结束
        if ratio < self.release_ratio {
            self.quiet += 1;
            if self.quiet >= self.post_roll_frames {
                self.active = false;
                return SwingEvent::Ended;
            }
        } else {
            self.quiet = 0;
        }
        SwingEvent::None
    }

    fn active(&self) -> bool {
        self.active
    }

    fn trigger_idx(&self) -> i64 {
        self.trigger_idx
    }

    fn pre_roll_frames(&self) -> u64 {
        self.pre_roll_frames
    }

    fn set_manual_active(&mut self, active: bool, trigger_idx: i64) {
        self.active = active;
        if active {
            self.trigger_idx = trigger_idx;
            self.quiet = 0;
        }
    }

    fn reset(&mut self) {
        self.prev = None;
        self.active = false;
        self.trigger_idx = -1;
        self.quiet = 0;
        self.last_ratio = 0.0;
        self.last_energy = 0.0;
    }

    fn last_ratio(&self) -> f64 {
        self.last_ratio
    }

    fn last_energy(&self) -> f64 {
        self.last_energy
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const FPS: f64 = 10.0;
    const ROI: Roi = (40, 40, 100, 100); // ROI 100x100 = 10000 像素
    const FRAME: (usize, usize) = (240, 200);

    fn empty_frame() -> GrayFrame {
        GrayFrame::new(FRAME.0, FRAME.1, vec![0; FRAME.0 * FRAME.1])
    }

    /// 在 prev 基础上把 ROI 内 [offset, offset+pixels) 像素置 value，控制运动占比。
    /// pixels 个像素帧差 value → 运动占比 = pixels/10000。
    fn energy_frame(prev: &GrayFrame, pixels: usize, offset: usize, value: u8) -> GrayFrame {
        let mut data = prev.data.as_ref().clone();
        // ROI (40,40,100,100) 按行展平后的 flat 下标 → 帧坐标
        for k in offset..offset + pixels {
            let row = 40 + k / 100;
            let col = 40 + k % 100;
            data[row * FRAME.0 + col] = value;
        }
        GrayFrame::new(FRAME.0, FRAME.1, data)
    }

    #[test]
    fn swing_trigger_and_end() {
        let mut det = SwingDetector::new(ROI, FPS, 25.0, 0.05, 0.03, 1.0, 0.5);
        let base = empty_frame();
        assert_eq!(det.update(&base, 0), SwingEvent::None); // 首帧无帧差
        assert_eq!(det.update(&base, 1), SwingEvent::None); // 静止
        // 占比 = 784/10000 ≈ 7.8% > 5% → 触发
        let burst = energy_frame(&base, 784, 0, 255);
        assert_eq!(det.update(&burst, 2), SwingEvent::Started);
        assert!(det.active());
        assert_eq!(det.trigger_idx(), 2);
        assert!((det.last_ratio - 0.0784).abs() < 0.001);
        // 持续运动（每帧画面都在变）：不结束（post_roll=5 帧）
        assert_eq!(det.update(&energy_frame(&base, 392, 0, 255), 3), SwingEvent::None); // 392px ≈ 3.9% > 3%
        assert_eq!(det.update(&energy_frame(&base, 784, 392, 255), 4), SwingEvent::None); // 另一区域点亮
        assert_eq!(det.update(&energy_frame(&base, 392, 0, 255), 5), SwingEvent::None);
        // 画面静止不变：占比 0 < 3%，持续 5 帧 → 第 5 帧结束
        let still = energy_frame(&base, 392, 0, 255);
        for i in 0..4 {
            assert_eq!(det.update(&still, 6 + i), SwingEvent::None);
        }
        assert_eq!(det.update(&still, 10), SwingEvent::Ended);
        assert!(!det.active());
    }

    #[test]
    fn swing_no_false_trigger_on_small_noise() {
        let mut det = SwingDetector::new(ROI, FPS, 25.0, 0.02, 0.008, 1.0, 1.0);
        let base = empty_frame();
        det.update(&base, 0);
        let mut prev = base;
        for i in 1..20u64 {
            // 交替点亮两个 50px 区域：每帧 100px 变化 = 1% < 2%，小幅扰动不触发
            let cur = energy_frame(&prev, 50, 50 * (i as usize % 2), 255);
            assert_eq!(det.update(&cur, i), SwingEvent::None);
            prev = cur;
        }
        assert!(!det.active());
    }

    #[test]
    fn swing_ignores_subthreshold_pixel_noise() {
        // 底噪免疫：大面积但低对比度变化（帧差 < pix_thresh）不触发。
        let mut det = SwingDetector::new(ROI, FPS, 25.0, 0.02, 0.008, 1.0, 1.0);
        let base = empty_frame();
        det.update(&base, 0);
        let mut prev = base;
        for i in 1..20u64 {
            // 50% 画面帧差仅 20（< 25）：占比判据为 0，不触发
            let cur = energy_frame(&prev, 5000, 0, 20);
            assert_eq!(det.update(&cur, i), SwingEvent::None);
            prev = cur;
        }
        assert!(!det.active());
    }

    #[test]
    fn swing_energy_threshold_boundary() {
        let mut det = SwingDetector::new(ROI, FPS, 25.0, 0.04, 0.01, 1.0, 1.0);
        let base = empty_frame();
        det.update(&base, 0);
        // 恰低于阈值：380px = 3.8% < 4% 不触发
        let just_below = energy_frame(&base, 380, 0, 255);
        assert_eq!(det.update(&just_below, 1), SwingEvent::None);
        assert!(!det.active());
        // 高于阈值：470px = 4.7% > 4% 触发
        let mut det2 = SwingDetector::new(ROI, FPS, 25.0, 0.04, 0.01, 1.0, 1.0);
        det2.update(&base, 0);
        let above = energy_frame(&base, 470, 0, 255);
        assert_eq!(det2.update(&above, 1), SwingEvent::Started);
    }

    #[test]
    fn swing_quiet_counter_resets_on_energy() {
        let mut det = SwingDetector::new(ROI, FPS, 25.0, 0.02, 0.015, 1.0, 0.3);
        let base = empty_frame();
        det.update(&base, 0);
        let burst = energy_frame(&base, 784, 0, 255); // 7.8%
        assert_eq!(det.update(&burst, 1), SwingEvent::Started);
        let quiet = empty_frame();
        // 安静 2 帧后又来一波运动 → quiet 计数清零，不结束
        det.update(&quiet, 2);
        det.update(&quiet, 3);
        let burst2 = energy_frame(&quiet, 470, 0, 255);
        assert_eq!(det.update(&burst2, 4), SwingEvent::None); // 4.7% > 1.5%，quiet 清零
        // 之后保持 burst2 画面不变（占比 0 < 1.5%），3 帧安静后结束
        for i in 0..2 {
            assert_eq!(det.update(&burst2, 5 + i), SwingEvent::None);
        }
        assert_eq!(det.update(&burst2, 7), SwingEvent::Ended); // post_roll = 3 帧
    }

    #[test]
    fn swing_pre_post_roll_frames_conversion() {
        let det = SwingDetector::new(ROI, 120.0, 25.0, 0.02, 0.008, 1.0, 1.0);
        assert_eq!(det.pre_roll_frames(), 120);
        assert_eq!(det.post_roll_frames, 120);
    }
}

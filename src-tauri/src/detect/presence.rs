//! ROI 就位检测：简化背景建模（无 OpenCV 依赖）。
//!
//! 模型：空场景学习静态背景均值帧（逐像素滑动均值），
//! 前景 = absdiff > 25 的像素占 ROI 比例。
//!
//! 学习策略（语义对齐 Python MOG2 版，针对线性均值模型做等价改造）：
//! - 就位后冻结：**人是来挥棒的，就位后必然长时间静止**；持续学习会把静止人形
//!   吸进背景，导致误报「人员离开」。冻结后静止人员永远保持前景
//!   （1500 帧回归用例锁定此语义）；
//! - 空场景快学习（idle_learning_rate，默认 0.08）：本帧判为空（占比低于阈值）时
//!   全像素快学——人离开后快速把空场景（含残影）学进背景；
//! - 入场/占用慢学习（learning_rate，默认 0.002）：本帧非空时仅慢学**非前景像素**。
//!   MOG2 靠混合高斯天然区分「人形/空场景」双峰，线性均值做不到——若对前景像素
//!   也学习，断续出现的人形会被学进背景（「断断续续出现永不就位」回归失败）。
//!   只学背景像素既吸收光照漂移，又永远不会把人形学进背景。

use crate::capture::{GrayFrame, Roi};

use super::{crop_roi, PresenceDetect};

/// 前景判定的帧差阈值（与 Python 版 absdiff>25 一致）
const FG_PIX_THRESH: f32 = 25.0;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PresenceMethod {
    /// 背景均值建模（默认，对应 Python "mog2" 的简化替代）
    BgMean,
    /// 帧差法（对应 Python "framediff"）
    FrameDiff,
}

pub struct PresenceDetector {
    pub roi: Roi,
    pub fps: f64,
    pub ratio_thresh: f64,
    stable_frames: usize,
    absent_frames: usize,
    method: PresenceMethod,
    learning_rate: f32,
    idle_learning_rate: f32,
    bg: Vec<f32>,          // ROI 尺寸 w*h 的背景均值帧
    bg_init: bool,
    prev: Option<Vec<u8>>, // framediff 法的上一帧 ROI
    hit: usize,
    miss: usize,
    present: bool,
    pub last_ratio: f64,
}

impl PresenceDetector {
    pub fn new(
        roi: Roi,
        fps: f64,
        ratio_thresh: f64,
        stable_seconds: f64,
        absent_seconds: f64,
        method: PresenceMethod,
        learning_rate: f64,
        idle_learning_rate: f64,
    ) -> Self {
        Self {
            roi,
            fps,
            ratio_thresh,
            stable_frames: ((stable_seconds * fps) + 0.5).max(1.0) as usize,
            absent_frames: ((absent_seconds * fps) + 0.5).max(1.0) as usize,
            method,
            learning_rate: learning_rate as f32,
            idle_learning_rate: idle_learning_rate as f32,
            bg: Vec::new(),
            bg_init: false,
            prev: None,
            hit: 0,
            miss: 0,
            present: false,
            last_ratio: 0.0,
        }
    }

    pub fn present(&self) -> bool {
        self.present
    }

    /// 计算当前帧 ROI 前景占比（内部按策略更新背景）。
    fn foreground_ratio(&mut self, frame_gray: &GrayFrame) -> f64 {
        let roi_frame = crop_roi(frame_gray, self.roi);
        match self.method {
            PresenceMethod::BgMean => {
                if !self.bg_init {
                    // 首帧直接作为背景初值，避免启动瞬态
                    self.bg = roi_frame.iter().map(|&v| v as f32).collect();
                    self.bg_init = true;
                    return 0.0;
                }
                // 1) 先用当前背景分类
                let mut count = 0usize;
                for (i, &px) in roi_frame.iter().enumerate() {
                    if (px as f32 - self.bg[i]).abs() > FG_PIX_THRESH {
                        count += 1;
                    }
                }
                let ratio = count as f64 / roi_frame.len() as f64;
                // 2) 再按本帧分类结果学习（策略见模块文档）
                let occupied = ratio > self.ratio_thresh;
                let lr = if self.present {
                    0.0 // 就位后冻结：静止人员不被吸进背景
                } else if occupied {
                    self.learning_rate // 入场慢学：仅非前景像素
                } else {
                    self.idle_learning_rate // 空场景快学：全像素
                };
                if lr > 0.0 {
                    let skip_fg = occupied; // 占用帧不学前景象素（防人形入背景）
                    for (i, &px) in roi_frame.iter().enumerate() {
                        let bg = self.bg[i];
                        if skip_fg && (px as f32 - bg).abs() > FG_PIX_THRESH {
                            continue;
                        }
                        self.bg[i] = bg + lr * (px as f32 - bg);
                    }
                }
                ratio
            }
            PresenceMethod::FrameDiff => {
                let prev = match self.prev.take() {
                    None => {
                        self.prev = Some(roi_frame);
                        return 0.0;
                    }
                    Some(p) => p,
                };
                let count = roi_frame
                    .iter()
                    .zip(prev.iter())
                    .filter(|(&a, &b)| a.abs_diff(b) > FG_PIX_THRESH as u8)
                    .count();
                self.prev = Some(roi_frame.clone());
                count as f64 / roi_frame.len() as f64
            }
        }
    }
}

impl PresenceDetect for PresenceDetector {
    fn update(&mut self, frame_gray: &GrayFrame) -> bool {
        let ratio = self.foreground_ratio(frame_gray);
        self.last_ratio = ratio;
        let occupied = ratio > self.ratio_thresh;
        if occupied {
            self.hit += 1;
            self.miss = 0;
        } else {
            self.miss += 1;
            self.hit = 0;
        }
        if !self.present && self.hit >= self.stable_frames {
            self.present = true;
        } else if self.present && self.miss >= self.absent_frames {
            self.present = false;
        }
        self.present
    }

    fn present(&self) -> bool {
        self.present
    }

    fn reset(&mut self) {
        self.hit = 0;
        self.miss = 0;
        self.present = false;
        self.last_ratio = 0.0;
        self.bg.clear();
        self.bg_init = false;
        self.prev = None;
    }

    fn last_ratio(&self) -> f64 {
        self.last_ratio
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const FPS: f64 = 10.0;
    const ROI: Roi = (40, 40, 100, 100); // 100x100 的 ROI
    const FRAME: (usize, usize) = (240, 200); // (宽, 高)

    fn empty_frame() -> GrayFrame {
        GrayFrame::new(FRAME.0, FRAME.1, vec![0; FRAME.0 * FRAME.1])
    }

    fn frame_with_block(x: usize, y: usize, w: usize, h: usize, value: u8) -> GrayFrame {
        let mut f = vec![0u8; FRAME.0 * FRAME.1];
        for row in y..y + h {
            for col in x..x + w {
                f[row * FRAME.0 + col] = value;
            }
        }
        GrayFrame::new(FRAME.0, FRAME.1, f)
    }

    fn make_det(ratio_thresh: f64, stable_seconds: f64, absent_seconds: f64) -> PresenceDetector {
        PresenceDetector::new(
            ROI,
            FPS,
            ratio_thresh,
            stable_seconds,
            absent_seconds,
            PresenceMethod::BgMean,
            0.002,
            0.08,
        )
    }

    fn learn_background(det: &mut PresenceDetector, frames: usize) {
        for _ in 0..frames {
            det.update(&empty_frame());
        }
    }

    #[test]
    fn presence_settles_after_stable_frames() {
        let mut det = make_det(0.05, 0.5, 0.6);
        learn_background(&mut det, 20);
        // ROI 内放 50% 占比的块 → 前景占比 0.5 > 0.05
        let occupied = frame_with_block(40, 40, 50, 100, 255);
        for i in 0..4 {
            // stable_frames = 0.5*10 = 5，前 4 帧不就位
            assert!(!det.update(&occupied), "第 {} 帧不应就位", i + 1);
        }
        assert!(det.update(&occupied));
        assert!((det.last_ratio - 0.5).abs() < 0.01);
    }

    #[test]
    fn presence_not_settled_if_unstable() {
        let mut det = make_det(0.05, 0.5, 1.0);
        learn_background(&mut det, 20);
        let occupied = frame_with_block(40, 40, 50, 100, 255);
        // 断断续续出现，永远凑不够连续 5 帧
        for _ in 0..5 {
            for _ in 0..4 {
                det.update(&occupied);
            }
            det.update(&empty_frame());
            assert!(!det.present());
        }
    }

    #[test]
    fn presence_ratio_boundary() {
        // 占比恰低于阈值（4% < 5%）：不就位
        let mut det = make_det(0.05, 0.3, 1.0);
        learn_background(&mut det, 20);
        let below = frame_with_block(40, 40, 20, 20, 255); // 400/10000 = 4%
        for _ in 0..10 {
            assert!(!det.update(&below));
        }
        // 占比高于阈值（16% > 5%）：就位
        let mut det2 = make_det(0.05, 0.3, 1.0);
        learn_background(&mut det2, 20);
        let above = frame_with_block(40, 40, 40, 40, 255); // 16%
        for _ in 0..3 {
            det2.update(&above);
        }
        assert!(det2.present());
    }

    #[test]
    fn presence_static_person_never_absorbed() {
        // 回归：人就位后长时间静止（远超背景吸收周期）不得误判离场。
        let mut det = make_det(0.05, 0.3, 0.5);
        learn_background(&mut det, 20);
        let occupied = frame_with_block(40, 40, 50, 100, 255);
        for _ in 0..5 {
            det.update(&occupied);
        }
        assert!(det.present());
        // 静止 1500 帧（旧实现持续学习下 ~500 帧即被吸进背景误判离开）
        for _ in 0..1500 {
            assert!(det.update(&occupied), "静止人员被背景模型吸收");
        }
        assert!((det.last_ratio - 0.5).abs() < 0.02);
        // 人离开 → 正常判离场，且空场景快速重学
        for _ in 0..6 {
            // absent_frames = 5
            det.update(&empty_frame());
        }
        assert!(!det.present());
        learn_background(&mut det, 30);
        assert!(det.last_ratio < 0.05);
    }

    #[test]
    fn presence_leaves_after_absent_timeout() {
        let mut det = make_det(0.05, 0.3, 0.5);
        learn_background(&mut det, 20);
        let occupied = frame_with_block(40, 40, 50, 100, 255);
        for _ in 0..3 {
            det.update(&occupied);
        }
        assert!(det.present());
        // 离开 4 帧（< absent_frames=5）：仍判就位
        for _ in 0..4 {
            assert!(det.update(&empty_frame()));
        }
        // 第 5 帧：离场
        assert!(!det.update(&empty_frame()));
    }

    #[test]
    fn presence_framediff_method() {
        let mut det = PresenceDetector::new(
            ROI,
            FPS,
            0.05,
            0.3,
            1.0,
            PresenceMethod::FrameDiff,
            0.002,
            0.08,
        );
        let occupied = frame_with_block(40, 40, 50, 100, 255);
        // 帧差法依赖帧间变化：交替喂空帧/占用帧，每帧 diff 占比均为 0.5
        det.update(&empty_frame()); // 首帧无 prev
        det.update(&occupied); // diff 0.5，hit 1
        det.update(&empty_frame()); // diff 0.5，hit 2
        assert!(det.update(&occupied)); // hit 3 ≥ stable_frames → 就位
    }
}

//! 预录环形缓冲：按秒 × fps 定容，线程安全，支持按帧序号区间提取。
//! 语义对齐 Python 版 app/capture/ring_buffer.py。

use std::collections::VecDeque;
use std::sync::Mutex;

use super::{BufferItem, GrayFrame};

/// 固定容量环形缓冲，满后覆盖最旧帧（wrap）。
///
/// 容量 = buffer_seconds × fps（四舍五入，至少 1）。供挥棒 pre-roll 回溯使用。
pub struct RingBuffer {
    capacity: usize,
    items: Mutex<VecDeque<BufferItem>>,
}

impl RingBuffer {
    pub fn new(buffer_seconds: f64, fps: f64) -> Self {
        let capacity = ((buffer_seconds * fps) + 0.5) as usize;
        Self {
            capacity: capacity.max(1),
            items: Mutex::new(VecDeque::new()),
        }
    }

    pub fn capacity(&self) -> usize {
        self.capacity
    }

    pub fn len(&self) -> usize {
        self.items.lock().unwrap().len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn push(&self, frame_idx: u64, ts_ns: u64, left: GrayFrame, right: GrayFrame) {
        let mut items = self.items.lock().unwrap();
        if items.len() >= self.capacity {
            items.pop_front();
        }
        items.push_back((frame_idx, ts_ns, left, right));
    }

    pub fn latest(&self) -> Option<BufferItem> {
        self.items.lock().unwrap().back().cloned()
    }

    /// 按帧序号闭区间 [start_idx, end_idx] 提取，已被覆盖的旧帧静默跳过。
    pub fn extract(&self, start_idx: u64, end_idx: u64) -> Vec<BufferItem> {
        self.items
            .lock()
            .unwrap()
            .iter()
            .filter(|item| item.0 >= start_idx && item.0 <= end_idx)
            .cloned()
            .collect()
    }

    /// 当前缓冲覆盖的帧序号区间 (最旧, 最新)，空缓冲返回 None。
    pub fn span(&self) -> Option<(u64, u64)> {
        let items = self.items.lock().unwrap();
        match (items.front(), items.back()) {
            (Some(first), Some(last)) => Some((first.0, last.0)),
            _ => None,
        }
    }

    pub fn clear(&self) {
        self.items.lock().unwrap().clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::capture::GrayFrame;
    use std::sync::Arc;

    fn make_frame(value: u8) -> GrayFrame {
        GrayFrame::new(200, 100, vec![value; 200 * 100])
    }

    fn push_n(buf: &RingBuffer, start: u64, count: u64) {
        for i in start..start + count {
            buf.push(
                i,
                i * 1_000_000,
                make_frame((i % 255) as u8),
                make_frame(((i * 2) % 255) as u8),
            );
        }
    }

    #[test]
    fn capacity_from_seconds_times_fps() {
        assert_eq!(RingBuffer::new(3.0, 120.0).capacity(), 360);
        assert_eq!(RingBuffer::new(0.5, 10.0).capacity(), 5);
        assert_eq!(RingBuffer::new(0.01, 10.0).capacity(), 1); // 至少 1
    }

    #[test]
    fn push_and_len_until_full() {
        let buf = RingBuffer::new(1.0, 10.0);
        push_n(&buf, 0, 7);
        assert_eq!(buf.len(), 7);
        push_n(&buf, 7, 3);
        assert_eq!(buf.len(), 10); // 满
    }

    #[test]
    fn wrap_overwrites_oldest() {
        let buf = RingBuffer::new(1.0, 10.0);
        push_n(&buf, 0, 10);
        push_n(&buf, 10, 5); // 覆盖 0..4
        assert_eq!(buf.len(), 10);
        assert_eq!(buf.span(), Some((5, 14)));
        assert_eq!(buf.latest().unwrap().0, 14);
    }

    #[test]
    fn extract_closed_interval() {
        let buf = RingBuffer::new(2.0, 10.0);
        push_n(&buf, 0, 15);
        let items = buf.extract(3, 7);
        let idxs: Vec<u64> = items.iter().map(|it| it.0).collect();
        assert_eq!(idxs, vec![3, 4, 5, 6, 7]);
        // 帧内容随 idx 携带
        assert_eq!(items[0].2.at(0, 0), 3);
        assert_eq!(items[0].3.at(0, 0), 6);
    }

    #[test]
    fn extract_skips_overwritten_frames() {
        let buf = RingBuffer::new(1.0, 10.0); // 容量 10
        push_n(&buf, 0, 15); // 覆盖后剩 5..14
        let items = buf.extract(0, 14);
        let idxs: Vec<u64> = items.iter().map(|it| it.0).collect();
        assert_eq!(idxs, (5..15).collect::<Vec<u64>>());
    }

    #[test]
    fn extract_empty_and_out_of_range() {
        let buf = RingBuffer::new(1.0, 10.0);
        assert!(buf.extract(0, 10).is_empty());
        assert_eq!(buf.span(), None);
        assert!(buf.latest().is_none());
        push_n(&buf, 0, 5);
        assert!(buf.extract(100, 200).is_empty());
    }

    #[test]
    fn thread_safety_concurrent_push_extract() {
        let buf = Arc::new(RingBuffer::new(2.0, 50.0)); // 容量 100
        let mut handles = vec![];
        for offset in [0u64, 1000] {
            let b = Arc::clone(&buf);
            handles.push(std::thread::spawn(move || push_n(&b, offset, 500)));
        }
        for _ in 0..2 {
            let b = Arc::clone(&buf);
            handles.push(std::thread::spawn(move || {
                for _ in 0..500 {
                    b.extract(0, 10_000);
                    b.span();
                }
            }));
        }
        for h in handles {
            h.join().unwrap();
        }
        assert_eq!(buf.len(), 100); // 最终满容量，无越界
    }
}

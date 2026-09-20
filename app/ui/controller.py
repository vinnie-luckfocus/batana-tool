"""采集控制器：帧源线程 + 状态机驱动 + Qt 信号桥（PRD F1/F5/F6 的 UI 侧）。

线程模型：
    _CaptureThread（QThread）：逐帧读帧源 → 状态机 feed_frame → 片段落盘
    CaptureController（QObject，主线程创建）：信号转发给 UI，手动控制加锁
状态机 listener 在构造时注册为 Qt 信号发射；跨线程连接自动走队列，
无头测试同线程调用 feed() 时信号同步直达。
"""

from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np
from PySide6.QtCore import QThread, QObject, Signal

from app.capture import ClipWriter, FrameSource, RingBuffer, UvcSource, split_sbs
from app.detect import CaptureStateMachine, Clip, PresenceDetector, Roi, SwingDetector
from app.envcheck import EnvCheckSettings, check_brightness, check_flicker
from app.envcheck.models import CheckResult
from app.session import SessionStore, new_session_id
from app.ui.settings import AppSettings
from app.voice import NullVoice, SayVoice, Voice

PREVIEW_FPS = 30.0  # 预览降频目标（PRD：120fps 流下预览 30fps 显示）
_ENV_SAMPLE_STRIDE = 4  # 持续环境监测：每 4 帧采一次亮度样本（120fps → 30 样本/秒）


class _CaptureThread(QThread):
    """抓帧线程：帧源 → SBS 切分 → 控制器 feed（含预览降频与遥测）。"""

    def __init__(self, controller: "CaptureController", source: FrameSource) -> None:
        super().__init__()
        self._ctl = controller
        self._source = source

    def run(self) -> None:
        ctl = self._ctl
        stride = max(1, round(self._source.fps / PREVIEW_FPS))
        n = 0
        t0 = time.monotonic()
        try:
            for idx, ts_ns, sbs in self._source.frames():
                if self.isInterruptionRequested():
                    break
                left, right = split_sbs(sbs)
                ctl.feed(idx, ts_ns, left, right)
                if idx % stride == 0:
                    ctl.preview_ready.emit((idx, ts_ns, left.copy(), right.copy()))
                n += 1
                now = time.monotonic()
                if now > t0:
                    ctl._measured_fps = n / (now - t0)
        except Exception as e:  # 相机断开 / 文件损坏 → 状态机 ERROR
            ctl.error_occurred.emit(str(e))
        finally:
            self._source.close()
            ctl.camera_active.emit(False)


class CaptureController(QObject):
    """采集编排控制器：UI 与核心层之间的唯一桥梁。"""

    # (frame_idx, ts_ns, 左目帧, 右目帧)，已按 PREVIEW_FPS 降频
    preview_ready = Signal(object)
    # 状态机迁移（listener 桥接）
    transitioned = Signal(object)
    # (实测帧率, 就位占比, 运动能量)
    telemetry = Signal(float, float, float)
    # SessionStore 新登记记录（dict）
    clip_saved = Signal(object)
    error_occurred = Signal(str)
    # 相机/帧源连接状态（终端绿指示灯唯一数据源）
    camera_active = Signal(bool)

    def __init__(
        self,
        settings: AppSettings,
        store: SessionStore,
        source: FrameSource | None = None,
        presence: PresenceDetector | None = None,
        swing: SwingDetector | None = None,
        voice: Voice | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.store = store
        self._source = source
        self._voice = voice if voice is not None else self._make_voice()
        self._writer = ClipWriter(fps=settings.capture_fps)
        self._lock = threading.Lock()
        self._thread: _CaptureThread | None = None
        self._paused = False
        self._measured_fps = 0.0
        self._last_frame: tuple[np.ndarray, np.ndarray] | None = None
        # F11 持续监测：滚动亮度样本（ROI 均值 / 整帧均值），保留约 3 秒窗口
        self._env_roi_means: deque[float] = deque(maxlen=512)
        self._env_frame_means: deque[float] = deque(maxlen=512)

        fps = float(settings.capture_fps)
        # 实际驱动帧率：文件源以文件为准（start 时刷新），否则用采集设置
        self._active_fps = float(source.fps) if source is not None else fps
        roi = settings.roi_tuple()
        self._presence = presence  # None 时待首帧按画面尺寸建默认 ROI
        self._swing = swing
        self._detector_roi = roi
        self._owns_detectors = presence is None and swing is None
        # 注入检测器时（测试/回放复用）立即建状态机；否则等首帧确定默认 ROI 后懒建
        self._sm: CaptureStateMachine | None = None
        if presence is not None and swing is not None:
            self._sm = self._make_state_machine(fps, roi, presence=presence, swing=swing)

    # ---- 构造辅助 ----

    def _make_voice(self) -> Voice:
        if self.settings.voice_enabled:
            try:
                return SayVoice(rate=self.settings.voice_rate)
            except Exception:
                pass
        return NullVoice()

    def _make_state_machine(
        self,
        fps: float,
        roi: Roi | None,
        presence: PresenceDetector | None = None,
        swing: SwingDetector | None = None,
    ) -> CaptureStateMachine:
        s = self.settings
        roi = roi or (0, 0, 1, 1)
        presence = presence or PresenceDetector(roi, fps, ratio_thresh=s.presence_ratio)
        swing = swing or SwingDetector(
            roi, fps,
            trigger_thresh=s.energy_trigger,
            release_thresh=s.energy_release,
            pre_roll_seconds=s.pre_roll_seconds,
            post_roll_seconds=s.post_roll_seconds,
        )
        self._presence = presence
        self._swing = swing
        sm = CaptureStateMachine(
            presence, swing, RingBuffer(s.buffer_seconds, fps), fps,
            voice=self._voice, clip_saver=self._save_clip,
            countdown_seconds=s.countdown_seconds,
        )
        sm.add_listener(self.transitioned.emit)
        return sm

    def _build_source(self) -> FrameSource:
        if self._source is not None:
            return self._source
        s = self.settings
        return UvcSource(
            device_index=s.camera_index,
            width=s.capture_width, height=s.capture_height,
            fps=s.capture_fps, pixel_format=s.pixel_format,
        )

    # ---- 属性 ----

    @property
    def state_machine(self) -> CaptureStateMachine:
        """状态机（懒构建：无显式检测器时等首帧确定 ROI）。"""
        return self._sm

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    # ---- 帧喂入（抓帧线程与无头测试共用） ----

    def feed(self, frame_idx: int, ts_ns: int, left: np.ndarray, right: np.ndarray) -> None:
        """喂入一帧：懒建状态机 → 驱动状态机 → 发遥测。"""
        with self._lock:
            if self._sm is None:
                h, w = left.shape[:2]
                roi = self.settings.roi_tuple() or (w // 4, h // 8, w // 2, h * 3 // 4)
                self._detector_roi = roi
                self._sm = self._make_state_machine(self._active_fps, roi)
            self._last_frame = (left, right)
            if not self._paused:
                self._sm.feed_frame(frame_idx, ts_ns, left, right)
            if frame_idx % _ENV_SAMPLE_STRIDE == 0:
                self._sample_env(left)
            self.telemetry.emit(
                self._measured_fps,
                self._presence.last_ratio if self._presence else 0.0,
                self._swing.last_energy if self._swing else 0.0,
            )

    # ---- F11 持续环境监测（亮度/频闪，轻量） ----

    def _sample_env(self, left: np.ndarray) -> None:
        """每 _ENV_SAMPLE_STRIDE 帧采一次亮度样本（抓帧线程内，开销可忽略）。"""
        roi = self._detector_roi
        if roi is not None:
            x, y, w, h = roi
            self._env_roi_means.append(float(left[y : y + h, x : x + w].mean()))
        self._env_frame_means.append(float(left.mean()))

    def evaluate_environment(self) -> list[CheckResult]:
        """用最近约 1 秒的亮度样本跑亮度+频闪检查，返回非 pass 的结果列表。

        由 UI 定时器（~5s）调用；样本不足（采集刚开始）时返回空列表。
        """
        with self._lock:
            roi_means = list(self._env_roi_means)
            frame_means = list(self._env_frame_means)
        sample_fps = self._active_fps / _ENV_SAMPLE_STRIDE
        window = max(8, int(sample_fps))  # 最近约 1 秒
        if len(frame_means) < window:
            return []
        cfg = EnvCheckSettings.from_app_settings(self.settings)
        results = [
            check_brightness(
                roi_means[-window:], None,
                fail_below=cfg.brightness_fail, warn_below=cfg.brightness_warn,
                high=cfg.brightness_high, highlight_ratio=cfg.highlight_ratio,
            ),
            check_flicker(
                frame_means[-window:], sample_fps,
                warn_pct=cfg.flicker_warn_pct, fail_pct=cfg.flicker_fail_pct,
                mains_tol_hz=cfg.mains_tol_hz,
            ),
        ]
        return [r for r in results if r.status != "pass"]

    # ---- 控制 ----

    def start(self) -> bool:
        """开始采集：起抓帧线程。返回是否成功启动。"""
        if self.running:
            return True
        try:
            source = self._build_source()
        except Exception as e:
            self.error_occurred.emit(str(e))
            return False
        # 文件源帧率以文件为准：自建检测器时状态机随首帧按新 fps 懒重建
        fps = float(source.fps)
        self._active_fps = fps
        if self._sm is not None and abs(fps - self._sm.fps) > 1e-6 and self._owns_detectors:
            self._sm = None
        self._writer = ClipWriter(fps=fps)
        self._paused = False
        self._thread = _CaptureThread(self, source)
        self._thread.start()
        self.camera_active.emit(True)
        return True

    def pause(self) -> None:
        """暂停采集（预览不断流，状态机回 IDLE）。"""
        with self._lock:
            self._paused = True
            if self._sm is not None:
                self._sm.pause()

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    def stop(self) -> None:
        """停止采集：中断线程并关闭帧源。"""
        if self._thread is not None:
            self._thread.requestInterruption()
            self._thread.wait(3000)
            self._thread = None
        self.camera_active.emit(False)

    def manual_start(self) -> None:
        with self._lock:
            if self._sm is not None:
                self._sm.manual_start()

    def manual_stop(self) -> None:
        with self._lock:
            if self._sm is not None:
                self._sm.manual_stop()

    def discard(self) -> None:
        with self._lock:
            if self._sm is not None:
                self._sm.discard()

    def set_muted(self, muted: bool) -> None:
        self._voice.set_muted(muted)

    def update_roi(self, roi: Roi) -> None:
        """ROI 框选变更：持久化到 settings.json，自建检测器时热更新状态机。"""
        with self._lock:
            self._detector_roi = roi
            self.settings.set_roi(roi)
            self.settings.save()
            if self._sm is not None and self._owns_detectors:
                self._sm = self._make_state_machine(self._sm.fps, roi)

    def close(self) -> None:
        self.stop()
        self._voice.close()

    # ---- 片段落盘（clip_saver，在抓帧线程内同步执行） ----

    def _save_clip(self, clip: Clip) -> None:
        session_id = new_session_id()
        out_dir = self.store.sessions_dir / session_id
        paths = self._writer.write_clip(clip.frames(), out_dir, meta={
            "source": "ui-capture",
            "roi": list(self._detector_roi) if self._detector_roi else None,
        })
        record = self.store.add_clip(
            session_id=session_id,
            clip_dir=paths.out_dir,
            frame_count=paths.frame_count,
            fps=self._writer.fps,
            trigger_idx=clip.trigger_idx,
        )
        self.clip_saved.emit(record)

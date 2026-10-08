"""采集控制器：帧源线程 + 状态机驱动 + Qt 信号桥（PRD F1/F5/F6 的 UI 侧）。

线程模型：
    _CaptureThread（QThread）：逐帧读帧源 → 状态机 feed_frame
    _SaveWorker（QThread）：片段编码落盘队列消费（M1：保存不阻塞抓帧）
    CaptureController（QObject，主线程创建）：信号转发给 UI，手动控制加锁
状态机 listener 在构造时注册为 Qt 信号发射；跨线程连接自动走队列，
无头测试同线程调用 feed() 时信号同步直达。
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque

import numpy as np
from PySide6.QtCore import QThread, QObject, Signal

from app.capture import (
    ClipWriter,
    FfmpegUvcSource,
    FrameSource,
    RingBuffer,
    UvcSource,
    list_video_devices,
    split_sbs,
)
from app.capture.ring_buffer import BufferItem
from app.detect import CaptureStateMachine, Clip, PresenceDetector, Roi, State, SwingDetector
from app.envcheck import EnvCheckSettings, check_brightness, check_flicker
from app.envcheck.models import CheckResult
from app.session import SessionStore, new_session_id
from app.ui.settings import AppSettings
from app.voice import NullVoice, SayVoice, Voice, error_prompt

PREVIEW_FPS = 30.0  # 预览降频目标（PRD：120fps 流下预览 30fps 显示）
_ENV_SAMPLE_STRIDE = 4  # 持续环境监测：每 4 帧采一次亮度样本（120fps → 30 样本/秒）

# 落盘任务 = (片段, 已提取帧序列, 编码帧率, 检测 ROI)
_SaveJob = tuple[Clip, list[BufferItem], float, "Roi | None"]


# 模组已知主档位（HBVCAM-W2237-2）：旧设置里的假想规格（如 2560x800）
# 协商失败时自动纠正到此模式，保证相机总能开起来
_FALLBACK_MODE = (1280, 400, 120.0)


def build_uvc_source(s: AppSettings) -> FrameSource:
    """按设置构建 UVC 帧源：macOS 下 auto 优先 ffmpeg 后端（OpenCV 协商会静默
    回退到 1280x720@30），ffmpeg 缺失时回退 OpenCV。

    容错：配置模式协商失败时自动改用 _FALLBACK_MODE 重试并回写设置；
    OpenCV 回退按设备名解析最新序号（USB 枚举顺序会漂移，不能迷信存盘的 index）。
    """
    backend = s.capture_backend
    if backend in ("auto", "ffmpeg") and FfmpegUvcSource.available():
        try:
            return FfmpegUvcSource(
                device=s.camera_name,
                width=s.capture_width, height=s.capture_height,
                fps=s.capture_fps,
            )
        except RuntimeError:
            if backend == "ffmpeg":
                raise
            w, h, fps = _FALLBACK_MODE
            if (s.capture_width, s.capture_height, s.capture_fps) != (w, h, fps):
                try:
                    src = FfmpegUvcSource(device=s.camera_name, width=w, height=h, fps=fps)
                except RuntimeError:
                    pass
                else:
                    s.capture_width, s.capture_height, s.capture_fps = w, h, fps
                    s.save()
                    return src
    index = s.camera_index
    for d in list_video_devices():
        if d.name == s.camera_name:
            index = d.index
            break
    return UvcSource(
        device_index=index,
        width=s.capture_width, height=s.capture_height,
        fps=s.capture_fps, pixel_format=s.pixel_format,
    )


class _SaveWorker(QThread):
    """片段落盘 worker：抓帧线程只入队，编码/写盘在此线程串行消费（PRD §5 EncodeWorker）。"""

    saved = Signal(object)   # 登记后的 record dict
    failed = Signal(str)     # 落盘异常消息

    def __init__(self, store: SessionStore, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._queue: queue.Queue[_SaveJob | None] = queue.Queue()

    def enqueue(self, job: _SaveJob) -> None:
        self._queue.put(job)

    def run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:  # 关停哨兵
                    return
                self._process(job)
            except Exception as e:
                self.failed.emit(str(e))
            finally:
                self._queue.task_done()

    def _process(self, job: _SaveJob) -> None:
        clip, items, fps, roi = job
        session_id = new_session_id()
        out_dir = self._store.sessions_dir / session_id
        paths = ClipWriter(fps=fps).write_clip(items, out_dir, meta={
            "source": "ui-capture",
            "roi": list(roi) if roi else None,
        })
        record = self._store.add_clip(
            session_id=session_id,
            clip_dir=paths.out_dir,
            frame_count=paths.frame_count,
            fps=fps,
            trigger_idx=clip.trigger_idx - clip.start_idx,  # 段内相对帧号
        )
        self.saved.emit(record)

    def shutdown(self, timeout_ms: int = 15000) -> None:
        """排空队列后退出（哨兵排在既有任务之后，不丢片段）。"""
        self._queue.put(None)
        self.wait(timeout_ms)


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
        except Exception as e:  # 相机断开 / 文件损坏 → 状态机 ERROR + 分类语音
            ctl._report_error(str(e))
        finally:
            self._source.close()
            ctl.camera_active.emit(False)


class CaptureController(QObject):
    """采集编排控制器：UI 与核心层之间的唯一桥梁。"""

    # (frame_idx, ts_ns, 左目帧, 右目帧)，已按 PREVIEW_FPS 降频
    preview_ready = Signal(object)
    # 状态机迁移（listener 桥接）
    transitioned = Signal(object)
    # (实测帧率, 就位占比, 运动占比%)
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
        # M1 落盘 worker：抓帧线程只入队，编码/写盘串行消费不阻塞采集
        self._save_worker = _SaveWorker(store, parent=self)
        self._save_worker.saved.connect(self.clip_saved.emit)
        self._save_worker.failed.connect(
            lambda msg: self._report_error(f"片段落盘失败: {msg}")
        )
        self._save_worker.start()
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
            pix_thresh=s.motion_pix_thresh,
            trigger_ratio=s.motion_trigger_pct / 100.0,
            release_ratio=s.motion_release_pct / 100.0,
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
        return build_uvc_source(self.settings)

    # ---- 属性 ----

    @property
    def state_machine(self) -> CaptureStateMachine:
        """状态机（懒构建：无显式检测器时等首帧确定 ROI）。"""
        return self._sm

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    @property
    def detector_roi(self) -> Roi | None:
        """当前生效的检测 ROI（含首帧懒建的默认框），供预览回显。"""
        return self._detector_roi

    def say(self, text: str, priority: int = 0) -> None:
        """UI 层直接播报（如 ARMED 超时提醒），走与状态机同一 Voice。"""
        self._voice.speak(text, priority=priority)

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
                (self._swing.last_ratio * 100.0) if self._swing else 0.0,
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
        self._save_worker.shutdown()
        self._voice.close()

    # ---- 异常上报（抓帧线程 / 落盘 worker 共用入口） ----

    def _report_error(self, message: str) -> None:
        """异常分类：状态机 ERROR（内部按消息分类语音播报）+ 通知 UI。"""
        with self._lock:
            sm = self._sm
        if sm is not None and sm.state is not State.ERROR:
            sm.error(message)
        elif sm is None:
            self._voice.speak(error_prompt(message), priority=2)
        self.error_occurred.emit(message)

    # ---- 片段落盘（clip_saver，抓帧线程内调用；编码写盘在 worker 线程） ----

    def _save_clip(self, clip: Clip) -> None:
        """即刻从环形缓冲取出帧引用入队（numpy 数组不拷贝，防缓冲被覆盖）。"""
        items = clip.frames()
        self._save_worker.enqueue((clip, items, self._writer.fps, self._detector_roi))

    def flush_saves(self, timeout: float = 10.0) -> bool:
        """等待落盘队列清空（测试 / 关停前用），返回是否已清空。"""
        deadline = time.monotonic() + timeout
        while self._save_worker._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return not self._save_worker._queue.unfinished_tasks

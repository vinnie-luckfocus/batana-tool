"""macOS 系统 `say` 实现的中文 TTS：subprocess 队列化串行播放。"""

from __future__ import annotations

import shutil
import subprocess
import threading
from queue import Empty, PriorityQueue


class SayVoice:
    """调用 macOS `say -v Tingting` 播报中文。

    内部单 worker 线程串行消费优先级队列，语音之间不重叠、不打断采集；
    播报进程异常只记录，不向外抛。
    """

    def __init__(self, voice: str = "Tingting", rate: int | None = None) -> None:
        if shutil.which("say") is None:
            raise RuntimeError("未找到 macOS `say` 命令（本实现仅支持 macOS）")
        self.voice = voice
        self.rate = rate
        self._muted = False
        self._queue: PriorityQueue[tuple[int, int, str]] = PriorityQueue()
        self._seq = 0
        self._closed = False
        self.errors: list[Exception] = []
        self._worker = threading.Thread(target=self._run, name="say-voice", daemon=True)
        self._worker.start()

    def speak(self, text: str, priority: int = 0) -> None:
        if self._muted or self._closed:
            return
        self._seq += 1
        # PriorityQueue 取最小值：优先级取负使大 priority 先出队；同优先级按入队序
        self._queue.put((-priority, self._seq, text))

    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        if muted:
            while True:
                try:
                    self._queue.get_nowait()
                except Empty:
                    break

    def _run(self) -> None:
        while not self._closed:
            try:
                _, _, text = self._queue.get(timeout=0.1)
            except Empty:
                continue
            cmd = ["say", "-v", self.voice]
            if self.rate:
                cmd += ["-r", str(self.rate)]
            cmd.append(text)
            try:
                subprocess.run(cmd, check=False, timeout=30)
            except Exception as e:  # 播报失败不阻断采集
                self.errors.append(e)

    def close(self) -> None:
        self._closed = True
        self._worker.join(timeout=2.0)

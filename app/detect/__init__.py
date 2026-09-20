"""app.detect：ROI 就位检测、挥棒运动能量检测、采集编排状态机。"""

from app.detect.presence import PresenceDetector, Roi, crop_roi
from app.detect.state_machine import CaptureStateMachine, Clip, State, Transition
from app.detect.swing import SwingDetector

__all__ = [
    "CaptureStateMachine",
    "Clip",
    "PresenceDetector",
    "Roi",
    "State",
    "SwingDetector",
    "Transition",
    "crop_roi",
]

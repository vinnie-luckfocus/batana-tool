"""app.envcheck：环境合规主动判定（PRD F11）——体检模型、8 项检查、执行器。"""

from app.envcheck.checks import (
    check_background_motion,
    check_brightness,
    check_disk,
    check_flicker,
    check_framing,
    check_framerate,
    check_level,
    check_sharpness,
)
from app.envcheck.defaults import EnvCheckSettings
from app.envcheck.models import (
    OVERALL_FAIL,
    OVERALL_OK,
    OVERALL_WARN,
    STATUS_FAIL,
    STATUS_LABELS,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    CheckResult,
    EnvironmentReport,
)
from app.envcheck.runner import EnvironmentChecker

__all__ = [
    "CheckResult",
    "EnvCheckSettings",
    "EnvironmentChecker",
    "EnvironmentReport",
    "OVERALL_FAIL",
    "OVERALL_OK",
    "OVERALL_WARN",
    "STATUS_FAIL",
    "STATUS_LABELS",
    "STATUS_PASS",
    "STATUS_SKIP",
    "STATUS_WARN",
    "check_background_motion",
    "check_brightness",
    "check_disk",
    "check_flicker",
    "check_framing",
    "check_framerate",
    "check_level",
    "check_sharpness",
]

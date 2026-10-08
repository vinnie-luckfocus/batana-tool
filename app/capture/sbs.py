"""SBS（side-by-side）双目整帧切分工具。"""

from __future__ import annotations

import numpy as np


def split_sbs(frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """把 2560x800 SBS 整帧对半切为 (左目, 右目)，各 1280x800。

    要求帧宽为偶数；返回的是原图视图（零拷贝），调用方如需修改请自行 copy。
    """
    if frame.ndim not in (2, 3):
        raise ValueError(f"不支持的帧维度: {frame.shape}")
    width = frame.shape[1]
    if width % 2 != 0:
        raise ValueError(f"SBS 帧宽必须为偶数，实际: {width}")
    mid = width // 2
    return frame[:, :mid], frame[:, mid:]

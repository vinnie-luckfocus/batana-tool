"""ULID 风格会话 id 生成：sess_ 前缀 + 26 位 Crockford Base32（对齐契约）。"""

from __future__ import annotations

import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # 无 ILOU


def new_session_id(now_ms: int | None = None) -> str:
    """生成 sess_ + 26 位 Crockford Base32：48 位毫秒时间 + 80 位随机。"""
    ts = int(now_ms if now_ms is not None else time.time() * 1000) & ((1 << 48) - 1)
    rand = secrets.randbits(80)
    value = (ts << 80) | rand
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "sess_" + "".join(reversed(chars))

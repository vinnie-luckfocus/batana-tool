//! ULID 风格会话 id 生成：sess_ 前缀 + 26 位 Crockford Base32（对齐契约）。
//! 语义对齐 Python 版 app/session/ulid.py。

use std::time::{SystemTime, UNIX_EPOCH};

/// Crockford Base32 字母表（无 ILOU）
const CROCKFORD: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";

/// 生成 sess_ + 26 位 Crockford Base32：48 位毫秒时间 + 80 位随机。
pub fn new_session_id() -> String {
    let now_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0);
    new_session_id_at(now_ms)
}

pub fn new_session_id_at(now_ms: u64) -> String {
    let ts = now_ms & ((1u64 << 48) - 1);
    let mut rand_bytes = [0u8; 10]; // 80 位随机
    if getrandom::fill(&mut rand_bytes).is_err() {
        // 极端兜底：进程随机性不可用时退化为时间+地址熵（不静默返回弱 id 的概率极低）
        rand_bytes = (SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos()
            ^ (&rand_bytes as *const _ as usize) as u128)
            .to_le_bytes()[..10]
            .try_into()
            .unwrap();
    }
    // 128 位容器：高 48 位时间 | 低 80 位随机
    let mut rand80: u128 = 0;
    for b in rand_bytes {
        rand80 = (rand80 << 8) | b as u128;
    }
    let mut value: u128 = ((ts as u128) << 80) | rand80;

    let mut chars = [0u8; 26];
    for c in chars.iter_mut().rev() {
        *c = CROCKFORD[(value & 31) as usize];
        value >>= 5;
    }
    format!("sess_{}", String::from_utf8(chars.to_vec()).unwrap())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn is_crockford(s: &str) -> bool {
        s.bytes().all(|b| CROCKFORD.contains(&b))
    }

    #[test]
    fn session_id_format() {
        let sid = new_session_id();
        assert!(sid.starts_with("sess_"));
        let body = &sid[5..];
        assert_eq!(body.len(), 26);
        assert!(is_crockford(body), "含非 Crockford 字符: {sid}");
        assert_ne!(new_session_id(), new_session_id());
    }

    #[test]
    fn session_id_timestamp_prefix_monotonic() {
        // 同一 26 位串的前 10 字符承载 48 位毫秒时间：较晚时间字典序更大
        let a = new_session_id_at(1_000_000);
        let b = new_session_id_at(2_000_000);
        assert!(a[5..15] < b[5..15]);
    }
}

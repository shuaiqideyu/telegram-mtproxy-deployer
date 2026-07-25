from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def format_iso_to_beijing(raw: str) -> str:
    """将 ISO 时间字符串转为北京时间字符串，失败时返回原值。"""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        dt_cn = dt.astimezone(ZoneInfo("Asia/Shanghai"))
        return dt_cn.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return raw


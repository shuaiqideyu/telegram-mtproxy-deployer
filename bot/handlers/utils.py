from __future__ import annotations

import html

from telegram import InlineKeyboardButton

ERROR_MAP: list[tuple[str, str]] = [
    ("permission denied", "SSH 认证失败：用户名或密码错误"),
    ("authentication failed", "SSH 认证失败：用户名或密码错误"),
    ("auth failed", "SSH 认证失败：用户名或密码错误"),
    ("connection refused", "SSH 连接被拒绝：请检查端口和防火墙"),
    ("no route to host", "无法连接到服务器：网络不可达"),
    ("host is unreachable", "无法连接到服务器：网络不可达"),
    ("timed out", "连接超时：服务器无响应"),
    ("operation timed out", "连接超时：服务器无响应"),
    ("ssh 连接超时", "SSH 连接超时：请检查 IP、端口和安全组"),
    ("network is unreachable", "网络不可达：请检查服务器网络配置"),
    ("name or service not known", "域名解析失败：请检查服务器地址"),
    ("connection reset", "连接被服务器重置"),
    ("broken pipe", "连接中断：SSH 会话意外断开"),
    ("ssh 会话中断", "SSH 会话中断：服务器网络抖动或重启导致通道断开，请重试"),
    ("connection lost", "SSH 会话中断：服务器网络抖动或重启导致通道断开，请重试"),
    ("ssh 会话仍然存活，可能是远端进程挂死", "远端命令长时间无响应（超过 30 分钟），可能是进程挂死，请重试或检查服务器状态"),
    ("docker: command not found", "服务器未安装 Docker，请先安装"),
    ("cannot connect to the docker daemon", "Docker 服务未启动"),
    # apt/dpkg 被别的进程（通常是 unattended-upgrades）占锁。
    ("could not get lock /var/lib/dpkg", "服务器正在自动安装系统更新，请 1-2 分钟后重试"),
    ("could not get lock /var/lib/apt", "服务器正在自动安装系统更新，请 1-2 分钟后重试"),
    ("unable to lock directory /var/lib/apt", "服务器正在自动安装系统更新，请 1-2 分钟后重试"),
    ("dpkg was interrupted", "服务器 dpkg 状态异常，请在 SSH 执行 dpkg --configure -a 后重试"),
    # 其他基础命令缺失通常是登录 shell profile 把 PATH 搞坏了
    ("command not found", "服务器环境异常：基础命令缺失，建议更换纯净 Ubuntu/Debian 镜像后重试"),
    ("no space left on device", "服务器磁盘空间不足"),
    ("out of memory", "服务器内存不足"),
    ("entity_text_invalid", "消息格式错误，请重试"),
    ("bad request", "请求失败，请重试"),
    ("message is not modified", "内容未变化"),
]

APP_EMOJI_FALLBACK: dict[str, str] = {
    "app_telegram": "✈️",
    "app_youtube": "▶️",
    "app_netflix": "🎬",
    "app_github": "🐙",
    "app_x": "🐦",
    "app_whatsapp": "💬",
    "app_spotify": "🎵",
    "app_google": "🔍",
}


def translate_error(exc: BaseException) -> str:
    raw = str(exc).lower()
    for keyword, zh in ERROR_MAP:
        if keyword in raw:
            return zh
    return str(exc)


def translate_error_html(exc: BaseException) -> str:
    return html.escape(translate_error(exc))


def button(
    text: str,
    callback_data: str,
    icon_emoji_id: str | None = None,
    style: str | None = None,
) -> InlineKeyboardButton:
    api_kwargs = {"style": style} if style else None
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        icon_custom_emoji_id=icon_emoji_id,
        api_kwargs=api_kwargs,
    )


def url_button(
    text: str,
    url: str,
    icon_emoji_id: str | None = None,
    style: str | None = None,
) -> InlineKeyboardButton:
    api_kwargs = {"style": style} if style else None
    return InlineKeyboardButton(
        text=text,
        url=url,
        icon_custom_emoji_id=icon_emoji_id,
        api_kwargs=api_kwargs,
    )


def render_progress(action: str, payload: str, *, emoji_map: dict[str, str], tg_emoji) -> str:
    parts = payload.split("|", 2)
    try:
        percent = int(parts[0])
    except (ValueError, IndexError):
        percent = 0
    stage = parts[1] if len(parts) > 1 else payload
    detail = parts[2] if len(parts) > 2 else ""
    filled = round(percent / 10)
    bar = "█" * filled + "░" * (10 - filled)
    skip_detail = not detail or detail in ("完成", "")
    detail_line = f"\n{tg_emoji(emoji_map['clock'], '⏰')} {html.escape(detail)}" if not skip_detail else ""
    return (
        f"{tg_emoji(emoji_map['deploy'], '🔨')} <b>{action}进度</b>\n\n"
        f"阶段：{html.escape(stage)}{detail_line}\n\n"
        f"<code>{percent}% [{bar}]</code>"
    )


def parse_int_suffix(raw: str, prefix: str) -> int | None:
    try:
        return int(raw.replace(prefix, "", 1))
    except ValueError:
        return None


def render_checklist(items: list[tuple[str, str, str]]) -> str:
    level_icon = {
        "ok": "✅",
        "warn": "⚠️",
        "block": "❌",
    }
    lines: list[str] = []
    for level, label, detail in items:
        icon = level_icon.get(level, "•")
        lines.append(f"{icon} <b>{html.escape(label)}</b>")
        if detail:
            lines.append(f"　└ {html.escape(detail)}")
    return "\n".join(lines)

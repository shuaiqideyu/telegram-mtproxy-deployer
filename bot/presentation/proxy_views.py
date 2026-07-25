from __future__ import annotations

import html
from typing import Any, Callable

from bot.services.database import ProxyRecord
from bot.services.deployer import DeploymentResult
from bot.utils.country import country_flag, country_short_name


def _location_display(location: str) -> str:
    """把 DB 中文地区字段渲染成 '🇯🇵 日本' 这种形式；空值回退为 '未知'。"""
    name = country_short_name(location)
    flag = country_flag(location)
    return f"{flag} {name}"


def _protocol_label(protocol: str) -> str:
    return "SOCKS5" if protocol == "socks5" else "MTProxy"


def _socks5_credentials_block(config: dict[str, Any]) -> str:
    username = str(config.get("socks5_username") or "tg")
    password = str(config.get("socks5_password") or "")
    if not password:
        return ""
    return (
        "SOCKS5 手动配置：\n"
        f"用户名：<code>{html.escape(username)}</code>\n"
        f"密码：<code>{html.escape(password)}</code>\n\n"
    )


TgEmojiRenderer = Callable[[str, str], str]
CreatedAtFormatter = Callable[[str], str]


def build_tools_text(
    session: dict[str, Any],
    checks: dict[str, Any],
    *,
    has_existing: bool,
    emoji_map: dict[str, str],
    tg_emoji: TgEmojiRenderer,
) -> str:
    """构建检测完成后的工具面板文案。"""
    ip = html.escape(str(session.get("ip", "")))
    port = html.escape(str(session.get("port", "22")))
    recommended_port = checks.get("recommended_port", session.get("proxy_port", "443"))
    recommended_reason = html.escape(str(checks.get("recommended_port_reason", "自动推荐")))
    proxy_port = html.escape(str(recommended_port))
    protocol = _protocol_label(str(session.get("protocol") or "mtproxy"))
    bbr_supported = bool(checks.get("bbr_supported"))
    bbr_line = (
        f"{tg_emoji(emoji_map['rocket'], '🚀')} BBR：✅ 将自动开启\n"
        if bbr_supported
        else f"{tg_emoji(emoji_map['warn'], '⚠️')} BBR：内核版本过低，不支持\n"
    )
    overwrite_warn = (
        f"\n{tg_emoji(emoji_map['warn'], '⚠️')} 该端口已有代理，部署时将覆盖旧配置"
        if has_existing
        else f"\n{tg_emoji(emoji_map['check'], '✅')} 该端口没有旧记录，本次会新增代理"
    )
    location_raw = str(checks.get("location", "未知"))
    return (
        f"{tg_emoji(emoji_map['info'], 'ℹ')} <b>服务器检测完成</b>\n\n"
        f"IP：<code>{ip}:{port}</code>\n"
        f"协议：<b>{protocol}</b>\n"
        f"系统：{html.escape(str(checks.get('os_name', '未知')))}\n"
        f"地区：{html.escape(_location_display(location_raw))}\n"
        f"推荐端口：<code>{proxy_port}</code>（{recommended_reason}）\n"
        f"{bbr_line}"
        f"{overwrite_warn}"
    )


def build_quick_check_text(
    *,
    ip: str,
    ssh_port: int,
    username: str,
    checklist_text: str,
    summary: str,
    emoji_map: dict[str, str],
    tg_emoji: TgEmojiRenderer,
) -> str:
    return (
        f"{tg_emoji(emoji_map['info'], '🔍')} <b>快速检查报告</b>\n\n"
        f"IP：<code>{html.escape(ip)}:{ssh_port}</code>\n"
        f"用户名：<code>{html.escape(username)}</code>\n\n"
        f"{checklist_text}\n\n"
        f"{tg_emoji(emoji_map['info'], 'ℹ')} {html.escape(summary)}"
    )


def build_proxy_detail_text(
    record: ProxyRecord,
    *,
    emoji_map: dict[str, str],
    tg_emoji: TgEmojiRenderer,
    format_created_at: CreatedAtFormatter,
) -> str:
    """构建代理详情文案（用户侧不展示广告状态）。"""
    created_at = format_created_at(record.created_at)
    remark = (record.remark or "").strip()
    protocol = _protocol_label(record.protocol)
    title = "代理详情"
    if remark:
        title = f"代理详情 - {html.escape(remark)}"
    return (
        f"{tg_emoji(emoji_map['info'], 'ℹ')} <b>{title}</b>\n\n"
        f"IP：<code>{html.escape(record.server_ip)}</code>\n"
        f"协议：<b>{protocol}</b>\n"
        f"系统：{html.escape(record.os_name)}\n"
        f"地区：{html.escape(_location_display(record.location))}\n"
        f"端口：<code>{record.proxy_port}</code>\n"
        f"创建时间：{html.escape(created_at)}\n\n"
        f"{tg_emoji(emoji_map['link'], '🔗')} <b>Telegram 导入链接</b>\n"
        f"{html.escape(record.tg_link)}\n\n"
        f"{_socks5_credentials_block(record.deploy_config) if record.protocol == 'socks5' else ''}"
        + (
            f"{tg_emoji(emoji_map['link'], '🔗')} <b>Web 代理链接</b>\n"
            f"{html.escape(record.web_link)}\n\n"
            if record.web_link else ""
        ) +
        "💡 使用提示\n"
        "• 使用本代理时请关闭其它 VPN / 翻墙工具\n"
        "• 新代理首次连接如无效，完全退出 Telegram 后台重新打开\n"
        + ("• 若 TG 链接点开无反应，可改用 Web 链接" if record.web_link else "• SOCKS5 可在 Telegram 代理设置中手动填写 IP、端口、用户名和密码")
    )


def build_deployment_success_text(
    result: DeploymentResult,
    *,
    is_overwrite: bool,
    emoji_map: dict[str, str],
    tg_emoji: TgEmojiRenderer,
) -> str:
    """构建部署完成文案（用户侧不展示广告状态）。"""
    overwrite_note = (
        f"\n{tg_emoji(emoji_map['warn'], '⚠️')} 旧代理配置已自动覆盖，新 Secret 已生效\n"
        if is_overwrite
        else f"\n{tg_emoji(emoji_map['check'], '✅')} 已新增一条代理记录\n"
    )
    protocol = _protocol_label(result.protocol)
    return (
        f"{tg_emoji(emoji_map['party'], '🎉')} <b>部署完成</b>\n"
        f"{overwrite_note}\n"
        f"IP：<code>{html.escape(result.ip)}</code>\n"
        f"协议：<b>{protocol}</b>\n"
        f"系统：<code>{html.escape(result.os_name)}</code>\n"
        f"地区：<code>{html.escape(_location_display(result.location))}</code>\n"
        f"端口：<code>{result.port}</code>\n"
        "\n"
        f"{tg_emoji(emoji_map['link'], '🔗')} <b>Telegram 导入链接</b>\n"
        f"{html.escape(result.tg_link)}\n\n"
        f"{_socks5_credentials_block(result.deploy_config) if result.protocol == 'socks5' else ''}"
        + (
            f"{tg_emoji(emoji_map['link'], '🔗')} <b>Web 代理链接</b>\n"
            f"{html.escape(result.web_link)}\n\n"
            if result.web_link else ""
        ) +
        "💡 使用提示\n"
        "• 使用本代理时请关闭其它 VPN / 翻墙工具\n"
        "• 新代理首次连接如无效，完全退出 Telegram 后台重新打开\n"
        + ("• 若 TG 链接点开无反应，可改用 Web 链接" if result.web_link else "• SOCKS5 可在 Telegram 代理设置中手动填写 IP、端口、用户名和密码")
    )


def build_port_unreachable_text(
    proxy_port: int,
    *,
    emoji_map: dict[str, str] | None = None,
    tg_emoji: TgEmojiRenderer | None = None,
) -> str:
    warn = tg_emoji(emoji_map["warn"], "⚠️") if emoji_map and tg_emoji else "⚠️"
    info = tg_emoji(emoji_map["info"], "ℹ️") if emoji_map and tg_emoji else "ℹ️"
    return (
        f"{warn} <b>端口外网不可达</b>\n\n"
        f"代理已部署成功，但端口 <code>{proxy_port}</code> 从外网无法连通，通常是云安全组未放行。\n"
        f"请在云服务商控制台（阿里云/腾讯云/AWS 等）添加入站规则：TCP <code>{proxy_port}</code>，来源 <code>0.0.0.0/0</code>。\n"
        f"{info} 可发送 /help 查看常见问题处理步骤。"
    )


def build_check_proxy_result_text(
    record: ProxyRecord,
    detail: dict[str, Any],
    *,
    server_tg_probe: tuple[bool, int | None],
    stats: dict[str, int],
    emoji_map: dict[str, str],
    tg_emoji: TgEmojiRenderer,
) -> str:
    """构建检测代理结果文案。"""
    status_ok = bool(detail.get("container_running")) and bool(detail.get("port_listening"))
    status_icon = tg_emoji(emoji_map["check"], "✅") if status_ok else tg_emoji(emoji_map["cross"], "❌")
    status_text = "运行中" if status_ok else "异常"
    if detail.get("uptime"):
        status_text = f"{status_text}（已运行 {html.escape(str(detail['uptime']))}）"

    server_ok, server_latency = server_tg_probe
    server_icon = tg_emoji(emoji_map["check"], "✅") if server_ok else tg_emoji(emoji_map["cross"], "❌")
    server_text = (
        f"{server_icon} 服务器到 Telegram：{server_latency} ms"
        if server_ok and server_latency is not None
        else f"{server_icon} 服务器到 Telegram：失败"
    )
    public_reachable = bool(detail.get("public_reachable"))
    if status_ok and public_reachable:
        public_line = f"{tg_emoji(emoji_map['check'], '✅')} 外网连通：正常"
    elif status_ok and not public_reachable:
        public_line = (
            f"{tg_emoji(emoji_map['warn'], '⚠️')} 外网连通：不可达"
            f"（安全组未放行端口 {record.proxy_port}，请到云控制台添加入站规则，详见 /help）"
        )
    else:
        public_line = f"{tg_emoji(emoji_map['cross'], '❌')} 外网连通：未通过（容器或端口异常）"

    latest_connections = int(stats.get("latest_connections", 0))
    traffic_1h_in = int(stats.get("traffic_1h_in", 0)) / (1024 * 1024)
    traffic_1h_out = int(stats.get("traffic_1h_out", 0)) / (1024 * 1024)
    traffic_1d_in = int(stats.get("traffic_1d_in", 0)) / (1024 * 1024)
    traffic_1d_out = int(stats.get("traffic_1d_out", 0)) / (1024 * 1024)

    return (
        f"{tg_emoji(emoji_map['speed'], '⚡')} <b>检测代理结果</b>\n\n"
        f"IP：<code>{html.escape(record.server_ip)}</code>\n"
        f"协议：<b>{_protocol_label(record.protocol)}</b>\n"
        f"{status_icon} 代理状态：{status_text}\n"
        f"{server_text}\n\n"
        f"{public_line}\n\n"
        f"{tg_emoji(emoji_map['info'], 'ℹ')} <b>统计</b>\n"
        f"当前连接：{latest_connections}\n"
        f"近 1h：↑{traffic_1h_in:.2f} MB ↓{traffic_1h_out:.2f} MB\n"
        f"近 24h：↑{traffic_1d_in:.2f} MB ↓{traffic_1d_out:.2f} MB"
    )

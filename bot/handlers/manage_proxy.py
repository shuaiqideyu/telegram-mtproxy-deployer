from __future__ import annotations

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.config import EMOJI, tg_emoji
from bot.handlers.constants import (
    ADMIN_NOTIFY_DELETE,
    ADMIN_NOTIFY_NEW,
    ADMIN_NOTIFY_OVERWRITE,
    ADMIN_NOTIFY_RESET,
    BTN_BLUE,
    BTN_GREEN,
    BTN_RED,
    CB_CONFIRM_DELETE_PREFIX,
    CB_CONFIRM_RESET_PREFIX,
    CB_PROXY_OPEN_PREFIX,
)
from bot.handlers.utils import button, render_progress, translate_error_html
from bot.services.ssh import SSHCredentials
from bot.utils.country import country_flag, country_short_name


def _host_line(record) -> str:
    """代理主机展示行：直接用原生 IP。"""
    return f"IP：<code>{html.escape(record.server_ip)}</code>\n"

logger = logging.getLogger(__name__)


class ManageProxyMixin:
    async def _confirm_reset_proxy(
        self,
        user_id: int,
        proxy_id: int,
        chat_id: int,
        bot_msg_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        record = await self.db.get_proxy(user_id=user_id, proxy_id=proxy_id)
        if not record or record.status != "active":
            await self._render_my_proxies(user_id, chat_id, bot_msg_id, context)
            return
        await self._edit_panel(
            context,
            chat_id,
            bot_msg_id,
            (
                f"{tg_emoji(EMOJI['warn'], '⚠️')} <b>确认重置代理？</b>\n\n"
                f"{_host_line(record)}"
                "重置将生成新的 Secret 和代理链接，旧链接将失效。"
            ),
            InlineKeyboardMarkup(
                [
                    [
                        button("确认重置", f"{CB_CONFIRM_RESET_PREFIX}{record.id}", EMOJI["deploy"], BTN_GREEN),
                        button("取消", f"{CB_PROXY_OPEN_PREFIX}{record.id}", EMOJI["cross"], BTN_RED),
                    ]
                ]
            ),
        )

    async def _confirm_delete_proxy(
        self,
        user_id: int,
        proxy_id: int,
        chat_id: int,
        bot_msg_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        record = await self.db.get_proxy(user_id=user_id, proxy_id=proxy_id)
        if not record:
            await self._render_my_proxies(user_id, chat_id, bot_msg_id, context)
            return
        await self._edit_panel(
            context,
            chat_id,
            bot_msg_id,
            (
                f"{tg_emoji(EMOJI['warn'], '⚠️')} <b>确认删除代理？</b>\n\n"
                f"{_host_line(record)}"
                "删除将停止远端容器并移除记录，此操作不可恢复。"
            ),
            InlineKeyboardMarkup(
                [
                    [
                        button("确认删除", f"{CB_CONFIRM_DELETE_PREFIX}{record.id}", EMOJI["cross"], BTN_RED),
                        button("取消", f"{CB_PROXY_OPEN_PREFIX}{record.id}", EMOJI["folder"], BTN_BLUE),
                    ]
                ]
            ),
        )

    async def _reset_proxy(
        self,
        user_id: int,
        proxy_id: int,
        chat_id: int,
        bot_msg_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        telegram_username: str | None = None,
    ) -> None:
        record = await self.db.get_proxy(user_id=user_id, proxy_id=proxy_id)
        if not record or record.status != "active":
            await self._render_my_proxies(user_id, chat_id, bot_msg_id, context)
            return

        credentials = SSHCredentials(
            host=record.server_ip,
            port=record.ssh_port,
            username=record.ssh_user,
            password=record.ssh_password,
        )
        progress = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{tg_emoji(EMOJI['deploy'], '🔨')} <b>重置进度</b>\n\n"
                f"{tg_emoji(EMOJI['clock'], '⏰')} 正在开始重置..."
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

        async def on_progress(text: str) -> None:
            await self._edit_panel(
                context,
                progress.chat_id,
                progress.message_id,
                render_progress("重置", text, emoji_map=EMOJI, tg_emoji=tg_emoji),
                None,
            )

        try:
            result = await self.deployer.deploy(
                credentials,
                on_progress=on_progress,
                proxy_port=record.proxy_port,
                protocol=record.protocol,
            )
            await self.db.reset_proxy(
                proxy_id=record.id,
                user_id=user_id,
                secret=result.secret,
                tg_link=result.tg_link,
                web_link=result.web_link,
                os_name=result.os_name,
                cpu_cores=result.cpu_cores,
                memory_mb=result.memory_mb,
                kernel=result.kernel,
                location=result.location,
                tag_supported=result.tag_supported,
                deploy_config=result.deploy_config,
            )
            await self._notify_admin(
                context=context,
                notify_type=ADMIN_NOTIFY_RESET,
                user_id=user_id,
                telegram_username=telegram_username,
                host=result.ip,
                port=result.port,
                secret=result.secret,
                location=result.location,
                tag_supported=result.tag_supported,
            )
            await self._try_delete(context, progress.chat_id, progress.message_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"{tg_emoji(EMOJI['check'], '✅')} <b>重置成功</b>\n\n"
                    f"{_host_line(record)}"
                    f"状态：已生成新的代理链接和 Secret"
                ),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001
            await self._try_delete(context, progress.chat_id, progress.message_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"{tg_emoji(EMOJI['cross'], '❌')} <b>重置失败</b>\n\n"
                    f"{_host_line(record)}"
                    f"状态：{translate_error_html(exc)}"
                ),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return

        await self._render_proxy_detail(user_id, proxy_id, chat_id, bot_msg_id, context)

    async def _delete_proxy(
        self,
        user_id: int,
        proxy_id: int,
        chat_id: int,
        bot_msg_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        telegram_username: str | None = None,
    ) -> None:
        record = await self.db.get_proxy(user_id=user_id, proxy_id=proxy_id)
        if not record:
            await self._render_my_proxies(user_id, chat_id, bot_msg_id, context)
            return

        credentials = SSHCredentials(
            host=record.server_ip,
            port=record.ssh_port,
            username=record.ssh_user,
            password=record.ssh_password,
        )
        progress = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{tg_emoji(EMOJI['cross'], '❌')} <b>删除中</b>\n\n"
                f"{tg_emoji(EMOJI['clock'], '⏰')} 正在移除远端容器..."
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        try:
            await self.deployer.remove_proxy_container(
                credentials,
                proxy_port=record.proxy_port,
                protocol=record.protocol,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("删除远端容器失败: %s", exc)
        await self.db.delete_proxy(user_id=user_id, proxy_id=proxy_id)
        await self._try_delete(context, progress.chat_id, progress.message_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{tg_emoji(EMOJI['check'], '✅')} <b>删除成功</b>\n\n"
                f"{_host_line(record)}"
                f"状态：已停止并从记录中移除"
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await self._notify_admin(
            context=context,
            notify_type=ADMIN_NOTIFY_DELETE,
            user_id=user_id,
            telegram_username=telegram_username,
            host=record.server_ip,
            port=record.proxy_port,
            location=record.location,
        )
        await self._render_my_proxies(user_id, chat_id, bot_msg_id, context)

    async def _notify_admin(
        self,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        notify_type: str,
        user_id: int,
        telegram_username: str | None,
        host: str,
        port: int,
        secret: str | None = None,
        location: str,
        tag_supported: bool | None = None,
    ) -> None:
        notify_map = {
            ADMIN_NOTIFY_NEW: (tg_emoji(EMOJI["rocket"], "🚀"), "新建代理"),
            ADMIN_NOTIFY_OVERWRITE: (tg_emoji(EMOJI["warn"], "⚠️"), "覆盖代理"),
            ADMIN_NOTIFY_RESET: (tg_emoji(EMOJI["gear"], "⚙️"), "重置代理"),
            ADMIN_NOTIFY_DELETE: (tg_emoji(EMOJI["cross"], "❌"), "删除代理"),
        }
        icon, title = notify_map.get(notify_type, (tg_emoji(EMOJI["info"], "ℹ️"), "管理员通知"))
        safe_user = html.escape(f"@{telegram_username}") if telegram_username else f"用户 {user_id}"
        tag_label = "✅ 已启用" if tag_supported else "❌ 不支持（direct 模式）"
        flag = country_flag(location)
        zh_name = country_short_name(location)
        lines = [
            f"{icon} <b>{title}</b>",
            "",
            f"{tg_emoji(EMOJI['user'], '👤')} 用户：{safe_user}",
            f"{tg_emoji(EMOJI['server'], '🖥')} 服务器：<code>{html.escape(host)}:{port}</code>",
            f"{tg_emoji(EMOJI['globe'], '🌐')} 地区：{flag} {html.escape(zh_name)}",
        ]
        if notify_type != ADMIN_NOTIFY_DELETE:
            lines.append(f"{tg_emoji(EMOJI['key'], '🔑')} Secret：<code>{html.escape(secret or '-')}</code>")
            lines.append(f"{tg_emoji(EMOJI['info'], '📢')} 广告标签：{tag_label}")
        message = "\n".join(lines)
        profile_text = f"@{telegram_username}" if telegram_username else f"用户 {user_id}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(text=profile_text, url=f"tg://user?id={user_id}")]])
        try:
            await context.bot.send_message(
                chat_id=self.settings.admin_user_id,
                text=message,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("发送管理员通知失败: %s", exc)

from __future__ import annotations

import asyncio
import math
from urllib.parse import quote

from telegram import InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.config import EMOJI, tg_emoji
from bot.handlers.constants import (
    BTN_BLUE,
    BTN_GREEN,
    BTN_RED,
    CB_BACK_MENU,
    CB_MY_PROXIES,
    CB_MY_PAGE_PREFIX,
    CB_NEW_PROXY,
    CB_NOOP,
    CB_PROXY_BACK,
    CB_PROXY_CHECK_PREFIX,
    CB_PROXY_DELETE_PREFIX,
    CB_PROXY_OPEN_PREFIX,
    CB_PROXY_TOGGLE_PUBLIC_PREFIX,
    CB_PROXY_RESET_PREFIX,
    CB_MODIFY_PORT_DETAIL_PREFIX,
    CB_MODIFY_REMARK_PREFIX,
    MY_PROXIES_PAGE_SIZE,
    STATE_AWAIT_DETAIL_PORT,
    STATE_AWAIT_REMARK,
    STATE_VIEW_MY,
)
from bot.handlers.utils import button, url_button
from bot.presentation.proxy_views import build_check_proxy_result_text, build_proxy_detail_text
from bot.services.database import ProxyRecord
from bot.services.ssh import SSHCredentials
from bot.utils.time_fmt import format_iso_to_beijing


class MyProxiesMixin:
    async def _render_my_proxies(
        self,
        user_id: int,
        chat_id: int,
        bot_msg_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        page: int = 0,
    ) -> None:
        proxies = await self.db.list_user_proxies(user_id=user_id, include_deleted=False)
        total = len(proxies)
        total_pages = max(math.ceil(total / MY_PROXIES_PAGE_SIZE), 1)
        page = max(0, min(page, total_pages - 1))
        await self.store.save_session(
            user_id,
            {"state": STATE_VIEW_MY, "chat_id": chat_id, "bot_msg_id": bot_msg_id, "my_page": page},
        )
        if not proxies:
            await self._edit_panel(
                context,
                chat_id,
                bot_msg_id,
                (
                    f"{tg_emoji(EMOJI['folder'], '📁')} <b>我的代理</b>\n\n"
                    f"{tg_emoji(EMOJI['info'], 'ℹ')} 暂无代理记录"
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            button("新建代理", CB_NEW_PROXY, EMOJI["deploy"], BTN_GREEN),
                            button("返回菜单", CB_BACK_MENU, EMOJI["cross"], BTN_RED),
                        ]
                    ]
                ),
            )
            return

        lines = [f"{tg_emoji(EMOJI['folder'], '📁')} <b>我的代理</b>"]
        rows = []
        start = page * MY_PROXIES_PAGE_SIZE
        end = start + MY_PROXIES_PAGE_SIZE
        for item in proxies[start:end]:
            display_name = (item.remark or "").strip() or f"{item.server_ip}:{item.proxy_port}"
            protocol_label = "SOCKS5" if item.protocol == "socks5" else "MT"
            rows.append(
                [button(f"{protocol_label} · {display_name}", f"{CB_PROXY_OPEN_PREFIX}{item.id}", EMOJI["server"], BTN_BLUE)]
            )
        if total_pages > 1:
            pager_row = []
            if page > 0:
                pager_row.append(
                    button("上一页", f"{CB_MY_PAGE_PREFIX}{page - 1}", None, BTN_BLUE)
                )
            pager_row.append(button(f"第 {page + 1}/{total_pages} 页", CB_NOOP, EMOJI["info"], BTN_BLUE))
            if page < total_pages - 1:
                pager_row.append(
                    button("下一页", f"{CB_MY_PAGE_PREFIX}{page + 1}", None, BTN_BLUE)
                )
            rows.append(pager_row)
        rows.append([button("返回菜单", CB_BACK_MENU, EMOJI["cross"], BTN_RED)])
        await self._edit_panel(context, chat_id, bot_msg_id, "\n".join(lines), InlineKeyboardMarkup(rows))

    async def _render_proxy_detail(
        self,
        user_id: int,
        proxy_id: int,
        chat_id: int,
        bot_msg_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        record = await self.db.get_proxy(user_id=user_id, proxy_id=proxy_id)
        if not record or record.status != "active":
            await self._edit_panel(
                context,
                chat_id,
                bot_msg_id,
                f"{tg_emoji(EMOJI['warn'], '❗')} <b>代理不存在或已删除</b>",
                InlineKeyboardMarkup([[button("返回我的代理", CB_PROXY_BACK, EMOJI["folder"], BTN_RED)]]),
            )
            return
        share_url = f"https://t.me/share/url?url={quote(record.tg_link, safe='')}"
        await self._edit_panel(
            context,
            chat_id,
            bot_msg_id,
            self._proxy_detail_text(record),
            InlineKeyboardMarkup(
                [
                    [
                        url_button("分享链接", share_url, EMOJI["link"], BTN_BLUE),
                        button("检测代理", f"{CB_PROXY_CHECK_PREFIX}{record.id}", EMOJI["speed"], BTN_BLUE),
                    ],
                    [
                        button("修改备注", f"{CB_MODIFY_REMARK_PREFIX}{record.id}", EMOJI["info"], BTN_GREEN),
                        button("修改端口", f"{CB_MODIFY_PORT_DETAIL_PREFIX}{record.id}", EMOJI["gear"], BTN_GREEN),
                    ],
                    [
                        button(
                            "取消公开" if record.is_public else "公开代理",
                            f"{CB_PROXY_TOGGLE_PUBLIC_PREFIX}{record.id}",
                            EMOJI["globe"] if not record.is_public else EMOJI["cross"],
                            BTN_RED if record.is_public else BTN_GREEN,
                        )
                    ],
                    [
                        button("重置代理", f"{CB_PROXY_RESET_PREFIX}{record.id}", EMOJI["deploy"], BTN_GREEN),
                        button("删除代理", f"{CB_PROXY_DELETE_PREFIX}{record.id}", EMOJI["cross"], BTN_RED),
                    ],
                    [button("返回我的代理", CB_PROXY_BACK, EMOJI["folder"], BTN_RED)],
                ]
            ),
        )

    async def _check_proxy(
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

        credentials = SSHCredentials(
            host=record.server_ip,
            port=record.ssh_port,
            username=record.ssh_user,
            password=record.ssh_password,
        )
        progress = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{tg_emoji(EMOJI['speed'], '⚡')} <b>检测代理中</b>\n\n"
                f"{tg_emoji(EMOJI['clock'], '⏰')} 正在检查运行状态、端口监听和 Telegram 连通性..."
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        try:
            runtime_task = self.deployer.inspect_runtime(
                credentials,
                record.proxy_port,
                include_public_probe=True,
                include_traffic=True,
                protocol=record.protocol,
            )
            stats_task = self.db.get_proxy_traffic_stats(record.id)
            runtime, stats = await asyncio.gather(runtime_task, stats_task)
            detail = {
                "container_status": runtime.container_status,
                "container_running": runtime.container_running,
                "uptime": runtime.uptime,
                "port_listening": runtime.port_listening,
                "public_reachable": runtime.public_reachable,
            }
            server_tg_probe = (runtime.tg_reachable, runtime.tg_latency_ms)
            sample = runtime.traffic
        except Exception as exc:  # noqa: BLE001
            detail = {
                "container_status": f"检测失败：{exc}",
                "uptime": None,
                "port_listening": False,
                "container_running": False,
                "public_reachable": False,
            }
            server_tg_probe = (False, None)
            sample = None
            stats = await self.db.get_proxy_traffic_stats(record.id)

        if sample is not None:
            stats = dict(stats)
            stats["latest_connections"] = sample.active_connections
            try:
                await self.db.insert_traffic_sample(
                    proxy_id=record.id,
                    active_connections=sample.active_connections,
                    bytes_in=sample.bytes_in,
                    bytes_out=sample.bytes_out,
                )
            except Exception:  # noqa: BLE001
                pass

        await self._try_delete(context, progress.chat_id, progress.message_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=build_check_proxy_result_text(
                record,
                detail,
                server_tg_probe=server_tg_probe,
                stats=stats,
                emoji_map=EMOJI,
                tg_emoji=tg_emoji,
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def _prompt_modify_remark(
        self,
        user_id: int,
        proxy_id: int,
        chat_id: int,
        bot_msg_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        session = await self.store.load_session(user_id) or {}
        session.update(
            {
                "state": STATE_AWAIT_REMARK,
                "editing_proxy_id": proxy_id,
                "chat_id": chat_id,
                "bot_msg_id": bot_msg_id,
            }
        )
        await self.store.save_session(user_id, session)
        await self._edit_panel(
            context,
            chat_id,
            bot_msg_id,
            (
                f"{tg_emoji(EMOJI['info'], 'ℹ️')} <b>修改备注</b>\n\n"
                "请回复新备注（最长 30 字），发送 <code>-</code> 可清空备注。"
            ),
            InlineKeyboardMarkup([[button("返回", CB_PROXY_BACK, EMOJI["cross"], BTN_RED)]]),
        )

    async def _prompt_modify_port_in_detail(
        self,
        user_id: int,
        proxy_id: int,
        chat_id: int,
        bot_msg_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        session = await self.store.load_session(user_id) or {}
        session.update(
            {
                "state": STATE_AWAIT_DETAIL_PORT,
                "editing_proxy_id": proxy_id,
                "chat_id": chat_id,
                "bot_msg_id": bot_msg_id,
            }
        )
        await self.store.save_session(user_id, session)
        await self._edit_panel(
            context,
            chat_id,
            bot_msg_id,
            (
                f"{tg_emoji(EMOJI['gear'], '⚙️')} <b>修改代理端口</b>\n\n"
                "请回复新的代理端口（1-65535）。\n"
                "提交后会按新端口重新部署覆盖当前代理。"
            ),
            InlineKeyboardMarkup([[button("返回", CB_PROXY_BACK, EMOJI["cross"], BTN_RED)]]),
        )


    @staticmethod
    def _proxy_detail_text(record: ProxyRecord) -> str:
        return build_proxy_detail_text(
            record,
            emoji_map=EMOJI,
            tg_emoji=tg_emoji,
            format_created_at=format_iso_to_beijing,
        )

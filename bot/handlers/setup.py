from __future__ import annotations

import html

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.config import EMOJI, tg_emoji
from bot.handlers.constants import (
    BTN_BLUE,
    BTN_GREEN,
    BTN_RED,
    CB_BACK,
    CB_BACK_MENU,
    CB_PUBLIC_COUNTRIES,
    CB_PROTOCOL_MTPROXY,
    CB_PROTOCOL_SOCKS5,
    CB_USE_ROOT,
    PROTOCOL_MTPROXY,
    PROTOCOL_SOCKS5,
    REPLY_SYNONYMS_MY,
    REPLY_SYNONYMS_NEW,
    REPLY_SYNONYMS_PUBLIC,
    STATE_AWAIT_DETAIL_PORT,
    STATE_AWAIT_IP,
    STATE_AWAIT_PASS,
    STATE_AWAIT_PROTOCOL,
    STATE_AWAIT_PROXY_PORT,
    STATE_AWAIT_REMARK,
    STATE_AWAIT_REMARK_INIT,
    STATE_AWAIT_USER,
    STATE_MENU,
    STATE_QUICK_CHECK,
    STATE_TOOLS,
    STATE_VIEW_MY,
)
from bot.handlers.utils import button, translate_error_html
from bot.utils.validators import ValidationError, parse_host_and_port, validate_username


class SetupMixin:
    async def on_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        if not message or not user:
            return

        text = (message.text or "").strip()
        session = await self.store.load_session(user.id)
        if not session or not session.get("chat_id") or not session.get("bot_msg_id"):
            if text in REPLY_SYNONYMS_NEW | REPLY_SYNONYMS_MY | REPLY_SYNONYMS_PUBLIC:
                await self._handle_reply_shortcut_without_session(update, context, text)
            return

        state = session.get("state")
        chat_id = session.get("chat_id")
        bot_msg_id = session.get("bot_msg_id")

        if text in REPLY_SYNONYMS_NEW:
            await self._try_delete(context, message.chat_id, message.message_id)
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=self._step_remark_init_text(),
                parse_mode="HTML",
                reply_markup=self._remark_init_keyboard(),
                disable_web_page_preview=True,
            )
            session.update(
                {
                    "state": STATE_AWAIT_REMARK_INIT,
                    "chat_id": sent.chat_id,
                    "bot_msg_id": sent.message_id,
                }
            )
            session.pop("remark", None)
            session.pop("protocol", None)
            session.pop("existing_proxy_id", None)
            session.pop("checks", None)
            await self.store.save_session(user.id, session)
            return

        if text in REPLY_SYNONYMS_MY:
            await self._try_delete(context, message.chat_id, message.message_id)
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=f"{tg_emoji(EMOJI['folder'], '📁')} 正在加载代理配置...",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await self._render_my_proxies(user.id, sent.chat_id, sent.message_id, context, page=0)
            return

        if text in REPLY_SYNONYMS_PUBLIC:
            await self._try_delete(context, message.chat_id, message.message_id)
            sent = await context.bot.send_message(
                chat_id=chat_id,
                text=f"{tg_emoji(EMOJI['globe'], '🌐')} 正在加载公开代理...",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await self._render_public_countries(user.id, sent.chat_id, sent.message_id, context)
            return

        if state == STATE_AWAIT_REMARK_INIT:
            await self._try_delete(context, message.chat_id, message.message_id)
            await self._consume_remark_init(user.id, session, context, text)
            return

        if state == STATE_AWAIT_IP:
            try:
                ip, port = parse_host_and_port(text)
            except ValidationError as exc:
                await self._edit_panel(
                    context,
                    chat_id,
                    bot_msg_id,
                    f"{self._step_ip_text(session, remark=str(session.get('remark') or ''))}\n\n{tg_emoji(EMOJI['warn'], '❗')} {translate_error_html(exc)}",
                    InlineKeyboardMarkup([[button("返回", CB_BACK, EMOJI["cross"], BTN_RED)]]),
                )
                await self._try_delete(context, message.chat_id, message.message_id)
                return

            session.update({"ip": ip, "port": port, "state": STATE_AWAIT_USER})
            await self._try_delete(context, message.chat_id, message.message_id)
            await self._refresh_panel(
                context,
                user.id,
                session,
                self._step_username_text(session),
                InlineKeyboardMarkup(
                    [
                        [button("使用 root", CB_USE_ROOT, EMOJI["root"], BTN_GREEN)],
                        [button("返回", CB_BACK, EMOJI["cross"], BTN_RED)],
                    ]
                ),
            )
            return

        if state == STATE_AWAIT_USER:
            try:
                username = validate_username(text)
            except ValidationError as exc:
                await self._edit_panel(
                    context,
                    chat_id,
                    bot_msg_id,
                    f"{self._step_username_text(session)}\n\n{tg_emoji(EMOJI['warn'], '❗')} {translate_error_html(exc)}",
                    InlineKeyboardMarkup(
                        [
                            [button("使用 root", CB_USE_ROOT, EMOJI["root"], BTN_GREEN)],
                            [button("返回", CB_BACK, EMOJI["cross"], BTN_RED)],
                        ]
                    ),
                )
                await self._try_delete(context, message.chat_id, message.message_id)
                return

            session.update({"username": username, "state": STATE_AWAIT_PASS})
            await self._try_delete(context, message.chat_id, message.message_id)
            await self._refresh_panel(
                context,
                user.id,
                session,
                self._step_password_text(session),
                InlineKeyboardMarkup([[button("返回", CB_BACK, EMOJI["cross"], BTN_RED)]]),
            )
            return

        if state == STATE_AWAIT_PASS:
            session["password"] = text
            session["proxy_port"] = int(self._default_proxy_port(session))
            session["proxy_port_user_set"] = False
            await self.store.save_session(user.id, session)
            await self._try_delete(context, message.chat_id, message.message_id)
            await self._inspect_after_bind(user.id, session, context)
            return

        if state == STATE_AWAIT_PROXY_PORT:
            try:
                port = int(text)
            except ValueError:
                port = 0
            if not 1 <= port <= 65535:
                await self._edit_panel(
                    context,
                    chat_id,
                    bot_msg_id,
                    f"{tg_emoji(EMOJI['warn'], '❗')} 端口范围必须是 1-65535，请重新输入。",
                    InlineKeyboardMarkup([[button("返回", CB_BACK, EMOJI["cross"], BTN_RED)]]),
                )
                await self._try_delete(context, message.chat_id, message.message_id)
                return
            session["proxy_port"] = port
            session["proxy_port_user_set"] = True
            await self.store.save_session(user.id, session)
            await self._try_delete(context, message.chat_id, message.message_id)
            await self._inspect_after_bind(user.id, session, context)
            return

        if state == STATE_AWAIT_REMARK:
            proxy_id = int(session.get("editing_proxy_id") or 0)
            if proxy_id <= 0:
                return
            remark = "" if text == "-" else text[:30]
            await self.db.update_proxy_remark(user.id, proxy_id, remark)
            session["state"] = STATE_VIEW_MY
            session.pop("editing_proxy_id", None)
            await self.store.save_session(user.id, session)
            await self._try_delete(context, message.chat_id, message.message_id)
            await self._render_proxy_detail(user.id, proxy_id, chat_id, bot_msg_id, context)
            return

        if state == STATE_AWAIT_DETAIL_PORT:
            proxy_id = int(session.get("editing_proxy_id") or 0)
            if proxy_id <= 0:
                return
            try:
                port = int(text)
            except ValueError:
                port = 0
            if not 1 <= port <= 65535:
                await self._edit_panel(
                    context,
                    chat_id,
                    bot_msg_id,
                    f"{tg_emoji(EMOJI['warn'], '❗')} 端口范围必须是 1-65535，请重新输入。",
                    InlineKeyboardMarkup([[button("返回", CB_BACK, EMOJI["cross"], BTN_RED)]]),
                )
                await self._try_delete(context, message.chat_id, message.message_id)
                return
            record = await self.db.get_proxy(user.id, proxy_id)
            if not record or record.status != "active":
                await self._render_my_proxies(user.id, chat_id, bot_msg_id, context)
                return
            session.update(
                {
                    "ip": record.server_ip,
                    "port": record.ssh_port,
                    "username": record.ssh_user,
                    "password": record.ssh_password,
                    "protocol": record.protocol,
                    "proxy_port": port,
                    "proxy_port_user_set": True,
                    "existing_proxy_id": record.id,
                    "state": STATE_TOOLS,
                }
            )
            session.pop("editing_proxy_id", None)
            await self.store.save_session(user.id, session)
            await self._try_delete(context, message.chat_id, message.message_id)
            await self._start_deployment(user.id, session, context, telegram_username=user.username)
            return

    async def _handle_reply_shortcut_without_session(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        message = update.effective_message
        user = update.effective_user
        if not message or not user:
            return

        await self.db.upsert_user(user.id, user.username, user.first_name)
        await self._try_delete(context, message.chat_id, message.message_id)

        if text in REPLY_SYNONYMS_NEW:
            sent = await context.bot.send_message(
                chat_id=message.chat_id,
                text=self._step_remark_init_text(),
                parse_mode="HTML",
                reply_markup=self._remark_init_keyboard(),
                disable_web_page_preview=True,
            )
            await self.store.save_session(
                user.id,
                {"state": STATE_AWAIT_REMARK_INIT, "chat_id": sent.chat_id, "bot_msg_id": sent.message_id},
            )
            return

        if text in REPLY_SYNONYMS_MY:
            sent = await context.bot.send_message(
                chat_id=message.chat_id,
                text=f"{tg_emoji(EMOJI['folder'], '📁')} 正在加载代理配置...",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await self._render_my_proxies(user.id, sent.chat_id, sent.message_id, context, page=0)
            return

        if text in REPLY_SYNONYMS_PUBLIC:
            sent = await context.bot.send_message(
                chat_id=message.chat_id,
                text=f"{tg_emoji(EMOJI['globe'], '🌐')} 正在加载公开代理...",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            await self._render_public_countries(user.id, sent.chat_id, sent.message_id, context)
            return

    async def _go_back(
        self,
        user_id: int,
        session: dict[str, object],
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        bot_msg_id: int,
    ) -> None:
        state = session.get("state")
        if state == STATE_AWAIT_REMARK_INIT:
            session.pop("remark", None)
            session["state"] = STATE_MENU
            await self.store.save_session(user_id, session)
            await self._edit_panel(context, chat_id, bot_msg_id, self._build_menu_text(), self._menu_keyboard())
            return

        if state == STATE_AWAIT_PROTOCOL:
            session["state"] = STATE_AWAIT_REMARK_INIT
            await self.store.save_session(user_id, session)
            await self._edit_panel(
                context,
                chat_id,
                bot_msg_id,
                self._step_remark_init_text(),
                self._remark_init_keyboard(),
            )
            return

        if state == STATE_AWAIT_IP:
            session["state"] = STATE_AWAIT_PROTOCOL
            await self.store.save_session(user_id, session)
            await self._edit_panel(
                context,
                chat_id,
                bot_msg_id,
                self._step_protocol_text(session),
                self._protocol_keyboard(),
            )
            return

        if state == STATE_AWAIT_USER:
            session["state"] = STATE_AWAIT_IP
            await self.store.save_session(user_id, session)
            await self._edit_panel(
                context,
                chat_id,
                bot_msg_id,
                self._step_ip_text(session, remark=str(session.get("remark") or "")),
                InlineKeyboardMarkup([[button("返回", CB_BACK, EMOJI["cross"], BTN_RED)]]),
            )
            return

        if state == STATE_AWAIT_PASS:
            session["state"] = STATE_AWAIT_USER
            await self.store.save_session(user_id, session)
            await self._edit_panel(
                context,
                chat_id,
                bot_msg_id,
                self._step_username_text(session),
                InlineKeyboardMarkup(
                    [
                        [button("使用 root", CB_USE_ROOT, EMOJI["root"], BTN_GREEN)],
                        [button("返回", CB_BACK, EMOJI["cross"], BTN_RED)],
                    ]
                ),
            )
            return

        if state == STATE_TOOLS:
            session["state"] = STATE_AWAIT_PASS
            await self.store.save_session(user_id, session)
            await self._edit_panel(
                context,
                chat_id,
                bot_msg_id,
                self._step_password_text(session),
                InlineKeyboardMarkup([[button("返回", CB_BACK, EMOJI["cross"], BTN_RED)]]),
            )
            return

        if state == STATE_AWAIT_PROXY_PORT:
            session["state"] = STATE_QUICK_CHECK
            await self.store.save_session(user_id, session)
            await self._render_quick_check(user_id, session, context)
            return

        if state in {STATE_AWAIT_REMARK, STATE_AWAIT_DETAIL_PORT}:
            proxy_id = int(session.get("editing_proxy_id") or 0)
            session.pop("editing_proxy_id", None)
            session["state"] = STATE_VIEW_MY
            await self.store.save_session(user_id, session)
            if proxy_id > 0:
                await self._render_proxy_detail(user_id, proxy_id, chat_id, bot_msg_id, context)
            else:
                await self._render_my_proxies(user_id, chat_id, bot_msg_id, context)
            return

        session["state"] = STATE_MENU
        await self.store.save_session(user_id, session)
        await self._edit_panel(context, chat_id, bot_msg_id, self._build_menu_text(), self._menu_keyboard())

    async def _consume_remark_init(
        self,
        user_id: int,
        session: dict[str, object],
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> None:
        remark_input = text.strip()
        if not remark_input or remark_input == "-":
            await self._edit_panel(
                context,
                int(session["chat_id"]),
                int(session["bot_msg_id"]),
                (
                    f"{self._step_remark_init_text()}\n\n"
                    f"{tg_emoji(EMOJI['warn'], '❗')} 备注不能为空，请重新输入。"
                ),
                self._remark_init_keyboard(),
            )
            return
        remark = remark_input[:30]
        session["remark"] = remark
        session["state"] = STATE_AWAIT_PROTOCOL
        await self._refresh_panel(
            context,
            user_id,
            session,
            self._step_protocol_text(session),
            self._protocol_keyboard(),
        )

    @staticmethod
    def _step_remark_init_text() -> str:
        return (
            f"{tg_emoji(EMOJI['info'], 'ℹ️')} <b>第 1 步：为代理起个备注</b>\n\n"
            "备注会显示在「我的代理」和「公开代理」列表里，方便你和他人识别这台节点。\n\n"
            f"{tg_emoji(EMOJI['check'], '✅')} 示例：<code>香港 · 小鸡 A</code>、<code>办公室专用</code>\n\n"
            f"{tg_emoji(EMOJI['warn'], '❗')} 备注必填，最多 30 字；发送后会自动修剪两端空格"
        )

    @staticmethod
    def _remark_init_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    button("返回菜单", CB_BACK_MENU, EMOJI["cross"], BTN_RED),
                ]
            ]
        )

    @staticmethod
    def _step_protocol_text(session: dict[str, object]) -> str:
        remark = html.escape(str(session.get("remark") or ""))
        return (
            f"{tg_emoji(EMOJI['check'], '✅')} 备注已录入：<code>{remark}</code>\n\n"
            f"{tg_emoji(EMOJI['gear'], '⚙️')} <b>请选择代理协议</b>\n\n"
            "MTProxy：Telegram 原生专用代理，兼容最好。\n"
            "SOCKS5：通用代理协议，Telegram 可直接导入。"
        )

    @staticmethod
    def _protocol_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    button("MTProxy", CB_PROTOCOL_MTPROXY, EMOJI["shield"], BTN_BLUE),
                    button("SOCKS5", CB_PROTOCOL_SOCKS5, EMOJI["speed"], BTN_GREEN),
                ],
                [button("返回", CB_BACK, EMOJI["cross"], BTN_RED)],
            ]
        )

    @staticmethod
    def _protocol_label(protocol: object) -> str:
        return "SOCKS5" if str(protocol) == PROTOCOL_SOCKS5 else "MTProxy"

    def _step_ip_text(self, session: dict[str, object] | None = None, remark: str | None = None) -> str:
        session = session or {}
        header = f"{tg_emoji(EMOJI['check'], '✅')} 备注已录入：<code>{html.escape(remark)}</code>\n\n" if remark else ""
        protocol = self._protocol_label(session.get("protocol", PROTOCOL_MTPROXY))
        return (
            f"{header}"
            f"{tg_emoji(EMOJI['gear'], '⚙️')} 协议：<b>{protocol}</b>\n\n"
            f"{tg_emoji(EMOJI['server'], '🖥')} <b>请回复你的服务器 IP 地址</b>\n\n"
            "支持格式：\n"
            f"{tg_emoji(EMOJI['check'], '✅')} <code>127.0.0.1</code>\n"
            f"{tg_emoji(EMOJI['check'], '✅')} <code>127.0.0.1:2222</code>（自定义 SSH 端口）\n"
            f"{tg_emoji(EMOJI['cross'], '❌')} 不要带 <code>http://</code>、<code>https://</code> 或路径"
        )

    @staticmethod
    def _step_username_text(session: dict[str, object]) -> str:
        ip = html.escape(str(session.get("ip", "")))
        port = html.escape(str(session.get("port", "22")))
        return (
            f"{tg_emoji(EMOJI['check'], '✅')} 服务器地址已录入\n\n"
            f"{tg_emoji(EMOJI['server'], '🖥')} IP：<code>{ip}:{port}</code>\n\n"
            f"{tg_emoji(EMOJI['user'], '👤')} <b>请选择 SSH 用户名</b>\n"
            "方式一：点击下方 <b>使用 root</b> 按钮\n"
            "方式二：直接回复自定义用户名（如 ubuntu / admin / debian）"
        )

    @staticmethod
    def _step_password_text(session: dict[str, object]) -> str:
        ip = html.escape(str(session.get("ip", "")))
        port = html.escape(str(session.get("port", "22")))
        username = html.escape(str(session.get("username", "root")))
        return (
            f"{tg_emoji(EMOJI['check'], '✅')} 用户名已录入\n\n"
            f"{tg_emoji(EMOJI['server'], '🖥')} IP：<code>{ip}:{port}</code>\n"
            f"{tg_emoji(EMOJI['user'], '👤')} 用户名：<code>{username}</code>\n\n"
            f"{tg_emoji(EMOJI['lock'], '🔒')} 请回复 SSH 密码（发送后将自动删除）\n"
            f"{tg_emoji(EMOJI['info'], 'ℹ️')} 当前仅支持密码登录，暂不支持 SSH 密钥"
        )

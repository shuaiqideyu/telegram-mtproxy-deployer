from __future__ import annotations

from telegram import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.config import EMOJI, tg_emoji
from bot.handlers.base import BotBase
from bot.handlers.constants import (
    BTN_BLUE,
    BTN_GREEN,
    BTN_RED,
    CB_BACK,
    CB_BACK_MENU,
    CB_CONFIRM_DELETE_PREFIX,
    CB_CONFIRM_RESET_PREFIX,
    CB_CONTINUE_DEPLOY,
    CB_DEPLOY_PROXY,
    CB_MY_PROXIES,
    CB_PUBLIC_COUNTRIES,
    CB_PUBLIC_COUNTRY_PAGE_PREFIX,
    CB_PUBLIC_LOCATION_PAGE_PREFIX,
    CB_PUBLIC_LOCATION_PREFIX,
    CB_MODIFY_PORT,
    CB_MODIFY_PORT_DETAIL_PREFIX,
    CB_MODIFY_REMARK_PREFIX,
    CB_MY_PAGE_PREFIX,
    CB_NEW_PROXY,
    CB_NOOP,
    CB_PROXY_BACK,
    CB_PROXY_CHECK_PREFIX,
    CB_PROXY_DELETE_PREFIX,
    CB_PROXY_OPEN_PREFIX,
    CB_PROXY_TOGGLE_PUBLIC_PREFIX,
    CB_PROXY_RESET_PREFIX,
    CB_PROTOCOL_MTPROXY,
    CB_PROTOCOL_SOCKS5,
    CB_RETRY_CONNECT,
    CB_SPEED_TEST,
    CB_USE_ROOT,
    PROTOCOL_MTPROXY,
    PROTOCOL_SOCKS5,
    REPLY_TEXT_MY_PROXIES,
    REPLY_TEXT_NEW_PROXY,
    REPLY_TEXT_PUBLIC_PROXIES,
    STATE_AWAIT_IP,
    STATE_AWAIT_PROTOCOL,
    STATE_AWAIT_REMARK_INIT,
    STATE_AWAIT_USER,
    STATE_MENU,
    STATE_QUICK_CHECK,
    STATE_TOOLS,
    STATE_AWAIT_PASS,
)
from bot.handlers.deploy import DeployMixin
from bot.handlers.manage_proxy import ManageProxyMixin
from bot.handlers.my_proxies import MyProxiesMixin
from bot.handlers.public_proxies import PublicProxiesMixin
from bot.handlers.quick_check import QuickCheckMixin
from bot.handlers.setup import SetupMixin
from bot.handlers.utils import button, parse_int_suffix, url_button


class BotHandlers(
    SetupMixin,
    DeployMixin,
    QuickCheckMixin,
    PublicProxiesMixin,
    MyProxiesMixin,
    ManageProxyMixin,
    BotBase,
):
    def register(self, app: Application) -> None:
        private_only = filters.ChatType.PRIVATE
        app.add_handler(CommandHandler("start", self.start, filters=private_only))
        app.add_handler(CommandHandler("new", self.command_new, filters=private_only))
        app.add_handler(CommandHandler("my", self.command_my, filters=private_only))
        app.add_handler(CommandHandler("help", self.command_help, filters=private_only))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND & private_only, self.on_text_message)
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        if not message or not user:
            return

        await self.db.upsert_user(user.id, user.username, user.first_name)
        sent = await message.reply_text(
            text=self._build_menu_text(),
            parse_mode="HTML",
            reply_markup=self._menu_keyboard(),
            disable_web_page_preview=True,
        )
        await self.store.save_session(
            user.id,
            {"state": STATE_MENU, "chat_id": sent.chat_id, "bot_msg_id": sent.message_id},
        )
        await message.reply_text(
            "快捷按钮已启用，可直接点击下方键盘。",
            reply_markup=self._quick_reply_keyboard(),
        )

    async def command_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        if not message or not user:
            return
        await self.db.upsert_user(user.id, user.username, user.first_name)
        sent = await message.reply_text(
            text=self._step_remark_init_text(),
            parse_mode="HTML",
            reply_markup=self._remark_init_keyboard(),
            disable_web_page_preview=True,
        )
        await self.store.save_session(
            user.id,
            {
                "state": STATE_AWAIT_REMARK_INIT,
                "chat_id": sent.chat_id,
                "bot_msg_id": sent.message_id,
            },
        )

    async def command_my(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        if not message or not user:
            return
        await self.db.upsert_user(user.id, user.username, user.first_name)
        sent = await message.reply_text(
            text="正在加载我的代理...",
            disable_web_page_preview=True,
        )
        await self._render_my_proxies(user.id, sent.chat_id, sent.message_id, context, page=0)

    async def command_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        if not message or not user:
            return
        await self.db.upsert_user(user.id, user.username, user.first_name)
        await message.reply_text(
            text=(
                f"{tg_emoji(EMOJI['info'], 'ℹ️')} <b>帮助</b>\n\n"
                "<b>常见问题</b>\n\n"
                "Q1：SSH 连接失败或认证失败？\n"
                "A：确认 IP、SSH 端口、用户名和密码正确；建议使用 root。若提示连接超时/拒绝，先检查服务器 22 端口和网络。\n\n"
                "Q2：快速检查出现“阻断项”怎么办？\n"
                "A：按提示先处理再部署。常见阻断是：中国大陆 IP（不可用）或磁盘空间不足（镜像拉取失败）。\n\n"
                "Q3：部署成功但 Telegram 连不上？\n"
                "A：到「我的代理」点「检测代理」。\n"
                "若显示“外网连通：不可达”，通常是云安全组未放行代理端口。\n"
                "在云控制台添加入站：TCP + 代理端口 + 0.0.0.0/0。\n\n"
                "Q4：检测里“服务器到 Telegram：失败”是什么意思？\n"
                "A：表示服务器出网到 Telegram 不通，即使容器在运行也可能不可用。建议更换线路更好的境外服务器。\n\n"
                "Q5：端口占用了怎么办？\n"
                "A：部署前可在快速检查里修改端口。系统也会自动推荐可用端口，优先用推荐值。\n\n"
                "Q6：重置代理、删除代理分别影响什么？\n"
                "A：重置会生成新 Secret，旧链接立即失效；删除会停止远端容器并移除记录。\n\n"
                "Q7：公开代理为什么会自动下线？\n"
                "A：系统会定时健康检查，连续失败会自动取消公开并通知你。修复后可在详情页重新开启公开。\n\n"
                "Q8：链接点开无反应怎么办？\n"
                "A：关闭其他 VPN/代理，完全退出 Telegram 后重开；优先用 Web 链接导入，再用“检测代理”复查。\n\n"
                "如果你有问题反馈或合作需求，可点击下方按钮联系。"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        button("我的代理", CB_MY_PROXIES, EMOJI["folder"], BTN_BLUE),
                        button("新建代理", CB_NEW_PROXY, EMOJI["deploy"], BTN_GREEN),
                    ],
                    [button("公开代理", CB_PUBLIC_COUNTRIES, EMOJI["globe"], BTN_RED)],
                    [url_button("联系作者", "https://t.me/bigbigbig", EMOJI["link"], BTN_BLUE)],
                ]
            ),
            disable_web_page_preview=True,
        )

    @staticmethod
    def _quick_reply_keyboard() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            [
                [
                    KeyboardButton(
                        text=REPLY_TEXT_NEW_PROXY,
                        style=BTN_GREEN,
                        icon_custom_emoji_id=EMOJI["deploy"],
                    ),
                    KeyboardButton(
                        text=REPLY_TEXT_MY_PROXIES,
                        style=BTN_BLUE,
                        icon_custom_emoji_id=EMOJI["folder"],
                    ),
                    KeyboardButton(
                        text=REPLY_TEXT_PUBLIC_PROXIES,
                        style=BTN_RED,
                        icon_custom_emoji_id=EMOJI["globe"],
                    ),
                ],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user = update.effective_user
        if not query or not user:
            return
        chat = update.effective_chat
        if chat and chat.type != "private":
            await query.answer()
            return
        await query.answer()

        session = await self.store.load_session(user.id) or {}
        data = query.data or ""
        chat_id = query.message.chat_id if query.message else session.get("chat_id")
        bot_msg_id = query.message.message_id if query.message else session.get("bot_msg_id")
        if not chat_id or not bot_msg_id:
            return

        if data == CB_NEW_PROXY:
            session.update(
                {
                    "state": STATE_AWAIT_REMARK_INIT,
                    "chat_id": chat_id,
                    "bot_msg_id": bot_msg_id,
                }
            )
            session.pop("remark", None)
            session.pop("protocol", None)
            session.pop("existing_proxy_id", None)
            session.pop("checks", None)
            await self._refresh_panel(
                context,
                user.id,
                session,
                self._step_remark_init_text(),
                self._remark_init_keyboard(),
            )
            return

        if data in {CB_PROTOCOL_MTPROXY, CB_PROTOCOL_SOCKS5}:
            if session.get("state") != STATE_AWAIT_PROTOCOL:
                return
            protocol = PROTOCOL_SOCKS5 if data == CB_PROTOCOL_SOCKS5 else PROTOCOL_MTPROXY
            session["protocol"] = protocol
            session["state"] = STATE_AWAIT_IP
            await self._refresh_panel(
                context,
                user.id,
                session,
                self._step_ip_text(session, remark=str(session.get("remark") or "")),
                InlineKeyboardMarkup([[button("返回", CB_BACK, EMOJI["cross"], BTN_RED)]]),
            )
            return

        if data == CB_MY_PROXIES:
            await self._render_my_proxies(user.id, chat_id, bot_msg_id, context)
            return

        if data == CB_BACK:
            await self._go_back(user.id, session, context, chat_id, bot_msg_id)
            return

        if data == CB_BACK_MENU:
            await self.store.save_session(
                user.id,
                {"state": STATE_MENU, "chat_id": chat_id, "bot_msg_id": bot_msg_id},
            )
            await self._edit_panel(context, chat_id, bot_msg_id, self._build_menu_text(), self._menu_keyboard())
            return

        if data == CB_USE_ROOT:
            if session.get("state") != STATE_AWAIT_USER:
                return
            session["username"] = "root"
            session["state"] = STATE_AWAIT_PASS
            await self._refresh_panel(
                context,
                user.id,
                session,
                self._step_password_text(session),
                InlineKeyboardMarkup([[button("返回", CB_BACK, EMOJI["cross"], BTN_RED)]]),
            )
            return

        if data == CB_SPEED_TEST:
            if session.get("state") != STATE_TOOLS:
                return
            await self._run_speed_test(user.id, session, context)
            return

        if data == CB_RETRY_CONNECT:
            if not all(session.get(k) for k in ("ip", "port", "username", "password")):
                await self._edit_panel(context, chat_id, bot_msg_id, self._build_menu_text(), self._menu_keyboard())
                return
            async with self._user_guard(user.id, context=context, chat_id=chat_id) as ok:
                if not ok:
                    return
                await self._inspect_after_bind(user.id, session, context)
            return

        if data == CB_CONTINUE_DEPLOY:
            if session.get("state") != STATE_QUICK_CHECK:
                return
            await self._continue_from_quick_check(user.id, session, context)
            return

        if data == CB_MODIFY_PORT:
            if session.get("state") != STATE_QUICK_CHECK:
                return
            await self._prompt_modify_proxy_port(user.id, session, context)
            return

        if data == CB_DEPLOY_PROXY:
            if session.get("state") != STATE_TOOLS:
                return
            async with self._user_guard(user.id, context=context, chat_id=chat_id) as ok:
                if not ok:
                    return
                await self._start_deployment(user.id, session, context, telegram_username=user.username)
            return

        if data == CB_NOOP:
            return

        if data == CB_PROXY_BACK:
            page = int(session.get("my_page") or 0)
            await self._render_my_proxies(user.id, chat_id, bot_msg_id, context, page=page)
            return

        if data == CB_PUBLIC_COUNTRIES:
            page = int(session.get("public_country_page") or 0)
            await self._render_public_countries(user.id, chat_id, bot_msg_id, context, page=page)
            return

        if data.startswith(CB_MY_PAGE_PREFIX):
            page = parse_int_suffix(data, CB_MY_PAGE_PREFIX)
            if page is not None:
                await self._render_my_proxies(user.id, chat_id, bot_msg_id, context, page=page)
            return

        if data.startswith(CB_PUBLIC_LOCATION_PREFIX):
            location_index = parse_int_suffix(data, CB_PUBLIC_LOCATION_PREFIX)
            if location_index is not None:
                await self._render_public_location(
                    user.id,
                    chat_id,
                    bot_msg_id,
                    context,
                    location_index=location_index,
                    page=0,
                )
            return

        if data.startswith(CB_PUBLIC_COUNTRY_PAGE_PREFIX):
            page = parse_int_suffix(data, CB_PUBLIC_COUNTRY_PAGE_PREFIX)
            if page is not None:
                await self._render_public_countries(user.id, chat_id, bot_msg_id, context, page=page)
            return

        if data.startswith(CB_PUBLIC_LOCATION_PAGE_PREFIX):
            payload = data.replace(CB_PUBLIC_LOCATION_PAGE_PREFIX, "", 1)
            try:
                location_raw, page_raw = payload.split(":", 1)
                location_index = int(location_raw)
                page = int(page_raw)
            except ValueError:
                return
            await self._render_public_location(
                user.id,
                chat_id,
                bot_msg_id,
                context,
                location_index=location_index,
                page=page,
            )
            return

        if data.startswith(CB_PROXY_OPEN_PREFIX):
            proxy_id = parse_int_suffix(data, CB_PROXY_OPEN_PREFIX)
            if proxy_id is not None:
                await self._render_proxy_detail(user.id, proxy_id, chat_id, bot_msg_id, context)
            return

        if data.startswith(CB_PROXY_CHECK_PREFIX):
            proxy_id = parse_int_suffix(data, CB_PROXY_CHECK_PREFIX)
            if proxy_id is not None:
                async with self._user_guard(user.id, context=context, chat_id=chat_id) as ok:
                    if not ok:
                        return
                    await self._check_proxy(user.id, proxy_id, chat_id, bot_msg_id, context)
            return

        if data.startswith(CB_MODIFY_REMARK_PREFIX):
            proxy_id = parse_int_suffix(data, CB_MODIFY_REMARK_PREFIX)
            if proxy_id is not None:
                await self._prompt_modify_remark(user.id, proxy_id, chat_id, bot_msg_id, context)
            return

        if data.startswith(CB_MODIFY_PORT_DETAIL_PREFIX):
            proxy_id = parse_int_suffix(data, CB_MODIFY_PORT_DETAIL_PREFIX)
            if proxy_id is not None:
                await self._prompt_modify_port_in_detail(user.id, proxy_id, chat_id, bot_msg_id, context)
            return

        if data.startswith(CB_PROXY_TOGGLE_PUBLIC_PREFIX):
            proxy_id = parse_int_suffix(data, CB_PROXY_TOGGLE_PUBLIC_PREFIX)
            if proxy_id is not None:
                await self._toggle_proxy_public(user.id, proxy_id, chat_id, bot_msg_id, context)
            return


        if data.startswith(CB_PROXY_RESET_PREFIX):
            proxy_id = parse_int_suffix(data, CB_PROXY_RESET_PREFIX)
            if proxy_id is not None:
                await self._confirm_reset_proxy(user.id, proxy_id, chat_id, bot_msg_id, context)
            return

        if data.startswith(CB_PROXY_DELETE_PREFIX):
            proxy_id = parse_int_suffix(data, CB_PROXY_DELETE_PREFIX)
            if proxy_id is not None:
                await self._confirm_delete_proxy(user.id, proxy_id, chat_id, bot_msg_id, context)
            return

        if data.startswith(CB_CONFIRM_RESET_PREFIX):
            proxy_id = parse_int_suffix(data, CB_CONFIRM_RESET_PREFIX)
            if proxy_id is not None:
                async with self._user_guard(user.id, context=context, chat_id=chat_id) as ok:
                    if not ok:
                        return
                    await self._reset_proxy(
                        user.id,
                        proxy_id,
                        chat_id,
                        bot_msg_id,
                        context,
                        telegram_username=user.username,
                    )
            return

        if data.startswith(CB_CONFIRM_DELETE_PREFIX):
            proxy_id = parse_int_suffix(data, CB_CONFIRM_DELETE_PREFIX)
            if proxy_id is not None:
                async with self._user_guard(user.id, context=context, chat_id=chat_id) as ok:
                    if not ok:
                        return
                    await self._delete_proxy(
                        user.id,
                        proxy_id,
                        chat_id,
                        bot_msg_id,
                        context,
                        telegram_username=user.username,
                    )
            return

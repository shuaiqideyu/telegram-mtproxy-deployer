from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

from telegram import InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from bot.config import Settings
from bot.services.database import DatabaseStore
from bot.services.deployer import MTProxyDeployer
from bot.services.memory_store import MemoryStore
from bot.services.public_health_checker import PublicHealthChecker


class BotBase:
    def __init__(
        self,
        settings: Settings,
        store: MemoryStore,
        deployer: MTProxyDeployer,
        db: DatabaseStore,
    ) -> None:
        self.settings = settings
        self.store = store
        self.deployer = deployer
        self.db = db
        self.public_checker: PublicHealthChecker | None = None
        # 用户级互斥锁：确保同一用户的 deploy/reset/delete/inspect 等写操作串行，
        # 防止连点按钮触发重复 SSH 会话或数据库状态不一致。
        self._user_locks: dict[int, asyncio.Lock] = {}

    def set_public_checker(self, checker: PublicHealthChecker) -> None:
        self.public_checker = checker

    def _user_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_locks[user_id] = lock
        return lock

    @asynccontextmanager
    async def _user_guard(
        self,
        user_id: int,
        *,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int | None = None,
    ):
        """用户级互斥守卫。若锁已被占用则轻提示后放弃本次操作。

        用法：
            async with self._user_guard(user_id, context=context, chat_id=chat_id) as ok:
                if not ok:
                    return
                await ...  # 被保护的长流程
        """
        lock = self._user_lock(user_id)
        if lock.locked():
            if chat_id is not None:
                with contextlib.suppress(Exception):
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="⏳ 当前有操作正在进行，请等待完成后再点击。",
                    )
            yield False
            return
        await lock.acquire()
        try:
            yield True
        finally:
            lock.release()

    async def _edit_panel(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def _try_delete(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int,
    ) -> None:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:  # noqa: BLE001
            pass

    async def _refresh_panel(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        user_id: int,
        session: dict[str, object],
        text: str,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> None:
        chat_id = int(session.get("chat_id") or 0)
        bot_msg_id = int(session.get("bot_msg_id") or 0)
        # chat_id 允许负值（群聊 / 频道），仅要求非零。
        if chat_id and bot_msg_id:
            await self._try_delete(context, chat_id, bot_msg_id)
        if not chat_id:
            return
        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
        session["chat_id"] = sent.chat_id
        session["bot_msg_id"] = sent.message_id
        await self.store.save_session(user_id, session)

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

REQUIRED_PYTHON = (3, 14)


def _ensure_supported_python() -> None:
    if sys.version_info[:2] != REQUIRED_PYTHON:
        current = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        required = f"{REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}"
        raise RuntimeError(
            f"当前运行环境是 Python {current}，本项目固定要求 Python {required}，"
            "请使用 python3.14 启动。"
        )


_ensure_supported_python()

from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    MenuButtonCommands,
)
from telegram.ext import Application

from bot.config import get_settings
from bot.handlers import BotHandlers
from bot.services.database import DatabaseStore
from bot.services.deployer import MTProxyDeployer
from bot.services.memory_store import MemoryStore
from bot.services.public_health_checker import PublicHealthChecker
from bot.services.traffic_sampler import TrafficSampler
from bot.utils.password_crypto import init_password_crypto


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def _keepalive_get_me(application: Application) -> None:
    while True:
        await asyncio.sleep(30)
        with contextlib.suppress(Exception):
            await application.bot.get_me()


async def _cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _run_bot() -> None:
    settings = get_settings()
    init_password_crypto(settings.bot_token, settings.password_encryption_key or None)
    store = MemoryStore(session_ttl_seconds=settings.session_ttl_seconds)
    db = DatabaseStore(settings.database_url)
    await db.connect()
    deployer = MTProxyDeployer(settings)
    sampler = TrafficSampler(
        db=db,
        deployer=deployer,
        interval_seconds=settings.traffic_sample_interval_seconds,
    )
    sampler_task: asyncio.Task[None] | None = None
    public_checker_task: asyncio.Task[None] | None = None

    application = (
        Application.builder()
        .token(settings.bot_token)
        .concurrent_updates(True)
        .build()
    )
    application.bot_data["store"] = store

    handlers = BotHandlers(
        settings=settings,
        store=store,
        deployer=deployer,
        db=db,
    )
    handlers.register(application)

    logger.info("Bot 启动中...")
    keepalive_task: asyncio.Task[None] | None = None
    async with application:
        await application.initialize()
        await application.start()
        private_commands = [
            BotCommand("start", "打开主菜单"),
            BotCommand("new", "新建代理"),
            BotCommand("my", "我的代理"),
            BotCommand("help", "使用帮助"),
        ]
        # 只在私聊上下文暴露命令；群组里清空命令表，避免误触。
        await application.bot.set_my_commands(
            private_commands,
            scope=BotCommandScopeAllPrivateChats(),
        )
        await application.bot.delete_my_commands()
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        await application.updater.start_polling(
            allowed_updates=None,
            drop_pending_updates=True,
        )
        public_checker = PublicHealthChecker(
            db=db,
            deployer=deployer,
            store=store,
            bot=application.bot,
            interval_seconds=600,
            max_failures=3,
        )
        handlers.set_public_checker(public_checker)
        sampler_task = asyncio.create_task(sampler.run(), name="traffic-sampler")
        public_checker_task = asyncio.create_task(
            public_checker.run(),
            name="public-health-checker",
        )
        keepalive_task = asyncio.create_task(
            _keepalive_get_me(application),
            name="telegram-keepalive",
        )
        logger.info("Bot 已就绪，等待消息中...")
        try:
            await asyncio.Event().wait()
        finally:
            await _cancel_task(keepalive_task)
            await _cancel_task(public_checker_task)
            await _cancel_task(sampler_task)
    await store.close()
    await db.close()


def main() -> None:
    import uvloop  # noqa: PLC0415

    uvloop.run(_run_bot())


if __name__ == "__main__":
    main()

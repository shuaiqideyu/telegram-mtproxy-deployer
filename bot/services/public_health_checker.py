from __future__ import annotations

import asyncio
import logging

from telegram import Bot

from bot.services.database import DatabaseStore, ProxyRecord
from bot.services.deployer import MTProxyDeployer
from bot.services.memory_store import MemoryStore
from bot.services.ssh import SSHCredentials

logger = logging.getLogger(__name__)


def latency_cache_key(proxy_id: int) -> str:
    return f"public_latency:{proxy_id}"


def public_fail_cache_key(proxy_id: int) -> str:
    return f"public_health_fail:{proxy_id}"


class PublicHealthChecker:
    def __init__(
        self,
        *,
        db: DatabaseStore,
        deployer: MTProxyDeployer,
        store: MemoryStore,
        bot: Bot,
        interval_seconds: int = 600,
        max_failures: int = 3,
    ) -> None:
        self.db = db
        self.deployer = deployer
        self.store = store
        self.bot = bot
        self.interval_seconds = max(interval_seconds, 120)
        self.max_failures = max(max_failures, 1)
        self.semaphore = asyncio.Semaphore(5)

    async def run(self) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                proxies = await self.db.list_public_proxies()
                await asyncio.gather(*(self._check_one(proxy, self.semaphore) for proxy in proxies))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("公开代理健康检查异常: %s", exc)
            await asyncio.sleep(self.interval_seconds)

    def trigger_probe(self, record: ProxyRecord) -> asyncio.Task:
        return asyncio.create_task(self._check_one(record, self.semaphore))

    async def _check_one(self, record: ProxyRecord, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            credentials = SSHCredentials(
                host=record.server_ip,
                port=record.ssh_port,
                username=record.ssh_user,
                password=record.ssh_password,
            )
            ok = False
            latency_ms: int | None = None
            try:
                runtime = await self.deployer.inspect_runtime(
                    credentials,
                    record.proxy_port,
                    include_public_probe=False,
                    include_traffic=False,
                    protocol=record.protocol,
                )
                latency_ms = runtime.tg_latency_ms
                ok = runtime.container_running and runtime.port_listening and runtime.tg_reachable
            except Exception as exc:  # noqa: BLE001
                logger.warning("公开代理检查失败 proxy_id=%s error=%s", record.id, exc)
                ok = False
                latency_ms = None

            store = self.store
            latency_ttl = self.interval_seconds * 2
            latency_key = latency_cache_key(record.id)
            if ok and latency_ms is not None:
                await store.kv_set(latency_key, str(int(latency_ms)), ex=latency_ttl)
            else:
                await store.kv_set(latency_key, "timeout", ex=latency_ttl)

            fail_key = public_fail_cache_key(record.id)
            if ok:
                await store.kv_delete(fail_key)
                return

            fails = await store.kv_incr(fail_key)
            await store.kv_expire(fail_key, 86400)
            if int(fails) < self.max_failures:
                return

            disabled = await self.db.disable_public(record.id)
            await store.kv_delete(fail_key, latency_key)
            if not disabled:
                return
            await self._notify_owner_offline(record)

    async def _notify_owner_offline(self, record: ProxyRecord) -> None:
        try:
            name = (record.remark or "").strip() or f"{record.server_ip}:{record.proxy_port}"
            await self.bot.send_message(
                chat_id=record.user_id,
                text=(
                    "⚠️ 公开代理已自动下线\n\n"
                    f"代理：{name}\n"
                    "原因：连续 3 次健康检查失败（容器/端口/Telegram 连通性异常）\n"
                    "已自动取消公开。修复后可在“我的代理”详情页重新开启。"
                ),
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("公开代理下线通知失败 proxy_id=%s user_id=%s error=%s", record.id, record.user_id, exc)

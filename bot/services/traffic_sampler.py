from __future__ import annotations

import asyncio
import logging

from bot.services.database import DatabaseStore, ProxyRecord
from bot.services.deployer import MTProxyDeployer
from bot.services.ssh import SSHCredentials

logger = logging.getLogger(__name__)


class TrafficSampler:
    def __init__(self, db: DatabaseStore, deployer: MTProxyDeployer, interval_seconds: int) -> None:
        self.db = db
        self.deployer = deployer
        self.interval_seconds = max(interval_seconds, 60)

    async def run(self) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                await self.db.cleanup_traffic_samples(keep_days=8)
                proxies = await self.db.list_active_proxies_for_sampling()
                semaphore = asyncio.Semaphore(5)
                await asyncio.gather(*(self._sample_one(proxy, semaphore) for proxy in proxies))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("采样任务异常: %s", exc)
            await asyncio.sleep(self.interval_seconds)

    async def _sample_one(self, record: ProxyRecord, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            credentials = SSHCredentials(
                host=record.server_ip,
                port=record.ssh_port,
                username=record.ssh_user,
                password=record.ssh_password,
            )
            try:
                sample = await self.deployer.sample_proxy_traffic(
                    credentials,
                    record.proxy_port,
                    protocol=record.protocol,
                )
                await self.db.insert_traffic_sample(
                    proxy_id=record.id,
                    active_connections=sample.active_connections,
                    bytes_in=sample.bytes_in,
                    bytes_out=sample.bytes_out,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("采样代理失败 proxy_id=%s error=%s", record.id, exc)

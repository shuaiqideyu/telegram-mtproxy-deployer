from __future__ import annotations

import asyncio
import time
from typing import Any


class MemoryStore:
    """进程内内存缓存，替代 Redis。

    - 会话（session）：按 user_id 保存 Telegram 操作流程的中间态，写入时按
      ``session_ttl_seconds`` 续期，过期自动丢弃。
    - 通用 KV（kv_*）：用于公开代理延迟与健康检查失败计数等短时缓存，
      支持 ``ex`` 秒级 TTL。

    bot 是 long-polling 单进程，会话与健康缓存允许在重启后清空，因此可以安全
    地以内存替代 Redis；对外仅保留原 ``RedisStore`` 中真正被使用的语义。
    """

    def __init__(self, session_ttl_seconds: int = 600) -> None:
        self._session_ttl = max(int(session_ttl_seconds), 1)
        self._sessions: dict[int, tuple[dict[str, Any], float]] = {}
        self._kv: dict[str, tuple[str, float | None]] = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        async with self._lock:
            self._sessions.clear()
            self._kv.clear()

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    # ---------- Session API ----------

    async def save_session(self, user_id: int, payload: dict[str, Any]) -> None:
        expire_at = self._now() + self._session_ttl
        async with self._lock:
            self._sessions[int(user_id)] = (dict(payload), expire_at)

    async def load_session(self, user_id: int) -> dict[str, Any] | None:
        key = int(user_id)
        async with self._lock:
            entry = self._sessions.get(key)
            if not entry:
                return None
            payload, expire_at = entry
            if expire_at <= self._now():
                self._sessions.pop(key, None)
                return None
            return dict(payload)

    async def update_session(self, user_id: int, **fields: Any) -> dict[str, Any]:
        current = await self.load_session(user_id) or {}
        current.update(fields)
        await self.save_session(user_id, current)
        return current

    async def clear_session(self, user_id: int) -> None:
        async with self._lock:
            self._sessions.pop(int(user_id), None)

    # ---------- 通用 KV API（替代 Redis latency / fail 计数） ----------

    async def kv_set(self, key: str, value: str, *, ex: int | None = None) -> None:
        expire_at: float | None = None
        if ex is not None:
            expire_at = self._now() + max(int(ex), 1)
        async with self._lock:
            self._kv[str(key)] = (str(value), expire_at)

    async def kv_get(self, key: str) -> str | None:
        async with self._lock:
            return self._kv_get_locked(str(key))

    async def kv_mget(self, keys: list[str]) -> list[str | None]:
        async with self._lock:
            return [self._kv_get_locked(str(key)) for key in keys]

    async def kv_delete(self, *keys: str) -> int:
        removed = 0
        async with self._lock:
            for key in keys:
                if self._kv.pop(str(key), None) is not None:
                    removed += 1
        return removed

    async def kv_incr(self, key: str) -> int:
        """对计数 key 自增 1，保留已有 TTL；无则返回 1 且无 TTL。"""
        key = str(key)
        async with self._lock:
            entry = self._kv.get(key)
            now = self._now()
            if entry is None:
                current = 0
                expire_at: float | None = None
            else:
                value, expire_at = entry
                if expire_at is not None and expire_at <= now:
                    current = 0
                    expire_at = None
                else:
                    try:
                        current = int(value)
                    except (TypeError, ValueError):
                        current = 0
            current += 1
            self._kv[key] = (str(current), expire_at)
            return current

    async def kv_expire(self, key: str, ttl: int) -> bool:
        key = str(key)
        async with self._lock:
            entry = self._kv.get(key)
            if entry is None:
                return False
            value, _ = entry
            self._kv[key] = (value, self._now() + max(int(ttl), 1))
            return True

    def _kv_get_locked(self, key: str) -> str | None:
        entry = self._kv.get(key)
        if entry is None:
            return None
        value, expire_at = entry
        if expire_at is not None and expire_at <= self._now():
            self._kv.pop(key, None)
            return None
        return value

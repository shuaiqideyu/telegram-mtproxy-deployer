from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import asyncpg

from bot.utils.password_crypto import decrypt_password, encrypt_password


@dataclass(slots=True)
class ProxyRecord:
    id: int
    user_id: int
    server_ip: str
    ssh_port: int
    ssh_user: str
    ssh_password: str
    proxy_port: int
    tls_domain: str
    secret: str
    tg_link: str
    web_link: str
    os_name: str
    cpu_cores: str
    memory_mb: str
    kernel: str
    location: str
    status: str
    tag_supported: bool
    remark: str
    deploy_config: dict[str, Any]
    protocol: str
    is_public: bool
    created_at: str
    deleted_at: str | None


class DatabaseStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._pool:
            return
        self._pool = await asyncpg.create_pool(
            dsn=self.database_url,
            min_size=2,
            max_size=20,
            init=self._init_connection,
        )
        await self.ensure_schema()

    @staticmethod
    async def _init_connection(conn: asyncpg.Connection) -> None:
        await conn.set_type_codec(
            "jsonb",
            encoder=lambda value: json.dumps(value, ensure_ascii=False),
            decoder=json.loads,
            schema="pg_catalog",
        )
        await conn.set_type_codec(
            "json",
            encoder=lambda value: json.dumps(value, ensure_ascii=False),
            decoder=json.loads,
            schema="pg_catalog",
        )

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    def _ensure_pool(self) -> asyncpg.Pool:
        if not self._pool:
            raise RuntimeError("数据库连接池未初始化，请先调用 connect()")
        return self._pool

    async def ensure_schema(self) -> None:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                CREATE TABLE IF NOT EXISTS proxies (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    server_ip TEXT NOT NULL,
                    ssh_port INTEGER NOT NULL,
                    ssh_user TEXT NOT NULL,
                    ssh_password TEXT NOT NULL,
                    proxy_port INTEGER NOT NULL,
                    tls_domain TEXT NOT NULL DEFAULT '',
                    secret TEXT NOT NULL,
                    tg_link TEXT NOT NULL,
                    web_link TEXT NOT NULL,
                    os_name TEXT NOT NULL DEFAULT '未知',
                    cpu_cores TEXT NOT NULL DEFAULT '未知',
                    memory_mb TEXT NOT NULL DEFAULT '未知',
                    kernel TEXT NOT NULL DEFAULT '未知',
                    location TEXT NOT NULL DEFAULT '未知',
                    remark TEXT NOT NULL DEFAULT '',
                    tag_supported BOOLEAN NOT NULL DEFAULT FALSE,
                    deploy_config JSONB NOT NULL DEFAULT '{}'::jsonb,
                    is_public BOOLEAN NOT NULL DEFAULT FALSE,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    deleted_at TIMESTAMPTZ
                );

                CREATE TABLE IF NOT EXISTS proxy_traffic_samples (
                    id BIGSERIAL PRIMARY KEY,
                    proxy_id BIGINT NOT NULL REFERENCES proxies(id) ON DELETE CASCADE,
                    active_connections INTEGER NOT NULL DEFAULT 0,
                    bytes_in BIGINT NOT NULL DEFAULT 0,
                    bytes_out BIGINT NOT NULL DEFAULT 0,
                    sampled_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
                ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

                ALTER TABLE proxies ADD COLUMN IF NOT EXISTS tls_domain TEXT NOT NULL DEFAULT '';
                ALTER TABLE proxies ADD COLUMN IF NOT EXISTS os_name TEXT NOT NULL DEFAULT '未知';
                ALTER TABLE proxies ADD COLUMN IF NOT EXISTS cpu_cores TEXT NOT NULL DEFAULT '未知';
                ALTER TABLE proxies ADD COLUMN IF NOT EXISTS memory_mb TEXT NOT NULL DEFAULT '未知';
                ALTER TABLE proxies ADD COLUMN IF NOT EXISTS kernel TEXT NOT NULL DEFAULT '未知';
                ALTER TABLE proxies ADD COLUMN IF NOT EXISTS location TEXT NOT NULL DEFAULT '未知';
                ALTER TABLE proxies ADD COLUMN IF NOT EXISTS remark TEXT NOT NULL DEFAULT '';
                ALTER TABLE proxies ADD COLUMN IF NOT EXISTS tag_supported BOOLEAN NOT NULL DEFAULT FALSE;
                ALTER TABLE proxies ADD COLUMN IF NOT EXISTS deploy_config JSONB NOT NULL DEFAULT '{}'::jsonb;
                ALTER TABLE proxies ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT FALSE;
                ALTER TABLE proxies ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
                ALTER TABLE proxies ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
                ALTER TABLE proxies ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

                CREATE INDEX IF NOT EXISTS idx_proxies_user_status_created
                    ON proxies (user_id, status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_proxies_user_endpoint
                    ON proxies (user_id, server_ip, proxy_port, status);
                CREATE INDEX IF NOT EXISTS idx_proxies_public_created
                    ON proxies (is_public, status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_proxy_traffic_proxy_sampled
                    ON proxy_traffic_samples (proxy_id, sampled_at DESC);
                """
            )

    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (id, username, first_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (id)
                DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name
                """,
                user_id,
                username,
                first_name,
            )

    async def create_proxy(self, payload: dict[str, Any]) -> int:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            proxy_id = await conn.fetchval(
                """
                INSERT INTO proxies (
                    user_id,
                    server_ip,
                    ssh_port,
                    ssh_user,
                    ssh_password,
                    proxy_port,
                    tls_domain,
                    secret,
                    tg_link,
                    web_link,
                    os_name,
                    cpu_cores,
                    memory_mb,
                    kernel,
                    location,
                    remark,
                    tag_supported,
                    deploy_config,
                    status
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, 'active'
                )
                RETURNING id
                """,
                payload["user_id"],
                payload["server_ip"],
                payload["ssh_port"],
                payload["ssh_user"],
                encrypt_password(payload["ssh_password"]),
                payload["proxy_port"],
                payload["tls_domain"],
                payload["secret"],
                payload["tg_link"],
                payload["web_link"],
                payload["os_name"],
                payload["cpu_cores"],
                payload["memory_mb"],
                payload["kernel"],
                payload["location"],
                payload.get("remark", ""),
                payload.get("tag_supported", False),
                payload.get("deploy_config") or {},
            )
        return int(proxy_id)

    async def get_active_proxy_by_endpoint(
        self,
        user_id: int,
        server_ip: str,
        proxy_port: int,
    ) -> ProxyRecord | None:
        """查找同一用户同一服务器端口上的活跃代理，用于覆盖同端口部署。"""
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM proxies
                WHERE user_id = $1
                  AND server_ip = $2
                  AND proxy_port = $3
                  AND status = 'active'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                user_id,
                server_ip,
                proxy_port,
            )
        if not row:
            return None
        return self._row_to_record(row)

    async def overwrite_proxy(
        self,
        *,
        proxy_id: int,
        user_id: int,
        ssh_port: int,
        ssh_user: str,
        ssh_password: str,
        proxy_port: int,
        tls_domain: str,
        secret: str,
        tg_link: str,
        web_link: str,
        os_name: str,
        cpu_cores: str,
        memory_mb: str,
        kernel: str,
        location: str,
        tag_supported: bool = False,
        deploy_config: dict[str, Any] | None = None,
    ) -> None:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE proxies
                SET
                    ssh_port = $1,
                    ssh_user = $2,
                    ssh_password = $3,
                    proxy_port = $4,
                    tls_domain = $5,
                    secret = $6,
                    tg_link = $7,
                    web_link = $8,
                    os_name = $9,
                    cpu_cores = $10,
                    memory_mb = $11,
                    kernel = $12,
                    location = $13,
                    tag_supported = $14,
                    deploy_config = $15,
                    status = 'active',
                    deleted_at = NULL,
                    created_at = now()
                WHERE id = $16 AND user_id = $17
                """,
                ssh_port,
                ssh_user,
                encrypt_password(ssh_password),
                proxy_port,
                tls_domain,
                secret,
                tg_link,
                web_link,
                os_name,
                cpu_cores,
                memory_mb,
                kernel,
                location,
                tag_supported,
                deploy_config or {},
                proxy_id,
                user_id,
            )

    async def list_user_proxies(self, user_id: int, include_deleted: bool = False) -> list[ProxyRecord]:
        pool = self._ensure_pool()
        where_clause = "user_id = $1" if include_deleted else "user_id = $1 AND status = 'active'"
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT *
                FROM proxies
                WHERE {where_clause}
                ORDER BY created_at DESC
                """,
                user_id,
            )
        return [self._row_to_record(row) for row in rows]

    async def list_public_proxies(self) -> list[ProxyRecord]:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM proxies
                WHERE status = 'active' AND is_public = TRUE
                ORDER BY created_at DESC
                """
            )
        return [self._row_to_record(row) for row in rows]

    async def count_public_by_location(self) -> list[tuple[str, int]]:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    COALESCE(NULLIF(TRIM(location), ''), '未知') AS location,
                    COUNT(*)::int AS total
                FROM proxies
                WHERE status = 'active' AND is_public = TRUE
                GROUP BY 1
                ORDER BY total DESC, location ASC
                """
            )
        return [(str(row["location"]), int(row["total"])) for row in rows]

    async def list_public_by_location(self, location: str, *, limit: int, offset: int = 0) -> list[ProxyRecord]:
        pool = self._ensure_pool()
        normalized = location.strip() or "未知"
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM proxies
                WHERE status = 'active'
                  AND is_public = TRUE
                  AND COALESCE(NULLIF(TRIM(location), ''), '未知') = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
                """,
                normalized,
                max(limit, 1),
                max(offset, 0),
            )
        return [self._row_to_record(row) for row in rows]

    async def get_proxy(self, user_id: int, proxy_id: int) -> ProxyRecord | None:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM proxies
                WHERE id = $1 AND user_id = $2
                LIMIT 1
                """,
                proxy_id,
                user_id,
            )
        if not row:
            return None
        return self._row_to_record(row)

    async def reset_proxy(
        self,
        *,
        proxy_id: int,
        user_id: int,
        secret: str,
        tg_link: str,
        web_link: str,
        os_name: str,
        cpu_cores: str,
        memory_mb: str,
        kernel: str,
        location: str,
        tag_supported: bool = False,
        deploy_config: dict[str, Any] | None = None,
    ) -> None:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE proxies
                SET
                    secret = $1,
                    tg_link = $2,
                    web_link = $3,
                    os_name = $4,
                    cpu_cores = $5,
                    memory_mb = $6,
                    kernel = $7,
                    location = $8,
                    tag_supported = $9,
                    deploy_config = $10,
                    status = 'active',
                    deleted_at = NULL
                WHERE id = $11 AND user_id = $12
                """,
                secret,
                tg_link,
                web_link,
                os_name,
                cpu_cores,
                memory_mb,
                kernel,
                location,
                tag_supported,
                deploy_config or {},
                proxy_id,
                user_id,
            )

    async def delete_proxy(self, user_id: int, proxy_id: int) -> None:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE proxies
                SET status = 'deleted', deleted_at = now()
                WHERE id = $1 AND user_id = $2
                """,
                proxy_id,
                user_id,
            )

    async def update_proxy_remark(self, user_id: int, proxy_id: int, remark: str) -> None:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE proxies
                SET remark = $1
                WHERE id = $2 AND user_id = $3 AND status = 'active'
                """,
                remark,
                proxy_id,
                user_id,
            )

    async def update_proxy_port(self, user_id: int, proxy_id: int, proxy_port: int) -> None:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE proxies
                SET proxy_port = $1
                WHERE id = $2 AND user_id = $3 AND status = 'active'
                """,
                proxy_port,
                proxy_id,
                user_id,
            )

    async def set_proxy_public(self, user_id: int, proxy_id: int, is_public: bool) -> bool:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            updated = await conn.execute(
                """
                UPDATE proxies
                SET is_public = $1
                WHERE id = $2 AND user_id = $3 AND status = 'active'
                """,
                is_public,
                proxy_id,
                user_id,
            )
        return updated.endswith(" 1")

    async def disable_public(self, proxy_id: int) -> bool:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            updated = await conn.execute(
                """
                UPDATE proxies
                SET is_public = FALSE
                WHERE id = $1 AND status = 'active' AND is_public = TRUE
                """,
                proxy_id,
            )
        return updated.endswith(" 1")

    async def list_active_proxies_for_sampling(self) -> list[ProxyRecord]:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *
                FROM proxies
                WHERE status = 'active'
                ORDER BY id DESC
                """
            )
        return [self._row_to_record(row) for row in rows]

    async def insert_traffic_sample(
        self,
        proxy_id: int,
        active_connections: int,
        bytes_in: int,
        bytes_out: int,
    ) -> None:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO proxy_traffic_samples (proxy_id, active_connections, bytes_in, bytes_out)
                VALUES ($1, $2, $3, $4)
                """,
                proxy_id,
                active_connections,
                bytes_in,
                bytes_out,
            )

    async def cleanup_traffic_samples(self, *, keep_days: int = 8) -> None:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM proxy_traffic_samples
                WHERE sampled_at < now() - make_interval(days => $1::int)
                """,
                keep_days,
            )

    async def get_proxy_traffic_stats(self, proxy_id: int) -> dict[str, int]:
        pool = self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH latest AS (
                    SELECT active_connections
                    FROM proxy_traffic_samples
                    WHERE proxy_id = $1
                    ORDER BY sampled_at DESC
                    LIMIT 1
                ),
                win_1h AS (
                    SELECT
                        COALESCE(MAX(bytes_in) - MIN(bytes_in), 0) AS in_1h,
                        COALESCE(MAX(bytes_out) - MIN(bytes_out), 0) AS out_1h
                    FROM proxy_traffic_samples
                    WHERE proxy_id = $1
                      AND sampled_at >= now() - interval '1 hour'
                ),
                win_1d AS (
                    SELECT
                        COALESCE(MAX(bytes_in) - MIN(bytes_in), 0) AS in_1d,
                        COALESCE(MAX(bytes_out) - MIN(bytes_out), 0) AS out_1d
                    FROM proxy_traffic_samples
                    WHERE proxy_id = $1
                      AND sampled_at >= now() - interval '24 hour'
                )
                SELECT
                    COALESCE((SELECT active_connections FROM latest), 0) AS latest_connections,
                    COALESCE((SELECT in_1h FROM win_1h), 0) AS traffic_1h_in,
                    COALESCE((SELECT out_1h FROM win_1h), 0) AS traffic_1h_out,
                    COALESCE((SELECT in_1d FROM win_1d), 0) AS traffic_1d_in,
                    COALESCE((SELECT out_1d FROM win_1d), 0) AS traffic_1d_out
                """
                ,
                proxy_id,
            )
        if not row:
            return {
                "latest_connections": 0,
                "traffic_1h_in": 0,
                "traffic_1h_out": 0,
                "traffic_1d_in": 0,
                "traffic_1d_out": 0,
            }
        return {
            "latest_connections": int(row["latest_connections"] or 0),
            "traffic_1h_in": int(row["traffic_1h_in"] or 0),
            "traffic_1h_out": int(row["traffic_1h_out"] or 0),
            "traffic_1d_in": int(row["traffic_1d_in"] or 0),
            "traffic_1d_out": int(row["traffic_1d_out"] or 0),
        }

    @staticmethod
    def _row_to_record(row: asyncpg.Record) -> ProxyRecord:
        deploy_raw = row.get("deploy_config")
        if isinstance(deploy_raw, dict):
            deploy_config = deploy_raw
        elif isinstance(deploy_raw, str):
            try:
                parsed = json.loads(deploy_raw)
                deploy_config = parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                deploy_config = {}
        else:
            deploy_config = {}
        return ProxyRecord(
            id=row["id"],
            user_id=row["user_id"],
            server_ip=row["server_ip"],
            ssh_port=row["ssh_port"],
            ssh_user=row["ssh_user"],
            ssh_password=decrypt_password(row["ssh_password"]),
            proxy_port=row["proxy_port"],
            tls_domain=row["tls_domain"] or "",
            secret=row["secret"],
            tg_link=row["tg_link"],
            web_link=row["web_link"],
            os_name=row["os_name"] or "未知",
            cpu_cores=row["cpu_cores"] or "未知",
            memory_mb=row["memory_mb"] or "未知",
            kernel=row["kernel"] or "未知",
            location=row["location"] or "未知",
            status=row["status"],
            tag_supported=bool(row.get("tag_supported", False)),
            remark=row.get("remark") or "",
            deploy_config=deploy_config,
            protocol=str(deploy_config.get("protocol") or "mtproxy"),
            is_public=bool(row.get("is_public", False)),
            created_at=row["created_at"].isoformat(),
            deleted_at=row["deleted_at"].isoformat() if row["deleted_at"] else None,
        )

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    load_dotenv()


EMOJI = {
    "server": "5942734685976138521",
    "shield": "6030537007350944596",
    "rocket": "6028346797368283073",
    "check": "5774022692642492953",
    "cross": "5774077015388852135",
    "lock": "6037249452824072506",
    "key": "6037496202990194718",
    "gear": "5904258298764334001",
    "link": "6028171274939797252",
    "user": "6032994772321309200",
    "globe": "5776233299424843260",
    "speed": "5884428842780594914",
    "info": "6028435952299413210",
    "deploy": "6039729023343400390",
    "star": "6028338546736107668",
    "warn": "6030563507299160824",
    "download": "6037157012242960559",
    "folder": "6037475557082403885",
    "clock": "5850317551090800862",
    "diamond": "5776023601941582822",
    "party": "6041731551845159060",
    "robot": "6030400221232501136",
    "root": "6032594876506312598",
    # ApplicationEmoji pack — 测速图标
    "app_telegram": "5330237710655306682",  # index 26
    "app_youtube":  "5334681713316479679",  # index 43
    "app_netflix":  "5318911503938634641",  # index 55
    "app_github":   "5359584650958226302",  # index 81
    "app_x":        "5330337435500951363",  # index 41
    "app_whatsapp": "5334998226636390258",  # index 40
    "app_spotify":  "5345818383126384016",  # index 80
    "app_google":   "5363810946022131056",  # index 90 (Chrome)
}


def tg_emoji(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} 未配置，请先填写 .env")
    return value


def _required_int_env(name: str) -> int:
    value = _required_env(name)
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数，当前值为 {value!r}") from exc


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数，当前值为 {value!r}") from exc


@dataclass(slots=True)
class Settings:
    bot_token: str
    database_url: str
    admin_user_id: int
    proxy_port: int
    tls_domain: str
    tls_domain_candidates: tuple[str, ...]
    mtp_tag: str
    mtp_image: str
    session_ttl_seconds: int
    deploy_timeout_seconds: int
    command_timeout_seconds: int
    traffic_sample_interval_seconds: int
    password_encryption_key: str


def get_settings() -> Settings:
    bot_token = _required_env("BOT_TOKEN")
    tls_domain = os.getenv("TLS_DOMAIN", "cloudflare.com").strip() or "cloudflare.com"
    tls_candidates_raw = os.getenv(
        "TLS_DOMAIN_CANDIDATES",
        "cloudflare.com,www.cloudflare.com,www.google.com",
    )
    tls_candidates = tuple(
        domain.strip() for domain in tls_candidates_raw.split(",") if domain.strip()
    ) or (tls_domain,)

    return Settings(
        bot_token=bot_token,
        database_url=_required_env("DATABASE_URL"),
        admin_user_id=_required_int_env("ADMIN_USER_ID"),
        proxy_port=_int_env("PROXY_PORT", 443),
        tls_domain=tls_domain,
        tls_domain_candidates=tls_candidates,
        mtp_tag=os.getenv("MTP_TAG", "").strip(),
        mtp_image=os.getenv("MTP_IMAGE", "ghcr.io/shuaiqideyu/mtproxy:master").strip(),
        session_ttl_seconds=_int_env("SESSION_TTL_SECONDS", 600),
        deploy_timeout_seconds=_int_env("DEPLOY_TIMEOUT_SECONDS", 300),
        command_timeout_seconds=_int_env("COMMAND_TIMEOUT_SECONDS", 1800),
        traffic_sample_interval_seconds=_int_env(
            "TRAFFIC_SAMPLE_INTERVAL_SECONDS",
            900,
        ),
        password_encryption_key=os.getenv("PASSWORD_ENCRYPTION_KEY", "").strip(),
    )

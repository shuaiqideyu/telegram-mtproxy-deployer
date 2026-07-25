from __future__ import annotations

import secrets
import string


def generate_secret() -> str:
    return secrets.token_hex(16)


def build_fake_tls_secret(secret: str, domain: str) -> str:
    normalized = secret.strip().lower()
    if len(normalized) != 32 or any(ch not in string.hexdigits for ch in normalized):
        raise ValueError("secret 必须是 32 位十六进制字符串")

    domain_hex = domain.encode("utf-8").hex()
    return f"ee{normalized}{domain_hex}"


def build_proxy_links(host: str, port: int, secret: str, domain: str) -> dict[str, str]:
    """生成 TG 代理分享链接。host 可以是 IP 或域名（如 hk0001.proxy.example.com）。"""
    client_secret = build_fake_tls_secret(secret, domain)
    tg_link = f"tg://proxy?server={host}&port={port}&secret={client_secret}"
    web_link = f"https://t.me/proxy?server={host}&port={port}&secret={client_secret}"
    return {"tg": tg_link, "web": web_link, "client_secret": client_secret}


def mask_secret(secret: str, keep: int = 0) -> str:
    if keep <= 0:
        return "******"
    return f"{secret[:keep]}{'*' * max(len(secret) - keep, 0)}"

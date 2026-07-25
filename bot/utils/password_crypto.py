"""SSH 密码对称加密工具。

- 使用 Fernet (AES-128-CBC + HMAC-SHA256) 对数据库中保存的 ssh_password 做透明加解密。
- 默认密钥由 BOT_TOKEN 派生（SHA-256 → urlsafe_b64 32 字节），用户无须额外配置。
- 也可通过环境变量 PASSWORD_ENCRYPTION_KEY 显式指定 44 字节 base64 密钥覆盖默认值。
- 向后兼容：decrypt_password 对非 Fernet 格式的历史明文值直接原样返回，
  这些旧记录会在下一次 overwrite_proxy / reset_proxy 时自动升级为密文。
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None


def init_password_crypto(bot_token: str, override_key: str | None = None) -> None:
    """初始化全局 Fernet 实例。应用启动时调用一次。"""
    global _fernet
    if override_key:
        try:
            Fernet(override_key.encode("ascii"))
            key_bytes = override_key.encode("ascii")
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                "PASSWORD_ENCRYPTION_KEY 无效：必须是 44 字节 urlsafe base64 字符串"
            ) from exc
    else:
        if not bot_token:
            raise ValueError("初始化密码加密需要 BOT_TOKEN 或 PASSWORD_ENCRYPTION_KEY")
        digest = hashlib.sha256(f"MTProxyBot::{bot_token}".encode("utf-8")).digest()
        key_bytes = base64.urlsafe_b64encode(digest)
    _fernet = Fernet(key_bytes)


def encrypt_password(plain: str) -> str:
    """加密明文密码。空字符串原样返回。"""
    if not plain:
        return ""
    if _fernet is None:
        raise RuntimeError("密码加密模块尚未初始化，请先调用 init_password_crypto")
    return _fernet.encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_password(value: str | None) -> str:
    """解密密文。非 Fernet 格式的历史明文会原样返回以保证兼容。"""
    if not value:
        return ""
    if _fernet is None:
        return value
    try:
        return _fernet.decrypt(value.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return value


def is_encrypted(value: str | None) -> bool:
    """粗略判断字段是否已加密（仅用于诊断/迁移）。"""
    if not value:
        return False
    return value.startswith("gAAAA")

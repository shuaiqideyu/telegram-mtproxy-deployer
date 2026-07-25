from __future__ import annotations

import ipaddress
import re


_HOST_PORT_PATTERN = re.compile(r"^(?P<host>[^:\s]+)(?::(?P<port>\d{1,5}))?$")


class ValidationError(ValueError):
    """输入校验失败。"""


def parse_host_and_port(raw: str, default_port: int = 22) -> tuple[str, int]:
    text = raw.strip()
    if not text:
        raise ValidationError("请输入服务器 IP")

    match = _HOST_PORT_PATTERN.match(text)
    if not match:
        raise ValidationError("格式错误，请使用 IP 或 IP:端口")

    host = match.group("host")
    port_text = match.group("port")

    try:
        ip = str(ipaddress.IPv4Address(host))
    except ipaddress.AddressValueError as exc:
        raise ValidationError("IP 地址无效，请输入合法 IPv4") from exc

    port = default_port
    if port_text:
        port = int(port_text)
        if port < 1 or port > 65535:
            raise ValidationError("端口必须在 1-65535 范围内")

    return ip, port


def validate_username(raw: str) -> str:
    username = raw.strip()
    if not username:
        raise ValidationError("用户名不能为空")
    if len(username) > 64:
        raise ValidationError("用户名过长")
    if " " in username:
        raise ValidationError("用户名不能包含空格")
    return username


def validate_proxy_port(raw: str) -> int:
    text = raw.strip()
    if not text:
        raise ValidationError("端口不能为空")
    if not text.isdigit():
        raise ValidationError("端口必须是数字")
    port = int(text)
    if port < 1 or port > 65535:
        raise ValidationError("端口必须在 1-65535 范围内")
    return port

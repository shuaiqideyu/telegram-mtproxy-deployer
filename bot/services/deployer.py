from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import secrets
import string

logger = logging.getLogger(__name__)
from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from bot.config import Settings
from bot.services.deploy_config import DeployConfig
from bot.services.ssh import AsyncSSHClient, SSHCredentials, SSHError
from bot.utils.country import COUNTRY_ZH_MAP
from bot.utils.crypto import build_proxy_links, generate_secret


ProgressCallback = Callable[[str], Awaitable[None]]


SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SAFE_SHELL_PREFIX = f"export PATH={SAFE_PATH}:$PATH; "


SYSCTL_CONFIG = """# ── TCP 拥塞控制（BBR）──
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr

# ── Socket 缓冲区（最大化吞吐）──
net.core.rmem_max = 134217728
net.core.wmem_max = 134217728
net.core.rmem_default = 262144
net.core.wmem_default = 262144
net.ipv4.tcp_rmem = 4096 262144 134217728
net.ipv4.tcp_wmem = 4096 262144 134217728
net.ipv4.udp_rmem_min = 8192
net.ipv4.udp_wmem_min = 8192

# ── 连接队列（高并发）──
net.core.somaxconn = 65535
net.core.netdev_max_backlog = 65535
net.ipv4.tcp_max_syn_backlog = 65535

# ── TCP 性能优化 ──
net.ipv4.tcp_fastopen = 3
net.ipv4.tcp_slow_start_after_idle = 0
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_keepalive_time = 300
net.ipv4.tcp_keepalive_intvl = 30
net.ipv4.tcp_keepalive_probes = 3
net.ipv4.tcp_max_tw_buckets = 262144
net.ipv4.ip_local_port_range = 1024 65535
net.ipv4.tcp_sack = 1
net.ipv4.tcp_timestamps = 1
net.ipv4.tcp_no_metrics_save = 1
net.ipv4.tcp_abort_on_overflow = 0
net.ipv4.tcp_notsent_lowat = 131072
net.ipv4.tcp_adv_win_scale = 2

# ── 额外 Socket 内存池（高并发场景）──
net.core.optmem_max = 67108864

# ── 连接跟踪 ──
net.netfilter.nf_conntrack_max = 1048576

# ── 文件句柄与进程数 ──
fs.file-max = 2097152
fs.nr_open = 2097152
kernel.pid_max = 4194304
"""

class DeploymentError(RuntimeError):
    """部署流程异常。"""

    def __init__(self, message: str, *, details: str | None = None) -> None:
        super().__init__(message)
        self.details = details


@dataclass(slots=True)
class DeploymentResult:
    ip: str
    port: int
    tls_domain: str
    secret: str
    tg_link: str
    web_link: str
    os_name: str
    cpu_cores: str
    memory_mb: str
    kernel: str
    location: str
    tag_supported: bool
    deploy_config: dict[str, Any]
    protocol: str
    port_warning: str | None = None
    id: int | None = None


@dataclass(slots=True)
class TrafficSample:
    active_connections: int
    bytes_in: int
    bytes_out: int


@dataclass(slots=True)
class ProbeResult:
    local_ok: bool
    public_ok: bool
    public_ip: str
    auto_fixed: bool = False


@dataclass(slots=True)
class ProxyRuntimeStatus:
    """一次 SSH 会话聚合回收的代理运行时信息。"""

    container_status: str
    container_running: bool
    uptime: str | None
    port_listening: bool
    public_reachable: bool
    tg_reachable: bool
    tg_latency_ms: int | None
    traffic: TrafficSample | None


# (显示名, emoji_key, url) — 按名称长度升序
LATENCY_TARGETS: list[tuple[str, str, str]] = [
    ("X",        "app_x",        "https://x.com"),
    ("YouTube",  "app_youtube",  "https://www.youtube.com"),
    ("Telegram", "app_telegram", "https://core.telegram.org"),
]

LatencyResult = dict[str, tuple[float | None, str]]  # label -> (ms, emoji_key)
PREFERRED_PROXY_PORTS: tuple[int, ...] = (443, 8443, 2053, 2083)


class MTProxyDeployer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _mtproxy_container_name(proxy_port: int) -> str:
        return f"mtproxy-{proxy_port}"

    @staticmethod
    def _mtproxy_config_path(proxy_port: int) -> str:
        return f"/tmp/mtproxy_config_{proxy_port}.py"

    @staticmethod
    def _socks5_service_name(proxy_port: int) -> str:
        return f"microsocks-tg-{proxy_port}.service"

    async def deploy(
        self,
        credentials: SSHCredentials,
        on_progress: ProgressCallback,
        proxy_port: int | None = None,
        config: DeployConfig | None = None,
        protocol: str = "mtproxy",
    ) -> DeploymentResult:
        if protocol == "socks5":
            return await self._deploy_socks5(credentials, on_progress, proxy_port=proxy_port)

        deploy_config = config or DeployConfig()
        deploy_config_dict = deploy_config.to_dict()
        deploy_config_dict["protocol"] = "mtproxy"
        deploy_config_dict["ad_tag"] = (deploy_config.ad_tag if deploy_config.ad_tag is not None else self.settings.mtp_tag).strip()
        requested_port = proxy_port or self.settings.proxy_port
        async with AsyncSSHClient(
            credentials,
            command_timeout=self.settings.command_timeout_seconds,
        ) as ssh:
            await on_progress(self._progress_text(3, "准备运行环境", "校验基础工具..."))
            # 不再包 wait_for：低配机器 apt 装基础工具可能需要十几分钟，
            # SSH 死掉会由 keepalive 抛 SSHError，活着就耐心等。
            await self._bootstrap_environment(ssh, on_progress)
            await on_progress(self._progress_text(8, "检测服务器", "正在读取系统信息，通常 3-8 秒..."))
            checks = await self._collect_environment(ssh, requested_port, strict_port=False)

            country = self._extract_country(checks.get("location_raw", ""))
            if country == "CN":
                raise DeploymentError(
                    "检测到服务器位于中国大陆（IP 归属地：中国）。\n"
                    "MTProxy 代理必须部署在境外服务器才能使用，请更换服务器后重试。"
                )
            await on_progress(self._progress_text(20, "检测服务器", f"完成 · {checks['os_name']} · {checks['location']}"))
            await on_progress(self._progress_text(22, "智能选端口", "正在检测端口占用并选择最佳端口..."))
            selected_port, selected_reason = await self._select_best_port(
                ssh,
                requested_port,
                user_specified=proxy_port is not None,
                protocol="mtproxy",
            )
            container_name = self._mtproxy_container_name(selected_port)
            config_path = self._mtproxy_config_path(selected_port)
            deploy_config_dict["container_name"] = container_name
            deploy_config_dict["config_path"] = config_path
            await on_progress(self._progress_text(23, "智能选端口", f"已选择端口 {selected_port}（{selected_reason}）"))

            await on_progress(self._progress_text(24, "检测网络线路", "测试服务器出网质量..."))
            network_mode = await self._detect_network_env(ssh)
            network_detail = "国内线路，已启用加速" if network_mode == "cn_mirror" else "海外直连，无需加速"
            await on_progress(self._progress_text(28, "检测网络线路", network_detail))
            await on_progress(self._progress_text(30, "验证域名", "验证 TLS 伪装域名兼容性..."))
            validated_domain = deploy_config.tls_domain or await self._validate_tls_domain(ssh)
            await on_progress(self._progress_text(33, "验证域名", f"已选择域名 {validated_domain}"))

            bbr_supported = checks.get("bbr_supported", False)
            if bbr_supported:
                await on_progress(self._progress_text(35, "优化网络", "写入内核参数，开启 BBR..."))
                await self._apply_sysctl(ssh, checks["kernel"])
                await on_progress(self._progress_text(50, "优化网络", "BBR 已开启"))
            else:
                await on_progress(self._progress_text(50, "优化网络", "内核不支持 BBR，已跳过"))

            docker_is_missing = checks["docker"] == "MISSING"
            if docker_is_missing:
                await on_progress(self._progress_text(55, "安装 Docker", "未检测到 Docker，正在安装（低配机器可能 5-20 分钟，请耐心等待）..."))
            else:
                await on_progress(self._progress_text(55, "准备运行环境", "Docker 已就绪，配置防火墙..."))
            # 全程不再设外层 wait_for —— SSH keepalive 会在会话真死时快速兜底。
            await self._ensure_docker(ssh, checks["docker"], network_mode=network_mode)
            await self._open_firewall(ssh, checks["firewall"], selected_port)
            await on_progress(self._progress_text(65, "准备运行环境", "运行环境就绪"))

            await on_progress(self._progress_text(70, "准备镜像", "检查本地镜像..."))
            secret = generate_secret()
            try:
                await self._ensure_master_image(ssh, on_progress, network_mode=network_mode)
                await on_progress(self._progress_text(90, "启动容器", "镜像就绪，正在启动代理容器..."))
                await self._run_mtproxy(ssh, secret, selected_port, validated_domain, checks, deploy_config)
            except Exception as exc:  # noqa: BLE001
                # 把原始堆栈留到日志，面向用户的通用消息则保留"请重试"。
                logger.exception("部署流程异常（host=%s port=%s）: %s", credentials.host, selected_port, exc)
                await ssh.run(f"docker rm -f {container_name} 2>/dev/null || true", check=False)
                logs = await ssh.run(f"docker logs --tail 20 {container_name} 2>/dev/null || true", check=False)
                log_text = (logs.stdout or logs.stderr or "").strip()
                details = f"容器日志（最近 20 行）：\n{log_text}" if log_text else None
                if isinstance(exc, DeploymentError):
                    raise DeploymentError(str(exc), details=details) from exc
                raise DeploymentError("部署流程失败，请重试。", details=details) from exc
            await on_progress(self._progress_text(95, "验证代理", "探测端口可达性..."))
            probe = await self._probe_proxy_port(ssh, selected_port, host_hint=credentials.host)
            if not probe.local_ok:
                raise DeploymentError(
                    f"代理容器已启动，但端口 {selected_port} 在服务器本机不可达。"
                    "请检查容器状态并重试。"
                )

            port_warning: str | None = None
            if probe.public_ok:
                await on_progress(self._progress_text(98, "验证代理", f"端口 {selected_port} 对外可达 ✓"))
            else:
                await on_progress(self._progress_text(96, "验证代理", "外网探测失败，尝试自动修复网络规则..."))
                await self._try_fix_port_access(ssh, selected_port, checks["firewall"])
                retry_probe = await self._probe_proxy_port(ssh, selected_port, host_hint=credentials.host)
                if not retry_probe.local_ok:
                    raise DeploymentError(
                        f"端口 {selected_port} 修复后仍在服务器本机不可达，请检查容器状态。"
                    )
                if retry_probe.public_ok:
                    await on_progress(self._progress_text(98, "验证代理", f"端口 {selected_port} 已自动修复并对外可达 ✓"))
                else:
                    port_warning = self._build_port_warning(selected_port)
                    await on_progress(
                        self._progress_text(
                            98,
                            "验证代理",
                            f"端口 {selected_port} 本机可达，但外网不可达（通常是云安全组未放行）",
                        )
                    )

            links = build_proxy_links(
                host=credentials.host,
                port=selected_port,
                secret=secret,
                domain=validated_domain,
            )

            return DeploymentResult(
                ip=credentials.host,
                port=selected_port,
                tls_domain=validated_domain,
                secret=secret,
                tg_link=links["tg"],
                web_link=links["web"],
                os_name=checks["os_name"],
                cpu_cores=checks["cpu"],
                memory_mb=checks["memory"],
                kernel=checks["kernel"],
                location=checks["location"],
                tag_supported=bool(deploy_config.ad_tag or self.settings.mtp_tag),
                deploy_config=deploy_config_dict,
                protocol="mtproxy",
                port_warning=port_warning,
            )

    async def _deploy_socks5(
        self,
        credentials: SSHCredentials,
        on_progress: ProgressCallback,
        proxy_port: int | None = None,
    ) -> DeploymentResult:
        requested_port = proxy_port or self.settings.proxy_port
        username = "tg"
        password = self._generate_socks5_password()

        async with AsyncSSHClient(
            credentials,
            command_timeout=self.settings.command_timeout_seconds,
        ) as ssh:
            await on_progress(self._progress_text(3, "准备运行环境", "校验基础工具..."))
            await self._bootstrap_environment(ssh, on_progress)
            await on_progress(self._progress_text(12, "检测服务器", "正在读取系统信息..."))
            checks = await self._collect_environment(
                ssh,
                requested_port,
                strict_port=False,
                protocol="socks5",
            )
            await on_progress(self._progress_text(20, "检测服务器", f"完成 · {checks['os_name']} · {checks['location']}"))

            await on_progress(self._progress_text(25, "智能选端口", "正在选择 SOCKS5 端口..."))
            selected_port, selected_reason = await self._select_best_port(
                ssh,
                requested_port,
                user_specified=proxy_port is not None,
                protocol="socks5",
            )
            service_name = self._socks5_service_name(selected_port)
            await on_progress(self._progress_text(30, "智能选端口", f"已选择端口 {selected_port}（{selected_reason}）"))

            if checks.get("bbr_supported"):
                await on_progress(self._progress_text(40, "优化网络", "写入内核参数，开启 BBR..."))
                await self._apply_sysctl(ssh, checks["kernel"])
                await on_progress(self._progress_text(55, "优化网络", "BBR 已开启"))
            else:
                await on_progress(self._progress_text(55, "优化网络", "内核不支持 BBR，已跳过"))

            await on_progress(self._progress_text(65, "安装 SOCKS5", "安装 microsocks 并配置 systemd..."))
            await self._ensure_microsocks(ssh)
            await self._open_firewall(ssh, checks["firewall"], selected_port)
            await self._run_socks5_service(ssh, selected_port, username, password)

            await on_progress(self._progress_text(90, "验证代理", "探测端口可达性..."))
            probe = await self._probe_proxy_port(ssh, selected_port, host_hint=credentials.host)
            if not probe.local_ok:
                raise DeploymentError(
                    f"SOCKS5 服务已启动，但端口 {selected_port} 在服务器本机不可达。"
                )

            port_warning: str | None = None
            if probe.public_ok:
                await on_progress(self._progress_text(98, "验证代理", f"端口 {selected_port} 对外可达 ✓"))
            else:
                await self._try_fix_port_access(ssh, selected_port, checks["firewall"])
                retry_probe = await self._probe_proxy_port(ssh, selected_port, host_hint=credentials.host)
                if retry_probe.public_ok:
                    await on_progress(self._progress_text(98, "验证代理", f"端口 {selected_port} 已自动修复并对外可达 ✓"))
                else:
                    port_warning = self._build_port_warning(selected_port)
                    await on_progress(
                        self._progress_text(
                            98,
                            "验证代理",
                            f"端口 {selected_port} 本机可达，但外网不可达（通常是云安全组未放行）",
                        )
                    )

            tg_link = self._build_socks5_link(credentials.host, selected_port, username, password)
            deploy_config = {
                "protocol": "socks5",
                "socks5_username": username,
                "socks5_password": password,
                "service_name": service_name.removesuffix(".service"),
            }
            return DeploymentResult(
                ip=credentials.host,
                port=selected_port,
                tls_domain="",
                secret=password,
                tg_link=tg_link,
                web_link="",
                os_name=checks["os_name"],
                cpu_cores=checks["cpu"],
                memory_mb=checks["memory"],
                kernel=checks["kernel"],
                location=checks["location"],
                tag_supported=False,
                deploy_config=deploy_config,
                protocol="socks5",
                port_warning=port_warning,
            )

    async def inspect_server(
        self,
        credentials: SSHCredentials,
        proxy_port: int | None = None,
        on_progress: ProgressCallback | None = None,
        user_specified: bool = False,
        protocol: str = "mtproxy",
    ) -> dict[str, Any]:
        selected_port = proxy_port or self.settings.proxy_port
        async with AsyncSSHClient(
            credentials,
            command_timeout=self.settings.command_timeout_seconds,
        ) as ssh:
            if on_progress:
                await on_progress("已连接服务器，正在检查基础工具...")
            # 快速检查阶段也要 bootstrap：裸机没 curl/python3 后续命令直接 127。
            # bootstrap 的进度用 `_progress_text(percent|stage|detail)` 格式，
            # inspect 的 on_progress 只需纯文本，用适配器转换一层。
            await self._bootstrap_environment(
                ssh, on_progress=self._adapt_progress_to_raw(on_progress)
            )
            checks = await self._collect_environment(
                ssh,
                selected_port,
                strict_port=False,
                on_progress=on_progress,
                protocol=protocol,
            )
            checks["protocol"] = protocol
            if on_progress:
                await on_progress("已读取，正在检测端口与出网...")
            recommended_port, reason = await self._select_best_port(
                ssh,
                selected_port,
                user_specified=user_specified,
                protocol=protocol,
            )
            checks["recommended_port"] = recommended_port
            checks["recommended_port_reason"] = reason
        return checks

    async def test_latency(self, credentials: SSHCredentials) -> LatencyResult:
        cmds = " & ".join(
            f"curl -so /dev/null -w '{label}:%{{time_total}}\\n' --max-time 10 {url}"
            for label, _emoji_key, url in LATENCY_TARGETS
        ) + " & wait"
        async with AsyncSSHClient(
            credentials,
            command_timeout=self.settings.command_timeout_seconds,
        ) as ssh:
            result = await ssh.run(cmds, check=False)

        out = result.stdout or ""
        ms_map: dict[str, float | None] = {}
        for line in out.splitlines():
            if ":" in line:
                label, _, raw = line.partition(":")
                ms_map[label.strip()] = self._seconds_to_ms(raw.strip())

        return {
            label: (ms_map.get(label), emoji_key)
            for label, emoji_key, _ in LATENCY_TARGETS
        }

    async def remove_proxy_container(
        self,
        credentials: SSHCredentials,
        *,
        proxy_port: int | None = None,
        protocol: str = "mtproxy",
    ) -> None:
        async with AsyncSSHClient(
            credentials,
            command_timeout=self.settings.command_timeout_seconds,
        ) as ssh:
            if protocol == "socks5":
                service_name = self._socks5_service_name(proxy_port) if proxy_port else "microsocks-tg.service"
                await ssh.run(f"systemctl disable --now {service_name} >/dev/null 2>&1 || true", check=False)
                await ssh.run(f"rm -f /etc/systemd/system/{service_name}", check=False)
                if proxy_port:
                    legacy_active = await ssh.run(
                        f"systemctl is-active microsocks-tg.service >/dev/null 2>&1 && "
                        f"ss -tlnp 2>/dev/null | grep ':{proxy_port}' | grep -q microsocks",
                        check=False,
                    )
                    if legacy_active.exit_status == 0:
                        await ssh.run("systemctl disable --now microsocks-tg.service >/dev/null 2>&1 || true", check=False)
                        await ssh.run("rm -f /etc/systemd/system/microsocks-tg.service", check=False)
                await ssh.run("systemctl daemon-reload >/dev/null 2>&1 || true", check=False)
                return
            if proxy_port:
                container_name = self._mtproxy_container_name(proxy_port)
                removed = await ssh.run(f"docker rm -f {container_name} >/dev/null 2>&1", check=False)
                if removed.exit_status == 0:
                    return
                legacy_active = await ssh.run(
                    f"docker inspect mtproxy >/dev/null 2>&1 && "
                    f"ss -tlnp 2>/dev/null | grep ':{proxy_port}' | grep -Eq 'beam.smp|python3|mtproxy|mtproto'",
                    check=False,
                )
                if legacy_active.exit_status == 0:
                    await ssh.run("docker rm -f mtproxy >/dev/null 2>&1 || true", check=False)
                return
            await ssh.run("docker rm -f mtproxy >/dev/null 2>&1 || true", check=False)

    async def check_proxy_status(self, credentials: SSHCredentials) -> str:
        async with AsyncSSHClient(
            credentials,
            command_timeout=self.settings.command_timeout_seconds,
        ) as ssh:
            result = await ssh.run(
                "docker inspect --format='{{.State.Running}}' mtproxy 2>/dev/null || echo MISSING",
                check=False,
            )

        status_raw = (result.stdout or "").strip().lower()
        if status_raw == "true":
            return "运行中"
        if status_raw == "false":
            return "已停止"
        if status_raw == "missing":
            return "未发现容器"
        return "状态未知"

    async def check_proxy_detail(self, credentials: SSHCredentials, proxy_port: int) -> dict[str, Any]:
        container_name = self._mtproxy_container_name(proxy_port)
        async with AsyncSSHClient(
            credentials,
            command_timeout=self.settings.command_timeout_seconds,
        ) as ssh:
            commands = [
                (
                    f"docker inspect --format '{{{{.State.Running}}}}' {container_name} 2>/dev/null || "
                    "docker inspect --format '{{.State.Running}}' mtproxy 2>/dev/null || echo missing"
                ),
                (
                    f"docker inspect --format '{{{{.State.Status}}}}' {container_name} 2>/dev/null || "
                    "docker inspect --format '{{.State.Status}}' mtproxy 2>/dev/null || echo missing"
                ),
                (
                    f"docker inspect --format '{{{{.State.StartedAt}}}}' {container_name} 2>/dev/null || "
                    "docker inspect --format '{{.State.StartedAt}}' mtproxy 2>/dev/null || echo none"
                ),
                f"ss -tln 2>/dev/null | awk -v p={proxy_port} 'BEGIN{{c=0}} {{n=split($4,a,\":\"); if(a[n]==p) c=1}} END{{print c}}'",
            ]
            results = await ssh.run_many(commands, check=False)

        running_raw = (results[0].stdout or "").strip().lower()
        status_raw = (results[1].stdout or "").strip().lower()
        started_raw = (results[2].stdout or "").strip()
        port_raw = (results[3].stdout or "").strip()

        if status_raw == "running":
            container_status = "运行中"
        elif status_raw in {"exited", "dead"}:
            container_status = "已停止"
        elif status_raw in {"missing", "none", ""}:
            container_status = "未发现容器"
        else:
            container_status = "状态未知"

        uptime = self._format_uptime(started_raw)
        port_listening = port_raw == "1"
        if not port_listening and running_raw == "true":
            # 端口没抓到但容器在跑，也当作监听——避免 awk/权限导致的误报
            port_listening = True
        public_reachable = False
        if port_listening:
            probe = await self._probe_proxy_port(ssh, proxy_port, host_hint=credentials.host)
            public_reachable = probe.public_ok

        return {
            "container_status": container_status,
            "container_running": running_raw == "true",
            "uptime": uptime,
            "port_listening": port_listening,
            "public_reachable": public_reachable,
        }

    async def sample_proxy_traffic(
        self,
        credentials: SSHCredentials,
        proxy_port: int,
        *,
        protocol: str = "mtproxy",
    ) -> TrafficSample:
        async with AsyncSSHClient(
            credentials,
            command_timeout=self.settings.command_timeout_seconds,
        ) as ssh:
            if protocol == "socks5":
                result = await ssh.run(
                    f"ss -tn state established '( sport = :{proxy_port} )' 2>/dev/null | wc -l",
                    check=False,
                )
                return TrafficSample(
                    active_connections=max(self._safe_int((result.stdout or "").strip()), 0),
                    bytes_in=0,
                    bytes_out=0,
                )
            container_name = self._mtproxy_container_name(proxy_port)
            commands = [
                f"ss -tn state established '( sport = :{proxy_port} )' 2>/dev/null | wc -l",
                (
                    f"docker stats {container_name} --no-stream --format '{{{{.NetIO}}}}' 2>/dev/null || "
                    "docker stats mtproxy --no-stream --format '{{.NetIO}}' 2>/dev/null || echo '0B / 0B'"
                ),
            ]
            results = await ssh.run_many(commands, check=False)

        active_connections = self._safe_int((results[0].stdout or "").strip())
        netio = (results[1].stdout or "0B / 0B").strip()
        inbound_raw, _, outbound_raw = netio.partition("/")
        return TrafficSample(
            active_connections=max(active_connections, 0),
            bytes_in=self._parse_size(inbound_raw.strip()),
            bytes_out=self._parse_size(outbound_raw.strip()),
        )

    async def inspect_runtime(
        self,
        credentials: SSHCredentials,
        proxy_port: int,
        *,
        include_public_probe: bool = False,
        include_traffic: bool = True,
        protocol: str = "mtproxy",
    ) -> ProxyRuntimeStatus:
        """单 SSH 会话聚合探测：容器状态 + 端口监听 + TG 延迟 + 流量（可选公网探测）。

        相比独立调用 check_proxy_detail / probe_telegram_egress / sample_proxy_traffic，
        节省 2-3 次 SSH 握手（约 2-6 秒），并发场景下显著降低服务器压力。
        """
        if protocol == "socks5":
            return await self._inspect_socks5_runtime(
                credentials,
                proxy_port,
                include_public_probe=include_public_probe,
                include_traffic=include_traffic,
            )

        container_name = self._mtproxy_container_name(proxy_port)
        commands = [
            (
                f"docker inspect --format '{{{{.State.Running}}}}' {container_name} 2>/dev/null || "
                "docker inspect --format '{{.State.Running}}' mtproxy 2>/dev/null || echo missing"
            ),
            (
                f"docker inspect --format '{{{{.State.Status}}}}' {container_name} 2>/dev/null || "
                "docker inspect --format '{{.State.Status}}' mtproxy 2>/dev/null || echo missing"
            ),
            (
                f"docker inspect --format '{{{{.State.StartedAt}}}}' {container_name} 2>/dev/null || "
                "docker inspect --format '{{.State.StartedAt}}' mtproxy 2>/dev/null || echo none"
            ),
            f"ss -tln 2>/dev/null | awk -v p={proxy_port} 'BEGIN{{c=0}} {{n=split($4,a,\":\"); if(a[n]==p) c=1}} END{{print c}}'",
            "curl -so /dev/null -w '%{time_total}' --max-time 6 https://core.telegram.org 2>/dev/null || echo FAIL",
        ]
        if include_traffic:
            commands.extend(
                [
                    f"ss -tn state established '( sport = :{proxy_port} )' 2>/dev/null | wc -l",
                    (
                        f"docker stats {container_name} --no-stream --format '{{{{.NetIO}}}}' 2>/dev/null || "
                        "docker stats mtproxy --no-stream --format '{{.NetIO}}' 2>/dev/null || echo '0B / 0B'"
                    ),
                ]
            )

        async with AsyncSSHClient(
            credentials,
            command_timeout=self.settings.command_timeout_seconds,
        ) as ssh:
            results = await ssh.run_many(commands, check=False, max_parallel=3)

            running_raw = (results[0].stdout or "").strip().lower()
            status_raw = (results[1].stdout or "").strip().lower()
            started_raw = (results[2].stdout or "").strip()
            port_raw = (results[3].stdout or "").strip()
            tg_raw = (results[4].stdout or "").strip()

            container_running = running_raw == "true"
            if status_raw == "running":
                container_status = "运行中"
            elif status_raw in {"exited", "dead"}:
                container_status = "已停止"
            elif status_raw in {"missing", "none", ""}:
                container_status = "未发现容器"
            else:
                container_status = "状态未知"

            uptime = self._format_uptime(started_raw)
            port_listening = port_raw == "1"
            if not port_listening and container_running:
                # 权限或 awk 差异导致的误判兜底
                port_listening = True

            tg_latency_ms = self._seconds_to_ms(tg_raw)
            tg_reachable = tg_latency_ms is not None
            tg_latency_int = int(tg_latency_ms) if tg_latency_ms is not None else None

            traffic: TrafficSample | None = None
            if include_traffic:
                conn_raw = (results[5].stdout or "").strip()
                netio = (results[6].stdout or "0B / 0B").strip()
                inbound_raw, _, outbound_raw = netio.partition("/")
                traffic = TrafficSample(
                    active_connections=max(self._safe_int(conn_raw), 0),
                    bytes_in=self._parse_size(inbound_raw.strip()),
                    bytes_out=self._parse_size(outbound_raw.strip()),
                )

            public_reachable = False
            if port_listening and include_public_probe:
                probe = await self._probe_proxy_port(ssh, proxy_port, host_hint=credentials.host)
                public_reachable = probe.public_ok

        return ProxyRuntimeStatus(
            container_status=container_status,
            container_running=container_running,
            uptime=uptime,
            port_listening=port_listening,
            public_reachable=public_reachable,
            tg_reachable=tg_reachable,
            tg_latency_ms=tg_latency_int,
            traffic=traffic,
        )

    async def _inspect_socks5_runtime(
        self,
        credentials: SSHCredentials,
        proxy_port: int,
        *,
        include_public_probe: bool,
        include_traffic: bool,
    ) -> ProxyRuntimeStatus:
        service_name = self._socks5_service_name(proxy_port)
        commands = [
            (
                f"systemctl is-active {service_name} 2>/dev/null || "
                "systemctl is-active microsocks-tg.service 2>/dev/null || echo inactive"
            ),
            (
                f"systemctl show {service_name} -p ActiveEnterTimestamp --value 2>/dev/null || "
                "systemctl show microsocks-tg.service -p ActiveEnterTimestamp --value 2>/dev/null || echo none"
            ),
            f"ss -tln 2>/dev/null | awk -v p={proxy_port} 'BEGIN{{c=0}} {{n=split($4,a,\":\"); if(a[n]==p) c=1}} END{{print c}}'",
            "curl -so /dev/null -w '%{time_total}' --max-time 6 https://core.telegram.org 2>/dev/null || echo FAIL",
        ]
        if include_traffic:
            commands.append(f"ss -tn state established '( sport = :{proxy_port} )' 2>/dev/null | wc -l")

        async with AsyncSSHClient(
            credentials,
            command_timeout=self.settings.command_timeout_seconds,
        ) as ssh:
            results = await ssh.run_many(commands, check=False, max_parallel=3)
            active_raw = (results[0].stdout or "").strip().lower()
            started_raw = (results[1].stdout or "").strip()
            port_raw = (results[2].stdout or "").strip()
            tg_raw = (results[3].stdout or "").strip()

            running = active_raw == "active"
            port_listening = port_raw == "1"
            public_reachable = False
            if include_public_probe and port_listening:
                probe = await self._probe_proxy_port(ssh, proxy_port, host_hint=credentials.host)
                public_reachable = probe.public_ok

            traffic = None
            if include_traffic:
                active_connections = self._safe_int((results[4].stdout or "").strip()) if len(results) > 4 else 0
                traffic = TrafficSample(
                    active_connections=max(active_connections, 0),
                    bytes_in=0,
                    bytes_out=0,
                )

        tg_latency_ms = self._seconds_to_ms(tg_raw)
        return ProxyRuntimeStatus(
            container_status="运行中" if running else "已停止",
            container_running=running,
            uptime=started_raw if started_raw and started_raw != "none" else None,
            port_listening=port_listening,
            public_reachable=public_reachable,
            tg_reachable=tg_latency_ms is not None,
            tg_latency_ms=int(tg_latency_ms) if tg_latency_ms is not None else None,
            traffic=traffic,
        )

    async def probe_telegram_egress(self, credentials: SSHCredentials) -> tuple[bool, int | None]:
        async with AsyncSSHClient(
            credentials,
            command_timeout=self.settings.command_timeout_seconds,
        ) as ssh:
            result = await ssh.run(
                "curl -so /dev/null -w '%{time_total}' --max-time 6 https://core.telegram.org 2>/dev/null || echo FAIL",
                check=False,
            )
        latency = self._seconds_to_ms((result.stdout or "").strip())
        if latency is None:
            return False, None
        return True, int(latency)

    async def enable_bbr(self, credentials: SSHCredentials) -> str:
        async with AsyncSSHClient(
            credentials,
            command_timeout=self.settings.command_timeout_seconds,
        ) as ssh:
            kernel = await ssh.run("uname -r", check=False)
            kernel_raw = (kernel.stdout or "").strip()
            if not self._kernel_supports_bbr(kernel_raw):
                return "unsupported"

            current = await ssh.run(
                "sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || echo unknown",
                check=False,
            )
            current_algo = (current.stdout or "").strip().lower()
            if current_algo == "bbr":
                return "already_enabled"

            try:
                await self._apply_sysctl(ssh, kernel_raw)
            except Exception as exc:  # noqa: BLE001
                return f"failed:{exc}"

            verify = await ssh.run(
                "sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || echo unknown",
                check=False,
            )
            if (verify.stdout or "").strip().lower() == "bbr":
                return "success"
            return "failed:未检测到 bbr 生效"

    async def _collect_environment(
        self,
        ssh: AsyncSSHClient,
        proxy_port: int,
        *,
        strict_port: bool = True,
        on_progress: ProgressCallback | None = None,
        protocol: str = "mtproxy",
    ) -> dict[str, Any]:
        if on_progress:
            await on_progress("已登录，正在读取系统信息...")
        base_commands = {
            "os_release": "cat /etc/os-release",
            "cpu": "nproc",
            "memory": "free -m | awk '/Mem:/{print $2}'",
            "disk": "df -Pm / | awk 'NR==2{print $4}'",
            "kernel": "uname -r",
            "docker": "command -v docker >/dev/null 2>&1 && docker --version || echo MISSING",
            "tcp_congestion": "sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || echo unknown",
            "firewall": (
                f"bash -c '{SAFE_SHELL_PREFIX}"
                "if command -v ufw >/dev/null 2>&1; then echo ufw; "
                "elif command -v firewall-cmd >/dev/null 2>&1; then echo firewalld; "
                "elif command -v iptables >/dev/null 2>&1; then echo iptables; "
                "else echo none; fi'"
            ),
        }
        mapping = await self._run_command_map(ssh, base_commands)

        if on_progress:
            await on_progress("系统信息已读取，正在检查网络连通...")
        network_commands = {
            "location": (
                "curl -s --max-time 4 ipinfo.io/json 2>/dev/null "
                "|| curl -s --max-time 4 https://ipapi.co/json/ 2>/dev/null "
                "|| curl -s --max-time 4 http://ip-api.com/json 2>/dev/null "
                "|| echo '{}'"
            ),
            "tg_reach": "curl -so /dev/null -w '%{time_total}' --max-time 6 https://core.telegram.org || echo FAIL",
        }
        mapping.update(await self._run_command_map(ssh, network_commands))

        if on_progress:
            await on_progress("正在检测端口占用...")
        container_name = self._mtproxy_container_name(proxy_port)
        port_commands = {
            "port": (
                f"bash -c '{SAFE_SHELL_PREFIX}"
                f"if ss -tln 2>/dev/null | grep -q \":{proxy_port}\"; then echo IN_USE; else echo FREE; fi'"
            ),
            "port_holder": (
                f"bash -c '{SAFE_SHELL_PREFIX}"
                f"ss -tlnp 2>/dev/null | grep \":{proxy_port}\" || echo \"\"'"
            ),
            "mtproxy_container": (
                f"docker inspect --format='{{{{.State.Status}}}}' {container_name} 2>/dev/null || "
                "docker inspect --format='{{.State.Status}}' mtproxy 2>/dev/null || echo MISSING"
            ),
        }
        mapping.update(await self._run_command_map(ssh, port_commands))

        port_state = (mapping["port"].stdout or "").strip().upper()
        port_in_use = port_state == "IN_USE"
        port_holder_raw = (mapping["port_holder"].stdout or "").strip()
        container_status = (mapping["mtproxy_container"].stdout or "").strip().lower()
        holder_is_managed = self._is_managed_proxy_holder(port_holder_raw.lower(), container_status, protocol)

        # 目标端口已被占用时，检查是否是当前协议的托管进程——若是则允许覆盖。
        if strict_port and port_in_use and not holder_is_managed:
            raise DeploymentError(f"目标服务器 {proxy_port} 端口已被其他进程占用，请先释放后重试。")

        os_name = self._extract_os_name(mapping["os_release"].stdout)
        location_raw = (mapping["location"].stdout or "{}").strip()
        country_code = self._extract_country(location_raw)
        location = self._extract_location(location_raw)
        kernel = (mapping["kernel"].stdout or "未知").strip()
        bbr_supported = self._kernel_supports_bbr(kernel)
        bbr_enabled = (mapping["tcp_congestion"].stdout or "").strip().lower() == "bbr"
        tg_latency_ms = self._seconds_to_ms((mapping["tg_reach"].stdout or "").strip())
        tg_latency_int = int(tg_latency_ms) if tg_latency_ms is not None else None

        return {
            "os_name": os_name,
            "cpu": (mapping["cpu"].stdout or "未知").strip(),
            "memory": (mapping["memory"].stdout or "未知").strip(),
            "disk_free_mb": self._safe_int((mapping["disk"].stdout or "").strip()),
            "kernel": kernel,
            "docker": (mapping["docker"].stdout or "MISSING").strip(),
            "firewall": (mapping["firewall"].stdout or "none").strip(),
            "location": location,
            "location_raw": location_raw,
            "country_code": country_code,
            "bbr_supported": bbr_supported,
            "bbr_enabled": bbr_enabled,
            "tg_reachable": tg_latency_int is not None,
            "tg_latency_ms": tg_latency_int,
            "port_in_use": port_in_use,
            "port_holder_summary": self._extract_port_holder_summary(port_holder_raw),
            "port_holder_is_mtproxy": self._is_mtproxy_holder(port_holder_raw.lower(), container_status),
            "port_holder_is_managed": holder_is_managed,
            "mtproxy_present": container_status not in {"missing", "none", ""},
        }

    async def _select_best_port(
        self,
        ssh: AsyncSSHClient,
        user_port: int,
        *,
        user_specified: bool = False,
        protocol: str = "mtproxy",
    ) -> tuple[int, str]:
        ss_result = await ssh.run("ss -tlnp 2>/dev/null || true", check=False)
        container_result = await ssh.run(
            "docker ps -a --format '{{.Names}} {{.State}}' "
            "| awk '$1 ~ /^mtproxy(-[0-9]+)?$/ {print $2; found=1} END {if (!found) print \"MISSING\"}'",
            check=False,
        )
        port_lines = (ss_result.stdout or "").splitlines()
        container_states = (container_result.stdout or "missing").strip().lower().splitlines()
        container_status = "running" if "running" in container_states else (container_states[0] if container_states else "missing")
        used_ports: dict[int, str] = {}
        for line in port_lines:
            matched = re.search(r":(\d+)\s", line)
            if not matched:
                continue
            port = int(matched.group(1))
            used_ports[port] = line.lower()

        def is_available(port: int) -> bool:
            holder = used_ports.get(port)
            if holder is None:
                return True
            return self._is_managed_proxy_holder(holder, container_status, protocol)

        if is_available(user_port):
            if user_specified:
                return user_port, f"你指定的端口 {user_port} 可用"
            return user_port, f"默认端口 {user_port} 可用"

        for port in PREFERRED_PROXY_PORTS:
            if port == user_port:
                continue
            if is_available(port):
                return port, f"端口 {user_port} 已被占用，自动切换到 {port}"

        for _ in range(300):
            candidate = random.randint(10000, 60000)
            if is_available(candidate):
                return candidate, f"预设端口占满，随机端口 {candidate}"

        raise DeploymentError("未找到可用端口，请检查服务器监听端口后重试。")

    async def _validate_tls_domain(self, ssh: AsyncSSHClient) -> str:
        # 候选域名来自配置，避免硬编码后在特定线路上失效。
        candidates = self.settings.tls_domain_candidates or (self.settings.tls_domain,)
        candidates_literal = ",".join(f"'{domain}'" for domain in candidates)
        cmd = (
            "python3 - <<'PY'\n"
            "import asyncio\n"
            "import os\n"
            "import struct\n"
            "\n"
            "CANDIDATES = ["
            f"{candidates_literal}"
            "]\n"
            "\n"
            "def gen_tls_client_hello(server_name: str) -> bytes:\n"
            "    sn = server_name.encode()\n"
            "    random_bytes = os.urandom(32)\n"
            "    session_id = os.urandom(32)\n"
            "    sni_data = struct.pack('!HBH', len(sn) + 3, 0, len(sn)) + sn\n"
            "    ext_sni = struct.pack('!HH', 0x0000, len(sni_data)) + sni_data\n"
            "    ext_sv = struct.pack('!HH', 0x002b, 3) + b'\\x02\\x03\\x04'\n"
            "    ext_sg = struct.pack('!HH', 0x000a, 4) + struct.pack('!HH', 2, 0x001d)\n"
            "    ext_sa = struct.pack('!HH', 0x000d, 4) + struct.pack('!HH', 2, 0x0403)\n"
            "    ks_data = struct.pack('!HH', 0x001d, 32) + os.urandom(32)\n"
            "    ext_ks = struct.pack('!HH', 0x0033, len(ks_data) + 2) + struct.pack('!H', len(ks_data)) + ks_data\n"
            "    extensions = ext_sni + ext_sv + ext_sg + ext_sa + ext_ks\n"
            "    cipher_suites = struct.pack('!H', 6) + struct.pack('!HHH', 0x1302, 0x1303, 0x1301)\n"
            "    body = b'\\x03\\x03' + random_bytes\n"
            "    body += struct.pack('B', 32) + session_id\n"
            "    body += cipher_suites\n"
            "    body += b'\\x01\\x00'\n"
            "    body += struct.pack('!H', len(extensions)) + extensions\n"
            "    handshake = b'\\x01' + struct.pack('!I', len(body))[1:] + body\n"
            "    return b'\\x16\\x03\\x01' + struct.pack('!H', len(handshake)) + handshake\n"
            "\n"
            "async def get_tls_record(reader):\n"
            "    try:\n"
            "        record_type = (await reader.readexactly(1))[0]\n"
            "        tls_version = await reader.readexactly(2)\n"
            "        if tls_version != b'\\x03\\x03':\n"
            "            return 0, b''\n"
            "        record_len = int.from_bytes(await reader.readexactly(2), 'big')\n"
            "        record = await reader.readexactly(record_len)\n"
            "        return record_type, record\n"
            "    except Exception:\n"
            "        return 0, b''\n"
            "\n"
            "async def validate(domain: str) -> bool:\n"
            "    try:\n"
            "        reader, writer = await asyncio.wait_for(asyncio.open_connection(domain, 443), timeout=5)\n"
            "        writer.write(gen_tls_client_hello(domain))\n"
            "        await writer.drain()\n"
            "        r1, _ = await get_tls_record(reader)\n"
            "        if r1 != 22:\n"
            "            writer.close()\n"
            "            return False\n"
            "        r2, _ = await get_tls_record(reader)\n"
            "        if r2 != 20:\n"
            "            writer.close()\n"
            "            return False\n"
            "        r3, rec3 = await get_tls_record(reader)\n"
            "        writer.close()\n"
            "        return r3 == 23 and len(rec3) >= 1024\n"
            "    except Exception:\n"
            "        return False\n"
            "\n"
            "async def main():\n"
            "    for domain in CANDIDATES:\n"
            "        if await validate(domain):\n"
            "            print(domain)\n"
            "            return\n"
            "    print('')\n"
            "\n"
            "asyncio.run(main())\n"
            "PY"
        )
        # TLS 域名验证：内部 Python 协程每个候选 5s 超时，总耗时可控，
        # 外层不再额外卡 timeout。
        result = await ssh.run(cmd, check=False)
        selected = (result.stdout or "").strip()
        if selected:
            return selected
        return self.settings.tls_domain

    async def _apply_sysctl(self, ssh: AsyncSSHClient, kernel: str) -> None:
        if not self._kernel_supports_bbr(kernel):
            # 低内核时跳过 BBR，但保留其他参数。
            config = SYSCTL_CONFIG.replace(
                "net.core.default_qdisc = fq\nnet.ipv4.tcp_congestion_control = bbr\n",
                "",
            )
        else:
            config = SYSCTL_CONFIG

        encoded = b64encode(config.encode("utf-8")).decode("ascii")
        write_cmd = (
            "python3 - <<'PY'\n"
            "import base64\n"
            "from pathlib import Path\n"
            f"Path('/etc/sysctl.d/99-mtproxy.conf').write_text(base64.b64decode('{encoded}').decode('utf-8'))\n"
            "PY"
        )
        await ssh.run(write_cmd, check=True)
        await ssh.run(f"{SAFE_SHELL_PREFIX}sysctl --system", check=True)

    # 真正需要在 bootstrap 阶段预置的工具，只有两个：
    # - curl：Docker 官方安装脚本需要（pipe 到 sh）、地理/公网/TG 可达性探测
    # - python3：TLS 验证、sysctl 写入、TCP 探测、MTProxy config 生成
    #
    # 旧版把 ss/awk/free/sysctl/fuser 也一并强制安装会放大失败面（7 个包一把梭，
    # 任一依赖冲突/404 都会整体失败）。这些工具：
    # - ss / free / sysctl / awk：即使缺失，后续命令都有 `2>/dev/null || true`
    #   兜底，只影响个别字段展示，不阻断部署。
    # - fuser：原本用于等 apt 锁，已被 `DPkg::Lock::Timeout=120` 替代。
    _REQUIRED_TOOLS: tuple[str, ...] = (
        "curl",
        "python3",
    )

    # 按"工具 → 包管理器 → 包名" 二维映射。只装当前真正缺失的工具对应的包。
    _TOOL_PACKAGES: dict[str, dict[str, str]] = {
        "curl": {
            "apt": "curl ca-certificates",
            "rpm": "curl ca-certificates",
            "apk": "curl ca-certificates",
        },
        "python3": {
            "apt": "python3",
            "rpm": "python3",
            "apk": "python3",
        },
    }

    async def _bootstrap_environment(
        self,
        ssh: AsyncSSHClient,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        """校验 root 身份 + 按需补齐最小工具集（curl / python3）。

        设计目标：
        - 绝大多数现代 Linux 默认自带 python3、curl，此函数直接快速通过。
        - 只有真的缺工具时才触发安装，且只装"缺什么补什么"对应的包。
        - 整个安装过程通过 `run_streaming` 流式回调 + 5s 心跳任务持续刷新面板，
          避免"apt 卡了 10 分钟，用户看不到任何进度"。
        """
        # 1. 必须 root。非 root 连 apt-get 都跑不起来，提前报错更友好。
        uid_res = await ssh.run("id -u", check=False)
        if (uid_res.stdout or "").strip() != "0":
            raise DeploymentError("当前 SSH 用户不是 root。请使用 root 用户重试。")

        # 2. 探测缺哪些工具（最常见结果是空，直接短路）
        probe_cmd = self._build_probe_cmd(self._REQUIRED_TOOLS)
        probe = await ssh.run(probe_cmd, check=False)
        missing_raw = (probe.stdout or "").strip()
        if not missing_raw:
            return
        missing_tools = missing_raw.split()

        # 3. 识别包管理器（单次 RTT 批量判断）
        pm = await self._detect_package_manager(ssh)
        if pm == "none":
            raise DeploymentError(
                f"未识别服务器的包管理器，无法自动安装基础工具（缺失：{missing_raw}）。\n"
                "支持的发行版：Debian/Ubuntu (apt) / CentOS/RHEL/Rocky/Alma (yum/dnf) / Alpine (apk)。\n"
                "请手动安装缺失的工具后重试。"
            )

        # 4. 根据缺失的工具，只计算需要的包名（去重）
        pkgs = self._resolve_packages(missing_tools, pm)
        manual_cmd = self._manual_install_cmd(pm, pkgs)

        if on_progress:
            await on_progress(self._progress_text(
                5, "准备运行环境",
                f"缺失 {missing_raw}，正在安装 {pkgs}...",
            ))

        # 5. 流式执行安装，同时用心跳持续刷新面板
        installer = self._build_installer(pm, pkgs)
        install_lines: list[str] = []
        last_line: list[str] = [""]

        def _on_line(line: str) -> None:
            text = line.strip()
            if not text:
                return
            install_lines.append(text)
            last_line[0] = text

        async def _heartbeat() -> None:
            elapsed = 0
            while True:
                await asyncio.sleep(5)
                elapsed += 5
                if on_progress:
                    tail = last_line[0][:80]
                    detail = f"安装中，已用 {elapsed}s"
                    if tail:
                        detail = f"{detail} · {tail}"
                    await on_progress(self._progress_text(6, "准备运行环境", detail))

        heartbeat_task = asyncio.create_task(_heartbeat())
        failure_kind: str | None = None
        failure_ssh_msg = ""
        try:
            result = await ssh.run_streaming(installer, on_line=_on_line)
            if result.exit_status != 0:
                failure_kind = "exit"
        except SSHError as exc:
            failure_kind = "ssh"
            failure_ssh_msg = str(exc)
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

        if failure_kind:
            output_text = "\n".join(install_lines).strip()
            if failure_ssh_msg:
                output_text = (output_text + "\n" + failure_ssh_msg).strip()
            hint = self._diagnose_install_failure(output_text, failure_kind)
            tail = self._tail_output(output_text, max_chars=1500, max_lines=40)
            raise DeploymentError(
                f"基础依赖自动安装失败（缺失：{missing_raw}）。{hint}\n\n"
                "请在服务器上手动执行以下命令后重试：\n"
                f"  {manual_cmd}",
                details=tail or None,
            )

        # 6. 二次验证：包管理器报告成功 ≠ 工具真的落到 PATH。
        verify = await ssh.run(probe_cmd, check=False)
        still_missing = (verify.stdout or "").strip()
        if still_missing:
            output_text = "\n".join(install_lines).strip()
            tail = self._tail_output(output_text, max_chars=1500, max_lines=40)
            raise DeploymentError(
                f"基础依赖安装完成，但以下工具仍不可用：{still_missing}。\n"
                "可能原因：包管理器报告成功但工具未落到 PATH，或当前发行版对应的包名不同。\n"
                "请检查 PATH 或手动排查后重试。",
                details=tail or None,
            )

        if on_progress:
            await on_progress(self._progress_text(7, "准备运行环境", "基础依赖已就绪"))

    @staticmethod
    def _build_probe_cmd(tools: tuple[str, ...]) -> str:
        """生成"批量探测工具缺失"的单行 shell。stdout 只输出缺失列表。"""
        tool_list = " ".join(tools)
        return (
            f'missing=""; for t in {tool_list}; do '
            'command -v "$t" >/dev/null 2>&1 || missing="$missing $t"; '
            'done; echo "${missing# }"'
        )

    @staticmethod
    async def _detect_package_manager(ssh: AsyncSSHClient) -> str:
        """识别包管理器：返回 apt / dnf / yum / apk / none 之一。"""
        probe = await ssh.run(
            "if command -v apt-get >/dev/null 2>&1; then echo apt; "
            "elif command -v dnf >/dev/null 2>&1; then echo dnf; "
            "elif command -v yum >/dev/null 2>&1; then echo yum; "
            "elif command -v apk >/dev/null 2>&1; then echo apk; "
            "else echo none; fi",
            check=False,
        )
        return (probe.stdout or "none").strip() or "none"

    def _resolve_packages(self, missing_tools: list[str], pm: str) -> str:
        """根据缺失工具和包管理器，求所需包名的并集。"""
        family = "apt" if pm == "apt" else ("apk" if pm == "apk" else "rpm")
        pkgs_set: set[str] = set()
        for tool in missing_tools:
            mapping = self._TOOL_PACKAGES.get(tool, {})
            pkgs_str = mapping.get(family, tool)
            pkgs_set.update(pkgs_str.split())
        # Alpine minimal 镜像常缺 bash，我们后续的 `bash -c` 包装依赖它。
        if pm == "apk":
            pkgs_set.add("bash")
        return " ".join(sorted(pkgs_set))

    @staticmethod
    def _build_installer(pm: str, pkgs: str) -> str:
        """按包管理器产出单行 installer。apt 自带锁超时+重试避免 dpkg 抢锁问题。"""
        if pm == "apt":
            apt_opts = "-o DPkg::Lock::Timeout=120 -o Acquire::Retries=3"
            return (
                f"bash -c '{SAFE_SHELL_PREFIX}"
                "export DEBIAN_FRONTEND=noninteractive; "
                f"for i in 1 2 3; do apt-get {apt_opts} update -y && break; sleep 3; done; "
                f"apt-get {apt_opts} install -y {pkgs}' 2>&1"
            )
        if pm == "dnf":
            return f"bash -c '{SAFE_SHELL_PREFIX}dnf install -y {pkgs}' 2>&1"
        if pm == "yum":
            return f"bash -c '{SAFE_SHELL_PREFIX}yum install -y {pkgs}' 2>&1"
        if pm == "apk":
            # Alpine 默认无 bash，首轮用 sh 启动 apk；bash 装好后后续命令可用 bash -c。
            return f"sh -c '{SAFE_SHELL_PREFIX}apk add --no-cache {pkgs}' 2>&1"
        raise DeploymentError(f"内部错误：未知包管理器 {pm}")

    @staticmethod
    def _manual_install_cmd(pm: str, pkgs: str) -> str:
        if pm == "apt":
            return f"apt-get install -y {pkgs}"
        if pm == "dnf":
            return f"dnf install -y {pkgs}"
        if pm == "yum":
            return f"yum install -y {pkgs}"
        if pm == "apk":
            return f"apk add --no-cache {pkgs}"
        return f"# 未知包管理器，请手动安装：{pkgs}"

    async def _ensure_docker(self, ssh: AsyncSSHClient, docker_status: str, *, network_mode: str) -> None:
        if docker_status != "MISSING":
            return

        docker_install_cmd = (
            "curl -fsSL https://get.docker.com | sh -s -- --mirror Aliyun"
            if network_mode == "cn_mirror"
            else "curl -fsSL https://get.docker.com | sh"
        )
        # Ubuntu/Debian 常驻的 unattended-upgrades / apt-daily.service 会占用 dpkg 锁；
        # 直接跑 apt-get 会报 "Could not get lock"。这里先等待最多 120s，
        # 再设 noninteractive 避免弹交互式菜单。
        apt_wait = (
            "for i in $(seq 1 60); do "
            "if fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock >/dev/null 2>&1; then "
            "sleep 2; else break; fi; "
            "done"
        )
        installer = (
            f"bash -c '{SAFE_SHELL_PREFIX}set -e; "
            "export DEBIAN_FRONTEND=noninteractive; "
            "if command -v apt-get >/dev/null 2>&1; then "
            f"{apt_wait}; "
            f"apt-get update -y && apt-get install -y curl ca-certificates && ({docker_install_cmd} || curl -fsSL https://get.docker.com | sh); "
            "elif command -v dnf >/dev/null 2>&1; then "
            f"dnf install -y curl ca-certificates && ({docker_install_cmd} || curl -fsSL https://get.docker.com | sh); "
            "elif command -v yum >/dev/null 2>&1; then "
            f"yum install -y curl ca-certificates && ({docker_install_cmd} || curl -fsSL https://get.docker.com | sh); "
            "else echo UNSUPPORTED; exit 1; fi'"
        )
        # Docker 官方安装脚本内部已有重试；低配机器可能跑几分钟到十几分钟，
        # 由 SSH keepalive 负责检测死链，这里不设显式 timeout。
        await ssh.run(installer, check=True)
        await ssh.run("docker --version", check=True)

    async def _ensure_microsocks(self, ssh: AsyncSSHClient) -> None:
        existing = await ssh.run("command -v microsocks >/dev/null 2>&1", check=False)
        if existing.exit_status == 0:
            return

        pm = await self._detect_package_manager(ssh)
        if pm == "none":
            raise DeploymentError("服务器未检测到支持的包管理器，无法自动安装 microsocks。")

        installer = self._build_installer(pm, "microsocks")
        result = await ssh.run(installer, check=False)
        if result.exit_status != 0:
            output = self._tail_output((result.stdout or "") + "\n" + (result.stderr or ""), max_chars=1500, max_lines=40)
            raise DeploymentError(
                "microsocks 自动安装失败，请确认系统软件源可用后重试。",
                details=output or None,
            )

        verify = await ssh.run("command -v microsocks >/dev/null 2>&1", check=False)
        if verify.exit_status != 0:
            raise DeploymentError("microsocks 安装完成后仍不可用，请检查服务器 PATH 或软件源包名。")

    async def _run_socks5_service(
        self,
        ssh: AsyncSSHClient,
        proxy_port: int,
        username: str,
        password: str,
    ) -> None:
        service_name = self._socks5_service_name(proxy_port)
        systemd = await ssh.run("command -v systemctl >/dev/null 2>&1", check=False)
        if systemd.exit_status != 0:
            raise DeploymentError("SOCKS5 自动托管需要 systemd，当前服务器未检测到 systemctl。")
        path_result = await ssh.run("command -v microsocks", check=True)
        microsocks_path = (path_result.stdout or "/usr/bin/microsocks").strip() or "/usr/bin/microsocks"
        await self._stop_socks5_on_port(ssh, proxy_port, service_name)

        unit = (
            "[Unit]\n"
            "Description=MicroSocks SOCKS5 proxy for Telegram\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={microsocks_path} -i 0.0.0.0 -p {proxy_port} -u {username} -P {password}\n"
            "Restart=on-failure\n"
            "RestartSec=2\n"
            "NoNewPrivileges=true\n\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )
        encoded = b64encode(unit.encode("utf-8")).decode("ascii")
        write_cmd = (
            "python3 - <<'PY'\n"
            "import base64\n"
            "from pathlib import Path\n"
            f"Path('/etc/systemd/system/{service_name}').write_text(base64.b64decode('{encoded}').decode('utf-8'))\n"
            "PY"
        )
        await ssh.run(write_cmd, check=True)
        await ssh.run("systemctl daemon-reload", check=True)
        await ssh.run(f"systemctl enable {service_name} >/dev/null 2>&1", check=False)
        await ssh.run(f"systemctl restart {service_name}", check=True)
        status = await ssh.run(f"systemctl is-active {service_name}", check=False)
        if "active" not in (status.stdout or "").strip().lower():
            logs = await ssh.run(f"journalctl -u {service_name} -n 40 --no-pager 2>/dev/null || true", check=False)
            raise DeploymentError("SOCKS5 服务启动失败。", details=(logs.stdout or logs.stderr or "").strip() or None)

    async def _stop_socks5_on_port(
        self,
        ssh: AsyncSSHClient,
        proxy_port: int,
        service_name: str,
    ) -> None:
        await ssh.run(f"systemctl disable --now {service_name} >/dev/null 2>&1 || true", check=False)
        holder = await ssh.run(
            f"ss -tlnp 2>/dev/null | grep ':{proxy_port}' | grep microsocks || true",
            check=False,
        )
        holder_text = (holder.stdout or "").strip()
        if not holder_text:
            return

        services = await ssh.run(
            "systemctl list-units --type=service --all --no-legend "
            "'microsocks-tg*.service' 2>/dev/null | awk '{print $1}'",
            check=False,
        )
        service_names = {
            item.strip()
            for item in (services.stdout or "").splitlines()
            if item.strip().startswith("microsocks-tg")
        }
        service_names.add("microsocks-tg.service")

        for name in sorted(service_names):
            await ssh.run(f"systemctl disable --now {name} >/dev/null 2>&1 || true", check=False)

        still_bound = await ssh.run(
            f"ss -tlnp 2>/dev/null | grep ':{proxy_port}' | grep microsocks || true",
            check=False,
        )
        if (still_bound.stdout or "").strip():
            raise DeploymentError(
                f"端口 {proxy_port} 已被 microsocks 占用，但未能自动停止旧服务，请先释放该端口后重试。"
            )
        await ssh.run("systemctl daemon-reload >/dev/null 2>&1 || true", check=False)

    async def _open_firewall(self, ssh: AsyncSSHClient, firewall_type: str, proxy_port: int) -> None:
        if firewall_type == "ufw":
            await ssh.run(f"ufw allow {proxy_port}/tcp", check=False)
            return

        if firewall_type == "firewalld":
            await ssh.run(f"firewall-cmd --permanent --add-port={proxy_port}/tcp", check=False)
            await ssh.run("firewall-cmd --reload", check=False)
            return

        if firewall_type == "iptables":
            await ssh.run(
                "iptables -C INPUT -p tcp "
                f"--dport {proxy_port} -j ACCEPT || "
                f"iptables -I INPUT -p tcp --dport {proxy_port} -j ACCEPT",
                check=False,
            )

    async def _probe_proxy_port(self, ssh: AsyncSSHClient, proxy_port: int, *, host_hint: str) -> ProbeResult:
        """探测端口本机与公网可达性。"""
        local_ok = await self._probe_tcp_port(ssh, "127.0.0.1", proxy_port)
        if not local_ok:
            return ProbeResult(local_ok=False, public_ok=False, public_ip=host_hint)

        public_ip = await self._detect_public_ip(ssh, fallback_host=host_hint)
        public_ok = await self._probe_tcp_port(ssh, public_ip, proxy_port)
        return ProbeResult(local_ok=True, public_ok=public_ok, public_ip=public_ip)

    async def _try_fix_port_access(self, ssh: AsyncSSHClient, proxy_port: int, firewall_type: str) -> None:
        """自动修复可在服务器内完成的端口放行问题。"""
        await self._open_firewall(ssh, firewall_type, proxy_port)
        await ssh.run(
            "iptables -C DOCKER-USER -p tcp "
            f"--dport {proxy_port} -j RETURN || "
            f"iptables -I DOCKER-USER -p tcp --dport {proxy_port} -j RETURN",
            check=False,
        )

    async def _detect_public_ip(self, ssh: AsyncSSHClient, *, fallback_host: str) -> str:
        cmd = (
            f"bash -c \"{SAFE_SHELL_PREFIX}for u in ifconfig.me ip.sb api.ipify.org; do "
            "ip=$(curl -s --max-time 3 $u 2>/dev/null | tr -d '\\n\\r '); "
            "if echo $ip | grep -Eq '^[0-9]{1,3}(\\.[0-9]{1,3}){3}$'; then echo $ip; exit 0; fi; "
            "done; echo ''\""
        )
        # curl --max-time 3 × 3 个 URL，命令自身有时间边界，无需外层 timeout。
        result = await ssh.run(cmd, check=False)
        public_ip = (result.stdout or "").strip()
        if public_ip:
            return public_ip
        return fallback_host

    async def _probe_tcp_port(self, ssh: AsyncSSHClient, host: str, port: int) -> bool:
        cmd = (
            "python3 - <<'PY'\n"
            "import socket\n"
            f"host = {host!r}\n"
            f"port = {port}\n"
            "try:\n"
            "    s = socket.create_connection((host, port), timeout=5)\n"
            "    s.close()\n"
            "    print('OPEN')\n"
            "except Exception:\n"
            "    print('CLOSED')\n"
            "PY"
        )
        # Python socket 内部 5s 超时已足够，外层不再叠加。
        result = await ssh.run(cmd, check=False)
        return "OPEN" in (result.stdout or "")

    @staticmethod
    def _build_port_warning(proxy_port: int) -> str:
        return (
            "端口外网不可达。\n"
            f"代理已部署成功，但端口 {proxy_port} 从外网无法连通，通常是云安全组未放行。\n"
            "请在云服务商控制台（阿里云/腾讯云/AWS 等）添加入站规则："
            f"TCP {proxy_port}，来源 0.0.0.0/0。"
        )

    async def _ensure_master_image(
        self,
        ssh: AsyncSSHClient,
        on_progress: ProgressCallback,
        *,
        network_mode: str,
    ) -> None:
        """获取 mtprotoproxy:master 镜像。
        主路径：从 GHCR 私有镜像仓库 pull（秒级）。
        兜底：pull 失败时从 GitHub master 源码本地构建（慢，约 5-15 分钟）。
        """
        check = await ssh.run(
            "docker images -q mtprotoproxy:master 2>/dev/null",
            check=False,
        )
        if (check.stdout or "").strip():
            return

        import asyncio as _asyncio

        # ── 主路径：从 GHCR 拉预构建镜像，通常 10-40 秒 ──
        ghcr_image = self.settings.mtp_image
        await on_progress(self._progress_text(76, "拉取镜像", f"从私有镜像仓库下载，通常 10-40 秒..."))

        pull_done: list[bool] = [False]

        async def _pull_heartbeat() -> None:
            secs = 0
            while not pull_done[0]:
                await _asyncio.sleep(5)
                secs += 5
                await on_progress(self._progress_text(
                    min(76 + secs // 3, 88),
                    "拉取镜像",
                    f"下载中，已等待 {secs} 秒...",
                ))

        heartbeat = _asyncio.create_task(_pull_heartbeat())
        try:
            # docker pull 内部自带分块重试，低带宽机器可能下载几十分钟；
            # 不设显式 timeout，交给 SSH keepalive 守护。
            pull = await ssh.run(
                f"docker pull {ghcr_image} 2>&1 && "
                f"docker tag {ghcr_image} mtprotoproxy:master",
                check=False,
            )
        finally:
            pull_done[0] = True
            heartbeat.cancel()
            with contextlib.suppress(_asyncio.CancelledError):
                await heartbeat

        if pull.exit_status == 0:
            await on_progress(self._progress_text(89, "拉取镜像", "镜像下载完成 ✓"))
            return

        # ── 兜底：GHCR 不可达时从 GitHub 源码构建 ──
        await on_progress(self._progress_text(77, "构建镜像", "镜像仓库不可达，改用源码构建（约 5-15 分钟）..."))
        clone_urls = [
            "https://ghfast.top/https://github.com/alexbers/mtprotoproxy.git",
            "https://mirror.ghproxy.com/https://github.com/alexbers/mtprotoproxy.git",
            "https://github.com/alexbers/mtprotoproxy.git",
        ]
        clone_ok = False
        for clone_url in clone_urls:
            r = await ssh.run(
                f"rm -rf /tmp/mtprotoproxy && git clone --depth 1 {clone_url} /tmp/mtprotoproxy",
                check=False,
            )
            if r.exit_status == 0:
                clone_ok = True
                break
        if not clone_ok:
            raise DeploymentError(
                "镜像获取失败：GHCR 不可达，且 GitHub 源码也无法克隆，请检查服务器出网策略。"
            )

        if network_mode == "cn_mirror":
            await self._patch_dockerfile_mirror(ssh)

        await on_progress(self._progress_text(78, "构建镜像", "编译镜像，首次较慢..."))

        last_line: list[str] = [""]

        def _on_build_line(line: str) -> None:
            text = line.strip()
            if not text:
                return
            lower = text.lower()
            if matched := re.search(r"#\d+\s+\[(\d+/\d+)\]", text):
                last_line[0] = f"构建步骤 {matched.group(1)}..."
                return
            if matched := re.search(r"step\s+(\d+/\d+)", lower):
                last_line[0] = f"构建步骤 {matched.group(1)}..."
                return
            if any(t in lower for t in ("apt-get", "get:", "fetched", "fetch", "unpack", "setting up")):
                last_line[0] = "安装依赖..."
                return
            if any(t in lower for t in ("pulling", "downloading", "extracting", "resolving")):
                last_line[0] = "拉取基础镜像..."
                return
            if any(t in lower for t in ("successfully built", "naming to", "exporting", "writing image")):
                last_line[0] = "镜像构建收尾..."
                return

        async def _flush_progress() -> None:
            loop = _asyncio.get_running_loop()
            started = loop.time()
            pct = 79
            while True:
                await _asyncio.sleep(3)
                pct = min(pct + 2, 89)
                elapsed = int(loop.time() - started)
                mins, secs = divmod(elapsed, 60)
                detail = last_line[0] or "编译中..."
                if pct >= 89:
                    detail = f"{detail}（已用时 {mins}分{secs:02d}秒）"
                await on_progress(self._progress_text(pct, "构建镜像", detail))

        # docker build 在低配机器 + 慢源的组合下可能需要 30+ 分钟；
        # 不设显式 timeout，由 SSH keepalive 负责"会话真死"的兜底。
        build_task = _asyncio.create_task(
            ssh.run_streaming(
                "cd /tmp/mtprotoproxy && docker build -t mtprotoproxy:master . 2>&1",
                on_line=_on_build_line,
            )
        )
        flush_task = _asyncio.create_task(_flush_progress())
        try:
            result = await build_task
        finally:
            flush_task.cancel()
            with contextlib.suppress(_asyncio.CancelledError):
                await flush_task

        if result.exit_status != 0:
            raise DeploymentError(f"镜像构建失败:\n{result.stdout[-800:]}")

        await ssh.run("rm -rf /tmp/mtprotoproxy", check=False)

    async def _detect_network_env(self, ssh: AsyncSSHClient) -> str:
        """检测出网质量，返回 direct 或 cn_mirror。"""
        # curl 内层 --max-time 5 已限定命令自身耗时，不叠加外层 timeout。
        probe = await ssh.run(
            "curl -so /dev/null -w '%{time_total}' --max-time 5 https://registry-1.docker.io 2>/dev/null || echo FAIL",
            check=False,
        )

        def _is_slow(raw: str) -> bool:
            text = (raw or "").strip()
            if text == "FAIL":
                return True
            try:
                return float(text) > 3.0
            except ValueError:
                return True

        return "cn_mirror" if _is_slow(probe.stdout) else "direct"

    async def _patch_dockerfile_mirror(self, ssh: AsyncSSHClient) -> None:
        """国内线路下注入 apt 换源命令，阿里源失败自动切清华源。"""
        patch_cmd = (
            "python3 - <<'PY'\n"
            "from pathlib import Path\n"
            "dockerfile = Path('/tmp/mtprotoproxy/Dockerfile')\n"
            "if not dockerfile.exists():\n"
            "    raise SystemExit(0)\n"
            "text = dockerfile.read_text(encoding='utf-8')\n"
            "mirror_sed = (\n"
            "    'sed -i \'s|archive.ubuntu.com/ubuntu|mirrors.aliyun.com/ubuntu|g;\'\n"
            "    's|security.ubuntu.com/ubuntu|mirrors.aliyun.com/ubuntu|g;\'\n"
            "    's|deb.debian.org/debian|mirrors.aliyun.com/debian|g;\'\n"
            "    's|security.debian.org|mirrors.aliyun.com/debian-security|g\'\n"
            "    ' /etc/apt/sources.list 2>/dev/null || true'\n"
            ")\n"
            "replacement = (\n"
            "    f'({mirror_sed}) && (apt-get update || (\'\n"
            "    'sed -i \'s|mirrors.aliyun.com|mirrors.tuna.tsinghua.edu.cn|g\'\n"
            "    ' /etc/apt/sources.list 2>/dev/null || true) && apt-get update)'\n"
            ")\n"
            "text = text.replace('apt-get update', replacement)\n"
            "dockerfile.write_text(text, encoding='utf-8')\n"
            "PY"
        )
        await ssh.run(patch_cmd, check=False)

    async def _run_mtproxy(
        self,
        ssh: AsyncSSHClient,
        secret: str,
        proxy_port: int,
        tls_domain: str,
        checks: dict[str, Any],
        config: DeployConfig,
    ) -> None:
        container_name = self._mtproxy_container_name(proxy_port)
        config_path = self._mtproxy_config_path(proxy_port)
        await ssh.run(f"docker rm -f {container_name} >/dev/null 2>&1 || true", check=False)
        if checks.get("port_holder_is_managed"):
            await ssh.run("docker rm -f mtproxy >/dev/null 2>&1 || true", check=False)
        # 兼容早期固定容器名的其他实现，仅在同一端口被它占用时清理。
        await ssh.run(
            f"ss -tlnp 2>/dev/null | grep ':{proxy_port}' | grep -Eq 'mtproto|mtproxy' && "
            "docker ps -a --format '{{.Names}}' | grep -E '^(mtproto.proxy)$' "
            "| xargs -r docker rm -f >/dev/null 2>&1 || true",
            check=False,
        )
        await ssh.run(f"rm -f {config_path} >/dev/null 2>&1 || true", check=False)

        config_lines = [
            f"PORT = {proxy_port}",
            "",
            "USERS = {",
            f'    "tg": "{secret}",',
            "}",
            "",
            "MODES = {",
            '    "classic": False,',
            '    "secure": False,',
            '    "tls": True,',
            "}",
            "",
            f'TLS_DOMAIN = "{tls_domain}"',
            # alexbers/mtprotoproxy 是单进程 asyncio + uvloop，源码不读 WORKERS，
            # 这里不再写入，避免误导。多核扩展方案见 _recommend_workers 注释。
        ]
        ad_tag = (config.ad_tag if config.ad_tag is not None else self.settings.mtp_tag).strip()
        if ad_tag:
            config_lines.append(f"AD_TAG = {ad_tag!r}")
            config_lines.append("USE_MIDDLE_PROXY = True")
        else:
            config_lines.append("USE_MIDDLE_PROXY = False")

        config_content = "\n".join(config_lines) + "\n"
        encoded = b64encode(config_content.encode("utf-8")).decode("ascii")
        write_cmd = (
            "python3 - <<'PY'\n"
            "import base64\n"
            "from pathlib import Path\n"
            f"Path('{config_path}').write_text(base64.b64decode('{encoded}').decode('utf-8'))\n"
            "PY"
        )
        await ssh.run(write_cmd, check=True)

        run_cmd = (
            "docker run -d "
            f"--name {container_name} "
            "--restart unless-stopped "
            "--network host "
            "--ulimit nofile=1048576:1048576 "
            "--ulimit nproc=65535:65535 "
            "--ulimit memlock=-1:-1 "
            f"-v {config_path}:/home/tgproxy/config.py:ro "
            "mtprotoproxy:master"
        )
        if config.extra_docker_args:
            run_cmd = f"{run_cmd} {' '.join(config.extra_docker_args)}"
        # docker run -d 极少卡住；保持默认超时足矣。
        await ssh.run(run_cmd, check=True)

        check_cmd = f"docker ps --filter name={container_name} --format '{{{{.Names}}}}' | grep -q '^{container_name}$'"
        try:
            await ssh.run(check_cmd, check=True)
        except SSHError as exc:
            logs = await ssh.run(f"docker logs --tail 80 {container_name}", check=False)
            raise DeploymentError(f"容器启动失败:\n{logs.stdout or logs.stderr}") from exc

        import asyncio as _asyncio
        port_ok = False
        # 最多等待 20 秒（10 次 × 2 秒），每次同时检查端口和容器状态
        for _ in range(10):
            await _asyncio.sleep(2)
            combined = await ssh.run(
                f"PORT=$(ss -tln 2>/dev/null | grep -c ':{proxy_port}'); "
                f"STATE=$(docker inspect --format='{{{{.State.Status}}}}' {container_name} 2>/dev/null || echo missing); "
                f"echo $PORT $STATE",
                check=False,
            )
            parts = (combined.stdout or "").strip().split()
            port_count = int(parts[0]) if parts and parts[0].isdigit() else 0
            status = parts[1].lower() if len(parts) > 1 else "missing"

            if port_count > 0:
                port_ok = True
                break

            if status in ("restarting", "exited", "dead", "missing"):
                logs = await ssh.run(f"docker logs --tail 50 {container_name}", check=False)
                raise DeploymentError(
                    f"容器启动后崩溃（状态：{status}）:\n{logs.stdout or logs.stderr}"
                )

        if not port_ok:
            logs = await ssh.run(f"docker logs --tail 50 {container_name}", check=False)
            raise DeploymentError(
                f"容器已启动但端口 {proxy_port} 20 秒内未监听:\n"
                f"{logs.stdout or logs.stderr}"
            )

    @staticmethod
    def _extract_os_name(os_release_text: str) -> str:
        """从 /etc/os-release 提取简短系统名，如 'Debian 12'、'Ubuntu 22.04'。"""
        pretty = ""
        name = ""
        version_id = ""
        for line in os_release_text.splitlines():
            line = line.strip()
            if line.startswith("PRETTY_NAME="):
                pretty = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("NAME="):
                name = line.split("=", 1)[1].strip().strip('"')
            elif line.startswith("VERSION_ID="):
                version_id = line.split("=", 1)[1].strip().strip('"')

        if pretty:
            m = re.match(
                r"^(Debian|Ubuntu|CentOS|Rocky|AlmaLinux|Fedora|Arch|openSUSE|Amazon Linux|Alpine)"
                r"[^\d]*(\d+(?:\.\d+)?)",
                pretty,
                re.IGNORECASE,
            )
            if m:
                return f"{m.group(1)} {m.group(2)}"
            simplified = re.sub(r"\s*\(.*\)", "", pretty).strip()
            simplified = re.sub(r"\s+LTS$", "", simplified).strip()
            return simplified or pretty

        if name and version_id:
            return f"{name} {version_id}"
        return name or "未知 Linux"

    @staticmethod
    def _extract_location(ipinfo_text: str) -> str:
        try:
            data = json.loads(ipinfo_text)
        except json.JSONDecodeError:
            return "未知"
        if not isinstance(data, dict):
            return "未知"

        country_code = (
            (data.get("country") or "").strip().upper()
            or (data.get("country_code") or "").strip().upper()
            or (data.get("countryCode") or "").strip().upper()
        )
        if country_code:
            return COUNTRY_ZH_MAP.get(country_code, country_code)

        country_name = (
            (data.get("country_name") or "").strip()
            or (data.get("country") or "").strip()
            or (data.get("region") or "").strip()
        )
        return country_name or "未知"

    @staticmethod
    def _extract_country(ipinfo_text: str) -> str:
        """从多来源地理 JSON 中提取两位国家代码，解析失败返回空字符串。"""
        try:
            data = json.loads(ipinfo_text)
        except (json.JSONDecodeError, AttributeError):
            return ""
        if not isinstance(data, dict):
            return ""
        country = (
            (data.get("country") or "").strip().upper()
            or (data.get("country_code") or "").strip().upper()
            or (data.get("countryCode") or "").strip().upper()
        )
        return country[:2] if len(country) >= 2 else ""

    @staticmethod
    def _recommend_workers(cpu_count: int) -> int:
        """按 CPU 核心数推荐 worker 数（预留给未来多进程/多容器模式使用）。

        当前 alexbers/mtprotoproxy Python 版是单进程 asyncio，此函数暂未启用。
        若后续切换到 C 版 mtproto-proxy 或 "一机多容器" 模式，可直接使用。

        策略：低核心全用；中核心 3-8 保留 1 核；高核心 >=9 保留 2 核；上限 32。
        """
        n = max(int(cpu_count), 1)
        if n <= 2:
            workers = n
        elif n <= 8:
            workers = n - 1
        else:
            workers = n - 2
        return min(max(workers, 1), 32)

    @staticmethod
    def _kernel_supports_bbr(kernel: str) -> bool:
        parts = re.split(r"[.-]", kernel)
        major = int(parts[0]) if parts and parts[0].isdigit() else 0
        minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return (major, minor) >= (4, 9)

    @staticmethod
    def _seconds_to_ms(raw: str) -> float | None:
        try:
            seconds = float((raw or "").strip())
        except ValueError:
            return None
        return round(seconds * 1000, 2)

    @staticmethod
    def _generate_socks5_password() -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(18))

    @staticmethod
    def _build_socks5_link(host: str, port: int, username: str, password: str) -> str:
        return (
            f"tg://socks?server={quote(host, safe='')}"
            f"&port={port}"
            f"&user={quote(username, safe='')}"
            f"&pass={quote(password, safe='')}"
        )

    @staticmethod
    def _safe_int(raw: str) -> int:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _parse_size(raw: str) -> int:
        text = (raw or "").strip().upper().replace(" ", "")
        if not text:
            return 0
        matched = re.match(r"^([0-9]*\.?[0-9]+)([KMGTP]?B)$", text)
        if not matched:
            return 0
        value = float(matched.group(1))
        unit = matched.group(2)
        unit_map = {
            "B": 1,
            "KB": 1024,
            "MB": 1024**2,
            "GB": 1024**3,
            "TB": 1024**4,
            "PB": 1024**5,
        }
        return int(value * unit_map.get(unit, 1))

    @staticmethod
    async def _run_command_map(
        ssh: AsyncSSHClient,
        commands: dict[str, str],
    ) -> dict[str, Any]:
        results = await ssh.run_many(list(commands.values()), check=False)
        return dict(zip(commands.keys(), results, strict=True))

    @staticmethod
    def _extract_port_holder_summary(holder_out: str) -> str:
        if not holder_out:
            return "未占用"
        parts = holder_out.split('"', 1)
        if len(parts) == 2 and parts[1]:
            process = parts[1].split('"', 1)[0]
            if process:
                return process
        return holder_out.splitlines()[0][:120]

    @staticmethod
    def _is_mtproxy_holder(holder_out: str, container_status: str) -> bool:
        return (
            "beam.smp" in holder_out
            or "python3" in holder_out
            or "mtproto" in holder_out
            or "mtproxy" in holder_out
            or container_status in {"running", "restarting", "exited", "created"}
        )

    @classmethod
    def _is_managed_proxy_holder(cls, holder_out: str, container_status: str, protocol: str) -> bool:
        if protocol == "socks5":
            return "microsocks" in holder_out or "microsocks-tg" in holder_out
        return cls._is_mtproxy_holder(holder_out, container_status)

    @staticmethod
    def _adapt_progress_to_raw(
        raw_cb: ProgressCallback | None,
    ) -> ProgressCallback | None:
        """把 `_progress_text(percent|stage|detail)` 格式适配成纯文本。

        给不渲染进度条、只显示一行状态的调用方（如 inspect_server）使用。
        返回 None 表示调用方压根没传回调，直接透传。
        """
        if raw_cb is None:
            return None

        async def _adapted(payload: str) -> None:
            if "|" in payload:
                parts = payload.split("|", 2)
                stage = parts[1].strip() if len(parts) > 1 else ""
                detail = parts[2].strip() if len(parts) > 2 else ""
                text = detail or stage or payload
            else:
                text = payload
            await raw_cb(text)

        return _adapted

    @staticmethod
    def _diagnose_install_failure(output: str, kind: str) -> str:
        """根据包管理器输出给出中文归因，失败时面向用户的一句话提示。"""
        if kind == "ssh":
            return "安装期间 SSH 会话中断，服务器可能已断开或重启。"
        lower = (output or "").lower()
        # 诊断顺序按常见程度排序，命中即返回。
        if "temporary failure in name resolution" in lower or "could not resolve" in lower:
            return "服务器 DNS 解析失败，请检查 /etc/resolv.conf 或 nameserver 配置。"
        if "no space left on device" in lower:
            return "服务器磁盘空间不足，请清理后重试。"
        if "404  not found" in lower or "unable to fetch some archives" in lower or "failed to fetch" in lower:
            return "软件源仓库不可达或已过期，请更换镜像源（常见于停更的旧 Ubuntu/Debian 版本）。"
        if "could not get lock" in lower or "unable to lock" in lower or "resource temporarily unavailable" in lower:
            return "apt/dpkg 锁被占用（通常是 unattended-upgrades 在后台跑），请 1-2 分钟后重试。"
        if "dpkg was interrupted" in lower or "needs to be reinstalled" in lower:
            return "dpkg 状态异常，请先 SSH 执行 `dpkg --configure -a` 后重试。"
        if "connection timed out" in lower or "connection refused" in lower or "network is unreachable" in lower:
            return "服务器到软件源的网络不通，请检查服务器出网策略。"
        if "unsupported_pm" in lower:
            return "未识别的包管理器（不是 apt/yum/dnf/apk），请手动安装。"
        if "hash sum mismatch" in lower or "gpg error" in lower or "not signed" in lower or "not valid yet" in lower:
            return "软件源签名/校验失败，请检查系统时间和仓库 GPG key。"
        return "请参考下方日志定位问题。"

    @staticmethod
    def _tail_output(text: str, *, max_chars: int, max_lines: int) -> str:
        if not text:
            return ""
        lines = text.splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        joined = "\n".join(lines).strip()
        if len(joined) > max_chars:
            joined = joined[-max_chars:]
        return joined

    @staticmethod
    def _format_uptime(started_at_raw: str) -> str | None:
        if not started_at_raw or started_at_raw in {"none", "0001-01-01T00:00:00Z"}:
            return None
        text = started_at_raw.replace("Z", "+00:00")
        try:
            started = datetime.fromisoformat(text)
        except ValueError:
            return None
        delta = datetime.now(timezone.utc) - started
        seconds = max(int(delta.total_seconds()), 0)
        hours, rem = divmod(seconds, 3600)
        mins, _ = divmod(rem, 60)
        if hours > 0:
            return f"{hours}小时{mins}分"
        return f"{mins}分"

    @staticmethod
    def _progress_text(percent: int, stage: str, detail: str = "") -> str:
        """返回 'percent|stage|detail' 供 handler 渲染。"""
        return f"{percent}|{stage}|{detail}"

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field

import asyncssh


class SSHError(RuntimeError):
    """SSH 连接或命令执行失败。"""


@dataclass(slots=True)
class SSHCredentials:
    host: str
    port: int
    username: str
    password: str


@dataclass(slots=True)
class CommandResult:
    command: str
    stdout: str
    stderr: str
    exit_status: int


class AsyncSSHClient:
    """SSH 会话封装。

    设计原则：
    - *连接* 有超时（detect 死服务器 / 错端口）。
    - *命令* 的执行只受"SSH 会话存活"这一个条件约束：通过 keepalive 快速发现
      TCP 断开，否则允许命令跑到业务完成（低配机器做 apt/docker pull/build
      等重活可能几十分钟，设太紧会误杀）。
    - 保留一个 30 分钟的默认天花板，只做"防远端真·进程挂死"兜底，正常任务
      远达不到。
    """

    def __init__(
        self,
        credentials: SSHCredentials,
        command_timeout: int = 1800,
        connect_timeout: int = 20,
        keepalive_interval: int = 15,
        keepalive_count_max: int = 4,
    ) -> None:
        self.credentials = credentials
        self.command_timeout = command_timeout
        self.connect_timeout = connect_timeout
        self.keepalive_interval = keepalive_interval
        self.keepalive_count_max = keepalive_count_max
        self._conn: asyncssh.SSHClientConnection | None = None

    async def __aenter__(self) -> "AsyncSSHClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._conn:
            return
        try:
            self._conn = await asyncio.wait_for(
                asyncssh.connect(
                    self.credentials.host,
                    port=self.credentials.port,
                    username=self.credentials.username,
                    password=self.credentials.password,
                    known_hosts=None,
                    server_host_key_algs=["ssh-ed25519", "rsa-sha2-512", "rsa-sha2-256"],
                    # TCP/SSH 存活探测：每 15s 一次 keepalive，连续 4 次无响应
                    # (即 ~60s) 后 asyncssh 会主动关闭通道，正在跑的命令会拿到
                    # ConnectionLost → SSHError，而不是空等到业务超时。
                    keepalive_interval=self.keepalive_interval,
                    keepalive_count_max=self.keepalive_count_max,
                ),
                timeout=self.connect_timeout,
            )
        except TimeoutError as exc:
            raise SSHError(
                f"SSH 连接超时（>{self.connect_timeout}s）：请检查 IP、端口、安全组"
            ) from exc
        except (asyncssh.Error, OSError) as exc:
            raise SSHError(f"SSH 连接失败: {exc}") from exc

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None

    async def run(
        self,
        command: str,
        *,
        check: bool = True,
        timeout: int | None = None,
    ) -> CommandResult:
        if not self._conn:
            await self.connect()

        assert self._conn is not None
        command_timeout = timeout or self.command_timeout
        try:
            completed = await asyncio.wait_for(
                self._conn.run(command, check=False),
                timeout=command_timeout,
            )
        except TimeoutError as exc:
            # 走到这里意味着 SSH 会话还活着但远端命令跑了 30 分钟没结束 —— 远端
            # 进程很可能真的挂死了。正常任务不应该命中这里。
            raise SSHError(
                f"命令超时({command_timeout}s，SSH 会话仍然存活，可能是远端进程挂死): {command}"
            ) from exc
        except (asyncssh.Error, OSError) as exc:
            raise SSHError(f"SSH 会话中断: {command}\n{exc}") from exc

        result = CommandResult(
            command=command,
            stdout=completed.stdout.strip(),
            stderr=completed.stderr.strip(),
            exit_status=completed.exit_status,
        )
        if check and result.exit_status != 0:
            message = result.stderr or result.stdout or "未知错误"
            raise SSHError(f"命令失败({result.exit_status}): {command}\n{message}")
        return result

    async def run_streaming(
        self,
        command: str,
        *,
        on_line: Callable[[str], None],
        timeout: int | None = None,
    ) -> CommandResult:
        """执行命令并实时回调每行输出（stdout+stderr 合并），最后返回完整结果。"""
        if not self._conn:
            await self.connect()

        assert self._conn is not None
        command_timeout = timeout or self.command_timeout
        stdout_buf: list[str] = []
        stderr_buf: list[str] = []

        async def _read(reader: asyncssh.SSHReader, buf: list[str]) -> None:
            async for line in reader:
                stripped = line.rstrip("\n")
                buf.append(stripped)
                on_line(stripped)

        try:
            proc = await self._conn.create_process(command, stderr=asyncssh.STDOUT)
            await asyncio.wait_for(
                asyncio.gather(
                    _read(proc.stdout, stdout_buf),
                ),
                timeout=command_timeout,
            )
            await proc.wait_closed()
            exit_status = proc.exit_status or 0
        except TimeoutError as exc:
            raise SSHError(
                f"命令超时({command_timeout}s，SSH 会话仍然存活，可能是远端进程挂死): {command}"
            ) from exc
        except (asyncssh.Error, OSError) as exc:
            raise SSHError(f"SSH 会话中断: {command}\n{exc}") from exc

        return CommandResult(
            command=command,
            stdout="\n".join(stdout_buf),
            stderr="\n".join(stderr_buf),
            exit_status=exit_status,
        )

    async def run_many(
        self,
        commands: Iterable[str],
        *,
        check: bool = True,
        timeout: int | None = None,
        max_parallel: int = 4,
    ) -> list[CommandResult]:
        cmd_list = list(commands)
        if not cmd_list:
            return []

        # 某些 VPS 的 SSH MaxSessions 很低，限制并发可避免 ChannelOpenError。
        parallel = max(1, min(max_parallel, len(cmd_list)))
        semaphore = asyncio.Semaphore(parallel)
        results: list[CommandResult | None] = [None] * len(cmd_list)

        async def _run_one(index: int, cmd: str) -> None:
            async with semaphore:
                results[index] = await self.run(cmd, check=check, timeout=timeout)

        await asyncio.gather(*[_run_one(i, cmd) for i, cmd in enumerate(cmd_list)])
        return [r for r in results if r is not None]

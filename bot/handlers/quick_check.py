from __future__ import annotations

from telegram import InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.config import EMOJI, tg_emoji
from bot.handlers.constants import (
    BTN_GREEN,
    BTN_RED,
    CB_BACK_MENU,
    CB_CONTINUE_DEPLOY,
    CB_MODIFY_PORT,
    PROTOCOL_SOCKS5,
    STATE_AWAIT_PROXY_PORT,
    STATE_QUICK_CHECK,
    STATE_TOOLS,
)
from bot.handlers.utils import button, render_checklist
from bot.presentation.proxy_views import build_quick_check_text
from bot.services.database import ProxyRecord


class QuickCheckMixin:
    async def _render_quick_check(
        self,
        user_id: int,
        session: dict[str, object],
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        chat_id = int(session["chat_id"])
        bot_msg_id = int(session["bot_msg_id"])
        checks = dict(session.get("checks") or {})
        protocol = str(session.get("protocol") or "mtproxy")
        proxy_port = int(checks.get("recommended_port") or session.get("proxy_port") or self._default_proxy_port(session))
        existing = await self.db.get_active_proxy_by_endpoint(
            user_id,
            str(session.get("ip", "")),
            proxy_port,
        )
        if existing and existing.protocol != protocol:
            existing = None

        items = self._build_checklist(checks, existing, protocol=protocol)
        checklist_text = render_checklist(items)
        highest_level = self._highest_level(items)
        summary_map = {
            "ok": "检查全部通过，可以继续部署。",
            "warn": "存在可继续的风险项，请确认后继续。",
            "block": "存在阻断项，需先处理后再部署。",
        }
        summary = summary_map[highest_level]

        session["state"] = STATE_QUICK_CHECK
        session["quick_check_level"] = highest_level
        session["proxy_port"] = proxy_port
        if existing:
            session["existing_proxy_id"] = existing.id
        else:
            session.pop("existing_proxy_id", None)
        await self.store.save_session(user_id, session)

        if highest_level == "block":
            keyboard = InlineKeyboardMarkup([[button("返回菜单", CB_BACK_MENU, EMOJI["cross"], BTN_RED)]])
        else:
            continue_label = "仍然继续" if highest_level == "warn" else "继续部署"
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        button(continue_label, CB_CONTINUE_DEPLOY, EMOJI["rocket"], BTN_GREEN),
                        button("修改端口", CB_MODIFY_PORT, EMOJI["gear"], BTN_GREEN),
                    ],
                    [
                        button("返回菜单", CB_BACK_MENU, EMOJI["cross"], BTN_RED),
                    ]
                ]
            )
        await self._edit_panel(
            context,
            chat_id,
            bot_msg_id,
            build_quick_check_text(
                ip=str(session.get("ip", "")),
                ssh_port=int(session.get("port") or 22),
                username=str(session.get("username", "root")),
                checklist_text=checklist_text,
                summary=summary,
                emoji_map=EMOJI,
                tg_emoji=tg_emoji,
            ),
            keyboard,
        )

    async def _continue_from_quick_check(
        self,
        user_id: int,
        session: dict[str, object],
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if session.get("state") != STATE_QUICK_CHECK:
            return
        if session.get("quick_check_level") == "block":
            await self._render_quick_check(user_id, session, context)
            return

        chat_id = int(session["chat_id"])
        bot_msg_id = int(session["bot_msg_id"])
        checks = dict(session.get("checks") or {})
        has_existing = bool(session.get("existing_proxy_id"))

        session["proxy_port"] = int(checks.get("recommended_port") or session.get("proxy_port") or self._default_proxy_port(session))
        session["state"] = STATE_TOOLS
        await self.store.save_session(user_id, session)
        await self._edit_panel(
            context,
            chat_id,
            bot_msg_id,
            self._build_tools_text(session, checks, has_existing=has_existing),
            self._tools_keyboard(),
        )

    async def _prompt_modify_proxy_port(
        self,
        user_id: int,
        session: dict[str, object],
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        chat_id = int(session["chat_id"])
        bot_msg_id = int(session["bot_msg_id"])
        session["state"] = STATE_AWAIT_PROXY_PORT
        await self.store.save_session(user_id, session)
        await self._edit_panel(
            context,
            chat_id,
            bot_msg_id,
            (
                f"{tg_emoji(EMOJI['gear'], '⚙️')} <b>修改代理端口</b>\n\n"
                "请回复新的代理端口（1-65535）。\n"
                "提示：这是代理端口，不是 SSH 登录端口。"
            ),
            InlineKeyboardMarkup([[button("返回", CB_BACK, EMOJI["cross"], BTN_RED)]]),
        )

    def _build_checklist(
        self,
        checks: dict[str, object],
        existing: ProxyRecord | None,
        *,
        protocol: str,
    ) -> list[tuple[str, str, str]]:
        items: list[tuple[str, str, str]] = []
        protocol_label = "SOCKS5" if protocol == PROTOCOL_SOCKS5 else "MTProxy"
        items.append(("ok", "代理协议", protocol_label))
        country_code = str(checks.get("country_code", "")).upper()
        location = str(checks.get("location", "未知"))
        if country_code == "CN" and protocol != PROTOCOL_SOCKS5:
            items.append(("block", "IP 归属地", f"{location}（中国大陆，无法部署 MTProxy）"))
        elif country_code == "CN":
            items.append(("warn", "IP 归属地", f"{location}（服务器到 Telegram 可能不可达）"))
        else:
            items.append(("ok", "IP 归属地", f"{location}（可部署）"))

        port_in_use = bool(checks.get("port_in_use"))
        holder_is_managed = bool(checks.get("port_holder_is_managed"))
        holder_summary = str(checks.get("port_holder_summary", "未知"))
        recommended_port = checks.get("recommended_port", self.settings.proxy_port)
        if port_in_use and not holder_is_managed:
            items.append(("warn", "端口占用", f"当前端口被 {holder_summary} 占用，建议使用 {recommended_port}"))
        elif port_in_use:
            items.append(("warn", "端口占用", "该端口已有正在运行的代理，本次继续执行会进行覆盖"))
        else:
            items.append(("ok", "端口占用", "目标端口空闲"))

        if existing:
            items.append(("warn", "旧代理记录", "当前账号已有该 IP 和端口的代理记录，继续部署将覆盖"))
        else:
            items.append(("ok", "旧代理记录", "该端口未发现旧记录，本次会新增代理"))

        mtproxy_present = bool(checks.get("mtproxy_present"))
        if mtproxy_present and not existing:
            items.append(("warn", "服务器已有 mtproxy", "检测到已有容器，继续部署将接管并覆盖"))
        else:
            items.append(("ok", "服务器已有 mtproxy", "未发现会冲突的历史容器"))

        docker_raw = str(checks.get("docker", "MISSING")).upper()
        if docker_raw == "MISSING":
            items.append(("warn", "Docker 环境", "未安装 Docker，部署阶段会自动安装"))
        else:
            items.append(("ok", "Docker 环境", "Docker 已安装"))

        if bool(checks.get("bbr_supported")):
            items.append(("ok", "内核能力", "支持 BBR 优化"))
        else:
            items.append(("warn", "内核能力", "内核不支持 BBR，将跳过该优化"))

        memory_mb = self._safe_int(checks.get("memory"))
        if memory_mb and memory_mb < 256:
            items.append(("warn", "内存容量", f"{memory_mb} MB，低于建议值 256 MB"))
        else:
            display_mem = f"{memory_mb} MB" if memory_mb else "未知"
            items.append(("ok", "内存容量", f"{display_mem}，满足部署"))

        disk_free_mb = self._safe_int(checks.get("disk_free_mb"))
        if disk_free_mb and disk_free_mb < 500:
            items.append(("block", "磁盘剩余", f"{disk_free_mb} MB，小于 500 MB，可能无法拉取镜像"))
        else:
            display_disk = f"{disk_free_mb} MB" if disk_free_mb else "未知"
            items.append(("ok", "磁盘剩余", f"{display_disk}"))

        tg_reachable = bool(checks.get("tg_reachable"))
        tg_latency = checks.get("tg_latency_ms")
        if tg_reachable:
            latency_text = f"{tg_latency} ms" if tg_latency is not None else "可达"
            items.append(("ok", "Telegram 连通性", f"可达（{latency_text}）"))
        else:
            items.append(("warn", "Telegram 连通性", "不可达，代理可能无法正常使用"))
        return items

    @staticmethod
    def _highest_level(items: list[tuple[str, str, str]]) -> str:
        levels = {item[0] for item in items}
        if "block" in levels:
            return "block"
        if "warn" in levels:
            return "warn"
        return "ok"

    @staticmethod
    def _safe_int(raw: object) -> int:
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            return 0

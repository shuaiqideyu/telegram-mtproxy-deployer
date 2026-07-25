from __future__ import annotations

import html
from urllib.parse import quote

from telegram import InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.config import EMOJI, tg_emoji
from bot.handlers.constants import (
    ADMIN_NOTIFY_NEW,
    ADMIN_NOTIFY_OVERWRITE,
    BTN_BLUE,
    BTN_GREEN,
    BTN_RED,
    CB_BACK,
    CB_BACK_MENU,
    CB_DEPLOY_PROXY,
    CB_MY_PROXIES,
    CB_MODIFY_REMARK_PREFIX,
    CB_NEW_PROXY,
    CB_RETRY_CONNECT,
    CB_SPEED_TEST,
    PROTOCOL_SOCKS5,
    STATE_DEPLOYING,
    STATE_MENU,
    STATE_QUICK_CHECK,
    STATE_TOOLS,
    STATE_AWAIT_PASS,
)
from bot.handlers.utils import APP_EMOJI_FALLBACK, button, render_progress, translate_error, translate_error_html, url_button
from bot.presentation.proxy_views import (
    build_deployment_success_text,
    build_port_unreachable_text,
    build_tools_text,
)
from bot.services.deployer import DeploymentError
from bot.services.ssh import SSHCredentials


class DeployMixin:
    async def _inspect_after_bind(
        self,
        user_id: int,
        session: dict[str, object],
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        chat_id = int(session["chat_id"])
        bot_msg_id = int(session["bot_msg_id"])
        credentials = SSHCredentials(
            host=str(session["ip"]),
            port=int(session["port"]),
            username=str(session["username"]),
            password=str(session["password"]),
        )
        proxy_port = int(session.get("proxy_port") or self._default_proxy_port(session))

        async def on_progress(raw_text: str) -> None:
            await self._edit_panel(
                context,
                chat_id,
                bot_msg_id,
                (
                    f"{tg_emoji(EMOJI['gear'], '⚙️')} <b>连接服务器中</b>\n\n"
                    f"IP：<code>{html.escape(str(session.get('ip', '')))}</code>\n\n"
                    f"{tg_emoji(EMOJI['clock'], '⏰')} {html.escape(raw_text)}"
                ),
                None,
            )

        try:
            checks = await self.deployer.inspect_server(
                credentials,
                proxy_port=proxy_port,
                on_progress=on_progress,
                user_specified=bool(session.get("proxy_port_user_set")),
                protocol=str(session.get("protocol") or "mtproxy"),
            )
        except Exception as exc:  # noqa: BLE001 —— 需要同时捕获 SSHError/DeploymentError/超时等多来源异常
            err_text = str(exc).lower()
            is_auth_error = any(k in err_text for k in ("permission denied", "authentication failed", "auth failed"))
            # DeploymentError 是 bootstrap 阶段抛的，SSH 已成功登录，
            # 再标成 "SSH 连接失败" 会误导用户去排查密码/端口/安全组。
            is_remote_env_error = isinstance(exc, DeploymentError)
            next_state = STATE_AWAIT_PASS if is_auth_error else STATE_MENU
            await self.store.save_session(user_id, {**session, "state": next_state})

            if is_auth_error:
                body = (
                    f"{tg_emoji(EMOJI['cross'], '❌')} <b>认证失败</b>\n\n"
                    f"{tg_emoji(EMOJI['warn'], '❗')} 用户名或密码错误，请重新输入密码，或点击返回修改用户名"
                )
            elif is_remote_env_error:
                details_block = ""
                details = getattr(exc, "details", None)
                if details:
                    details_block = f"\n\n<pre>{html.escape(details)}</pre>"
                body = (
                    f"{tg_emoji(EMOJI['cross'], '❌')} <b>服务器环境准备失败</b>\n\n"
                    f"{tg_emoji(EMOJI['warn'], '❗')} {translate_error_html(exc)}"
                    f"{details_block}"
                )
            else:
                body = (
                    f"{tg_emoji(EMOJI['cross'], '❌')} <b>SSH 连接失败</b>\n\n"
                    f"{tg_emoji(EMOJI['warn'], '❗')} {translate_error_html(exc)}"
                )

            await self._edit_panel(
                context,
                chat_id,
                bot_msg_id,
                body,
                InlineKeyboardMarkup(
                    [
                        [
                            (
                                button("返回", CB_BACK, EMOJI["cross"], BTN_RED)
                                if is_auth_error
                                else button("重试", CB_RETRY_CONNECT, EMOJI["deploy"], BTN_GREEN)
                            ),
                            button("返回菜单", CB_BACK_MENU, EMOJI["cross"], BTN_RED),
                        ]
                    ]
                ),
            )
            return

        session["state"] = STATE_QUICK_CHECK
        session["checks"] = checks
        session["proxy_port"] = int(
            checks.get("recommended_port") or session.get("proxy_port") or self.settings.proxy_port
        )
        await self.store.save_session(user_id, session)
        await self._render_quick_check(user_id, session, context)

    async def _run_speed_test(
        self,
        user_id: int,
        session: dict[str, object],
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del user_id
        chat_id = int(session["chat_id"])
        credentials = SSHCredentials(
            host=str(session["ip"]),
            port=int(session["port"]),
            username=str(session["username"]),
            password=str(session["password"]),
        )
        progress = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{tg_emoji(EMOJI['speed'], '⚡')} <b>延迟测速中</b>\n\n"
                f"{tg_emoji(EMOJI['clock'], '⏰')} 正在并发测试，约 10 秒出结果..."
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        try:
            latency = await self.deployer.test_latency(credentials)
            lines = [f"{tg_emoji(EMOJI['speed'], '⚡')} <b>延迟测速结果</b>\n"]
            for label, (ms, emoji_key) in latency.items():
                fallback = APP_EMOJI_FALLBACK.get(emoji_key, "🌐")
                icon = tg_emoji(EMOJI.get(emoji_key, EMOJI["globe"]), fallback)
                val = f"<code>{int(ms)} ms</code>" if ms is not None else "超时"
                lines.append(f"{icon} {label}  {val}")
            await self._try_delete(context, progress.chat_id, progress.message_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text="\n".join(lines),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001
            await self._try_delete(context, progress.chat_id, progress.message_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"{tg_emoji(EMOJI['warn'], '❗')} 测速失败：{html.escape(translate_error(exc))}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

    async def _start_deployment(
        self,
        user_id: int,
        session: dict[str, object],
        context: ContextTypes.DEFAULT_TYPE,
        telegram_username: str | None = None,
    ) -> None:
        chat_id = int(session["chat_id"])
        bot_msg_id = int(session["bot_msg_id"])
        existing_proxy_id: int | None = session.get("existing_proxy_id")
        session["state"] = STATE_DEPLOYING
        await self.store.save_session(user_id, session)

        async def on_progress(text: str) -> None:
            await self._edit_panel(
                context,
                chat_id,
                bot_msg_id,
                render_progress("部署", text, emoji_map=EMOJI, tg_emoji=tg_emoji),
                None,
            )

        try:
            credentials = SSHCredentials(
                host=str(session["ip"]),
                port=int(session["port"]),
                username=str(session["username"]),
                password=str(session["password"]),
            )
            result = await self.deployer.deploy(
                credentials,
                on_progress=on_progress,
                proxy_port=int(session.get("proxy_port") or self._default_proxy_port(session)),
                protocol=str(session.get("protocol") or "mtproxy"),
            )

            remark = str(session.get("remark") or "").strip()[:30]
            payload = {
                "user_id": user_id,
                "server_ip": result.ip,
                "ssh_port": int(session["port"]),
                "ssh_user": session["username"],
                "ssh_password": session["password"],
                "proxy_port": result.port,
                "tls_domain": result.tls_domain,
                "secret": result.secret,
                "tg_link": result.tg_link,
                "web_link": result.web_link,
                "os_name": result.os_name,
                "cpu_cores": result.cpu_cores,
                "memory_mb": result.memory_mb,
                "kernel": result.kernel,
                "location": result.location,
                "tag_supported": result.tag_supported,
                "deploy_config": result.deploy_config,
                "remark": remark,
            }
            if existing_proxy_id:
                await self.db.overwrite_proxy(
                    proxy_id=existing_proxy_id,
                    user_id=user_id,
                    **{k: v for k, v in payload.items() if k not in ("user_id", "server_ip", "remark")},
                )
                if remark:
                    await self.db.update_proxy_remark(user_id, existing_proxy_id, remark)
                result.id = existing_proxy_id
                is_overwrite = True
            else:
                result.id = await self.db.create_proxy(payload)
                is_overwrite = False

        except (DeploymentError, Exception) as exc:  # noqa: BLE001
            session["state"] = STATE_TOOLS
            await self.store.save_session(user_id, session)
            details = ""
            if isinstance(exc, DeploymentError) and exc.details:
                details = f"\n\n<pre>{html.escape(exc.details)}</pre>"
            await self._edit_panel(
                context,
                chat_id,
                bot_msg_id,
                (
                    f"{tg_emoji(EMOJI['cross'], '❌')} <b>部署失败</b>\n\n"
                    f"{tg_emoji(EMOJI['warn'], '❗')} {translate_error_html(exc)}"
                    f"{details}"
                ),
                InlineKeyboardMarkup(
                    [
                        [
                            button("重试连接", CB_RETRY_CONNECT, EMOJI["deploy"], BTN_GREEN),
                            button("返回菜单", CB_BACK_MENU, EMOJI["cross"], BTN_RED),
                        ]
                    ]
                ),
            )
            return

        await self.store.save_session(
            user_id,
            {"state": STATE_MENU, "chat_id": chat_id, "bot_msg_id": bot_msg_id},
        )
        await self._notify_admin(
            context=context,
            notify_type=ADMIN_NOTIFY_OVERWRITE if is_overwrite else ADMIN_NOTIFY_NEW,
            user_id=user_id,
            telegram_username=telegram_username,
            host=result.ip,
            port=result.port,
            secret=result.secret,
            location=result.location,
            tag_supported=result.tag_supported,
        )
        await self._edit_panel(
            context,
            chat_id,
            bot_msg_id,
            build_deployment_success_text(
                result,
                is_overwrite=is_overwrite,
                emoji_map=EMOJI,
                tg_emoji=tg_emoji,
            ),
            InlineKeyboardMarkup(self._deployment_success_keyboard(result)),
        )
        if result.port_warning:
            await context.bot.send_message(
                chat_id=chat_id,
                text=build_port_unreachable_text(
                    result.port,
                    emoji_map=EMOJI,
                    tg_emoji=tg_emoji,
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

    def _default_proxy_port(self, session: dict[str, object]) -> int:
        return 10808 if str(session.get("protocol") or "mtproxy") == PROTOCOL_SOCKS5 else int(self.settings.proxy_port)

    @staticmethod
    def _deployment_success_keyboard(result) -> list[list]:
        share_url = f"https://t.me/share/url?url={quote(result.tg_link, safe='')}"
        first_row = [url_button("分享链接", share_url, EMOJI["link"], BTN_BLUE)]
        if result.id is not None:
            first_row.append(
                button(
                    "修改备注",
                    f"{CB_MODIFY_REMARK_PREFIX}{result.id}",
                    EMOJI["info"],
                    BTN_GREEN,
                )
            )
        return [
            first_row,
            [
                button("新建代理", CB_NEW_PROXY, EMOJI["deploy"], BTN_GREEN),
                button("我的代理", CB_MY_PROXIES, EMOJI["folder"], BTN_BLUE),
            ],
        ]

    @staticmethod
    def _menu_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    button("新建代理", CB_NEW_PROXY, EMOJI["deploy"], BTN_GREEN),
                    button("我的代理", CB_MY_PROXIES, EMOJI["folder"], BTN_BLUE),
                ]
            ]
        )

    @staticmethod
    def _tools_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    button("网络测速", CB_SPEED_TEST, EMOJI["speed"], BTN_BLUE),
                    button("部署代理", CB_DEPLOY_PROXY, EMOJI["rocket"], BTN_GREEN),
                ],
                [button("返回菜单", CB_BACK_MENU, EMOJI["cross"], BTN_RED)],
            ]
        )

    @staticmethod
    def _build_menu_text() -> str:
        return (
            f"{tg_emoji(EMOJI['robot'], '🤖')} <b>代理自动部署工具</b>\n\n"
            f"{tg_emoji(EMOJI['star'], '⭐')} <b>最低配置</b>\n"
            f"{tg_emoji(EMOJI['server'], '🖥')} 系统：Ubuntu 22.04\n"
            f"{tg_emoji(EMOJI['gear'], '⚙️')} 配置：1 核 512 MB 及以上\n"
            f"{tg_emoji(EMOJI['lock'], '🔒')} 权限：root\n\n"
            f"{tg_emoji(EMOJI['shield'], '🛡')} <b>支持协议</b>\n"
            f"MTProxy / SOCKS5\n\n"
            f"{tg_emoji(EMOJI['rocket'], '🚀')} <b>推荐网络</b>\n"
            f"{tg_emoji(EMOJI['globe'], '🌐')} CN2 GIA / 三网优化 / 专线，独立 IP"
        )

    @staticmethod
    def _build_tools_text(
        session: dict[str, object],
        checks: dict[str, object],
        has_existing: bool = False,
    ) -> str:
        return build_tools_text(
            session,
            checks,
            has_existing=has_existing,
            emoji_map=EMOJI,
            tg_emoji=tg_emoji,
        )

from __future__ import annotations

import math

from telegram import InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot.config import EMOJI, tg_emoji
from bot.handlers.constants import (
    BTN_BLUE,
    BTN_GREEN,
    BTN_RED,
    CB_BACK_MENU,
    CB_MY_PROXIES,
    CB_NOOP,
    CB_PUBLIC_COUNTRY_PAGE_PREFIX,
    CB_PUBLIC_COUNTRIES,
    CB_PUBLIC_LOCATION_PAGE_PREFIX,
    CB_PUBLIC_LOCATION_PREFIX,
    COUNTRY_PAGE_SIZE,
    PUBLIC_PROXIES_PAGE_SIZE,
    STATE_VIEW_PUBLIC,
)
from bot.handlers.utils import button, url_button
from bot.services.public_health_checker import latency_cache_key
from bot.utils.country import country_flag, country_short_name

COUNTRY_COLUMNS_PER_ROW = 3


class PublicProxiesMixin:
    async def _render_public_countries(
        self,
        user_id: int,
        chat_id: int,
        bot_msg_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        page: int = 0,
    ) -> None:
        country_rows = await self.db.count_public_by_location()
        locations = [location for location, _count in country_rows]
        location_counts = {location: count for location, count in country_rows}
        total = len(country_rows)
        total_pages = max(math.ceil(total / COUNTRY_PAGE_SIZE), 1)
        page = max(0, min(page, total_pages - 1))
        await self.store.save_session(
            user_id,
            {
                "state": STATE_VIEW_PUBLIC,
                "chat_id": chat_id,
                "bot_msg_id": bot_msg_id,
                "public_locations": locations,
                "public_location_counts": location_counts,
                "public_country_page": page,
            },
        )
        if not country_rows:
            await self._edit_panel(
                context,
                chat_id,
                bot_msg_id,
                (
                    f"{tg_emoji(EMOJI['globe'], '🌐')} <b>公开代理</b>\n\n"
                    f"{tg_emoji(EMOJI['info'], 'ℹ️')} 当前暂无可用公开代理"
                ),
                InlineKeyboardMarkup([[button("返回菜单", CB_BACK_MENU, EMOJI["cross"], BTN_RED)]]),
            )
            return

        rows: list[list] = []
        current: list = []
        start = page * COUNTRY_PAGE_SIZE
        end = start + COUNTRY_PAGE_SIZE
        for index, (location, _count) in enumerate(country_rows[start:end], start=start):
            flag = country_flag(location)
            label = f"{flag} {country_short_name(location)}"
            current.append(button(label, f"{CB_PUBLIC_LOCATION_PREFIX}{index}", None, BTN_BLUE))
            if len(current) >= COUNTRY_COLUMNS_PER_ROW:
                rows.append(current)
                current = []
        if current:
            rows.append(current)

        if total_pages > 1:
            pager: list = []
            if page > 0:
                pager.append(button("上一页", f"{CB_PUBLIC_COUNTRY_PAGE_PREFIX}{page - 1}", None, BTN_BLUE))
            pager.append(button(f"第 {page + 1}/{total_pages} 页", CB_NOOP, EMOJI["info"], BTN_BLUE))
            if page < total_pages - 1:
                pager.append(button("下一页", f"{CB_PUBLIC_COUNTRY_PAGE_PREFIX}{page + 1}", None, BTN_BLUE))
            rows.append(pager)

        rows.append([button("我的代理", CB_MY_PROXIES, EMOJI["folder"], BTN_GREEN)])
        rows.append([button("返回菜单", CB_BACK_MENU, EMOJI["cross"], BTN_RED)])
        await self._edit_panel(
            context,
            chat_id,
            bot_msg_id,
            f"{tg_emoji(EMOJI['globe'], '🌐')} <b>公开代理</b>",
            InlineKeyboardMarkup(rows),
        )

    async def _render_public_location(
        self,
        user_id: int,
        chat_id: int,
        bot_msg_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        location_index: int,
        page: int = 0,
    ) -> None:
        session = await self.store.load_session(user_id) or {}
        locations = session.get("public_locations") or []
        if not isinstance(locations, list) or not locations:
            await self._render_public_countries(user_id, chat_id, bot_msg_id, context)
            return
        if location_index < 0 or location_index >= len(locations):
            await self._render_public_countries(user_id, chat_id, bot_msg_id, context)
            return

        location = str(locations[location_index])
        count_map = session.get("public_location_counts") or {}
        total = int(count_map.get(location, 0)) if isinstance(count_map, dict) else 0
        total_pages = max(math.ceil(total / PUBLIC_PROXIES_PAGE_SIZE), 1)
        page = max(0, min(page, total_pages - 1))
        session["public_location_index"] = location_index
        session["public_location_page"] = page
        session["state"] = STATE_VIEW_PUBLIC
        session["chat_id"] = chat_id
        session["bot_msg_id"] = bot_msg_id
        await self.store.save_session(user_id, session)
        offset = page * PUBLIC_PROXIES_PAGE_SIZE
        proxies = await self.db.list_public_by_location(location, limit=PUBLIC_PROXIES_PAGE_SIZE, offset=offset)

        latency_values: list[str | None] = []
        if proxies:
            keys = [latency_cache_key(item.id) for item in proxies]
            latency_values = await self.store.kv_mget(keys)

        rows: list[list] = []
        for item, raw in zip(proxies, latency_values):
            base_name = (item.remark or "").strip() or f"代理#{item.id}"
            protocol = "SOCKS5" if item.protocol == "socks5" else "MT"
            suffix = self._format_latency_suffix(raw)
            display_name = f"{protocol} · {base_name}{suffix}"
            rows.append([url_button(display_name, item.tg_link, EMOJI["link"], BTN_BLUE)])

        if total_pages > 1:
            pager: list = []
            if page > 0:
                pager.append(button("上一页", f"{CB_PUBLIC_LOCATION_PAGE_PREFIX}{location_index}:{page - 1}", None, BTN_BLUE))
            pager.append(button(f"第 {page + 1}/{total_pages} 页", CB_NOOP, EMOJI["info"], BTN_BLUE))
            if page < total_pages - 1:
                pager.append(button("下一页", f"{CB_PUBLIC_LOCATION_PAGE_PREFIX}{location_index}:{page + 1}", None, BTN_BLUE))
            rows.append(pager)

        rows.append([button("返回国家列表", CB_PUBLIC_COUNTRIES, EMOJI["globe"], BTN_GREEN)])
        rows.append([button("返回菜单", CB_BACK_MENU, EMOJI["cross"], BTN_RED)])
        await self._edit_panel(
            context,
            chat_id,
            bot_msg_id,
            f"{tg_emoji(EMOJI['globe'], '🌐')} <b>公开代理 - {country_flag(location)} {country_short_name(location)}</b>",
            InlineKeyboardMarkup(rows),
        )

    async def _toggle_proxy_public(
        self,
        user_id: int,
        proxy_id: int,
        chat_id: int,
        bot_msg_id: int,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        record = await self.db.get_proxy(user_id=user_id, proxy_id=proxy_id)
        if not record or record.status != "active":
            await self._render_my_proxies(user_id, chat_id, bot_msg_id, context)
            return
        target = not bool(record.is_public)
        await self.db.set_proxy_public(user_id=user_id, proxy_id=proxy_id, is_public=target)
        if target:
            record.is_public = True
            if self.public_checker is not None:
                self.public_checker.trigger_probe(record)
        else:
            await self.store.kv_delete(latency_cache_key(proxy_id))
        await self._render_proxy_detail(user_id, proxy_id, chat_id, bot_msg_id, context)

    @staticmethod
    def _format_latency_suffix(raw: str | None) -> str:
        if raw is None:
            return ""
        if raw == "timeout":
            return "(超时)"
        try:
            ms = int(raw)
        except (TypeError, ValueError):
            return ""
        return f"({ms}ms)"

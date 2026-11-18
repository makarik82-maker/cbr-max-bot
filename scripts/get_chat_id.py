#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Помощник: показывает chat_id чатов/каналов, куда добавлен бот MAX.

Как использовать:
1. Добавьте бота в группу и назначьте его администратором (право «write»).
2. Запустите этот скрипт (локально или через workflow «Get MAX chat id»).
3. Найдите в выводе событие bot_added / bot_admin_permissions_changed
   с is_channel=False — его chat_id и есть ID вашей группы.

Важно: метод GET /updates не работает, если у бота активна webhook-подписка.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from check_rates import MAX_API_BASE, log, make_session


def main() -> None:
    token = os.environ.get("MAX_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задана переменная окружения MAX_BOT_TOKEN")
    session = make_session()
    resp = session.get(
        f"{MAX_API_BASE}/updates",
        params={"timeout": 0, "limit": 100},
        headers={"Authorization": token},
        timeout=60,
    )
    if resp.status_code == 405:
        raise RuntimeError(
            "GET /updates запрещён: у бота активна webhook-подписка. "
            "Удалите её (DELETE /subscriptions) или создайте отдельного бота."
        )
    if resp.status_code != 200:
        raise RuntimeError(f"MAX API вернул {resp.status_code}: {resp.text[:500]}")
    updates = resp.json().get("updates", [])
    if not updates:
        log("Событий пока нет. Добавьте бота в группу, сделайте его админом и повторите.")
        return
    log(f"Получено событий: {len(updates)}\n")
    for upd in updates:
        ts = datetime.fromtimestamp(upd.get("timestamp", 0) / 1000, tz=timezone.utc)
        kind = upd.get("update_type")
        chat_id = upd.get("chat_id")
        is_channel = upd.get("is_channel")
        marker = "  <-- похож на вашу группу" if kind in (
            "bot_added", "bot_admin_permissions_changed"
        ) and is_channel is False else ""
        log(f"{ts:%d.%m.%Y %H:%M:%S} UTC | {kind} | chat_id={chat_id} | "
            f"is_channel={is_channel}{marker}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

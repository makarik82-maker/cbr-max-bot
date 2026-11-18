#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Мониторинг курсов USD/EUR ЦБ РФ и уведомление в мессенджер MAX.

Логика:
1. Скачивает страницу https://www.cbr.ru/currency_base/daily/ (резерв: XML ЦБ, зеркало).
2. Сравнивает USD/EUR с предыдущими значениями из state.json.
3. Если курсы изменились — отправляет сообщение ботом MAX в группу.
4. Сохраняет новые значения в state.json (его коммитит GitHub Actions).

Переменные окружения:
  MAX_BOT_TOKEN — токен бота MAX (секрет репозитория)
  MAX_CHAT_ID   — chat_id группового чата MAX (секрет репозитория)
  FORCE_SEND    — "true": отправить текущие курсы принудительно (тест)
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import certifi
import requests
from bs4 import BeautifulSoup

ROOT_DIR = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT_DIR / "state.json"

CBR_HTML_URL = "https://www.cbr.ru/currency_base/daily/"
CBR_XML_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
MIRROR_JSON_URL = "https://www.cbr-xml-daily.ru/daily_json.js"  # неофициальное зеркало данных ЦБ

MAX_API_BASE = "https://platform-api2.max.ru"

# Сертификаты НУЦ Минцифры: и cbr.ru, и platform-api2.max.ru используют TLS-сертификаты,
# которых нет в стандартном наборе доверенных корневых сертификатов.
RU_CA_URLS = [
    "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt",
    "https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

FOUR = Decimal("0.0001")  # курсы ЦБ публикуются с 4 знаками после запятой


def log(msg: str) -> None:
    print(msg, flush=True)


def build_ca_bundle() -> str:
    """Собирает PEM-бандл: стандартные CA (certifi) + корневые сертификаты Минцифры."""
    bundle_path = Path(tempfile.gettempdir()) / "cbr_max_ca_bundle.pem"
    parts = [Path(certifi.where()).read_bytes()]
    for url in RU_CA_URLS:
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            if b"BEGIN CERTIFICATE" in resp.content:
                parts.append(resp.content)
            else:
                log(f"[warn] {url}: ответ не похож на сертификат, пропускаю")
        except requests.RequestException as exc:
            log(f"[warn] не удалось скачать {url}: {exc}")
    bundle_path.write_bytes(b"\n".join(parts))
    return str(bundle_path)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    session.verify = build_ca_bundle()
    return session


def http_get(session: requests.Session, url: str, attempts: int = 3) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            log(f"[warn] попытка {attempt}/{attempts} GET {url}: {exc}")
            if attempt < attempts:
                time.sleep(5 * attempt)
    raise RuntimeError(f"Не удалось получить {url}: {last_exc}")


def norm_rate(value: Decimal, nominal: int = 1) -> str:
    """Нормализует курс к строке с 4 знаками (курс за 1 единицу валюты)."""
    return format((value / nominal).quantize(FOUR), "f")


def parse_cbr_html(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="data")
    if table is None:
        raise ValueError("На странице cbr.ru не найдена таблица курсов (table.data)")
    rates: dict[str, str] = {}
    for row in table.find_all("tr"):
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if len(cells) < 5:
            continue
        char_code = cells[1].upper()
        if char_code in ("USD", "EUR"):
            nominal = int(re.sub(r"[\s\u00a0]", "", cells[2]))
            value = Decimal(cells[4].replace(",", "."))
            rates[char_code.lower()] = norm_rate(value, nominal)
    if "usd" not in rates or "eur" not in rates:
        raise ValueError("В таблице cbr.ru не найдены USD/EUR")
    text = soup.get_text(" ", strip=True)
    m = re.search(r"установил\s+с\s+(\d{2}\.\d{2}\.\d{4})", text) or re.search(
        r"с\s+(\d{2}\.\d{2}\.\d{4})", text
    )
    return {"date": m.group(1) if m else "", **rates}


def parse_cbr_xml(content: bytes) -> dict:
    root = ET.fromstring(content)  # кодировка windows-1251 объявлена внутри XML
    rates: dict[str, str] = {}
    for valute in root.findall("Valute"):
        char_code = (valute.findtext("CharCode") or "").upper()
        if char_code in ("USD", "EUR"):
            nominal = int(valute.findtext("Nominal") or "1")
            value = Decimal((valute.findtext("Value") or "0").replace(",", "."))
            rates[char_code.lower()] = norm_rate(value, nominal)
    if "usd" not in rates or "eur" not in rates:
        raise ValueError("В XML ЦБ не найдены USD/EUR")
    return {"date": root.attrib.get("Date", ""), **rates}


def parse_mirror_json(data: dict) -> dict:
    rates: dict[str, str] = {}
    for code in ("USD", "EUR"):
        item = data["Valute"][code]
        rates[code.lower()] = norm_rate(Decimal(str(item["Value"])), int(item["Nominal"]))
    date = ""
    if data.get("Date"):
        date = datetime.fromisoformat(data["Date"]).strftime("%d.%m.%Y")
    return {"date": date, **rates}


def fetch_rates(session: requests.Session) -> dict:
    """Основной источник — HTML-страница ЦБ; при сбое — XML ЦБ, затем зеркало."""
    errors: list[str] = []
    try:
        data = parse_cbr_html(http_get(session, CBR_HTML_URL).text)
        return {**data, "source": "cbr.ru (страница)"}
    except Exception as exc:
        errors.append(f"HTML cbr.ru: {exc}")
        log(f"[warn] {errors[-1]}")
    try:
        data = parse_cbr_xml(http_get(session, CBR_XML_URL).content)
        return {**data, "source": "cbr.ru (XML)"}
    except Exception as exc:
        errors.append(f"XML cbr.ru: {exc}")
        log(f"[warn] {errors[-1]}")
    try:
        data = parse_mirror_json(http_get(session, MIRROR_JSON_URL).json())
        return {**data, "source": "cbr-xml-daily.ru (зеркало)"}
    except Exception as exc:
        errors.append(f"Зеркало: {exc}")
    raise RuntimeError("Все источники курсов недоступны:\n" + "\n".join(errors))


def load_state() -> dict | None:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None


def save_state(current: dict) -> None:
    state = {
        "date": current["date"],
        "usd": current["usd"],
        "eur": current["eur"],
        "source": current["source"],
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log(f"Состояние сохранено: {STATE_FILE}")


def fmt_rate(value: str) -> str:
    return f"{Decimal(value):.4f}".replace(".", ",")


def delta_suffix(new: str, old: str | None) -> str:
    if old is None:
        return ""
    diff = (Decimal(new) - Decimal(old)).quantize(FOUR)
    if diff == 0:
        return " (без изменений)"
    sign = "+" if diff > 0 else "−"
    return f" ({sign}{fmt_rate(str(abs(diff)))})"


def build_message(current: dict, previous: dict | None) -> str:
    changed = previous is not None and any(
        current[k] != previous.get(k) for k in ("usd", "eur")
    )
    title = "💱 Курсы ЦБ РФ обновились" if changed else "💱 Курсы ЦБ РФ"
    date_part = f" — действуют с {current['date']}" if current.get("date") else ""
    lines = [f"{title}{date_part}", ""]
    for code, flag in (("usd", "🇺🇸 USD"), ("eur", "🇪🇺 EUR")):
        old = previous.get(code) if previous else None
        lines.append(f"{flag}: {fmt_rate(current[code])} ₽{delta_suffix(current[code], old)}")
    lines += ["", "Источник: cbr.ru/currency_base/daily/"]
    return "\n".join(lines)


def send_max(session: requests.Session, text: str, attempts: int = 3) -> None:
    token = os.environ.get("MAX_BOT_TOKEN", "").strip()
    chat_id_raw = os.environ.get("MAX_CHAT_ID", "").strip()
    if not token or not chat_id_raw:
        raise RuntimeError(
            "Не заданы MAX_BOT_TOKEN / MAX_CHAT_ID. Добавьте их в Secrets репозитория."
        )
    chat_id = int(chat_id_raw)  # проверяем, что это число
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.post(
                f"{MAX_API_BASE}/messages",
                params={"chat_id": chat_id, "disable_link_preview": "true"},
                headers={"Authorization": token},  # токен БЕЗ слова Bearer
                json={"text": text[:4000], "notify": True},
                timeout=30,
            )
            if resp.status_code == 200:
                log("Сообщение отправлено в MAX")
                return
            err = f"MAX API вернул {resp.status_code}: {resp.text[:500]}"
            if resp.status_code != 429 and resp.status_code < 500:
                # 400/401/404 — ошибка токена/chat_id/прав: повторять бессмысленно
                raise RuntimeError(err)
            last_exc = RuntimeError(err)  # 429/5xx — временная ошибка, повторим
            log(f"[warn] попытка {attempt}/{attempts} отправки в MAX: {err}")
        except requests.RequestException as exc:
            last_exc = exc
            log(f"[warn] попытка {attempt}/{attempts} отправки в MAX: {exc}")
        if attempt < attempts:
            time.sleep(5 * attempt)
    raise RuntimeError(f"Не удалось отправить сообщение в MAX: {last_exc}")


def main() -> None:
    force = os.environ.get("FORCE_SEND", "false").strip().lower() == "true"
    session = make_session()
    current = fetch_rates(session)
    log(
        f"Курсы на {current['date']}: USD={current['usd']}, EUR={current['eur']} "
        f"(источник: {current['source']})"
    )
    previous = load_state()

    if previous is None:
        log("Первый запуск: сохраняю текущие курсы как точку отсчёта.")
        if force:
            send_max(session, build_message(current, None))
        save_state(current)
        return

    changed = any(current[k] != previous.get(k) for k in ("usd", "eur"))
    if changed:
        log("Зафиксировано изменение курсов — отправляю уведомление.")
        send_max(session, build_message(current, previous))
        save_state(current)
    elif force:
        log("Изменений нет, но FORCE_SEND=true — отправляю тестовое сообщение.")
        send_max(session, build_message(current, previous))
        save_state(current)
    else:
        log("Курсы USD/EUR не изменились — уведомление не требуется.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

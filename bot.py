"""
Telegram-бот «Во сколько вставать» — бесплатная версия БЕЗ Яндекс API.

Что умеет:
    прем к 15:00
    мне надо в премьер завтра к 12:30
    виктория к 18:00
    почтовая к 14:20
    лиза к 19:00
    лизина квартира к 20:00
    на работу
    на работу к 09:30
    адрес: Московское шоссе, 33 к 17:00
    мне надо по адресу Первомайский проспект, 21 к 16:40

Логика бесплатной версии:
1) Никаких ключей Яндекс Карт и никаких запросов к картографическим API.
2) Для основных мест используются средние времена по опубликованным маршрутам
   общественного транспорта Рязани + небольшой запас на реальную дорогу.
3) Для произвольного адреса применяется приблизительная оценка по названию улицы/
   району. Если улица неизвестна боту, используется консервативная средняя поездка
   по Рязани.
4) Будильник = время выхода - 90 минут сборов - 10 минут TikTok.
5) Для работы персональное правило: если к 09:30, выйти в 08:40, будильник в 07:00.

Единственная обязательная переменная окружения:
    BOT_TOKEN

Важно: эта версия НЕ знает текущие пробки, ДТП, фактическое положение автобуса и
временные перекрытия. Все времена дороги — ориентиры.
"""

from __future__ import annotations

import csv
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8820034586:AAF0Cz7wf4pHY3IiS_oJ2lRK6dTD4aBkmZQ")

CITY = "Рязань"
TZ = ZoneInfo("Europe/Moscow")

HOME_ADDRESS = "Рязань, Шереметьевская улица, 10к3"
HOME_STOP_NAME = "Дубовая Роща"

# Сколько времени после будильника уходит на TikTok и сборы.
TIKTOK_MINUTES = 10
PREP_MINUTES = 90

# Рабочий график.
WORK_START_HOUR = 9
WORK_START_MINUTE = 30
WORK_END_HOUR = 21
WORK_END_MINUTE = 0

# Личное правило для работы: к 09:30 выйти в 08:40.
WORK_LEAVE_BEFORE_MINUTES = 50
WORK_ELEVATOR_EXIT_MINUTES = 5
WORK_WALK_MINUTES = 3

# До «Дубовой Рощи» от дома берём около 4 минут пешком.
HOME_TO_STOP_MINUTES = 4

# При интервале автобуса около 10 минут среднее ожидание ≈ 5 минут.
AVERAGE_WAIT_MINUTES = 5

LOG_FILE = "bot_log.csv"
ADMIN_ID = 432613414

# Основные места. Время в автобусе основано на опубликованном расписании
# маршрута №53 от «Дубовой Рощи» в сторону центра/Московского шоссе.
# reliability_buffer — небольшой запас на светофоры, посадку и обычные задержки.
DESTINATIONS = {
    "work": {
        "name": "Работа",
        "address": "Рязань, Шереметьевская улица, 11",
        "aliases": (
            "работа",
            "на работу",
            "работу",
            "шереметьевская 11",
            "шереметьевская улица 11",
        ),
        "kind": "work",
    },
    "premier": {
        "name": "ТЦ Премьер",
        "address": "Рязань, Московское шоссе, 21",
        "aliases": ("прем", "премьер", "тц премьер", "премик"),
        "kind": "transit",
        "exit_stop": "ТРЦ Премьер",
        "routes": "53, 66 (ориентир)",
        "transit_minutes": 37,
        "walk_after_minutes": 2,
        "reliability_buffer": 5,
    },
    "victoria": {
        "name": "ТЦ Виктория Плаза",
        "address": "Рязань, Первомайский проспект, 70, корп. 1",
        "aliases": ("виктория", "виктория плаза", "тц виктория плаза", "вик"),
        "kind": "transit",
        "exit_stop": "Вокзальная улица",
        "routes": "53, 66 и другие по Первомайскому проспекту",
        "transit_minutes": 33,
        "walk_after_minutes": 3,
        "reliability_buffer": 5,
    },
    "pochtovaya": {
        "name": "Почтовая улица",
        "address": "Рязань, Почтовая улица",
        "aliases": (
            "почтовая",
            "почта",
            "почтовая улица",
            "площадь ленина",
            "пл ленина",
            "пл. ленина",
        ),
        "kind": "transit",
        "exit_stop": "Площадь Ленина",
        "routes": "17, 53, 65, 66 (ориентир)",
        "transit_minutes": 28,
        # Почтовая длиннее одной точки, поэтому берём чуть более консервативно.
        "walk_after_minutes": 5,
        "reliability_buffer": 5,
    },
    "liza": {
        "name": "Лиза — квартира",
        "address": "Рязань, Быстрецкая улица, 18к2",
        "aliases": (
            "лиза",
            "лизка",
            "лизя",
            "лизина квартира",
            "квартира лизы",
            "к лизе",
            "к лизке",
            "к лизе домой",
            "домой к лизе",
            "быстрецкая 18к2",
            "быстрецкая 18 к2",
            "быстрецкая 18 корпус 2",
            "быстрецкая улица 18к2",
            "быстрецкая улица 18 к2",
        ),
        "kind": "transit",
        "exit_stop": "ближайшая остановка к Быстрецкой, 18к2",
        "routes": "городской транспорт (ориентир)",
        # Персональная средняя оценка для маршрута к Лизе.
        "transit_minutes": 15,
        "walk_after_minutes": 5,
        "reliability_buffer": 5,
    },
}

# =========================================================
# REGEX / DATA MODELS
# =========================================================

TIME_WITH_MINUTES_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*[:.]\s*(\d{2})(?:\s*(утра|дня|вечера|ночи))?\b",
    re.IGNORECASE,
)
TIME_WITH_PREPOSITION_RE = re.compile(
    r"(?:^|\s)(?:к|в|на)\s*(\d{1,2})(?:\s*(утра|дня|вечера|ночи))?\b",
    re.IGNORECASE,
)
ADDRESS_PREFIX_RE = re.compile(r"(?:по\s+адресу|адрес)\s*[:\-]?\s*(.+)", re.IGNORECASE)
ADDRESS_HINT_RE = re.compile(
    r"\b(улиц\w*|ул\.?|шоссе|ш\.?|проспект\w*|просп\.?|пр-т|переул\w*|пер\.?|"
    r"площад\w*|пл\.?|проезд\w*|наб\.?|набережн\w*)\b",
    re.IGNORECASE,
)


@dataclass
class Destination:
    key: str
    name: str
    address: str
    kind: str
    custom: bool = False


@dataclass
class RouteEstimate:
    walk_to_stop: int
    wait: int
    transit: int
    walk_after: int
    buffer: int
    exit_stop: str
    routes: str
    reason: str

    @property
    def total_minutes(self) -> int:
        return self.walk_to_stop + self.wait + self.transit + self.walk_after + self.buffer


# =========================================================
# TEXT PARSING
# =========================================================


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


def _apply_period(hour: int, period: str) -> int:
    period = (period or "").lower()
    if period == "утра":
        return 0 if hour == 12 else hour
    if period in ("дня", "вечера"):
        return hour + 12 if 1 <= hour < 12 else hour
    if period == "ночи":
        return 0 if hour == 12 else hour
    return hour


def parse_target_time(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    now = now or datetime.now(TZ)

    match = TIME_WITH_MINUTES_RE.search(text)
    minute = 0
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        period = match.group(3) or ""
    else:
        match = TIME_WITH_PREPOSITION_RE.search(text)
        if not match:
            return None
        hour = int(match.group(1))
        period = match.group(2) or ""

    hour = _apply_period(hour, period)
    if hour > 23 or minute > 59:
        return None

    lower = normalize_text(text)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if "завтра" in lower:
        target += timedelta(days=1)
    elif "сегодня" in lower:
        pass
    elif target <= now:
        target += timedelta(days=1)

    return target


def default_work_target(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(TZ)
    target = now.replace(
        hour=WORK_START_HOUR,
        minute=WORK_START_MINUTE,
        second=0,
        microsecond=0,
    )
    if target <= now:
        target += timedelta(days=1)
    return target


def _remove_time_expression(text: str) -> str:
    cleaned = TIME_WITH_MINUTES_RE.sub(" ", text)
    cleaned = TIME_WITH_PREPOSITION_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\b(сегодня|завтра)\b", " ", cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip(" ,.-")


def _clean_address(text: str) -> str:
    address = _remove_time_expression(text)
    address = re.sub(
        r"\b(?:мне\s+надо|надо|нужно|быть|приехать|доехать|попасть)\b",
        " ",
        address,
        flags=re.IGNORECASE,
    )
    address = re.sub(r"^(?:в|на|к)\s+", "", address, flags=re.IGNORECASE)
    address = re.sub(r"\s+(?:к|в|на)$", "", address, flags=re.IGNORECASE)
    address = re.sub(r"\s+", " ", address).strip(" ,.-")
    if address and "рязань" not in normalize_text(address):
        address = f"{CITY}, {address}"
    return address


def detect_destination(text: str) -> Optional[Destination]:
    lower = normalize_text(text)

    explicit = ADDRESS_PREFIX_RE.search(text)
    if explicit:
        address = _clean_address(explicit.group(1))
        if address:
            norm = normalize_text(address)
            if "быстрецкая" in norm and re.search(r"(?<!\d)18\s*(?:к|корп(?:ус)?\.?\s*)?2(?!\d)", norm):
                data = DESTINATIONS["liza"]
                return Destination("liza", data["name"], data["address"], data["kind"])
            return Destination("custom", address, address, "transit", custom=True)

    # Сначала основные места — чтобы "Московское шоссе, 21" можно было узнать как Премьер
    # только если пользователь использует его имя/алиас. Явный "адрес:" всегда остаётся custom.
    candidates = []
    for key, data in DESTINATIONS.items():
        for alias in data["aliases"]:
            candidates.append((len(alias), alias, key, data))

    for _, alias, key, data in sorted(candidates, reverse=True):
        alias_norm = normalize_text(alias)
        if re.search(rf"(?<!\w){re.escape(alias_norm)}(?!\w)", lower):
            return Destination(key, data["name"], data["address"], data["kind"])

    # Произвольный точный адрес или название улицы.
    if ADDRESS_HINT_RE.search(text):
        address = _clean_address(text)
        if address:
            # Если это адрес работы, сохраняем специальное рабочее правило.
            norm = normalize_text(address)
            if "шереметьевская" in norm and re.search(r"(?<!\d)11(?!\d)", norm):
                data = DESTINATIONS["work"]
                return Destination("work", data["name"], data["address"], "work")
            if "быстрецкая" in norm and re.search(r"(?<!\d)18\s*(?:к|корп(?:ус)?\.?\s*)?2(?!\d)", norm):
                data = DESTINATIONS["liza"]
                return Destination("liza", data["name"], data["address"], data["kind"])
            return Destination("custom", address, address, "transit", custom=True)

    return None


# =========================================================
# FREE / OFFLINE ROUTE ESTIMATES
# =========================================================


def preset_route_estimate(destination: Destination) -> RouteEstimate:
    data = DESTINATIONS[destination.key]
    return RouteEstimate(
        walk_to_stop=HOME_TO_STOP_MINUTES,
        wait=AVERAGE_WAIT_MINUTES,
        transit=int(data["transit_minutes"]),
        walk_after=int(data["walk_after_minutes"]),
        buffer=int(data["reliability_buffer"]),
        exit_stop=str(data["exit_stop"]),
        routes=str(data["routes"]),
        reason="среднее по расписанию блять + небольшой запас, потому что яндекс пидорасы требует 200к за точный расчет",
    )


def estimate_custom_route(address: str) -> RouteEstimate:
    """Грубая бесплатная оценка по названию улицы без геокодирования и API."""
    a = normalize_text(address)

    # Совсем рядом с домом — разумнее считать пешком.
    if any(x in a for x in ("шереметьев", "песоченск")):
        return RouteEstimate(
            walk_to_stop=0,
            wait=0,
            transit=0,
            walk_after=15,
            buffer=5,
            exit_stop="—",
            routes="пешком",
            reason="адрес в районе дома; грубая оцена на глазик, потому что яндекс пидорасы требует 200к за точный расчет",
        )

    # Ближняя Песочня / Новосёлов / Зубковой.
    if any(x in a for x in ("новоселов", "новосёлов", "зубковой", "тимакова")):
        return RouteEstimate(
            walk_to_stop=HOME_TO_STOP_MINUTES,
            wait=AVERAGE_WAIT_MINUTES,
            transit=10,
            walk_after=4,
            buffer=5,
            exit_stop="ближайшая остановка к адресу",
            routes="городской транспорт",
            reason="оценка для ближней части Песочни, потому что яндекс пидорасы требует 200к за точный расчет",
        )

    # Касимовское шоссе / Советской Армии — заметно ближе центра.
    if any(x in a for x in ("касимовск", "советской армии", "кальное")):
        return RouteEstimate(
            walk_to_stop=HOME_TO_STOP_MINUTES,
            wait=AVERAGE_WAIT_MINUTES,
            transit=16,
            walk_after=5,
            buffer=5,
            exit_stop="ближайшая остановка к адресу",
            routes="городской транспорт",
            reason="оценка по восточной части маршрута к центру, потому что яндекс пидорасы требует 200к за точный расчет",
        )

    # Центр: Ленина / Соборная / Свободы / Театральная / Почтовая.
    if any(x in a for x in ("ленина", "соборн", "свободы", "театраль", "почтов", "грибоедова")):
        return RouteEstimate(
            walk_to_stop=HOME_TO_STOP_MINUTES,
            wait=AVERAGE_WAIT_MINUTES,
            transit=28,
            walk_after=6,
            buffer=5,
            exit_stop="остановка в центре рядом с адресом",
            routes="17 / 53 / 65 / 66 или аналогичный",
            reason="оценка по центральной части Рязани, потому что яндекс пидорасы требует 200к за точный расчет",
        )

    # Первомайский / вокзалы / площадь Победы.
    if any(x in a for x in ("первомайск", "вокзальн", "победы", "дзержин")):
        return RouteEstimate(
            walk_to_stop=HOME_TO_STOP_MINUTES,
            wait=AVERAGE_WAIT_MINUTES,
            transit=33,
            walk_after=5,
            buffer=5,
            exit_stop="Вокзальная / Площадь Победы или соседняя",
            routes="53 / 66 или аналогичный",
            reason="оценка по району Первомайского проспекта, потому что яндекс пидорасы требует 200к за точный расчет",
        )

    # Московское шоссе — район Премьера/автовокзала и дальше.
    if any(x in a for x in ("московск", "мервин", "автовокзал", "завражнова")):
        return RouteEstimate(
            walk_to_stop=HOME_TO_STOP_MINUTES,
            wait=AVERAGE_WAIT_MINUTES,
            transit=38,
            walk_after=6,
            buffer=7,
            exit_stop="ближайшая остановка на Московском шоссе",
            routes="53 / 66 или другой подходящий",
            reason="оценка по Московскому шоссе, потому что яндекс пидорасы требует 200к за точный расчет",
        )

    # Любой неизвестный адрес в Рязани: не притворяемся, что знаем точный маршрут.
    return RouteEstimate(
        walk_to_stop=HOME_TO_STOP_MINUTES,
        wait=7,
        transit=38,
        walk_after=6,
        buffer=7,
        exit_stop="ближайшая остановка к адресу",
        routes="подходящий городской транспорт",
        reason="средняя консервативная оценка по Рязани; потому что яндекс пидорасы требует 200к за точный расчет",
    )


def calculate_route_estimate(destination: Destination) -> RouteEstimate:
    if destination.key in ("premier", "victoria", "pochtovaya", "liza"):
        return preset_route_estimate(destination)
    return estimate_custom_route(destination.address)


# =========================================================
# REPLY BUILDING
# =========================================================


def day_label(target: datetime, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(TZ)
    if target.date() == now.date():
        return "сегодня"
    if target.date() == (now + timedelta(days=1)).date():
        return "завтра"
    return target.strftime("%d.%m.%Y")


def alarm_time_from_leave(leave_time: datetime) -> datetime:
    return leave_time - timedelta(minutes=PREP_MINUTES + TIKTOK_MINUTES)


def build_work_reply(target: datetime) -> str:
    leave_time = target - timedelta(minutes=WORK_LEAVE_BEFORE_MINUTES)
    alarm_time = alarm_time_from_leave(leave_time)

    personal_buffer = max(
        0,
        WORK_LEAVE_BEFORE_MINUTES - WORK_ELEVATOR_EXIT_MINUTES - WORK_WALK_MINUTES,
    )

    return (
        f"🏢 Работа(хаха уебище на работу надо) — {DESTINATIONS['work']['address']}\n"
        f"Нужно быть {day_label(target)} к {target.strftime('%H:%M')}.\n\n"
        f"⏰ Будильник(я тебя могну): {alarm_time.strftime('%H:%M')}\n"
        f"📱 TikTok(уебище): {TIKTOK_MINUTES} мин\n"
        f"🧼 Сборы: {PREP_MINUTES} мин\n"
        f"🚪 Выйти из дома: {leave_time.strftime('%H:%M')}\n\n"
        f"Маршрут до работы:\n"
        f"• лифт + выход из дома: ~{WORK_ELEVATOR_EXIT_MINUTES} мин\n"
        f"• пешком до работы: ~{WORK_WALK_MINUTES} мин\n"
        f"• личный ранний запас: ~{personal_buffer} мин\n\n"
        f"Для смены 09:30 получается именно: будильник 07:00 → выход 08:40."
    )


def build_transit_reply(destination: Destination, target: datetime) -> str:
    estimate = calculate_route_estimate(destination)
    leave_time = target - timedelta(minutes=estimate.total_minutes)
    alarm_time = alarm_time_from_leave(leave_time)
    now = datetime.now(TZ)

    late_note = ""
    if alarm_time <= now:
        late_note = (
            "\n⚠️ По этому расчёту будильник уже должен был прозвенеть. "
            "Если поездка сегодня — лучше собираться и выходить как можно раньше."
        )

    custom_note = ""
    if destination.custom:
        custom_note = (
            "\n⚠️ Это произвольный адрес: без карт/API я определяю время только "
            "примерно по названию улицы/району."
        )

    return (
        f"📍 {destination.name}\n"
        f"Нужно быть {day_label(target)} к {target.strftime('%H:%M')}.\n\n"
        f"⏰ Будильник: {alarm_time.strftime('%H:%M')}\n"
        f"📱 TikTok(уебище злое): {TIKTOK_MINUTES} мин\n"
        f"🧼 Сборы: {PREP_MINUTES} мин\n"
        f"🚪 Выйти из дома: {leave_time.strftime('%H:%M')}\n\n"
        f"🚌 Бесплатная примерная оценка дороги:\n"
        f"• пешком до {HOME_STOP_NAME}: ~{estimate.walk_to_stop} мин\n"
        f"• среднее ожидание транспорта: ~{estimate.wait} мин\n"
        f"• в транспорте: ~{estimate.transit} мин\n"
        f"• выйти на: {estimate.exit_stop}\n"
        f"• пешком после транспорта: ~{estimate.walk_after} мин\n"
        f"• запас на обычные задержки: ~{estimate.buffer} мин\n"
        f"• ВСЯ ДОРОГА: ~{estimate.total_minutes} мин\n"
        f"• транспорт: {estimate.routes}\n\n"
        f"ℹ️ Основа расчёта: {estimate.reason}. Пробки в реальном времени не учитываются."
        f"{custom_note}{late_note}"
    )


# =========================================================
# TELEGRAM HANDLERS
# =========================================================


def log_interaction(update: Update) -> None:
    user = update.effective_user
    text = update.message.text or ""
    file_exists = os.path.isfile(LOG_FILE)

    with open(LOG_FILE, mode="a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "user_id", "username", "first_name", "message"])
        writer.writerow(
            [
                datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
                user.id if user else "",
                user.username if user else "",
                user.first_name if user else "",
                text,
            ]
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    log_interaction(update)

    destination = detect_destination(text)
    target = parse_target_time(text)

    # «на работу» без времени = к началу смены 09:30.
    if destination and destination.kind == "work" and target is None:
        target = default_work_target()

    if destination is None:
        await update.message.reply_text(
            "Не понял блять место. Тупорылая напиши, например:\n"
            "• «прем к 15:00»\n"
            "• «виктория завтра к 18:30»\n"
            "• «почтовая к 14:00»\n"
            "• «лиза к 19:00»\n"
            "• «на работу»\n"
            "• «адрес: Московское шоссе, 33 к 17:00»"
        )
        return

    if target is None:
        await update.message.reply_text(
            "Не вижу время, к которому надо быть на месте. Например: «прем к 15:00»."
        )
        return

    now = datetime.now(TZ)
    if "сегодня" in normalize_text(text) and target <= now:
        await update.message.reply_text(
            "Это время сегодня уже прошло. Укажи другое время или напиши «завтра»."
        )
        return

    if destination.kind == "work":
        reply = build_work_reply(target)
    else:
        reply = build_transit_reply(destination, target)

    await update.message.reply_text(reply)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бесплатный режим: Яндекс API не нужен. Я считаю дорогу по средним временам.\n\n"
        "Напиши место и время прибытия:\n"
        "• «прем к 15:00»\n"
        "• «виктория к 18:00»\n"
        "• «почтовая к 14:30»\n"
        "• «лиза к 19:00» / «лизина квартира к 20:00»\n"
        "• «на работу» — автоматически к 09:30\n\n"
        "Можно и точный адрес:\n"
        "• «адрес: Московское шоссе, 33 к 17:00»\n\n"
        "Для произвольных адресов расчёт будет примерным по улице/району, "
        "потому что картографические API полностью отключены."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)


def main():
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬТЕ_СЮДА_ВАШ_ТОКЕН":
        raise RuntimeError(
            "Не задан BOT_TOKEN. Укажите токен Telegram-бота в переменной окружения BOT_TOKEN."
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен в бесплатном режиме без Яндекс API")
    app.run_polling()


if __name__ == "__main__":
    main()

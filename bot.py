"""
Telegram-бот «Во сколько вставать» — версия с маршрутами Яндекс Карт.

Примеры сообщений:
    прем к 15:00
    мне надо в премьер завтра к 12:30
    виктория к 18:00
    почтовая к 14:20
    на работу
    на работу к 09:30
    адрес: Московское шоссе, 33 к 17:00
    мне надо по адресу Первомайский проспект, 21 к 16:40

Логика:
1) Пользователь указывает место и время, к которому нужно быть на месте.
2) Для обычных мест бот строит маршрут через официальные API Яндекс Карт.
3) Бот считает время выхода назад от времени прибытия.
4) Время будильника = время выхода - 90 минут сборов - 10 минут TikTok.
5) Для работы действует персональное правило: при начале в 09:30 выйти в 08:40.

Нужные переменные окружения:
    BOT_TOKEN
    YANDEX_ROUTER_API_KEY
    YANDEX_GEOCODER_API_KEY

Можно вместо двух ключей задать один YANDEX_MAPS_API_KEY — код использует его
как запасной вариант для обоих API.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
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

# Официальные API Яндекс Карт.
YANDEX_MAPS_API_KEY = os.environ.get("YANDEX_MAPS_API_KEY", "")
YANDEX_ROUTER_API_KEY = os.environ.get("YANDEX_ROUTER_API_KEY", YANDEX_MAPS_API_KEY)
YANDEX_GEOCODER_API_KEY = os.environ.get("YANDEX_GEOCODER_API_KEY", YANDEX_MAPS_API_KEY)

CITY = "Рязань"
TZ = ZoneInfo("Europe/Moscow")

HOME_ADDRESS = "Рязань, Шереметьевская улица, 10к3"
HOME_STOP_NAME = "Дубовая Роща"

# Координаты остановки «Дубовая Роща» в Рязани.
# Формат Router API: latitude, longitude.
HOME_STOP_COORDS = (54.601111, 39.832609)

# Сборы и привычка после будильника.
PREP_MINUTES = 90
TIKTOK_MINUTES = 10

# Дополнительный общий запас для поездок. По вашему описанию отдельный запас
# не нужен, поэтому 0. Если захотите всегда приезжать на 5 минут раньше — поставьте 5.
SAFETY_BUFFER_MINUTES = 0

# Рабочий график.
WORK_START_HOUR = 9
WORK_START_MINUTE = 30
WORK_END_HOUR = 21
WORK_END_MINUTE = 0

# Персональное правило для работы: при начале в 09:30 выйти в 08:40,
# то есть за 50 минут до нужного времени. Это намеренно важнее фактических
# 2–3 минут пешком, потому что вы отдельно указали желаемый выход 08:40.
WORK_LEAVE_BEFORE_MINUTES = 50

# Используется только если Яндекс недоступен при расчёте пешего участка до работы.
WORK_FALLBACK_WALK_MINUTES = 3

# Оценка лифта + выхода из квартиры/дома. Она нужна только для понятной разбивки.
# Время выхода из дома для работы всё равно задаётся правилом выше.
ELEVATOR_AND_EXIT_MINUTES = 5

LOG_FILE = "bot_log.csv"
ADMIN_ID = 432613414

# Основные места и сокращения.
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
        "aliases": (
            "прем",
            "премьер",
            "тц премьер",
            "премик",
        ),
        "kind": "transit",
    },
    "victoria": {
        "name": "ТЦ Виктория Плаза",
        "address": "Рязань, Первомайский проспект, 70, корп. 1",
        "aliases": (
            "виктория",
            "виктория плаза",
            "тц виктория плаза",
            "вик",
        ),
        "kind": "transit",
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
        "stop_hint": "Площадь Ленина",
    },
}

# =========================================================
# REGEX / DATA MODELS
# =========================================================

# Приоритет формату с минутами, чтобы не принять номер дома за время.
TIME_WITH_MINUTES_RE = re.compile(
    r"(?<!\d)(\d{1,2})\s*[:.]\s*(\d{2})(?:\s*(утра|дня|вечера|ночи))?\b",
    re.IGNORECASE,
)
TIME_WITH_PREPOSITION_RE = re.compile(
    r"(?:^|\s)(?:к|в|на)\s*(\d{1,2})(?:\s*(утра|дня|вечера|ночи))?\b",
    re.IGNORECASE,
)

ADDRESS_PREFIX_RE = re.compile(
    r"(?:по\s+адресу|адрес)\s*[:\-]?\s*(.+)",
    re.IGNORECASE,
)

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
    stop_hint: Optional[str] = None
    custom: bool = False


@dataclass
class RouteBreakdown:
    total_seconds: float
    walk_before_seconds: float
    transit_seconds: float
    transfer_walk_seconds: float
    walk_after_seconds: float
    distance_meters: float


@dataclass
class JourneyPlan:
    leave_time: datetime
    total_seconds: float
    walk_to_home_stop_seconds: float
    transit_seconds: float
    transfer_walk_seconds: float
    walk_after_seconds: float
    distance_meters: float
    source: str = "Яндекс Карты"


# Кэш геокодирования на время жизни процесса.
_geocode_cache: dict[str, tuple[float, float]] = {}


class YandexAPIError(RuntimeError):
    pass


# =========================================================
# TEXT PARSING
# =========================================================


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


def _apply_period(hour: int, period: str) -> int:
    period = (period or "").lower()

    if period == "утра":
        if hour == 12:
            return 0
        return hour

    if period in ("дня", "вечера"):
        if 1 <= hour < 12:
            return hour + 12
        return hour

    if period == "ночи":
        # «2 ночи» = 02:00, «12 ночи» = 00:00.
        if hour == 12:
            return 0
        return hour

    return hour


def parse_target_time(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Парсит время и возвращает ближайший будущий момент в московском времени."""
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
        target = target + timedelta(days=1)
    elif "сегодня" in lower:
        # Если пользователь явно написал «сегодня», не переносим молча на завтра.
        # Обработчик ниже скажет, что время уже прошло.
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


def detect_destination(text: str) -> Optional[Destination]:
    lower = normalize_text(text)

    # Если пользователь явно написал «адрес: ...» / «по адресу ...»,
    # это всегда произвольный адрес, даже если внутри встречается слово
    # «Почтовая» или другое имя из списка основных мест.
    explicit = ADDRESS_PREFIX_RE.search(text)
    if explicit:
        address = _remove_time_expression(explicit.group(1))
        address = re.sub(r"\b(?:мне\s+надо|быть|приехать)\b", " ", address, flags=re.IGNORECASE)
        address = re.sub(r"\s+(?:к|в|на)$", "", address, flags=re.IGNORECASE)
        address = re.sub(r"\s+", " ", address).strip(" ,.-")
        if address:
            if "рязань" not in normalize_text(address):
                address = f"{CITY}, {address}"
            return Destination(
                key="custom",
                name=address,
                address=address,
                kind="transit",
                custom=True,
            )

    # Точный адрес с номером дома тоже считаем произвольным адресом.
    # Исключение — известный адрес работы, для него сохраняем персональное правило 08:40.
    if ADDRESS_HINT_RE.search(text):
        possible_address = _remove_time_expression(text)
        if re.search(r"\d", possible_address):
            possible_norm = normalize_text(possible_address)
            if "шереметьевская" in possible_norm and re.search(r"(?<!\d)11(?!\d)", possible_norm):
                data = DESTINATIONS["work"]
                return Destination(
                    key="work",
                    name=data["name"],
                    address=data["address"],
                    kind=data["kind"],
                )

            address = re.sub(
                r"\b(?:мне\s+надо|надо|нужно|быть|приехать|доехать|попасть)\b",
                " ",
                possible_address,
                flags=re.IGNORECASE,
            )
            address = re.sub(r"^(?:в|на|к)\s+", "", address, flags=re.IGNORECASE)
            address = re.sub(r"\s+(?:к|в|на)$", "", address, flags=re.IGNORECASE)
            address = re.sub(r"\s+", " ", address).strip(" ,.-")
            if address:
                if "рязань" not in normalize_text(address):
                    address = f"{CITY}, {address}"
                return Destination(
                    key="custom",
                    name=address,
                    address=address,
                    kind="transit",
                    custom=True,
                )

    # Основные места. Длинные aliases проверяем раньше коротких.
    candidates = []
    for key, data in DESTINATIONS.items():
        for alias in data["aliases"]:
            candidates.append((len(alias), alias, key, data))

    for _, alias, key, data in sorted(candidates, reverse=True):
        alias_norm = normalize_text(alias)
        # Для коротких алиасов требуем границы слова: «вик» не должен совпасть случайно.
        if re.search(rf"(?<!\w){re.escape(alias_norm)}(?!\w)", lower):
            return Destination(
                key=key,
                name=data["name"],
                address=data["address"],
                kind=data["kind"],
                stop_hint=data.get("stop_hint"),
            )

    # Адрес без номера дома: например «Первомайский проспект к 18:00».
    if ADDRESS_HINT_RE.search(text):
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
        if address:
            if "рязань" not in normalize_text(address):
                address = f"{CITY}, {address}"
            return Destination(
                key="custom",
                name=address,
                address=address,
                kind="transit",
                custom=True,
            )

    return None


# =========================================================
# YANDEX API
# =========================================================


def _http_get_json(url: str, params: dict, timeout: int = 15) -> dict:
    query = urlencode(params)
    request = Request(
        f"{url}?{query}",
        headers={"User-Agent": "wake-up-telegram-bot/2.0"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise YandexAPIError(f"Ошибка запроса к Яндекс API: {exc}") from exc


async def geocode_address(address: str) -> tuple[float, float]:
    if address in _geocode_cache:
        return _geocode_cache[address]

    if not YANDEX_GEOCODER_API_KEY:
        raise YandexAPIError("Не задан YANDEX_GEOCODER_API_KEY")

    data = await asyncio.to_thread(
        _http_get_json,
        "https://geocode-maps.yandex.ru/v1/",
        {
            "apikey": YANDEX_GEOCODER_API_KEY,
            "geocode": address,
            "lang": "ru_RU",
            "format": "json",
            "results": 1,
            # Центр Рязани помогает Яндексу выбирать нужный объект при неоднозначности.
            "ll": "39.7359,54.6296",
            "spn": "0.5,0.5",
        },
    )

    try:
        members = data["response"]["GeoObjectCollection"]["featureMember"]
        pos = members[0]["GeoObject"]["Point"]["pos"]
        lon_s, lat_s = pos.split()
        coords = (float(lat_s), float(lon_s))
    except (KeyError, IndexError, ValueError) as exc:
        raise YandexAPIError(f"Яндекс не смог найти адрес: {address}") from exc

    _geocode_cache[address] = coords
    return coords


def _flatten_route_steps(data: dict) -> list[dict]:
    route = data.get("route")
    if not route:
        routes = data.get("routes") or []
        route = routes[0] if routes else None

    if not route:
        raise YandexAPIError("Яндекс не вернул маршрут")

    steps: list[dict] = []
    for leg in route.get("legs", []):
        if leg.get("status") != "OK":
            raise YandexAPIError("Яндекс не смог построить один из участков маршрута")
        steps.extend(leg.get("steps", []))

    if not steps:
        raise YandexAPIError("Маршрут пуст")
    return steps


async def yandex_route(
    start: tuple[float, float],
    finish: tuple[float, float],
    mode: str,
    departure_time: Optional[datetime] = None,
) -> RouteBreakdown:
    if not YANDEX_ROUTER_API_KEY:
        raise YandexAPIError("Не задан YANDEX_ROUTER_API_KEY")

    params = {
        "apikey": YANDEX_ROUTER_API_KEY,
        "waypoints": f"{start[0]:.6f},{start[1]:.6f}|{finish[0]:.6f},{finish[1]:.6f}",
        "mode": mode,
    }

    if departure_time is not None and mode == "transit":
        # API запрещает время отправления в прошлом.
        now = datetime.now(TZ)
        safe_departure = max(departure_time, now + timedelta(minutes=1))
        params["departure_time"] = int(safe_departure.timestamp())

    data = await asyncio.to_thread(
        _http_get_json,
        "https://api.routing.yandex.net/v2/route",
        params,
    )

    if data.get("errors"):
        raise YandexAPIError("; ".join(map(str, data["errors"])))

    steps = _flatten_route_steps(data)

    total = sum(float(step.get("duration", 0) or 0) for step in steps)
    distance = sum(float(step.get("length", 0) or 0) for step in steps)

    transit_indexes = [i for i, step in enumerate(steps) if step.get("mode") == "transit"]

    if mode == "walking" or not transit_indexes:
        return RouteBreakdown(
            total_seconds=total,
            walk_before_seconds=total,
            transit_seconds=0,
            transfer_walk_seconds=0,
            walk_after_seconds=0,
            distance_meters=distance,
        )

    first_transit = transit_indexes[0]
    last_transit = transit_indexes[-1]

    walk_before = 0.0
    transfer_walk = 0.0
    walk_after = 0.0
    transit = 0.0

    for i, step in enumerate(steps):
        duration = float(step.get("duration", 0) or 0)
        step_mode = step.get("mode")

        if step_mode == "transit":
            transit += duration
        elif step_mode == "walking":
            if i < first_transit:
                walk_before += duration
            elif i > last_transit:
                walk_after += duration
            else:
                transfer_walk += duration

    return RouteBreakdown(
        total_seconds=total,
        walk_before_seconds=walk_before,
        transit_seconds=transit,
        transfer_walk_seconds=transfer_walk,
        walk_after_seconds=walk_after,
        distance_meters=distance,
    )


async def calculate_transit_plan(destination: Destination, target: datetime) -> JourneyPlan:
    home_coords, destination_coords = await asyncio.gather(
        geocode_address(HOME_ADDRESS),
        geocode_address(destination.address),
    )

    # Отдельно считаем путь из дома до вашей фиксированной остановки.
    walk_to_stop = await yandex_route(
        home_coords,
        HOME_STOP_COORDS,
        mode="walking",
    )

    # Ищем отправление назад от требуемого времени прибытия.
    # Два прохода обычно достаточно, чтобы подставить в прогноз более реалистичное
    # время посадки на транспорт.
    leave_guess = target - timedelta(minutes=45)
    transit_part: Optional[RouteBreakdown] = None

    for _ in range(2):
        boarding_guess = leave_guess + timedelta(seconds=walk_to_stop.total_seconds)
        transit_part = await yandex_route(
            HOME_STOP_COORDS,
            destination_coords,
            mode="transit",
            departure_time=boarding_guess,
        )

        total_seconds = walk_to_stop.total_seconds + transit_part.total_seconds
        leave_guess = target - timedelta(
            seconds=total_seconds,
            minutes=SAFETY_BUFFER_MINUTES,
        )

    assert transit_part is not None

    # Если Router API добавил короткую ходьбу от координаты остановки до точки
    # посадки, относим её к «дойти до остановки/посадки».
    walk_before = walk_to_stop.total_seconds + transit_part.walk_before_seconds
    total_seconds = walk_to_stop.total_seconds + transit_part.total_seconds
    leave_time = target - timedelta(
        seconds=total_seconds,
        minutes=SAFETY_BUFFER_MINUTES,
    )

    return JourneyPlan(
        leave_time=leave_time,
        total_seconds=total_seconds,
        walk_to_home_stop_seconds=walk_before,
        transit_seconds=transit_part.transit_seconds,
        transfer_walk_seconds=transit_part.transfer_walk_seconds,
        walk_after_seconds=transit_part.walk_after_seconds,
        distance_meters=walk_to_stop.distance_meters + transit_part.distance_meters,
    )


async def calculate_work_walk_minutes() -> int:
    """Пытается получить реальное пешее время Яндекса; при ошибке возвращает 3 мин."""
    try:
        home_coords, work_coords = await asyncio.gather(
            geocode_address(HOME_ADDRESS),
            geocode_address(DESTINATIONS["work"]["address"]),
        )
        route = await yandex_route(home_coords, work_coords, mode="walking")
        return max(1, round(route.total_seconds / 60))
    except YandexAPIError:
        return WORK_FALLBACK_WALK_MINUTES


# =========================================================
# REPLY BUILDING
# =========================================================


def ceil_minutes(seconds: float) -> int:
    return max(0, int((seconds + 59) // 60))


def format_duration(seconds: float) -> str:
    minutes = ceil_minutes(seconds)
    if minutes < 60:
        return f"{minutes} мин"
    hours, mins = divmod(minutes, 60)
    if mins == 0:
        return f"{hours} ч"
    return f"{hours} ч {mins} мин"


def day_label(target: datetime, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(TZ)
    if target.date() == now.date():
        return "сегодня"
    if target.date() == (now + timedelta(days=1)).date():
        return "завтра"
    return target.strftime("%d.%m.%Y")


def alarm_time_from_leave(leave_time: datetime) -> datetime:
    return leave_time - timedelta(minutes=PREP_MINUTES + TIKTOK_MINUTES)


async def build_work_reply(target: datetime) -> str:
    walk_minutes = await calculate_work_walk_minutes()
    leave_time = target - timedelta(minutes=WORK_LEAVE_BEFORE_MINUTES)
    alarm_time = alarm_time_from_leave(leave_time)

    personal_buffer = max(
        0,
        WORK_LEAVE_BEFORE_MINUTES - ELEVATOR_AND_EXIT_MINUTES - walk_minutes,
    )

    return (
        f"🏢 Работа — {DESTINATIONS['work']['address']}\n"
        f"Нужно быть {day_label(target)} к {target.strftime('%H:%M')}.\n\n"
        f"⏰ Будильник: {alarm_time.strftime('%H:%M')}\n"
        f"📱 TikTok после будильника: {TIKTOK_MINUTES} мин\n"
        f"🧼 Сборы: {PREP_MINUTES} мин\n"
        f"🚪 Выйти из дома: {leave_time.strftime('%H:%M')}\n\n"
        f"Маршрут до работы:\n"
        f"• лифт + выход из дома: ~{ELEVATOR_AND_EXIT_MINUTES} мин\n"
        f"• пешком по маршруту: ~{walk_minutes} мин\n"
        f"• ваш личный ранний запас: ~{personal_buffer} мин\n\n"
        f"Для смены 09:30 это даёт ваш идеальный выход в 08:40."
    )


async def build_transit_reply(destination: Destination, target: datetime) -> str:
    plan = await calculate_transit_plan(destination, target)
    alarm_time = alarm_time_from_leave(plan.leave_time)
    now = datetime.now(TZ)

    late_note = ""
    if alarm_time <= now:
        late_note = (
            "\n⚠️ По этому расчёту время будильника уже прошло. "
            "Если едете сейчас, выходить лучше как можно скорее."
        )

    stop_note = f" ({destination.stop_hint})" if destination.stop_hint else ""

    return (
        f"📍 {destination.name}{stop_note}\n"
        f"Нужно быть {day_label(target)} к {target.strftime('%H:%M')}.\n\n"
        f"⏰ Будильник(я тебя могнул): {alarm_time.strftime('%H:%M')}\n"
        f"📱 TikTok(уебище): {TIKTOK_MINUTES} мин\n"
        f"🧼 Сборы: {PREP_MINUTES} мин\n"
        f"🚪 Выйти из дома: {plan.leave_time.strftime('%H:%M')}\n\n"
        f"🗺 Разбивка маршрута по Яндекс Картам:\n"
        f"• пешком до {HOME_STOP_NAME}: ~{format_duration(plan.walk_to_home_stop_seconds)}\n"
        f"• в общественном транспорте: ~{format_duration(plan.transit_seconds)}\n"
        f"• пешие переходы при пересадках: ~{format_duration(plan.transfer_walk_seconds)}\n"
        f"• пешком после транспорта до точки: ~{format_duration(plan.walk_after_seconds)}\n"
        f"• весь путь: ~{format_duration(plan.total_seconds)}\n"
        f"• расстояние по участкам: ~{plan.distance_meters / 1000:.1f} км"
        f"{late_note}"
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
            "Не понял место. Можно написать, например:\n"
            "• «прем к 15:00»\n"
            "• «виктория завтра к 18:30»\n"
            "• «почтовая к 14:00»\n"
            "• «на работу»\n"
            "• «адрес: Московское шоссе, 33 к 17:00»"
        )
        return

    if target is None:
        await update.message.reply_text(
            "Не вижу время прибытия. Напиши, например: «прем к 15:00»."
        )
        return

    now = datetime.now(TZ)
    if "сегодня" in normalize_text(text) and target <= now:
        await update.message.reply_text(
            "Это время сегодня уже прошло. Укажи другое время или напиши «завтра»."
        )
        return

    try:
        if destination.kind == "work":
            reply = await build_work_reply(target)
        else:
            reply = await build_transit_reply(destination, target)
    except YandexAPIError as exc:
        logger.exception("Yandex API error")
        await update.message.reply_text(
            "Не смог получить маршрут из Яндекс Карт.\n\n"
            f"Причина: {exc}\n\n"
            "Проверь переменные YANDEX_ROUTER_API_KEY и "
            "YANDEX_GEOCODER_API_KEY. Для маршрутов вне работы они обязательны."
        )
        return

    await update.message.reply_text(reply)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Напиши место и время, к которому нужно быть там — я посчитаю будильник, "
        "сборы и дорогу.\n\n"
        "Основные места:\n"
        "• «прем к 15:00» — ТЦ Премьер\n"
        "• «виктория к 18:00» — Виктория Плаза\n"
        "• «почтовая к 14:30» — Почтовая / Площадь Ленина\n"
        "• «на работу» — автоматически к 09:30\n\n"
        "Можно и любой точный адрес:\n"
        "«адрес: Московское шоссе, 33 к 17:00»"
    )


def main():
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬТЕ_СЮДА_ВАШ_ТОКЕН":
        raise RuntimeError(
            "Не задан BOT_TOKEN. Укажите его в переменной окружения BOT_TOKEN."
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()

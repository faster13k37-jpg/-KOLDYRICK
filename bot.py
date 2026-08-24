"""
Телеграм-бот "Во сколько вставать"
------------------------------------
Бот понимает сообщения вида:
    "мне надо на работу к 15:00"
    "к 9 утра на встречу"
    "в 18:30 быть на месте"

...и отвечает, во сколько нужно проснуться, во сколько выйти из дома,
на каком транспорте ехать и с какой остановки.

НАСТРОЙКА (обязательно поправьте под себя!)
--------------------------------------------
Всё, что нужно поменять, находится в блоке CONFIG ниже.
"""

import os
import re
import logging
from datetime import datetime, timedelta
from typing import Optional

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =========================================================
#                     CONFIG — ПОМЕНЯЙТЕ ПОД СЕБЯ
# =========================================================

# Токен вашего бота, который выдаёт @BotFather в Telegram.
# Лучше не писать его прямо в коде, а задать через переменную окружения
# BOT_TOKEN (так безопаснее и удобнее при деплое).
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8820034586:AAF0Cz7wf4pHY3IiS_oJ2lRK6dTD4aBkmZQ")

# Сколько минут вам обычно нужно, чтобы собраться после пробуждения
# (умыться, одеться, позавтракать и т.д.)
PREP_MINUTES = 90  # 12:30 -> 14:00 в вашем примере

# Сколько минут занимает дорога от дома до работы ЦЕЛИКОМ,
# включая дорогу до остановки и путь на транспорте.
TRAVEL_MINUTES = 55  # 14:00 -> примерно 14:55

# Небольшой запас на всякий случай (пробки, ожидание транспорта и т.д.)
BUFFER_MINUTES = 5  # итого 14:00 -> 15:00 в вашем примере

# Какой транспорт использовать
TRANSPORT = "ТЦК бусик №65,66,47,53,17 "  # например: "метро", "трамвай №5", "пешком"

# Ближайшая к вам остановка/станция
NEAREST_STOP = "остановка Дубовая роща"  # впишите реальное название

# =========================================================


TIME_PATTERN = re.compile(
    r"(?:к|в|на)\s*(\d{1,2})[:.]?(\d{2})?\s*(утра|дня|вечера|ночи)?",
    re.IGNORECASE,
)


def parse_target_time(text: str) -> Optional[datetime]:
    """Ищет в тексте время вида 'к 15:00', 'в 9', 'на 18.30 вечера' и т.п.
    Возвращает ближайший будущий datetime с этим временем, либо None."""
    match = TIME_PATTERN.search(text)
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) else 0
    period = (match.group(3) or "").lower()

    if period in ("вечера", "ночи") and hour < 12:
        hour += 12

    if hour > 23 or minute > 59:
        return None

    now = datetime.now()
    target = now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)

    # Если это время уже прошло сегодня — считаем, что имеется в виду завтра
    if target <= now:
        target += timedelta(days=1)

    return target


def build_schedule_reply(target: datetime) -> str:
    leave_time = target - timedelta(minutes=TRAVEL_MINUTES + BUFFER_MINUTES)
    wake_time = leave_time - timedelta(minutes=PREP_MINUTES)

    day_note = ""
    if target.date() != datetime.now().date():
        day_note = " (завтра)"

    return (
        f"Плюсь вайб, нужно быть на месте к {target.strftime('%H:%M')}{day_note}.\n\n"
        f"⏰ Проснуться и полежать злой: {wake_time.strftime('%H:%M')}\n"
        f"🚪 Выйти из дома: {leave_time.strftime('%H:%M')}\n"
        f"🚌 БУСИК ТЦК: {TRANSPORT}\n"
        f"📍 ОстановОчка: {NEAREST_STOP}"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    target = parse_target_time(text)

    if target is None:
        await update.message.reply_text(
            "Не понял блять. Напиши, например:\n"
            "«мне надо на блядки к 15:00»\n"
            "«в 9 утра на трассу»"
        )
        return

    await update.message.reply_text(build_schedule_reply(target))


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Уебище колдырное, напиши, к какому времени те нужно быть на месте,тварь блять — "
        "я посчитаю, во сколько вставать и выходить.\n\n Cпециальный помощник для слабоумой малютки Анастасии со следующими болезнями - СДВГ,Дезориентация.\n\n" 
        "Например: «мне надо на работу к 15:00»"
    )


def main():
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬТЕ_СЮДА_ВАШ_ТОКЕН":
        raise RuntimeError(
            "Не задан BOT_TOKEN. Укажите его в переменной окружения BOT_TOKEN "
            "или впишите прямо в код (см. блок CONFIG)."
        )

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()

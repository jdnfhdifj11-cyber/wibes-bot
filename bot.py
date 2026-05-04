"""
╔══════════════════════════════════════════════════════╗
║           WIBES ADMIN BOT — bot.py                  ║
║  Генерация кодов + уведомления о выводах            ║
╚══════════════════════════════════════════════════════╝

КОМАНДЫ:
  /gencode         — сгенерировать 1 код
  /gencode 10      — сгенерировать 10 кодов сразу
  /codes           — список всех активных кодов
  /used            — список использованных кодов
  /stats           — статистика
  /withdrawals     — последние запросы на вывод
  /help            — помощь
"""

import os
import json
import random
import string
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ══════════════════════════════════════
#   НАСТРОЙКИ — ЗАПОЛНИТЕ ЭТИ ПОЛЯ
# ══════════════════════════════════════

BOT_TOKEN = os.getenv('BOT_TOKEN', 'ВСТАВЬТЕ_ТОКЕН_СЮДА')
# Только вы сможете использовать бота
ADMIN_ID  = int(os.getenv('ADMIN_ID', '0'))  # ваш Telegram user ID

# Файл для хранения кодов (автоматически создаётся)
CODES_FILE = 'codes.json'

# ══════════════════════════════════════

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────
#  Работа с базой кодов (JSON файл)
# ──────────────────────────────────────

def load_codes() -> dict:
    """Загружает базу кодов из файла."""
    if os.path.exists(CODES_FILE):
        with open(CODES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        "active": [],      # коды, которые ещё не использованы
        "used": {},        # {код: {user_agent, date, ...}}
        "withdrawals": []  # запросы на вывод от пользователей
    }

def save_codes(data: dict):
    """Сохраняет базу кодов в файл."""
    with open(CODES_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def generate_code(length: int = 8) -> str:
    """Генерирует уникальный код: буквы + цифры."""
    chars = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(random.choices(chars, k=length))
        # Добавляем тире для читаемости: XXXX-XXXX
        code = f"{code[:4]}-{code[4:]}"
        db = load_codes()
        if code not in db['active'] and code not in db['used']:
            return code

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


# ──────────────────────────────────────
#  Команды бота
# ──────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    await update.message.reply_text(
        "🤖 <b>Wibes Admin Bot</b>\n\n"
        "Команды:\n"
        "/gencode — сгенерировать 1 код\n"
        "/gencode 5 — сгенерировать 5 кодов\n"
        "/codes — все активные коды\n"
        "/used — использованные коды\n"
        "/withdrawals — запросы на вывод\n"
        "/stats — статистика\n",
        parse_mode='HTML'
    )


async def cmd_gencode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Генерирует один или несколько кодов доступа."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return

    # Определяем количество кодов
    count = 1
    if ctx.args:
        try:
            count = max(1, min(int(ctx.args[0]), 50))  # максимум 50 за раз
        except ValueError:
            count = 1

    db = load_codes()
    new_codes = []

    for _ in range(count):
        code = generate_code()
        db['active'].append(code)
        new_codes.append(code)

    save_codes(db)

    # Форматируем ответ
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    if count == 1:
        text = (
            f"✅ <b>Новый код создан</b>\n\n"
            f"🔑 <code>{new_codes[0]}</code>\n\n"
            f"📋 Скопируйте и отправьте пользователю.\n"
            f"⏰ Создан: {now}"
        )
    else:
        codes_list = '\n'.join([f"  🔑 <code>{c}</code>" for c in new_codes])
        text = (
            f"✅ <b>Создано {count} кодов:</b>\n\n"
            f"{codes_list}\n\n"
            f"⏰ Создан: {now}\n"
            f"📊 Активных кодов: {len(db['active'])}"
        )

    await update.message.reply_text(text, parse_mode='HTML')


async def cmd_codes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показывает все активные (неиспользованные) коды."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return

    db = load_codes()
    active = db.get('active', [])

    if not active:
        await update.message.reply_text("📭 Активных кодов нет.\n\nИспользуйте /gencode чтобы создать новые.")
        return

    codes_list = '\n'.join([f"  <code>{c}</code>" for c in active])
    text = (
        f"🔑 <b>Активные коды ({len(active)}):</b>\n\n"
        f"{codes_list}\n\n"
        f"💡 Эти коды ещё не использованы."
    )
    await update.message.reply_text(text, parse_mode='HTML')


async def cmd_used(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показывает использованные коды."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return

    db = load_codes()
    used = db.get('used', {})

    if not used:
        await update.message.reply_text("📭 Использованных кодов нет.")
        return

    items = []
    for code, info in list(used.items())[-20:]:  # последние 20
        date = info.get('date', '?')
        lang = info.get('lang', '?')
        items.append(f"  ✅ <code>{code}</code> — {date} ({lang})")

    text = (
        f"📋 <b>Использованные коды ({len(used)}):</b>\n\n"
        + '\n'.join(items)
    )
    if len(used) > 20:
        text += f"\n\n...и ещё {len(used) - 20}"

    await update.message.reply_text(text, parse_mode='HTML')


async def cmd_withdrawals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показывает последние запросы на вывод."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return

    db = load_codes()
    wds = db.get('withdrawals', [])

    if not wds:
        await update.message.reply_text("📭 Запросов на вывод ещё нет.")
        return

    items = []
    for w in wds[-15:]:  # последние 15
        items.append(
            f"👤 <b>{w.get('user','?')}</b>\n"
            f"   💰 {w.get('amount','?')} USDT · {w.get('network','?')}\n"
            f"   📍 <code>{w.get('address','?')}</code>\n"
            f"   📅 {w.get('date','?')}"
        )

    text = f"💸 <b>Запросы на вывод ({len(wds)}):</b>\n\n" + '\n\n'.join(items)
    await update.message.reply_text(text, parse_mode='HTML')


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показывает общую статистику."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return

    db = load_codes()
    active_count = len(db.get('active', []))
    used_count = len(db.get('used', {}))
    wd_count = len(db.get('withdrawals', []))

    # Сумма выводов
    total_wd = sum(float(w.get('amount', 0)) for w in db.get('withdrawals', []))

    text = (
        f"📊 <b>Статистика Wibes</b>\n\n"
        f"🔑 Активных кодов: <b>{active_count}</b>\n"
        f"✅ Использовано кодов: <b>{used_count}</b>\n"
        f"👥 Всего пользователей: <b>{used_count}</b>\n\n"
        f"💸 Запросов на вывод: <b>{wd_count}</b>\n"
        f"💰 Сумма всех выводов: <b>{total_wd:.2f} USDT</b>"
    )
    await update.message.reply_text(text, parse_mode='HTML')


# ──────────────────────────────────────
#  Webhook endpoint для сайта
#  (принимает данные о выводах и использованных кодах)
# ──────────────────────────────────────

async def handle_webhook_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает входящие сообщения (от сайта через webhook)."""
    # Этот хендлер для сообщений от пользователей не нужен,
    # уведомления о выводах приходят напрямую через Telegram Bot API из JS
    pass


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await cmd_start(update, ctx)


# ──────────────────────────────────────
#  Запуск бота
# ──────────────────────────────────────

def main():
    if BOT_TOKEN == 'ВСТАВЬТЕ_ТОКЕН_СЮДА' or ADMIN_ID == 0:
        print("❌ Заполните BOT_TOKEN и ADMIN_ID в bot.py или в переменных окружения!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("gencode",     cmd_gencode))
    app.add_handler(CommandHandler("codes",       cmd_codes))
    app.add_handler(CommandHandler("used",        cmd_used))
    app.add_handler(CommandHandler("withdrawals", cmd_withdrawals))
    app.add_handler(CommandHandler("stats",       cmd_stats))

    print("🤖 Wibes Bot запущен!")
    print(f"👤 Admin ID: {ADMIN_ID}")
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()

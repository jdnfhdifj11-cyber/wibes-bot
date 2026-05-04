"""
WIBES ADMIN BOT
Telegram бот + HTTP API + автоматическое зачисление USDT через TronGrid
"""

import os, json, random, string, logging, threading, time, asyncio
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# ══════════════════════════════════════
#   НАСТРОЙКИ
# ══════════════════════════════════════

BOT_TOKEN      = os.getenv('BOT_TOKEN', '')
ADMIN_ID       = int(os.getenv('ADMIN_ID', '0'))
PORT           = int(os.getenv('PORT', 8080))
TRONGRID_KEY   = '004500f1-188c-48e6-9d6c-e6789f9463cf'
USDT_ADDRESS   = 'TCXfpgseqD2oNkRTh1vp7PiQugvErRg2BP'
USDT_CONTRACT  = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'  # USDT TRC-20 контракт
CODES_FILE     = 'codes.json'
CHECK_INTERVAL = 30  # проверять каждые 30 секунд

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ── База данных ──

def load_db():
    if os.path.exists(CODES_FILE):
        with open(CODES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        "active": [],
        "used": {},
        "balances": {},      # {код: баланс_в_usdt}
        "withdrawals": [],
        "transactions": [],  # уже обработанные txID
        "last_tx_time": 0    # время последней проверенной транзакции
    }

def save_db(data):
    with open(CODES_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def generate_code():
    chars = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(random.choices(chars, k=8))
        db = load_db()
        if code not in db['active'] and code not in db['used']:
            return code

def is_admin(uid): return uid == ADMIN_ID

# ── TronGrid: получить транзакции USDT на наш адрес ──

def get_usdt_transactions(min_timestamp=0):
    """Получает входящие USDT транзакции через TronGrid API."""
    try:
        url = (
            f"https://api.trongrid.io/v1/accounts/{USDT_ADDRESS}"
            f"/transactions/trc20"
            f"?contract_address={USDT_CONTRACT}"
            f"&only_to=true"
            f"&limit=20"
            f"&min_timestamp={min_timestamp}"
        )
        req = urllib.request.Request(url, headers={
            'TRON-PRO-API-KEY': TRONGRID_KEY,
            'Accept': 'application/json'
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get('data', [])
    except Exception as e:
        logger.error(f'TronGrid error: {e}')
        return []

def find_user_by_amount(amount_usdt, db):
    """
    Ищет пользователя который должен был заплатить эту сумму.
    Логика: смотрим pending_payments — пользователь нажал кнопку задания
    и система ждёт его оплату.
    """
    pending = db.get('pending_payments', {})
    for code, payment in pending.items():
        if abs(payment['amount'] - amount_usdt) < 0.01:  # допуск 0.01 USDT
            return code, payment
    return None, None

# ── Мониторинг транзакций ──

async def monitor_transactions(bot: Bot):
    """Фоновая задача: каждые 30 сек проверяет новые USDT транзакции."""
    logger.info('🔍 Мониторинг транзакций запущен')

    while True:
        try:
            db = load_db()
            last_time = db.get('last_tx_time', 0)
            processed = db.get('transactions', [])

            # Получаем транзакции за последние N секунд
            min_ts = last_time if last_time else int(time.time() * 1000) - 3600000

            txs = get_usdt_transactions(min_ts)

            for tx in txs:
                tx_id = tx.get('transaction_id', '')
                if tx_id in processed:
                    continue

                # Сумма в USDT (делим на 10^6)
                value = int(tx.get('value', 0))
                amount_usdt = value / 1_000_000

                if amount_usdt < 1:
                    continue

                tx_time = int(tx.get('block_timestamp', 0))
                from_addr = tx.get('from', '?')

                logger.info(f'Новая транзакция: {amount_usdt} USDT от {from_addr}')

                # Ищем пользователя по сумме
                code, payment = find_user_by_amount(amount_usdt, db)

                if code:
                    # Зачисляем баланс
                    if 'balances' not in db:
                        db['balances'] = {}
                    db['balances'][code] = round(
                        db['balances'].get(code, 0) + amount_usdt, 2
                    )

                    # Убираем из ожидающих
                    if code in db.get('pending_payments', {}):
                        del db['pending_payments'][code]

                    # Сохраняем транзакцию
                    processed.append(tx_id)
                    db['transactions'] = processed
                    db['last_tx_time'] = max(last_time, tx_time)
                    save_db(db)

                    # Уведомляем админа
                    await bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            f"✅ <b>Баланс пополнен автоматически</b>\n\n"
                            f"👤 Пользователь: <code>{code}</code>\n"
                            f"💰 Сумма: <b>{amount_usdt:.2f} USDT</b>\n"
                            f"📍 От: <code>{from_addr}</code>\n"
                            f"🔗 TX: <code>{tx_id[:20]}...</code>"
                        ),
                        parse_mode='HTML'
                    )
                    logger.info(f'✅ Зачислено {amount_usdt} USDT пользователю {code}')
                else:
                    # Транзакция не найдена в ожидающих — уведомляем админа
                    processed.append(tx_id)
                    db['transactions'] = processed
                    db['last_tx_time'] = max(last_time, tx_time)
                    save_db(db)

                    await bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            f"⚠️ <b>Неопознанная транзакция</b>\n\n"
                            f"💰 Сумма: <b>{amount_usdt:.2f} USDT</b>\n"
                            f"📍 От: <code>{from_addr}</code>\n"
                            f"🔗 TX: <code>{tx_id[:20]}...</code>\n\n"
                            f"Используйте /addbalance КОД {amount_usdt:.2f} для зачисления вручную."
                        ),
                        parse_mode='HTML'
                    )

        except Exception as e:
            logger.error(f'Monitor error: {e}')

        await asyncio.sleep(CHECK_INTERVAL)

# ── HTTP API ──

class APIHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send_json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path)
        q = parse_qs(p.query)

        # Проверить код
        if p.path == '/check':
            code = q.get('code', [''])[0].strip().upper()
            db = load_db()
            if code in db['used']:
                self.send_json(200, {'valid': False, 'reason': 'used'})
            elif code in db['active']:
                self.send_json(200, {'valid': True})
            else:
                self.send_json(200, {'valid': False, 'reason': 'invalid'})
            return

        # Использовать код (вход на сайт)
        if p.path == '/use':
            code = q.get('code', [''])[0].strip().upper()
            lang = q.get('lang', ['en'])[0]
            db = load_db()
            if code not in db['active']:
                self.send_json(200, {'ok': False})
                return
            db['active'].remove(code)
            db['used'][code] = {'date': datetime.now().strftime('%d.%m.%Y %H:%M'), 'lang': lang}
            if 'balances' not in db:
                db['balances'] = {}
            db['balances'].setdefault(code, 0)
            save_db(db)
            self.send_json(200, {'ok': True})
            return

        # Получить баланс пользователя
        if p.path == '/balance':
            code = q.get('code', [''])[0].strip().upper()
            db = load_db()
            balance = db.get('balances', {}).get(code, 0)
            self.send_json(200, {'balance': balance})
            return

        # Обновить баланс после покупки задания (списание)
        if p.path == '/deduct':
            code   = q.get('code',   [''])[0].strip().upper()
            amount = float(q.get('amount', ['0'])[0])
            db = load_db()
            current = db.get('balances', {}).get(code, 0)
            if current < amount:
                self.send_json(200, {'ok': False, 'reason': 'insufficient'})
                return
            db['balances'][code] = round(current - amount, 2)
            save_db(db)
            self.send_json(200, {'ok': True, 'balance': db['balances'][code]})
            return

        # Зарегистрировать ожидаемый платёж
        if p.path == '/expect':
            code   = q.get('code',   [''])[0].strip().upper()
            amount = float(q.get('amount', ['0'])[0])
            db = load_db()
            if 'pending_payments' not in db:
                db['pending_payments'] = {}
            db['pending_payments'][code] = {
                'amount': amount,
                'time': int(time.time())
            }
            save_db(db)
            self.send_json(200, {'ok': True, 'address': USDT_ADDRESS})
            return

        # Запрос на вывод — отправляем уведомление с кнопками
        if p.path == '/withdrawal':
            code    = q.get('code',    ['?'])[0]
            amount  = float(q.get('amount',  ['0'])[0])
            address = q.get('address', ['?'])[0]
            network = q.get('network', ['TRX TRC-20'])[0]
            now_str = datetime.now().strftime('%d.%m.%Y %H:%M')

            db = load_db()
            wd_id = len(db.get('withdrawals', []))

            # Проверяем не менялся ли адрес (берём предыдущий адрес этого юзера)
            prev_withdrawals = [w for w in db.get('withdrawals', []) if w.get('user') == code]
            prev_addr = prev_withdrawals[-1].get('address', '') if prev_withdrawals else ''
            addr_changed = prev_addr and prev_addr != address

            db.setdefault('withdrawals', []).append({
                'id': wd_id, 'user': code, 'amount': amount,
                'address': address, 'network': network,
                'date': now_str, 'status': 'pending'
            })
            save_db(db)

            # Формируем сообщение с кнопками
            addr_display = f'<s>{address}</s> ⚠️ АДРЕС ИЗМЕНЁН!' if addr_changed else f'<code>{address}</code>'
            msg = (
                f"💸 <b>ЗАПРОС НА ВЫВОД #{wd_id}</b>\n\n"
                f"👤 Пользователь: <code>{code}</code>\n"
                f"💰 Сумма: <b>{amount:.2f} USDT</b>\n"
                f"🌐 Сеть: <b>{network}</b>\n"
                f"📬 Адрес: {addr_display}\n"
                f"📅 {now_str}"
            )

            # Inline кнопки
            keyboard = {
                'inline_keyboard': [[
                    {'text': f'✅ Выплатить {amount:.2f} USDT', 'callback_data': f'pay_{wd_id}'},
                    {'text': '❌ Отклонить', 'callback_data': f'reject_{wd_id}'}
                ]]
            }

            # Отправляем через Telegram Bot API
            try:
                tg_url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
                payload = json.dumps({
                    'chat_id': ADMIN_ID,
                    'text': msg,
                    'parse_mode': 'HTML',
                    'reply_markup': keyboard
                }).encode()
                req = urllib.request.Request(tg_url, data=payload,
                    headers={'Content-Type': 'application/json'}, method='POST')
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                logger.error(f'TG send error: {e}')

            self.send_json(200, {'ok': True})
            return

        self.send_json(404, {'error': 'not found'})

    def do_POST(self): self.do_GET()

def run_server():
    HTTPServer(('0.0.0.0', PORT), APIHandler).serve_forever()

# ── Команды бота ──

async def cmd_start(u: Update, _):
    if not is_admin(u.effective_user.id):
        await u.message.reply_text("⛔ Доступ запрещён.")
        return
    await u.message.reply_text(
        "🤖 <b>Wibes Admin Bot</b>\n\n"
        "/gencode — создать 1 код\n"
        "/gencode 5 — создать 5 кодов\n"
        "/codes — активные коды\n"
        "/used — использованные коды\n"
        "/balances — балансы пользователей\n"
        "/addbalance КОД СУММА — зачислить вручную\n"
        "/withdrawals — запросы вывода\n"
        "/stats — статистика\n"
        "/pending — ожидающие платежи",
        parse_mode='HTML'
    )

async def cmd_gencode(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    count = 1
    if ctx.args:
        try: count = max(1, min(int(ctx.args[0]), 50))
        except: pass
    db = load_db()
    codes = []
    for _ in range(count):
        code = generate_code()
        db['active'].append(code)
        codes.append(code)
    save_db(db)
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    if count == 1:
        text = f"✅ <b>Новый код создан</b>\n\n🔑 <code>{codes[0]}</code>\n\n📋 Скопируйте и отправьте пользователю.\n⏰ {now}"
    else:
        text = f"✅ <b>Создано {count} кодов:</b>\n\n" + '\n'.join([f"🔑 <code>{c}</code>" for c in codes]) + f"\n\n⏰ {now}"
    await u.message.reply_text(text, parse_mode='HTML')

async def cmd_addbalance(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Вручную зачислить баланс: /addbalance КОД СУММА"""
    if not is_admin(u.effective_user.id): return
    if not ctx.args or len(ctx.args) < 2:
        await u.message.reply_text("Использование: /addbalance КОД СУММА\nПример: /addbalance ABC12345 10")
        return
    code = ctx.args[0].strip().upper()
    try:
        amount = float(ctx.args[1])
    except:
        await u.message.reply_text("❌ Неверная сумма")
        return
    db = load_db()
    if 'balances' not in db:
        db['balances'] = {}
    db['balances'][code] = round(db['balances'].get(code, 0) + amount, 2)
    save_db(db)
    await u.message.reply_text(
        f"✅ Зачислено <b>{amount} USDT</b> пользователю <code>{code}</code>\n"
        f"💰 Новый баланс: <b>{db['balances'][code]} USDT</b>",
        parse_mode='HTML'
    )

async def cmd_balances(u: Update, _):
    if not is_admin(u.effective_user.id): return
    db = load_db()
    balances = db.get('balances', {})
    if not balances:
        await u.message.reply_text("📭 Нет пользователей с балансом.")
        return
    items = [f"<code>{c}</code>: <b>{b} USDT</b>" for c, b in sorted(balances.items(), key=lambda x: -x[1])[:20]]
    await u.message.reply_text(f"💰 <b>Балансы ({len(balances)}):</b>\n\n" + '\n'.join(items), parse_mode='HTML')

async def cmd_pending(u: Update, _):
    if not is_admin(u.effective_user.id): return
    db = load_db()
    pending = db.get('pending_payments', {})
    if not pending:
        await u.message.reply_text("📭 Нет ожидающих платежей.")
        return
    items = [f"<code>{c}</code>: <b>{p['amount']} USDT</b>" for c, p in pending.items()]
    await u.message.reply_text(f"⏳ <b>Ожидают оплаты ({len(pending)}):</b>\n\n" + '\n'.join(items), parse_mode='HTML')

async def cmd_codes(u: Update, _):
    if not is_admin(u.effective_user.id): return
    db = load_db()
    active = db.get('active', [])
    if not active:
        await u.message.reply_text("📭 Нет активных кодов. /gencode")
        return
    await u.message.reply_text(
        f"🔑 <b>Активные коды ({len(active)}):</b>\n\n" + '\n'.join([f"<code>{c}</code>" for c in active]),
        parse_mode='HTML'
    )

async def cmd_used(u: Update, _):
    if not is_admin(u.effective_user.id): return
    db = load_db()
    used = db.get('used', {})
    if not used:
        await u.message.reply_text("📭 Использованных кодов нет.")
        return
    items = [f"✅ <code>{c}</code> — {i.get('date','?')}" for c,i in list(used.items())[-20:]]
    await u.message.reply_text(f"📋 <b>Использованные ({len(used)}):</b>\n\n" + '\n'.join(items), parse_mode='HTML')

async def cmd_withdrawals(u: Update, _):
    if not is_admin(u.effective_user.id): return
    db = load_db()
    wds = db.get('withdrawals', [])
    if not wds:
        await u.message.reply_text("📭 Запросов на вывод нет.")
        return
    items = [f"👤 <b>{w.get('user','?')}</b>\n💰 {w.get('amount','?')} USDT · {w.get('network','?')}\n📍 <code>{w.get('address','?')}</code>\n📅 {w.get('date','?')}" for w in wds[-15:]]
    await u.message.reply_text(f"💸 <b>Запросы на вывод ({len(wds)}):</b>\n\n" + '\n\n'.join(items), parse_mode='HTML')

async def cmd_stats(u: Update, _):
    if not is_admin(u.effective_user.id): return
    db = load_db()
    total = sum(float(w.get('amount',0)) for w in db.get('withdrawals',[]))
    total_bal = sum(db.get('balances',{}).values())
    await u.message.reply_text(
        f"📊 <b>Статистика</b>\n\n"
        f"🔑 Активных кодов: <b>{len(db.get('active',[]))}</b>\n"
        f"✅ Пользователей: <b>{len(db.get('used',{}))}</b>\n"
        f"💰 Суммарный баланс: <b>{total_bal:.2f} USDT</b>\n"
        f"💸 Выводов: <b>{len(db.get('withdrawals',[]))}</b>\n"
        f"💵 Выведено: <b>{total:.2f} USDT</b>",
        parse_mode='HTML'
    )

async def cmd_wallet(u: Update, _):
    """Показывает баланс вашего USDT кошелька через TronGrid."""
    if not is_admin(u.effective_user.id): return
    try:
        url = f'https://api.trongrid.io/v1/accounts/{USDT_ADDRESS}'
        req = urllib.request.Request(url, headers={'TRON-PRO-API-KEY': TRONGRID_KEY, 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            account = data.get('data', [{}])[0]

            # TRX баланс
            trx_balance = account.get('balance', 0) / 1_000_000

            # USDT баланс
            usdt_balance = 0
            for token in account.get('trc20', []):
                if USDT_CONTRACT in token:
                    usdt_balance = int(token[USDT_CONTRACT]) / 1_000_000
                    break

            await u.message.reply_text(
                f"💼 <b>Баланс кошелька</b>\n\n"
                f"📍 <code>{USDT_ADDRESS}</code>\n\n"
                f"💵 USDT TRC-20: <b>{usdt_balance:.2f} USDT</b>\n"
                f"⚡ TRX: <b>{trx_balance:.2f} TRX</b>",
                parse_mode='HTML'
            )
    except Exception as e:
        await u.message.reply_text(f"❌ Ошибка получения баланса: {e}")

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обработка кнопок Выплатить / Отклонить."""
    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        return

    data = query.data
    db = load_db()

    if data.startswith('pay_'):
        wd_id = int(data.split('_')[1])
        wds = db.get('withdrawals', [])
        wd = next((w for w in wds if w.get('id') == wd_id), None)
        if not wd:
            await query.edit_message_text("❌ Запрос не найден.")
            return

        address = wd.get('address', '')
        amount  = wd.get('amount', 0)
        code    = wd.get('user', '?')

        # Обновляем статус
        wd['status'] = 'paid'
        save_db(db)

        await query.edit_message_text(
            f"✅ <b>Выплата подтверждена</b>\n\n"
            f"👤 {code}\n"
            f"💰 {amount:.2f} USDT\n"
            f"📬 <code>{address}</code>\n\n"
            f"⚡ Отправьте средства вручную через ваш кошелёк.\n"
            f"📋 Адрес скопирован выше.",
            parse_mode='HTML'
        )

    elif data.startswith('reject_'):
        wd_id = int(data.split('_')[1])
        wds = db.get('withdrawals', [])
        wd = next((w for w in wds if w.get('id') == wd_id), None)
        if wd:
            wd['status'] = 'rejected'
            # Возвращаем баланс пользователю
            code = wd.get('user', '')
            amount = wd.get('amount', 0)
            if code:
                db['balances'][code] = round(db.get('balances', {}).get(code, 0) + amount, 2)
            save_db(db)

        await query.edit_message_text(
            f"❌ <b>Выплата отклонена</b>\n"
            f"💰 Баланс пользователя восстановлен.",
            parse_mode='HTML'
        )

# ── Запуск ──

def main():
    if not BOT_TOKEN or not ADMIN_ID:
        print("❌ Заполните BOT_TOKEN и ADMIN_ID!")
        return

    from telegram.ext import CallbackQueryHandler

    # HTTP сервер в отдельном потоке
    threading.Thread(target=run_server, daemon=True).start()

    # Telegram бот
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_start))
    app.add_handler(CommandHandler("gencode",     cmd_gencode))
    app.add_handler(CommandHandler("codes",       cmd_codes))
    app.add_handler(CommandHandler("used",        cmd_used))
    app.add_handler(CommandHandler("balances",    cmd_balances))
    app.add_handler(CommandHandler("addbalance",  cmd_addbalance))
    app.add_handler(CommandHandler("pending",     cmd_pending))
    app.add_handler(CommandHandler("withdrawals", cmd_withdrawals))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("wallet",      cmd_wallet))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Запускаем мониторинг транзакций
    async def post_init(application):
        asyncio.create_task(monitor_transactions(application.bot))

    app.post_init = post_init

    print(f"🤖 Wibes Bot запущен! API на порту {PORT}")
    print(f"🔍 Мониторинг адреса: {USDT_ADDRESS}")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()

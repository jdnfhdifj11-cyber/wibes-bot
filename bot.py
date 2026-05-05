"""
WIBES ADMIN BOT
- Уникальный TRX адрес для каждого пользователя
- Автоматическое зачисление USDT на баланс сайта
- Автоматический перевод USDT на главный кошелёк
- Telegram уведомления с кнопками
"""

import os, json, random, string, logging, threading, time, asyncio, secrets
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# tronpy для работы с блокчейном Tron
try:
    from tronpy import Tron
    from tronpy.keys import PrivateKey
    from tronpy.providers import HTTPProvider
    TRONPY_OK = True
except ImportError:
    TRONPY_OK = False
    logging.warning('tronpy не установлен — блокчейн функции отключены')

# ══════════════════════════════════════
#   НАСТРОЙКИ
# ══════════════════════════════════════

BOT_TOKEN          = os.getenv('BOT_TOKEN', '')
ADMIN_ID           = int(os.getenv('ADMIN_ID', '0'))
PORT               = int(os.getenv('PORT', 8080))
TRONGRID_KEY       = '004500f1-188c-48e6-9d6c-e6789f9463cf'
MAIN_ADDRESS       = 'TSw9PuQCTp3kHcPVni2uKo8c5PJiHvxupp'  # новый TronLink кошелёк
MAIN_PRIVATE_KEY   = os.getenv('MAIN_PRIVATE_KEY', '')      # приватный ключ нового TronLink кошелька
USDT_CONTRACT      = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'
CODES_FILE         = 'codes.json'
CHECK_INTERVAL     = 30
TRX_FOR_FEE        = 10  # TRX отправляем на каждый новый адрес для комиссий

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Tron клиент
try:
    tron_client = Tron(HTTPProvider(api_key=TRONGRID_KEY)) if TRONPY_OK else None
except Exception as e:
    tron_client = None
    logging.error(f'Tron client error: {e}')

# ── БД ──

def load_db():
    if os.path.exists(CODES_FILE):
        with open(CODES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        "active": [],
        "used": {},
        "balances": {},
        "wallets": {},      # {код: {address, private_key}}
        "withdrawals": [],
        "transactions": [], # обработанные tx_id
        "progress": {}
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

# ── ГЕНЕРАЦИЯ КОШЕЛЬКА ──

def create_wallet():
    """Генерирует новый TRX кошелёк."""
    if not TRONPY_OK:
        # Fallback — генерируем случайный адрес (только для тестов)
        import hashlib
        raw = secrets.token_bytes(32)
        addr = 'T' + hashlib.sha256(raw).hexdigest()[:33].upper()
        return addr, raw.hex()
    priv = PrivateKey(secrets.token_bytes(32))
    address = priv.public_key.to_base58check_address()
    private_key = priv.hex()
    return address, private_key

def get_or_create_wallet(code):
    """Возвращает кошелёк пользователя, создаёт если нет."""
    db = load_db()
    wallets = db.get('wallets', {})
    if code in wallets:
        return wallets[code]['address'], wallets[code]['private_key']
    # Создаём новый кошелёк
    address, private_key = create_wallet()
    db.setdefault('wallets', {})[code] = {
        'address': address,
        'private_key': private_key,
        'created': datetime.now().strftime('%d.%m.%Y %H:%M'),
        'trx_sent': False
    }
    save_db(db)
    logger.info(f'Создан кошелёк для {code}: {address}')
    # Отправляем TRX для комиссий в отдельном потоке
    def send_fee_task(addr=address, c=code):
        time.sleep(3)
        success = send_trx_for_fee(addr)
        if success:
            db2 = load_db()
            if c in db2.get('wallets', {}):
                db2['wallets'][c]['trx_sent'] = True
                save_db(db2)
            logger.info(f'TRX для комиссий отправлен на {addr}')
        else:
            logger.error(f'Не удалось отправить TRX на {addr}')
    threading.Thread(target=send_fee_task, daemon=True).start()
    return address, private_key

# ── TRON API ──

def get_usdt_balance(address):
    """Получает баланс USDT TRC-20 на адресе."""
    try:
        url = f'https://api.trongrid.io/v1/accounts/{address}'
        req = urllib.request.Request(url, headers={
            'TRON-PRO-API-KEY': TRONGRID_KEY,
            'Accept': 'application/json'
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            acc = data.get('data', [{}])[0]
            for token in acc.get('trc20', []):
                if USDT_CONTRACT in token:
                    return int(token[USDT_CONTRACT]) / 1_000_000
            return 0
    except Exception as e:
        logger.error(f'Balance check error for {address}: {e}')
        return 0

def get_trx_balance(address):
    """Получает баланс TRX (нужен для комиссии)."""
    try:
        url = f'https://api.trongrid.io/v1/accounts/{address}'
        req = urllib.request.Request(url, headers={
            'TRON-PRO-API-KEY': TRONGRID_KEY,
            'Accept': 'application/json'
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            acc = data.get('data', [{}])[0]
            return acc.get('balance', 0) / 1_000_000
    except:
        return 0

def get_transactions(address, min_timestamp=0):
    """Получает входящие USDT транзакции на адрес."""
    try:
        url = (
            f'https://api.trongrid.io/v1/accounts/{address}/transactions/trc20'
            f'?contract_address={USDT_CONTRACT}&only_to=true&limit=10'
            f'&min_timestamp={min_timestamp}'
        )
        req = urllib.request.Request(url, headers={
            'TRON-PRO-API-KEY': TRONGRID_KEY,
            'Accept': 'application/json'
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode()).get('data', [])
    except Exception as e:
        logger.error(f'Tx fetch error: {e}')
        return []

def send_trx_for_fee(to_address):
    """Отправляет TRX на новый кошелёк для оплаты комиссий."""
    if not TRONPY_OK or not tron_client:
        logger.warning('tronpy недоступен — пропускаем отправку TRX')
        return False
    if not MAIN_PRIVATE_KEY:
        logger.warning('MAIN_PRIVATE_KEY не задан')
        return False
    try:
        priv = PrivateKey(bytes.fromhex(MAIN_PRIVATE_KEY))
        sender = priv.public_key.to_base58check_address()
        amount_sun = TRX_FOR_FEE * 1_000_000
        txn = (
            tron_client.trx.transfer(sender, to_address, amount_sun)
            .build().sign(priv).broadcast()
        )
        result = txn.wait()
        logger.info(f'Отправлено {TRX_FOR_FEE} TRX на {to_address}: {result}')
        return True
    except Exception as e:
        logger.error(f'Ошибка отправки TRX на {to_address}: {e}')
        return False

def forward_usdt(private_key_hex, from_address, amount_usdt):
    """Пересылает USDT с временного кошелька на главный."""
    if not TRONPY_OK or not tron_client:
        logger.warning('tronpy недоступен — пересылка USDT невозможна')
        return False
    try:
        priv = PrivateKey(bytes.fromhex(private_key_hex))
        contract = tron_client.get_contract(USDT_CONTRACT)
        send_amount = int((amount_usdt - 0.1) * 1_000_000)
        if send_amount <= 0:
            return False
        txn = (
            contract.functions.transfer(MAIN_ADDRESS, send_amount)
            .with_owner(from_address)
            .fee_limit(20_000_000)
            .build().sign(priv).broadcast()
        )
        result = txn.wait()
        logger.info(f'Forward {amount_usdt} USDT from {from_address}: {result}')
        return True
    except Exception as e:
        logger.error(f'Forward error: {e}')
        return False

def get_main_wallet_balance():
    """Баланс основного кошелька."""
    try:
        url = f'https://api.trongrid.io/v1/accounts/{MAIN_ADDRESS}'
        req = urllib.request.Request(url, headers={
            'TRON-PRO-API-KEY': TRONGRID_KEY,
            'Accept': 'application/json'
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            acc = data.get('data', [{}])[0]
            trx = acc.get('balance', 0) / 1_000_000
            usdt = 0
            for token in acc.get('trc20', []):
                if USDT_CONTRACT in token:
                    usdt = int(token[USDT_CONTRACT]) / 1_000_000
                    break
            return trx, usdt
    except Exception as e:
        logger.error(f'Main wallet balance error: {e}')
        return None, None

# ── МОНИТОРИНГ ──

async def monitor_wallets(bot):
    """Проверяет все кошельки пользователей каждые 30 секунд."""
    logger.info('🔍 Мониторинг кошельков запущен')
    loop = asyncio.get_event_loop()
    while True:
        try:
            db = load_db()
            wallets = db.get('wallets', {})
            processed = db.get('transactions', [])
            min_ts = int(time.time() * 1000) - 3600000

            for code, wallet_info in wallets.items():
                address = wallet_info['address']
                private_key = wallet_info['private_key']

                # Run blocking call in thread pool so bot stays responsive
                txs = await loop.run_in_executor(None, get_transactions, address, min_ts)

                for tx in txs:
                    tx_id = tx.get('transaction_id', '')
                    if tx_id in processed:
                        continue

                    amount_usdt = int(tx.get('value', 0)) / 1_000_000
                    if amount_usdt < 0.5:
                        continue

                    tx_time = int(tx.get('block_timestamp', 0))
                    logger.info(f'Новая транзакция для {code}: +{amount_usdt} USDT')

                    db.setdefault('balances', {})[code] = round(
                        db['balances'].get(code, 0) + amount_usdt, 2
                    )
                    processed.append(tx_id)
                    db['transactions'] = processed

                    if code in db.get('progress', {}):
                        db['progress'][code]['bal'] = db['balances'][code]

                    save_db(db)

                    # Уведомление админу
                    await bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            f"✅ <b>Баланс пополнен автоматически</b>\n\n"
                            f"👤 Пользователь: <code>{code}</code>\n"
                            f"💰 Зачислено: <b>{amount_usdt:.2f} USDT</b>\n"
                            f"📍 Адрес: <code>{address}</code>\n"
                            f"🔗 TX: <code>{tx_id[:20]}...</code>\n\n"
                            f"⚡ Пересылаю на ваш кошелёк..."
                        ),
                        parse_mode='HTML'
                    )

                    # Пересылаем USDT на главный кошелёк в отдельном потоке
                    def forward_task(pk=private_key, addr=address, amt=amount_usdt, c=code):
                        time.sleep(5)  # ждём подтверждения
                        success = forward_usdt(pk, addr, amt)
                        if success:
                            logger.info(f'USDT успешно переслан от {addr}')
                        else:
                            logger.error(f'Ошибка пересылки от {addr}')
                    threading.Thread(target=forward_task, daemon=True).start()

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

        # Использовать код — при входе создаём кошелёк
        if p.path == '/use':
            code = q.get('code', [''])[0].strip().upper()
            lang = q.get('lang', ['en'])[0]
            db = load_db()
            if code not in db['active']:
                self.send_json(200, {'ok': False}); return
            db['active'].remove(code)
            db['used'][code] = {'date': datetime.now().strftime('%d.%m.%Y %H:%M'), 'lang': lang}
            db.setdefault('balances', {}).setdefault(code, 0)
            save_db(db)
            # Создаём кошелёк для пользователя
            address, _ = get_or_create_wallet(code)
            self.send_json(200, {'ok': True, 'deposit_address': address})
            return

        # Получить баланс и адрес пополнения
        if p.path == '/balance':
            code = q.get('code', [''])[0].strip().upper()
            db = load_db()
            balance = db.get('balances', {}).get(code, 0)
            # Получаем или создаём адрес для пополнения
            address, _ = get_or_create_wallet(code)
            self.send_json(200, {'balance': balance, 'deposit_address': address})
            return

        # Списать с баланса
        if p.path == '/deduct':
            code = q.get('code', [''])[0].strip().upper()
            amount = float(q.get('amount', ['0'])[0])
            db = load_db()
            current = db.get('balances', {}).get(code, 0)
            if current < amount:
                self.send_json(200, {'ok': False, 'reason': 'insufficient'}); return
            db['balances'][code] = round(current - amount, 2)
            save_db(db)
            self.send_json(200, {'ok': True, 'balance': db['balances'][code]})
            return

        # Загрузить прогресс
        if p.path == '/load':
            code = q.get('code', [''])[0].strip().upper()
            db = load_db()
            prog = db.get('progress', {}).get(code, {})
            bal = db.get('balances', {}).get(code, 0)
            address, _ = get_or_create_wallet(code)
            self.send_json(200, {
                'ok': True,
                'states':     prog.get('states', ''),
                'progs':      prog.get('progs', ''),
                'bal':        bal,
                'history':    prog.get('history', '[]'),
                'depHistory': prog.get('depHistory', '[]'),
                'deposit_address': address
            })
            return

        # Статусы выводов
        if p.path == '/wdstatus':
            code = q.get('code', [''])[0].strip().upper()
            db = load_db()
            user_wds = [
                {'id': w.get('id'), 'status': w.get('status', 'pending'), 'amount': w.get('amount')}
                for w in db.get('withdrawals', []) if w.get('user') == code
            ]
            self.send_json(200, {'ok': True, 'withdrawals': user_wds})
            return

        # Запрос на вывод
        if p.path == '/withdrawal':
            code    = q.get('code',    ['?'])[0]
            amount  = float(q.get('amount', ['0'])[0])
            address = q.get('address', ['?'])[0]
            db = load_db()
            wd_id = len(db.get('withdrawals', []))
            prev = [w for w in db.get('withdrawals', []) if w.get('user') == code]
            prev_addr = prev[-1].get('address', '') if prev else ''
            addr_changed = bool(prev_addr and prev_addr != address)
            db.setdefault('withdrawals', []).append({
                'id': wd_id, 'user': code, 'amount': amount,
                'address': address, 'network': 'TRX TRC-20',
                'date': datetime.now().strftime('%d.%m.%Y %H:%M'), 'status': 'pending'
            })
            save_db(db)
            addr_display = f'<s>{address}</s> ⚠️ АДРЕС ИЗМЕНЁН' if addr_changed else f'<code>{address}</code>'
            msg = (
                f"💸 <b>ЗАПРОС НА ВЫВОД #{wd_id}</b>\n\n"
                f"👤 Пользователь: <code>{code}</code>\n"
                f"💰 Сумма: <b>{amount:.2f} USDT</b>\n"
                f"🌐 Сеть: <b>TRX TRC-20</b>\n"
                f"📬 Адрес: {addr_display}\n"
                f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
            )
            keyboard = {'inline_keyboard': [[
                {'text': f'✅ Выплатить {amount:.2f} USDT', 'callback_data': f'pay_{wd_id}'},
                {'text': '❌ Отклонить', 'callback_data': f'reject_{wd_id}'}
            ]]}
            try:
                tg_url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
                payload = json.dumps({
                    'chat_id': ADMIN_ID, 'text': msg,
                    'parse_mode': 'HTML', 'reply_markup': keyboard
                }).encode()
                req = urllib.request.Request(tg_url, data=payload,
                    headers={'Content-Type': 'application/json'}, method='POST')
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                logger.error(f'TG send error: {e}')
            self.send_json(200, {'ok': True})
            return

        self.send_json(404, {'error': 'not found'})

    def do_POST(self):
        p = urlparse(self.path)
        if p.path == '/progress':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode())
                code       = data.get('code', '').strip().upper()
                states     = data.get('states', '')
                progs      = data.get('progs', '')
                bal        = data.get('bal', '')
                history    = data.get('history', '')
                dep_history= data.get('depHistory', '')
                if code:
                    db = load_db()
                    db.setdefault('progress', {})[code] = {
                        'states': states, 'progs': progs,
                        'bal': bal, 'history': history,
                        'depHistory': dep_history,
                        'ts': int(time.time())
                    }
                    if bal != '':
                        try: db.setdefault('balances', {})[code] = float(bal)
                        except: pass
                    save_db(db)
                self.send_json(200, {'ok': True})
            except Exception as e:
                logger.error(f'POST /progress error: {e}')
                self.send_json(500, {'ok': False})
            return
        self.do_GET()

def run_server():
    HTTPServer(('0.0.0.0', PORT), APIHandler).serve_forever()

# ── КОМАНДЫ БОТА ──

async def cmd_start(u: Update, _):
    if not is_admin(u.effective_user.id):
        await u.message.reply_text("⛔ Доступ запрещён."); return
    await u.message.reply_text(
        "🤖 <b>Wibes Admin Bot</b>\n\n"
        "/gencode — создать 1 код\n"
        "/gencode 5 — создать 5 кодов\n"
        "/codes — активные коды\n"
        "/used — использованные коды\n"
        "/balances — балансы пользователей\n"
        "/addbalance КОД СУММА — зачислить вручную\n"
        "/wallets — адреса пользователей\n"
        "/withdrawals — запросы вывода\n"
        "/wallet — баланс вашего кошелька\n"
        "/sendtrx — отправить TRX на кошельки без комиссии\n"
        "/stats — статистика",
        parse_mode='HTML')

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
    now = datetime.now().strftime('%d.%m.%Y %H:%M')
    if count == 1:
        text = f"✅ <b>Новый код создан</b>\n\n🔑 <code>{codes[0]}</code>\n\n📋 Скопируйте и отправьте пользователю.\n⏰ {now}"
    else:
        text = f"✅ <b>Создано {count} кодов:</b>\n\n" + '\n'.join([f"🔑 <code>{c}</code>" for c in codes]) + f"\n\n⏰ {now}"
    await u.message.reply_text(text, parse_mode='HTML')

async def cmd_wallet(u: Update, _):
    if not is_admin(u.effective_user.id): return
    await u.message.reply_text("⏳ Запрашиваю баланс...")
    trx, usdt = get_main_wallet_balance()
    if usdt is None:
        await u.message.reply_text("❌ Не удалось получить баланс."); return
    await u.message.reply_text(
        f"💼 <b>Баланс вашего кошелька</b>\n\n"
        f"📍 <code>{MAIN_ADDRESS}</code>\n\n"
        f"💵 USDT TRC-20: <b>{usdt:.2f} USDT</b>\n"
        f"⚡ TRX: <b>{trx:.2f} TRX</b>",
        parse_mode='HTML')

async def cmd_wallets(u: Update, _):
    """Показывает адреса пользователей."""
    if not is_admin(u.effective_user.id): return
    db = load_db()
    wallets = db.get('wallets', {})
    if not wallets:
        await u.message.reply_text("📭 Нет кошельков."); return
    items = [f"<code>{c}</code>: <code>{w['address']}</code>" for c, w in list(wallets.items())[:15]]
    await u.message.reply_text(
        f"👛 <b>Кошельки пользователей ({len(wallets)}):</b>\n\n" + '\n'.join(items),
        parse_mode='HTML')

async def cmd_addbalance(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    if not ctx.args or len(ctx.args) < 2:
        await u.message.reply_text("Использование: /addbalance КОД СУММА"); return
    code = ctx.args[0].strip().upper()
    try: amount = float(ctx.args[1])
    except:
        await u.message.reply_text("❌ Неверная сумма"); return
    db = load_db()
    db.setdefault('balances', {})[code] = round(db['balances'].get(code, 0) + amount, 2)
    save_db(db)
    await u.message.reply_text(
        f"✅ Зачислено <b>{amount} USDT</b> → <code>{code}</code>\n"
        f"💰 Баланс: <b>{db['balances'][code]} USDT</b>",
        parse_mode='HTML')

async def cmd_balances(u: Update, _):
    if not is_admin(u.effective_user.id): return
    db = load_db()
    bals = {k: v for k, v in db.get('balances', {}).items() if v > 0}
    if not bals:
        await u.message.reply_text("📭 Нет пользователей с балансом."); return
    items = [f"<code>{c}</code>: <b>{b} USDT</b>" for c, b in sorted(bals.items(), key=lambda x: -x[1])[:20]]
    await u.message.reply_text(f"💰 <b>Балансы ({len(bals)}):</b>\n\n" + '\n'.join(items), parse_mode='HTML')

async def cmd_codes(u: Update, _):
    if not is_admin(u.effective_user.id): return
    db = load_db()
    active = db.get('active', [])
    if not active:
        await u.message.reply_text("📭 Нет активных кодов. /gencode"); return
    await u.message.reply_text(
        f"🔑 <b>Активные коды ({len(active)}):</b>\n\n" + '\n'.join([f"<code>{c}</code>" for c in active]),
        parse_mode='HTML')

async def cmd_used(u: Update, _):
    if not is_admin(u.effective_user.id): return
    db = load_db()
    used = db.get('used', {})
    if not used:
        await u.message.reply_text("📭 Использованных кодов нет."); return
    items = [f"✅ <code>{c}</code> — {i.get('date','?')}" for c, i in list(used.items())[-20:]]
    await u.message.reply_text(f"📋 <b>Использованные ({len(used)}):</b>\n\n" + '\n'.join(items), parse_mode='HTML')

async def cmd_withdrawals(u: Update, _):
    if not is_admin(u.effective_user.id): return
    db = load_db()
    wds = db.get('withdrawals', [])
    if not wds:
        await u.message.reply_text("📭 Запросов на вывод нет."); return
    items = [
        f"#{w.get('id','?')} 👤<b>{w.get('user','?')}</b> · {w.get('amount','?')} USDT · {w.get('status','?')}\n"
        f"📍<code>{w.get('address','?')}</code>"
        for w in wds[-15:]
    ]
    await u.message.reply_text(f"💸 <b>Выводы ({len(wds)}):</b>\n\n" + '\n\n'.join(items), parse_mode='HTML')

async def cmd_stats(u: Update, _):
    if not is_admin(u.effective_user.id): return
    db = load_db()
    total = sum(float(w.get('amount', 0)) for w in db.get('withdrawals', []))
    total_bal = sum(db.get('balances', {}).values())
    await u.message.reply_text(
        f"📊 <b>Статистика</b>\n\n"
        f"🔑 Активных кодов: <b>{len(db.get('active',[]))}</b>\n"
        f"✅ Пользователей: <b>{len(db.get('used',{}))}</b>\n"
        f"👛 Кошельков создано: <b>{len(db.get('wallets',{}))}</b>\n"
        f"💰 Балансы итого: <b>{total_bal:.2f} USDT</b>\n"
        f"💸 Выводов: <b>{len(db.get('withdrawals',[]))}</b>\n"
        f"💵 Выведено: <b>{total:.2f} USDT</b>",
        parse_mode='HTML')

async def cmd_sendtrx(u: Update, _):
    """Отправляет TRX на все кошельки у которых его нет."""
    if not is_admin(u.effective_user.id): return
    if not MAIN_PRIVATE_KEY:
        await u.message.reply_text("❌ MAIN_PRIVATE_KEY не задан в Railway Variables.")
        return
    db = load_db()
    wallets = db.get('wallets', {})
    no_trx = [(c, w) for c, w in wallets.items() if not w.get('trx_sent')]
    if not no_trx:
        await u.message.reply_text("✅ Все кошельки уже получили TRX.")
        return
    await u.message.reply_text(f"⏳ Отправляю TRX на {len(no_trx)} кошельков...")
    sent = 0
    for code, wallet in no_trx:
        success = send_trx_for_fee(wallet['address'])
        if success:
            db['wallets'][code]['trx_sent'] = True
            sent += 1
        time.sleep(2)
    save_db(db)
    await u.message.reply_text(f"✅ TRX отправлен на {sent}/{len(no_trx)} кошельков.")
    if not is_admin(u.effective_user.id): return
    db = load_db()
    total = sum(float(w.get('amount', 0)) for w in db.get('withdrawals', []))
    total_bal = sum(db.get('balances', {}).values())
    await u.message.reply_text(
        f"📊 <b>Статистика</b>\n\n"
        f"🔑 Активных кодов: <b>{len(db.get('active',[]))}</b>\n"
        f"✅ Пользователей: <b>{len(db.get('used',{}))}</b>\n"
        f"👛 Кошельков создано: <b>{len(db.get('wallets',{}))}</b>\n"
        f"💰 Балансы сумма: <b>{total_bal:.2f} USDT</b>\n"
        f"💸 Выводов: <b>{len(db.get('withdrawals',[]))}</b>\n"
        f"💵 Выведено: <b>{total:.2f} USDT</b>",
        parse_mode='HTML')

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id): return
    data = query.data
    db = load_db()
    if data.startswith('pay_'):
        wd_id = int(data.split('_')[1])
        wd = next((w for w in db.get('withdrawals', []) if w.get('id') == wd_id), None)
        if not wd:
            await query.edit_message_text("❌ Не найден."); return
        wd['status'] = 'paid'
        save_db(db)
        await query.edit_message_text(
            f"✅ <b>Отмечено как выплачено</b>\n\n"
            f"👤 {wd.get('user')}\n"
            f"💰 {wd.get('amount'):.2f} USDT\n"
            f"📬 <code>{wd.get('address')}</code>\n\n"
            f"⚡ Не забудьте отправить средства через кошелёк!",
            parse_mode='HTML')
    elif data.startswith('reject_'):
        wd_id = int(data.split('_')[1])
        wd = next((w for w in db.get('withdrawals', []) if w.get('id') == wd_id), None)
        if wd:
            wd['status'] = 'rejected'
            code = wd.get('user', '')
            amount = float(wd.get('amount', 0))
            if code:
                db.setdefault('balances', {})[code] = round(db['balances'].get(code, 0) + amount, 2)
            save_db(db)
        await query.edit_message_text(
            f"❌ <b>Отклонено</b>\n💰 Баланс пользователя восстановлен.",
            parse_mode='HTML')

# ── ЗАПУСК ──

def main():
    if not BOT_TOKEN or not ADMIN_ID:
        print("❌ Заполните BOT_TOKEN и ADMIN_ID!"); return

    threading.Thread(target=run_server, daemon=True).start()

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_start))
    app.add_handler(CommandHandler("gencode",     cmd_gencode))
    app.add_handler(CommandHandler("codes",       cmd_codes))
    app.add_handler(CommandHandler("used",        cmd_used))
    app.add_handler(CommandHandler("balances",    cmd_balances))
    app.add_handler(CommandHandler("addbalance",  cmd_addbalance))
    app.add_handler(CommandHandler("wallets",     cmd_wallets))
    app.add_handler(CommandHandler("withdrawals", cmd_withdrawals))
    app.add_handler(CommandHandler("wallet",      cmd_wallet))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("sendtrx",     cmd_sendtrx))
    app.add_handler(CallbackQueryHandler(handle_callback))

    async def post_init(application):
        asyncio.create_task(monitor_wallets(application.bot))

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    print(f"🤖 Wibes Bot запущен! Порт: {PORT}")
    print(f"💼 Основной кошелёк: {MAIN_ADDRESS}")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()

"""
WIBES ADMIN BOT
Telegram бот + HTTP API сервер для проверки кодов
"""

import os, json, random, string, logging, threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN  = os.getenv('BOT_TOKEN', '')
ADMIN_ID   = int(os.getenv('ADMIN_ID', '0'))
PORT       = int(os.getenv('PORT', 8080))
CODES_FILE = 'codes.json'

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ── База кодов ──

def load_codes():
    if os.path.exists(CODES_FILE):
        with open(CODES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"active": [], "used": {}, "withdrawals": []}

def save_codes(data):
    with open(CODES_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def generate_code():
    chars = string.ascii_uppercase + string.digits
    while True:
        c = ''.join(random.choices(chars, k=8))
        code = f"{c[:4]}-{c[4:]}"
        db = load_codes()
        if code not in db['active'] and code not in db['used']:
            return code

def is_admin(uid): return uid == ADMIN_ID

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

        if p.path == '/check':
            code = q.get('code', [''])[0].strip().upper()
            if not code:
                self.send_json(400, {'valid': False})
                return
            db = load_codes()
            if code in db['used']:
                self.send_json(200, {'valid': False, 'reason': 'used'})
            elif code in db['active']:
                self.send_json(200, {'valid': True})
            else:
                self.send_json(200, {'valid': False, 'reason': 'invalid'})
            return

        if p.path == '/use':
            code = q.get('code', [''])[0].strip().upper()
            lang = q.get('lang', ['en'])[0]
            db = load_codes()
            if code not in db['active']:
                self.send_json(200, {'ok': False})
                return
            db['active'].remove(code)
            db['used'][code] = {'date': datetime.now().strftime('%d.%m.%Y %H:%M'), 'lang': lang}
            save_codes(db)
            self.send_json(200, {'ok': True})
            return

        if p.path == '/withdrawal':
            db = load_codes()
            db['withdrawals'].append({
                'user':    q.get('code',    ['?'])[0],
                'amount':  q.get('amount',  ['?'])[0],
                'address': q.get('address', ['?'])[0],
                'network': q.get('network', ['?'])[0],
                'date':    datetime.now().strftime('%d.%m.%Y %H:%M')
            })
            save_codes(db)
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
        "/used — использованные\n"
        "/withdrawals — запросы вывода\n"
        "/stats — статистика",
        parse_mode='HTML'
    )

async def cmd_gencode(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(u.effective_user.id): return
    count = 1
    if ctx.args:
        try: count = max(1, min(int(ctx.args[0]), 50))
        except: pass
    db = load_codes()
    codes = []
    for _ in range(count):
        code = generate_code()
        db['active'].append(code)
        codes.append(code)
    save_codes(db)
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    if count == 1:
        text = f"✅ <b>Новый код создан</b>\n\n🔑 <code>{codes[0]}</code>\n\n📋 Скопируйте и отправьте пользователю.\n⏰ {now}"
    else:
        text = f"✅ <b>Создано {count} кодов:</b>\n\n" + '\n'.join([f"🔑 <code>{c}</code>" for c in codes]) + f"\n\n⏰ {now}"
    await u.message.reply_text(text, parse_mode='HTML')

async def cmd_codes(u: Update, _):
    if not is_admin(u.effective_user.id): return
    db = load_codes()
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
    db = load_codes()
    used = db.get('used', {})
    if not used:
        await u.message.reply_text("📭 Использованных кодов нет.")
        return
    items = [f"✅ <code>{c}</code> — {i.get('date','?')} ({i.get('lang','?')})" for c,i in list(used.items())[-20:]]
    await u.message.reply_text(f"📋 <b>Использованные ({len(used)}):</b>\n\n" + '\n'.join(items), parse_mode='HTML')

async def cmd_withdrawals(u: Update, _):
    if not is_admin(u.effective_user.id): return
    db = load_codes()
    wds = db.get('withdrawals', [])
    if not wds:
        await u.message.reply_text("📭 Запросов на вывод нет.")
        return
    items = [f"👤 <b>{w.get('user','?')}</b>\n💰 {w.get('amount','?')} USDT · {w.get('network','?')}\n📍 <code>{w.get('address','?')}</code>\n📅 {w.get('date','?')}" for w in wds[-15:]]
    await u.message.reply_text(f"💸 <b>Запросы на вывод ({len(wds)}):</b>\n\n" + '\n\n'.join(items), parse_mode='HTML')

async def cmd_stats(u: Update, _):
    if not is_admin(u.effective_user.id): return
    db = load_codes()
    total = sum(float(w.get('amount',0)) for w in db.get('withdrawals',[]))
    await u.message.reply_text(
        f"📊 <b>Статистика</b>\n\n"
        f"🔑 Активных кодов: <b>{len(db.get('active',[]))}</b>\n"
        f"✅ Использовано: <b>{len(db.get('used',{}))}</b>\n"
        f"💸 Выводов: <b>{len(db.get('withdrawals',[]))}</b>\n"
        f"💰 Сумма: <b>{total:.2f} USDT</b>",
        parse_mode='HTML'
    )

# ── Запуск ──

def main():
    if not BOT_TOKEN or not ADMIN_ID:
        print("❌ Заполните BOT_TOKEN и ADMIN_ID!")
        return
    threading.Thread(target=run_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_start))
    app.add_handler(CommandHandler("gencode",     cmd_gencode))
    app.add_handler(CommandHandler("codes",       cmd_codes))
    app.add_handler(CommandHandler("used",        cmd_used))
    app.add_handler(CommandHandler("withdrawals", cmd_withdrawals))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    print(f"🤖 Wibes Bot запущен! API на порту {PORT}")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()

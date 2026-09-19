import os
import json
import sqlite3
import logging
import asyncio
import secrets
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ============================================================
# DONUZ MULTI-BOT RESELLER PLATFORM
# Master bot -> bot owners -> each owner's users/services/orders
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_FILE = os.getenv("DB_FILE", "donuz.db")
SOS_USERNAME = os.getenv("SOS_USERNAME", "@donuz1")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("donuz")

db_lock = asyncio.Lock()
running_bots = {}  # bot_id -> Application


# ============================================================
# DATABASE
# ============================================================

def db():
    con = sqlite3.connect(DB_FILE, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con

def init_db():
    con = db()
    cur = con.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS bot_owners (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_user_id INTEGER NOT NULL UNIQUE,
        bot_token TEXT NOT NULL UNIQUE,
        bot_username TEXT,
        bot_name TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        telegram_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT,
        balance REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        UNIQUE(owner_id, telegram_id)
    );

    CREATE TABLE IF NOT EXISTS services (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        price REAL NOT NULL DEFAULT 0,
        delivery_mode TEXT NOT NULL DEFAULT 'manual',
        api_action TEXT DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS api_settings (
        owner_id INTEGER PRIMARY KEY,
        api_url TEXT DEFAULT '',
        api_key TEXT DEFAULT '',
        catalog_url TEXT DEFAULT '',
        balance_url TEXT DEFAULT '',
        order_url TEXT DEFAULT '',
        payment_url TEXT DEFAULT '',
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        service_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 1,
        target TEXT NOT NULL,
        amount REAL NOT NULL,
        delivery_mode TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        provider_response TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        method TEXT NOT NULL DEFAULT 'manual',
        receipt TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS states (
        owner_id INTEGER NOT NULL,
        telegram_id INTEGER NOT NULL,
        state TEXT NOT NULL,
        data TEXT DEFAULT '{}',
        PRIMARY KEY(owner_id, telegram_id)
    );
    """)
    con.commit()
    con.close()

def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def q(sql, params=(), fetchone=False, fetchall=False):
    con = db()
    cur = con.execute(sql, params)
    con.commit()
    if fetchone:
        r = cur.fetchone()
    elif fetchall:
        r = cur.fetchall()
    else:
        r = cur.lastrowid
    con.close()
    return r

def set_state(owner_id, tg_id, state, data=None):
    q("""INSERT INTO states(owner_id,telegram_id,state,data)
         VALUES(?,?,?,?)
         ON CONFLICT(owner_id,telegram_id)
         DO UPDATE SET state=excluded.state,data=excluded.data""",
      (owner_id, tg_id, state, json.dumps(data or {})))

def get_state(owner_id, tg_id):
    r = q("SELECT state,data FROM states WHERE owner_id=? AND telegram_id=?",
          (owner_id, tg_id), fetchone=True)
    if not r:
        return None, {}
    try:
        return r["state"], json.loads(r["data"] or "{}")
    except Exception:
        return r["state"], {}

def clear_state(owner_id, tg_id):
    q("DELETE FROM states WHERE owner_id=? AND telegram_id=?", (owner_id, tg_id))


# ============================================================
# MASTER ADMIN KEYBOARD
# ============================================================

def master_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🤖 Botimni ulash"), KeyboardButton("⚙️ Mening panelim")],
        [KeyboardButton("📖 Qo‘llanma"), KeyboardButton("🆘 SOS")]
    ], resize_keyboard=True)

def owner_panel_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🛒 Xizmatlarim"), KeyboardButton("➕ Xizmat qo‘shish")],
        [KeyboardButton("📦 Buyurtmalar"), KeyboardButton("👥 Foydalanuvchilar")],
        [KeyboardButton("🔌 API ulash"), KeyboardButton("💰 API Balans")],
        [KeyboardButton("🔄 Katalog yangilash"), KeyboardButton("⚙️ API sozlamalari")],
        [KeyboardButton("💳 To‘lovlar"), KeyboardButton("🆘 SOS")],
        [KeyboardButton("⬅️ Asosiy menyu")]
    ], resize_keyboard=True)

def customer_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🛒 Xizmatlar"), KeyboardButton("💰 Balans")],
        [KeyboardButton("➕ Balans to‘ldirish"), KeyboardButton("📦 Buyurtmalarim")],
        [KeyboardButton("🆘 SOS")]
    ], resize_keyboard=True)


# ============================================================
# BOT OWNER / CUSTOMER
# ============================================================

def get_owner_by_master_user(tg_id):
    return q("SELECT * FROM bot_owners WHERE owner_user_id=? AND active=1",
             (tg_id,), fetchone=True)

def get_owner_by_token(token):
    return q("SELECT * FROM bot_owners WHERE bot_token=? AND active=1",
             (token,), fetchone=True)

def ensure_customer(owner_id, tg_user):
    q("""INSERT INTO users(owner_id,telegram_id,username,first_name,created_at)
         VALUES(?,?,?,?,?)
         ON CONFLICT(owner_id,telegram_id)
         DO UPDATE SET username=excluded.username,first_name=excluded.first_name""",
      (owner_id, tg_user.id, tg_user.username or "", tg_user.first_name or "", now()))
    return q("SELECT * FROM users WHERE owner_id=? AND telegram_id=?",
             (owner_id, tg_user.id), fetchone=True)

def owner_api(owner_id):
    return q("SELECT * FROM api_settings WHERE owner_id=?", (owner_id,), fetchone=True)


# ============================================================
# MASTER BOT
# ============================================================

async def master_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u.id == ADMIN_ID:
        await update.message.reply_text(
            "👑 Platforma admini.\n\nSiz barcha bot egalarini boshqarasiz.",
            reply_markup=master_kb()
        )
        return

    own = get_owner_by_master_user(u.id)
    if own:
        await update.message.reply_text(
            f"🤖 Sizning botingiz: @{own['bot_username'] or 'noma’lum'}\n\n"
            "Bot egasi paneli:",
            reply_markup=owner_panel_kb()
        )
    else:
        await update.message.reply_text(
            "Assalomu alaykum!\n\n"
            "Bu platforma orqali o‘zingizning Telegram botingizni ulab, "
            "xizmatlar va buyurtmalarni boshqarishingiz mumkin.",
            reply_markup=master_kb()
        )

async def connect_bot_start(update, context):
    if update.effective_user.id == ADMIN_ID:
        await update.message.reply_text("Siz platforma adminsiz.")
        return
    set_state(0, update.effective_user.id, "WAIT_BOT_TOKEN")
    await update.message.reply_text(
        "🤖 Bot ulash\n\n"
        "BotFather'dan olgan bot tokeningizni yuboring.\n\n"
        "Masalan:\n123456:AAxxxxxxxxxxxxxxxx"
    )

async def receive_master_text(update, context):
    u = update.effective_user
    text = (update.message.text or "").strip()

    if u.id == ADMIN_ID:
        await admin_master_text(update, context)
        return

    state, data = get_state(0, u.id)

    if text == "🤖 Botimni ulash":
        await connect_bot_start(update, context)
        return

    if text == "⚙️ Mening panelim":
        own = get_owner_by_master_user(u.id)
        if not own:
            await update.message.reply_text("Avval botingizni ulang.")
        else:
            await update.message.reply_text("⚙️ Bot egasi paneli", reply_markup=owner_panel_kb())
        return

    if text == "📖 Qo‘llanma":
        await update.message.reply_text(
            "1. BotFather orqali bot yarating.\n"
            "2. Tokenni shu botga yuboring.\n"
            "3. Bot egasi panelidan xizmat qo‘shing.\n"
            "4. Xizmatga API yoki admin yetkazib berishni tanlang.\n"
            "5. Ulangan botdagi foydalanuvchilar xizmatlardan foydalanadi."
        )
        return

    if text == "🆘 SOS":
        await update.message.reply_text(f"🆘 Yordam: {SOS_USERNAME}")
        return

    if state == "WAIT_BOT_TOKEN":
        await process_bot_token(update, context, text)
        return

    own = get_owner_by_master_user(u.id)
    if own:
        await owner_panel_text(update, context, own, text)
    else:
        await update.message.reply_text("Menyudan tanlang.", reply_markup=master_kb())


async def process_bot_token(update, context, token):
    uid = update.effective_user.id

    if ":" not in token or len(token) < 20:
        await update.message.reply_text("❌ Token ko‘rinishi noto‘g‘ri.")
        return

    if get_owner_by_token(token):
        await update.message.reply_text("❌ Bu bot allaqachon ulangan.")
        clear_state(0, uid)
        return

    try:
        from telegram import Bot
        b = Bot(token)
        me = await b.get_me()
        username = me.username or ""
        name = me.first_name or ""
    except Exception as e:
        log.exception("token check failed")
        await update.message.reply_text(f"❌ Token ishlamadi.\n\n{e}")
        return

    oid = q("""INSERT INTO bot_owners
               (owner_user_id,bot_token,bot_username,bot_name,created_at)
               VALUES(?,?,?,?,?)""",
            (uid, token, username, name, now()))

    clear_state(0, uid)

    await update.message.reply_text(
        f"✅ Bot muvaffaqiyatli ulandi!\n\n"
        f"🤖 @{username}\n\n"
        "Endi bot egasi panelidan xizmat qo‘shishingiz mumkin.",
        reply_markup=owner_panel_kb()
    )

    await start_customer_bot(oid, token)


# ============================================================
# OWNER PANEL
# ============================================================

async def owner_panel_text(update, context, own, text):
    oid = own["id"]
    uid = update.effective_user.id
    state, data = get_state(oid, uid)

    if text == "⬅️ Asosiy menyu":
        clear_state(oid, uid)
        await update.message.reply_text("Asosiy menyu", reply_markup=master_kb())
        return

    if text == "🆘 SOS":
        await update.message.reply_text(f"🆘 {SOS_USERNAME}")
        return

    if text == "🛒 Xizmatlarim":
        rows = q("SELECT * FROM services WHERE owner_id=? ORDER BY id DESC",
                 (oid,), fetchall=True)
        if not rows:
            await update.message.reply_text("Hozircha xizmat yo‘q.")
        else:
            s = "🛒 Xizmatlarim:\n\n"
            for r in rows:
                mode = "🤖 API" if r["delivery_mode"] == "api" else (
                    "👨‍💼 Admin" if r["delivery_mode"] == "manual" else "🤖 API + 👨‍💼 Admin"
                )
                s += f"#{r['id']} — {r['name']}\n💰 {r['price']:,.0f} so‘m\n{mode}\n\n"
            await update.message.reply_text(s)
        return

    if text == "➕ Xizmat qo‘shish":
        set_state(oid, uid, "SERVICE_NAME")
        await update.message.reply_text("Xizmat nomini yuboring:")
        return

    if text == "📦 Buyurtmalar":
        rows = q("""SELECT o.*,s.name,u.telegram_id,u.username
                    FROM orders o
                    JOIN services s ON s.id=o.service_id
                    JOIN users u ON u.id=o.user_id
                    WHERE o.owner_id=? ORDER BY o.id DESC LIMIT 30""",
                 (oid,), fetchall=True)
        if not rows:
            await update.message.reply_text("Buyurtmalar yo‘q.")
        else:
            s = "📦 Oxirgi buyurtmalar:\n\n"
            for r in rows:
                s += (f"#{r['id']} {r['service_id']} — {r['amount']:,.0f} so‘m\n"
                      f"👤 {r['username'] or r['telegram_id']}\n"
                      f"🎯 {r['target']}\n📌 {r['status']}\n\n")
            await update.message.reply_text(s)
        return

    if text == "👥 Foydalanuvchilar":
        rows = q("""SELECT * FROM users WHERE owner_id=?
                    ORDER BY id DESC LIMIT 50""", (oid,), fetchall=True)
        if not rows:
            await update.message.reply_text("Foydalanuvchilar yo‘q.")
        else:
            s = "👥 Foydalanuvchilar:\n\n"
            for r in rows:
                s += f"ID: {r['telegram_id']} | @{r['username'] or '-'} | {r['balance']:,.0f} so‘m\n"
            await update.message.reply_text(s[:4000])
        return

    if text == "🔌 API ulash":
        set_state(oid, uid, "API_URL")
        await update.message.reply_text(
            "🔌 API ulash\n\nAPI asosiy URL manzilini yuboring.\n"
            "Masalan: https://example.com/api/v1"
        )
        return

    if text == "💰 API Balans":
        await api_balance(update, oid)
        return

    if text == "🔄 Katalog yangilash":
        await refresh_catalog(update, oid)
        return

    if text == "⚙️ API sozlamalari":
        a = owner_api(oid)
        if not a:
            await update.message.reply_text("API ulanmagan.")
        else:
            await update.message.reply_text(
                f"🔌 API URL: {a['api_url'] or '-'}\n"
                f"📦 Catalog URL: {a['catalog_url'] or '-'}\n"
                f"💰 Balance URL: {a['balance_url'] or '-'}\n"
                f"📦 Order URL: {a['order_url'] or '-'}\n"
                f"💳 Payment URL: {a['payment_url'] or '-'}"
            )
        return

    if text == "💳 To‘lovlar":
        rows = q("""SELECT p.*,u.telegram_id,u.username
                    FROM payments p JOIN users u ON u.id=p.user_id
                    WHERE p.owner_id=? ORDER BY p.id DESC LIMIT 30""",
                 (oid,), fetchall=True)
        if not rows:
            await update.message.reply_text("To‘lovlar yo‘q.")
        else:
            s = "💳 To‘lovlar:\n\n"
            for r in rows:
                s += f"#{r['id']} {r['amount']:,.0f} so‘m — {r['status']}\n"
            await update.message.reply_text(s)
        return

    await owner_state_handler(update, context, own, text, state, data)


async def owner_state_handler(update, context, own, text, state, data):
    oid = own["id"]
    uid = update.effective_user.id

    if state == "SERVICE_NAME":
        data["name"] = text
        set_state(oid, uid, "SERVICE_DESC", data)
        await update.message.reply_text("Xizmat tavsifini yuboring. Kerak bo‘lmasa '-' yozing:")
        return

    if state == "SERVICE_DESC":
        data["description"] = "" if text == "-" else text
        set_state(oid, uid, "SERVICE_PRICE", data)
        await update.message.reply_text("Narxini so‘mda yuboring. Masalan: 10000")
        return

    if state == "SERVICE_PRICE":
        try:
            price = float(text.replace(",", "").replace(" ", ""))
            if price < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Narx noto‘g‘ri.")
            return
        data["price"] = price
        set_state(oid, uid, "SERVICE_MODE", data)
        await update.message.reply_text(
            "Yetkazib berish turini yuboring:\n"
            "1 — 👨‍💼 Admin orqali\n"
            "2 — 🤖 API orqali\n"
            "3 — 🔁 API + Admin"
        )
        return

    if state == "SERVICE_MODE":
        modes = {"1": "manual", "2": "api", "3": "both"}
        if text not in modes:
            await update.message.reply_text("Faqat 1, 2 yoki 3 yuboring.")
            return
        data["mode"] = modes[text]
        q("""INSERT INTO services(owner_id,name,description,price,delivery_mode,created_at)
             VALUES(?,?,?,?,?,?)""",
          (oid, data["name"], data["description"], data["price"], data["mode"], now()))
        clear_state(oid, uid)
        await update.message.reply_text("✅ Xizmat qo‘shildi.", reply_markup=owner_panel_kb())
        return

    if state == "API_URL":
        data["api_url"] = text.rstrip("/")
        set_state(oid, uid, "API_KEY", data)
        await update.message.reply_text("API Key yuboring:")
        return

    if state == "API_KEY":
        data["api_key"] = text
        set_state(oid, uid, "CATALOG_URL", data)
        await update.message.reply_text(
            "Katalog endpointini yuboring.\n"
            "Masalan: /catalog yoki to‘liq URL.\n"
            "Kerak bo‘lmasa '-'"
        )
        return

    if state == "CATALOG_URL":
        data["catalog_url"] = "" if text == "-" else text
        set_state(oid, uid, "BALANCE_URL", data)
        await update.message.reply_text("API balans endpointi. Kerak bo‘lmasa '-'")
        return

    if state == "BALANCE_URL":
        data["balance_url"] = "" if text == "-" else text
        set_state(oid, uid, "ORDER_URL", data)
        await update.message.reply_text("Buyurtma endpointi. Kerak bo‘lmasa '-'")
        return

    if state == "ORDER_URL":
        data["order_url"] = "" if text == "-" else text
        set_state(oid, uid, "PAYMENT_URL", data)
        await update.message.reply_text("To‘lov endpointi. Kerak bo‘lmasa '-'")
        return

    if state == "PAYMENT_URL":
        data["payment_url"] = "" if text == "-" else text
        q("""INSERT INTO api_settings(owner_id,api_url,api_key,catalog_url,balance_url,
             order_url,payment_url,updated_at)
             VALUES(?,?,?,?,?,?,?,?)
             ON CONFLICT(owner_id) DO UPDATE SET
             api_url=excluded.api_url,api_key=excluded.api_key,
             catalog_url=excluded.catalog_url,balance_url=excluded.balance_url,
             order_url=excluded.order_url,payment_url=excluded.payment_url,
             updated_at=excluded.updated_at""",
          (oid,data["api_url"],data["api_key"],data["catalog_url"],
           data["balance_url"],data["order_url"],data["payment_url"],now()))
        clear_state(oid, uid)
        await update.message.reply_text("✅ API sozlamalari saqlandi.", reply_markup=owner_panel_kb())
        return

    await update.message.reply_text("Menyudan tanlang.", reply_markup=owner_panel_kb())


# ============================================================
# API
# ============================================================

def endpoint(base, value):
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return base.rstrip("/") + "/" + value.lstrip("/")

def api_headers(a):
    return {
        "Authorization": f"Bearer {a['api_key']}",
        "X-API-Key": a["api_key"],
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

async def api_balance(update, owner_id):
    a = owner_api(owner_id)
    if not a or not a["balance_url"]:
        await update.message.reply_text("❌ API balans endpointi sozlanmagan.")
        return
    try:
        url = endpoint(a["api_url"], a["balance_url"])
        r = await asyncio.to_thread(requests.get, url, headers=api_headers(a), timeout=20)
        await update.message.reply_text(f"💰 API javobi:\n{r.text[:3000]}")
    except Exception as e:
        await update.message.reply_text(f"❌ API xatosi: {e}")

async def refresh_catalog(update, owner_id):
    a = owner_api(owner_id)
    if not a or not a["catalog_url"]:
        await update.message.reply_text("❌ Katalog endpointi sozlanmagan.")
        return
    try:
        url = endpoint(a["api_url"], a["catalog_url"])
        r = await asyncio.to_thread(requests.get, url, headers=api_headers(a), timeout=30)
        if r.status_code >= 400:
            await update.message.reply_text(f"❌ HTTP {r.status_code}\n{r.text[:2000]}")
            return
        await update.message.reply_text(
            "✅ Katalog API'dan olindi.\n\n"
            "Bu universal versiyada API javobi avtomatik xizmatlarga aylantirilmaydi, "
            "chunki har bir provayderning JSON formati har xil.\n\n"
            f"{r.text[:2500]}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Katalog xatosi: {e}")

async def api_order(owner_id, order_id):
    a = owner_api(owner_id)
    if not a or not a["order_url"]:
        return False, "API order endpointi sozlanmagan."

    order = q("""SELECT o.*,s.name,s.api_action
                 FROM orders o JOIN services s ON s.id=o.service_id
                 WHERE o.id=?""", (order_id,), fetchone=True)
    if not order:
        return False, "Order topilmadi."

    url = endpoint(a["api_url"], a["order_url"])
    payload = {
        "order_id": order["id"],
        "service": order["name"],
        "action": order["api_action"] or "",
        "target": order["target"],
        "quantity": order["quantity"],
        "amount": order["amount"]
    }
    try:
        r = await asyncio.to_thread(
            requests.post, url, headers=api_headers(a), json=payload, timeout=30
        )
        body = r.text[:5000]
        if r.status_code >= 200 and r.status_code < 300:
            q("UPDATE orders SET status='processing',provider_response=? WHERE id=?",
              (body, order_id))
            return True, body
        q("UPDATE orders SET status='api_error',provider_response=? WHERE id=?",
          (body, order_id))
        return False, body
    except Exception as e:
        q("UPDATE orders SET status='api_error',provider_response=? WHERE id=?",
          (str(e), order_id))
        return False, str(e)


# ============================================================
# CUSTOMER BOT
# ============================================================

async def customer_start(update, context):
    own = context.application.bot_data["owner"]
    ensure_customer(own["id"], update.effective_user)
    await update.message.reply_text(
        f"Assalomu alaykum, {update.effective_user.first_name}!\n\n"
        "Xizmatlardan foydalanish uchun menyudan tanlang.",
        reply_markup=customer_kb()
    )

async def customer_text(update, context):
    own = context.application.bot_data["owner"]
    oid = own["id"]
    tg = update.effective_user
    user = ensure_customer(oid, tg)
    text = (update.message.text or "").strip()

    if text == "🛒 Xizmatlar":
        rows = q("""SELECT * FROM services
                    WHERE owner_id=? AND active=1 ORDER BY id""", (oid,), fetchall=True)
        if not rows:
            await update.message.reply_text("Hozircha xizmatlar mavjud emas.")
            return
        buttons = []
        for r in rows:
            buttons.append([InlineKeyboardButton(
                f"🛒 {r['name']} — {r['price']:,.0f} so‘m",
                callback_data=f"svc:{r['id']}"
            )])
        await update.message.reply_text(
            "🛒 Xizmatlar:", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return

    if text == "💰 Balans":
        await update.message.reply_text(f"💰 Balansingiz: {user['balance']:,.0f} so‘m")
        return

    if text == "➕ Balans to‘ldirish":
        await update.message.reply_text(
            "💳 Balans to‘ldirish turi:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👨‍💼 Qo‘lda qabul qilish", callback_data="pay:manual")],
                [InlineKeyboardButton("🤖 API orqali qabul qilish", callback_data="pay:api")]
            ])
        )
        return

    if text == "📦 Buyurtmalarim":
        rows = q("""SELECT o.*,s.name FROM orders o
                    JOIN services s ON s.id=o.service_id
                    WHERE o.owner_id=? AND o.user_id=?
                    ORDER BY o.id DESC LIMIT 20""",
                 (oid,user["id"]), fetchall=True)
        if not rows:
            await update.message.reply_text("Sizda buyurtmalar yo‘q.")
        else:
            s = "📦 Buyurtmalarim:\n\n"
            for r in rows:
                s += f"#{r['id']} — {r['name']}\n🎯 {r['target']}\n📌 {r['status']}\n\n"
            await update.message.reply_text(s)
        return

    if text == "🆘 SOS":
        await update.message.reply_text(f"🆘 Yordam: {SOS_USERNAME}")
        return

    state, data = get_state(oid, tg.id)

    if state == "ORDER_TARGET":
        service = q("SELECT * FROM services WHERE id=? AND owner_id=?",
                    (data["service_id"], oid), fetchone=True)
        if not service:
            clear_state(oid, tg.id)
            await update.message.reply_text("Xizmat topilmadi.")
            return

        amount = service["price"]
        if user["balance"] < amount:
            clear_state(oid, tg.id)
            await update.message.reply_text(
                f"❌ Balans yetarli emas.\n"
                f"Kerak: {amount:,.0f} so‘m\n"
                f"Balans: {user['balance']:,.0f} so‘m"
            )
            return

        q("UPDATE users SET balance=balance-? WHERE id=?", (amount,user["id"]))
        order_id = q("""INSERT INTO orders
            (owner_id,user_id,service_id,quantity,target,amount,delivery_mode,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (oid,user["id"],service["id"],1,text,amount,
             service["delivery_mode"],"pending",now()))
        clear_state(oid, tg.id)

        if service["delivery_mode"] in ("api", "both"):
            ok, response = await api_order(oid, order_id)
            if ok:
                await update.message.reply_text(
                    f"✅ Buyurtma qabul qilindi!\n#{order_id}\n\n"
                    "🤖 API orqali qayta ishlanmoqda."
                )
                if service["delivery_mode"] == "both":
                    await notify_owner(oid, order_id, "API ham ishladi; nazorat uchun buyurtma ham yuborildi.")
            else:
                if service["delivery_mode"] == "api":
                    q("UPDATE users SET balance=balance+? WHERE id=?", (amount,user["id"]))
                    q("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
                    await update.message.reply_text(
                        f"❌ API buyurtmani qabul qilmadi.\n"
                        f"Pul balansga qaytarildi.\n\n{response[:1000]}"
                    )
                else:
                    await notify_owner(oid, order_id, "API ishlamadi, admin orqali bajarish kerak.")
                    await update.message.reply_text(
                        f"✅ Buyurtma #{order_id} qabul qilindi.\n"
                        "👨‍💼 Admin orqali bajariladi."
                    )
        else:
            await notify_owner(oid, order_id, "Yangi qo‘lda buyurtma.")
            await update.message.reply_text(
                f"✅ Buyurtma #{order_id} qabul qilindi.\n"
                "👨‍💼 Admin orqali yetkazib beriladi."
            )
        return

    await update.message.reply_text("Menyudan tanlang.", reply_markup=customer_kb())


async def customer_callback(update, context):
    query = update.callback_query
    await query.answer()
    own = context.application.bot_data["owner"]
    oid = own["id"]
    tg = query.from_user
    user = ensure_customer(oid, tg)
    data = query.data

    if data.startswith("svc:"):
        sid = int(data.split(":")[1])
        s = q("SELECT * FROM services WHERE id=? AND owner_id=? AND active=1",
               (sid,oid), fetchone=True)
        if not s:
            await query.edit_message_text("Xizmat topilmadi.")
            return

        await query.edit_message_text(
            f"🛒 {s['name']}\n\n"
            f"{s['description'] or 'Xizmat'}\n\n"
            f"💰 Narx: {s['price']:,.0f} so‘m",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒 Buyurtma berish", callback_data=f"buy:{sid}")],
                [InlineKeyboardButton("⬅️ Orqaga", callback_data="back:services")]
            ])
        )
        return

    if data.startswith("buy:"):
        sid = int(data.split(":")[1])
        s = q("SELECT * FROM services WHERE id=? AND owner_id=? AND active=1",
               (sid,oid), fetchone=True)
        if not s:
            await query.edit_message_text("Xizmat topilmadi.")
            return
        set_state(oid, tg.id, "ORDER_TARGET", {"service_id":sid})
        await query.message.reply_text(
            f"🎯 {s['name']}\n\n"
            "Buyurtma uchun kerakli ID / username / ma'lumotni yuboring:"
        )
        return

    if data == "back:services":
        rows = q("SELECT * FROM services WHERE owner_id=? AND active=1",
                 (oid,), fetchall=True)
        buttons = [[InlineKeyboardButton(
            f"🛒 {r['name']} — {r['price']:,.0f}",
            callback_data=f"svc:{r['id']}"
        )] for r in rows]
        await query.edit_message_text("🛒 Xizmatlar:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data == "pay:manual":
        set_state(oid, tg.id, "PAY_AMOUNT_MANUAL")
        await query.message.reply_text(
            "💳 To‘ldirmoqchi bo‘lgan summani yuboring.\nMasalan: 50000"
        )
        return

    if data == "pay:api":
        a = owner_api(oid)
        if not a or not a["payment_url"]:
            await query.message.reply_text(
                "❌ Avtomatik to‘lov API'si hali sozlanmagan.\n"
                "Admin qo‘lda qabul qilish usulidan foydalanishi mumkin."
            )
            return
        set_state(oid, tg.id, "PAY_AMOUNT_API")
        await query.message.reply_text("💳 Summani yuboring. Masalan: 50000")
        return


# ============================================================
# PAYMENTS
# ============================================================

async def payment_text(update, context):
    own = context.application.bot_data["owner"]
    oid = own["id"]
    uid = update.effective_user.id
    state, data = get_state(oid, uid)

    if state not in ("PAY_AMOUNT_MANUAL", "PAY_AMOUNT_API"):
        return False

    try:
        amount = float(update.message.text.replace(" ", "").replace(",", ""))
        if amount <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ Summani to‘g‘ri kiriting.")
        return True

    user = ensure_customer(oid, update.effective_user)
    method = "manual" if state == "PAY_AMOUNT_MANUAL" else "api"

    pid = q("""INSERT INTO payments(owner_id,user_id,amount,method,status,created_at)
               VALUES(?,?,?,?,?,?)""",
            (oid,user["id"],amount,method,"pending",now()))
    clear_state(oid, uid)

    if method == "manual":
        await notify_owner_payment(oid, pid)
        await update.message.reply_text(
            f"✅ To‘lov so‘rovi #{pid} yaratildi.\n"
            "👨‍💼 Admin tasdiqlagandan keyin balansingiz to‘ldiriladi."
        )
    else:
        a = owner_api(oid)
        try:
            url = endpoint(a["api_url"], a["payment_url"])
            payload = {
                "payment_id": pid,
                "user_id": update.effective_user.id,
                "amount": amount
            }
            r = await asyncio.to_thread(
                requests.post, url, headers=api_headers(a),
                json=payload, timeout=30
            )
            q("UPDATE payments SET receipt=? WHERE id=?", (r.text[:5000],pid))
            await update.message.reply_text(
                f"🤖 Avtomatik to‘lov yaratildi.\n#{pid}\n\n{r.text[:1500]}"
            )
        except Exception as e:
            q("UPDATE payments SET status='failed',receipt=? WHERE id=?", (str(e),pid))
            await update.message.reply_text(f"❌ To‘lov API xatosi: {e}")
    return True


# ============================================================
# OWNER NOTIFICATIONS
# ============================================================

async def notify_owner(owner_id, order_id, extra=""):
    own = q("SELECT * FROM bot_owners WHERE id=?", (owner_id,), fetchone=True)
    if not own:
        return
    try:
        await master_app.bot.send_message(
            chat_id=own["owner_user_id"],
            text=f"📦 Yangi buyurtma #{order_id}\n\n{extra}\n\n"
                 "⚙️ Bot egasi panelidagi Buyurtmalar bo‘limidan ko‘ring."
        )
    except Exception as e:
        log.warning("owner notify: %s", e)

async def notify_owner_payment(owner_id, payment_id):
    own = q("SELECT * FROM bot_owners WHERE id=?", (owner_id,), fetchone=True)
    if not own:
        return
    p = q("""SELECT p.*,u.telegram_id,u.username
             FROM payments p JOIN users u ON u.id=p.user_id
             WHERE p.id=?""", (payment_id,), fetchone=True)
    try:
        await master_app.bot.send_message(
            chat_id=own["owner_user_id"],
            text=f"💳 Yangi balans to‘ldirish so‘rovi #{payment_id}\n"
                 f"👤 @{p['username'] or '-'} / {p['telegram_id']}\n"
                 f"💰 {p['amount']:,.0f} so‘m\n\n"
                 "Tasdiqlash hozircha admin panel orqali emas, "
                 "bazadagi payment statusi orqali amalga oshiriladi."
        )
    except Exception:
        pass


# ============================================================
# ADMIN / PLATFORM OWNER
# ============================================================

async def admin_master_text(update, context):
    text = (update.message.text or "").strip()
    if text == "🤖 Botimni ulash":
        await update.message.reply_text("Siz platforma adminsiz.")
        return
    if text == "⚙️ Mening panelim":
        rows = q("SELECT * FROM bot_owners ORDER BY id DESC", fetchall=True)
        s = "👑 BOTLAR\n\n"
        if not rows:
            s += "Ulangan botlar yo‘q."
        for r in rows:
            s += f"#{r['id']} @{r['bot_username']} — owner {r['owner_user_id']}\n"
        await update.message.reply_text(s[:4000], reply_markup=master_kb())
        return
    if text == "📖 Qo‘llanma":
        await update.message.reply_text(
            "Platforma admini:\n\n"
            "Ulangan botlarni ko‘rish uchun ⚙️ Mening panelim.\n"
            "Har bir bot egasi o‘z foydalanuvchilari va xizmatlarini alohida boshqaradi."
        )
        return
    if text == "🆘 SOS":
        await update.message.reply_text(f"SOS: {SOS_USERNAME}")
        return
    await update.message.reply_text(
        "👑 Platforma admini\n\n"
        "Ulangan botlarni ko‘rish: ⚙️ Mening panelim",
        reply_markup=master_kb()
    )


# ============================================================
# MULTI BOT START
# ============================================================

async def start_customer_bot(owner_id, token):
    if owner_id in running_bots:
        return

    own = q("SELECT * FROM bot_owners WHERE id=?", (owner_id,), fetchone=True)
    if not own:
        return

    app = Application.builder().token(token).build()
    app.bot_data["owner"] = dict(own)

    async def wrapped_text(update, context):
        # Payment states need priority over generic customer text
        handled = await payment_text(update, context)
        if handled:
            return
        await customer_text(update, context)

    app.add_handler(CommandHandler("start", customer_start))
    app.add_handler(CallbackQueryHandler(customer_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, wrapped_text))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    running_bots[owner_id] = app
    log.info("Customer bot started: @%s", own["bot_username"])

async def start_all_customer_bots():
    rows = q("SELECT * FROM bot_owners WHERE active=1", fetchall=True)
    for r in rows:
        try:
            await start_customer_bot(r["id"], r["bot_token"])
        except Exception as e:
            log.exception("Could not start bot %s: %s", r["id"], e)


# ============================================================
# MAIN
# ============================================================

master_app = None

async def post_init(app):
    global master_app
    master_app = app
    await start_all_customer_bots()

async def post_shutdown(app):
    for oid, a in list(running_bots.items()):
        try:
            await a.updater.stop()
            await a.stop()
            await a.shutdown()
        except Exception:
            pass

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN .env/environment variable is missing.")
    if ADMIN_ID == 0:
        raise RuntimeError("ADMIN_ID .env/environment variable is missing.")

    init_db()

    global master_app
    master_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    master_app.add_handler(CommandHandler("start", master_start))
    master_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, receive_master_text)
    )

    log.info("DONUZ multi-bot platform started.")
    master_app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

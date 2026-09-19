import os
import json
import sqlite3
import logging
import asyncio
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Bot,
    BotCommandScopeDefault,
    MenuButtonDefault,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
DB_FILE = os.getenv("DB_FILE", "donuz.db")
SOS_USERNAME = os.getenv("SOS_USERNAME", "@donuz1").strip()

PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi")

if not ADMIN_ID:
    logging.warning("ADMIN_ID berilmagan")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("DONUZ")


# =========================================================
# WEB SERVICE HEALTH SERVER
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"DONUZ BOT OK")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def log_message(self, format, *args):
        pass


def start_health_server():
    try:
        server = HTTPServer(
            ("0.0.0.0", PORT),
            HealthHandler,
        )

        logger.info(f"Health server started on 0.0.0.0:{PORT}")

        server.serve_forever()

    except Exception as e:
        logger.exception(f"Health server error: {e}")


# =========================================================
# DATABASE
# =========================================================

def get_db():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False,
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    return conn


def init_db():

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_owners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL UNIQUE,
            bot_token TEXT NOT NULL UNIQUE,
            bot_id INTEGER,
            bot_username TEXT,
            bot_name TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_owner_id INTEGER NOT NULL,
            telegram_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            balance REAL DEFAULT 0,
            joined_at TEXT NOT NULL,
            UNIQUE(bot_owner_id, telegram_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            price REAL DEFAULT 0,
            api_mode TEXT DEFAULT 'manual',
            api_url TEXT DEFAULT '',
            api_action TEXT DEFAULT '',
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_owner_id INTEGER NOT NULL UNIQUE,
            api_url TEXT DEFAULT '',
            api_key TEXT DEFAULT '',
            api_header TEXT DEFAULT 'Authorization',
            api_prefix TEXT DEFAULT 'Bearer',
            updated_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_owner_id INTEGER NOT NULL,
            user_telegram_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            quantity INTEGER DEFAULT 1,
            amount REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            api_response TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_owner_id INTEGER NOT NULL,
            user_telegram_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'pending',
            receipt TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS states (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            bot_owner_id INTEGER,
            state TEXT NOT NULL,
            data TEXT DEFAULT '{}',
            UNIQUE(user_id, bot_owner_id)
        )
    """)

    conn.commit()
    conn.close()

    logger.info("Database initialized")


# =========================================================
# STATE
# =========================================================

def set_state(user_id, bot_owner_id, state, data=None):

    data = data or {}

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO states
        (user_id, bot_owner_id, state, data)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, bot_owner_id)
        DO UPDATE SET
            state=excluded.state,
            data=excluded.data
    """, (
        user_id,
        bot_owner_id,
        state,
        json.dumps(data, ensure_ascii=False),
    ))

    conn.commit()
    conn.close()


def get_state(user_id, bot_owner_id):

    conn = get_db()

    row = conn.execute("""
        SELECT state, data
        FROM states
        WHERE user_id=? AND bot_owner_id=?
    """, (
        user_id,
        bot_owner_id,
    )).fetchone()

    conn.close()

    if not row:
        return None, {}

    try:
        data = json.loads(row["data"])
    except Exception:
        data = {}

    return row["state"], data


def clear_state(user_id, bot_owner_id):

    conn = get_db()

    conn.execute("""
        DELETE FROM states
        WHERE user_id=? AND bot_owner_id=?
    """, (
        user_id,
        bot_owner_id,
    ))

    conn.commit()
    conn.close()


# =========================================================
# KEYBOARDS
# =========================================================

def master_keyboard():

    return ReplyKeyboardMarkup(
        [
            ["🤖 Botimni ulash"],
            ["⚙️ Mening panelim"],
            ["📖 Qo‘llanma", "🆘 SOS"],
        ],
        resize_keyboard=True,
    )


def owner_keyboard():

    return ReplyKeyboardMarkup(
        [
            ["🛒 Xizmatlarim", "➕ Xizmat qo‘shish"],
            ["📦 Buyurtmalar", "👥 Foydalanuvchilar"],
            ["🔌 API ulash", "💰 API Balans"],
            ["🔄 Katalog yangilash"],
            ["⚙️ API sozlamalari"],
            ["💳 To‘lovlar"],
            ["🆘 SOS"],
            ["⬅️ Asosiy menyu"],
        ],
        resize_keyboard=True,
    )


def customer_keyboard():

    return ReplyKeyboardMarkup(
        [
            ["🛒 Xizmatlar"],
            ["💰 Balans", "➕ Balans to‘ldirish"],
            ["📦 Buyurtmalarim"],
            ["🆘 SOS"],
        ],
        resize_keyboard=True,
    )


# =========================================================
# TELEGRAM MENU HIDE
# =========================================================

async def hide_bot_commands(bot):

    try:
        await bot.delete_my_commands(
            scope=BotCommandScopeDefault()
        )

        await bot.set_chat_menu_button(
            menu_button=MenuButtonDefault()
        )

    except Exception as e:
        logger.warning(
            f"Bot menu sozlamasida xato: {e}"
        )


# =========================================================
# MASTER / OWNER HELPERS
# =========================================================

def get_owner_by_user(user_id):

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM bot_owners
        WHERE owner_user_id=?
    """, (user_id,)).fetchone()

    conn.close()

    return row


def get_owner_by_token(token):

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM bot_owners
        WHERE bot_token=?
    """, (token,)).fetchone()

    conn.close()

    return row


def get_owner(owner_id):

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM bot_owners
        WHERE id=?
    """, (owner_id,)).fetchone()

    conn.close()

    return row


def now():

    return datetime.now(
        timezone.utc
    ).isoformat()


# =========================================================
# MASTER START
# =========================================================

async def master_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    clear_state(user.id, None)

    await update.message.reply_text(
        "Assalomu alaykum! 👋\n\n"
        "🐷 DONUZ botimizga xush kelibsiz!\n\n"
        "Bu bot orqali o‘zingizga shaxsiy Telegram bot "
        "yaratishingiz va uni boshqarishingiz mumkin.\n\n"
        "🤖 Botni ulang\n"
        "🛒 Xizmatlaringizni boshqaring\n"
        "🔌 API ulang\n"
        "📦 Buyurtmalarni kuzating\n"
        "👥 Foydalanuvchilarni boshqaring\n"
        "💳 To‘lovlarni nazorat qiling\n"
        "🔄 Katalogni yangilang\n\n"
        "Boshlash uchun quyidagi menyudan foydalaning.",
        reply_markup=master_keyboard(),
    )


# =========================================================
# CONNECT BOT
# =========================================================

async def connect_bot_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    existing = get_owner_by_user(user.id)

    if existing:

        await update.message.reply_text(
            "Sizda allaqachon ulangan bot mavjud. 🤖\n\n"
            "Boshqarish uchun «⚙️ Mening panelim» "
            "tugmasidan foydalaning.",
            reply_markup=master_keyboard(),
        )

        return

    set_state(
        user.id,
        None,
        "WAIT_BOT_TOKEN",
    )

    await update.message.reply_text(
        "🤖 Bot ulash\n\n"
        "BotFather orqali olgan bot tokeningizni yuboring.\n\n"
        "Masalan:\n"
        "`123456789:AA...`",
        parse_mode="Markdown",
    )


async def process_bot_token(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    token: str,
):

    user = update.effective_user

    token = token.strip()

    if not token:

        await update.message.reply_text(
            "❌ Token bo‘sh bo‘lishi mumkin emas."
        )

        return

    if get_owner_by_token(token):

        await update.message.reply_text(
            "❌ Bu bot allaqachon DONUZ platformasiga ulangan."
        )

        clear_state(user.id, None)

        return

    try:

        async with Bot(token=token) as bot:

            me = await bot.get_me()

    except Exception:

        await update.message.reply_text(
            "❌ Bot token noto‘g‘ri yoki botga ulanib bo‘lmadi.\n\n"
            "BotFather'dan tokenni qayta tekshirib yuboring."
        )

        return

    try:

        conn = get_db()

        cur = conn.cursor()

        cur.execute("""
            INSERT INTO bot_owners
            (
                owner_user_id,
                bot_token,
                bot_id,
                bot_username,
                bot_name,
                active,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?)
        """, (
            user.id,
            token,
            me.id,
            me.username or "",
            me.first_name or "",
            now(),
        ))

        owner_id = cur.lastrowid

        conn.commit()
        conn.close()

        clear_state(user.id, None)

        await update.message.reply_text(
            "✅ Bot muvaffaqiyatli ulandi!\n\n"
            f"🤖 Bot: @{me.username or me.first_name}\n\n"
            "Endi botingizni quyidagi panel orqali boshqarishingiz mumkin.",
            reply_markup=owner_keyboard(),
        )

        await start_customer_bot(owner_id)

    except sqlite3.IntegrityError:

        await update.message.reply_text(
            "❌ Bu bot yoki sizning akkauntingiz "
            "allaqachon ulangan."
        )

    except Exception as e:

        logger.exception(
            f"Bot ulashda xato: {e}"
        )

        await update.message.reply_text(
            "❌ Botni ulashda xatolik yuz berdi."
        )


# =========================================================
# MASTER TEXT
# =========================================================

async def receive_master_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user
    text = (update.message.text or "").strip()

    state, data = get_state(
        user.id,
        None,
    )

    if state == "WAIT_BOT_TOKEN":

        await process_bot_token(
            update,
            context,
            text,
        )

        return

    owner = get_owner_by_user(user.id)

    if text == "🤖 Botimni ulash":

        await connect_bot_start(
            update,
            context,
        )

        return

    if text == "⚙️ Mening panelim":

        if not owner:

            await update.message.reply_text(
                "Sizda hali ulangan bot yo‘q. 🤖\n\n"
                "Avval «🤖 Botimni ulash» tugmasini bosing.",
                reply_markup=master_keyboard(),
            )

            return

        await update.message.reply_text(
            owner_panel_text(owner["id"]),
            reply_markup=owner_keyboard(),
        )

        return

    if text == "📖 Qo‘llanma":

        await update.message.reply_text(
            "📖 DONUZ qo‘llanmasi\n\n"
            "1️⃣ «🤖 Botimni ulash» orqali BotFather tokenini yuboring.\n\n"
            "2️⃣ Bot ulanganidan keyin «⚙️ Mening panelim»ga kiring.\n\n"
            "3️⃣ Xizmat qo‘shing.\n\n"
            "4️⃣ API sozlamalarini kiriting.\n\n"
            "5️⃣ Foydalanuvchilar botingiz orqali xizmat sotib oladi.\n\n"
            "6️⃣ Buyurtmalar va to‘lovlarni paneldan boshqarasiz.",
            reply_markup=master_keyboard(),
        )

        return

    if text == "🆘 SOS":

        await update.message.reply_text(
            f"🆘 Yordam kerakmi?\n\n"
            f"Admin: {SOS_USERNAME}",
            reply_markup=master_keyboard(),
        )

        return

    await update.message.reply_text(
        "Menyudan kerakli bo‘limni tanlang.",
        reply_markup=master_keyboard(),
    )


# =========================================================
# OWNER PANEL
# =========================================================

def owner_panel_text(owner_id):

    conn = get_db()

    owner = conn.execute("""
        SELECT *
        FROM bot_owners
        WHERE id=?
    """, (owner_id,)).fetchone()

    services = conn.execute("""
        SELECT COUNT(*) AS c
        FROM services
        WHERE bot_owner_id=?
    """, (owner_id,)).fetchone()["c"]

    users = conn.execute("""
        SELECT COUNT(*) AS c
        FROM users
        WHERE bot_owner_id=?
    """, (owner_id,)).fetchone()["c"]

    orders = conn.execute("""
        SELECT COUNT(*) AS c
        FROM orders
        WHERE bot_owner_id=?
    """, (owner_id,)).fetchone()["c"]

    conn.close()

    if not owner:
        return "❌ Bot topilmadi."

    return (
        "⚙️ DONUZ boshqaruv paneli\n\n"
        f"🤖 Bot: @{owner['bot_username'] or '-'}\n"
        f"📦 Xizmatlar: {services}\n"
        f"👥 Foydalanuvchilar: {users}\n"
        f"🧾 Buyurtmalar: {orders}\n\n"
        "Kerakli bo‘limni tanlang."
    )


async def owner_state_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
):

    user = update.effective_user
    text = (update.message.text or "").strip()

    state, data = get_state(
        user.id,
        owner_id,
    )

    # -----------------------------------------------------
    # SERVICE NAME
    # -----------------------------------------------------

    if state == "SERVICE_NAME":

        data["name"] = text

        set_state(
            user.id,
            owner_id,
            "SERVICE_DESCRIPTION",
            data,
        )

        await update.message.reply_text(
            "📝 Xizmat tavsifini yuboring.\n\n"
            "Agar kerak bo‘lmasa: -"
        )

        return True

    # -----------------------------------------------------
    # SERVICE DESCRIPTION
    # -----------------------------------------------------

    if state == "SERVICE_DESCRIPTION":

        data["description"] = (
            "" if text == "-" else text
        )

        set_state(
            user.id,
            owner_id,
            "SERVICE_PRICE",
            data,
        )

        await update.message.reply_text(
            "💰 Xizmat narxini UZSda yuboring.\n\n"
            "Masalan: 15000"
        )

        return True

    # -----------------------------------------------------
    # SERVICE PRICE
    # -----------------------------------------------------

    if state == "SERVICE_PRICE":

        try:

            price = float(
                text.replace(",", ".")
            )

            if price <= 0:
                raise ValueError

        except Exception:

            await update.message.reply_text(
                "❌ Narx noto‘g‘ri.\n"
                "Masalan: 15000"
            )

            return True

        data["price"] = price

        set_state(
            user.id,
            owner_id,
            "SERVICE_MODE",
            data,
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔌 API orqali",
                    callback_data="smode:api",
                ),
                InlineKeyboardButton(
                    "👨‍💼 Manual",
                    callback_data="smode:manual",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔄 API + Manual",
                    callback_data="smode:both",
                )
            ]
        ])

        await update.message.reply_text(
            "⚙️ Xizmat yetkazib berish usulini tanlang:",
            reply_markup=keyboard,
        )

        return True

    # -----------------------------------------------------
    # API ACTION
    # -----------------------------------------------------

    if state == "SERVICE_ACTION":

        data["api_action"] = text

        conn = get_db()

        conn.execute("""
            INSERT INTO services
            (
                bot_owner_id,
                name,
                description,
                price,
                api_mode,
                api_action,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            owner_id,
            data.get("name", ""),
            data.get("description", ""),
            data.get("price", 0),
            data.get("api_mode", "manual"),
            data.get("api_action", ""),
            now(),
        ))

        conn.commit()
        conn.close()

        clear_state(
            user.id,
            owner_id,
        )

        await update.message.reply_text(
            "✅ Xizmat muvaffaqiyatli qo‘shildi!",
            reply_markup=owner_keyboard(),
        )

        return True

    # -----------------------------------------------------
    # API URL
    # -----------------------------------------------------

    if state == "API_URL":

        data["api_url"] = text

        set_state(
            user.id,
            owner_id,
            "API_KEY",
            data,
        )

        await update.message.reply_text(
            "🔑 API key/tokenni yuboring:"
        )

        return True

    # -----------------------------------------------------
    # API KEY
    # -----------------------------------------------------

    if state == "API_KEY":

        data["api_key"] = text

        set_state(
            user.id,
            owner_id,
            "API_HEADER",
            data,
        )

        await update.message.reply_text(
            "📌 API header nomini yuboring.\n\n"
            "Odatda:\n"
            "Authorization\n\n"
            "Agar X-API-Key bo‘lsa:\n"
            "X-API-Key"
        )

        return True

    # -----------------------------------------------------
    # API HEADER
    # -----------------------------------------------------

    if state == "API_HEADER":

        data["api_header"] = text

        set_state(
            user.id,
            owner_id,
            "API_PREFIX",
            data,
        )

        await update.message.reply_text(
            "🔐 API prefixni yuboring.\n\n"
            "Authorization uchun:\n"
            "Bearer\n\n"
            "X-API-Key uchun:\n"
            "bo‘sh qoldirish mumkin emas — none deb yuboring."
        )

        return True

    # -----------------------------------------------------
    # API PREFIX
    # -----------------------------------------------------

    if state == "API_PREFIX":

        data["api_prefix"] = (
            "" if text.lower() == "none" else text
        )

        conn = get_db()

        conn.execute("""
            INSERT INTO api_settings
            (
                bot_owner_id,
                api_url,
                api_key,
                api_header,
                api_prefix,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(bot_owner_id)
            DO UPDATE SET
                api_url=excluded.api_url,
                api_key=excluded.api_key,
                api_header=excluded.api_header,
                api_prefix=excluded.api_prefix,
                updated_at=excluded.updated_at
        """, (
            owner_id,
            data.get("api_url", ""),
            data.get("api_key", ""),
            data.get("api_header", "Authorization"),
            data.get("api_prefix", ""),
            now(),
        ))

        conn.commit()
        conn.close()

        clear_state(
            user.id,
            owner_id,
        )

        await update.message.reply_text(
            "✅ API sozlamalari saqlandi!",
            reply_markup=owner_keyboard(),
        )

        return True

    return False


# =========================================================
# API HELPERS
# =========================================================

def get_api_settings(owner_id):

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM api_settings
        WHERE bot_owner_id=?
    """, (owner_id,)).fetchone()

    conn.close()

    return row


def build_api_headers(settings):

    if not settings:
        return {}

    header = settings["api_header"] or "Authorization"
    key = settings["api_key"] or ""
    prefix = settings["api_prefix"] or ""

    if prefix:

        value = f"{prefix} {key}"

    else:

        value = key

    return {
        header: value,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def api_get(owner_id, endpoint=""):

    settings = get_api_settings(owner_id)

    if not settings:
        return {
            "ok": False,
            "error": "API sozlanmagan",
        }

    base_url = (
        settings["api_url"] or ""
    ).rstrip("/")

    url = base_url + "/" + endpoint.lstrip("/")

    try:

        response = requests.get(
            url,
            headers=build_api_headers(settings),
            timeout=20,
        )

        try:
            data = response.json()
        except Exception:
            data = response.text

        return {
            "ok": response.ok,
            "status": response.status_code,
            "data": data,
        }

    except Exception as e:

        return {
            "ok": False,
            "error": str(e),
        }


def api_post(owner_id, endpoint="", payload=None):

    settings = get_api_settings(owner_id)

    if not settings:
        return {
            "ok": False,
            "error": "API sozlanmagan",
        }

    base_url = (
        settings["api_url"] or ""
    ).rstrip("/")

    url = base_url + "/" + endpoint.lstrip("/")

    try:

        response = requests.post(
            url,
            headers=build_api_headers(settings),
            json=payload or {},
            timeout=30,
        )

        try:
            data = response.json()
        except Exception:
            data = response.text

        return {
            "ok": response.ok,
            "status": response.status_code,
            "data": data,
        }

    except Exception as e:

        return {
            "ok": False,
            "error": str(e),
        }


# =========================================================
# API BALANCE
# =========================================================

async def api_balance(
    update,
    owner_id,
):

    result = api_get(
        owner_id,
        "/balance",
    )

    if not result["ok"]:

        await update.message.reply_text(
            "❌ API balansini olishda xatolik.\n\n"
            f"{result.get('error', 'API xatosi')}"
        )

        return

    data = result.get("data")

    await update.message.reply_text(
        "💰 API balans\n\n"
        f"{json.dumps(data, ensure_ascii=False, indent=2)[:3500]}"
    )


# =========================================================
# CATALOG REFRESH
# =========================================================

async def refresh_catalog(
    update,
    owner_id,
):

    result = api_get(
        owner_id,
        "/catalog",
    )

    if not result["ok"]:

        await update.message.reply_text(
            "❌ Katalogni yangilab bo‘lmadi.\n\n"
            f"{result.get('error', 'API xatosi')}"
        )

        return

    await update.message.reply_text(
        "✅ API katalogiga so‘rov yuborildi.\n\n"
        "API formatiga qarab katalog ma’lumotlari qaytarildi."
    )


# =========================================================
# OWNER TEXT
# =========================================================

async def receive_owner_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
):

    user = update.effective_user
    text = (update.message.text or "").strip()

    handled = await owner_state_handler(
        update,
        context,
        owner_id,
    )

    if handled:
        return

    if text == "🛒 Xizmatlarim":

        conn = get_db()

        rows = conn.execute("""
            SELECT *
            FROM services
            WHERE bot_owner_id=?
            ORDER BY id DESC
        """, (owner_id,)).fetchall()

        conn.close()

        if not rows:

            await update.message.reply_text(
                "🛒 Hozircha xizmatlar yo‘q.",
                reply_markup=owner_keyboard(),
            )

            return

        lines = [
            "🛒 Xizmatlaringiz:\n"
        ]

        for row in rows:

            lines.append(
                f"#{row['id']} — {row['name']}\n"
                f"💰 {row['price']:,.0f} UZS\n"
                f"⚙️ {row['api_mode']}\n"
            )

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=owner_keyboard(),
        )

        return

    if text == "➕ Xizmat qo‘shish":

        set_state(
            user.id,
            owner_id,
            "SERVICE_NAME",
        )

        await update.message.reply_text(
            "➕ Yangi xizmat\n\n"
            "Xizmat nomini yuboring:"
        )

        return

    if text == "📦 Buyurtmalar":

        conn = get_db()

        rows = conn.execute("""
            SELECT
                o.*,
                s.name
            FROM orders o
            LEFT JOIN services s
                ON s.id=o.service_id
            WHERE o.bot_owner_id=?
            ORDER BY o.id DESC
            LIMIT 20
        """, (owner_id,)).fetchall()

        conn.close()

        if not rows:

            await update.message.reply_text(
                "📦 Hozircha buyurtmalar yo‘q.",
                reply_markup=owner_keyboard(),
            )

            return

        lines = ["📦 Oxirgi buyurtmalar:\n"]

        for row in rows:

            lines.append(
                f"#{row['id']} — {row['name'] or '-'}\n"
                f"👤 {row['user_telegram_id']}\n"
                f"💰 {row['amount']:,.0f} UZS\n"
                f"📌 {row['status']}\n"
            )

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=owner_keyboard(),
        )

        return

    if text == "👥 Foydalanuvchilar":

        conn = get_db()

        rows = conn.execute("""
            SELECT *
            FROM users
            WHERE bot_owner_id=?
            ORDER BY id DESC
            LIMIT 50
        """, (owner_id,)).fetchall()

        conn.close()

        if not rows:

            await update.message.reply_text(
                "👥 Hozircha foydalanuvchilar yo‘q.",
                reply_markup=owner_keyboard(),
            )

            return

        lines = ["👥 Foydalanuvchilar:\n"]

        for row in rows:

            username = (
                f"@{row['username']}"
                if row["username"]
                else "-"
            )

            lines.append(
                f"ID: {row['telegram_id']}\n"
                f"Username: {username}\n"
                f"Balans: {row['balance']:,.0f} UZS\n"
                f"Qo‘shilgan: {row['joined_at']}\n"
            )

        await update.message.reply_text(
            "\n".join(lines)[:4000],
            reply_markup=owner_keyboard(),
        )

        return

    if text == "🔌 API ulash":

        set_state(
            user.id,
            owner_id,
            "API_URL",
        )

        await update.message.reply_text(
            "🔌 API ulash\n\n"
            "API Base URL manzilini yuboring.\n\n"
            "Masalan:\n"
            "https://example.com/api/v1"
        )

        return

    if text == "💰 API Balans":

        await api_balance(
            update,
            owner_id,
        )

        return

    if text == "🔄 Katalog yangilash":

        await refresh_catalog(
            update,
            owner_id,
        )

        return

    if text == "⚙️ API sozlamalari":

        settings = get_api_settings(
            owner_id
        )

        if not settings:

            await update.message.reply_text(
                "⚙️ API hali sozlanmagan.\n\n"
                "«🔌 API ulash» bo‘limidan sozlang.",
                reply_markup=owner_keyboard(),
            )

            return

        await update.message.reply_text(
            "⚙️ API sozlamalari\n\n"
            f"URL: {settings['api_url']}\n"
            f"Header: {settings['api_header']}\n"
            f"Prefix: {settings['api_prefix'] or '-'}",
            reply_markup=owner_keyboard(),
        )

        return

    if text == "💳 To‘lovlar":

        conn = get_db()

        rows = conn.execute("""
            SELECT *
            FROM payments
            WHERE bot_owner_id=?
            ORDER BY id DESC
            LIMIT 20
        """, (owner_id,)).fetchall()

        conn.close()

        if not rows:

            await update.message.reply_text(
                "💳 Hozircha to‘lovlar yo‘q.",
                reply_markup=owner_keyboard(),
            )

            return

        lines = ["💳 To‘lovlar:\n"]

        for row in rows:

            lines.append(
                f"#{row['id']}\n"
                f"👤 {row['user_telegram_id']}\n"
                f"💰 {row['amount']:,.0f} UZS\n"
                f"📌 {row['status']}\n"
            )

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=owner_keyboard(),
        )

        return

    if text == "🆘 SOS":

        await update.message.reply_text(
            f"🆘 Yordam:\n\n{SOS_USERNAME}",
            reply_markup=owner_keyboard(),
        )

        return

    if text == "⬅️ Asosiy menyu":

        await update.message.reply_text(
            "Asosiy menyu:",
            reply_markup=master_keyboard(),
        )

        return

    await update.message.reply_text(
        "Panel menyusidan foydalaning.",
        reply_markup=owner_keyboard(),
    )


# =========================================================
# SERVICE MODE CALLBACK
# =========================================================

async def owner_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
):

    query = update.callback_query

    await query.answer()

    user = query.from_user
    data = query.data or ""

    if data.startswith("smode:"):

        mode = data.split(":", 1)[1]

        state, saved = get_state(
            user.id,
            owner_id,
        )

        if state != "SERVICE_MODE":

            await query.edit_message_text(
                "❌ Bu amalning vaqti tugagan."
            )

            return

        saved["api_mode"] = mode

        if mode in ("api", "both"):

            set_state(
                user.id,
                owner_id,
                "SERVICE_ACTION",
                saved,
            )

            await query.edit_message_text(
                "🔌 API action/endpoint nomini yuboring.\n\n"
                "Masalan:\n"
                "top-up\n"
                "order\n"
                "buy"
            )

            return

        conn = get_db()

        conn.execute("""
            INSERT INTO services
            (
                bot_owner_id,
                name,
                description,
                price,
                api_mode,
                api_action,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            owner_id,
            saved.get("name", ""),
            saved.get("description", ""),
            saved.get("price", 0),
            mode,
            "",
            now(),
        ))

        conn.commit()
        conn.close()

        clear_state(
            user.id,
            owner_id,
        )

        await query.edit_message_text(
            "✅ Xizmat muvaffaqiyatli qo‘shildi!"
        )


# =========================================================
# CUSTOMER HELPERS
# =========================================================

def get_or_create_customer(
    owner_id,
    telegram_user,
):

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE bot_owner_id=? AND telegram_id=?
    """, (
        owner_id,
        telegram_user.id,
    )).fetchone()

    if not row:

        conn.execute("""
            INSERT INTO users
            (
                bot_owner_id,
                telegram_id,
                username,
                first_name,
                balance,
                joined_at
            )
            VALUES (?, ?, ?, ?, 0, ?)
        """, (
            owner_id,
            telegram_user.id,
            telegram_user.username or "",
            telegram_user.first_name or "",
            now(),
        ))

        conn.commit()

        row = conn.execute("""
            SELECT *
            FROM users
            WHERE bot_owner_id=? AND telegram_id=?
        """, (
            owner_id,
            telegram_user.id,
        )).fetchone()

    else:

        conn.execute("""
            UPDATE users
            SET username=?, first_name=?
            WHERE bot_owner_id=? AND telegram_id=?
        """, (
            telegram_user.username or "",
            telegram_user.first_name or "",
            owner_id,
            telegram_user.id,
        ))

        conn.commit()

        row = conn.execute("""
            SELECT *
            FROM users
            WHERE bot_owner_id=? AND telegram_id=?
        """, (
            owner_id,
            telegram_user.id,
        )).fetchone()

    conn.close()

    return row


# =========================================================
# CUSTOMER START
# =========================================================

async def customer_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
):

    user = update.effective_user

    get_or_create_customer(
        owner_id,
        user,
    )

    await update.message.reply_text(
        "Assalomu alaykum! 👋\n\n"
        "Xush kelibsiz!\n\n"
        "Kerakli xizmatni menyudan tanlang.",
        reply_markup=customer_keyboard(),
    )


# =========================================================
# CUSTOMER TEXT
# =========================================================

async def customer_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
):

    user = update.effective_user
    text = (update.message.text or "").strip()

    customer = get_or_create_customer(
        owner_id,
        user,
    )

    if text == "🛒 Xizmatlar":

        conn = get_db()

        rows = conn.execute("""
            SELECT *
            FROM services
            WHERE bot_owner_id=? AND active=1
            ORDER BY id
        """, (owner_id,)).fetchall()

        conn.close()

        if not rows:

            await update.message.reply_text(
                "🛒 Hozircha xizmatlar mavjud emas.",
                reply_markup=customer_keyboard(),
            )

            return

        keyboard = []

        for row in rows:

            keyboard.append([
                InlineKeyboardButton(
                    f"{row['name']} — {row['price']:,.0f} UZS",
                    callback_data=f"service:{row['id']}",
                )
            ])

        await update.message.reply_text(
            "🛒 Xizmatni tanlang:",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            ),
        )

        return

    if text == "💰 Balans":

        await update.message.reply_text(
            f"💰 Sizning balansingiz:\n\n"
            f"{customer['balance']:,.0f} UZS",
            reply_markup=customer_keyboard(),
        )

        return

    if text == "➕ Balans to‘ldirish":

        set_state(
            user.id,
            owner_id,
            "PAYMENT_AMOUNT",
        )

        await update.message.reply_text(
            "➕ Balans to‘ldirish\n\n"
            "To‘ldirmoqchi bo‘lgan summani yuboring.\n\n"
            "Masalan: 50000"
        )

        return

    if text == "📦 Buyurtmalarim":

        conn = get_db()

        rows = conn.execute("""
            SELECT
                o.*,
                s.name
            FROM orders o
            LEFT JOIN services s
                ON s.id=o.service_id
            WHERE o.bot_owner_id=?
              AND o.user_telegram_id=?
            ORDER BY o.id DESC
            LIMIT 20
        """, (
            owner_id,
            user.id,
        )).fetchall()

        conn.close()

        if not rows:

            await update.message.reply_text(
                "📦 Hozircha buyurtmalar yo‘q.",
                reply_markup=customer_keyboard(),
            )

            return

        lines = ["📦 Buyurtmalaringiz:\n"]

        for row in rows:

            lines.append(
                f"#{row['id']} — {row['name'] or '-'}\n"
                f"💰 {row['amount']:,.0f} UZS\n"
                f"📌 {row['status']}\n"
            )

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=customer_keyboard(),
        )

        return

    if text == "🆘 SOS":

        await update.message.reply_text(
            f"🆘 Yordam:\n\n{SOS_USERNAME}",
            reply_markup=customer_keyboard(),
        )

        return

    state, data = get_state(
        user.id,
        owner_id,
    )

    if state == "PAYMENT_AMOUNT":

        try:

            amount = float(
                text.replace(",", ".")
            )

            if amount <= 0:
                raise ValueError

        except Exception:

            await update.message.reply_text(
                "❌ Summani noto‘g‘ri yubordingiz."
            )

            return

        data["amount"] = amount

        set_state(
            user.id,
            owner_id,
            "PAYMENT_RECEIPT",
            data,
        )

        await update.message.reply_text(
            f"💰 To‘lov summasi: {amount:,.0f} UZS\n\n"
            "To‘lovni amalga oshiring va chek/rasmni yuboring."
        )

        return

    if state == "PAYMENT_RECEIPT":

        receipt = text

        amount = float(
            data.get("amount", 0)
        )

        conn = get_db()

        conn.execute("""
            INSERT INTO payments
            (
                bot_owner_id,
                user_telegram_id,
                amount,
                status,
                receipt,
                created_at
            )
            VALUES (?, ?, ?, 'pending', ?, ?)
        """, (
            owner_id,
            user.id,
            amount,
            receipt,
            now(),
        ))

        conn.commit()
        conn.close()

        clear_state(
            user.id,
            owner_id,
        )

        await update.message.reply_text(
            "✅ To‘lov ma’lumoti qabul qilindi.\n\n"
            "Admin tasdiqlaganidan keyin balansingizga qo‘shiladi.",
            reply_markup=customer_keyboard(),
        )

        return

    await update.message.reply_text(
        "Menyudan kerakli bo‘limni tanlang.",
        reply_markup=customer_keyboard(),
    )


# =========================================================
# CUSTOMER CALLBACK
# =========================================================

async def customer_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
):

    query = update.callback_query

    await query.answer()

    user = query.from_user
    data = query.data or ""

    if data.startswith("service:"):

        service_id = int(
            data.split(":", 1)[1]
        )

        conn = get_db()

        service = conn.execute("""
            SELECT *
            FROM services
            WHERE id=?
              AND bot_owner_id=?
              AND active=1
        """, (
            service_id,
            owner_id,
        )).fetchone()

        conn.close()

        if not service:

            await query.edit_message_text(
                "❌ Xizmat topilmadi."
            )

            return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🛒 Sotib olish",
                    callback_data=f"buy:{service_id}",
                )
            ]
        ])

        await query.edit_message_text(
            f"🛒 {service['name']}\n\n"
            f"{service['description'] or 'Tavsif mavjud emas'}\n\n"
            f"💰 Narx: {service['price']:,.0f} UZS",
            reply_markup=keyboard,
        )

        return

    if data.startswith("buy:"):

        service_id = int(
            data.split(":", 1)[1]
        )

        conn = get_db()

        service = conn.execute("""
            SELECT *
            FROM services
            WHERE id=?
              AND bot_owner_id=?
              AND active=1
        """, (
            service_id,
            owner_id,
        )).fetchone()

        user_row = conn.execute("""
            SELECT *
            FROM users
            WHERE bot_owner_id=?
              AND telegram_id=?
        """, (
            owner_id,
            user.id,
        )).fetchone()

        conn.close()

        if not service or not user_row:

            await query.edit_message_text(
                "❌ Ma’lumot topilmadi."
            )

            return

        price = float(
            service["price"]
        )

        balance = float(
            user_row["balance"]
        )

        if balance < price:

            await query.edit_message_text(
                "❌ Balansingiz yetarli emas.\n\n"
                f"💰 Narx: {price:,.0f} UZS\n"
                f"💳 Balans: {balance:,.0f} UZS"
            )

            return

        conn = get_db()

        conn.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE bot_owner_id=?
              AND telegram_id=?
        """, (
            price,
            owner_id,
            user.id,
        ))

        cur = conn.execute("""
            INSERT INTO orders
            (
                bot_owner_id,
                user_telegram_id,
                service_id,
                quantity,
                amount,
                status,
                created_at
            )
            VALUES (?, ?, ?, 1, ?, 'pending', ?)
        """, (
            owner_id,
            user.id,
            service_id,
            price,
            now(),
        ))

        order_id = cur.lastrowid

        conn.commit()
        conn.close()

        status = "pending"

        if service["api_mode"] in (
            "api",
            "both",
        ):

            result = api_post(
                owner_id,
                service["api_action"],
                {
                    "user_id": user.id,
                    "quantity": 1,
                    "order_id": order_id,
                },
            )

            conn = get_db()

            if result["ok"]:

                status = "completed"

                conn.execute("""
                    UPDATE orders
                    SET status='completed',
                        api_response=?
                    WHERE id=?
                """, (
                    json.dumps(
                        result.get("data"),
                        ensure_ascii=False,
                    )[:10000],
                    order_id,
                ))

            elif service["api_mode"] == "api":

                status = "failed"

                conn.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE bot_owner_id=?
                      AND telegram_id=?
                """, (
                    price,
                    owner_id,
                    user.id,
                ))

                conn.execute("""
                    UPDATE orders
                    SET status='failed',
                        api_response=?
                    WHERE id=?
                """, (
                    json.dumps(
                        result,
                        ensure_ascii=False,
                    )[:10000],
                    order_id,
                ))

            else:

                status = "manual"

                conn.execute("""
                    UPDATE orders
                    SET status='manual',
                        api_response=?
                    WHERE id=?
                """, (
                    json.dumps(
                        result,
                        ensure_ascii=False,
                    )[:10000],
                    order_id,
                ))

            conn.commit()
            conn.close()

        else:

            conn = get_db()

            conn.execute("""
                UPDATE orders
                SET status='manual'
                WHERE id=?
            """, (order_id,))

            conn.commit()
            conn.close()

        if status == "completed":

            message = (
                "✅ Buyurtma muvaffaqiyatli bajarildi!\n\n"
                f"🧾 Buyurtma: #{order_id}"
            )

        elif status == "failed":

            message = (
                "❌ Buyurtma bajarilmadi.\n\n"
                "Balansingiz qaytarildi."
            )

        else:

            message = (
                "✅ Buyurtma qabul qilindi!\n\n"
                f"🧾 Buyurtma: #{order_id}\n"
                "📌 Holat: Kutilmoqda"
            )

        await query.edit_message_text(
            message
        )


# =========================================================
# START CUSTOMER BOT
# =========================================================

customer_apps = {}
customer_locks = {}


async def start_customer_bot(owner_id):

    if owner_id in customer_apps:

        return

    lock = customer_locks.setdefault(
        owner_id,
        asyncio.Lock(),
    )

    async with lock:

        if owner_id in customer_apps:
            return

        owner = get_owner(owner_id)

        if not owner:
            return

        if not owner["active"]:
            return

        token = owner["bot_token"]

        try:

            app = (
                Application.builder()
                .token(token)
                .build()
            )

            async def start_handler(
                update,
                context,
            ):
                await customer_start(
                    update,
                    context,
                    owner_id,
                )

            async def text_handler(
                update,
                context,
            ):
                await customer_text(
                    update,
                    context,
                    owner_id,
                )

            async def callback_handler(
                update,
                context,
            ):
                await customer_callback(
                    update,
                    context,
                    owner_id,
                )

            app.add_handler(
                CommandHandler(
                    "start",
                    start_handler,
                )
            )

            app.add_handler(
                CallbackQueryHandler(
                    callback_handler
                )
            )

            app.add_handler(
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    text_handler,
                )
            )

            await app.initialize()

            await hide_bot_commands(
                app.bot
            )

            await app.start()

            if app.updater:

                await app.updater.start_polling(
                    drop_pending_updates=True
                )

            customer_apps[owner_id] = app

            logger.info(
                f"Customer bot started: {owner['bot_username']}"
            )

        except Exception as e:

            logger.exception(
                f"Customer bot start error "
                f"{owner_id}: {e}"
            )


# =========================================================
# START ALL CUSTOMER BOTS
# =========================================================

async def start_all_customer_bots():

    conn = get_db()

    rows = conn.execute("""
        SELECT id
        FROM bot_owners
        WHERE active=1
    """).fetchall()

    conn.close()

    for row in rows:

        try:

            await start_customer_bot(
                row["id"]
            )

        except Exception as e:

            logger.exception(
                f"Saved bot start error: {e}"
            )


# =========================================================
# POST INIT
# =========================================================

async def post_init(
    application: Application,
):

    await hide_bot_commands(
        application.bot
    )

    await start_all_customer_bots()


# =========================================================
# POST SHUTDOWN
# =========================================================

async def post_shutdown(
    application: Application,
):

    logger.info(
        "DONUZ shutting down..."
    )

    for owner_id, app in list(
        customer_apps.items()
    ):

        try:

            if app.updater:
                await app.updater.stop()

            await app.stop()
            await app.shutdown()

        except Exception as e:

            logger.warning(
                f"Customer bot shutdown error: {e}"
            )

    customer_apps.clear()


# =========================================================
# MAIN
# =========================================================

def main():

    # -----------------------------------------------------
    # START RENDER HEALTH SERVER
    # -----------------------------------------------------

    health_thread = threading.Thread(
        target=start_health_server,
        daemon=True,
    )

    health_thread.start()

    # -----------------------------------------------------
    # DATABASE
    # -----------------------------------------------------

    init_db()

    # -----------------------------------------------------
    # MASTER BOT
    # -----------------------------------------------------

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # -----------------------------------------------------
    # MASTER HANDLERS
    # -----------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            master_start,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_master_text,
        )
    )

    # -----------------------------------------------------
    # OWNER CALLBACKS
    # -----------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            lambda update, context: owner_callback_router(
                update,
                context,
            )
        )
    )

    # -----------------------------------------------------
    # RUN POLLING
    # -----------------------------------------------------

    logger.info(
        "DONUZ master bot starting..."
    )

    application.run_polling(
        drop_pending_updates=True
    )


# =========================================================
# OWNER CALLBACK ROUTER
# =========================================================

async def owner_callback_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if not query:
        return

    user_id = query.from_user.id

    owner = get_owner_by_user(
        user_id
    )

    if not owner:
        return

    await owner_callback(
        update,
        context,
        owner["id"],
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()

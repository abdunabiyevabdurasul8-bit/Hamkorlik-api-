import os
import json
import sqlite3
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from telegram import (
    Update,
    Bot,
    BotCommandScopeDefault,
    MenuButtonDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
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

# Render Web Service PORT
PORT = int(os.getenv("PORT", "10000"))


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("DONUZ")


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi")


# =========================================================
# WEB SERVICE HEALTH SERVER
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )
        self.end_headers()
        self.wfile.write(b"DONUZ BOT OK")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )
        self.end_headers()

    def log_message(self, format, *args):
        pass


def start_health_server():

    try:
        server = HTTPServer(
            ("0.0.0.0", PORT),
            HealthHandler
        )

        logger.info(
            f"Health server started on port {PORT}"
        )

        server.serve_forever()

    except Exception as e:
        logger.exception(
            f"Health server error: {e}"
        )


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

    conn.execute(
        "PRAGMA journal_mode=WAL"
    )

    conn.execute(
        "PRAGMA busy_timeout=30000"
    )

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
# TIME
# =========================================================

def now():
    return datetime.now(
        timezone.utc
    ).isoformat()


# =========================================================
# STATES
# =========================================================

def set_state(
    user_id,
    owner_id,
    state,
    data=None
):

    data = data or {}

    conn = get_db()

    conn.execute("""
        INSERT INTO states
        (
            user_id,
            bot_owner_id,
            state,
            data
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, bot_owner_id)
        DO UPDATE SET
            state=excluded.state,
            data=excluded.data
    """, (
        user_id,
        owner_id,
        state,
        json.dumps(
            data,
            ensure_ascii=False
        ),
    ))

    conn.commit()
    conn.close()


def get_state(
    user_id,
    owner_id
):

    conn = get_db()

    row = conn.execute("""
        SELECT state, data
        FROM states
        WHERE user_id=?
          AND bot_owner_id IS ?
    """, (
        user_id,
        owner_id,
    )).fetchone()

    conn.close()

    if not row:
        return None, {}

    try:
        data = json.loads(
            row["data"]
        )
    except Exception:
        data = {}

    return row["state"], data


def clear_state(
    user_id,
    owner_id
):

    conn = get_db()

    conn.execute("""
        DELETE FROM states
        WHERE user_id=?
          AND bot_owner_id IS ?
    """, (
        user_id,
        owner_id,
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
# HIDE BOT COMMANDS
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
            f"Telegram menu setup error: {e}"
        )


# =========================================================
# OWNER HELPERS
# =========================================================

def get_owner_by_user(
    user_id
):

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM bot_owners
        WHERE owner_user_id=?
    """, (
        user_id,
    )).fetchone()

    conn.close()

    return row


def get_owner_by_id(
    owner_id
):

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM bot_owners
        WHERE id=?
    """, (
        owner_id,
    )).fetchone()

    conn.close()

    return row


def get_owner_by_token(
    token
):

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM bot_owners
        WHERE bot_token=?
    """, (
        token,
    )).fetchone()

    conn.close()

    return row


# =========================================================
# MASTER START
# =========================================================

async def master_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    clear_state(
        user.id,
        None
    )

    await update.message.reply_text(
        "Assalomu alaykum! 👋\n\n"
        "DONUZ botimizga xush kelibsiz!\n\n"
        "Bu bot orqali o‘zingizga shaxsiy Telegram "
        "bot yaratishingiz mumkin.\n\n"
        "🤖 Shaxsiy bot ulash\n"
        "🛒 Xizmatlar qo‘shish\n"
        "🔌 API ulash\n"
        "📦 Buyurtmalarni boshqarish\n"
        "👥 Foydalanuvchilarni ko‘rish\n"
        "💳 To‘lovlarni nazorat qilish\n"
        "🔄 Katalogni yangilash",
        reply_markup=master_keyboard(),
    )


# =========================================================
# CONNECT BOT
# =========================================================

async def connect_bot_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    owner = get_owner_by_user(
        user.id
    )

    if owner:

        await update.message.reply_text(
            "🤖 Sizning botingiz allaqachon ulangan.",
            reply_markup=owner_keyboard(),
        )

        return

    set_state(
        user.id,
        None,
        "WAIT_BOT_TOKEN"
    )

    await update.message.reply_text(
        "🤖 Bot ulash\n\n"
        "BotFather bergan tokenni yuboring."
    )


# =========================================================
# PROCESS BOT TOKEN
# =========================================================

async def process_bot_token(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    token
):

    user = update.effective_user

    token = token.strip()

    if not token:

        await update.message.reply_text(
            "❌ Token bo‘sh."
        )

        return

    if get_owner_by_token(token):

        clear_state(
            user.id,
            None
        )

        await update.message.reply_text(
            "❌ Bu bot allaqachon ulangan."
        )

        return

    try:

        async with Bot(
            token=token
        ) as bot:

            me = await bot.get_me()

    except Exception:

        await update.message.reply_text(
            "❌ Token noto‘g‘ri.\n\n"
            "BotFather tokenini tekshirib qayta yuboring."
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

        clear_state(
            user.id,
            None
        )

        await update.message.reply_text(
            "✅ Bot muvaffaqiyatli ulandi!\n\n"
            f"🤖 Bot: @{me.username or me.first_name}\n\n"
            "⚙️ Boshqaruv paneli:",
            reply_markup=owner_keyboard(),
        )

        await start_customer_bot(
            owner_id
        )

    except sqlite3.IntegrityError:

        await update.message.reply_text(
            "❌ Bu bot yoki akkaunt allaqachon ulangan."
        )

    except Exception as e:

        logger.exception(
            f"Bot connection error: {e}"
        )

        await update.message.reply_text(
            "❌ Botni ulashda xatolik yuz berdi."
        )


# =========================================================
# MASTER TEXT
# =========================================================

async def receive_master_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    text = (
        update.message.text or ""
    ).strip()

    state, data = get_state(
        user.id,
        None
    )

    if state == "WAIT_BOT_TOKEN":

        await process_bot_token(
            update,
            context,
            text
        )

        return

    owner = get_owner_by_user(
        user.id
    )

    if text == "🤖 Botimni ulash":

        await connect_bot_start(
            update,
            context
        )

        return

    if text == "⚙️ Mening panelim":

        if not owner:

            await update.message.reply_text(
                "🤖 Avval botingizni ulang.",
                reply_markup=master_keyboard(),
            )

            return

        await update.message.reply_text(
            owner_panel_text(
                owner["id"]
            ),
            reply_markup=owner_keyboard(),
        )

        return

    if text == "📖 Qo‘llanma":

        await update.message.reply_text(
            "📖 DONUZ qo‘llanmasi\n\n"
            "1. 🤖 Botimni ulash\n"
            "2. BotFather tokenini yuborish\n"
            "3. ⚙️ Mening panelim\n"
            "4. ➕ Xizmat qo‘shish\n"
            "5. 🔌 API ulash\n"
            "6. 🔄 Katalog yangilash\n"
            "7. Buyurtmalarni boshqarish",
            reply_markup=master_keyboard(),
        )

        return

    if text == "🆘 SOS":

        await update.message.reply_text(
            f"🆘 Yordam: {SOS_USERNAME}",
            reply_markup=master_keyboard(),
        )

        return

    if owner:

        await receive_owner_text(
            update,
            context,
            owner["id"]
        )

        return

    await update.message.reply_text(
        "DONUZ menyusi:",
        reply_markup=master_keyboard(),
    )


# =========================================================
# OWNER PANEL
# =========================================================

def owner_panel_text(
    owner_id
):

    conn = get_db()

    owner = conn.execute("""
        SELECT *
        FROM bot_owners
        WHERE id=?
    """, (
        owner_id,
    )).fetchone()

    services = conn.execute("""
        SELECT COUNT(*) AS total
        FROM services
        WHERE bot_owner_id=?
    """, (
        owner_id,
    )).fetchone()["total"]

    users = conn.execute("""
        SELECT COUNT(*) AS total
        FROM users
        WHERE bot_owner_id=?
    """, (
        owner_id,
    )).fetchone()["total"]

    orders = conn.execute("""
        SELECT COUNT(*) AS total
        FROM orders
        WHERE bot_owner_id=?
    """, (
        owner_id,
    )).fetchone()["total"]

    conn.close()

    if not owner:
        return "❌ Bot topilmadi."

    return (
        "⚙️ Boshqaruv paneli\n\n"
        f"🤖 Bot: @{owner['bot_username'] or '-'}\n"
        f"🛒 Xizmatlar: {services}\n"
        f"👥 Foydalanuvchilar: {users}\n"
        f"📦 Buyurtmalar: {orders}"
    )


# =========================================================
# OWNER STATE
# =========================================================

async def owner_state_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id
):

    user = update.effective_user

    text = (
        update.message.text or ""
    ).strip()

    state, data = get_state(
        user.id,
        owner_id
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
            data
        )

        await update.message.reply_text(
            "📝 Xizmat tavsifini yuboring.\n\n"
            "Kerak bo‘lmasa: -"
        )

        return True

    # -----------------------------------------------------
    # DESCRIPTION
    # -----------------------------------------------------

    if state == "SERVICE_DESCRIPTION":

        data["description"] = (
            ""
            if text == "-"
            else text
        )

        set_state(
            user.id,
            owner_id,
            "SERVICE_PRICE",
            data
        )

        await update.message.reply_text(
            "💰 Narxni UZSda yuboring.\n\n"
            "Masalan: 15000"
        )

        return True

    # -----------------------------------------------------
    # PRICE
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
                "❌ Narx noto‘g‘ri."
            )

            return True

        data["price"] = price

        set_state(
            user.id,
            owner_id,
            "SERVICE_MODE",
            data
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔌 API",
                    callback_data="smode:api"
                ),
                InlineKeyboardButton(
                    "👨‍💼 Manual",
                    callback_data="smode:manual"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔄 API + Manual",
                    callback_data="smode:both"
                )
            ]
        ])

        await update.message.reply_text(
            "⚙️ Yetkazib berish usulini tanlang:",
            reply_markup=keyboard
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
            owner_id
        )

        await update.message.reply_text(
            "✅ Xizmat qo‘shildi.",
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
            data
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
            data
        )

        await update.message.reply_text(
            "📌 Header nomini yuboring.\n\n"
            "Authorization yoki X-API-Key"
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
            data
        )

        await update.message.reply_text(
            "🔐 Prefixni yuboring.\n\n"
            "Bearer yoki none"
        )

        return True

    # -----------------------------------------------------
    # API PREFIX
    # -----------------------------------------------------

    if state == "API_PREFIX":

        data["api_prefix"] = (
            ""
            if text.lower() == "none"
            else text
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
            data.get(
                "api_header",
                "Authorization"
            ),
            data.get(
                "api_prefix",
                ""
            ),
            now(),
        ))

        conn.commit()
        conn.close()

        clear_state(
            user.id,
            owner_id
        )

        await update.message.reply_text(
            "✅ API sozlamalari saqlandi.",
            reply_markup=owner_keyboard(),
        )

        return True

    return False


# =========================================================
# API SETTINGS
# =========================================================

def get_api_settings(
    owner_id
):

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM api_settings
        WHERE bot_owner_id=?
    """, (
        owner_id,
    )).fetchone()

    conn.close()

    return row


def build_api_headers(
    settings
):

    if not settings:
        return {}

    header = (
        settings["api_header"]
        or "Authorization"
    )

    key = (
        settings["api_key"]
        or ""
    )

    prefix = (
        settings["api_prefix"]
        or ""
    )

    value = (
        f"{prefix} {key}"
        if prefix
        else key
    )

    return {
        header: value,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def api_get(
    owner_id,
    endpoint
):

    settings = get_api_settings(
        owner_id
    )

    if not settings:

        return {
            "ok": False,
            "error": "API sozlanmagan"
        }

    base_url = (
        settings["api_url"]
        or ""
    ).rstrip("/")

    url = (
        base_url
        + "/"
        + endpoint.lstrip("/")
    )

    try:

        response = requests.get(
            url,
            headers=build_api_headers(
                settings
            ),
            timeout=20,
        )

        try:
            result = response.json()
        except Exception:
            result = response.text

        return {
            "ok": response.ok,
            "status": response.status_code,
            "data": result
        }

    except Exception as e:

        return {
            "ok": False,
            "error": str(e)
        }


def api_post(
    owner_id,
    endpoint,
    payload=None
):

    settings = get_api_settings(
        owner_id
    )

    if not settings:

        return {
            "ok": False,
            "error": "API sozlanmagan"
        }

    base_url = (
        settings["api_url"]
        or ""
    ).rstrip("/")

    url = (
        base_url
        + "/"
        + endpoint.lstrip("/")
    )

    try:

        response = requests.post(
            url,
            headers=build_api_headers(
                settings
            ),
            json=payload or {},
            timeout=30,
        )

        try:
            result = response.json()
        except Exception:
            result = response.text

        return {
            "ok": response.ok,
            "status": response.status_code,
            "data": result
        }

    except Exception as e:

        return {
            "ok": False,
            "error": str(e)
        }


# =========================================================
# API BALANCE
# =========================================================

async def show_api_balance(
    update,
    owner_id
):

    result = api_get(
        owner_id,
        "balance"
    )

    if not result["ok"]:

        await update.message.reply_text(
            "❌ API balansini olish imkoni bo‘lmadi."
        )

        return

    await update.message.reply_text(
        "💰 API balans\n\n"
        + json.dumps(
            result["data"],
            ensure_ascii=False,
            indent=2
        )[:3500]
    )


# =========================================================
# CATALOG
# =========================================================

async def refresh_catalog(
    update,
    owner_id
):

    result = api_get(
        owner_id,
        "catalog"
    )

    if not result["ok"]:

        await update.message.reply_text(
            "❌ Katalogni yangilab bo‘lmadi."
        )

        return

    await update.message.reply_text(
        "✅ Katalog yangilandi."
    )


# =========================================================
# OWNER TEXT
# =========================================================

async def receive_owner_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id
):

    user = update.effective_user

    text = (
        update.message.text or ""
    ).strip()

    handled = await owner_state_handler(
        update,
        context,
        owner_id
    )

    if handled:
        return

    # -----------------------------------------------------
    # SERVICES
    # -----------------------------------------------------

    if text == "🛒 Xizmatlarim":

        conn = get_db()

        rows = conn.execute("""
            SELECT *
            FROM services
            WHERE bot_owner_id=?
            ORDER BY id DESC
        """, (
            owner_id,
        )).fetchall()

        conn.close()

        if not rows:

            await update.message.reply_text(
                "🛒 Xizmatlar hali yo‘q.",
                reply_markup=owner_keyboard(),
            )

            return

        result = "🛒 Xizmatlar:\n\n"

        for row in rows:

            result += (
                f"#{row['id']} {row['name']}\n"
                f"💰 {row['price']:,.0f} UZS\n"
                f"⚙️ {row['api_mode']}\n\n"
            )

        await update.message.reply_text(
            result[:4000],
            reply_markup=owner_keyboard(),
        )

        return

    # -----------------------------------------------------
    # ADD SERVICE
    # -----------------------------------------------------

    if text == "➕ Xizmat qo‘shish":

        set_state(
            user.id,
            owner_id,
            "SERVICE_NAME"
        )

        await update.message.reply_text(
            "➕ Xizmat nomini yuboring:"
        )

        return

    # -----------------------------------------------------
    # ORDERS
    # -----------------------------------------------------

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
            LIMIT 30
        """, (
            owner_id,
        )).fetchall()

        conn.close()

        if not rows:

            await update.message.reply_text(
                "📦 Buyurtmalar hali yo‘q.",
                reply_markup=owner_keyboard(),
            )

            return

        result = "📦 Buyurtmalar:\n\n"

        for row in rows:

            result += (
                f"#{row['id']} — "
                f"{row['name'] or '-'}\n"
                f"👤 {row['user_telegram_id']}\n"
                f"💰 {row['amount']:,.0f} UZS\n"
                f"📌 {row['status']}\n\n"
            )

        await update.message.reply_text(
            result[:4000],
            reply_markup=owner_keyboard(),
        )

        return

    # -----------------------------------------------------
    # USERS
    # -----------------------------------------------------

    if text == "👥 Foydalanuvchilar":

        conn = get_db()

        rows = conn.execute("""
            SELECT *
            FROM users
            WHERE bot_owner_id=?
            ORDER BY id DESC
            LIMIT 50
        """, (
            owner_id,
        )).fetchall()

        conn.close()

        if not rows:

            await update.message.reply_text(
                "👥 Foydalanuvchilar hali yo‘q.",
                reply_markup=owner_keyboard(),
            )

            return

        result = "👥 Foydalanuvchilar:\n\n"

        for row in rows:

            username = (
                "@"
                + row["username"]
                if row["username"]
                else "-"
            )

            result += (
                f"🆔 {row['telegram_id']}\n"
                f"👤 {username}\n"
                f"💰 {row['balance']:,.0f} UZS\n"
                f"📅 {row['joined_at']}\n\n"
            )

        await update.message.reply_text(
            result[:4000],
            reply_markup=owner_keyboard(),
        )

        return

    # -----------------------------------------------------
    # API CONNECT
    # -----------------------------------------------------

    if text == "🔌 API ulash":

        set_state(
            user.id,
            owner_id,
            "API_URL"
        )

        await update.message.reply_text(
            "🔌 API Base URL yuboring:"
        )

        return

    # -----------------------------------------------------
    # API BALANCE
    # -----------------------------------------------------

    if text == "💰 API Balans":

        await show_api_balance(
            update,
            owner_id
        )

        return

    # -----------------------------------------------------
    # REFRESH
    # -----------------------------------------------------

    if text == "🔄 Katalog yangilash":

        await refresh_catalog(
            update,
            owner_id
        )

        return

    # -----------------------------------------------------
    # API SETTINGS
    # -----------------------------------------------------

    if text == "⚙️ API sozlamalari":

        settings = get_api_settings(
            owner_id
        )

        if not settings:

            await update.message.reply_text(
                "⚙️ API hali ulanmagan.",
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

    # -----------------------------------------------------
    # PAYMENTS
    # -----------------------------------------------------

    if text == "💳 To‘lovlar":

        conn = get_db()

        rows = conn.execute("""
            SELECT *
            FROM payments
            WHERE bot_owner_id=?
            ORDER BY id DESC
            LIMIT 30
        """, (
            owner_id,
        )).fetchall()

        conn.close()

        if not rows:

            await update.message.reply_text(
                "💳 To‘lovlar hali yo‘q.",
                reply_markup=owner_keyboard(),
            )

            return

        result = "💳 To‘lovlar:\n\n"

        for row in rows:

            result += (
                f"#{row['id']}\n"
                f"👤 {row['user_telegram_id']}\n"
                f"💰 {row['amount']:,.0f} UZS\n"
                f"📌 {row['status']}\n\n"
            )

        await update.message.reply_text(
            result[:4000],
            reply_markup=owner_keyboard(),
        )

        return

    # -----------------------------------------------------
    # SOS
    # -----------------------------------------------------

    if text == "🆘 SOS":

        await update.message.reply_text(
            f"🆘 {SOS_USERNAME}",
            reply_markup=owner_keyboard(),
        )

        return

    # -----------------------------------------------------
    # BACK
    # -----------------------------------------------------

    if text == "⬅️ Asosiy menyu":

        await update.message.reply_text(
            "DONUZ",
            reply_markup=master_keyboard(),
        )

        return


# =========================================================
# OWNER CALLBACK
# =========================================================

async def owner_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id
):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    data = query.data or ""

    if not data.startswith("smode:"):
        return

    mode = data.split(
        ":",
        1
    )[1]

    state, saved = get_state(
        user.id,
        owner_id
    )

    if state != "SERVICE_MODE":

        await query.edit_message_text(
            "❌ Amal muddati tugagan."
        )

        return

    saved["api_mode"] = mode

    if mode in (
        "api",
        "both"
    ):

        set_state(
            user.id,
            owner_id,
            "SERVICE_ACTION",
            saved
        )

        await query.edit_message_text(
            "🔌 API endpoint/action yuboring.\n\n"
            "Masalan:\n"
            "buy\n"
            "order\n"
            "top-up"
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
        owner_id
    )

    await query.edit_message_text(
        "✅ Xizmat qo‘shildi."
    )


# =========================================================
# CUSTOMER
# =========================================================

def get_or_create_customer(
    owner_id,
    user
):

    conn = get_db()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE bot_owner_id=?
          AND telegram_id=?
    """, (
        owner_id,
        user.id,
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
            user.id,
            user.username or "",
            user.first_name or "",
            now(),
        ))

        conn.commit()

    else:

        conn.execute("""
            UPDATE users
            SET username=?,
                first_name=?
            WHERE bot_owner_id=?
              AND telegram_id=?
        """, (
            user.username or "",
            user.first_name or "",
            owner_id,
            user.id,
        ))

        conn.commit()

    row = conn.execute("""
        SELECT *
        FROM users
        WHERE bot_owner_id=?
          AND telegram_id=?
    """, (
        owner_id,
        user.id,
    )).fetchone()

    conn.close()

    return row


# =========================================================
# CUSTOMER START
# =========================================================

async def customer_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id
):

    user = update.effective_user

    get_or_create_customer(
        owner_id,
        user
    )

    await update.message.reply_text(
        "Assalomu alaykum! 👋\n\n"
        "Xush kelibsiz!",
        reply_markup=customer_keyboard(),
    )


# =========================================================
# CUSTOMER TEXT
# =========================================================

async def customer_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id
):

    user = update.effective_user

    text = (
        update.message.text or ""
    ).strip()

    customer = get_or_create_customer(
        owner_id,
        user
    )

    # -----------------------------------------------------
    # SERVICES
    # -----------------------------------------------------

    if text == "🛒 Xizmatlar":

        conn = get_db()

        rows = conn.execute("""
            SELECT *
            FROM services
            WHERE bot_owner_id=?
              AND active=1
            ORDER BY id
        """, (
            owner_id,
        )).fetchall()

        conn.close()

        if not rows:

            await update.message.reply_text(
                "🛒 Xizmatlar mavjud emas.",
                reply_markup=customer_keyboard(),
            )

            return

        keyboard = []

        for row in rows:

            keyboard.append([
                InlineKeyboardButton(
                    f"{row['name']} — "
                    f"{row['price']:,.0f} UZS",
                    callback_data=f"service:{row['id']}"
                )
            ])

        await update.message.reply_text(
            "🛒 Xizmatlar:",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            )
        )

        return

    # -----------------------------------------------------
    # BALANCE
    # -----------------------------------------------------

    if text == "💰 Balans":

        await update.message.reply_text(
            f"💰 Balans: "
            f"{customer['balance']:,.0f} UZS",
            reply_markup=customer_keyboard(),
        )

        return

    # -----------------------------------------------------
    # TOP UP
    # -----------------------------------------------------

    if text == "➕ Balans to‘ldirish":

        set_state(
            user.id,
            owner_id,
            "PAYMENT_AMOUNT"
        )

        await update.message.reply_text(
            "💰 To‘ldirmoqchi bo‘lgan summani yuboring."
        )

        return

    # -----------------------------------------------------
    # ORDERS
    # -----------------------------------------------------

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
                "📦 Buyurtmalar yo‘q.",
                reply_markup=customer_keyboard(),
            )

            return

        result = "📦 Buyurtmalar:\n\n"

        for row in rows:

            result += (
                f"#{row['id']} — "
                f"{row['name'] or '-'}\n"
                f"💰 {row['amount']:,.0f} UZS\n"
                f"📌 {row['status']}\n\n"
            )

        await update.message.reply_text(
            result[:4000],
            reply_markup=customer_keyboard(),
        )

        return

    # -----------------------------------------------------
    # SOS
    # -----------------------------------------------------

    if text == "🆘 SOS":

        await update.message.reply_text(
            f"🆘 {SOS_USERNAME}",
            reply_markup=customer_keyboard(),
        )

        return

    # -----------------------------------------------------
    # PAYMENT STATE
    # -----------------------------------------------------

    state, data = get_state(
        user.id,
        owner_id
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
                "❌ Summa noto‘g‘ri."
            )

            return

        data["amount"] = amount

        set_state(
            user.id,
            owner_id,
            "PAYMENT_RECEIPT",
            data
        )

        await update.message.reply_text(
            f"💰 Summa: {amount:,.0f} UZS\n\n"
            "To‘lovni amalga oshirib, "
            "chek yoki to‘lov ma’lumotini yuboring."
        )

        return

    if state == "PAYMENT_RECEIPT":

        amount = float(
            data.get(
                "amount",
                0
            )
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
            text,
            now(),
        ))

        conn.commit()
        conn.close()

        clear_state(
            user.id,
            owner_id
        )

        await update.message.reply_text(
            "✅ To‘lov ma’lumoti qabul qilindi.",
            reply_markup=customer_keyboard(),
        )

        return


# =========================================================
# CUSTOMER CALLBACK
# =========================================================

async def customer_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id
):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    data = query.data or ""

    # -----------------------------------------------------
    # SERVICE
    # -----------------------------------------------------

    if data.startswith("service:"):

        service_id = int(
            data.split(
                ":",
                1
            )[1]
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
                    callback_data=f"buy:{service_id}"
                )
            ]
        ])

        await query.edit_message_text(
            f"🛒 {service['name']}\n\n"
            f"{service['description'] or ''}\n\n"
            f"💰 {service['price']:,.0f} UZS",
            reply_markup=keyboard
        )

        return

    # -----------------------------------------------------
    # BUY
    # -----------------------------------------------------

    if data.startswith("buy:"):

        service_id = int(
            data.split(
                ":",
                1
            )[1]
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

        customer = conn.execute("""
            SELECT *
            FROM users
            WHERE bot_owner_id=?
              AND telegram_id=?
        """, (
            owner_id,
            user.id,
        )).fetchone()

        conn.close()

        if not service or not customer:

            await query.edit_message_text(
                "❌ Ma’lumot topilmadi."
            )

            return

        price = float(
            service["price"]
        )

        balance = float(
            customer["balance"]
        )

        if balance < price:

            await query.edit_message_text(
                "❌ Balans yetarli emas.\n\n"
                f"💰 Narx: {price:,.0f} UZS\n"
                f"💳 Balans: {balance:,.0f} UZS"
            )

            return

        # Deduct balance
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

        status = "manual"

        # -------------------------------------------------
        # API ORDER
        # -------------------------------------------------

        if service["api_mode"] in (
            "api",
            "both"
        ):

            result = api_post(
                owner_id,
                service["api_action"],
                {
                    "user_id": user.id,
                    "quantity": 1,
                    "order_id": order_id,
                }
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
                        ensure_ascii=False
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
                        ensure_ascii=False
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
                        ensure_ascii=False
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
            """, (
                order_id,
            ))

            conn.commit()
            conn.close()

        # -------------------------------------------------
        # RESULT
        # -------------------------------------------------

        if status == "completed":

            message = (
                "✅ Buyurtma bajarildi!\n\n"
                f"🧾 #{order_id}"
            )

        elif status == "failed":

            message = (
                "❌ Buyurtma bajarilmadi.\n\n"
                "💰 Mablag‘ balansga qaytarildi."
            )

        else:

            message = (
                "✅ Buyurtma qabul qilindi!\n\n"
                f"🧾 #{order_id}\n"
                "📌 Kutilmoqda"
            )

        await query.edit_message_text(
            message
        )


# =========================================================
# CUSTOMER BOT STORAGE
# =========================================================

customer_apps = {}


# =========================================================
# START CUSTOMER BOT
# =========================================================

async def start_customer_bot(
    owner_id
):

    if owner_id in customer_apps:
        return

    owner = get_owner_by_id(
        owner_id
    )

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
            context
        ):

            await customer_start(
                update,
                context,
                owner_id
            )

        async def text_handler(
            update,
            context
        ):

            await customer_text(
                update,
                context,
                owner_id
            )

        async def callback_handler(
            update,
            context
        ):

            await customer_callback(
                update,
                context,
                owner_id
            )

        app.add_handler(
            CommandHandler(
                "start",
                start_handler
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
                text_handler
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

        customer_apps[
            owner_id
        ] = app

        logger.info(
            f"Customer bot started: "
            f"@{owner['bot_username']}"
        )

    except Exception as e:

        logger.exception(
            f"Customer bot start error "
            f"{owner_id}: {e}"
        )


# =========================================================
# START SAVED BOTS
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
                f"Saved bot error: {e}"
            )


# =========================================================
# POST INIT
# =========================================================

async def post_init(
    application: Application
):

    await hide_bot_commands(
        application.bot
    )

    await start_all_customer_bots()


# =========================================================
# POST SHUTDOWN
# =========================================================

async def post_shutdown(
    application: Application
):

    logger.info(
        "DONUZ shutting down"
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
                f"Shutdown error: {e}"
            )

    customer_apps.clear()


# =========================================================
# MAIN
# =========================================================

def main():

    # Render PORT server
    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()

    # Database
    init_db()

    # Master application
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # /start
    application.add_handler(
        CommandHandler(
            "start",
            master_start
        )
    )

    # Text
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_master_text
        )
    )

    # Callback
    application.add_handler(
        CallbackQueryHandler(
            owner_callback_router
        )
    )

    logger.info(
        "DONUZ master bot starting..."
    )

    # Telegram polling
    application.run_polling(
        drop_pending_updates=True
    )


# =========================================================
# OWNER CALLBACK ROUTER
# =========================================================

async def owner_callback_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    owner = get_owner_by_user(
        query.from_user.id
    )

    if not owner:
        return

    await owner_callback(
        update,
        context,
        owner["id"]
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()

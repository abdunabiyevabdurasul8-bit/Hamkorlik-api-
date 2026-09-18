import os
import json
import sqlite3
import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIG
# ============================================================

MAIN_BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except Exception:
    ADMIN_ID = 0

PAYMENT_CARD = os.getenv(
    "PAYMENT_CARD",
    "KARTA_RAQAMINI_RENDER_ENVGA_QOYING"
).strip()

DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "bots.db"
    )
)

try:
    PORT = int(os.getenv("PORT", "10000"))
except Exception:
    PORT = 10000


# ============================================================
# SUBSCRIPTION PRICES
# ============================================================

SUBSCRIPTIONS = {
    "1d": {
        "name": "1 kun",
        "days": 1,
        "price": 5000,
    },
    "3d": {
        "name": "3 kun",
        "days": 3,
        "price": 13000,
    },
    "7d": {
        "name": "7 kun",
        "days": 7,
        "price": 25000,
    },
    "1m": {
        "name": "1 oy",
        "days": 30,
        "price": 43000,
    },
}


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# RUNNING CHILD BOTS
# ============================================================

running_bots = {}
running_tasks = {}


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30
    )
    conn.row_factory = sqlite3.Row
    return conn


def init_db():

    db_dir = os.path.dirname(
        os.path.abspath(DB_PATH)
    )

    os.makedirs(
        db_dir,
        exist_ok=True
    )

    conn = db()

    # --------------------------------------------------------
    # USERS
    # --------------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Existing DB migration
    try:
        conn.execute(
            "ALTER TABLE users ADD COLUMN balance INTEGER DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass

    # --------------------------------------------------------
    # BOTS
    # --------------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            bot_id INTEGER,
            username TEXT DEFAULT '',
            name TEXT DEFAULT '',
            start_text TEXT DEFAULT 'Assalomu Aleykum! 👋',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # --------------------------------------------------------
    # SERVICES
    # --------------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            price INTEGER NOT NULL DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # --------------------------------------------------------
    # CUSTOM BUTTONS
    # --------------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS custom_buttons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            active INTEGER DEFAULT 1
        )
    """)

    # --------------------------------------------------------
    # BOT SETTINGS
    # --------------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            bot_id INTEGER PRIMARY KEY,
            welcome_text TEXT DEFAULT 'Assalomu Aleykum! 👋'
        )
    """)

    # --------------------------------------------------------
    # PAYMENTS
    # --------------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            bot_db_id INTEGER DEFAULT 0,
            amount INTEGER NOT NULL DEFAULT 0,
            photo_id TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            source TEXT DEFAULT 'main',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            processed_at TEXT DEFAULT '',
            processed_by INTEGER DEFAULT 0
        )
    """)

    # --------------------------------------------------------
    # SUBSCRIPTIONS
    # --------------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_db_id INTEGER NOT NULL UNIQUE,
            owner_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            start_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # --------------------------------------------------------
    # BALANCE HISTORY
    # --------------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS balance_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            type TEXT NOT NULL,
            description TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # --------------------------------------------------------
    # ORDERS
    # --------------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            bot_db_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT DEFAULT 'paid',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # --------------------------------------------------------
    # SETTINGS
    # --------------------------------------------------------

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        )
    """)

    # Save card if not exists
    conn.execute("""
        INSERT OR IGNORE INTO settings
        (key, value)
        VALUES ('payment_card', ?)
    """, (PAYMENT_CARD,))

    conn.commit()
    conn.close()


# ============================================================
# TIME
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


def parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


# ============================================================
# USER FUNCTIONS
# ============================================================

def save_user(user):

    conn = db()

    conn.execute("""
        INSERT INTO users
        (
            user_id,
            username,
            first_name
        )
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name
    """, (
        user.id,
        user.username or "",
        user.first_name or "",
    ))

    conn.commit()
    conn.close()


def get_balance(user_id):

    conn = db()

    row = conn.execute("""
        SELECT balance
        FROM users
        WHERE user_id = ?
    """, (user_id,)).fetchone()

    conn.close()

    if not row:
        return 0

    return int(row["balance"] or 0)


def change_balance(
    user_id,
    amount,
    history_type,
    description=""
):

    conn = db()

    conn.execute("""
        INSERT INTO users
        (
            user_id,
            balance
        )
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            balance = balance + excluded.balance
    """, (
        user_id,
        amount,
    ))

    conn.execute("""
        INSERT INTO balance_history
        (
            user_id,
            amount,
            type,
            description
        )
        VALUES (?, ?, ?, ?)
    """, (
        user_id,
        amount,
        history_type,
        description,
    ))

    conn.commit()
    conn.close()

    return get_balance(user_id)


# ============================================================
# BOT DATABASE
# ============================================================

def save_bot(
    owner_id,
    token,
    bot_id,
    username,
    name
):

    conn = db()

    conn.execute("""
        INSERT INTO bots
        (
            owner_id,
            token,
            bot_id,
            username,
            name,
            active
        )
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(token) DO UPDATE SET
            owner_id = excluded.owner_id,
            bot_id = excluded.bot_id,
            username = excluded.username,
            name = excluded.name,
            active = 1
    """, (
        owner_id,
        token,
        bot_id,
        username or "",
        name or "",
    ))

    conn.commit()

    row = conn.execute("""
        SELECT id
        FROM bots
        WHERE token = ?
    """, (token,)).fetchone()

    conn.close()

    return row["id"]


def get_bot(bot_db_id):

    conn = db()

    row = conn.execute("""
        SELECT *
        FROM bots
        WHERE id = ?
    """, (bot_db_id,)).fetchone()

    conn.close()

    return row


def get_user_bots(user_id):

    conn = db()

    rows = conn.execute("""
        SELECT *
        FROM bots
        WHERE owner_id = ?
        ORDER BY id DESC
    """, (user_id,)).fetchall()

    conn.close()

    return rows


def get_active_bots():

    conn = db()

    rows = conn.execute("""
        SELECT *
        FROM bots
        WHERE active = 1
    """).fetchall()

    conn.close()

    return rows


def user_owns_bot(user_id, bot_db_id):

    conn = db()

    row = conn.execute("""
        SELECT id
        FROM bots
        WHERE id = ?
        AND owner_id = ?
    """, (
        bot_db_id,
        user_id,
    )).fetchone()

    conn.close()

    return bool(row)


# ============================================================
# SUBSCRIPTION
# ============================================================

def get_subscription(bot_db_id):

    conn = db()

    row = conn.execute("""
        SELECT *
        FROM subscriptions
        WHERE bot_db_id = ?
    """, (bot_db_id,)).fetchone()

    conn.close()

    return row


def subscription_active(bot_db_id):

    sub = get_subscription(bot_db_id)

    if not sub:
        return False

    if not int(sub["active"] or 0):
        return False

    end = parse_time(sub["end_at"])

    if not end:
        return False

    if end <= now_utc():

        conn = db()

        conn.execute("""
            UPDATE subscriptions
            SET active = 0
            WHERE bot_db_id = ?
        """, (bot_db_id,))

        conn.commit()
        conn.close()

        return False

    return True


def subscription_text(bot_db_id):

    sub = get_subscription(bot_db_id)

    if not sub:
        return (
            "🔒 Obuna yo‘q.\n\n"
            "Bot ishlashi uchun obuna sotib oling."
        )

    end = parse_time(sub["end_at"])

    if not end or end <= now_utc():

        return (
            "🔴 Obuna muddati tugagan.\n\n"
            "Bot ishlashi uchun yangi obuna oling."
        )

    remaining = end - now_utc()

    days = remaining.days
    hours = remaining.seconds // 3600

    return (
        f"🟢 Obuna faol\n\n"
        f"📦 Tarif: {SUBSCRIPTIONS.get(sub['plan'], {}).get('name', sub['plan'])}\n"
        f"📅 Tugash vaqti: {end.astimezone().strftime('%Y-%m-%d %H:%M')}\n"
        f"⏳ Qolgan: {days} kun {hours} soat"
    )


def activate_subscription(
    bot_db_id,
    owner_id,
    plan
):

    if plan not in SUBSCRIPTIONS:
        return False, "Tarif topilmadi."

    info = SUBSCRIPTIONS[plan]

    current = get_subscription(bot_db_id)

    current_end = None

    if current:
        current_end = parse_time(
            current["end_at"]
        )

    current_now = now_utc()

    if current_end and current_end > current_now:
        start = current_end
    else:
        start = current_now

    end = start + timedelta(
        days=info["days"]
    )

    conn = db()

    conn.execute("""
        INSERT INTO subscriptions
        (
            bot_db_id,
            owner_id,
            plan,
            start_at,
            end_at,
            active
        )
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(bot_db_id) DO UPDATE SET
            owner_id = excluded.owner_id,
            plan = excluded.plan,
            start_at = excluded.start_at,
            end_at = excluded.end_at,
            active = 1
    """, (
        bot_db_id,
        owner_id,
        plan,
        start.isoformat(),
        end.isoformat(),
    ))

    conn.commit()
    conn.close()

    return True, end


# ============================================================
# SERVICES
# ============================================================

def get_services(bot_db_id):

    conn = db()

    rows = conn.execute("""
        SELECT *
        FROM services
        WHERE bot_id = ?
        AND active = 1
        ORDER BY id ASC
    """, (bot_db_id,)).fetchall()

    conn.close()

    return rows


def get_all_services(bot_db_id):

    conn = db()

    rows = conn.execute("""
        SELECT *
        FROM services
        WHERE bot_id = ?
        ORDER BY id ASC
    """, (bot_db_id,)).fetchall()

    conn.close()

    return rows


def add_service(
    bot_db_id,
    name,
    description,
    price
):

    conn = db()

    cur = conn.execute("""
        INSERT INTO services
        (
            bot_id,
            name,
            description,
            price
        )
        VALUES (?, ?, ?, ?)
    """, (
        bot_db_id,
        name,
        description,
        price,
    ))

    conn.commit()

    service_id = cur.lastrowid

    conn.close()

    return service_id


def update_service(
    service_id,
    name,
    description,
    price
):

    conn = db()

    conn.execute("""
        UPDATE services
        SET
            name = ?,
            description = ?,
            price = ?
        WHERE id = ?
    """, (
        name,
        description,
        price,
        service_id,
    ))

    conn.commit()
    conn.close()


def delete_service(service_id):

    conn = db()

    conn.execute("""
        DELETE FROM services
        WHERE id = ?
    """, (service_id,))

    conn.commit()
    conn.close()


def get_service(service_id):

    conn = db()

    row = conn.execute("""
        SELECT *
        FROM services
        WHERE id = ?
        AND active = 1
    """, (service_id,)).fetchone()

    conn.close()

    return row


# ============================================================
# CUSTOM BUTTONS
# ============================================================

def get_buttons(bot_db_id):

    conn = db()

    rows = conn.execute("""
        SELECT *
        FROM custom_buttons
        WHERE bot_id = ?
        AND active = 1
        ORDER BY id ASC
    """, (bot_db_id,)).fetchall()

    conn.close()

    return rows


def add_custom_button(
    bot_db_id,
    title,
    text
):

    conn = db()

    conn.execute("""
        INSERT INTO custom_buttons
        (
            bot_id,
            title,
            text
        )
        VALUES (?, ?, ?)
    """, (
        bot_db_id,
        title,
        text,
    ))

    conn.commit()
    conn.close()


# ============================================================
# START TEXT
# ============================================================

def get_start_text(bot_db_id):

    conn = db()

    row = conn.execute("""
        SELECT start_text
        FROM bots
        WHERE id = ?
    """, (bot_db_id,)).fetchone()

    conn.close()

    if row and row["start_text"]:
        return row["start_text"]

    return "Assalomu Aleykum! 👋"


def set_start_text(
    bot_db_id,
    text
):

    conn = db()

    conn.execute("""
        UPDATE bots
        SET start_text = ?
        WHERE id = ?
    """, (
        text,
        bot_db_id,
    ))

    conn.commit()
    conn.close()


# ============================================================
# PAYMENT CARD
# ============================================================

def get_payment_card():

    conn = db()

    row = conn.execute("""
        SELECT value
        FROM settings
        WHERE key = 'payment_card'
    """).fetchone()

    conn.close()

    if row and row["value"]:
        return row["value"]

    return PAYMENT_CARD


def set_payment_card(card):

    conn = db()

    conn.execute("""
        INSERT INTO settings
        (key, value)
        VALUES ('payment_card', ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value
    """, (card,))

    conn.commit()
    conn.close()


# ============================================================
# PAYMENTS
# ============================================================

def create_payment(
    user_id,
    bot_db_id,
    amount,
    photo_id,
    source
):

    conn = db()

    cur = conn.execute("""
        INSERT INTO payments
        (
            user_id,
            bot_db_id,
            amount,
            photo_id,
            status,
            source
        )
        VALUES (?, ?, ?, ?, 'pending', ?)
    """, (
        user_id,
        bot_db_id,
        amount,
        photo_id,
        source,
    ))

    conn.commit()

    payment_id = cur.lastrowid

    conn.close()

    return payment_id


def get_payment(payment_id):

    conn = db()

    row = conn.execute("""
        SELECT *
        FROM payments
        WHERE id = ?
    """, (payment_id,)).fetchone()

    conn.close()

    return row


def process_payment(
    payment_id,
    approve,
    admin_id
):

    conn = db()

    row = conn.execute("""
        SELECT *
        FROM payments
        WHERE id = ?
    """, (payment_id,)).fetchone()

    if not row:
        conn.close()
        return None, "not_found"

    if row["status"] != "pending":
        conn.close()
        return row, "already_processed"

    new_status = "approved" if approve else "rejected"

    conn.execute("""
        UPDATE payments
        SET
            status = ?,
            processed_at = ?,
            processed_by = ?
        WHERE id = ?
    """, (
        new_status,
        iso_now(),
        admin_id,
        payment_id,
    ))

    conn.commit()
    conn.close()

    if approve:
        change_balance(
            row["user_id"],
            int(row["amount"]),
            "payment",
            f"To‘lov #{payment_id} tasdiqlandi"
        )

    return row, new_status


# ============================================================
# ORDERS
# ============================================================

def create_order(
    user_id,
    bot_db_id,
    service_id,
    amount
):

    conn = db()

    cur = conn.execute("""
        INSERT INTO orders
        (
            user_id,
            bot_db_id,
            service_id,
            amount,
            status
        )
        VALUES (?, ?, ?, ?, 'paid')
    """, (
        user_id,
        bot_db_id,
        service_id,
        amount,
    ))

    conn.commit()

    order_id = cur.lastrowid

    conn.close()

    return order_id


# ============================================================
# MAIN MENU
# ============================================================

def main_menu():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🤖 Bot yaratish",
                callback_data="create_bot"
            )
        ],
        [
            InlineKeyboardButton(
                "📋 Mening botlarim",
                callback_data="my_bots"
            )
        ],
        [
            InlineKeyboardButton(
                "💰 Balans",
                callback_data="main_balance"
            ),
            InlineKeyboardButton(
                "➕ Balans to‘ldirish",
                callback_data="main_topup"
            )
        ],
        [
            InlineKeyboardButton(
                "💳 Obunalar",
                callback_data="main_subscriptions"
            )
        ],
    ])


# ============================================================
# BOT SETTINGS MENU
# ============================================================

def settings_menu(bot_id):

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🛍 Xizmatlar",
                callback_data=f"services:{bot_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "➕ Xizmat qo‘shish",
                callback_data=f"add_service:{bot_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "✏️ Xizmatni o‘zgartirish",
                callback_data=f"edit_services:{bot_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🗑 Xizmatni o‘chirish",
                callback_data=f"delete_services:{bot_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "👋 Start xabar",
                callback_data=f"start_text:{bot_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🔘 Tugma qo‘shish",
                callback_data=f"add_button:{bot_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "💳 Obuna",
                callback_data=f"bot_subscription:{bot_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "💻 Kod orqali dasturlash",
                callback_data=f"code_mode:{bot_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Orqaga",
                callback_data="my_bots"
            )
        ],
    ])


# ============================================================
# CHILD MENU
# ============================================================

def child_menu(bot_db_id):

    buttons = [
        [
            InlineKeyboardButton(
                "🛍 Xizmatlar",
                callback_data=f"child_services:{bot_db_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "💰 Balans",
                callback_data=f"child_balance:{bot_db_id}"
            ),
            InlineKeyboardButton(
                "➕ Balans to‘ldirish",
                callback_data=f"child_topup:{bot_db_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "💳 Obuna",
                callback_data=f"child_subscription:{bot_db_id}"
            )
        ],
    ]

    custom = get_buttons(bot_db_id)

    for button in custom:

        buttons.append([
            InlineKeyboardButton(
                button["title"],
                callback_data=(
                    f"custom:{button['id']}:"
                    f"{bot_db_id}"
                )
            )
        ])

    return InlineKeyboardMarkup(buttons)


# ============================================================
# SUBSCRIPTION MENU
# ============================================================

def subscription_menu(bot_db_id):

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🗓 1 kun — 5 000 so‘m",
                callback_data=f"buy_sub:1d:{bot_db_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🗓 3 kun — 13 000 so‘m",
                callback_data=f"buy_sub:3d:{bot_db_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🗓 7 kun — 25 000 so‘m",
                callback_data=f"buy_sub:7d:{bot_db_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🗓 1 oy — 43 000 so‘m",
                callback_data=f"buy_sub:1m:{bot_db_id}"
            )
        ],
    ])


# ============================================================
# START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if user:
        save_user(user)

    context.user_data.clear()

    await update.message.reply_text(
        "Assalomu Aleykum! 👋\n\n"
        "🤖 Bu platforma orqali o‘zingizga "
        "Telegram bot yaratishingiz va sozlashingiz mumkin.",
        reply_markup=main_menu()
    )


# ============================================================
# CREATE BOT
# ============================================================

async def create_bot_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    context.user_data.clear()

    context.user_data["state"] = "waiting_bot_token"

    await query.message.reply_text(
        "🚀 Yangi bot yaratish\n\n"
        "1️⃣ @BotFather ni oching.\n"
        "2️⃣ /newbot yuboring.\n"
        "3️⃣ Bot yarating.\n"
        "4️⃣ BotFather bergan tokenni nusxalang.\n\n"
        "📌 Tokenni shu yerga yuboring."
    )


# ============================================================
# CHECK TOKEN
# ============================================================

async def check_token(token):

    temp_app = None

    try:

        temp_app = (
            Application.builder()
            .token(token)
            .build()
        )

        await temp_app.initialize()

        me = await temp_app.bot.get_me()

        return me

    except Exception as e:

        logger.error(
            "Token error: %s",
            e
        )

        return None

    finally:

        if temp_app:

            try:
                await temp_app.shutdown()
            except Exception:
                pass


# ============================================================
# MAIN BALANCE
# ============================================================

async def main_balance_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    balance = get_balance(
        query.from_user.id
    )

    await query.message.reply_text(
        f"💰 Sizning balansingiz:\n\n"
        f"💵 {balance:,} so‘m",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "➕ Balans to‘ldirish",
                    callback_data="main_topup"
                )
            ]
        ])
    )


# ============================================================
# TOPUP START
# ============================================================

async def topup_start(
    update,
    context,
    bot_db_id=0,
    source="main"
):

    context.user_data.clear()

    context.user_data["state"] = "topup_amount"

    context.user_data["payment_bot_id"] = bot_db_id

    context.user_data["payment_source"] = source

    card = get_payment_card()

    await update.message.reply_text(
        "➕ Balans to‘ldirish\n\n"
        "💳 Karta:\n"
        f"`{card}`\n\n"
        "💰 Qancha summa to‘ldirmoqchisiz?\n\n"
        "Masalan: 25000",
        parse_mode="Markdown"
    )


# ============================================================
# MAIN TOPUP
# ============================================================

async def main_topup_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    context.user_data.clear()

    context.user_data["state"] = "topup_amount"

    context.user_data["payment_bot_id"] = 0

    context.user_data["payment_source"] = "main"

    card = get_payment_card()

    await query.message.reply_text(
        "➕ Balans to‘ldirish\n\n"
        "💳 Karta:\n"
        f"`{card}`\n\n"
        "💰 Summani yuboring.\n\n"
        "Masalan: 25000",
        parse_mode="Markdown"
    )


# ============================================================
# MAIN SUBSCRIPTIONS
# ============================================================

async def main_subscriptions_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    bots = get_user_bots(
        query.from_user.id
    )

    if not bots:

        await query.message.reply_text(
            "❌ Avval bot yarating."
        )

        return

    buttons = []

    for bot in bots:

        status = (
            "🟢"
            if subscription_active(bot["id"])
            else "🔴"
        )

        buttons.append([
            InlineKeyboardButton(
                f"{status} @{bot['username'] or bot['name']}",
                callback_data=f"bot_subscription:{bot['id']}"
            )
        ])

    await query.message.reply_text(
        "💳 Qaysi bot uchun obuna olasiz?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# BOT SUBSCRIPTION
# ============================================================

async def bot_subscription_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    bot_db_id = int(
        query.data.split(":")[1]
    )

    if not user_owns_bot(
        query.from_user.id,
        bot_db_id
    ):
        await query.message.reply_text(
            "❌ Bu bot sizniki emas."
        )
        return

    bot = get_bot(bot_db_id)

    await query.message.reply_text(
        f"💳 {bot['name']} uchun obuna\n\n"
        f"{subscription_text(bot_db_id)}\n\n"
        "👇 Tarifni tanlang:",
        reply_markup=subscription_menu(bot_db_id)
    )


# ============================================================
# BUY SUBSCRIPTION
# ============================================================

async def buy_subscription_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    parts = query.data.split(":")

    plan = parts[1]
    bot_db_id = int(parts[2])

    if not user_owns_bot(
        query.from_user.id,
        bot_db_id
    ):
        await query.message.reply_text(
            "❌ Bu bot sizniki emas."
        )
        return

    if plan not in SUBSCRIPTIONS:
        await query.message.reply_text(
            "❌ Tarif topilmadi."
        )
        return

    info = SUBSCRIPTIONS[plan]

    balance = get_balance(
        query.from_user.id
    )

    if balance < info["price"]:

        await query.message.reply_text(
            "❌ Balans yetarli emas.\n\n"
            f"💰 Kerak: {info['price']:,} so‘m\n"
            f"💵 Balansingiz: {balance:,} so‘m\n\n"
            "Avval balansni to‘ldiring.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "➕ Balans to‘ldirish",
                        callback_data="main_topup"
                    )
                ]
            ])
        )

        return

    change_balance(
        query.from_user.id,
        -info["price"],
        "subscription",
        f"{info['name']} obuna"
    )

    ok, end = activate_subscription(
        bot_db_id,
        query.from_user.id,
        plan
    )

    if not ok:

        change_balance(
            query.from_user.id,
            info["price"],
            "refund",
            "Obuna aktivlashtirish qaytarildi"
        )

        await query.message.reply_text(
            "❌ Obunani yoqishda xato."
        )

        return

    await query.message.reply_text(
        "✅ OBUNA FAOLLASHTIRILDI!\n\n"
        f"📦 Tarif: {info['name']}\n"
        f"💰 Narx: {info['price']:,} so‘m\n"
        f"📅 Tugash: "
        f"{end.astimezone().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"💵 Qolgan balans: "
        f"{get_balance(query.from_user.id):,} so‘m"
    )

    bot = get_bot(bot_db_id)

    if bot:

        token = bot["token"]

        if token not in running_bots:

            task = asyncio.create_task(
                run_child_bot(token)
            )

            running_tasks[token] = task


# ============================================================
# RECEIVE MAIN TEXT
# ============================================================

async def receive_main_text(
    update,
    context
):

    if not update.message:
        return

    state = context.user_data.get("state")

    if not state:
        return

    text = update.message.text.strip()

    # --------------------------------------------------------
    # BOT TOKEN
    # --------------------------------------------------------

    if state == "waiting_bot_token":

        if ":" not in text:

            await update.message.reply_text(
                "❌ Token noto‘g‘ri."
            )

            return

        await update.message.reply_text(
            "⏳ Token tekshirilmoqda..."
        )

        me = await check_token(text)

        if not me:

            await update.message.reply_text(
                "❌ Token ishlamadi.\n\n"
                "BotFather tokenini tekshiring."
            )

            return

        bot_db_id = save_bot(
            update.effective_user.id,
            text,
            me.id,
            me.username,
            me.first_name,
        )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ BOT QO‘SHILDI!\n\n"
            f"🤖 Nomi: {me.first_name}\n"
            f"👤 Username: "
            f"@{me.username or 'yo‘q'}\n\n"
            "⚠️ Bot ishlashi uchun obuna sotib oling.",
            reply_markup=settings_menu(bot_db_id)
        )

        return

    # --------------------------------------------------------
    # TOPUP AMOUNT
    # --------------------------------------------------------

    if state == "topup_amount":

        try:

            amount = int(
                text.replace(" ", "")
                .replace(",", "")
            )

            if amount <= 0:
                raise ValueError

        except Exception:

            await update.message.reply_text(
                "❌ Summani faqat raqam bilan yuboring.\n\n"
                "Masalan: 25000"
            )

            return

        context.user_data["topup_amount"] = amount

        context.user_data["state"] = "topup_photo"

        await update.message.reply_text(
            f"💰 Summa: {amount:,} so‘m\n\n"
            "📸 Endi to‘lov chekini RASM qilib yuboring."
        )

        return

    # --------------------------------------------------------
    # SERVICE NAME
    # --------------------------------------------------------

    if state == "service_name":

        context.user_data["service_name"] = text

        context.user_data["state"] = (
            "service_description"
        )

        await update.message.reply_text(
            "📝 Xizmat tavsifini yuboring."
        )

        return

    # --------------------------------------------------------
    # SERVICE DESCRIPTION
    # --------------------------------------------------------

    if state == "service_description":

        context.user_data["service_description"] = text

        context.user_data["state"] = "service_price"

        await update.message.reply_text(
            "💰 Narxni yuboring.\n\n"
            "Masalan: 10000"
        )

        return

    # --------------------------------------------------------
    # SERVICE PRICE
    # --------------------------------------------------------

    if state == "service_price":

        try:

            price = int(
                text.replace(" ", "")
                .replace(",", "")
            )

            if price < 0:
                raise ValueError

        except Exception:

            await update.message.reply_text(
                "❌ Narx noto‘g‘ri."
            )

            return

        bot_id = context.user_data["bot_id"]

        add_service(
            bot_id,
            context.user_data["service_name"],
            context.user_data["service_description"],
            price
        )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ Xizmat qo‘shildi!",
            reply_markup=settings_menu(bot_id)
        )

        return

    # --------------------------------------------------------
    # START TEXT
    # --------------------------------------------------------

    if state == "start_text":

        bot_id = context.user_data["bot_id"]

        set_start_text(
            bot_id,
            text
        )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ Start xabari o‘zgartirildi.",
            reply_markup=settings_menu(bot_id)
        )

        return

    # --------------------------------------------------------
    # BUTTON TITLE
    # --------------------------------------------------------

    if state == "button_title":

        context.user_data["button_title"] = text

        context.user_data["state"] = "button_text"

        await update.message.reply_text(
            "📝 Tugma bosilganda chiqadigan matnni yuboring."
        )

        return

    # --------------------------------------------------------
    # BUTTON TEXT
    # --------------------------------------------------------

    if state == "button_text":

        bot_id = context.user_data["bot_id"]

        add_custom_button(
            bot_id,
            context.user_data["button_title"],
            text
        )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ Tugma qo‘shildi!",
            reply_markup=settings_menu(bot_id)
        )

        return

    # --------------------------------------------------------
    # CODE MODE
    # --------------------------------------------------------

    if state == "code_mode":

        bot_id = context.user_data["bot_id"]

        try:
            data = json.loads(text)
        except Exception:

            await update.message.reply_text(
                "❌ JSON formati noto‘g‘ri."
            )

            return

        if not isinstance(data, dict):

            await update.message.reply_text(
                "❌ JSON obyekt bo‘lishi kerak."
            )

            return

        services = data.get(
            "services",
            []
        )

        added = 0

        if isinstance(services, list):

            for item in services:

                if not isinstance(item, dict):
                    continue

                name = str(
                    item.get("name", "")
                ).strip()

                description = str(
                    item.get("description", "")
                ).strip()

                try:
                    price = int(
                        item.get("price", 0)
                    )
                except Exception:
                    continue

                if not name or price < 0:
                    continue

                add_service(
                    bot_id,
                    name,
                    description,
                    price
                )

                added += 1

        welcome = data.get("welcome")

        if isinstance(welcome, str):

            if welcome.strip():

                set_start_text(
                    bot_id,
                    welcome.strip()
                )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ Kod orqali sozlash tugadi!\n\n"
            f"🛍 {added} ta xizmat qo‘shildi.",
            reply_markup=settings_menu(bot_id)
        )

        return


# ============================================================
# RECEIVE PAYMENT PHOTO
# ============================================================

async def receive_photo(
    update,
    context
):

    state = context.user_data.get("state")

    if state != "topup_photo":
        return

    if not update.message.photo:
        return

    photo = update.message.photo[-1]

    amount = int(
        context.user_data.get(
            "topup_amount",
            0
        )
    )

    bot_db_id = int(
        context.user_data.get(
            "payment_bot_id",
            0
        )
    )

    source = context.user_data.get(
        "payment_source",
        "main"
    )

    payment_id = create_payment(
        update.effective_user.id,
        bot_db_id,
        amount,
        photo.file_id,
        source
    )

    context.user_data.clear()

    await update.message.reply_text(
        "✅ Chek qabul qilindi!\n\n"
        f"💰 Summa: {amount:,} so‘m\n"
        f"🧾 To‘lov ID: #{payment_id}\n\n"
        "⏳ Admin tasdiqlashini kuting."
    )

    # Send to admin through main bot
    if ADMIN_ID and MAIN_BOT_TOKEN:

        try:

            admin_app = (
                Application.builder()
                .token(MAIN_BOT_TOKEN)
                .build()
            )

            await admin_app.initialize()

            user = update.effective_user

            caption = (
                "💳 YANGI TO‘LOV\n\n"
                f"🧾 ID: #{payment_id}\n"
                f"👤 User ID: {user.id}\n"
                f"👤 Username: @{user.username or 'yo‘q'}\n"
                f"💰 Summa: {amount:,} so‘m\n"
                f"📍 Manba: {source}\n"
                f"🤖 Bot ID: {bot_db_id}"
            )

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ TASDIQLASH",
                        callback_data=f"pay_approve:{payment_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ RAD ETISH",
                        callback_data=f"pay_reject:{payment_id}"
                    )
                ]
            ])

            await admin_app.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo.file_id,
                caption=caption,
                reply_markup=keyboard
            )

            await admin_app.shutdown()

        except Exception as e:

            logger.exception(
                "Admin notification error: %s",
                e
            )


# ============================================================
# MY BOTS
# ============================================================

async def my_bots_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    bots = get_user_bots(
        query.from_user.id
    )

    if not bots:

        await query.message.reply_text(
            "📋 Sizda hali bot yo‘q.",
            reply_markup=main_menu()
        )

        return

    buttons = []

    for bot in bots:

        username = bot["username"]

        status = (
            "🟢"
            if subscription_active(bot["id"])
            else "🔴"
        )

        title = (
            f"{status} @{username}"
            if username
            else f"{status} {bot['name']}"
        )

        buttons.append([
            InlineKeyboardButton(
                title,
                callback_data=f"manage:{bot['id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🤖 Yangi bot",
            callback_data="create_bot"
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            "⬅️ Orqaga",
            callback_data="back_main"
        )
    ])

    await query.message.reply_text(
        "📋 Mening botlarim:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# MANAGE BOT
# ============================================================

async def manage_bot_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    bot_id = int(
        query.data.split(":")[1]
    )

    bot = get_bot(bot_id)

    if not bot:

        await query.message.reply_text(
            "❌ Bot topilmadi."
        )

        return

    if bot["owner_id"] != query.from_user.id:

        await query.message.reply_text(
            "❌ Bu bot sizniki emas."
        )

        return

    await query.message.reply_text(
        f"⚙️ Bot sozlamalari\n\n"
        f"🤖 {bot['name']}\n"
        f"@{bot['username'] or 'yo‘q'}\n\n"
        f"{subscription_text(bot_id)}",
        reply_markup=settings_menu(bot_id)
    )


# ============================================================
# SERVICES ADMIN
# ============================================================

async def services_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    bot_id = int(
        query.data.split(":")[1]
    )

    if not user_owns_bot(
        query.from_user.id,
        bot_id
    ):
        await query.message.reply_text(
            "❌ Bu bot sizniki emas."
        )
        return

    services = get_services(bot_id)

    if not services:

        await query.message.reply_text(
            "🛍 Hozircha xizmatlar yo‘q.",
            reply_markup=settings_menu(bot_id)
        )

        return

    text = "🛍 Xizmatlar:\n\n"

    for service in services:

        text += (
            f"🔹 {service['name']}\n"
            f"💰 {service['price']:,} so‘m\n"
        )

        if service["description"]:
            text += (
                f"📝 {service['description']}\n"
            )

        text += "\n"

    await query.message.reply_text(
        text,
        reply_markup=settings_menu(bot_id)
    )


# ============================================================
# ADD SERVICE
# ============================================================

async def add_service_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    bot_id = int(
        query.data.split(":")[1]
    )

    if not user_owns_bot(
        query.from_user.id,
        bot_id
    ):
        await query.message.reply_text(
            "❌ Bu bot sizniki emas."
        )
        return

    context.user_data.clear()

    context.user_data["bot_id"] = bot_id
    context.user_data["state"] = "service_name"

    await query.message.reply_text(
        "➕ Xizmat nomini yuboring."
    )


# ============================================================
# EDIT SERVICES
# ============================================================

async def edit_services_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    bot_id = int(
        query.data.split(":")[1]
    )

    if not user_owns_bot(
        query.from_user.id,
        bot_id
    ):
        await query.message.reply_text(
            "❌ Bu bot sizniki emas."
        )
        return

    services = get_all_services(bot_id)

    if not services:

        await query.message.reply_text(
            "❌ Xizmatlar yo‘q.",
            reply_markup=settings_menu(bot_id)
        )

        return

    buttons = []

    for service in services:

        buttons.append([
            InlineKeyboardButton(
                f"✏️ {service['name']}",
                callback_data=(
                    f"editone:{service['id']}:"
                    f"{bot_id}"
                )
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "⬅️ Orqaga",
            callback_data=f"manage:{bot_id}"
        )
    ])

    await query.message.reply_text(
        "✏️ Xizmatni tanlang:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# EDIT ONE
# ============================================================

async def edit_one_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    parts = query.data.split(":")

    service_id = int(parts[1])
    bot_id = int(parts[2])

    if not user_owns_bot(
        query.from_user.id,
        bot_id
    ):
        await query.message.reply_text(
            "❌ Bu bot sizniki emas."
        )
        return

    conn = db()

    service = conn.execute("""
        SELECT *
        FROM services
        WHERE id = ?
        AND bot_id = ?
    """, (
        service_id,
        bot_id
    )).fetchone()

    conn.close()

    if not service:

        await query.message.reply_text(
            "❌ Xizmat topilmadi."
        )

        return

    context.user_data.clear()

    context.user_data["state"] = (
        "edit_service_name"
    )

    context.user_data["service_id"] = service_id
    context.user_data["bot_id"] = bot_id

    await query.message.reply_text(
        "✏️ Yangi xizmat nomini yuboring.\n\n"
        f"Eski: {service['name']}"
    )


# ============================================================
# DELETE SERVICES
# ============================================================

async def delete_services_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    bot_id = int(
        query.data.split(":")[1]
    )

    if not user_owns_bot(
        query.from_user.id,
        bot_id
    ):
        await query.message.reply_text(
            "❌ Bu bot sizniki emas."
        )
        return

    services = get_all_services(bot_id)

    if not services:

        await query.message.reply_text(
            "❌ Xizmatlar yo‘q."
        )

        return

    buttons = []

    for service in services:

        buttons.append([
            InlineKeyboardButton(
                f"🗑 {service['name']}",
                callback_data=(
                    f"deleteone:{service['id']}:"
                    f"{bot_id}"
                )
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "⬅️ Orqaga",
            callback_data=f"manage:{bot_id}"
        )
    ])

    await query.message.reply_text(
        "🗑 O‘chirmoqchi bo‘lgan xizmatni tanlang:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# DELETE ONE
# ============================================================

async def delete_one_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    parts = query.data.split(":")

    service_id = int(parts[1])
    bot_id = int(parts[2])

    if not user_owns_bot(
        query.from_user.id,
        bot_id
    ):
        await query.message.reply_text(
            "❌ Bu bot sizniki emas."
        )
        return

    conn = db()

    conn.execute("""
        DELETE FROM services
        WHERE id = ?
        AND bot_id = ?
    """, (
        service_id,
        bot_id
    ))

    conn.commit()
    conn.close()

    await query.message.reply_text(
        "✅ Xizmat o‘chirildi.",
        reply_markup=settings_menu(bot_id)
    )


# ============================================================
# START TEXT
# ============================================================

async def start_text_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    bot_id = int(
        query.data.split(":")[1]
    )

    if not user_owns_bot(
        query.from_user.id,
        bot_id
    ):
        await query.message.reply_text(
            "❌ Bu bot sizniki emas."
        )
        return

    context.user_data.clear()

    context.user_data["state"] = "start_text"
    context.user_data["bot_id"] = bot_id

    await query.message.reply_text(
        "👋 Yangi Start xabarini yuboring."
    )


# ============================================================
# ADD BUTTON
# ============================================================

async def add_button_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    bot_id = int(
        query.data.split(":")[1]
    )

    if not user_owns_bot(
        query.from_user.id,
        bot_id
    ):
        await query.message.reply_text(
            "❌ Bu bot sizniki emas."
        )
        return

    context.user_data.clear()

    context.user_data["state"] = "button_title"
    context.user_data["bot_id"] = bot_id

    await query.message.reply_text(
        "🔘 Tugma nomini yuboring.\n\n"
        "Masalan: 📞 Admin"
    )


# ============================================================
# CODE MODE
# ============================================================

async def code_mode_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    bot_id = int(
        query.data.split(":")[1]
    )

    if not user_owns_bot(
        query.from_user.id,
        bot_id
    ):
        await query.message.reply_text(
            "❌ Bu bot sizniki emas."
        )
        return

    context.user_data.clear()

    context.user_data["state"] = "code_mode"
    context.user_data["bot_id"] = bot_id

    example = {
        "welcome": "Assalomu Aleykum! 👋",
        "services": [
            {
                "name": "Telegram Premium 1 oy",
                "description": "1 oylik Premium",
                "price": 10000
            },
            {
                "name": "Telegram Stars",
                "description": "100 Stars",
                "price": 25000
            }
        ]
    }

    await query.message.reply_text(
        "💻 Xavfsiz JSON konfiguratsiya:\n\n"
        + json.dumps(
            example,
            ensure_ascii=False,
            indent=2
        )
    )


# ============================================================
# EDIT SERVICE TEXT
# ============================================================

async def edit_service_text_handler(
    update,
    context
):

    state = context.user_data.get(
        "state"
    )

    if state == "edit_service_name":

        context.user_data["new_name"] = (
            update.message.text.strip()
        )

        context.user_data["state"] = (
            "edit_service_description"
        )

        await update.message.reply_text(
            "📝 Yangi tavsifni yuboring.\n\n"
            "Kerak bo‘lmasa: -"
        )

        return True

    if state == "edit_service_description":

        desc = update.message.text.strip()

        if desc == "-":
            desc = ""

        context.user_data["new_description"] = desc

        context.user_data["state"] = (
            "edit_service_price"
        )

        await update.message.reply_text(
            "💰 Yangi narxni yuboring."
        )

        return True

    if state == "edit_service_price":

        try:

            price = int(
                update.message.text
                .strip()
                .replace(" ", "")
                .replace(",", "")
            )

            if price < 0:
                raise ValueError

        except Exception:

            await update.message.reply_text(
                "❌ Narx noto‘g‘ri."
            )

            return True

        service_id = context.user_data[
            "service_id"
        ]

        bot_id = context.user_data[
            "bot_id"
        ]

        update_service(
            service_id,
            context.user_data["new_name"],
            context.user_data["new_description"],
            price
        )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ Xizmat o‘zgartirildi!",
            reply_markup=settings_menu(bot_id)
        )

        return True

    return False


# ============================================================
# CHILD START
# ============================================================

async def child_start(
    update,
    context
):

    bot_db_id = context.application.bot_data[
        "bot_db_id"
    ]

    save_user(
        update.effective_user
    )

    if not subscription_active(bot_db_id):

        bot = get_bot(bot_db_id)

        await update.message.reply_text(
            "🔴 Bu botning obunasi tugagan.\n\n"
            "Bot egasi asosiy bot orqali obunani yangilashi kerak."
        )

        return

    text = get_start_text(
        bot_db_id
    )

    await update.message.reply_text(
        text,
        reply_markup=child_menu(bot_db_id)
    )


# ============================================================
# CHILD BALANCE
# ============================================================

async def child_balance_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    balance = get_balance(
        query.from_user.id
    )

    await query.message.reply_text(
        f"💰 Balansingiz:\n\n"
        f"{balance:,} so‘m"
    )


# ============================================================
# CHILD TOPUP
# ============================================================

async def child_topup_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    bot_db_id = context.application.bot_data[
        "bot_db_id"
    ]

    context.user_data.clear()

    context.user_data["state"] = "topup_amount"
    context.user_data["payment_bot_id"] = bot_db_id
    context.user_data["payment_source"] = "child"

    card = get_payment_card()

    await query.message.reply_text(
        "➕ Balans to‘ldirish\n\n"
        "💳 Karta:\n"
        f"`{card}`\n\n"
        "💰 Summani yuboring.\n"
        "Masalan: 25000",
        parse_mode="Markdown"
    )


# ============================================================
# CHILD SUBSCRIPTION
# ============================================================

async def child_subscription_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    bot_db_id = context.application.bot_data[
        "bot_db_id"
    ]

    await query.message.reply_text(
        f"💳 Bot obunasi\n\n"
        f"{subscription_text(bot_db_id)}\n\n"
        "Obunani bot egasi asosiy bot orqali sotib oladi.",
        reply_markup=subscription_menu(bot_db_id)
    )


# ============================================================
# CHILD SERVICES
# ============================================================

async def child_services_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    bot_db_id = context.application.bot_data[
        "bot_db_id"
    ]

    if not subscription_active(bot_db_id):

        await query.message.reply_text(
            "🔴 Bot obunasi tugagan."
        )

        return

    services = get_services(bot_db_id)

    if not services:

        await query.message.reply_text(
            "🛍 Hozircha xizmatlar mavjud emas."
        )

        return

    buttons = []

    for service in services:

        buttons.append([
            InlineKeyboardButton(
                f"{service['name']} — "
                f"{service['price']:,} so‘m",
                callback_data=(
                    f"service:{service['id']}:"
                    f"{bot_db_id}"
                )
            )
        ])

    await query.message.reply_text(
        "🛍 Xizmatlar:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# CHILD SERVICE
# ============================================================

async def child_service_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    bot_db_id = context.application.bot_data[
        "bot_db_id"
    ]

    if not subscription_active(bot_db_id):

        await query.message.reply_text(
            "🔴 Bot obunasi tugagan."
        )

        return

    service_id = int(
        query.data.split(":")[1]
    )

    service = get_service(
        service_id
    )

    if not service:

        await query.message.reply_text(
            "❌ Xizmat topilmadi."
        )

        return

    text = (
        f"🛍 {service['name']}\n\n"
    )

    if service["description"]:

        text += (
            f"📝 {service['description']}\n\n"
        )

    text += (
        f"💰 Narxi: "
        f"{service['price']:,} so‘m"
    )

    await query.message.reply_text(
        text
    )


# ============================================================
# CUSTOM BUTTON
# ============================================================

async def custom_button_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    parts = query.data.split(":")

    button_id = int(parts[1])
    bot_db_id = int(parts[2])

    if not subscription_active(bot_db_id):

        await query.message.reply_text(
            "🔴 Bot obunasi tugagan."
        )

        return

    conn = db()

    row = conn.execute("""
        SELECT *
        FROM custom_buttons
        WHERE id = ?
        AND bot_id = ?
        AND active = 1
    """, (
        button_id,
        bot_db_id
    )).fetchone()

    conn.close()

    if not row:

        await query.message.reply_text(
            "❌ Tugma topilmadi."
        )

        return

    await query.message.reply_text(
        row["text"]
    )


# ============================================================
# CHILD HELP
# ============================================================

async def child_help(
    update,
    context
):

    await update.message.reply_text(
        "ℹ️ Yordam\n\n"
        "/start — Asosiy menyu\n"
        "/help — Yordam"
    )


# ============================================================
# ADMIN CHECK
# ============================================================

def is_admin(user_id):

    return (
        ADMIN_ID != 0
        and user_id == ADMIN_ID
    )


# ============================================================
# ADMIN PANEL
# ============================================================

def admin_menu():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💳 Karta",
                callback_data="admin_card"
            )
        ],
        [
            InlineKeyboardButton(
                "💰 Balanslar",
                callback_data="admin_balances"
            )
        ],
        [
            InlineKeyboardButton(
                "📊 Statistika",
                callback_data="admin_stats"
            )
        ],
    ])


async def admin_command(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        await update.message.reply_text(
            "❌ Siz admin emassiz."
        )
        return

    await update.message.reply_text(
        "👨‍💼 ADMIN PANEL",
        reply_markup=admin_menu()
    )


# ============================================================
# ADMIN CARD
# ============================================================

async def admin_card_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    if not is_admin(
        query.from_user.id
    ):
        return

    context.user_data.clear()

    context.user_data["state"] = "admin_card"

    await query.message.reply_text(
        "💳 Yangi karta raqamini yuboring."
    )


# ============================================================
# ADMIN STATS
# ============================================================

async def admin_stats_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    if not is_admin(
        query.from_user.id
    ):
        return

    conn = db()

    users = conn.execute(
        "SELECT COUNT(*) AS c FROM users"
    ).fetchone()["c"]

    bots = conn.execute(
        "SELECT COUNT(*) AS c FROM bots"
    ).fetchone()["c"]

    payments = conn.execute(
        "SELECT COUNT(*) AS c FROM payments"
    ).fetchone()["c"]

    approved = conn.execute("""
        SELECT COALESCE(SUM(amount), 0) AS s
        FROM payments
        WHERE status = 'approved'
    """).fetchone()["s"]

    conn.close()

    await query.message.reply_text(
        "📊 STATISTIKA\n\n"
        f"👤 Foydalanuvchilar: {users}\n"
        f"🤖 Botlar: {bots}\n"
        f"💳 To‘lovlar: {payments}\n"
        f"💰 Tasdiqlangan tushum: {approved:,} so‘m"
    )


# ============================================================
# ADMIN BALANCES
# ============================================================

async def admin_balances_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    if not is_admin(
        query.from_user.id
    ):
        return

    conn = db()

    rows = conn.execute("""
        SELECT user_id, username, balance
        FROM users
        ORDER BY balance DESC
        LIMIT 20
    """).fetchall()

    conn.close()

    if not rows:

        await query.message.reply_text(
            "Foydalanuvchilar yo‘q."
        )

        return

    text = "💰 BALANSLAR\n\n"

    for row in rows:

        text += (
            f"👤 {row['user_id']}\n"
            f"@{row['username'] or 'yo‘q'}\n"
            f"💵 {int(row['balance'] or 0):,} so‘m\n\n"
        )

    await query.message.reply_text(
        text
    )


# ============================================================
# ADMIN PAYMENT PROCESS
# ============================================================

async def admin_payment_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    if not is_admin(
        query.from_user.id
    ):
        await query.message.reply_text(
            "❌ Admin emassiz."
        )
        return

    parts = query.data.split(":")

    action = parts[0]
    payment_id = int(parts[1])

    approve = (
        action == "pay_approve"
    )

    row, result = process_payment(
        payment_id,
        approve,
        query.from_user.id
    )

    if result == "not_found":

        await query.message.reply_text(
            "❌ To‘lov topilmadi."
        )

        return

    if result == "already_processed":

        await query.message.reply_text(
            "⚠️ Bu to‘lov avval ko‘rib chiqilgan."
        )

        return

    if approve:

        await query.message.reply_text(
            f"✅ To‘lov #{payment_id} tasdiqlandi.\n\n"
            f"💰 {row['amount']:,} so‘m balansga qo‘shildi."
        )

        try:

            main_app = (
                Application.builder()
                .token(MAIN_BOT_TOKEN)
                .build()
            )

            await main_app.initialize()

            await main_app.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    "✅ TO‘LOV TASDIQLANDI!\n\n"
                    f"🧾 ID: #{payment_id}\n"
                    f"💰 +{row['amount']:,} so‘m\n\n"
                    f"💵 Yangi balans: "
                    f"{get_balance(row['user_id']):,} so‘m"
                )
            )

            await main_app.shutdown()

        except Exception as e:

            logger.error(
                "User notification error: %s",
                e
            )

    else:

        await query.message.reply_text(
            f"❌ To‘lov #{payment_id} rad etildi."
        )

        try:

            main_app = (
                Application.builder()
                .token(MAIN_BOT_TOKEN)
                .build()
            )

            await main_app.initialize()

            await main_app.bot.send_message(
                chat_id=row["user_id"],
                text=(
                    "❌ TO‘LOV RAD ETILDI.\n\n"
                    f"🧾 ID: #{payment_id}\n"
                    f"💰 Summa: {row['amount']:,} so‘m"
                )
            )

            await main_app.shutdown()

        except Exception:
            pass


# ============================================================
# ADMIN TEXT
# ============================================================

async def admin_text_handler(
    update,
    context
):

    if not is_admin(
        update.effective_user.id
    ):
        return False

    state = context.user_data.get(
        "state"
    )

    if state == "admin_card":

        card = update.message.text.strip()

        set_payment_card(card)

        context.user_data.clear()

        await update.message.reply_text(
            "✅ Karta raqami o‘zgartirildi.",
            reply_markup=admin_menu()
        )

        return True

    return False


# ============================================================
# CHILD RUNNER
# ============================================================

async def run_child_bot(token):

    if token in running_bots:
        return

    app = None

    try:

        conn = db()

        row = conn.execute("""
            SELECT *
            FROM bots
            WHERE token = ?
            AND active = 1
        """, (token,)).fetchone()

        conn.close()

        if not row:
            return

        bot_db_id = row["id"]

        # No subscription = don't run
        if not subscription_active(bot_db_id):

            logger.info(
                "Child bot %s has no active subscription.",
                bot_db_id
            )

            return

        app = (
            Application.builder()
            .token(token)
            .build()
        )

        app.bot_data["bot_db_id"] = bot_db_id

        app.add_handler(
            CommandHandler(
                "start",
                child_start
            )
        )

        app.add_handler(
            CommandHandler(
                "help",
                child_help
            )
        )

        app.add_handler(
            CommandHandler(
                "admin",
                admin_command
            )
        )

        app.add_handler(
            CallbackQueryHandler(
                child_services_callback,
                pattern=r"^child_services:"
            )
        )

        app.add_handler(
            CallbackQueryHandler(
                child_balance_callback,
                pattern=r"^child_balance:"
            )
        )

        app.add_handler(
            CallbackQueryHandler(
                child_topup_callback,
                pattern=r"^child_topup:"
            )
        )

        app.add_handler(
            CallbackQueryHandler(
                child_subscription_callback,
                pattern=r"^child_subscription:"
            )
        )

        app.add_handler(
            CallbackQueryHandler(
                child_service_callback,
                pattern=r"^service:"
            )
        )

        app.add_handler(
            CallbackQueryHandler(
                custom_button_callback,
                pattern=r"^custom:"
            )
        )

        app.add_handler(
            CallbackQueryHandler(
                admin_payment_callback,
                pattern=r"^pay_(approve|reject):"
            )
        )

        app.add_handler(
            CallbackQueryHandler(
                admin_card_callback,
                pattern=r"^admin_card$"
            )
        )

        app.add_handler(
            CallbackQueryHandler(
                admin_stats_callback,
                pattern=r"^admin_stats$"
            )
        )

        app.add_handler(
            CallbackQueryHandler(
                admin_balances_callback,
                pattern=r"^admin_balances$"
            )
        )

        app.add_handler(
            MessageHandler(
                filters.PHOTO,
                receive_photo
            )
        )

        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                child_text_router
            )
        )

        await app.initialize()

        await app.start()

        await app.updater.start_polling(
            drop_pending_updates=True
        )

        running_bots[token] = app

        logger.info(
            "Child bot started: %s",
            bot_db_id
        )

        while token in running_bots:

            if not subscription_active(bot_db_id):

                logger.info(
                    "Subscription expired: %s",
                    bot_db_id
                )

                break

            await asyncio.sleep(10)

    except Exception as e:

        logger.exception(
            "Child bot error: %s",
            e
        )

    finally:

        running_bots.pop(
            token,
            None
        )

        if app:

            try:
                await app.updater.stop()
            except Exception:
                pass

            try:
                await app.stop()
            except Exception:
                pass

            try:
                await app.shutdown()
            except Exception:
                pass


# ============================================================
# CHILD TEXT ROUTER
# ============================================================

async def child_text_router(
    update,
    context
):

    state = context.user_data.get(
        "state"
    )

    if state == "topup_amount":

        await receive_main_text(
            update,
            context
        )

        return

    if state == "topup_photo":

        return

    if state == "admin_card":

        handled = await admin_text_handler(
            update,
            context
        )

        if handled:
            return

    await receive_main_text(
        update,
        context
    )


# ============================================================
# RESTORE BOTS
# ============================================================

async def restore_saved_bots():

    rows = get_active_bots()

    logger.info(
        "Found %s saved bots.",
        len(rows)
    )

    for row in rows:

        if not subscription_active(
            row["id"]
        ):
            continue

        token = row["token"]

        if token in running_bots:
            continue

        task = asyncio.create_task(
            run_child_bot(token)
        )

        running_tasks[token] = task

        await asyncio.sleep(1)


# ============================================================
# EXPIRATION MONITOR
# ============================================================

async def subscription_monitor():

    while True:

        try:

            rows = get_active_bots()

            for row in rows:

                if not subscription_active(
                    row["id"]
                ):

                    token = row["token"]

                    if token in running_bots:

                        logger.info(
                            "Stopping expired bot %s",
                            row["id"]
                        )

                        running_bots.pop(
                            token,
                            None
                        )

            await asyncio.sleep(30)

        except Exception as e:

            logger.error(
                "Subscription monitor error: %s",
                e
            )

            await asyncio.sleep(30)


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(
    update,
    context
):

    query = update.callback_query

    if not query:
        return

    data = query.data

    # --------------------------------------------------------
    # ADMIN PAYMENTS
    # --------------------------------------------------------

    if data.startswith(
        "pay_approve:"
    ) or data.startswith(
        "pay_reject:"
    ):

        await admin_payment_callback(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # ADMIN
    # --------------------------------------------------------

    if data == "admin_card":

        await admin_card_callback(
            update,
            context
        )

        return

    if data == "admin_stats":

        await admin_stats_callback(
            update,
            context
        )

        return

    if data == "admin_balances":

        await admin_balances_callback(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # MAIN
    # --------------------------------------------------------

    if data == "create_bot":

        await create_bot_callback(
            update,
            context
        )

    elif data == "my_bots":

        await my_bots_callback(
            update,
            context
        )

    elif data == "main_balance":

        await main_balance_callback(
            update,
            context
        )

    elif data == "main_topup":

        await main_topup_callback(
            update,
            context
        )

    elif data == "main_subscriptions":

        await main_subscriptions_callback(
            update,
            context
        )

    elif data == "back_main":

        await query.answer()

        await query.message.reply_text(
            "Asosiy menyu:",
            reply_markup=main_menu()
        )

    elif data.startswith(
        "manage:"
    ):

        await manage_bot_callback(
            update,
            context
        )

    elif data.startswith(
        "services:"
    ):

        await services_callback(
            update,
            context
        )

    elif data.startswith(
        "add_service:"
    ):

        await add_service_callback(
            update,
            context
        )

    elif data.startswith(
        "edit_services:"
    ):

        await edit_services_callback(
            update,
            context
        )

    elif data.startswith(
        "editone:"
    ):

        await edit_one_callback(
            update,
            context
        )

    elif data.startswith(
        "delete_services:"
    ):

        await delete_services_callback(
            update,
            context
        )

    elif data.startswith(
        "deleteone:"
    ):

        await delete_one_callback(
            update,
            context
        )

    elif data.startswith(
        "start_text:"
    ):

        await start_text_callback(
            update,
            context
        )

    elif data.startswith(
        "add_button:"
    ):

        await add_button_callback(
            update,
            context
        )

    elif data.startswith(
        "code_mode:"
    ):

        await code_mode_callback(
            update,
            context
        )

    elif data.startswith(
        "bot_subscription:"
    ):

        await bot_subscription_callback(
            update,
            context
        )

    elif data.startswith(
        "buy_sub:"
    ):

        await buy_subscription_callback(
            update,
            context
        )

    else:

        await query.answer()


# ============================================================
# MAIN TEXT ROUTER
# ============================================================

async def main_text_router(
    update,
    context
):

    handled = await admin_text_handler(
        update,
        context
    )

    if handled:
        return

    state = context.user_data.get(
        "state"
    )

    if state in (
        "edit_service_name",
        "edit_service_description",
        "edit_service_price"
    ):

        handled = await edit_service_text_handler(
            update,
            context
        )

        if handled:
            return

    await receive_main_text(
        update,
        context
    )


# ============================================================
# HEALTH SERVER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            b"BOT SERVER OK"
        )

    def log_message(
        self,
        format,
        *args
    ):

        return


def start_health_server():

    server = HTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        HealthHandler
    )

    logger.info(
        "Health server on port %s",
        PORT
    )

    server.serve_forever()


# ============================================================
# MAIN
# ============================================================

async def main():

    if not MAIN_BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN topilmadi. "
            "Render Environment Variables ga "
            "BOT_TOKEN qo‘ying."
        )

    if ADMIN_ID == 0:

        logger.warning(
            "ADMIN_ID qo‘yilmagan."
        )

    init_db()

    # Health server
    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()

    app = (
        Application.builder()
        .token(MAIN_BOT_TOKEN)
        .build()
    )

    # Main commands
    app.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    app.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    # Main callbacks
    app.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    # Photo
    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            receive_photo
        )
    )

    # Text
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            main_text_router
        )
    )

    await app.initialize()

    await app.start()

    await app.updater.start_polling(
        drop_pending_updates=True
    )

    logger.info(
        "Main bot started."
    )

    # Restore child bots
    await restore_saved_bots()

    # Subscription monitor
    asyncio.create_task(
        subscription_monitor()
    )

    try:

        while True:

            await asyncio.sleep(60)

    except asyncio.CancelledError:

        pass

    finally:

        await app.updater.stop()

        await app.stop()

        await app.shutdown()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info(
            "Application stopped."
        )

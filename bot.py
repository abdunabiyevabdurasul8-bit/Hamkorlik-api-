import os
import sqlite3
import asyncio
import logging
import threading
import time
import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.constants import ParseMode

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ============================================================
# SOZLAMALAR
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

db_lock = threading.Lock()


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(
        DB_PATH,
        check_same_thread=False,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():
    with db_lock:
        conn = db()

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                balance INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                photo_id TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                approved_at TEXT DEFAULT ''
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                bot_token TEXT NOT NULL UNIQUE,
                bot_username TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                active INTEGER DEFAULT 1
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                api_key TEXT NOT NULL,
                api_url TEXT DEFAULT '',
                active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_api_connections (
                bot_id INTEGER PRIMARY KEY,
                api_connection_id INTEGER NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                price INTEGER NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_states (
                user_id INTEGER PRIMARY KEY,
                state TEXT DEFAULT '',
                data TEXT DEFAULT ''
            )
        """)

        defaults = {
            "payment_card": "",
            "payment_name": "",
            "price_1_day": "5000",
            "price_3_day": "13000",
            "price_7_day": "25000",
            "price_1_month": "43000"
        }

        for key, value in defaults.items():
            conn.execute(
                """
                INSERT OR IGNORE INTO settings(key, value)
                VALUES(?, ?)
                """,
                (key, value)
            )

        conn.commit()
        conn.close()


# ============================================================
# VAQT
# ============================================================

def now_dt():
    return datetime.now(timezone.utc)


def now_str():
    return now_dt().isoformat()


def parse_dt(value):
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def format_dt(value):
    dt = parse_dt(value)

    if not dt:
        return value

    return dt.astimezone(timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


# ============================================================
# SETTINGS
# ============================================================

def get_setting(key, default=""):
    with db_lock:
        conn = db()

        row = conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ).fetchone()

        conn.close()

    if row:
        return row["value"]

    return default


def set_setting(key, value):
    with db_lock:
        conn = db()

        conn.execute(
            """
            INSERT INTO settings(key, value)
            VALUES(?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
            """,
            (key, str(value))
        )

        conn.commit()
        conn.close()


# ============================================================
# USERS
# ============================================================

def save_user(user):
    if not user:
        return

    with db_lock:
        conn = db()

        conn.execute(
            """
            INSERT INTO users(
                user_id,
                username,
                first_name,
                balance,
                created_at
            )
            VALUES(?, ?, ?, 0, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
            """,
            (
                user.id,
                user.username or "",
                user.first_name or "",
                now_str()
            )
        )

        conn.commit()
        conn.close()


def get_user(user_id):
    with db_lock:
        conn = db()

        row = conn.execute(
            """
            SELECT *
            FROM users
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        conn.close()

    return row


def get_balance(user_id):
    row = get_user(user_id)

    if not row:
        return 0

    return int(row["balance"])


def change_balance(user_id, amount):
    with db_lock:
        conn = db()

        conn.execute(
            """
            UPDATE users
            SET balance=balance+?
            WHERE user_id=?
            """,
            (amount, user_id)
        )

        conn.commit()
        conn.close()


# ============================================================
# STATE
# ============================================================

def set_state(user_id, state, data=""):
    with db_lock:
        conn = db()

        conn.execute(
            """
            INSERT INTO user_states(
                user_id,
                state,
                data
            )
            VALUES(?, ?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                state=excluded.state,
                data=excluded.data
            """,
            (
                user_id,
                state,
                data
            )
        )

        conn.commit()
        conn.close()


def get_state(user_id):
    with db_lock:
        conn = db()

        row = conn.execute(
            """
            SELECT *
            FROM user_states
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        conn.close()

    if not row:
        return "", ""

    return row["state"], row["data"]


def clear_state(user_id):
    with db_lock:
        conn = db()

        conn.execute(
            """
            DELETE FROM user_states
            WHERE user_id=?
            """,
            (user_id,)
        )

        conn.commit()
        conn.close()


# ============================================================
# SUBSCRIPTION
# ============================================================

PLANS = {
    "1_day": {
        "name": "1 kun",
        "days": 1,
        "price_key": "price_1_day"
    },
    "3_day": {
        "name": "3 kun",
        "days": 3,
        "price_key": "price_3_day"
    },
    "7_day": {
        "name": "7 kun",
        "days": 7,
        "price_key": "price_7_day"
    },
    "1_month": {
        "name": "1 oy",
        "days": 30,
        "price_key": "price_1_month"
    }
}


def plan_price(plan):
    if plan not in PLANS:
        return 0

    return int(
        get_setting(
            PLANS[plan]["price_key"],
            "0"
        )
    )


def expire_old_subscriptions():
    current = now_dt().isoformat()

    with db_lock:
        conn = db()

        conn.execute(
            """
            UPDATE subscriptions
            SET status='expired'
            WHERE status='active'
            AND end_at<=?
            """,
            (current,)
        )

        conn.commit()
        conn.close()


def get_active_subscription(user_id):
    expire_old_subscriptions()

    with db_lock:
        conn = db()

        row = conn.execute(
            """
            SELECT *
            FROM subscriptions
            WHERE user_id=?
            AND status='active'
            AND end_at>?
            ORDER BY end_at DESC
            LIMIT 1
            """,
            (
                user_id,
                now_str()
            )
        ).fetchone()

        conn.close()

    return row


def create_subscription(user_id, plan):
    if plan not in PLANS:
        return False, "Noto‘g‘ri tarif."

    price = plan_price(plan)

    if price <= 0:
        return False, "Tarif narxi sozlanmagan."

    balance = get_balance(user_id)

    if balance < price:
        return False, "BALANCE_LOW"

    current = now_dt()

    active = get_active_subscription(user_id)

    if active:
        old_end = parse_dt(active["end_at"])

        if old_end and old_end > current:
            start = old_end
        else:
            start = current
    else:
        start = current

    end = start + timedelta(
        days=PLANS[plan]["days"]
    )

    with db_lock:
        conn = db()

        conn.execute(
            """
            UPDATE subscriptions
            SET status='expired'
            WHERE user_id=?
            AND status='active'
            """,
            (user_id,)
        )

        conn.execute(
            """
            INSERT INTO subscriptions(
                user_id,
                plan,
                price,
                start_at,
                end_at,
                status,
                created_at
            )
            VALUES(?, ?, ?, ?, ?, 'active', ?)
            """,
            (
                user_id,
                plan,
                price,
                start.isoformat(),
                end.isoformat(),
                now_str()
            )
        )

        conn.execute(
            """
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            """,
            (
                price,
                user_id
            )
        )

        conn.commit()
        conn.close()

    return True, {
        "price": price,
        "start": start.isoformat(),
        "end": end.isoformat()
    }


# ============================================================
# MAIN MENU
# ============================================================

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🤖 Bot yaratish",
                callback_data="create_bot"
            ),
            InlineKeyboardButton(
                "⚙️ Botni sozlash",
                callback_data="settings"
            )
        ],
        [
            InlineKeyboardButton(
                "💳 Obuna sotib olish",
                callback_data="subscription"
            )
        ],
        [
            InlineKeyboardButton(
                "👤 Profil",
                callback_data="profile"
            ),
            InlineKeyboardButton(
                "💰 Balans",
                callback_data="balance"
            )
        ],
        [
            InlineKeyboardButton(
                "ℹ️ Yordam",
                callback_data="help"
            )
        ]
    ])


def settings_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔌 API ulash",
                callback_data="connect_api"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 Mening botlarim",
                callback_data="my_bots"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Orqaga",
                callback_data="back_main"
            )
        ]
    ])


def subscription_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"1 kun — {plan_price('1_day'):,} so‘m",
                callback_data="buy_1_day"
            )
        ],
        [
            InlineKeyboardButton(
                f"3 kun — {plan_price('3_day'):,} so‘m",
                callback_data="buy_3_day"
            )
        ],
        [
            InlineKeyboardButton(
                f"7 kun — {plan_price('7_day'):,} so‘m",
                callback_data="buy_7_day"
            )
        ],
        [
            InlineKeyboardButton(
                f"1 oy — {plan_price('1_month'):,} so‘m",
                callback_data="buy_1_month"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Orqaga",
                callback_data="back_main"
            )
        ]
    ])


def admin_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💰 Balans +",
                callback_data="admin_balance_plus"
            ),
            InlineKeyboardButton(
                "💰 Balans -",
                callback_data="admin_balance_minus"
            )
        ],
        [
            InlineKeyboardButton(
                "💳 To‘lovlar",
                callback_data="admin_payments"
            )
        ],
        [
            InlineKeyboardButton(
                "👤 Foydalanuvchilar",
                callback_data="admin_users"
            ),
            InlineKeyboardButton(
                "📊 Statistika",
                callback_data="admin_stats"
            )
        ],
        [
            InlineKeyboardButton(
                "💳 Karta",
                callback_data="admin_card"
            )
        ],
        [
            InlineKeyboardButton(
                "💵 Narxlar",
                callback_data="admin_prices"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Bosh menyu",
                callback_data="back_main"
            )
        ]
    ])


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user:
        return

    save_user(user)
    clear_state(user.id)

    await update.message.reply_text(
        "Assalomu Aleykum! 👋\n\n"
        "🤖 Telegram bot platformasiga xush kelibsiz.\n\n"
        "Kerakli bo‘limni tanlang:",
        reply_markup=main_menu()
    )


# ============================================================
# PROFILE
# ============================================================

async def show_profile(query):
    user_id = query.from_user.id

    row = get_user(user_id)

    subscription = get_active_subscription(user_id)

    if not row:
        text = "Profil topilmadi."
    else:
        username = row["username"] or "username yo‘q"

        text = (
            "👤 <b>Profil</b>\n\n"
            f"🆔 ID: <code>{user_id}</code>\n"
            f"👤 Username: @{username}\n"
            f"💰 Balans: <b>{row['balance']:,} so‘m</b>\n\n"
        )

        if subscription:
            text += (
                "💳 <b>Obuna</b>\n"
                f"📦 Tarif: {PLANS.get(subscription['plan'], {}).get('name', subscription['plan'])}\n"
                f"🟢 Boshlanishi: {format_dt(subscription['start_at'])}\n"
                f"🔴 Tugashi: {format_dt(subscription['end_at'])}"
            )
        else:
            text += "❌ Aktiv obuna yo‘q."

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )


# ============================================================
# BALANCE
# ============================================================

async def show_balance(query):
    balance = get_balance(query.from_user.id)

    await query.edit_message_text(
        "💰 <b>Balansingiz</b>\n\n"
        f"💵 {balance:,} so‘m",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )


# ============================================================
# HELP
# ============================================================

async def show_help(query):
    await query.edit_message_text(
        "ℹ️ <b>Yordam</b>\n\n"
        "🤖 Bot yaratish — Telegram botingizni ulash.\n\n"
        "⚙️ Botni sozlash — API ulash va botlarni ko‘rish.\n\n"
        "🔌 API ulash — o‘zingiz ishlatadigan API ma’lumotlarini ulash.\n\n"
        "💳 Obuna sotib olish — platformadan foydalanish obunasini olish.\n\n"
        "💰 Balans — hisobingizdagi mablag‘ni ko‘rish.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )


# ============================================================
# API ULASH
# ============================================================

async def start_api_connection(query):
    set_state(
        query.from_user.id,
        "waiting_api_key"
    )

    await query.edit_message_text(
        "🔌 <b>API ulash</b>\n\n"
        "API Key'ingizni yuboring.\n\n"
        "Faqat o‘zingizga tegishli API Key'ni yuboring.\n\n"
        "❗ Secret Key so‘ralmaydi.",
        parse_mode=ParseMode.HTML
    )


async def receive_api_key(update, context):
    user_id = update.effective_user.id

    state, _ = get_state(user_id)

    if state != "waiting_api_key":
        return False

    api_key = (
        update.message.text or ""
    ).strip()

    if not api_key:
        await update.message.reply_text(
            "❌ API Key bo‘sh bo‘lishi mumkin emas."
        )
        return True

    with db_lock:
        conn = db()

        current = now_str()

        conn.execute(
            """
            INSERT INTO api_connections(
                user_id,
                api_key,
                api_url,
                active,
                created_at,
                updated_at
            )
            VALUES(?, ?, '', 1, ?, ?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                api_key=excluded.api_key,
                active=1,
                updated_at=excluded.updated_at
            """,
            (
                user_id,
                api_key,
                current,
                current
            )
        )

        conn.commit()
        conn.close()

    clear_state(user_id)

    await update.message.reply_text(
        "✅ <b>API muvaffaqiyatli ulandi!</b>\n\n"
        "API ma’lumotingiz saqlandi.",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_menu()
    )

    return True


# ============================================================
# BOT TOKEN
# ============================================================

async def start_create_bot(query):
    set_state(
        query.from_user.id,
        "waiting_bot_token"
    )

    await query.edit_message_text(
        "🤖 <b>Bot yaratish</b>\n\n"
        "BotFather'dan olgan bot tokeningizni yuboring.\n\n"
        "Masalan:\n"
        "<code>123456789:AA...</code>\n\n"
        "⚠️ Tokenni hech kimga bermang.",
        parse_mode=ParseMode.HTML
    )


def get_bot_info(token):
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=15
        )

        if response.status_code != 200:
            return None

        data = response.json()

        if not data.get("ok"):
            return None

        return data.get("result")

    except Exception as e:
        logger.error(
            "getMe error: %s",
            e
        )

        return None


def bot_token_exists(token):
    with db_lock:
        conn = db()

        row = conn.execute(
            """
            SELECT id
            FROM user_bots
            WHERE bot_token=?
            """,
            (token,)
        ).fetchone()

        conn.close()

    return row is not None


async def receive_bot_token(update, context):
    user_id = update.effective_user.id

    state, _ = get_state(user_id)

    if state != "waiting_bot_token":
        return False

    token = (
        update.message.text or ""
    ).strip()

    if not token:
        await update.message.reply_text(
            "❌ Bot token yuboring."
        )
        return True

    if bot_token_exists(token):
        clear_state(user_id)

        await update.message.reply_text(
            "❌ <b>Bu bot allaqachon qo‘shilgan.</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=settings_menu()
        )

        return True

    await update.message.reply_text(
        "⏳ Bot tekshirilmoqda..."
    )

    info = await asyncio.to_thread(
        get_bot_info,
        token
    )

    if not info:
        await update.message.reply_text(
            "❌ Bot token noto‘g‘ri.\n\n"
            "BotFather bergan tokenni qayta yuboring."
        )
        return True

    username = info.get(
        "username",
        ""
    )

    with db_lock:
        conn = db()

        try:
            conn.execute(
                """
                INSERT INTO user_bots(
                    user_id,
                    bot_token,
                    bot_username,
                    created_at,
                    active
                )
                VALUES(?, ?, ?, ?, 1)
                """,
                (
                    user_id,
                    token,
                    username,
                    now_str()
                )
            )

            conn.commit()

        except sqlite3.IntegrityError:
            conn.close()

            await update.message.reply_text(
                "❌ Bu bot allaqachon tizimda mavjud."
            )

            return True

        conn.close()

    clear_state(user_id)

    await update.message.reply_text(
        "✅ <b>Bot muvaffaqiyatli qo‘shildi!</b>\n\n"
        f"🤖 @{username}\n\n"
        "Bot ma’lumotlari saqlandi.",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_menu()
    )

    return True


# ============================================================
# MY BOTS
# ============================================================

async def show_my_bots(query):
    user_id = query.from_user.id

    with db_lock:
        conn = db()

        rows = conn.execute(
            """
            SELECT *
            FROM user_bots
            WHERE user_id=?
            ORDER BY id DESC
            """,
            (user_id,)
        ).fetchall()

        conn.close()

    if not rows:
        text = (
            "🤖 <b>Mening botlarim</b>\n\n"
            "Hozircha bot qo‘shilmagan."
        )
    else:
        lines = [
            "🤖 <b>Mening botlarim</b>\n"
        ]

        for row in rows:
            username = row["bot_username"] or "Noma’lum"

            status = (
                "🟢 Aktiv"
                if row["active"]
                else "🔴 O‘chirilgan"
            )

            lines.append(
                f"• @{username} — {status}"
            )

        text = "\n".join(lines)

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=settings_menu()
    )


# ============================================================
# SUBSCRIPTION
# ============================================================

async def show_subscription(query):
    active = get_active_subscription(
        query.from_user.id
    )

    if active:
        text = (
            "💳 <b>Aktiv obuna mavjud</b>\n\n"
            f"📦 Tarif: "
            f"{PLANS.get(active['plan'], {}).get('name', active['plan'])}\n"
            f"🟢 Boshlanishi: "
            f"{format_dt(active['start_at'])}\n"
            f"🔴 Tugashi: "
            f"{format_dt(active['end_at'])}\n\n"
            "Yangi obuna olsangiz, muddati "
            "mavjud obunadan keyin davom etadi."
        )
    else:
        text = (
            "💳 <b>Obuna sotib olish</b>\n\n"
            "Kerakli muddatni tanlang:"
        )

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=subscription_menu()
    )


async def buy_subscription(query, plan):
    user_id = query.from_user.id

    price = plan_price(plan)

    balance = get_balance(user_id)

    if balance < price:
        card = get_setting(
            "payment_card",
            ""
        )

        name = get_setting(
            "payment_name",
            ""
        )

        set_state(
            user_id,
            "waiting_payment_amount",
            str(price)
        )

        text = (
            "❌ <b>Balans yetarli emas.</b>\n\n"
            f"📦 Tarif: {PLANS[plan]['name']}\n"
            f"💵 Kerak: {price:,} so‘m\n"
            f"💰 Balansingiz: {balance:,} so‘m\n\n"
        )

        if card:
            text += (
                "💳 <b>To‘lov uchun karta:</b>\n"
                f"<code>{card}</code>\n"
            )

            if name:
                text += f"👤 {name}\n"

            text += (
                "\nTo‘lovni amalga oshiring va "
                "chek rasmini shu botga yuboring."
            )
        else:
            text += (
                "⚠️ Karta hali admin tomonidan "
                "o‘rnatilmagan."
            )

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML
        )

        return

    ok, result = create_subscription(
        user_id,
        plan
    )

    if not ok:
        await query.answer(
            result,
            show_alert=True
        )
        return

    clear_state(user_id)

    await query.edit_message_text(
        "✅ <b>Obuna muvaffaqiyatli olindi!</b>\n\n"
        f"📦 Tarif: {PLANS[plan]['name']}\n"
        f"💵 Narx: {result['price']:,} so‘m\n"
        f"🟢 Boshlanishi: {format_dt(result['start'])}\n"
        f"🔴 Tugashi: {format_dt(result['end'])}",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )


# ============================================================
# RECEIPT
# ============================================================

async def receive_receipt(update, context):
    user_id = update.effective_user.id

    state, data = get_state(user_id)

    if state != "waiting_payment_amount":
        return False

    if not update.message.photo:
        await update.message.reply_text(
            "❌ Iltimos, to‘lov chekini rasm ko‘rinishida yuboring."
        )
        return True

    try:
        amount = int(data)
    except Exception:
        amount = 0

    photo_id = update.message.photo[-1].file_id

    with db_lock:
        conn = db()

        cursor = conn.execute(
            """
            INSERT INTO payments(
                user_id,
                amount,
                photo_id,
                status,
                created_at
            )
            VALUES(?, ?, ?, 'pending', ?)
            """,
            (
                user_id,
                amount,
                photo_id,
                now_str()
            )
        )

        payment_id = cursor.lastrowid

        conn.commit()
        conn.close()

    clear_state(user_id)

    await update.message.reply_text(
        "✅ <b>Chek qabul qilindi.</b>\n\n"
        f"💵 Summa: {amount:,} so‘m\n"
        f"🧾 To‘lov №{payment_id}\n\n"
        "Admin tekshirganidan keyin balansingizga "
        "qo‘shiladi.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )

    if ADMIN_ID:
        try:
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ Tasdiqlash",
                        callback_data=f"approve_payment_{payment_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ Rad etish",
                        callback_data=f"reject_payment_{payment_id}"
                    )
                ]
            ])

            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo_id,
                caption=(
                    "💳 <b>Yangi to‘lov</b>\n\n"
                    f"🧾 To‘lov: #{payment_id}\n"
                    f"👤 User ID: <code>{user_id}</code>\n"
                    f"💵 Summa: <b>{amount:,} so‘m</b>"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )

        except Exception as e:
            logger.error(
                "Admin receipt error: %s",
                e
            )

    return True


# ============================================================
# ADMIN PAYMENT APPROVAL
# ============================================================

async def approve_payment(query):
    try:
        payment_id = int(
            query.data.split("_")[-1]
        )
    except Exception:
        await query.answer(
            "Xato payment ID.",
            show_alert=True
        )
        return

    with db_lock:
        conn = db()

        payment = conn.execute(
            """
            SELECT *
            FROM payments
            WHERE id=?
            """,
            (payment_id,)
        ).fetchone()

        if not payment:
            conn.close()

            await query.answer(
                "To‘lov topilmadi.",
                show_alert=True
            )
            return

        if payment["status"] != "pending":
            conn.close()

            await query.answer(
                "Bu to‘lov allaqachon ko‘rib chiqilgan.",
                show_alert=True
            )
            return

        conn.execute(
            """
            UPDATE payments
            SET status='approved',
                approved_at=?
            WHERE id=?
            """,
            (
                now_str(),
                payment_id
            )
        )

        conn.execute(
            """
            UPDATE users
            SET balance=balance+?
            WHERE user_id=?
            """,
            (
                payment["amount"],
                payment["user_id"]
            )
        )

        conn.commit()
        conn.close()

    await query.edit_message_caption(
        caption=(
            "✅ <b>To‘lov tasdiqlandi.</b>\n\n"
            f"🧾 #{payment_id}\n"
            f"👤 {payment['user_id']}\n"
            f"💵 {payment['amount']:,} so‘m"
        ),
        parse_mode=ParseMode.HTML
    )

    await query.answer(
        "To‘lov tasdiqlandi."
    )


async def reject_payment(query):
    try:
        payment_id = int(
            query.data.split("_")[-1]
        )
    except Exception:
        await query.answer(
            "Xato payment ID.",
            show_alert=True
        )
        return

    with db_lock:
        conn = db()

        payment = conn.execute(
            """
            SELECT *
            FROM payments
            WHERE id=?
            """,
            (payment_id,)
        ).fetchone()

        if not payment:
            conn.close()

            await query.answer(
                "To‘lov topilmadi.",
                show_alert=True
            )
            return

        if payment["status"] != "pending":
            conn.close()

            await query.answer(
                "Bu to‘lov allaqachon ko‘rib chiqilgan.",
                show_alert=True
            )
            return

        conn.execute(
            """
            UPDATE payments
            SET status='rejected'
            WHERE id=?
            """,
            (payment_id,)
        )

        conn.commit()
        conn.close()

    await query.edit_message_caption(
        caption=(
            "❌ <b>To‘lov rad etildi.</b>\n\n"
            f"🧾 #{payment_id}\n"
            f"👤 {payment['user_id']}\n"
            f"💵 {payment['amount']:,} so‘m"
        ),
        parse_mode=ParseMode.HTML
    )

    await query.answer(
        "To‘lov rad etildi."
    )


# ============================================================
# ADMIN CARD
# ============================================================

async def admin_card(query):
    card = get_setting(
        "payment_card",
        ""
    )

    name = get_setting(
        "payment_name",
        ""
    )

    await query.edit_message_text(
        "💳 <b>Karta</b>\n\n"
        f"💳 Karta: <code>{card or 'O‘rnatilmagan'}</code>\n"
        f"👤 Ism: {name or 'O‘rnatilmagan'}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✏️ Karta o‘zgartirish",
                    callback_data="admin_set_card"
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Admin panel",
                    callback_data="admin_panel"
                )
            ]
        ])
    )


async def admin_set_card(query):
    set_state(
        query.from_user.id,
        "admin_card"
    )

    await query.edit_message_text(
        "💳 Yangi karta raqamini yuboring."
    )


# ============================================================
# ADMIN PRICES
# ============================================================

async def admin_prices(query):
    await query.edit_message_text(
        "💵 <b>Obuna narxlari</b>\n\n"
        f"1 kun: {plan_price('1_day'):,} so‘m\n"
        f"3 kun: {plan_price('3_day'):,} so‘m\n"
        f"7 kun: {plan_price('7_day'):,} so‘m\n"
        f"1 oy: {plan_price('1_month'):,} so‘m",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "1 kun",
                    callback_data="admin_price_1"
                ),
                InlineKeyboardButton(
                    "3 kun",
                    callback_data="admin_price_3"
                )
            ],
            [
                InlineKeyboardButton(
                    "7 kun",
                    callback_data="admin_price_7"
                ),
                InlineKeyboardButton(
                    "1 oy",
                    callback_data="admin_price_30"
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Admin panel",
                    callback_data="admin_panel"
                )
            ]
        ])
    )


# ============================================================
# ADMIN USERS
# ============================================================

async def admin_users(query):
    with db_lock:
        conn = db()

        total = conn.execute(
            "SELECT COUNT(*) AS c FROM users"
        ).fetchone()["c"]

        conn.close()

    await query.edit_message_text(
        "👤 <b>Foydalanuvchilar</b>\n\n"
        f"Jami: <b>{total}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu()
    )


# ============================================================
# ADMIN STATS
# ============================================================

async def admin_stats(query):
    with db_lock:
        conn = db()

        users = conn.execute(
            "SELECT COUNT(*) AS c FROM users"
        ).fetchone()["c"]

        payments = conn.execute(
            "SELECT COUNT(*) AS c FROM payments"
        ).fetchone()["c"]

        approved = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS s
            FROM payments
            WHERE status='approved'
            """
        ).fetchone()["s"]

        bots = conn.execute(
            "SELECT COUNT(*) AS c FROM user_bots"
        ).fetchone()["c"]

        subscriptions = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM subscriptions
            WHERE status='active'
            """
        ).fetchone()["c"]

        conn.close()

    await query.edit_message_text(
        "📊 <b>Statistika</b>\n\n"
        f"👤 Foydalanuvchilar: {users}\n"
        f"💳 To‘lovlar: {payments}\n"
        f"💵 Tasdiqlangan to‘lov: {approved:,} so‘m\n"
        f"🤖 Botlar: {bots}\n"
        f"💳 Aktiv obunalar: {subscriptions}",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu()
    )


# ============================================================
# ADMIN PAYMENTS
# ============================================================

async def admin_payments(query):
    with db_lock:
        conn = db()

        rows = conn.execute(
            """
            SELECT *
            FROM payments
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()

        conn.close()

    if not rows:
        text = "💳 To‘lovlar yo‘q."
    else:
        lines = [
            "💳 <b>To‘lovlar</b>\n"
        ]

        for row in rows:
            lines.append(
                f"#{row['id']} | "
                f"ID: {row['user_id']} | "
                f"{row['amount']:,} so‘m | "
                f"{row['status']}"
            )

        text = "\n".join(lines)

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu()
    )


# ============================================================
# ADMIN BALANCE
# ============================================================

async def admin_balance_start(query, mode):
    set_state(
        query.from_user.id,
        f"admin_balance_{mode}"
    )

    await query.edit_message_text(
        "🆔 Foydalanuvchi Telegram ID'sini yuboring."
    )


# ============================================================
# ADMIN STATE
# ============================================================

async def process_admin_state(update):
    user_id = update.effective_user.id

    if user_id != ADMIN_ID:
        return False

    state, data = get_state(user_id)

    text = (
        update.message.text or ""
    ).strip()

    if state == "admin_card":
        set_setting(
            "payment_card",
            text
        )

        clear_state(user_id)

        await update.message.reply_text(
            "✅ Karta raqami saqlandi.",
            reply_markup=admin_menu()
        )

        return True

    if state.startswith("admin_price_"):
        try:
            amount = int(text)

            if amount <= 0:
                raise ValueError

            mapping = {
                "admin_price_1": "price_1_day",
                "admin_price_3": "price_3_day",
                "admin_price_7": "price_7_day",
                "admin_price_30": "price_1_month"
            }

            key = mapping.get(state)

            if not key:
                return True

            set_setting(
                key,
                amount
            )

            clear_state(user_id)

            await update.message.reply_text(
                "✅ Narx saqlandi.",
                reply_markup=admin_menu()
            )

        except ValueError:
            await update.message.reply_text(
                "❌ Narx raqam bo‘lishi kerak."
            )

        return True

    if state in (
        "admin_balance_plus",
        "admin_balance_minus"
    ):
        try:
            target_id = int(text)

            if not get_user(target_id):
                await update.message.reply_text(
                    "❌ Bunday foydalanuvchi topilmadi."
                )
                return True

            next_state = (
                "admin_balance_plus_amount"
                if state == "admin_balance_plus"
                else "admin_balance_minus_amount"
            )

            set_state(
                user_id,
                next_state,
                str(target_id)
            )

            await update.message.reply_text(
                "💰 Summani yuboring."
            )

        except ValueError:
            await update.message.reply_text(
                "❌ ID raqam bo‘lishi kerak."
            )

        return True

    if state in (
        "admin_balance_plus_amount",
        "admin_balance_minus_amount"
    ):
        try:
            amount = int(text)

            if amount <= 0:
                raise ValueError

            target_id = int(data)

            if state == "admin_balance_plus_amount":

                change_balance(
                    target_id,
                    amount
                )

                result = (
                    "✅ Balans qo‘shildi.\n\n"
                    f"🆔 {target_id}\n"
                    f"💰 +{amount:,} so‘m"
                )

            else:

                current = get_balance(
                    target_id
                )

                if amount > current:
                    amount = current

                change_balance(
                    target_id,
                    -amount
                )

                result = (
                    "✅ Balans ayrildi.\n\n"
                    f"🆔 {target_id}\n"
                    f"💰 -{amount:,} so‘m"
                )

            clear_state(user_id)

            await update.message.reply_text(
                result,
                reply_markup=admin_menu()
            )

        except ValueError:
            await update.message.reply_text(
                "❌ Summa noto‘g‘ri."
            )

        return True

    return False


# ============================================================
# CALLBACKS
# ============================================================

async def callbacks(update, context):
    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    save_user(query.from_user)

    data = query.data

    # MAIN
    if data == "back_main":
        clear_state(user_id)

        await query.edit_message_text(
            "🏠 <b>Bosh menyu</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu()
        )

        return

    # PROFILE
    if data == "profile":
        await show_profile(query)
        return

    # BALANCE
    if data == "balance":
        await show_balance(query)
        return

    # HELP
    if data == "help":
        await show_help(query)
        return

    # CREATE BOT
    if data == "create_bot":
        await start_create_bot(query)
        return

    # SETTINGS
    if data == "settings":
        await query.edit_message_text(
            "⚙️ <b>Botni sozlash</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=settings_menu()
        )
        return

    # API
    if data == "connect_api":
        await start_api_connection(query)
        return

    # MY BOTS
    if data == "my_bots":
        await show_my_bots(query)
        return

    # SUBSCRIPTION
    if data == "subscription":
        await show_subscription(query)
        return

    if data.startswith("buy_"):
        plan = data.replace(
            "buy_",
            "",
            1
        )

        if plan in PLANS:
            await buy_subscription(
                query,
                plan
            )

        return

    # ADMIN ONLY
    if user_id != ADMIN_ID:
        return

    if data == "admin_panel":
        clear_state(user_id)

        await query.edit_message_text(
            "👑 <b>Admin panel</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_menu()
        )

        return

    if data == "admin_card":
        await admin_card(query)
        return

    if data == "admin_set_card":
        await admin_set_card(query)
        return

    if data == "admin_prices":
        await admin_prices(query)
        return

    if data == "admin_users":
        await admin_users(query)
        return

    if data == "admin_stats":
        await admin_stats(query)
        return

    if data == "admin_payments":
        await admin_payments(query)
        return

    if data == "admin_balance_plus":
        await admin_balance_start(
            query,
            "plus"
        )
        return

    if data == "admin_balance_minus":
        await admin_balance_start(
            query,
            "minus"
        )
        return

    if data.startswith("approve_payment_"):
        await approve_payment(query)
        return

    if data.startswith("reject_payment_"):
        await reject_payment(query)
        return

    if data.startswith("admin_price_"):
        set_state(
            user_id,
            data
        )

        await query.edit_message_text(
            "💵 Yangi narxni yuboring.\n\n"
            "Masalan: 5000"
        )

        return


# ============================================================
# PHOTO HANDLER
# ============================================================

async def photo_handler(update, context):
    user = update.effective_user

    if not user:
        return

    save_user(user)

    handled = await receive_receipt(
        update,
        context
    )

    if handled:
        return


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update, context):
    user = update.effective_user

    if not user:
        return

    save_user(user)

    # ADMIN STATE
    if user.id == ADMIN_ID:

        handled = await process_admin_state(
            update
        )

        if handled:
            return

    state, _ = get_state(
        user.id
    )

    # API
    if state == "waiting_api_key":
        await receive_api_key(
            update,
            context
        )
        return

    # BOT TOKEN
    if state == "waiting_bot_token":
        await receive_bot_token(
            update,
            context
        )
        return

    # Payment amount state
    if state == "waiting_payment_amount":
        await update.message.reply_text(
            "❗ Avval to‘lovni amalga oshiring va "
            "chek rasmini yuboring."
        )
        return


# ============================================================
# ADMIN COMMAND
# ============================================================

async def admin_command(update, context):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(
            "❌ Siz admin emassiz."
        )
        return

    await update.message.reply_text(
        "👑 <b>Admin panel</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu()
    )


# ============================================================
# SUBSCRIPTION EXPIRY CHECK
# ============================================================

async def subscription_checker(context):
    try:
        expire_old_subscriptions()
    except Exception as e:
        logger.error(
            "Subscription checker error: %s",
            e
        )


# ============================================================
# RENDER HEALTH
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        self.wfile.write(
            b"BOT OK"
        )

    def log_message(self, format, *args):
        return


def start_health_server():

    try:

        server = HTTPServer(
            (
                "0.0.0.0",
                PORT
            ),
            HealthHandler
        )

        logger.info(
            "Health server started on port %s",
            PORT
        )

        server.serve_forever()

    except Exception as e:

        logger.error(
            "Health server error: %s",
            e
        )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN Render Environment Variables'da yo‘q!"
        )

    if ADMIN_ID == 0:

        raise RuntimeError(
            "ADMIN_ID Render Environment Variables'da noto‘g‘ri!"
        )

    init_db()

    # Render port
    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # COMMANDS
    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    # CALLBACKS
    application.add_handler(
        CallbackQueryHandler(
            callbacks
        )
    )

    # PHOTOS / CHEKS
    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_handler
        )
    )

    # TEXT
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    # OBUNA TEKSHIRUVI
    application.job_queue.run_repeating(
        subscription_checker,
        interval=60,
        first=10
    )

    logger.info(
        "Bot ishga tushmoqda..."
    )

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()

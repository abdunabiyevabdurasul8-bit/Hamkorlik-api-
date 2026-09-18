import os
import sqlite3
import asyncio
import logging
import threading
import time
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

# PlayPay
PLAYPAY_BASE_URL = "https://playpay.uz/api/v1"

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)


# ============================================================
# SQLITE
# ============================================================

db_lock = threading.Lock()


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

        # Default settings
        defaults = {
            "payment_card": "",
            "payment_name": "",
        }

        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, value)
            )

        conn.commit()
        conn.close()


# ============================================================
# YORDAMCHI FUNKSIYALAR
# ============================================================

def now_str():
    return datetime.now(timezone.utc).isoformat()


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
        conn.execute("""
            INSERT INTO settings(key, value)
            VALUES(?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
        """, (key, str(value)))
        conn.commit()
        conn.close()


def save_user(user):
    with db_lock:
        conn = db()

        conn.execute("""
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
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            now_str()
        ))

        conn.commit()
        conn.close()


def get_user(user_id):
    with db_lock:
        conn = db()
        row = conn.execute(
            "SELECT * FROM users WHERE user_id=?",
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
            "UPDATE users SET balance=balance+? WHERE user_id=?",
            (amount, user_id)
        )

        conn.commit()
        conn.close()


def set_state(user_id, state, data=""):
    with db_lock:
        conn = db()

        conn.execute("""
            INSERT INTO user_states(user_id, state, data)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                state=excluded.state,
                data=excluded.data
        """, (user_id, state, data))

        conn.commit()
        conn.close()


def get_state(user_id):
    with db_lock:
        conn = db()

        row = conn.execute(
            "SELECT * FROM user_states WHERE user_id=?",
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
            "DELETE FROM user_states WHERE user_id=?",
            (user_id,)
        )

        conn.commit()
        conn.close()


# ============================================================
# MENU
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

    text = (
        "Assalomu Aleykum! 👋\n\n"
        "🤖 Telegram bot platformasiga xush kelibsiz.\n\n"
        "Quyidagi menyudan kerakli bo‘limni tanlang:"
    )

    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=main_menu()
        )


# ============================================================
# PROFILE
# ============================================================

async def show_profile(query):
    user_id = query.from_user.id

    row = get_user(user_id)

    if not row:
        text = "Profil topilmadi."
    else:
        username = row["username"] or "username yo‘q"

        text = (
            "👤 <b>Profil</b>\n\n"
            f"🆔 ID: <code>{user_id}</code>\n"
            f"👤 Username: @{username}\n"
            f"💰 Balans: <b>{row['balance']:,} so‘m</b>"
        )

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )


# ============================================================
# BALANCE
# ============================================================

async def show_balance(query):
    user_id = query.from_user.id

    balance = get_balance(user_id)

    text = (
        "💰 <b>Balansingiz</b>\n\n"
        f"💵 {balance:,} so‘m"
    )

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )


# ============================================================
# HELP
# ============================================================

async def show_help(query):
    text = (
        "ℹ️ <b>Yordam</b>\n\n"
        "🤖 Bot yaratish — o‘zingizning Telegram botingizni ulash.\n\n"
        "⚙️ Botni sozlash — API ulash va botlarni ko‘rish.\n\n"
        "🔌 API ulash — PlayPay API Key ulash.\n\n"
        "💳 Obuna sotib olish — hozircha ishlamaydi."
    )

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu()
    )


# ============================================================
# API ULASH
# ============================================================

async def start_api_connection(query):
    user_id = query.from_user.id

    set_state(user_id, "waiting_api_key")

    text = (
        "🔌 <b>API ulash</b>\n\n"
        "PlayPay API Key'ingizni yuboring.\n\n"
        "Masalan:\n"
        "<code>pp_xxxxxxxxx</code>\n\n"
        "❗ Secret Key kerak emas.\n"
        "❗ Status endpoint kerak emas.\n"
        "❗ Services endpoint kerak emas.\n\n"
        "Faqat API Key yuboring."
    )

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML
    )


def check_playpay_api_key(api_key):
    """
    PlayPay API Key bilan /games endpointini tekshiradi.
    """

    try:
        response = requests.get(
            f"{PLAYPAY_BASE_URL}/games",
            headers={
                "X-API-Key": api_key,
                "Accept": "application/json"
            },
            timeout=15
        )

        return response

    except requests.RequestException as e:
        logger.error("PlayPay API error: %s", e)
        return None


async def receive_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    state, _ = get_state(user_id)

    if state != "waiting_api_key":
        return False

    api_key = (update.message.text or "").strip()

    if not api_key:
        await update.message.reply_text(
            "❌ API Key bo‘sh bo‘lishi mumkin emas."
        )
        return True

    if len(api_key) < 8:
        await update.message.reply_text(
            "❌ API Key noto‘g‘ri ko‘rinmoqda.\n\n"
            "PlayPay API Key'ni qayta yuboring."
        )
        return True

    await update.message.reply_text(
        "⏳ API Key tekshirilmoqda..."
    )

    response = await asyncio.to_thread(
        check_playpay_api_key,
        api_key
    )

    if response is None:
        await update.message.reply_text(
            "❌ PlayPay serveriga ulanib bo‘lmadi.\n"
            "Keyinroq qayta urinib ko‘ring."
        )
        return True

    if response.status_code == 200:
        with db_lock:
            conn = db()

            current_time = now_str()

            conn.execute("""
                INSERT INTO api_connections(
                    user_id,
                    api_key,
                    active,
                    created_at,
                    updated_at
                )
                VALUES(?, ?, 1, ?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET
                    api_key=excluded.api_key,
                    active=1,
                    updated_at=excluded.updated_at
            """, (
                user_id,
                api_key,
                current_time,
                current_time
            ))

            conn.commit()
            conn.close()

        clear_state(user_id)

        await update.message.reply_text(
            "✅ <b>API muvaffaqiyatli ulandi!</b>\n\n"
            "PlayPay API Key saqlandi.\n\n"
            "Endi bu API Key sizning akkauntingizga tegishli.",
            parse_mode=ParseMode.HTML,
            reply_markup=settings_menu()
        )

        return True

    if response.status_code == 401:
        await update.message.reply_text(
            "❌ <b>API Key noto‘g‘ri yoki faol emas.</b>\n\n"
            "PlayPay bergan faol <code>pp_...</code> API Key'ni yuboring.",
            parse_mode=ParseMode.HTML
        )
        return True

    try:
        body = response.text[:500]
    except Exception:
        body = ""

    await update.message.reply_text(
        f"❌ API xatosi: <b>{response.status_code}</b>\n\n"
        f"<code>{body}</code>",
        parse_mode=ParseMode.HTML
    )

    return True


# ============================================================
# BOT YARATISH
# ============================================================

async def start_create_bot(query):
    user_id = query.from_user.id

    set_state(user_id, "waiting_bot_token")

    text = (
        "🤖 <b>Bot yaratish</b>\n\n"
        "BotFather'dan olgan bot tokeningizni yuboring.\n\n"
        "Masalan:\n"
        "<code>123456789:AA...</code>\n\n"
        "⚠️ Tokenni boshqa odamga yubormang."
    )

    await query.edit_message_text(
        text,
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
        logger.error("getMe error: %s", e)
        return None


def bot_token_exists(token):
    with db_lock:
        conn = db()

        row = conn.execute(
            "SELECT id FROM user_bots WHERE bot_token=?",
            (token,)
        ).fetchone()

        conn.close()

    return row is not None


async def receive_bot_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    state, _ = get_state(user_id)

    if state != "waiting_bot_token":
        return False

    token = (update.message.text or "").strip()

    if not token:
        await update.message.reply_text(
            "❌ Token yuboring."
        )
        return True

    if bot_token_exists(token):
        clear_state(user_id)

        await update.message.reply_text(
            "❌ <b>Bu bot allaqachon qo‘shilgan.</b>\n\n"
            "Bitta bot tokenini ikkinchi marta ulash mumkin emas.",
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
            "❌ Bot token noto‘g‘ri yoki bot ishlamayapti.\n\n"
            "BotFather bergan tokenni qayta yuboring."
        )
        return True

    bot_username = info.get("username", "")

    with db_lock:
        conn = db()

        try:
            conn.execute("""
                INSERT INTO user_bots(
                    user_id,
                    bot_token,
                    bot_username,
                    created_at,
                    active
                )
                VALUES(?, ?, ?, ?, 1)
            """, (
                user_id,
                token,
                bot_username,
                now_str()
            ))

            conn.commit()

        except sqlite3.IntegrityError:
            conn.close()

            await update.message.reply_text(
                "❌ Bu bot tokeni allaqachon tizimda mavjud."
            )

            return True

        conn.close()

    clear_state(user_id)

    await update.message.reply_text(
        "✅ <b>Bot muvaffaqiyatli qo‘shildi!</b>\n\n"
        f"🤖 @{bot_username}\n\n"
        "Endi botingiz tizimga saqlandi.",
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

        rows = conn.execute("""
            SELECT *
            FROM user_bots
            WHERE user_id=?
            ORDER BY id DESC
        """, (user_id,)).fetchall()

        conn.close()

    if not rows:
        text = (
            "🤖 <b>Mening botlarim</b>\n\n"
            "Hozircha hech qanday bot qo‘shilmagan."
        )
    else:
        lines = [
            "🤖 <b>Mening botlarim</b>\n"
        ]

        for row in rows:
            username = row["bot_username"] or "Noma'lum"

            status = "🟢 Aktiv" if row["active"] else "🔴 O‘chirilgan"

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
# HOZIRCHA ISHLAMAYDI
# ============================================================

async def subscription_disabled(query):
    await query.answer(
        "💳 Obuna sotib olish hozircha ishlamaydi.",
        show_alert=True
    )


# ============================================================
# ADMIN
# ============================================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id != ADMIN_ID:
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
# ADMIN CARD
# ============================================================

async def admin_card(query):
    card = get_setting("payment_card", "")

    name = get_setting("payment_name", "")

    text = (
        "💳 <b>Karta</b>\n\n"
        f"💳 Karta: <code>{card or 'O‘rnatilmagan'}</code>\n"
        f"👤 Ism: {name or 'O‘rnatilmagan'}"
    )

    keyboard = InlineKeyboardMarkup([
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

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


async def admin_set_card(query):
    set_state(query.from_user.id, "admin_card")

    await query.edit_message_text(
        "💳 Yangi karta raqamini yuboring."
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

    text = (
        "👤 <b>Foydalanuvchilar</b>\n\n"
        f"Jami: <b>{total}</b>"
    )

    await query.edit_message_text(
        text,
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

        bots = conn.execute(
            "SELECT COUNT(*) AS c FROM user_bots"
        ).fetchone()["c"]

        api_count = conn.execute(
            "SELECT COUNT(*) AS c FROM api_connections WHERE active=1"
        ).fetchone()["c"]

        conn.close()

    text = (
        "📊 <b>Statistika</b>\n\n"
        f"👤 Foydalanuvchilar: {users}\n"
        f"💳 To‘lovlar: {payments}\n"
        f"🤖 Botlar: {bots}\n"
        f"🔌 Aktiv API: {api_count}"
    )

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


async def admin_payments(query):
    with db_lock:
        conn = db()

        rows = conn.execute("""
            SELECT *
            FROM payments
            ORDER BY id DESC
            LIMIT 10
        """).fetchall()

        conn.close()

    if not rows:
        text = "💳 To‘lovlar yo‘q."
    else:
        lines = ["💳 <b>Oxirgi to‘lovlar</b>\n"]

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
# ADMIN MESSAGE STATE
# ============================================================

async def process_admin_state(update: Update):
    user_id = update.effective_user.id

    if user_id != ADMIN_ID:
        return False

    state, _ = get_state(user_id)

    text = (update.message.text or "").strip()

    if state == "admin_card":
        set_setting("payment_card", text)
        clear_state(user_id)

        await update.message.reply_text(
            "✅ Karta raqami saqlandi.",
            reply_markup=admin_menu()
        )

        return True

    if state == "admin_balance_plus":
        try:
            target_id = int(text)

            set_state(
                user_id,
                "admin_balance_plus_amount",
                str(target_id)
            )

            await update.message.reply_text(
                "💰 Endi qo‘shiladigan summani yuboring."
            )

        except ValueError:
            await update.message.reply_text(
                "❌ Telegram ID raqam bo‘lishi kerak."
            )

        return True

    if state == "admin_balance_minus":
        try:
            target_id = int(text)

            set_state(
                user_id,
                "admin_balance_minus_amount",
                str(target_id)
            )

            await update.message.reply_text(
                "💰 Endi ayriladigan summani yuboring."
            )

        except ValueError:
            await update.message.reply_text(
                "❌ Telegram ID raqam bo‘lishi kerak."
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

            _, data = get_state(user_id)

            target_id = int(data)

            if not get_user(target_id):
                clear_state(user_id)

                await update.message.reply_text(
                    "❌ Bunday foydalanuvchi topilmadi."
                )

                return True

            if state == "admin_balance_plus_amount":
                change_balance(target_id, amount)

                result = (
                    f"✅ {target_id} balansiga "
                    f"{amount:,} so‘m qo‘shildi."
                )

            else:
                current = get_balance(target_id)

                if amount > current:
                    amount = current

                change_balance(target_id, -amount)

                result = (
                    f"✅ {target_id} balansidan "
                    f"{amount:,} so‘m ayrildi."
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
# CALLBACKLAR
# ============================================================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    save_user(query.from_user)

    data = query.data

    # ----------------------------
    # MAIN
    # ----------------------------

    if data == "back_main":
        clear_state(user_id)

        await query.edit_message_text(
            "🏠 <b>Bosh menyu</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu()
        )

        return

    # ----------------------------
    # PROFILE
    # ----------------------------

    if data == "profile":
        await show_profile(query)
        return

    # ----------------------------
    # BALANCE
    # ----------------------------

    if data == "balance":
        await show_balance(query)
        return

    # ----------------------------
    # HELP
    # ----------------------------

    if data == "help":
        await show_help(query)
        return

    # ----------------------------
    # CREATE BOT
    # ----------------------------

    if data == "create_bot":
        await start_create_bot(query)
        return

    # ----------------------------
    # SETTINGS
    # ----------------------------

    if data == "settings":
        await query.edit_message_text(
            "⚙️ <b>Botni sozlash</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=settings_menu()
        )
        return

    # ----------------------------
    # CONNECT API
    # ----------------------------

    if data == "connect_api":
        await start_api_connection(query)
        return

    # ----------------------------
    # MY BOTS
    # ----------------------------

    if data == "my_bots":
        await show_my_bots(query)
        return

    # ----------------------------
    # SUBSCRIPTION
    # ----------------------------

    if data == "subscription":
        await subscription_disabled(query)
        return

    # ----------------------------
    # ADMIN
    # ----------------------------

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


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user:
        return

    save_user(user)

    # Admin state
    if user.id == ADMIN_ID:
        handled = await process_admin_state(update)

        if handled:
            return

    # User state
    state, _ = get_state(user.id)

    if state == "waiting_api_key":
        await receive_api_key(update, context)
        return

    if state == "waiting_bot_token":
        await receive_bot_token(update, context)
        return


# ============================================================
# ADMIN COMMAND
# ============================================================

async def admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text(
        "👑 <b>Admin panel</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu()
    )


# ============================================================
# RENDER HEALTH SERVER
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
            ("0.0.0.0", PORT),
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

    # Commands
    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("admin", admin_command)
    )

    # Callback buttons
    application.add_handler(
        CallbackQueryHandler(callbacks)
    )

    # Text messages
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    logger.info("Bot ishga tushmoqda...")

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()

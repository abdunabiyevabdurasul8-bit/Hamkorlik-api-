import os
import json
import sqlite3
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
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
# SOZLAMALAR
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip())
except Exception:
    ADMIN_ID = 0

DB_FILE = "bot.db"

PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"BOT OK")

    def log_message(self, format, *args):
        return


def start_health_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        logger.info("Health server started on port %s", PORT)
        server.serve_forever()
    except Exception as e:
        logger.error("Health server error: %s", e)


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance INTEGER DEFAULT 0,
            subscription_until TEXT DEFAULT '',
            created_at TEXT DEFAULT ''
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            bot_token TEXT NOT NULL,
            bot_id INTEGER DEFAULT 0,
            bot_username TEXT DEFAULT '',
            bot_name TEXT DEFAULT '',
            welcome_text TEXT DEFAULT 'Assalomu Aleykum!',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER DEFAULT 0,
            photo_id TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT '',
            approved_at TEXT DEFAULT ''
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan TEXT DEFAULT '',
            amount INTEGER DEFAULT 0,
            days INTEGER DEFAULT 0,
            started_at TEXT DEFAULT '',
            expires_at TEXT DEFAULT '',
            status TEXT DEFAULT 'active'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        )
    """)

    # Foydalanuvchining shaxsiy API ulanishlari
    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT DEFAULT '',
            base_url TEXT DEFAULT '',
            api_key TEXT DEFAULT '',
            secret TEXT DEFAULT '',
            api_type TEXT DEFAULT 'generic',
            balance_endpoint TEXT DEFAULT '',
            services_endpoint TEXT DEFAULT '',
            order_endpoint TEXT DEFAULT '',
            status_endpoint TEXT DEFAULT '',
            auth_type TEXT DEFAULT 'bearer',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        )
    """)

    # API xizmatlari
    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_id INTEGER NOT NULL,
            external_id TEXT DEFAULT '',
            name TEXT DEFAULT '',
            description TEXT DEFAULT '',
            api_price REAL DEFAULT 0,
            markup REAL DEFAULT 0,
            sale_price REAL DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        )
    """)

    # API buyurtmalari
    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            api_id INTEGER NOT NULL,
            service_id INTEGER DEFAULT 0,
            external_order_id TEXT DEFAULT '',
            target TEXT DEFAULT '',
            quantity TEXT DEFAULT '',
            api_price REAL DEFAULT 0,
            markup REAL DEFAULT 0,
            sale_price REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        )
    """)

    # Ustama
    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_markup (
            user_id INTEGER PRIMARY KEY,
            markup REAL DEFAULT 0,
            updated_at TEXT DEFAULT ''
        )
    """)

    # Promo
    cur.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            amount INTEGER DEFAULT 0,
            max_uses INTEGER DEFAULT 0,
            used_count INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT ''
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS promo_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            promo_code TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            used_at TEXT DEFAULT '',
            UNIQUE(promo_code, user_id)
        )
    """)

    # Boshlang'ich sozlamalar
    defaults = {
        "payment_card": "",
        "payment_name": "",
        "welcome_text": "Assalomu Aleykum!",
        "support_text": "Yordam uchun administratorga murojaat qiling.",
        "price_1_day": "5000",
        "price_3_day": "13000",
        "price_7_day": "25000",
        "price_1_month": "43000",
    }

    for key, value in defaults.items():
        cur.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (key, value),
        )

    conn.commit()
    conn.close()


# ============================================================
# YORDAMCHI FUNKSIYALAR
# ============================================================

def now_str():
    return datetime.now(timezone.utc).isoformat()


def get_setting(key, default=""):
    conn = db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (key,),
    ).fetchone()
    conn.close()

    if row is None:
        return default

    return row["value"]


def set_setting(key, value):
    conn = db()
    conn.execute(
        "INSERT INTO settings(key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def ensure_user(user):
    if not user:
        return

    conn = db()

    conn.execute("""
        INSERT INTO users(
            user_id,
            username,
            first_name,
            balance,
            subscription_until,
            created_at
        )
        VALUES (?, ?, ?, 0, '', ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (
        user.id,
        user.username or "",
        user.first_name or "",
        now_str(),
    ))

    conn.commit()
    conn.close()


def get_user(user_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,),
    ).fetchone()
    conn.close()
    return row


def get_markup(user_id):
    conn = db()
    row = conn.execute(
        "SELECT markup FROM api_markup WHERE user_id=?",
        (user_id,),
    ).fetchone()
    conn.close()

    if row:
        return float(row["markup"])

    return 0.0


def set_markup(user_id, value):
    conn = db()
    conn.execute("""
        INSERT INTO api_markup(user_id, markup, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET
            markup=excluded.markup,
            updated_at=excluded.updated_at
    """, (
        user_id,
        float(value),
        now_str(),
    ))
    conn.commit()
    conn.close()


def get_price(plan):
    if plan == "1_day":
        return int(get_setting("price_1_day", "5000"))

    if plan == "3_day":
        return int(get_setting("price_3_day", "13000"))

    if plan == "7_day":
        return int(get_setting("price_7_day", "25000"))

    if plan == "1_month":
        return int(get_setting("price_1_month", "43000"))

    return 0


def plan_days(plan):
    if plan == "1_day":
        return 1

    if plan == "3_day":
        return 3

    if plan == "7_day":
        return 7

    if plan == "1_month":
        return 30

    return 0


def money(value):
    try:
        return f"{int(float(value)):,}".replace(",", " ")
    except Exception:
        return "0"


# ============================================================
# KLAVIATURALAR
# ============================================================

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🤖 Bot yaratish", callback_data="create_bot"),
            InlineKeyboardButton("⚙️ Botni sozlash", callback_data="bot_settings"),
        ],
        [
            InlineKeyboardButton("💳 Obuna sotib olish", callback_data="buy_subscription"),
        ],
        [
            InlineKeyboardButton("👤 Profil", callback_data="profile"),
            InlineKeyboardButton("💰 Balans", callback_data="balance"),
        ],
        [
            InlineKeyboardButton("ℹ️ Yordam", callback_data="help"),
        ],
    ])


def back_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="main_menu")]
    ])


def bot_settings_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔌 Xizmatga ulash",
                callback_data="connect_service"
            )
        ],
        [
            InlineKeyboardButton(
                "💰 API balansi",
                callback_data="api_balance"
            )
        ],
        [
            InlineKeyboardButton(
                "📦 Xizmatlarim",
                callback_data="my_services"
            )
        ],
        [
            InlineKeyboardButton(
                "➕ Ustama",
                callback_data="set_markup"
            )
        ],
        [
            InlineKeyboardButton(
                "✏️ Welcome matn",
                callback_data="edit_welcome"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Orqaga",
                callback_data="main_menu"
            )
        ],
    ])


def connect_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📝 Qo‘lda ulash",
                callback_data="manual_api"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 API bilan AUTO",
                callback_data="auto_api"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Orqaga",
                callback_data="bot_settings"
            )
        ],
    ])


def subscription_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"1 kun — {money(get_price('1_day'))} UZS",
                callback_data="sub_1_day"
            )
        ],
        [
            InlineKeyboardButton(
                f"3 kun — {money(get_price('3_day'))} UZS",
                callback_data="sub_3_day"
            )
        ],
        [
            InlineKeyboardButton(
                f"7 kun — {money(get_price('7_day'))} UZS",
                callback_data="sub_7_day"
            )
        ],
        [
            InlineKeyboardButton(
                f"1 oy — {money(get_price('1_month'))} UZS",
                callback_data="sub_1_month"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Orqaga",
                callback_data="main_menu"
            )
        ],
    ])


def admin_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💳 Karta", callback_data="admin_card"),
            InlineKeyboardButton("💵 Narxlar", callback_data="admin_prices"),
        ],
        [
            InlineKeyboardButton(
                "👥 Foydalanuvchilar",
                callback_data="admin_users"
            ),
            InlineKeyboardButton(
                "📊 Statistika",
                callback_data="admin_stats"
            ),
        ],
        [
            InlineKeyboardButton(
                "🚪 Admin paneldan chiqish",
                callback_data="main_menu"
            )
        ],
    ])


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    # Har doim oddiy menyu
    await update.message.reply_text(
        "Assalomu Aleykum!\n\n"
        "Kerakli bo‘limni tanlang:",
        reply_markup=main_menu(),
    )


# ============================================================
# ADMIN
# ============================================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if user.id != ADMIN_ID:
        await update.message.reply_text(
            "❌ Sizda admin huquqi yo‘q."
        )
        return

    await update.message.reply_text(
        "🔐 Admin panel",
        reply_markup=admin_menu(),
    )


# ============================================================
# CALLBACK
# ============================================================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    ensure_user(user)

    data = query.data

    # --------------------------------------------------------
    # MAIN
    # --------------------------------------------------------

    if data == "main_menu":
        await query.edit_message_text(
            "Assalomu Aleykum!\n\n"
            "Kerakli bo‘limni tanlang:",
            reply_markup=main_menu(),
        )
        return

    # --------------------------------------------------------
    # PROFILE
    # --------------------------------------------------------

    if data == "profile":
        row = get_user(user.id)

        if row:
            balance = row["balance"]
            subscription = row["subscription_until"] or "Yo‘q"
        else:
            balance = 0
            subscription = "Yo‘q"

        text = (
            "👤 <b>Profil</b>\n\n"
            f"🆔 ID: <code>{user.id}</code>\n"
            f"👤 Username: @{user.username or 'yo‘q'}\n"
            f"💰 Balans: <b>{money(balance)} UZS</b>\n"
            f"📅 Obuna: <b>{subscription}</b>"
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=back_menu(),
        )
        return

    # --------------------------------------------------------
    # BALANCE
    # --------------------------------------------------------

    if data == "balance":
        row = get_user(user.id)
        balance = row["balance"] if row else 0

        card = get_setting("payment_card", "")
        name = get_setting("payment_name", "")

        text = (
            "💰 <b>Balans</b>\n\n"
            f"Joriy balans: <b>{money(balance)} UZS</b>\n\n"
            "Balansni to‘ldirish uchun:\n"
            f"💳 Karta: <code>{card or 'Admin karta kiritmagan'}</code>\n"
            f"👤 Ism: {name or '—'}\n\n"
            "To‘lov qilganingizdan so‘ng chekni yuboring."
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=back_menu(),
        )
        return

    # --------------------------------------------------------
    # HELP
    # --------------------------------------------------------

    if data == "help":
        text = get_setting(
            "support_text",
            "Yordam uchun administratorga murojaat qiling."
        )

        await query.edit_message_text(
            "ℹ️ <b>Yordam</b>\n\n" + text,
            parse_mode="HTML",
            reply_markup=back_menu(),
        )
        return

    # --------------------------------------------------------
    # CREATE BOT
    # --------------------------------------------------------

    if data == "create_bot":
        context.user_data["state"] = "waiting_bot_token"

        await query.edit_message_text(
            "🤖 <b>Bot yaratish</b>\n\n"
            "BotFather orqali bot yarating va "
            "bot tokenini shu yerga yuboring.\n\n"
            "Masalan:\n"
            "<code>123456:ABC...</code>",
            parse_mode="HTML",
            reply_markup=back_menu(),
        )
        return

    # --------------------------------------------------------
    # BOT SETTINGS
    # --------------------------------------------------------

    if data == "bot_settings":
        await query.edit_message_text(
            "⚙️ <b>Botni sozlash</b>\n\n"
            "Kerakli bo‘limni tanlang:",
            parse_mode="HTML",
            reply_markup=bot_settings_menu(),
        )
        return

    # --------------------------------------------------------
    # CONNECT SERVICE
    # --------------------------------------------------------

    if data == "connect_service":
        await query.edit_message_text(
            "🔌 <b>Xizmatga ulash</b>\n\n"
            "API xizmatini qanday ulaysiz?",
            parse_mode="HTML",
            reply_markup=connect_menu(),
        )
        return

    # --------------------------------------------------------
    # MANUAL API
    # --------------------------------------------------------

    if data == "manual_api":
        context.user_data["state"] = "api_base_url"

        await query.edit_message_text(
            "📝 <b>Qo‘lda API ulash</b>\n\n"
            "1-qadam.\n"
            "API Base URL manzilini yuboring.\n\n"
            "Masalan:\n"
            "<code>https://example.com/api</code>",
            parse_mode="HTML",
            reply_markup=back_menu(),
        )
        return

    # --------------------------------------------------------
    # AUTO API
    # --------------------------------------------------------

    if data == "auto_api":
        context.user_data["state"] = "api_base_url"

        await query.edit_message_text(
            "🤖 <b>API bilan AUTO</b>\n\n"
            "Avval API Base URL manzilini yuboring.\n\n"
            "Keyin bot sizdan API Key va boshqa "
            "kerakli ma’lumotlarni so‘raydi.\n\n"
            "⚠️ Har xil API provayderlarning formati "
            "har xil bo‘lishi mumkin.",
            parse_mode="HTML",
            reply_markup=back_menu(),
        )
        return

    # --------------------------------------------------------
    # API BALANCE
    # --------------------------------------------------------

    if data == "api_balance":
        conn = db()
        row = conn.execute("""
            SELECT *
            FROM api_connections
            WHERE user_id=? AND active=1
            ORDER BY id DESC
            LIMIT 1
        """, (user.id,)).fetchone()
        conn.close()

        if not row:
            await query.edit_message_text(
                "❌ Sizda ulangan API yo‘q.",
                reply_markup=back_menu(),
            )
            return

        result = await api_balance(row)

        await query.edit_message_text(
            result,
            parse_mode="HTML",
            reply_markup=back_menu(),
        )
        return

    # --------------------------------------------------------
    # MY SERVICES
    # --------------------------------------------------------

    if data == "my_services":
        conn = db()

        rows = conn.execute("""
            SELECT *
            FROM api_services
            WHERE api_id IN (
                SELECT id
                FROM api_connections
                WHERE user_id=? AND active=1
            )
            AND active=1
            ORDER BY id DESC
            LIMIT 30
        """, (user.id,)).fetchall()

        conn.close()

        if not rows:
            await query.edit_message_text(
                "📦 Hozircha xizmatlar yuklanmagan.",
                reply_markup=back_menu(),
            )
            return

        text = "📦 <b>Xizmatlarim</b>\n\n"

        for r in rows:
            text += (
                f"🆔 {r['external_id']}\n"
                f"📌 {r['name']}\n"
                f"💵 API: {money(r['api_price'])} UZS\n"
                f"➕ Ustama: {money(r['markup'])} UZS\n"
                f"💰 Sotuv: {money(r['sale_price'])} UZS\n\n"
            )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=back_menu(),
        )
        return

    # --------------------------------------------------------
    # MARKUP
    # --------------------------------------------------------

    if data == "set_markup":
        context.user_data["state"] = "markup"

        current = get_markup(user.id)

        await query.edit_message_text(
            "➕ <b>USTAMA</b>\n\n"
            f"Hozirgi ustama: <b>{money(current)} UZS</b>\n\n"
            "Yangi ustamani UZSda yuboring.\n\n"
            "Masalan:\n"
            "<code>2000</code>",
            parse_mode="HTML",
            reply_markup=back_menu(),
        )
        return

    # --------------------------------------------------------
    # WELCOME
    # --------------------------------------------------------

    if data == "edit_welcome":
        context.user_data["state"] = "welcome"

        await query.edit_message_text(
            "✏️ Yangi welcome matnni yuboring:",
            reply_markup=back_menu(),
        )
        return

    # --------------------------------------------------------
    # SUBSCRIPTION
    # --------------------------------------------------------

    if data == "buy_subscription":
        await query.edit_message_text(
            "💳 <b>Obuna sotib olish</b>\n\n"
            "Kerakli muddatni tanlang:",
            parse_mode="HTML",
            reply_markup=subscription_menu(),
        )
        return

    if data.startswith("sub_"):
        plan = data.replace("sub_", "")

        price = get_price(plan)
        days = plan_days(plan)

        row = get_user(user.id)

        if not row:
            await query.edit_message_text(
                "❌ Foydalanuvchi topilmadi.",
                reply_markup=back_menu(),
            )
            return

        balance = int(row["balance"])

        if balance < price:
            card = get_setting("payment_card", "")
            name = get_setting("payment_name", "")

            await query.edit_message_text(
                "❌ <b>Balans yetarli emas.</b>\n\n"
                f"💰 Sizda: {money(balance)} UZS\n"
                f"💳 Kerak: {money(price)} UZS\n\n"
                f"💳 Karta: <code>{card or 'Admin karta kiritmagan'}</code>\n"
                f"👤 Ism: {name or '—'}\n\n"
                "To‘lov qilib, chekni yuboring.",
                parse_mode="HTML",
                reply_markup=back_menu(),
            )
            return

        current_until = row["subscription_until"]

        now = datetime.now(timezone.utc)

        if current_until:
            try:
                old_until = datetime.fromisoformat(current_until)

                if old_until > now:
                    start = old_until
                else:
                    start = now
            except Exception:
                start = now
        else:
            start = now

        expires = start + timedelta(days=days)

        conn = db()

        conn.execute("""
            UPDATE users
            SET balance=balance-?,
                subscription_until=?
            WHERE user_id=?
        """, (
            price,
            expires.isoformat(),
            user.id,
        ))

        conn.execute("""
            INSERT INTO subscriptions(
                user_id,
                plan,
                amount,
                days,
                started_at,
                expires_at,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, 'active')
        """, (
            user.id,
            plan,
            price,
            days,
            start.isoformat(),
            expires.isoformat(),
        ))

        conn.commit()
        conn.close()

        await query.edit_message_text(
            "✅ <b>Obuna faollashtirildi!</b>\n\n"
            f"📦 Muddat: {days} kun\n"
            f"💵 To‘lov: {money(price)} UZS\n"
            f"📅 Tugash vaqti: {expires.strftime('%Y-%m-%d %H:%M')}\n\n"
            "Agar oldingi obunangiz hali tugamagan bo‘lsa, "
            "yangi muddat unga qo‘shildi.",
            parse_mode="HTML",
            reply_markup=back_menu(),
        )
        return

    # ========================================================
    # ADMIN
    # ========================================================

    if user.id != ADMIN_ID:
        if data.startswith("admin_"):
            await query.edit_message_text(
                "❌ Sizda admin huquqi yo‘q.",
                reply_markup=back_menu(),
            )
            return

    if data == "admin_card":
        context.user_data["state"] = "admin_card"

        current_card = get_setting("payment_card", "")
        current_name = get_setting("payment_name", "")

        await query.edit_message_text(
            "💳 <b>Karta sozlamasi</b>\n\n"
            f"Hozirgi karta: <code>{current_card or 'yo‘q'}</code>\n"
            f"Hozirgi ism: {current_name or 'yo‘q'}\n\n"
            "Yangi karta raqamini yuboring.\n"
            "Keyin karta egasining ismini so‘rayman.",
            parse_mode="HTML",
            reply_markup=back_menu(),
        )
        return

    if data == "admin_prices":
        context.user_data["state"] = "admin_prices"

        await query.edit_message_text(
            "💵 <b>Narxlar</b>\n\n"
            f"1 kun: {money(get_price('1_day'))} UZS\n"
            f"3 kun: {money(get_price('3_day'))} UZS\n"
            f"7 kun: {money(get_price('7_day'))} UZS\n"
            f"1 oy: {money(get_price('1_month'))} UZS\n\n"
            "Yangi narxlarni quyidagi formatda yuboring:\n\n"
            "<code>5000 13000 25000 43000</code>",
            parse_mode="HTML",
            reply_markup=back_menu(),
        )
        return

    if data == "admin_users":
        conn = db()
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM users"
        ).fetchone()["c"]
        conn.close()

        await query.edit_message_text(
            f"👥 <b>Foydalanuvchilar</b>\n\n"
            f"Jami: <b>{count}</b>",
            parse_mode="HTML",
            reply_markup=admin_menu(),
        )
        return

    if data == "admin_stats":
        conn = db()

        users = conn.execute(
            "SELECT COUNT(*) AS c FROM users"
        ).fetchone()["c"]

        active_subs = conn.execute("""
            SELECT COUNT(*) AS c
            FROM subscriptions
            WHERE status='active'
        """).fetchone()["c"]

        total_sub = conn.execute("""
            SELECT COALESCE(SUM(amount),0) AS s
            FROM subscriptions
        """).fetchone()["s"]

        payments = conn.execute("""
            SELECT COUNT(*) AS c
            FROM payments
        """).fetchone()["c"]

        conn.close()

        await query.edit_message_text(
            "📊 <b>Statistika</b>\n\n"
            f"👥 Foydalanuvchilar: {users}\n"
            f"📅 Obunalar: {active_subs}\n"
            f"💵 Obuna tushumi: {money(total_sub)} UZS\n"
            f"💳 To‘lovlar: {payments}",
            parse_mode="HTML",
            reply_markup=admin_menu(),
        )
        return


# ============================================================
# API YORDAMCHI
# ============================================================

def normalize_url(base, endpoint):
    base = (base or "").strip().rstrip("/")
    endpoint = (endpoint or "").strip()

    if not endpoint:
        return base

    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint

    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint

    return base + endpoint


def build_headers(row):
    api_key = (row["api_key"] or "").strip()
    secret = (row["secret"] or "").strip()
    auth_type = (row["auth_type"] or "bearer").lower()

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    if api_key:
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {api_key}"
        elif auth_type == "x-api-key":
            headers["X-API-Key"] = api_key
        elif auth_type == "api-key":
            headers["API-Key"] = api_key
        elif auth_type == "authorization":
            headers["Authorization"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"

    if secret:
        headers["X-API-Secret"] = secret

    return headers


def request_json(method, url, headers, data=None):
    try:
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=data,
            timeout=20,
        )

        try:
            result = response.json()
        except Exception:
            result = {
                "text": response.text
            }

        return response.status_code, result

    except Exception as e:
        return 0, {
            "error": str(e)
        }


async def api_balance(row):
    endpoint = row["balance_endpoint"]

    if not endpoint:
        return (
            "❌ API balans endpointi sozlanmagan.\n\n"
            "API provayderingizning balance endpointini "
            "API sozlamalariga kiriting."
        )

    url = normalize_url(row["base_url"], endpoint)
    headers = build_headers(row)

    status, data = await asyncio.to_thread(
        request_json,
        "GET",
        url,
        headers,
        None,
    )

    if status == 0:
        return (
            "❌ APIga ulanishda xato.\n\n"
            f"<code>{data.get('error', 'Noma’lum xato')}</code>"
        )

    if status >= 400:
        return (
            "❌ API xato qaytardi.\n\n"
            f"HTTP: <code>{status}</code>\n"
            f"<code>{json.dumps(data, ensure_ascii=False)[:1500]}</code>"
        )

    balance = extract_value(
        data,
        [
            "balance",
            "data.balance",
            "data.balance.uzs",
        ],
    )

    currency = extract_value(
        data,
        [
            "currency",
            "data.currency",
        ],
    )

    if balance is None:
        return (
            "✅ API javob berdi, lekin balans maydoni "
            "avtomatik aniqlanmadi.\n\n"
            f"<code>{json.dumps(data, ensure_ascii=False)[:2000]}</code>"
        )

    return (
        "💰 <b>API balansi</b>\n\n"
        f"Balans: <b>{balance}</b>"
        f"{(' ' + str(currency)) if currency else ''}"
    )


def extract_value(data, paths):
    for path in paths:
        current = data

        try:
            for part in path.split("."):
                if isinstance(current, dict):
                    current = current.get(part)
                else:
                    current = None

                if current is None:
                    break

            if current is not None:
                return current

        except Exception:
            pass

    return None


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    text = (update.message.text or "").strip()
    state = context.user_data.get("state", "")

    # --------------------------------------------------------
    # BOT TOKEN
    # --------------------------------------------------------

    if state == "waiting_bot_token":
        context.user_data["state"] = ""

        token = text

        try:
            test = await context.bot.get_me()

            if not test:
                raise Exception("Telegram javob bermadi")

            # Muhim:
            # Foydalanuvchi yuborgan tokenni tekshirish
            from telegram import Bot

            temp_bot = Bot(token=token)

            bot_info = await temp_bot.get_me()

            conn = db()

            conn.execute("""
                INSERT INTO user_bots(
                    user_id,
                    bot_token,
                    bot_id,
                    bot_username,
                    bot_name,
                    welcome_text,
                    active,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """, (
                user.id,
                token,
                bot_info.id,
                bot_info.username or "",
                bot_info.first_name or "",
                "Assalomu Aleykum!",
                now_str(),
                now_str(),
            ))

            conn.commit()
            conn.close()

            await queryless_reply(
                update,
                "✅ <b>Bot muvaffaqiyatli qo‘shildi!</b>\n\n"
                f"🤖 Bot: @{bot_info.username or 'nomi yo‘q'}\n"
                f"🆔 ID: {bot_info.id}\n\n"
                "Endi ⚙️ Botni sozlash bo‘limidan foydalanishingiz mumkin."
            )

        except Exception as e:
            await queryless_reply(
                update,
                "❌ Bot token noto‘g‘ri yoki tekshirib bo‘lmadi.\n\n"
                "BotFather bergan tokenni to‘liq yuboring."
            )

        return

    # --------------------------------------------------------
    # MARKUP
    # --------------------------------------------------------

    if state == "markup":
        try:
            value = float(
                text.replace(" ", "")
                    .replace(",", ".")
            )

            if value < 0:
                raise ValueError

            set_markup(user.id, value)

            # Mavjud xizmatlarning ustamasini yangilash
            conn = db()

            conn.execute("""
                UPDATE api_services
                SET markup=?,
                    sale_price=api_price+?
                WHERE api_id IN (
                    SELECT id
                    FROM api_connections
                    WHERE user_id=?
                )
            """, (
                value,
                value,
                user.id,
            ))

            conn.commit()
            conn.close()

            context.user_data["state"] = ""

            await queryless_reply(
                update,
                "✅ Ustama saqlandi.\n\n"
                f"➕ Ustama: <b>{money(value)} UZS</b>"
            )

        except Exception:
            await queryless_reply(
                update,
                "❌ Noto‘g‘ri summa.\n\nMasalan: <code>2000</code>",
            )

        return

    # --------------------------------------------------------
    # WELCOME
    # --------------------------------------------------------

    if state == "welcome":
        set_setting("welcome_text", text)

        context.user_data["state"] = ""

        await queryless_reply(
            update,
            "✅ Welcome matn saqlandi."
        )
        return

    # --------------------------------------------------------
    # ADMIN CARD
    # --------------------------------------------------------

    if state == "admin_card":
        if user.id != ADMIN_ID:
            context.user_data["state"] = ""
            await queryless_reply(
                update,
                "❌ Sizda admin huquqi yo‘q."
            )
            return

        set_setting("payment_card", text)

        context.user_data["state"] = "admin_card_name"

        await queryless_reply(
            update,
            "✅ Karta raqami saqlandi.\n\n"
            "Endi karta egasining ismini yuboring."
        )
        return

    if state == "admin_card_name":
        if user.id != ADMIN_ID:
            context.user_data["state"] = ""
            await queryless_reply(
                update,
                "❌ Sizda admin huquqi yo‘q."
            )
            return

        set_setting("payment_name", text)

        context.user_data["state"] = ""

        await queryless_reply(
            update,
            "✅ Karta ma’lumotlari saqlandi."
        )
        return

    # --------------------------------------------------------
    # ADMIN PRICES
    # --------------------------------------------------------

    if state == "admin_prices":
        if user.id != ADMIN_ID:
            context.user_data["state"] = ""
            await queryless_reply(
                update,
                "❌ Sizda admin huquqi yo‘q."
            )
            return

        parts = text.replace(",", " ").split()

        if len(parts) != 4:
            await queryless_reply(
                update,
                "❌ 4 ta narx yuboring.\n\n"
                "Masalan:\n"
                "<code>5000 13000 25000 43000</code>"
            )
            return

        try:
            p1 = int(parts[0])
            p3 = int(parts[1])
            p7 = int(parts[2])
            pm = int(parts[3])

            if min(p1, p3, p7, pm) < 0:
                raise ValueError

            set_setting("price_1_day", p1)
            set_setting("price_3_day", p3)
            set_setting("price_7_day", p7)
            set_setting("price_1_month", pm)

            context.user_data["state"] = ""

            await queryless_reply(
                update,
                "✅ Narxlar saqlandi."
            )

        except Exception:
            await queryless_reply(
                update,
                "❌ Narxlarni raqamda yuboring."
            )

        return

    # --------------------------------------------------------
    # API WIZARD
    # --------------------------------------------------------

    if state == "api_base_url":
        if not (
            text.startswith("http://")
            or text.startswith("https://")
        ):
            await queryless_reply(
                update,
                "❌ URL http:// yoki https:// bilan boshlanishi kerak."
            )
            return

        context.user_data["api_base_url"] = text
        context.user_data["state"] = "api_key"

        await queryless_reply(
            update,
            "🔑 <b>2-qadam</b>\n\n"
            "API Key yuboring.",
            parse_mode="HTML",
        )
        return

    if state == "api_key":
        context.user_data["api_key"] = text
        context.user_data["state"] = "api_secret"

        await queryless_reply(
            update,
            "🔐 <b>3-qadam</b>\n\n"
            "Agar API Secret bo‘lsa yuboring.\n"
            "Bo‘lmasa <code>-</code> yuboring.",
            parse_mode="HTML",
        )
        return

    if state == "api_secret":
        secret = "" if text == "-" else text

        context.user_data["api_secret"] = secret
        context.user_data["state"] = "api_balance_endpoint"

        await queryless_reply(
            update,
            "💰 <b>4-qadam</b>\n\n"
            "Balance endpointini yuboring.\n\n"
            "Masalan:\n"
            "<code>/balance</code>\n\n"
            "Agar Base URLning o‘zi balance bo‘lsa:\n"
            "<code>-</code>",
            parse_mode="HTML",
        )
        return

    if state == "api_balance_endpoint":
        endpoint = "" if text == "-" else text

        context.user_data["api_balance_endpoint"] = endpoint
        context.user_data["state"] = "api_services_endpoint"

        await queryless_reply(
            update,
            "📦 <b>5-qadam</b>\n\n"
            "Services/catalog endpointini yuboring.\n\n"
            "Masalan:\n"
            "<code>/services</code>\n\n"
            "Agar hozircha kerak bo‘lmasa:\n"
            "<code>-</code>",
            parse_mode="HTML",
        )
        return

    if state == "api_services_endpoint":
        endpoint = "" if text == "-" else text

        context.user_data["api_services_endpoint"] = endpoint
        context.user_data["state"] = "api_order_endpoint"

        await queryless_reply(
            update,
            "🛒 <b>6-qadam</b>\n\n"
            "Order endpointini yuboring.\n\n"
            "Masalan:\n"
            "<code>/order</code>\n\n"
            "Kerak bo‘lmasa:\n"
            "<code>-</code>",
            parse_mode="HTML",
        )
        return

    if state == "api_order_endpoint":
        endpoint = "" if text == "-" else text

        context.user_data["api_order_endpoint"] = endpoint
        context.user_data["state"] = "api_status_endpoint"

        await queryless_reply(
            update,
            "📊 <b>7-qadam</b>\n\n"
            "Order status endpointini yuboring.\n\n"
            "Masalan:\n"
            "<code>/status</code>\n\n"
            "Kerak bo‘lmasa:\n"
            "<code>-</code>",
            parse_mode="HTML",
        )
        return

    if state == "api_status_endpoint":
        endpoint = "" if text == "-" else text

        context.user_data["api_status_endpoint"] = endpoint
        context.user_data["state"] = "api_auth_type"

        await queryless_reply(
            update,
            "🔐 <b>8-qadam</b>\n\n"
            "API Key qanday yuboriladi?\n\n"
            "Quyidagilardan birini yuboring:\n"
            "<code>bearer</code>\n"
            "<code>x-api-key</code>\n"
            "<code>api-key</code>\n"
            "<code>authorization</code>",
            parse_mode="HTML",
        )
        return

    if state == "api_auth_type":
        auth_type = text.lower()

        allowed = {
            "bearer",
            "x-api-key",
            "api-key",
            "authorization",
        }

        if auth_type not in allowed:
            await queryless_reply(
                update,
                "❌ Noto‘g‘ri.\n\n"
                "bearer / x-api-key / api-key / authorization"
            )
            return

        context.user_data["api_auth_type"] = auth_type

        await save_api_connection(update, context)

        context.user_data.clear()

        return

    # Oddiy matn
    await queryless_reply(
        update,
        "Kerakli bo‘limni tugmalardan tanlang.",
        reply_markup=main_menu(),
    )


# ============================================================
# API CONNECTION SAQLASH
# ============================================================

async def save_api_connection(update, context):
    user = update.effective_user

    base_url = context.user_data.get("api_base_url", "")
    api_key = context.user_data.get("api_key", "")
    secret = context.user_data.get("api_secret", "")
    balance_endpoint = context.user_data.get(
        "api_balance_endpoint", ""
    )
    services_endpoint = context.user_data.get(
        "api_services_endpoint", ""
    )
    order_endpoint = context.user_data.get(
        "api_order_endpoint", ""
    )
    status_endpoint = context.user_data.get(
        "api_status_endpoint", ""
    )
    auth_type = context.user_data.get(
        "api_auth_type",
        "bearer"
    )

    conn = db()

    # Old connectionlarni o‘chirib qo‘ymaymiz,
    # faqat active=0 qilamiz.
    conn.execute("""
        UPDATE api_connections
        SET active=0
        WHERE user_id=?
    """, (user.id,))

    cur = conn.execute("""
        INSERT INTO api_connections(
            user_id,
            name,
            base_url,
            api_key,
            secret,
            api_type,
            balance_endpoint,
            services_endpoint,
            order_endpoint,
            status_endpoint,
            auth_type,
            active,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, 'generic', ?, ?, ?, ?, ?, 1, ?, ?)
    """, (
        user.id,
        "My API",
        base_url,
        api_key,
        secret,
        balance_endpoint,
        services_endpoint,
        order_endpoint,
        status_endpoint,
        auth_type,
        now_str(),
        now_str(),
    ))

    api_id = cur.lastrowid

    conn.commit()
    conn.close()

    # API test
    conn = db()
    row = conn.execute(
        "SELECT * FROM api_connections WHERE id=?",
        (api_id,),
    ).fetchone()
    conn.close()

    if balance_endpoint:
        result = await api_balance(row)
    else:
        result = "⚠️ Balance endpoint berilmagan."

    await update.message.reply_text(
        "✅ <b>API muvaffaqiyatli saqlandi!</b>\n\n"
        f"🌐 URL: <code>{base_url}</code>\n"
        f"🔐 Auth: <code>{auth_type}</code>\n"
        f"💰 Balance endpoint: <code>{balance_endpoint or '-'}</code>\n"
        f"📦 Services endpoint: <code>{services_endpoint or '-'}</code>\n"
        f"🛒 Order endpoint: <code>{order_endpoint or '-'}</code>\n"
        f"📊 Status endpoint: <code>{status_endpoint or '-'}</code>\n\n"
        f"{result}",
        parse_mode="HTML",
        reply_markup=main_menu(),
    )


# ============================================================
# UNIVERSAL REPLY
# ============================================================

async def queryless_reply(
    update,
    text,
    reply_markup=None,
    parse_mode=None,
):
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )


# ============================================================
# COMMANDLAR
# ============================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)

    await update.message.reply_text(
        get_setting(
            "support_text",
            "Yordam uchun administratorga murojaat qiling."
        ),
        reply_markup=main_menu(),
    )


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)

    row = get_user(update.effective_user.id)

    await update.message.reply_text(
        "👤 Profil\n\n"
        f"🆔 ID: {update.effective_user.id}\n"
        f"💰 Balans: {money(row['balance'])} UZS\n"
        f"📅 Obuna: {row['subscription_until'] or 'Yo‘q'}",
        reply_markup=main_menu(),
    )


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)

    row = get_user(update.effective_user.id)

    await update.message.reply_text(
        f"💰 Balansingiz: {money(row['balance'])} UZS",
        reply_markup=main_menu(),
    )


async def set_commands(application):
    normal_commands = [
        BotCommand("start", "Botni ishga tushirish"),
        BotCommand("help", "Yordam"),
        BotCommand("profile", "Profil"),
        BotCommand("balance", "Balans"),
    ]

    await application.bot.set_my_commands(
        normal_commands
    )

    if ADMIN_ID:
        admin_commands = normal_commands + [
            BotCommand("admin", "Admin panel"),
        ]

        try:
            from telegram import BotCommandScopeChat

            await application.bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(
                    chat_id=ADMIN_ID
                ),
            )
        except Exception as e:
            logger.warning(
                "Admin command scope error: %s",
                e
            )


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN Render Environment Variables ichida yo‘q."
        )

    init_db()

    health_thread = Thread(
        target=start_health_server,
        daemon=True,
    )
    health_thread.start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("admin", admin_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("profile", profile_command)
    )

    application.add_handler(
        CommandHandler("balance", balance_command)
    )

    application.add_handler(
        CallbackQueryHandler(callbacks)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    async def post_init(app):
        await set_commands(app)

    application.post_init = post_init

    logger.info("Bot ishga tushmoqda...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()

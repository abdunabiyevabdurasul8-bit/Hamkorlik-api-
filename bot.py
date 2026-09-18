import os
import json
import sqlite3
import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urljoin

import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Bot,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip())
except Exception:
    ADMIN_ID = 0

PORT = int(os.getenv("PORT", "10000"))
DB_FILE = os.getenv("DB_FILE", "bot.db")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# Obuna narxlari
PLAN_PRICES = {
    "1_day": 5000,
    "3_day": 13000,
    "7_day": 25000,
    "1_month": 43000,
}

PLAN_DAYS = {
    "1_day": 1,
    "3_day": 3,
    "7_day": 7,
    "1_month": 30,
}

# =========================================================
# RENDER HEALTH SERVER
# =========================================================

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


# =========================================================
# DATABASE
# =========================================================

db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():

    with db_lock:
        conn = db()

        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance INTEGER DEFAULT 0,
            subscription_until TEXT DEFAULT '',
            subscription_started TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            photo_id TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            approved_at TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS user_bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            bot_token TEXT NOT NULL UNIQUE,
            bot_id INTEGER UNIQUE,
            bot_username TEXT DEFAULT '',
            bot_name TEXT DEFAULT '',
            welcome_text TEXT DEFAULT 'Assalomu Aleykum!',
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS api_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            base_url TEXT NOT NULL,
            api_key TEXT NOT NULL,
            api_secret TEXT DEFAULT '',
            services_endpoint TEXT DEFAULT '',
            order_endpoint TEXT DEFAULT '',
            balance_endpoint TEXT DEFAULT '',
            status_endpoint TEXT DEFAULT '',
            auth_type TEXT DEFAULT 'bearer',
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS api_services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            external_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            api_price INTEGER DEFAULT 0,
            markup INTEGER DEFAULT 0,
            sale_price INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, external_id)
        );

        CREATE TABLE IF NOT EXISTS api_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            external_order_id TEXT DEFAULT '',
            target TEXT DEFAULT '',
            quantity INTEGER DEFAULT 0,
            api_price INTEGER DEFAULT 0,
            markup INTEGER DEFAULT 0,
            sale_price INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bot_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            bot_id INTEGER NOT NULL,
            customer_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            target TEXT DEFAULT '',
            quantity INTEGER DEFAULT 0,
            amount INTEGER DEFAULT 0,
            api_order_id TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
        """)

        conn.commit()
        conn.close()

    # default settings
    set_setting_if_missing(
        "payment_card",
        ""
    )

    set_setting_if_missing(
        "payment_name",
        ""
    )


def now_str():
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value):
    if not value:
        return None

    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def set_setting_if_missing(key, value):

    with db_lock:
        conn = db()

        row = conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?)",
                (key, value)
            )
            conn.commit()

        conn.close()


def get_setting(key, default=""):

    with db_lock:
        conn = db()

        row = conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ).fetchone()

        conn.close()

    if row is None:
        return default

    return row["value"]


def set_setting(key, value):

    with db_lock:
        conn = db()

        conn.execute("""
            INSERT INTO settings(key,value)
            VALUES(?,?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
        """, (key, str(value)))

        conn.commit()
        conn.close()


# =========================================================
# USERS
# =========================================================

def ensure_user(tg_user):

    if tg_user is None:
        return

    with db_lock:
        conn = db()

        row = conn.execute(
            "SELECT user_id FROM users WHERE user_id=?",
            (tg_user.id,)
        ).fetchone()

        if row is None:
            conn.execute("""
                INSERT INTO users(
                    user_id,
                    username,
                    first_name,
                    balance,
                    subscription_until,
                    subscription_started,
                    created_at
                )
                VALUES(?,?,?,?,?,?,?)
            """, (
                tg_user.id,
                tg_user.username or "",
                tg_user.first_name or "",
                0,
                "",
                "",
                now_str()
            ))
        else:
            conn.execute("""
                UPDATE users
                SET username=?, first_name=?
                WHERE user_id=?
            """, (
                tg_user.username or "",
                tg_user.first_name or "",
                tg_user.id
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


def change_balance(user_id, amount):

    with db_lock:
        conn = db()

        conn.execute("""
            UPDATE users
            SET balance = balance + ?
            WHERE user_id=?
        """, (
            amount,
            user_id
        ))

        conn.commit()
        conn.close()


def get_balance(user_id):

    row = get_user(user_id)

    if not row:
        return 0

    return int(row["balance"] or 0)


# =========================================================
# SUBSCRIPTION
# =========================================================

def subscription_active(user_id):

    row = get_user(user_id)

    if not row:
        return False

    until = parse_dt(row["subscription_until"])

    if not until:
        return False

    return until > datetime.now(timezone.utc)


def get_subscription_until(user_id):

    row = get_user(user_id)

    if not row:
        return None

    return parse_dt(row["subscription_until"])


def buy_subscription(user_id, plan):

    if plan not in PLAN_PRICES:
        return False, "Noto‘g‘ri tarif."

    price = PLAN_PRICES[plan]
    days = PLAN_DAYS[plan]

    balance = get_balance(user_id)

    if balance < price:
        return False, "BALANS_YETARLI_EMAS"

    current = get_subscription_until(user_id)
    now = datetime.now(timezone.utc)

    if current and current > now:
        start = current
    else:
        start = now

    if plan == "1_month":
        expires = start + timedelta(days=30)
    else:
        expires = start + timedelta(days=days)

    with db_lock:
        conn = db()

        conn.execute("""
            UPDATE users
            SET balance=balance-?,
                subscription_started=?,
                subscription_until=?
            WHERE user_id=?
        """, (
            price,
            now.isoformat(),
            expires.isoformat(),
            user_id
        ))

        conn.commit()
        conn.close()

    return True, expires


def format_subscription(user_id):

    row = get_user(user_id)

    if not row:
        return "❌ Obuna topilmadi."

    until = parse_dt(row["subscription_until"])
    started = parse_dt(row["subscription_started"])

    if not until or until <= datetime.now(timezone.utc):
        return (
            "🔴 <b>Obuna:</b> Tugagan\n"
            "❌ Obuna muddati tugagan."
        )

    start_text = started.astimezone().strftime(
        "%d.%m.%Y %H:%M"
    ) if started else "-"

    until_text = until.astimezone().strftime(
        "%d.%m.%Y %H:%M"
    )

    remaining = until - datetime.now(timezone.utc)

    days = remaining.days
    hours = remaining.seconds // 3600
    minutes = (remaining.seconds % 3600) // 60

    return (
        "🟢 <b>Obuna:</b> Faol\n"
        f"📅 <b>Boshlangan:</b> {start_text}\n"
        f"⏳ <b>Tugaydi:</b> {until_text}\n"
        f"⌛ <b>Qolgan:</b> {days} kun, {hours} soat, {minutes} daqiqa"
    )


async def subscription_checker(app):

    already_notified = set()

    while True:

        try:

            with db_lock:
                conn = db()

                rows = conn.execute("""
                    SELECT user_id, subscription_until
                    FROM users
                    WHERE subscription_until != ''
                """).fetchall()

                conn.close()

            now = datetime.now(timezone.utc)

            for row in rows:

                uid = int(row["user_id"])
                until = parse_dt(row["subscription_until"])

                if not until:
                    continue

                if until <= now:

                    key = f"{uid}:{row['subscription_until']}"

                    if key in already_notified:
                        continue

                    already_notified.add(key)

                    try:
                        await app.bot.send_message(
                            chat_id=uid,
                            text=(
                                "❌ <b>Obuna muddati tugadi.</b>\n\n"
                                "Botdan foydalanishni davom ettirish "
                                "uchun yangi obuna sotib oling."
                            ),
                            parse_mode=ParseMode.HTML
                        )
                    except Exception as e:
                        logger.warning(
                            "Subscription notification error: %s",
                            e
                        )

        except Exception as e:
            logger.error(
                "Subscription checker error: %s",
                e
            )

        await asyncio.sleep(30)


# =========================================================
# MAIN BOT KEYBOARDS
# =========================================================

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
                "⚙️ Botni sozlash",
                callback_data="bot_settings"
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


def back_menu():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ Orqaga",
                callback_data="home"
            )
        ]
    ])


def settings_menu():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔌 API ulash",
                callback_data="api_connect"
            )
        ],
        [
            InlineKeyboardButton(
                "📦 Xizmatlar",
                callback_data="api_services"
            )
        ],
        [
            InlineKeyboardButton(
                "➕ Ustama UZS",
                callback_data="api_markup"
            )
        ],
        [
            InlineKeyboardButton(
                "✏️ Welcome matn",
                callback_data="welcome"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Orqaga",
                callback_data="home"
            )
        ]
    ])


def subscription_menu():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "1 kun — 5 000 so‘m",
                callback_data="sub_1_day"
            )
        ],
        [
            InlineKeyboardButton(
                "3 kun — 13 000 so‘m",
                callback_data="sub_3_day"
            )
        ],
        [
            InlineKeyboardButton(
                "7 kun — 25 000 so‘m",
                callback_data="sub_7_day"
            )
        ],
        [
            InlineKeyboardButton(
                "1 oy — 43 000 so‘m",
                callback_data="sub_1_month"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Orqaga",
                callback_data="home"
            )
        ]
    ])


# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user:
        return

    ensure_user(update.effective_user)

    context.user_data.clear()

    await update.message.reply_text(
        "Assalomu Aleykum! 👋\n\n"
        "Kerakli bo‘limni tanlang:",
        reply_markup=main_menu()
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user:
        return

    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(
            "❌ Sizda admin huquqi yo‘q."
        )
        return

    await update.message.reply_text(
        "👑 <b>Admin panel</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "💳 Karta",
                    callback_data="admin_card"
                )
            ],
            [
                InlineKeyboardButton(
                    "💰 Balans qo‘shish",
                    callback_data="admin_balance"
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
                    "👥 Foydalanuvchilar",
                    callback_data="admin_users"
                )
            ],
            [
                InlineKeyboardButton(
                    "📊 Statistika",
                    callback_data="admin_stats"
                )
            ]
        ])
    )


# =========================================================
# API HELPERS
# =========================================================

def get_api(user_id):

    with db_lock:
        conn = db()

        row = conn.execute("""
            SELECT *
            FROM api_connections
            WHERE user_id=? AND active=1
        """, (user_id,)).fetchone()

        conn.close()

    return row


def api_headers(api):

    auth = (api["auth_type"] or "bearer").lower()

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    key = api["api_key"]

    if auth == "bearer":
        headers["Authorization"] = f"Bearer {key}"

    elif auth == "x-api-key":
        headers["X-API-Key"] = key

    elif auth == "api-key":
        headers["api-key"] = key

    elif auth == "authorization":
        headers["Authorization"] = key

    else:
        headers["Authorization"] = f"Bearer {key}"

    if api["api_secret"]:
        headers["X-API-Secret"] = api["api_secret"]

    return headers


def make_api_url(base_url, endpoint):

    if not endpoint:
        return base_url

    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint

    return urljoin(
        base_url.rstrip("/") + "/",
        endpoint.lstrip("/")
    )


def api_request_sync(
    api,
    endpoint,
    method="GET",
    payload=None
):

    url = make_api_url(
        api["base_url"],
        endpoint
    )

    headers = api_headers(api)

    response = requests.request(
        method=method.upper(),
        url=url,
        headers=headers,
        json=payload,
        timeout=30
    )

    response.raise_for_status()

    if not response.text.strip():
        return {}

    try:
        return response.json()
    except Exception:
        return {
            "raw": response.text
        }


async def api_request(
    api,
    endpoint,
    method="GET",
    payload=None
):

    return await asyncio.to_thread(
        api_request_sync,
        api,
        endpoint,
        method,
        payload
    )


# =========================================================
# API PARSING
# =========================================================

def find_list(data):

    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    for key in (
        "services",
        "data",
        "items",
        "results"
    ):
        value = data.get(key)

        if isinstance(value, list):
            return value

    return []


def find_value(item, keys, default=""):

    if not isinstance(item, dict):
        return default

    for key in keys:

        if key in item:
            value = item[key]

            if value is not None:
                return value

    return default


def money_to_int(value):

    try:

        if isinstance(value, int):
            return value

        if isinstance(value, float):
            return int(round(value))

        text = str(value).strip()

        text = text.replace(" ", "")
        text = text.replace(",", ".")

        number = Decimal(text)

        return int(number)

    except (InvalidOperation, ValueError, TypeError):
        return 0


# =========================================================
# API WIZARD
# =========================================================

async def api_connect_start(query, context):

    context.user_data["api_step"] = "base_url"

    await query.edit_message_text(
        "🔌 <b>API ulash</b>\n\n"
        "1️⃣ API Base URL yuboring.\n\n"
        "Masalan:\n"
        "<code>https://example.com/api</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu()
    )


async def handle_api_wizard(update, context):

    if not update.message:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()

    step = context.user_data.get("api_step")

    if not step:
        return False

    if step == "base_url":

        context.user_data["api_base_url"] = text

        context.user_data["api_step"] = "api_key"

        await update.message.reply_text(
            "2️⃣ API Key yuboring:",
            reply_markup=back_menu()
        )

        return True

    if step == "api_key":

        context.user_data["api_key"] = text

        context.user_data["api_step"] = "api_secret"

        await update.message.reply_text(
            "3️⃣ API Secret yuboring.\n\n"
            "Agar kerak bo‘lmasa <code>-</code> yuboring.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu()
        )

        return True

    if step == "api_secret":

        context.user_data["api_secret"] = (
            "" if text == "-" else text
        )

        context.user_data["api_step"] = "services_endpoint"

        await update.message.reply_text(
            "4️⃣ Services endpoint yuboring.\n\n"
            "Masalan:\n"
            "<code>/services</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu()
        )

        return True

    if step == "services_endpoint":

        context.user_data["services_endpoint"] = text

        context.user_data["api_step"] = "order_endpoint"

        await update.message.reply_text(
            "5️⃣ Order endpoint yuboring.\n\n"
            "Masalan:\n"
            "<code>/order</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu()
        )

        return True

    if step == "order_endpoint":

        context.user_data["order_endpoint"] = text

        context.user_data["api_step"] = "balance_endpoint"

        await update.message.reply_text(
            "6️⃣ Balance endpoint yuboring.\n\n"
            "Kerak bo‘lmasa <code>-</code> yuboring.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu()
        )

        return True

    if step == "balance_endpoint":

        context.user_data["balance_endpoint"] = (
            "" if text == "-" else text
        )

        context.user_data["api_step"] = "status_endpoint"

        await update.message.reply_text(
            "7️⃣ Status endpoint yuboring.\n\n"
            "Kerak bo‘lmasa <code>-</code> yuboring.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu()
        )

        return True

    if step == "status_endpoint":

        context.user_data["status_endpoint"] = (
            "" if text == "-" else text
        )

        context.user_data["api_step"] = "auth_type"

        await update.message.reply_text(
            "8️⃣ API autentifikatsiya turini tanlang:",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "Bearer",
                        callback_data="auth_bearer"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "X-API-Key",
                        callback_data="auth_x-api-key"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "api-key",
                        callback_data="auth_api-key"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Authorization",
                        callback_data="auth_authorization"
                    )
                ]
            ])
        )

        return True

    return False


async def save_api(update, context, auth_type):

    user_id = update.effective_user.id

    base_url = context.user_data.get(
        "api_base_url",
        ""
    ).strip()

    api_key = context.user_data.get(
        "api_key",
        ""
    ).strip()

    api_secret = context.user_data.get(
        "api_secret",
        ""
    ).strip()

    services_endpoint = context.user_data.get(
        "services_endpoint",
        ""
    ).strip()

    order_endpoint = context.user_data.get(
        "order_endpoint",
        ""
    ).strip()

    balance_endpoint = context.user_data.get(
        "balance_endpoint",
        ""
    ).strip()

    status_endpoint = context.user_data.get(
        "status_endpoint",
        ""
    ).strip()

    if not base_url or not api_key:
        await update.callback_query.edit_message_text(
            "❌ API Base URL yoki API Key bo‘sh."
        )
        return

    with db_lock:
        conn = db()

        conn.execute("""
            INSERT INTO api_connections(
                user_id,
                base_url,
                api_key,
                api_secret,
                services_endpoint,
                order_endpoint,
                balance_endpoint,
                status_endpoint,
                auth_type,
                active,
                created_at,
                updated_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                base_url=excluded.base_url,
                api_key=excluded.api_key,
                api_secret=excluded.api_secret,
                services_endpoint=excluded.services_endpoint,
                order_endpoint=excluded.order_endpoint,
                balance_endpoint=excluded.balance_endpoint,
                status_endpoint=excluded.status_endpoint,
                auth_type=excluded.auth_type,
                active=1,
                updated_at=excluded.updated_at
        """, (
            user_id,
            base_url,
            api_key,
            api_secret,
            services_endpoint,
            order_endpoint,
            balance_endpoint,
            status_endpoint,
            auth_type,
            1,
            now_str(),
            now_str()
        ))

        conn.commit()
        conn.close()

    context.user_data.pop("api_step", None)

    await update.callback_query.edit_message_text(
        "✅ <b>API muvaffaqiyatli saqlandi.</b>\n\n"
        "Endi API orqali xizmatlarni yangilashingiz mumkin.",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_menu()
    )


# =========================================================
# SYNC SERVICES
# =========================================================

async def sync_services_for_user(user_id):

    api = get_api(user_id)

    if not api:
        return False, "❌ Avval API ulang."

    if not api["services_endpoint"]:
        return False, "❌ Services endpoint kiritilmagan."

    try:

        data = await api_request(
            api,
            api["services_endpoint"],
            "GET"
        )

        services = find_list(data)

        if not services:
            return False, (
                "❌ API xizmatlar ro‘yxatini qaytarmadi.\n\n"
                "API response formatini tekshiring."
            )

        markup = get_user_markup(user_id)

        count = 0

        with db_lock:
            conn = db()

            for item in services:

                external_id = str(
                    find_value(
                        item,
                        [
                            "id",
                            "service",
                            "service_id"
                        ],
                        ""
                    )
                )

                name = str(
                    find_value(
                        item,
                        [
                            "name",
                            "service_name",
                            "title"
                        ],
                        f"Xizmat {external_id}"
                    )
                )

                description = str(
                    find_value(
                        item,
                        [
                            "description",
                            "desc"
                        ],
                        ""
                    )
                )

                api_price = money_to_int(
                    find_value(
                        item,
                        [
                            "price",
                            "cost",
                            "api_price"
                        ],
                        0
                    )
                )

                if not external_id:
                    continue

                sale_price = api_price + markup

                conn.execute("""
                    INSERT INTO api_services(
                        user_id,
                        external_id,
                        name,
                        description,
                        api_price,
                        markup,
                        sale_price,
                        active,
                        created_at,
                        updated_at
                    )
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(user_id,external_id)
                    DO UPDATE SET
                        name=excluded.name,
                        description=excluded.description,
                        api_price=excluded.api_price,
                        markup=excluded.markup,
                        sale_price=excluded.sale_price,
                        active=1,
                        updated_at=excluded.updated_at
                """, (
                    user_id,
                    external_id,
                    name,
                    description,
                    api_price,
                    markup,
                    sale_price,
                    1,
                    now_str(),
                    now_str()
                ))

                count += 1

            conn.commit()
            conn.close()

        return True, f"✅ {count} ta xizmat yangilandi."

    except Exception as e:

        logger.exception("API services error")

        return False, (
            "❌ API bilan bog‘lanishda xato:\n"
            f"<code>{str(e)[:1000]}</code>"
        )


def get_user_markup(user_id):

    value = get_setting(
        f"markup:{user_id}",
        "0"
    )

    try:
        return int(value)
    except Exception:
        return 0


def set_user_markup(user_id, amount):

    set_setting(
        f"markup:{user_id}",
        str(amount)
    )


# =========================================================
# SERVICES
# =========================================================

def get_services(user_id):

    with db_lock:
        conn = db()

        rows = conn.execute("""
            SELECT *
            FROM api_services
            WHERE user_id=? AND active=1
            ORDER BY id ASC
        """, (user_id,)).fetchall()

        conn.close()

    return rows


def get_service(service_id):

    with db_lock:
        conn = db()

        row = conn.execute("""
            SELECT *
            FROM api_services
            WHERE id=?
        """, (service_id,)).fetchone()

        conn.close()

    return row


# =========================================================
# API ORDER
# =========================================================

async def send_api_order(
    user_id,
    service,
    target,
    quantity
):

    api = get_api(user_id)

    if not api:
        return False, None, "API ulanmagan."

    if not api["order_endpoint"]:
        return False, None, "Order endpoint kiritilmagan."

    payload = {
        "service": service["external_id"],
        "service_id": service["external_id"],
        "id": service["external_id"],
        "target": target,
        "link": target,
        "quantity": quantity
    }

    try:

        data = await api_request(
            api,
            api["order_endpoint"],
            "POST",
            payload
        )

        if isinstance(data, dict):

            external_id = find_value(
                data,
                [
                    "order",
                    "order_id",
                    "id"
                ],
                ""
            )

            if isinstance(data.get("data"), dict):

                external_id = external_id or str(
                    find_value(
                        data["data"],
                        [
                            "order",
                            "order_id",
                            "id"
                        ],
                        ""
                    )
                )

            if data.get("error"):
                return False, None, str(data["error"])

            if external_id:
                return True, str(external_id), data

        return True, "", data

    except Exception as e:

        return False, None, str(e)


# =========================================================
# CREATED BOT
# =========================================================

def get_user_bots(user_id):

    with db_lock:
        conn = db()

        rows = conn.execute("""
            SELECT *
            FROM user_bots
            WHERE user_id=? AND active=1
            ORDER BY id ASC
        """, (user_id,)).fetchall()

        conn.close()

    return rows


def bot_token_exists(token):

    with db_lock:
        conn = db()

        row = conn.execute("""
            SELECT id
            FROM user_bots
            WHERE bot_token=?
        """, (token,)).fetchone()

        conn.close()

    return row is not None


async def validate_bot_token(token):

    try:

        bot = Bot(token=token)

        me = await bot.get_me()

        return me

    except Exception as e:

        logger.warning(
            "Bot token validation failed: %s",
            e
        )

        return None


async def add_user_bot(user_id, token):

    token = token.strip()

    if bot_token_exists(token):
        return False, "DUPLICATE", None

    me = await validate_bot_token(token)

    if not me:
        return False, "INVALID", None

    with db_lock:
        conn = db()

        try:

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
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (
                user_id,
                token,
                me.id,
                me.username or "",
                me.first_name or "",
                "Assalomu Aleykum!",
                1,
                now_str(),
                now_str()
            ))

            conn.commit()

        except sqlite3.IntegrityError:

            conn.close()

            return False, "DUPLICATE", None

        conn.close()

    return True, "OK", me


# =========================================================
# CREATED BOT POLLING
# =========================================================

running_created_bots = {}


def get_bot_record_by_token(token):

    with db_lock:
        conn = db()

        row = conn.execute("""
            SELECT *
            FROM user_bots
            WHERE bot_token=? AND active=1
        """, (token,)).fetchone()

        conn.close()

    return row


async def created_bot_send_menu(bot, chat_id, owner_id):

    await bot.send_message(
        chat_id=chat_id,
        text="Kerakli bo‘limni tanlang:",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📦 Xizmatlar",
                    callback_data="cb_services"
                )
            ],
            [
                InlineKeyboardButton(
                    "💰 Balans",
                    callback_data="cb_balance"
                )
            ],
            [
                InlineKeyboardButton(
                    "ℹ️ Yordam",
                    callback_data="cb_help"
                )
            ]
        ])
    )


async def created_bot_process_update(
    bot,
    owner_id,
    update
):

    try:

        if update.message:

            message = update.message

            if message.text:

                text = message.text.strip()

                if text.startswith("/start"):

                    await bot.send_message(
                        chat_id=message.chat_id,
                        text=(
                            "Assalomu Aleykum! 👋\n\n"
                            "Xizmatlardan foydalanish uchun "
                            "bo‘limni tanlang."
                        ),
                        reply_markup=InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton(
                                    "📦 Xizmatlar",
                                    callback_data="cb_services"
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    "ℹ️ Yordam",
                                    callback_data="cb_help"
                                )
                            ]
                        ])
                    )

                    return

                if text.isdigit():

                    service_id = int(
                        text
                    )

                    service = get_service(
                        service_id
                    )

                    if service and service["user_id"] == owner_id:

                        await bot.send_message(
                            chat_id=message.chat_id,
                            text=(
                                f"📦 <b>{service['name']}</b>\n\n"
                                f"💰 Narx: "
                                f"{service['sale_price']:,} so‘m\n\n"
                                "Buyurtma berish uchun:\n"
                                "<code>/buy TARGET MIQDOR</code>"
                            ),
                            parse_mode=ParseMode.HTML
                        )

                    return

                if text.startswith("/buy"):

                    parts = text.split(maxsplit=2)

                    if len(parts) < 3:

                        await bot.send_message(
                            chat_id=message.chat_id,
                            text=(
                                "❌ Format:\n"
                                "/buy TARGET MIQDOR"
                            )
                        )

                        return

                    target = parts[1]

                    try:
                        quantity = int(parts[2])
                    except Exception:

                        await bot.send_message(
                            chat_id=message.chat_id,
                            text="❌ Miqdor noto‘g‘ri."
                        )

                        return

                    services = get_services(owner_id)

                    if not services:

                        await bot.send_message(
                            chat_id=message.chat_id,
                            text="❌ Hozircha xizmatlar mavjud emas."
                        )

                        return

                    # Oddiy holatda birinchi aktiv xizmat
                    service = services[0]

                    amount = int(service["sale_price"])

                    ok, external_id, result = await send_api_order(
                        owner_id,
                        service,
                        target,
                        quantity
                    )

                    if not ok:

                        await bot.send_message(
                            chat_id=message.chat_id,
                            text=(
                                "❌ Buyurtma yuborilmadi.\n\n"
                                f"{result}"
                            )
                        )

                        return

                    with db_lock:
                        conn = db()

                        conn.execute("""
                            INSERT INTO bot_orders(
                                owner_id,
                                bot_id,
                                customer_id,
                                service_id,
                                target,
                                quantity,
                                amount,
                                api_order_id,
                                status,
                                created_at
                            )
                            SELECT
                                ?,
                                id,
                                ?,
                                ?,
                                ?,
                                ?,
                                ?,
                                ?,
                                'sent',
                                ?
                            FROM user_bots
                            WHERE user_id=? AND active=1
                            LIMIT 1
                        """, (
                            owner_id,
                            message.from_user.id,
                            service["id"],
                            target,
                            quantity,
                            amount,
                            str(external_id or ""),
                            now_str(),
                            owner_id
                        ))

                        conn.commit()
                        conn.close()

                    await bot.send_message(
                        chat_id=message.chat_id,
                        text=(
                            "✅ <b>Buyurtma API'ga yuborildi.</b>\n\n"
                            f"🆔 API ID: <code>{external_id}</code>"
                        ),
                        parse_mode=ParseMode.HTML
                    )

                    return

        if update.callback_query:

            q = update.callback_query

            await q.answer()

            if q.data == "cb_help":

                await bot.send_message(
                    chat_id=q.message.chat_id,
                    text=(
                        "ℹ️ Yordam\n\n"
                        "Xizmatlar bo‘limidan kerakli xizmatni "
                        "tanlang."
                    )
                )

                return

            if q.data == "cb_balance":

                await bot.send_message(
                    chat_id=q.message.chat_id,
                    text=(
                        "💰 Bu botdagi mijoz balansi "
                        "alohida tizim orqali boshqariladi."
                    )
                )

                return

            if q.data == "cb_services":

                services = get_services(owner_id)

                if not services:

                    await bot.send_message(
                        chat_id=q.message.chat_id,
                        text="❌ Xizmatlar topilmadi."
                    )

                    return

                buttons = []

                for service in services[:50]:

                    buttons.append([
                        InlineKeyboardButton(
                            (
                                f"{service['name'][:35]} — "
                                f"{service['sale_price']:,} so‘m"
                            ),
                            callback_data=(
                                f"cb_service_{service['id']}"
                            )
                        )
                    ])

                await bot.send_message(
                    chat_id=q.message.chat_id,
                    text="📦 <b>Xizmatlar</b>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(buttons)
                )

                return

            if q.data.startswith("cb_service_"):

                try:
                    service_id = int(
                        q.data.split("_")[-1]
                    )
                except Exception:
                    return

                service = get_service(
                    service_id
                )

                if not service:
                    return

                if service["user_id"] != owner_id:
                    return

                await bot.send_message(
                    chat_id=q.message.chat_id,
                    text=(
                        f"📦 <b>{service['name']}</b>\n\n"
                        f"💰 Narx: "
                        f"{service['sale_price']:,} so‘m\n\n"
                        "Buyurtma:\n"
                        "<code>/buy TARGET MIQDOR</code>"
                    ),
                    parse_mode=ParseMode.HTML
                )

    except Exception as e:

        logger.exception(
            "Created bot update error: %s",
            e
        )


async def created_bot_poll_loop(token):

    if token in running_created_bots:
        return

    running_created_bots[token] = True

    try:

        row = get_bot_record_by_token(token)

        if not row:
            return

        owner_id = int(row["user_id"])

        bot = Bot(token=token)

        offset = 0

        logger.info(
            "Started created bot @%s",
            row["bot_username"]
        )

        while True:

            try:

                updates = await bot.get_updates(
                    offset=offset,
                    timeout=30,
                    allowed_updates=[
                        "message",
                        "callback_query"
                    ]
                )

                for update in updates:

                    offset = update.update_id + 1

                    await created_bot_process_update(
                        bot,
                        owner_id,
                        update
                    )

            except Exception as e:

                logger.error(
                    "Created bot polling error @%s: %s",
                    row["bot_username"],
                    e
                )

                await asyncio.sleep(5)

    finally:

        running_created_bots.pop(
            token,
            None
        )


async def start_all_created_bots():

    rows = []

    with db_lock:
        conn = db()

        rows = conn.execute("""
            SELECT bot_token
            FROM user_bots
            WHERE active=1
        """).fetchall()

        conn.close()

    for row in rows:

        token = row["bot_token"]

        if token not in running_created_bots:

            asyncio.create_task(
                created_bot_poll_loop(token)
            )


# =========================================================
# PAYMENT
# =========================================================

async def payment_start(query, context):

    card = get_setting(
        "payment_card",
        ""
    )

    name = get_setting(
        "payment_name",
        ""
    )

    if not card:

        await query.edit_message_text(
            "❌ Hozircha karta raqami sozlanmagan.",
            reply_markup=back_menu()
        )

        return

    context.user_data["payment_step"] = "amount"

    await query.edit_message_text(
        "💳 <b>Qo‘lda to‘lov</b>\n\n"
        f"💳 Karta: <code>{card}</code>\n"
        f"👤 Karta egasi: {name or '-'}\n\n"
        "To‘lamoqchi bo‘lgan summangizni yuboring.\n"
        "Masalan: <code>43000</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu()
    )


async def handle_payment_amount(update, context):

    step = context.user_data.get(
        "payment_step"
    )

    if step != "amount":
        return False

    text = update.message.text.strip()

    try:
        amount = int(
            text.replace(" ", "").replace(",", "")
        )
    except Exception:

        await update.message.reply_text(
            "❌ Summani faqat raqam bilan yuboring."
        )

        return True

    if amount <= 0:

        await update.message.reply_text(
            "❌ Summa noto‘g‘ri."
        )

        return True

    context.user_data["payment_amount"] = amount
    context.user_data["payment_step"] = "photo"

    await update.message.reply_text(
        "📸 Endi to‘lov chekini <b>rasm</b> qilib yuboring.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_menu()
    )

    return True


async def handle_payment_photo(update, context):

    step = context.user_data.get(
        "payment_step"
    )

    if step != "photo":
        return False

    if not update.message.photo:

        await update.message.reply_text(
            "❌ Chek rasmini yuboring."
        )

        return True

    amount = int(
        context.user_data.get(
            "payment_amount",
            0
        )
    )

    photo_id = update.message.photo[-1].file_id

    with db_lock:
        conn = db()

        cur = conn.execute("""
            INSERT INTO payments(
                user_id,
                amount,
                photo_id,
                status,
                created_at
            )
            VALUES(?,?,?,?,?)
        """, (
            update.effective_user.id,
            amount,
            photo_id,
            "pending",
            now_str()
        ))

        payment_id = cur.lastrowid

        conn.commit()
        conn.close()

    context.user_data.pop(
        "payment_step",
        None
    )

    await update.message.reply_text(
        "✅ Chek qabul qilindi.\n\n"
        "Admin tasdiqlashini kuting."
    )

    if ADMIN_ID:

        try:

            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo_id,
                caption=(
                    "💳 <b>Yangi to‘lov</b>\n\n"
                    f"🆔 To‘lov: {payment_id}\n"
                    f"👤 User: {update.effective_user.id}\n"
                    f"💰 Summa: {amount:,} so‘m"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "✅ Tasdiqlash",
                            callback_data=f"pay_ok_{payment_id}"
                        ),
                        InlineKeyboardButton(
                            "❌ Rad etish",
                            callback_data=f"pay_no_{payment_id}"
                        )
                    ]
                ])
            )

        except Exception as e:

            logger.error(
                "Admin payment notification error: %s",
                e
            )

    return True


# =========================================================
# ADMIN PAYMENT ACTIONS
# =========================================================

async def approve_payment(query, payment_id):

    if query.from_user.id != ADMIN_ID:
        await query.answer(
            "Ruxsat yo‘q.",
            show_alert=True
        )
        return

    with db_lock:
        conn = db()

        payment = conn.execute("""
            SELECT *
            FROM payments
            WHERE id=?
        """, (payment_id,)).fetchone()

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
                "Bu to‘lov allaqachon ko‘rilgan.",
                show_alert=True
            )

            return

        conn.execute("""
            UPDATE payments
            SET status='approved',
                approved_at=?
            WHERE id=?
        """, (
            now_str(),
            payment_id
        ))

        conn.execute("""
            UPDATE users
            SET balance=balance+?
            WHERE user_id=?
        """, (
            payment["amount"],
            payment["user_id"]
        ))

        conn.commit()
        conn.close()

    await query.edit_message_caption(
        caption=(
            "✅ <b>To‘lov tasdiqlandi.</b>\n\n"
            f"🆔 {payment_id}\n"
            f"👤 {payment['user_id']}\n"
            f"💰 {payment['amount']:,} so‘m"
        ),
        parse_mode=ParseMode.HTML
    )

    try:

        await query.get_bot().send_message(
            chat_id=payment["user_id"],
            text=(
                "✅ <b>To‘lovingiz tasdiqlandi.</b>\n\n"
                f"💰 Balansingizga "
                f"{payment['amount']:,} so‘m qo‘shildi."
            ),
            parse_mode=ParseMode.HTML
        )

    except Exception:
        pass


async def reject_payment(query, payment_id):

    if query.from_user.id != ADMIN_ID:
        await query.answer(
            "Ruxsat yo‘q.",
            show_alert=True
        )
        return

    with db_lock:
        conn = db()

        payment = conn.execute("""
            SELECT *
            FROM payments
            WHERE id=?
        """, (payment_id,)).fetchone()

        if not payment:
            conn.close()

            await query.answer(
                "To‘lov topilmadi.",
                show_alert=True
            )

            return

        conn.execute("""
            UPDATE payments
            SET status='rejected'
            WHERE id=?
        """, (payment_id,))

        conn.commit()
        conn.close()

    await query.edit_message_caption(
        caption=(
            "❌ <b>To‘lov rad etildi.</b>\n\n"
            f"🆔 {payment_id}"
        ),
        parse_mode=ParseMode.HTML
    )

    try:

        await query.get_bot().send_message(
            chat_id=payment["user_id"],
            text=(
                "❌ <b>To‘lovingiz rad etildi.</b>\n\n"
                "Chek yoki to‘lov ma'lumotlarini tekshiring."
            ),
            parse_mode=ParseMode.HTML
        )

    except Exception:
        pass


# =========================================================
# CALLBACKS
# =========================================================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    user_id = query.from_user.id
    data = query.data

    ensure_user(query.from_user)

    # ---------------- HOME ----------------

    if data == "home":

        context.user_data.clear()

        await query.edit_message_text(
            "Assalomu Aleykum! 👋\n\n"
            "Kerakli bo‘limni tanlang:",
            reply_markup=main_menu()
        )

        return

    # ---------------- CREATE BOT ----------------

    if data == "create_bot":

        context.user_data["bot_token_step"] = True

        await query.edit_message_text(
            "🤖 <b>Bot yaratish</b>\n\n"
            "1️⃣ @BotFather orqali bot yarating.\n"
            "2️⃣ BotFather bergan TOKENni oling.\n"
            "3️⃣ TOKENni shu yerga yuboring.\n\n"
            "⚠️ Tokenni boshqa odamga bermang.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu()
        )

        return

    # ---------------- SETTINGS ----------------

    if data == "bot_settings":

        await query.edit_message_text(
            "⚙️ <b>Botni sozlash</b>\n\n"
            "Bu yerda faqat API ulash va "
            "bot sozlamalari mavjud.",
            parse_mode=ParseMode.HTML,
            reply_markup=settings_menu()
        )

        return

    # ---------------- API CONNECT ----------------

    if data == "api_connect":

        await api_connect_start(
            query,
            context
        )

        return

    # ---------------- AUTH ----------------

    if data.startswith("auth_"):

        auth_type = data.replace(
            "auth_",
            "",
            1
        )

        await save_api(
            update,
            context,
            auth_type
        )

        return

    # ---------------- SERVICES ----------------

    if data == "api_services":

        await query.edit_message_text(
            "⏳ API xizmatlari yangilanmoqda..."
        )

        ok, message = await sync_services_for_user(
            user_id
        )

        await query.edit_message_text(
            message,
            parse_mode=ParseMode.HTML,
            reply_markup=settings_menu()
        )

        return

    # ---------------- MARKUP ----------------

    if data == "api_markup":

        current = get_user_markup(
            user_id
        )

        context.user_data["markup_step"] = True

        await query.edit_message_text(
            "➕ <b>Ustama UZS</b>\n\n"
            f"Hozirgi ustama: <b>{current:,} so‘m</b>\n\n"
            "Yangi ustama summasini yuboring.\n"
            "Masalan: <code>2000</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu()
        )

        return

    # ---------------- WELCOME ----------------

    if data == "welcome":

        context.user_data["welcome_step"] = True

        await query.edit_message_text(
            "✏️ Yangi Welcome matnni yuboring:",
            reply_markup=back_menu()
        )

        return

    # ---------------- SUBSCRIPTION ----------------

    if data == "subscription":

        await query.edit_message_text(
            "💳 <b>Obuna sotib olish</b>\n\n"
            "Kerakli muddatni tanlang:",
            parse_mode=ParseMode.HTML,
            reply_markup=subscription_menu()
        )

        return

    if data.startswith("sub_"):

        plan = data.replace(
            "sub_",
            "",
            1
        )

        ok, result = buy_subscription(
            user_id,
            plan
        )

        if not ok:

            if result == "BALANS_YETARLI_EMAS":

                await query.edit_message_text(
                    "❌ <b>Balansingiz yetarli emas.</b>\n\n"
                    "Avval balansni to‘ldiring.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "💳 Balans to‘ldirish",
                                callback_data="payment"
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "⬅️ Orqaga",
                                callback_data="home"
                            )
                        ]
                    ])
                )

            else:

                await query.edit_message_text(
                    f"❌ {result}",
                    reply_markup=back_menu()
                )

            return

        expires = result

        await query.edit_message_text(
            "✅ <b>Obuna muvaffaqiyatli sotib olindi!</b>\n\n"
            f"📅 Tugash vaqti: "
            f"{expires.astimezone().strftime('%d.%m.%Y %H:%M')}\n\n"
            "Obuna muddati tugaganda sizga avtomatik "
            "xabar yuboriladi.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu()
        )

        return

    # ---------------- PROFILE ----------------

    if data == "profile":

        row = get_user(user_id)

        await query.edit_message_text(
            "👤 <b>Profil</b>\n\n"
            f"🆔 ID: <code>{user_id}</code>\n"
            f"💰 Balans: <b>{row['balance']:,} so‘m</b>\n\n"
            f"{format_subscription(user_id)}",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu()
        )

        return

    # ---------------- BALANCE ----------------

    if data == "balance":

        await query.edit_message_text(
            f"💰 <b>Balansingiz:</b>\n\n"
            f"<b>{get_balance(user_id):,} so‘m</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "💳 Balans to‘ldirish",
                        callback_data="payment"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Orqaga",
                        callback_data="home"
                    )
                ]
            ])
        )

        return

    # ---------------- PAYMENT ----------------

    if data == "payment":

        await payment_start(
            query,
            context
        )

        return

    # ---------------- HELP ----------------

    if data == "help":

        await query.edit_message_text(
            "ℹ️ <b>Yordam</b>\n\n"
            "🤖 Bot yaratish — o‘zingizning botingizni "
            "ulash.\n\n"
            "⚙️ Botni sozlash — API ulash va sozlamalar.\n\n"
            "💳 Obuna — botdan foydalanish muddatini "
            "sotib olish.\n\n"
            "💰 Balans — hisobingizni ko‘rish.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu()
        )

        return

    # ---------------- ADMIN ----------------

    if data == "admin_card":

        if user_id != ADMIN_ID:
            return

        card = get_setting(
            "payment_card",
            ""
        )

        name = get_setting(
            "payment_name",
            ""
        )

        context.user_data["admin_card_step"] = True

        await query.edit_message_text(
            "💳 <b>Karta sozlamasi</b>\n\n"
            f"Karta: <code>{card or '-'}</code>\n"
            f"Egasi: {name or '-'}\n\n"
            "Yangi karta raqamini yuboring.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu()
        )

        return

    if data == "admin_balance":

        if user_id != ADMIN_ID:
            return

        context.user_data["admin_balance_step"] = True

        await query.edit_message_text(
            "💰 Balans qo‘shish\n\n"
            "Format:\n"
            "<code>USER_ID SUMMA</code>\n\n"
            "Masalan:\n"
            "<code>123456789 50000</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu()
        )

        return

    if data == "admin_payments":

        if user_id != ADMIN_ID:
            return

        with db_lock:
            conn = db()

            rows = conn.execute("""
                SELECT *
                FROM payments
                WHERE status='pending'
                ORDER BY id DESC
                LIMIT 20
            """).fetchall()

            conn.close()

        if not rows:

            text = "💳 Pending to‘lovlar yo‘q."

        else:

            lines = ["💳 <b>Kutilayotgan to‘lovlar</b>\n"]

            for row in rows:

                lines.append(
                    f"🆔 {row['id']} | "
                    f"👤 {row['user_id']} | "
                    f"💰 {row['amount']:,}"
                )

            text = "\n".join(lines)

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu()
        )

        return

    if data == "admin_users":

        if user_id != ADMIN_ID:
            return

        with db_lock:
            conn = db()

            count = conn.execute(
                "SELECT COUNT(*) AS c FROM users"
            ).fetchone()["c"]

            bots = conn.execute(
                "SELECT COUNT(*) AS c FROM user_bots"
            ).fetchone()["c"]

            conn.close()

        await query.edit_message_text(
            "👥 <b>Foydalanuvchilar</b>\n\n"
            f"👤 Users: {count}\n"
            f"🤖 Ulangan botlar: {bots}",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu()
        )

        return

    if data == "admin_stats":

        if user_id != ADMIN_ID:
            return

        with db_lock:
            conn = db()

            users = conn.execute(
                "SELECT COUNT(*) AS c FROM users"
            ).fetchone()["c"]

            payments = conn.execute("""
                SELECT COALESCE(SUM(amount),0) AS s
                FROM payments
                WHERE status='approved'
            """).fetchone()["s"]

            bots = conn.execute(
                "SELECT COUNT(*) AS c FROM user_bots"
            ).fetchone()["c"]

            conn.close()

        await query.edit_message_text(
            "📊 <b>Statistika</b>\n\n"
            f"👥 Foydalanuvchilar: {users}\n"
            f"🤖 Botlar: {bots}\n"
            f"💰 Tasdiqlangan to‘lovlar: "
            f"{payments:,} so‘m",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu()
        )

        return

    # ---------------- PAYMENT APPROVE ----------------

    if data.startswith("pay_ok_"):

        try:
            payment_id = int(
                data.split("_")[-1]
            )
        except Exception:
            return

        await approve_payment(
            query,
            payment_id
        )

        return

    if data.startswith("pay_no_"):

        try:
            payment_id = int(
                data.split("_")[-1]
            )
        except Exception:
            return

        await reject_payment(
            query,
            payment_id
        )

        return


# =========================================================
# TEXT HANDLER
# =========================================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    ensure_user(update.effective_user)

    # API wizard
    handled = await handle_api_wizard(
        update,
        context
    )

    if handled:
        return

    # payment amount
    handled = await handle_payment_amount(
        update,
        context
    )

    if handled:
        return

    # markup
    if context.user_data.get("markup_step"):

        text = update.message.text.strip()

        try:
            amount = int(
                text.replace(" ", "")
            )
        except Exception:

            await update.message.reply_text(
                "❌ Faqat raqam yuboring."
            )

            return

        if amount < 0:

            await update.message.reply_text(
                "❌ Ustama manfiy bo‘lishi mumkin emas."
            )

            return

        set_user_markup(
            update.effective_user.id,
            amount
        )

        context.user_data.pop(
            "markup_step",
            None
        )

        await update.message.reply_text(
            f"✅ Ustama <b>{amount:,} so‘m</b> qilib saqlandi.",
            parse_mode=ParseMode.HTML,
            reply_markup=settings_menu()
        )

        return

    # welcome
    if context.user_data.get("welcome_step"):

        text = update.message.text.strip()

        with db_lock:
            conn = db()

            conn.execute("""
                UPDATE user_bots
                SET welcome_text=?,
                    updated_at=?
                WHERE user_id=?
            """, (
                text,
                now_str(),
                update.effective_user.id
            ))

            conn.commit()
            conn.close()

        context.user_data.pop(
            "welcome_step",
            None
        )

        await update.message.reply_text(
            "✅ Welcome matn saqlandi.",
            reply_markup=settings_menu()
        )

        return

    # create bot token
    if context.user_data.get("bot_token_step"):

        token = update.message.text.strip()

        context.user_data.pop(
            "bot_token_step",
            None
        )

        await update.message.reply_text(
            "⏳ Bot tekshirilmoqda..."
        )

        ok, status, me = await add_user_bot(
            update.effective_user.id,
            token
        )

        if status == "DUPLICATE":

            await update.message.reply_text(
                "❌ <b>Bu bot allaqachon qo‘shilgan.</b>\n\n"
                "Bir botni qayta qo‘shib bo‘lmaydi.",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu()
            )

            return

        if status == "INVALID":

            await update.message.reply_text(
                "❌ Bot TOKEN noto‘g‘ri yoki botga ulanish imkoni yo‘q.",
                reply_markup=main_menu()
            )

            return

        if ok and me:

            await update.message.reply_text(
                "✅ <b>Bot muvaffaqiyatli qo‘shildi!</b>\n\n"
                f"🤖 Bot: @{me.username}\n\n"
                "Bot ishga tushirilmoqda...",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu()
            )

            # newly added bot
            asyncio.create_task(
                created_bot_poll_loop(token)
            )

            return

    # admin card
    if context.user_data.get("admin_card_step"):

        if update.effective_user.id != ADMIN_ID:
            return

        card = update.message.text.strip()

        set_setting(
            "payment_card",
            card
        )

        context.user_data.pop(
            "admin_card_step",
            None
        )

        await update.message.reply_text(
            "✅ Karta raqami saqlandi.",
            reply_markup=main_menu()
        )

        return

    # admin balance
    if context.user_data.get("admin_balance_step"):

        if update.effective_user.id != ADMIN_ID:
            return

        parts = update.message.text.strip().split()

        if len(parts) != 2:

            await update.message.reply_text(
                "❌ Format:\nUSER_ID SUMMA"
            )

            return

        try:

            target_user = int(parts[0])
            amount = int(parts[1])

        except Exception:

            await update.message.reply_text(
                "❌ Raqamlar noto‘g‘ri."
            )

            return

        change_balance(
            target_user,
            amount
        )

        context.user_data.pop(
            "admin_balance_step",
            None
        )

        await update.message.reply_text(
            "✅ Balans qo‘shildi."
        )

        try:

            await context.bot.send_message(
                chat_id=target_user,
                text=(
                    "💰 Balansingizga "
                    f"{amount:,} so‘m qo‘shildi."
                )
            )

        except Exception:
            pass

        return


# =========================================================
# PHOTO HANDLER
# =========================================================

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    ensure_user(update.effective_user)

    handled = await handle_payment_photo(
        update,
        context
    )

    if handled:
        return


# =========================================================
# POST INIT
# =========================================================

async def post_init(application):

    logger.info("Bot started.")

    asyncio.create_task(
        subscription_checker(application)
    )

    asyncio.create_task(
        start_all_created_bots()
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN Render Environment'da topilmadi."
        )

    if not ADMIN_ID:

        raise RuntimeError(
            "ADMIN_ID Render Environment'da topilmadi."
        )

    init_db()

    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

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

    application.add_handler(
        CallbackQueryHandler(
            callbacks
        )
    )

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_handler
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    logger.info(
        "Main bot polling started."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main()

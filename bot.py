import os
import sqlite3
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# SOZLAMALAR
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip())
except Exception:
    ADMIN_ID = 0

DB_FILE = "bot.db"

try:
    PORT = int(os.getenv("PORT", "10000"))
except Exception:
    PORT = 10000

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# =========================================================
# RENDER HEALTH SERVER
# =========================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )
        self.end_headers()
        self.wfile.write(b"API ORDER BOT OK")

    def log_message(self, format, *args):
        return


def run_health_server():
    try:
        server = HTTPServer(
            ("0.0.0.0", PORT),
            HealthHandler
        )
        logger.info("Health server PORT=%s", PORT)
        server.serve_forever()
    except Exception as e:
        logger.error("Health server error: %s", e)


# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30
    )
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            added_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            api_key TEXT NOT NULL,
            balance_url TEXT,
            catalog_url TEXT,
            order_url TEXT,
            markup_uzs REAL DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_id INTEGER NOT NULL,
            external_id TEXT NOT NULL,
            name TEXT NOT NULL,
            api_price REAL DEFAULT 0,
            sale_price REAL DEFAULT 0,
            active INTEGER DEFAULT 1,
            UNIQUE(api_id, external_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            api_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            product_name TEXT,
            external_product_id TEXT,
            player_data TEXT,
            delivery_type TEXT,
            api_price REAL DEFAULT 0,
            sale_price REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            provider_order_id TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    conn.commit()
    conn.close()


# =========================================================
# YORDAMCHI FUNKSIYALAR
# =========================================================

def now_str():
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def money(value):
    try:
        return f"{float(value):,.0f}".replace(",", " ")
    except Exception:
        return "0"


def save_user(user):
    conn = db()

    conn.execute("""
        INSERT INTO users (
            user_id,
            username,
            first_name,
            created_at
        )
        VALUES (?, ?, ?, ?)
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


def get_all_admin_ids():
    result = []

    if ADMIN_ID > 0:
        result.append(ADMIN_ID)

    conn = db()

    rows = conn.execute("""
        SELECT user_id
        FROM admins
    """).fetchall()

    conn.close()

    for row in rows:
        uid = int(row["user_id"])

        if uid not in result:
            result.append(uid)

    return result


def is_admin(user_id):
    if ADMIN_ID > 0 and user_id == ADMIN_ID:
        return True

    conn = db()

    row = conn.execute("""
        SELECT user_id
        FROM admins
        WHERE user_id=?
    """, (user_id,)).fetchone()

    conn.close()

    return row is not None


# =========================================================
# TELEGRAM COMMANDLAR
# =========================================================

USER_COMMANDS = [
    BotCommand(
        "start",
        "Botni ishga tushirish"
    ),
    BotCommand(
        "help",
        "Yordam"
    ),
    BotCommand(
        "profile",
        "Profil"
    ),
    BotCommand(
        "balance",
        "Balans"
    ),
    BotCommand(
        "order",
        "Buyurtma"
    ),
]

ADMIN_COMMANDS = [
    BotCommand(
        "start",
        "Botni ishga tushirish"
    ),
    BotCommand(
        "help",
        "Yordam"
    ),
    BotCommand(
        "profile",
        "Profil"
    ),
    BotCommand(
        "balance",
        "Balans"
    ),
    BotCommand(
        "order",
        "Buyurtma"
    ),
    BotCommand(
        "admin",
        "Admin panel"
    ),
]


async def set_user_commands(bot, user_id):
    try:

        if is_admin(user_id):

            await bot.set_my_commands(
                ADMIN_COMMANDS,
                scope=BotCommandScopeChat(user_id)
            )

        else:

            await bot.set_my_commands(
                USER_COMMANDS,
                scope=BotCommandScopeChat(user_id)
            )

    except Exception as e:
        logger.warning(
            "Command menu error: %s",
            e
        )


async def setup_commands(application):

    try:

        # DEFAULT MENYU:
        # /admin YO'Q
        await application.bot.set_my_commands(
            USER_COMMANDS,
            scope=BotCommandScopeDefault()
        )

    except Exception as e:

        logger.warning(
            "Default command error: %s",
            e
        )

    # Adminlargagina /admin ko'rinadi.
    for uid in get_all_admin_ids():

        try:

            await application.bot.set_my_commands(
                ADMIN_COMMANDS,
                scope=BotCommandScopeChat(uid)
            )

        except Exception as e:

            logger.warning(
                "Admin command error: %s",
                e
            )


# =========================================================
# ODDIY USER MENU
# =========================================================

def user_menu():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "🔑 API ulash",
                callback_data="api_add"
            )
        ],

        [
            InlineKeyboardButton(
                "🤖 APIlarim",
                callback_data="api_list"
            ),

            InlineKeyboardButton(
                "💰 Balans",
                callback_data="my_balance"
            )
        ],

        [
            InlineKeyboardButton(
                "📦 Buyurtma",
                callback_data="order"
            )
        ],

        [
            InlineKeyboardButton(
                "👤 Profil",
                callback_data="profile"
            )
        ],

        [
            InlineKeyboardButton(
                "❓ Yordam",
                callback_data="help"
            )
        ]

    ])


# =========================================================
# ADMIN MENU
# =========================================================

def admin_menu():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "👥 Foydalanuvchilar",
                callback_data="admin_users"
            )
        ],

        [
            InlineKeyboardButton(
                "📦 Buyurtmalar",
                callback_data="admin_orders"
            )
        ],

        [
            InlineKeyboardButton(
                "👑 Adminlar",
                callback_data="admins"
            )
        ],

        [
            InlineKeyboardButton(
                "➕ Admin qo‘shish",
                callback_data="admin_add"
            )
        ],

        [
            InlineKeyboardButton(
                "➖ Admin olib tashlash",
                callback_data="admin_remove"
            )
        ],

        [
            InlineKeyboardButton(
                "📊 Statistika",
                callback_data="admin_stats"
            )
        ],

        [
            InlineKeyboardButton(
                "🚪 Admin paneldan chiqish",
                callback_data="admin_exit"
            )
        ]

    ])


# =========================================================
# API MENU
# =========================================================

def api_menu(api_id):

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "💰 API Balans",
                callback_data=f"api_balance:{api_id}"
            )
        ],

        [
            InlineKeyboardButton(
                "🔄 API narxlarini yangilash",
                callback_data=f"api_refresh:{api_id}"
            )
        ],

        [
            InlineKeyboardButton(
                "💵 Ustama qo‘shish",
                callback_data=f"api_markup:{api_id}"
            )
        ],

        [
            InlineKeyboardButton(
                "📦 Buyurtma",
                callback_data=f"api_order:{api_id}"
            )
        ],

        [
            InlineKeyboardButton(
                "🔙 Orqaga",
                callback_data="back_user"
            )
        ]

    ])


# =========================================================
# /START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    save_user(user)

    # /start BOSILGANDA HAR DOIM ODDIY MENU
    # ADMIN PANEL OCHILMAYDI.
    await set_user_commands(
        context.bot,
        user.id
    )

    context.user_data.clear()

    await update.message.reply_text(
        "Assalomu Aleykum! 👋\n\n"
        "🤖 API buyurtma botiga xush kelibsiz.",
        reply_markup=user_menu()
    )


# =========================================================
# /ADMIN
# =========================================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    save_user(user)

    # Oddiy user /admin yozsa:
    if not is_admin(user.id):

        await update.message.reply_text(
            "❌ Sizda admin huquqi yo‘q."
        )

        return

    context.user_data.clear()

    await update.message.reply_text(
        "👑 ADMIN PANEL\n\n"
        "Kerakli bo‘limni tanlang:",
        reply_markup=admin_menu()
    )


# =========================================================
# ADMIN PANELDAN CHIQISH
# =========================================================

async def admin_exit(query, context):

    user = query.from_user

    context.user_data.clear()

    # Admin panel yopiladi.
    # Oddiy user menyusi qaytadi.
    await query.message.edit_text(
        "🏠 Asosiy menyu\n\n"
        "🤖 API buyurtma botiga xush kelibsiz.",
        reply_markup=user_menu()
    )


# =========================================================
# /HELP
# =========================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    text = (
        "❓ Yordam\n\n"
        "/start — Botni ishga tushirish\n"
        "/help — Yordam\n"
        "/profile — Profil\n"
        "/balance — Balans\n"
        "/order — Buyurtma"
    )

    if is_admin(user.id):
        text += "\n/admin — Admin panel"

    await update.message.reply_text(text)


# =========================================================
# /PROFILE
# =========================================================

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    save_user(user)

    conn = db()

    api_count = conn.execute("""
        SELECT COUNT(*) AS c
        FROM api_accounts
        WHERE user_id=? AND active=1
    """, (user.id,)).fetchone()["c"]

    order_count = conn.execute("""
        SELECT COUNT(*) AS c
        FROM orders
        WHERE user_id=?
    """, (user.id,)).fetchone()["c"]

    conn.close()

    await update.message.reply_text(
        "👤 Profil\n\n"
        f"🆔 ID: {user.id}\n"
        f"👤 Username: "
        f"@{user.username if user.username else 'yo‘q'}\n"
        f"📝 Ism: {user.first_name or 'yo‘q'}\n"
        f"🤖 APIlar: {api_count}\n"
        f"📦 Buyurtmalar: {order_count}"
    )


# =========================================================
# /BALANCE
# =========================================================

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    conn = db()

    rows = conn.execute("""
        SELECT id, name
        FROM api_accounts
        WHERE user_id=? AND active=1
        ORDER BY id DESC
    """, (user.id,)).fetchall()

    conn.close()

    if not rows:

        await update.message.reply_text(
            "❌ Siz hali API ulamagansiz.\n\n"
            "🔑 API ulash tugmasini bosing.",
            reply_markup=user_menu()
        )

        return

    buttons = []

    for row in rows:

        buttons.append([
            InlineKeyboardButton(
                f"💰 {row['name']}",
                callback_data=f"api_balance:{row['id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 Orqaga",
            callback_data="back_user"
        )
    ])

    await update.message.reply_text(
        "💰 API tanlang:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# =========================================================
# /ORDER
# =========================================================

async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    conn = db()

    rows = conn.execute("""
        SELECT id, name
        FROM api_accounts
        WHERE user_id=? AND active=1
        ORDER BY id DESC
    """, (user.id,)).fetchall()

    conn.close()

    if not rows:

        await update.message.reply_text(
            "❌ Avval API ulang.",
            reply_markup=user_menu()
        )

        return

    buttons = []

    for row in rows:

        buttons.append([
            InlineKeyboardButton(
                f"🤖 {row['name']}",
                callback_data=f"api_order:{row['id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 Orqaga",
            callback_data="back_user"
        )
    ])

    await update.message.reply_text(
        "📦 API tanlang:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# =========================================================
# API QO'SHISH
# =========================================================

async def api_add_start(query, context):

    context.user_data.clear()

    context.user_data["state"] = "api_name"

    await query.message.edit_text(
        "🔑 API ulash\n\n"
        "API nomini yuboring.\n\n"
        "Masalan:\n"
        "My API"
    )


# =========================================================
# API HEADER
# =========================================================

def api_headers(api):

    return {
        "Authorization": f"Bearer {api['api_key']}",
        "X-API-Key": api["api_key"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def make_url(base_url, endpoint):

    if not endpoint:
        return ""

    endpoint = endpoint.strip()

    if endpoint.startswith("http://"):
        return endpoint

    if endpoint.startswith("https://"):
        return endpoint

    return (
        base_url.rstrip("/")
        + "/"
        + endpoint.lstrip("/")
    )


# =========================================================
# BALANCE TOPISH
# =========================================================

def find_balance(data):

    if isinstance(data, (int, float)):
        return data

    if isinstance(data, str):

        try:
            return float(data)
        except Exception:
            return None

    if isinstance(data, dict):

        keys = [
            "balance",
            "Balance",
            "amount",
            "Amount",
            "credit",
            "credits",
            "wallet",
            "funds",
            "available_balance",
        ]

        for key in keys:

            if key in data:

                result = find_balance(
                    data[key]
                )

                if result is not None:
                    return result

        for value in data.values():

            result = find_balance(value)

            if result is not None:
                return result

    if isinstance(data, list):

        for item in data:

            result = find_balance(item)

            if result is not None:
                return result

    return None


# =========================================================
# API BALANCE
# =========================================================

def get_api_balance(api):

    if not api["balance_url"]:

        return {
            "ok": False,
            "error": "Balance endpoint kiritilmagan."
        }

    url = make_url(
        api["base_url"],
        api["balance_url"]
    )

    try:

        response = requests.get(
            url,
            headers=api_headers(api),
            timeout=20
        )

        if response.status_code >= 400:

            return {
                "ok": False,
                "error": (
                    f"HTTP {response.status_code}: "
                    f"{response.text[:300]}"
                )
            }

        try:
            data = response.json()
        except Exception:

            return {
                "ok": False,
                "error": "API JSON qaytarmadi."
            }

        balance = find_balance(data)

        if balance is None:

            return {
                "ok": False,
                "error": "Balans topilmadi."
            }

        return {
            "ok": True,
            "balance": balance
        }

    except Exception as e:

        return {
            "ok": False,
            "error": str(e)
        }


# =========================================================
# CATALOG NORMALIZE
# =========================================================

def normalize_products(data):

    result = []

    def walk(value):

        if isinstance(value, list):

            for item in value:
                walk(item)

            return

        if not isinstance(value, dict):
            return

        external_id = (
            value.get("id")
            or value.get("product_id")
            or value.get("productId")
            or value.get("service")
            or value.get("service_id")
            or value.get("package_id")
        )

        name = (
            value.get("name")
            or value.get("title")
            or value.get("product_name")
            or value.get("description")
            or value.get("package_name")
        )

        price = value.get("price")

        if price is None:
            price = value.get("cost")

        if price is None:
            price = value.get("amount")

        if price is None:
            price = value.get("sale_price")

        if (
            external_id is not None
            and name is not None
            and price is not None
        ):

            try:

                result.append({
                    "external_id": str(external_id),
                    "name": str(name),
                    "price": float(price)
                })

            except Exception:
                pass

        for key in [
            "data",
            "products",
            "services",
            "packages",
            "items",
            "results",
            "catalog"
        ]:

            if key in value:
                walk(value[key])

    walk(data)

    unique = {}

    for item in result:

        key = item["external_id"]

        if key not in unique:
            unique[key] = item

    return list(unique.values())


# =========================================================
# CATALOG YANGILASH
# =========================================================

def refresh_catalog(api_id):

    conn = db()

    api = conn.execute("""
        SELECT *
        FROM api_accounts
        WHERE id=? AND active=1
    """, (api_id,)).fetchone()

    conn.close()

    if not api:

        return {
            "ok": False,
            "error": "API topilmadi."
        }

    if not api["catalog_url"]:

        return {
            "ok": False,
            "error": "Catalog endpoint kiritilmagan."
        }

    url = make_url(
        api["base_url"],
        api["catalog_url"]
    )

    try:

        response = requests.get(
            url,
            headers=api_headers(api),
            timeout=30
        )

        if response.status_code >= 400:

            return {
                "ok": False,
                "error": (
                    f"HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
            }

        try:
            data = response.json()
        except Exception:

            return {
                "ok": False,
                "error": "API JSON qaytarmadi."
            }

        products = normalize_products(data)

        if not products:

            return {
                "ok": False,
                "error": "Mahsulotlar topilmadi."
            }

        conn = db()

        conn.execute("""
            UPDATE products
            SET active=0
            WHERE api_id=?
        """, (api_id,))

        markup = float(
            api["markup_uzs"] or 0
        )

        for product in products:

            api_price = float(
                product["price"]
            )

            sale_price = (
                api_price + markup
            )

            conn.execute("""
                INSERT INTO products (
                    api_id,
                    external_id,
                    name,
                    api_price,
                    sale_price,
                    active
                )
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(api_id, external_id)
                DO UPDATE SET
                    name=excluded.name,
                    api_price=excluded.api_price,
                    sale_price=excluded.sale_price,
                    active=1
            """, (
                api_id,
                product["external_id"],
                product["name"],
                api_price,
                sale_price
            ))

        conn.commit()
        conn.close()

        return {
            "ok": True,
            "count": len(products)
        }

    except Exception as e:

        return {
            "ok": False,
            "error": str(e)
        }


# =========================================================
# PRODUCTS KO'RSATISH
# =========================================================

async def show_products(query, api_id):

    conn = db()

    rows = conn.execute("""
        SELECT *
        FROM products
        WHERE api_id=? AND active=1
        ORDER BY id ASC
    """, (api_id,)).fetchall()

    conn.close()

    if not rows:

        await query.message.edit_text(
            "❌ Katalog bo‘sh.\n\n"
            "🔄 API narxlarini yangilang.",
            reply_markup=api_menu(api_id)
        )

        return

    buttons = []

    for row in rows:

        name = str(row["name"])

        if len(name) > 35:
            name = name[:32] + "..."

        buttons.append([
            InlineKeyboardButton(
                f"{name} — "
                f"{money(row['sale_price'])} UZS",
                callback_data=f"product:{row['id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 Orqaga",
            callback_data=f"api_open:{api_id}"
        )
    ])

    await query.message.edit_text(
        "📦 Mahsulotni tanlang:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# =========================================================
# USER API LIST
# =========================================================

async def show_user_api_list(
    query,
    user_id,
    mode
):

    conn = db()

    rows = conn.execute("""
        SELECT id, name
        FROM api_accounts
        WHERE user_id=? AND active=1
        ORDER BY id DESC
    """, (user_id,)).fetchall()

    conn.close()

    if not rows:

        await query.message.edit_text(
            "❌ Avval API ulang.",
            reply_markup=user_menu()
        )

        return

    buttons = []

    for row in rows:

        if mode == "balance":
            callback = (
                f"api_balance:{row['id']}"
            )
        else:
            callback = (
                f"api_order:{row['id']}"
            )

        buttons.append([
            InlineKeyboardButton(
                f"🤖 {row['name']}",
                callback_data=callback
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 Orqaga",
            callback_data="back_user"
        )
    ])

    await query.message.edit_text(
        "🤖 API tanlang:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# =========================================================
# ADMIN MANUAL BUYURTMA
# =========================================================

async def notify_admins_manual_order(
    bot,
    order_id
):

    conn = db()

    order = conn.execute("""
        SELECT *
        FROM orders
        WHERE id=?
    """, (order_id,)).fetchone()

    if not order:
        conn.close()
        return

    user = conn.execute("""
        SELECT *
        FROM users
        WHERE user_id=?
    """, (order["user_id"],)).fetchone()

    conn.close()

    username = (
        f"@{user['username']}"
        if user and user["username"]
        else "yo‘q"
    )

    text = (
        "📦 YANGI MANUAL BUYURTMA\n\n"
        f"🆔 Buyurtma: #{order['id']}\n"
        f"👤 User ID: {order['user_id']}\n"
        f"👤 Username: {username}\n"
        f"📦 Mahsulot: {order['product_name']}\n"
        f"🎮 Ma'lumot: {order['player_data']}\n"
        f"💵 Narx: "
        f"{money(order['sale_price'])} UZS\n"
        f"📌 Holat: {order['status']}"
    )

    for admin_id in get_all_admin_ids():

        try:

            await bot.send_message(
                chat_id=admin_id,
                text=text
            )

        except Exception as e:

            logger.warning(
                "Admin notify error: %s",
                e
            )


# =========================================================
# CALLBACK
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    save_user(user)

    data = query.data or ""

    # =====================================================
    # ADMIN PANELDAN CHIQISH
    # =====================================================

    if data == "admin_exit":

        # Bu tugmani faqat admin ishlata oladi.
        if not is_admin(user.id):

            await query.message.edit_text(
                "❌ Sizda admin huquqi yo‘q.",
                reply_markup=user_menu()
            )

            return

        await admin_exit(
            query,
            context
        )

        return

    # =====================================================
    # ADMIN CALLBACKLAR HIMOYASI
    # =====================================================

    if (
        data.startswith("admin_")
        or data == "admins"
    ):

        if not is_admin(user.id):

            await query.message.edit_text(
                "❌ Sizda admin huquqi yo‘q.",
                reply_markup=user_menu()
            )

            return

    # =====================================================
    # USER PROFILE
    # =====================================================

    if data == "profile":

        conn = db()

        api_count = conn.execute("""
            SELECT COUNT(*) AS c
            FROM api_accounts
            WHERE user_id=? AND active=1
        """, (user.id,)).fetchone()["c"]

        order_count = conn.execute("""
            SELECT COUNT(*) AS c
            FROM orders
            WHERE user_id=?
        """, (user.id,)).fetchone()["c"]

        conn.close()

        await query.message.edit_text(
            "👤 Profil\n\n"
            f"🆔 ID: {user.id}\n"
            f"👤 Username: "
            f"@{user.username or 'yo‘q'}\n"
            f"🤖 APIlar: {api_count}\n"
            f"📦 Buyurtmalar: {order_count}",
            reply_markup=user_menu()
        )

        return

    # =====================================================
    # HELP
    # =====================================================

    if data == "help":

        await query.message.edit_text(
            "❓ Yordam\n\n"
            "🔑 API ulash — API ulash\n"
            "🤖 APIlarim — APIlarni boshqarish\n"
            "💰 Balans — API balansi\n"
            "📦 Buyurtma — buyurtma berish",
            reply_markup=user_menu()
        )

        return

    # =====================================================
    # BACK USER
    # =====================================================

    if data == "back_user":

        context.user_data.clear()

        await query.message.edit_text(
            "🏠 Asosiy menyu",
            reply_markup=user_menu()
        )

        return

    # =====================================================
    # API ADD
    # =====================================================

    if data == "api_add":

        await api_add_start(
            query,
            context
        )

        return

    # =====================================================
    # API LIST
    # =====================================================

    if data == "api_list":

        conn = db()

        rows = conn.execute("""
            SELECT id, name
            FROM api_accounts
            WHERE user_id=? AND active=1
            ORDER BY id DESC
        """, (user.id,)).fetchall()

        conn.close()

        if not rows:

            await query.message.edit_text(
                "❌ Sizda hali API yo‘q.",
                reply_markup=user_menu()
            )

            return

        buttons = []

        for row in rows:

            buttons.append([
                InlineKeyboardButton(
                    f"🤖 {row['name']}",
                    callback_data=f"api_open:{row['id']}"
                )
            ])

        buttons.append([
            InlineKeyboardButton(
                "🔙 Orqaga",
                callback_data="back_user"
            )
        ])

        await query.message.edit_text(
            "🤖 APIlarim:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

        return

    # =====================================================
    # BALANCE
    # =====================================================

    if data == "my_balance":

        await show_user_api_list(
            query,
            user.id,
            "balance"
        )

        return

    # =====================================================
    # ORDER
    # =====================================================

    if data == "order":

        await show_user_api_list(
            query,
            user.id,
            "order"
        )

        return

    # =====================================================
    # API OPEN
    # =====================================================

    if data.startswith("api_open:"):

        try:
            api_id = int(
                data.split(":")[1]
            )
        except Exception:
            return

        conn = db()

        api = conn.execute("""
            SELECT *
            FROM api_accounts
            WHERE id=? AND user_id=? AND active=1
        """, (
            api_id,
            user.id
        )).fetchone()

        conn.close()

        if not api:

            await query.message.edit_text(
                "❌ API topilmadi.",
                reply_markup=user_menu()
            )

            return

        await query.message.edit_text(
            f"🤖 API: {api['name']}\n\n"
            "API boshqaruvi:",
            reply_markup=api_menu(api_id)
        )

        return

    # =====================================================
    # API BALANCE
    # =====================================================

    if data.startswith("api_balance:"):

        try:
            api_id = int(
                data.split(":")[1]
            )
        except Exception:
            return

        conn = db()

        api = conn.execute("""
            SELECT *
            FROM api_accounts
            WHERE id=? AND user_id=? AND active=1
        """, (
            api_id,
            user.id
        )).fetchone()

        conn.close()

        if not api:

            await query.message.edit_text(
                "❌ API topilmadi.",
                reply_markup=user_menu()
            )

            return

        await query.message.edit_text(
            "⏳ API balansi tekshirilmoqda..."
        )

        result = await asyncio.to_thread(
            get_api_balance,
            api
        )

        if result["ok"]:

            await query.message.edit_text(
                f"💰 {api['name']} API balansi:\n\n"
                f"💵 {money(result['balance'])}",
                reply_markup=api_menu(api_id)
            )

        else:

            await query.message.edit_text(
                "❌ API balansini olishda xato.\n\n"
                f"{result['error']}",
                reply_markup=api_menu(api_id)
            )

        return

    # =====================================================
    # API REFRESH
    # =====================================================

    if data.startswith("api_refresh:"):

        try:
            api_id = int(
                data.split(":")[1]
            )
        except Exception:
            return

        conn = db()

        api = conn.execute("""
            SELECT *
            FROM api_accounts
            WHERE id=? AND user_id=? AND active=1
        """, (
            api_id,
            user.id
        )).fetchone()

        conn.close()

        if not api:

            await query.message.edit_text(
                "❌ API topilmadi.",
                reply_markup=user_menu()
            )

            return

        await query.message.edit_text(
            "⏳ Katalog yangilanmoqda..."
        )

        result = await asyncio.to_thread(
            refresh_catalog,
            api_id
        )

        if result["ok"]:

            await query.message.edit_text(
                "✅ Katalog yangilandi!\n\n"
                f"📦 Mahsulotlar: {result['count']}",
                reply_markup=api_menu(api_id)
            )

        else:

            await query.message.edit_text(
                "❌ KATALOG YANGILANMADI!\n\n"
                f"Xato: {result['error']}",
                reply_markup=api_menu(api_id)
            )

        return

    # =====================================================
    # MARKUP
    # =====================================================

    if data.startswith("api_markup:"):

        try:
            api_id = int(
                data.split(":")[1]
            )
        except Exception:
            return

        conn = db()

        api = conn.execute("""
            SELECT *
            FROM api_accounts
            WHERE id=? AND user_id=? AND active=1
        """, (
            api_id,
            user.id
        )).fetchone()

        conn.close()

        if not api:

            await query.message.edit_text(
                "❌ API topilmadi.",
                reply_markup=user_menu()
            )

            return

        context.user_data["state"] = "markup"
        context.user_data["markup_api_id"] = api_id

        await query.message.edit_text(
            "💵 Ustama miqdorini UZSda yuboring.\n\n"
            f"Hozirgi ustama: "
            f"{money(api['markup_uzs'])} UZS\n\n"
            "Masalan:\n"
            "5000"
        )

        return

    # =====================================================
    # API ORDER
    # =====================================================

    if data.startswith("api_order:"):

        try:
            api_id = int(
                data.split(":")[1]
            )
        except Exception:
            return

        conn = db()

        api = conn.execute("""
            SELECT *
            FROM api_accounts
            WHERE id=? AND user_id=? AND active=1
        """, (
            api_id,
            user.id
        )).fetchone()

        conn.close()

        if not api:

            await query.message.edit_text(
                "❌ API topilmadi.",
                reply_markup=user_menu()
            )

            return

        await show_products(
            query,
            api_id
        )

        return

    # =====================================================
    # PRODUCT
    # =====================================================

    if data.startswith("product:"):

        try:
            product_id = int(
                data.split(":")[1]
            )
        except Exception:
            return

        conn = db()

        product = conn.execute("""
            SELECT *
            FROM products
            WHERE id=? AND active=1
        """, (
            product_id,
        )).fetchone()

        conn.close()

        if not product:

            await query.message.edit_text(
                "❌ Mahsulot topilmadi."
            )

            return

        context.user_data[
            "selected_product_id"
        ] = product_id

        context.user_data[
            "selected_api_id"
        ] = product["api_id"]

        keyboard = InlineKeyboardMarkup([

            [
                InlineKeyboardButton(
                    "🤖 AUTO",
                    callback_data="delivery:AUTO"
                )
            ],

            [
                InlineKeyboardButton(
                    "👨‍💼 MANUAL",
                    callback_data="delivery:MANUAL"
                )
            ],

            [
                InlineKeyboardButton(
                    "🔙 Orqaga",
                    callback_data=(
                        f"api_order:{product['api_id']}"
                    )
                )
            ]

        ])

        await query.message.edit_text(
            f"📦 {product['name']}\n\n"
            f"💰 API narxi: "
            f"{money(product['api_price'])} UZS\n"
            f"💵 Sotuv narxi: "
            f"{money(product['sale_price'])} UZS\n\n"
            "Yetkazish turini tanlang:",
            reply_markup=keyboard
        )

        return

    # =====================================================
    # DELIVERY
    # =====================================================

    if data.startswith("delivery:"):

        delivery_type = data.split(":")[1]

        product_id = context.user_data.get(
            "selected_product_id"
        )

        api_id = context.user_data.get(
            "selected_api_id"
        )

        if not product_id or not api_id:

            await query.message.edit_text(
                "❌ Mahsulot tanlanmagan.",
                reply_markup=user_menu()
            )

            return

        context.user_data["state"] = "order_data"

        context.user_data["order_info"] = {
            "product_id": product_id,
            "api_id": api_id,
            "delivery_type": delivery_type
        }

        await query.message.edit_text(
            "🎮 Buyurtma ma'lumotini yuboring.\n\n"
            "Masalan:\n"
            "Player ID\n"
            "yoki API talab qiladigan ID."
        )

        return

    # =====================================================
    # ADMIN USERS
    # =====================================================

    if data == "admin_users":

        conn = db()

        count = conn.execute("""
            SELECT COUNT(*) AS c
            FROM users
        """).fetchone()["c"]

        rows = conn.execute("""
            SELECT user_id, username, first_name
            FROM users
            ORDER BY user_id DESC
            LIMIT 20
        """).fetchall()

        conn.close()

        text = (
            f"👥 Foydalanuvchilar: {count}\n\n"
        )

        for row in rows:

            username = (
                f"@{row['username']}"
                if row["username"]
                else "username yo‘q"
            )

            text += (
                f"🆔 {row['user_id']} | "
                f"{username} | "
                f"{row['first_name'] or ''}\n"
            )

        await query.message.edit_text(
            text,
            reply_markup=admin_menu()
        )

        return

    # =====================================================
    # ADMIN ORDERS
    # =====================================================

    if data == "admin_orders":

        conn = db()

        rows = conn.execute("""
            SELECT *
            FROM orders
            ORDER BY id DESC
            LIMIT 20
        """).fetchall()

        conn.close()

        if not rows:

            text = "📦 Hali buyurtmalar yo‘q."

        else:

            text = "📦 Oxirgi buyurtmalar:\n\n"

            for row in rows:

                text += (
                    f"#{row['id']} | "
                    f"{row['product_name']}\n"
                    f"👤 {row['user_id']}\n"
                    f"💵 {money(row['sale_price'])} UZS\n"
                    f"📌 {row['status']}\n"
                    f"🚚 {row['delivery_type']}\n\n"
                )

        await query.message.edit_text(
            text,
            reply_markup=admin_menu()
        )

        return

    # =====================================================
    # ADMINS
    # =====================================================

    if data == "admins":

        conn = db()

        rows = conn.execute("""
            SELECT *
            FROM admins
            ORDER BY added_at DESC
        """).fetchall()

        conn.close()

        text = (
            "👑 Adminlar\n\n"
            f"👑 Asosiy ADMIN_ID: {ADMIN_ID}\n\n"
        )

        if not rows:

            text += "Qo‘shimcha adminlar yo‘q."

        else:

            for row in rows:

                username = (
                    f"@{row['username']}"
                    if row["username"]
                    else "username yo‘q"
                )

                text += (
                    f"🆔 {row['user_id']}\n"
                    f"👤 {username}\n\n"
                )

        await query.message.edit_text(
            text,
            reply_markup=admin_menu()
        )

        return

    # =====================================================
    # ADMIN ADD
    # =====================================================

    if data == "admin_add":

        context.user_data["state"] = "admin_add"

        await query.message.edit_text(
            "➕ Admin qo‘shish\n\n"
            "Foydalanuvchining @username ini yuboring.\n\n"
            "Masalan:\n"
            "@username"
        )

        return

    # =====================================================
    # ADMIN REMOVE
    # =====================================================

    if data == "admin_remove":

        context.user_data["state"] = "admin_remove"

        await query.message.edit_text(
            "➖ Admin olib tashlash\n\n"
            "@username yuboring."
        )

        return

    # =====================================================
    # ADMIN STATS
    # =====================================================

    if data == "admin_stats":

        conn = db()

        users = conn.execute("""
            SELECT COUNT(*) AS c
            FROM users
        """).fetchone()["c"]

        apis = conn.execute("""
            SELECT COUNT(*) AS c
            FROM api_accounts
            WHERE active=1
        """).fetchone()["c"]

        orders = conn.execute("""
            SELECT COUNT(*) AS c
            FROM orders
        """).fetchone()["c"]

        sent = conn.execute("""
            SELECT COUNT(*) AS c
            FROM orders
            WHERE status='sent'
        """).fetchone()["c"]

        pending = conn.execute("""
            SELECT COUNT(*) AS c
            FROM orders
            WHERE status='pending'
        """).fetchone()["c"]

        failed = conn.execute("""
            SELECT COUNT(*) AS c
            FROM orders
            WHERE status='failed'
        """).fetchone()["c"]

        conn.close()

        await query.message.edit_text(
            "📊 Statistika\n\n"
            f"👥 Foydalanuvchilar: {users}\n"
            f"🤖 Aktiv APIlar: {apis}\n"
            f"📦 Buyurtmalar: {orders}\n"
            f"✅ Yuborilgan: {sent}\n"
            f"⏳ Kutilmoqda: {pending}\n"
            f"❌ Xato: {failed}",
            reply_markup=admin_menu()
        )

        return


# =========================================================
# AUTO ORDER
# =========================================================

def send_real_order_sync(order_id):

    conn = db()

    order = conn.execute("""
        SELECT
            o.*,
            a.base_url,
            a.api_key,
            a.order_url
        FROM orders o
        JOIN api_accounts a
            ON a.id=o.api_id
        WHERE o.id=?
    """, (order_id,)).fetchone()

    conn.close()

    if not order:

        return {
            "ok": False,
            "error": "Buyurtma topilmadi."
        }

    if not order["order_url"]:

        return {
            "ok": False,
            "error": "Order endpoint kiritilmagan."
        }

    url = make_url(
        order["base_url"],
        order["order_url"]
    )

    payload = {
        "product_id": order["external_product_id"],
        "service": order["external_product_id"],
        "player_data": order["player_data"],
        "quantity": 1,
        "order_id": str(order_id)
    }

    headers = {
        "Authorization": (
            f"Bearer {order['api_key']}"
        ),
        "X-API-Key": order["api_key"],
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Idempotency-Key": str(uuid.uuid4())
    }

    try:

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30
        )

        if response.status_code >= 400:

            conn = db()

            conn.execute("""
                UPDATE orders
                SET status=?,
                    updated_at=?
                WHERE id=?
            """, (
                "failed",
                now_str(),
                order_id
            ))

            conn.commit()
            conn.close()

            return {
                "ok": False,
                "error": (
                    f"HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
            }

        try:
            data = response.json()
        except Exception:
            data = {}

        provider_id = ""

        if isinstance(data, dict):

            for key in [
                "order_id",
                "orderId",
                "id",
                "request_id",
                "requestId"
            ]:

                if data.get(key) is not None:

                    provider_id = str(
                        data[key]
                    )

                    break

        conn = db()

        conn.execute("""
            UPDATE orders
            SET status=?,
                provider_order_id=?,
                updated_at=?
            WHERE id=?
        """, (
            "sent",
            provider_id,
            now_str(),
            order_id
        ))

        conn.commit()
        conn.close()

        return {
            "ok": True,
            "provider_order_id": provider_id
        }

    except Exception as e:

        conn = db()

        conn.execute("""
            UPDATE orders
            SET status=?,
                updated_at=?
            WHERE id=?
        """, (
            "failed",
            now_str(),
            order_id
        ))

        conn.commit()
        conn.close()

        return {
            "ok": False,
            "error": str(e)
        }


async def send_real_order(order_id):

    return await asyncio.to_thread(
        send_real_order_sync,
        order_id
    )


# =========================================================
# TEXT HANDLER
# =========================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    save_user(user)

    text = (
        update.message.text or ""
    ).strip()

    state = context.user_data.get(
        "state"
    )

    # =====================================================
    # ADMIN ADD
    # =====================================================

    if state == "admin_add":

        if not is_admin(user.id):

            context.user_data.clear()

            await update.message.reply_text(
                "❌ Sizda admin huquqi yo‘q."
            )

            return

        username = text.lstrip("@")

        conn = db()

        row = conn.execute("""
            SELECT *
            FROM users
            WHERE LOWER(username)=LOWER(?)
        """, (
            username,
        )).fetchone()

        if not row:

            conn.close()

            await update.message.reply_text(
                "❌ Foydalanuvchi topilmadi.\n\n"
                "U avval /start bosishi kerak."
            )

            return

        target_id = int(
            row["user_id"]
        )

        if target_id == ADMIN_ID:

            conn.close()

            context.user_data.clear()

            await update.message.reply_text(
                "ℹ️ Bu asosiy admin."
            )

            return

        conn.execute("""
            INSERT INTO admins (
                user_id,
                username,
                added_at
            )
            VALUES (?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET
                username=excluded.username
        """, (
            target_id,
            row["username"] or username,
            now_str()
        ))

        conn.commit()
        conn.close()

        try:

            await context.bot.set_my_commands(
                ADMIN_COMMANDS,
                scope=BotCommandScopeChat(
                    target_id
                )
            )

        except Exception as e:

            logger.warning(
                "Admin command error: %s",
                e
            )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ Admin qo‘shildi.\n\n"
            f"👤 @{username}\n"
            f"🆔 ID: {target_id}",
            reply_markup=admin_menu()
        )

        return

    # =====================================================
    # ADMIN REMOVE
    # =====================================================

    if state == "admin_remove":

        if not is_admin(user.id):

            context.user_data.clear()

            await update.message.reply_text(
                "❌ Sizda admin huquqi yo‘q."
            )

            return

        username = text.lstrip("@")

        conn = db()

        row = conn.execute("""
            SELECT *
            FROM admins
            WHERE LOWER(username)=LOWER(?)
        """, (
            username,
        )).fetchone()

        if not row:

            conn.close()

            await update.message.reply_text(
                "❌ Admin topilmadi."
            )

            return

        target_id = int(
            row["user_id"]
        )

        conn.execute("""
            DELETE FROM admins
            WHERE user_id=?
        """, (
            target_id,
        ))

        conn.commit()
        conn.close()

        # /admin menyudan olib tashlanadi.
        try:

            await context.bot.set_my_commands(
                USER_COMMANDS,
                scope=BotCommandScopeChat(
                    target_id
                )
            )

        except Exception as e:

            logger.warning(
                "Reset commands error: %s",
                e
            )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ Adminlik olib tashlandi.\n\n"
            f"👤 @{username}\n"
            f"🆔 ID: {target_id}",
            reply_markup=admin_menu()
        )

        return

    # =====================================================
    # API NAME
    # =====================================================

    if state == "api_name":

        context.user_data[
            "api_name"
        ] = text

        context.user_data[
            "state"
        ] = "api_base"

        await update.message.reply_text(
            "🌐 API Base URL yuboring.\n\n"
            "Masalan:\n"
            "https://example.com/api"
        )

        return

    # =====================================================
    # API BASE
    # =====================================================

    if state == "api_base":

        if not (
            text.startswith("http://")
            or text.startswith("https://")
        ):

            await update.message.reply_text(
                "❌ URL noto‘g‘ri.\n\n"
                "https:// bilan boshlang."
            )

            return

        context.user_data[
            "api_base"
        ] = text.rstrip("/")

        context.user_data[
            "state"
        ] = "api_key"

        await update.message.reply_text(
            "🔐 API Key yuboring."
        )

        return

    # =====================================================
    # API KEY
    # =====================================================

    if state == "api_key":

        context.user_data[
            "api_key"
        ] = text

        context.user_data[
            "state"
        ] = "balance_url"

        await update.message.reply_text(
            "💰 Balance endpoint yuboring.\n\n"
            "Masalan:\n"
            "/balance\n\n"
            "Kerak bo‘lmasa:\n"
            "skip"
        )

        return

    # =====================================================
    # BALANCE URL
    # =====================================================

    if state == "balance_url":

        context.user_data[
            "balance_url"
        ] = (
            ""
            if text.lower() == "skip"
            else text
        )

        context.user_data[
            "state"
        ] = "catalog_url"

        await update.message.reply_text(
            "📋 Catalog endpoint yuboring.\n\n"
            "Masalan:\n"
            "/products\n\n"
            "Kerak bo‘lmasa:\n"
            "skip"
        )

        return

    # =====================================================
    # CATALOG URL
    # =====================================================

    if state == "catalog_url":

        context.user_data[
            "catalog_url"
        ] = (
            ""
            if text.lower() == "skip"
            else text
        )

        context.user_data[
            "state"
        ] = "order_url"

        await update.message.reply_text(
            "📦 Order endpoint yuboring.\n\n"
            "Masalan:\n"
            "/order\n\n"
            "Kerak bo‘lmasa:\n"
            "skip"
        )

        return

    # =====================================================
    # ORDER URL
    # =====================================================

    if state == "order_url":

        context.user_data[
            "order_url"
        ] = (
            ""
            if text.lower() == "skip"
            else text
        )

        conn = db()

        cur = conn.execute("""
            INSERT INTO api_accounts (
                user_id,
                name,
                base_url,
                api_key,
                balance_url,
                catalog_url,
                order_url,
                markup_uzs,
                active,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user.id,
            context.user_data.get(
                "api_name",
                "API"
            ),
            context.user_data.get(
                "api_base",
                ""
            ),
            context.user_data.get(
                "api_key",
                ""
            ),
            context.user_data.get(
                "balance_url",
                ""
            ),
            context.user_data.get(
                "catalog_url",
                ""
            ),
            context.user_data.get(
                "order_url",
                ""
            ),
            0,
            1,
            now_str()
        ))

        api_id = cur.lastrowid

        conn.commit()
        conn.close()

        context.user_data.clear()

        await update.message.reply_text(
            "✅ API muvaffaqiyatli ulandi!\n\n"
            "API boshqaruvi:",
            reply_markup=api_menu(api_id)
        )

        return

    # =====================================================
    # MARKUP
    # =====================================================

    if state == "markup":

        try:
            markup = float(text)
        except Exception:

            await update.message.reply_text(
                "❌ Faqat raqam yuboring.\n\n"
                "Masalan:\n"
                "5000"
            )

            return

        if markup < 0:

            await update.message.reply_text(
                "❌ Manfiy summa mumkin emas."
            )

            return

        api_id = context.user_data.get(
            "markup_api_id"
        )

        conn = db()

        conn.execute("""
            UPDATE api_accounts
            SET markup_uzs=?
            WHERE id=? AND user_id=?
        """, (
            markup,
            api_id,
            user.id
        ))

        conn.commit()
        conn.close()

        context.user_data.clear()

        await update.message.reply_text(
            "✅ Ustama saqlandi.\n\n"
            f"💵 Ustama: "
            f"{money(markup)} UZS",
            reply_markup=api_menu(api_id)
        )

        return

    # =====================================================
    # ORDER DATA
    # =====================================================

    if state == "order_data":

        info = context.user_data.get(
            "order_info"
        )

        if not info:

            context.user_data.clear()

            await update.message.reply_text(
                "❌ Buyurtma ma'lumoti topilmadi."
            )

            return

        api_id = info["api_id"]
        product_id = info["product_id"]
        delivery_type = info["delivery_type"]

        conn = db()

        product = conn.execute("""
            SELECT *
            FROM products
            WHERE id=? AND api_id=? AND active=1
        """, (
            product_id,
            api_id
        )).fetchone()

        api = conn.execute("""
            SELECT *
            FROM api_accounts
            WHERE id=? AND user_id=? AND active=1
        """, (
            api_id,
            user.id
        )).fetchone()

        if not product or not api:

            conn.close()

            context.user_data.clear()

            await update.message.reply_text(
                "❌ Mahsulot yoki API topilmadi."
            )

            return

        cur = conn.execute("""
            INSERT INTO orders (
                user_id,
                api_id,
                product_id,
                product_name,
                external_product_id,
                player_data,
                delivery_type,
                api_price,
                sale_price,
                status,
                provider_order_id,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user.id,
            api_id,
            product_id,
            product["name"],
            product["external_id"],
            text,
            delivery_type,
            product["api_price"],
            product["sale_price"],
            "pending",
            "",
            now_str(),
            now_str()
        ))

        order_id = cur.lastrowid

        conn.commit()
        conn.close()

        context.user_data.clear()

        if delivery_type == "AUTO":

            await update.message.reply_text(
                f"⏳ Buyurtma #{order_id} yuborilmoqda..."
            )

            result = await send_real_order(
                order_id
            )

            if result["ok"]:

                await update.message.reply_text(
                    "✅ Buyurtma yuborildi!\n\n"
                    f"📦 Buyurtma: #{order_id}\n"
                    f"🔖 Provider ID: "
                    f"{result.get('provider_order_id', '-')}"
                )

            else:

                await update.message.reply_text(
                    "❌ Buyurtma yuborilmadi.\n\n"
                    f"Sabab: {result['error']}"
                )

        else:

            await update.message.reply_text(
                "✅ Buyurtma qabul qilindi!\n\n"
                f"📦 Buyurtma: #{order_id}\n"
                "👨‍💼 Admin tekshiradi."
            )

            await notify_admins_manual_order(
                context.bot,
                order_id
            )

        return

    # =====================================================
    # NOT FOUND STATE
    # =====================================================

    await update.message.reply_text(
        "🏠 Asosiy menyu:",
        reply_markup=user_menu()
    )


# =========================================================
# ERROR
# =========================================================

async def error_handler(
    update,
    context
):

    logger.error(
        "Bot error: %s",
        context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN Render Environment Variables "
            "ichida yo‘q."
        )

    init_db()

    Thread(
        target=run_health_server,
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
            "help",
            help_command
        )
    )

    application.add_handler(
        CommandHandler(
            "profile",
            profile_command
        )
    )

    application.add_handler(
        CommandHandler(
            "balance",
            balance_command
        )
    )

    application.add_handler(
        CommandHandler(
            "order",
            order_command
        )
    )

    # /admin FAQAT SHU YERDA OCHILADI
    application.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    # CALLBACK
    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    # TEXT
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    application.add_error_handler(
        error_handler
    )

    async def post_init(app):

        await setup_commands(app)

    application.post_init = post_init

    logger.info(
        "BOT ISHGA TUSHMOQDA..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()

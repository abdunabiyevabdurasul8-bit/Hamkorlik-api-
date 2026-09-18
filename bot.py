import os
import json
import sqlite3
import asyncio
import logging
import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

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
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DB_FILE = "bot.db"
CONFIG_FILE = "manage.json"

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
        self.wfile.write(b"API ORDER BOT OK")

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Health server started on port %s", port)
    server.serve_forever()


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
            username TEXT,
            first_name TEXT,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            added_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS api_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            api_key TEXT NOT NULL,
            balance_url TEXT DEFAULT '',
            catalog_url TEXT DEFAULT '',
            order_url TEXT DEFAULT '',
            markup_uzs INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
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
            api_id INTEGER,
            product_id INTEGER,
            product_name TEXT,
            external_product_id TEXT,
            player_data TEXT,
            delivery_type TEXT,
            api_price REAL DEFAULT 0,
            sale_price REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            provider_order_id TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


# ============================================================
# CONFIG
# ============================================================

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {
            "settings": {
                "payment_card": "",
                "payment_owner": ""
            }
        }

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "settings": {
                "payment_card": "",
                "payment_owner": ""
            }
        }


# ============================================================
# USER
# ============================================================

def save_user(user):
    conn = db()
    conn.execute("""
        INSERT INTO users(user_id, username, first_name, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (
        user.id,
        user.username or "",
        user.first_name or "",
        datetime.now().isoformat()
    ))
    conn.commit()
    conn.close()


def is_admin(user_id):
    if user_id == ADMIN_ID:
        return True

    conn = db()
    row = conn.execute(
        "SELECT 1 FROM admins WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()

    return row is not None


# ============================================================
# COMMANDS
# ============================================================

async def setup_commands(application):
    user_commands = [
        BotCommand("start", "Botni ishga tushirish"),
        BotCommand("help", "Yordam"),
        BotCommand("profile", "Profil"),
        BotCommand("balance", "Balans"),
        BotCommand("order", "Buyurtma"),
    ]

    admin_commands = [
        BotCommand("start", "Botni ishga tushirish"),
        BotCommand("help", "Yordam"),
        BotCommand("profile", "Profil"),
        BotCommand("balance", "Balans"),
        BotCommand("admin", "Admin panel"),
    ]

    await application.bot.set_my_commands(user_commands)

    # Telegram Bot API scope orqali admin uchun alohida command
    for uid in get_all_admin_ids():
        try:
            from telegram import BotCommandScopeChat

            await application.bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(uid)
            )
        except Exception as e:
            logger.warning("Admin command scope error: %s", e)


def get_all_admin_ids():
    ids = {ADMIN_ID}

    conn = db()
    rows = conn.execute(
        "SELECT user_id FROM admins"
    ).fetchall()
    conn.close()

    for row in rows:
        ids.add(row["user_id"])

    return list(ids)


# ============================================================
# MAIN MENU
# ============================================================

def user_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔑 API ulash", callback_data="api_add")
        ],
        [
            InlineKeyboardButton("🤖 APIlarim", callback_data="api_list"),
            InlineKeyboardButton("💰 Balans", callback_data="my_balance")
        ],
        [
            InlineKeyboardButton("📦 Buyurtma", callback_data="order")
        ],
        [
            InlineKeyboardButton("👤 Profil", callback_data="profile")
        ],
        [
            InlineKeyboardButton("❓ Yordam", callback_data="help")
        ]
    ])


def admin_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="admin_users")
        ],
        [
            InlineKeyboardButton("📦 Buyurtmalar", callback_data="admin_orders")
        ],
        [
            InlineKeyboardButton("👑 Adminlar", callback_data="admins")
        ],
        [
            InlineKeyboardButton("➕ Admin qo‘shish", callback_data="admin_add")
        ],
        [
            InlineKeyboardButton("➖ Admin olib tashlash", callback_data="admin_remove")
        ],
        [
            InlineKeyboardButton("📊 Statistika", callback_data="admin_stats")
        ]
    ])


# ============================================================
# /START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user)

    if is_admin(user.id):
        text = (
            "Assalomu Aleykum! 👋\n\n"
            "👑 Admin paneliga xush kelibsiz."
        )
        await update.message.reply_text(
            text,
            reply_markup=admin_menu()
        )
    else:
        text = (
            "Assalomu Aleykum! 👋\n\n"
            "🤖 API buyurtma botiga xush kelibsiz."
        )
        await update.message.reply_text(
            text,
            reply_markup=user_menu()
        )


# ============================================================
# /HELP
# ============================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    text = (
        "❓ Yordam\n\n"
        "/start — Botni ishga tushirish\n"
        "/help — Yordam\n"
        "/profile — Profil\n"
        "/balance — Balans\n"
        "/order — Buyurtma\n"
    )

    if is_admin(user.id):
        text += "/admin — Admin panel\n"

    await update.message.reply_text(text)


# ============================================================
# /PROFILE
# ============================================================

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user)

    conn = db()

    api_count = conn.execute(
        "SELECT COUNT(*) c FROM api_accounts WHERE user_id=? AND active=1",
        (user.id,)
    ).fetchone()["c"]

    order_count = conn.execute(
        "SELECT COUNT(*) c FROM orders WHERE user_id=?",
        (user.id,)
    ).fetchone()["c"]

    conn.close()

    text = (
        "👤 Profil\n\n"
        f"🆔 ID: {user.id}\n"
        f"👤 Username: @{user.username or 'yo‘q'}\n"
        f"🔑 APIlar: {api_count} ta\n"
        f"📦 Buyurtmalar: {order_count} ta"
    )

    await update.message.reply_text(text)


# ============================================================
# /BALANCE
# ============================================================

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_api_list_for_balance(update.effective_chat.id, context)


async def show_api_list_for_balance(chat_id, context):
    conn = db()
    rows = conn.execute("""
        SELECT id, name
        FROM api_accounts
        WHERE user_id=? AND active=1
        ORDER BY id DESC
    """, (chat_id,)).fetchall()
    conn.close()

    if not rows:
        await context.bot.send_message(
            chat_id,
            "❌ Sizda hali API ulanmagan.\n\n"
            "🔑 API ulash tugmasini bosing.",
            reply_markup=user_menu()
        )
        return

    buttons = []

    for row in rows:
        buttons.append([
            InlineKeyboardButton(
                f"💰 {row['name']} balans",
                callback_data=f"api_balance:{row['id']}"
            )
        ])

    await context.bot.send_message(
        chat_id,
        "💰 Qaysi API balansini ko‘rmoqchisiz?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# /ORDER
# ============================================================

async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_order_apis(update.effective_chat.id, context)


async def show_order_apis(chat_id, context):
    conn = db()

    rows = conn.execute("""
        SELECT id, name
        FROM api_accounts
        WHERE user_id=? AND active=1
        ORDER BY id DESC
    """, (chat_id,)).fetchall()

    conn.close()

    if not rows:
        await context.bot.send_message(
            chat_id,
            "❌ Avval API ulang.",
            reply_markup=user_menu()
        )
        return

    buttons = [
        [
            InlineKeyboardButton(
                f"🤖 {r['name']}",
                callback_data=f"order_api:{r['id']}"
            )
        ]
        for r in rows
    ]

    await context.bot.send_message(
        chat_id,
        "📦 Qaysi API orqali buyurtma qilasiz?",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# API ADD STATE
# ============================================================

def set_state(context, name, value):
    context.user_data[name] = value


def get_state(context, name):
    return context.user_data.get(name)


def clear_api_state(context):
    for key in [
        "api_name",
        "api_base_url",
        "api_key",
        "api_balance_url",
        "api_catalog_url",
        "api_order_url",
        "selected_api_id",
        "selected_product_id",
        "delivery_type",
        "player_data"
    ]:
        context.user_data.pop(key, None)


# ============================================================
# API ULASH
# ============================================================

async def api_add_start(query, context):
    clear_api_state(context)
    set_state(context, "api_step", "name")

    await query.message.edit_text(
        "🔑 API ulash\n\n"
        "1️⃣ API nomini yuboring.\n\n"
        "Masalan:\n"
        "Father"
    )


async def handle_api_add_text(update, context):
    step = get_state(context, "api_step")

    if not step:
        return False

    text = update.message.text.strip()

    if step == "name":
        if len(text) < 2:
            await update.message.reply_text(
                "❌ API nomi juda qisqa."
            )
            return True

        set_state(context, "api_name", text)
        set_state(context, "api_step", "base_url")

        await update.message.reply_text(
            "🌐 API Base URL ni yuboring.\n\n"
            "Masalan:\n"
            "https://example.com/api"
        )
        return True

    if step == "base_url":
        if not text.startswith("http://") and not text.startswith("https://"):
            await update.message.reply_text(
                "❌ URL http:// yoki https:// bilan boshlanishi kerak."
            )
            return True

        set_state(context, "api_base_url", text.rstrip("/"))
        set_state(context, "api_step", "key")

        await update.message.reply_text(
            "🔐 API key/tokenni yuboring."
        )
        return True

    if step == "key":
        set_state(context, "api_key", text)
        set_state(context, "api_step", "balance_url")

        await update.message.reply_text(
            "💰 Balans endpointini yuboring.\n\n"
            "Masalan:\n"
            "/balance\n\n"
            "Agar Base URLning o‘zi balans uchun ishlatilsa:\n"
            "skip"
        )
        return True

    if step == "balance_url":
        if text.lower() == "skip":
            text = ""

        set_state(context, "api_balance_url", text)
        set_state(context, "api_step", "catalog_url")

        await update.message.reply_text(
            "📦 Narx/katalog endpointini yuboring.\n\n"
            "Masalan:\n"
            "/products\n\n"
            "Agar hozircha kerak bo‘lmasa:\n"
            "skip"
        )
        return True

    if step == "catalog_url":
        if text.lower() == "skip":
            text = ""

        set_state(context, "api_catalog_url", text)
        set_state(context, "api_step", "order_url")

        await update.message.reply_text(
            "🚚 Buyurtma endpointini yuboring.\n\n"
            "Masalan:\n"
            "/order"
        )
        return True

    if step == "order_url":
        set_state(context, "api_order_url", text)

        conn = db()

        cur = conn.execute("""
            INSERT INTO api_accounts(
                user_id,
                name,
                base_url,
                api_key,
                balance_url,
                catalog_url,
                order_url,
                markup_uzs,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
        """, (
            update.effective_user.id,
            get_state(context, "api_name"),
            get_state(context, "api_base_url"),
            get_state(context, "api_key"),
            get_state(context, "api_balance_url"),
            get_state(context, "api_catalog_url"),
            get_state(context, "api_order_url"),
            datetime.now().isoformat()
        ))

        api_id = cur.lastrowid

        conn.commit()
        conn.close()

        clear_api_state(context)

        await update.message.reply_text(
            "🔄 API tekshirilmoqda..."
        )

        result = await asyncio.to_thread(
            test_api_connection,
            api_id
        )

        if result["ok"]:
            await update.message.reply_text(
                f"✅ API muvaffaqiyatli ulandi!\n\n"
                f"🔑 API: {result['name']}\n\n"
                "Endi API uchun quyidagi sozlamalar mavjud:",
                reply_markup=api_control_keyboard(api_id)
            )
        else:
            await update.message.reply_text(
                "⚠️ API saqlandi, lekin tekshirishda javob olinmadi.\n\n"
                f"Xato: {result['error']}\n\n"
                "API sozlamalarini tekshirib ko‘ring.",
                reply_markup=api_control_keyboard(api_id)
            )

        return True

    return False


# ============================================================
# API KEYBOARD
# ============================================================

def api_control_keyboard(api_id):
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
                callback_data=f"order_api:{api_id}"
            )
        ]
    ])


# ============================================================
# API TEST
# ============================================================

def get_api(api_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM api_accounts WHERE id=?",
        (api_id,)
    ).fetchone()
    conn.close()
    return row


def build_url(base_url, endpoint):
    if not endpoint:
        return base_url

    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint

    return base_url.rstrip("/") + "/" + endpoint.lstrip("/")


def api_headers(api):
    return {
        "Authorization": f"Bearer {api['api_key']}",
        "X-API-Key": api["api_key"],
        "Accept": "application/json",
        "Content-Type": "application/json"
    }


def test_api_connection(api_id):
    api = get_api(api_id)

    if not api:
        return {
            "ok": False,
            "error": "API topilmadi"
        }

    try:
        if api["balance_url"]:
            url = build_url(
                api["base_url"],
                api["balance_url"]
            )

            response = requests.get(
                url,
                headers=api_headers(api),
                timeout=20
            )

            if response.status_code >= 400:
                return {
                    "ok": False,
                    "error": f"HTTP {response.status_code}"
                }

        return {
            "ok": True,
            "name": api["name"]
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


# ============================================================
# API BALANCE
# ============================================================

def fetch_balance(api_id):
    api = get_api(api_id)

    if not api:
        return {
            "ok": False,
            "error": "API topilmadi"
        }

    if not api["balance_url"]:
        return {
            "ok": False,
            "error": "Balans endpointi kiritilmagan"
        }

    try:
        url = build_url(
            api["base_url"],
            api["balance_url"]
        )

        response = requests.get(
            url,
            headers=api_headers(api),
            timeout=20
        )

        data = response.json()

        if response.status_code >= 400:
            return {
                "ok": False,
                "error": f"HTTP {response.status_code}: {data}"
            }

        balance = find_balance(data)

        if balance is None:
            return {
                "ok": False,
                "error": "Javob ichidan balans topilmadi"
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


def find_balance(data):
    if isinstance(data, dict):
        possible = [
            "balance",
            "Balance",
            "amount",
            "credit",
            "credits",
            "money",
            "wallet"
        ]

        for key in possible:
            if key in data:
                return data[key]

        for value in data.values():
            result = find_balance(value)
            if result is not None:
                return result

    elif isinstance(data, list):
        for item in data:
            result = find_balance(item)
            if result is not None:
                return result

    return None


# ============================================================
# API CATALOG
# ============================================================

def fetch_catalog(api_id):
    api = get_api(api_id)

    if not api:
        return {
            "ok": False,
            "error": "API topilmadi"
        }

    if not api["catalog_url"]:
        return {
            "ok": False,
            "error": "Katalog endpointi kiritilmagan"
        }

    try:
        url = build_url(
            api["base_url"],
            api["catalog_url"]
        )

        response = requests.get(
            url,
            headers=api_headers(api),
            timeout=30
        )

        data = response.json()

        if response.status_code >= 400:
            return {
                "ok": False,
                "error": f"HTTP {response.status_code}: {data}"
            }

        products = normalize_products(data)

        if not products:
            return {
                "ok": False,
                "error": "API javobidan mahsulotlar topilmadi"
            }

        return {
            "ok": True,
            "products": products
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


def normalize_products(data):
    if isinstance(data, dict):
        for key in [
            "products",
            "packages",
            "items",
            "services",
            "data"
        ]:
            if key in data:
                result = normalize_products(data[key])
                if result:
                    return result

        # Bitta mahsulot bo‘lishi mumkin
        if any(
            key in data
            for key in ["id", "product_id", "package_id"]
        ):
            return [data]

    if isinstance(data, list):
        result = []

        for item in data:
            if not isinstance(item, dict):
                continue

            external_id = (
                item.get("id")
                or item.get("product_id")
                or item.get("package_id")
                or item.get("code")
            )

            name = (
                item.get("name")
                or item.get("title")
                or item.get("package")
                or f"Product {external_id}"
            )

            price = (
                item.get("price")
                or item.get("amount")
                or item.get("cost")
                or item.get("sale_price")
            )

            if external_id is None or price is None:
                continue

            try:
                price = float(price)
            except Exception:
                continue

            result.append({
                "external_id": str(external_id),
                "name": str(name),
                "price": price
            })

        return result

    return []


def refresh_catalog(api_id):
    result = fetch_catalog(api_id)

    if not result["ok"]:
        return result

    api = get_api(api_id)
    markup = float(api["markup_uzs"])

    conn = db()

    count = 0

    for item in result["products"]:
        sale_price = item["price"] + markup

        conn.execute("""
            INSERT INTO products(
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
            item["external_id"],
            item["name"],
            item["price"],
            sale_price
        ))

        count += 1

    conn.commit()
    conn.close()

    return {
        "ok": True,
        "count": count,
        "markup": markup
    }


# ============================================================
# MARKUP
# ============================================================

async def api_markup_start(query, context, api_id):
    set_state(context, "markup_api_id", api_id)
    set_state(context, "markup_step", True)

    await query.message.reply_text(
        "💵 Ustama qancha qo‘shmoqchisiz?\n\n"
        "UZSda raqam yuboring.\n\n"
        "Masalan:\n"
        "5000"
    )


async def handle_markup_text(update, context):
    if not get_state(context, "markup_step"):
        return False

    text = update.message.text.strip().replace(",", "").replace(" ", "")

    try:
        value = int(text)

        if value < 0:
            raise ValueError

    except Exception:
        await update.message.reply_text(
            "❌ Faqat musbat UZS summa kiriting.\n\n"
            "Masalan: 5000"
        )
        return True

    api_id = get_state(context, "markup_api_id")

    conn = db()

    conn.execute(
        "UPDATE api_accounts SET markup_uzs=? WHERE id=? AND user_id=?",
        (value, api_id, update.effective_user.id)
    )

    conn.commit()
    conn.close()

    context.user_data.pop("markup_step", None)
    context.user_data.pop("markup_api_id", None)

    await update.message.reply_text(
        f"✅ Ustama saqlandi!\n\n"
        f"💵 Ustama: +{value:,} UZS",
        reply_markup=api_control_keyboard(api_id)
    )

    return True


# ============================================================
# API LIST
# ============================================================

async def show_api_list(update, context):
    user_id = update.effective_user.id

    conn = db()

    rows = conn.execute("""
        SELECT *
        FROM api_accounts
        WHERE user_id=? AND active=1
        ORDER BY id DESC
    """, (user_id,)).fetchall()

    conn.close()

    if not rows:
        if update.callback_query:
            await update.callback_query.message.edit_text(
                "❌ Sizda API yo‘q.",
                reply_markup=user_menu()
            )
        else:
            await update.message.reply_text(
                "❌ Sizda API yo‘q.",
                reply_markup=user_menu()
            )
        return

    buttons = []

    for row in rows:
        buttons.append([
            InlineKeyboardButton(
                f"🔑 {row['name']}",
                callback_data=f"api_view:{row['id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "➕ Yangi API ulash",
            callback_data="api_add"
        )
    ])

    markup = InlineKeyboardMarkup(buttons)

    if update.callback_query:
        await update.callback_query.message.edit_text(
            "🔑 APIlarim:",
            reply_markup=markup
        )
    else:
        await update.message.reply_text(
            "🔑 APIlarim:",
            reply_markup=markup
        )


# ============================================================
# PRODUCTS
# ============================================================

async def show_products(query, context, api_id):
    conn = db()

    rows = conn.execute("""
        SELECT *
        FROM products
        WHERE api_id=? AND active=1
        ORDER BY id ASC
        LIMIT 100
    """, (api_id,)).fetchall()

    conn.close()

    if not rows:
        await query.message.edit_text(
            "❌ Hali mahsulot yo‘q.\n\n"
            "🔄 Avval API narxlarini yangilang."
        )
        return

    buttons = []

    for row in rows:
        buttons.append([
            InlineKeyboardButton(
                f"{row['name']} — {int(row['sale_price']):,} UZS",
                callback_data=f"product:{row['id']}"
            )
        ])

    await query.message.edit_text(
        "📦 Mahsulotni tanlang:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# ORDER
# ============================================================

async def order_product(query, context, product_id):
    conn = db()

    row = conn.execute("""
        SELECT p.*, a.name api_name
        FROM products p
        JOIN api_accounts a ON a.id=p.api_id
        WHERE p.id=?
    """, (product_id,)).fetchone()

    conn.close()

    if not row:
        await query.answer("Mahsulot topilmadi", show_alert=True)
        return

    set_state(context, "selected_product_id", product_id)
    set_state(context, "selected_api_id", row["api_id"])
    set_state(context, "order_step", "delivery")

    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🤖 AUTO yetkazish",
                callback_data="delivery:auto"
            )
        ],
        [
            InlineKeyboardButton(
                "👤 MANUAL yetkazish",
                callback_data="delivery:manual"
            )
        ]
    ])

    await query.message.edit_text(
        f"📦 {row['name']}\n"
        f"💰 Narx: {int(row['sale_price']):,} UZS\n\n"
        "🚚 Yetkazish usulini tanlang:",
        reply_markup=markup
    )


async def delivery_selected(query, context, delivery_type):
    context.user_data["delivery_type"] = delivery_type
    context.user_data["order_step"] = "player_data"

    if delivery_type == "auto":
        text = (
            "🤖 AUTO yetkazish tanlandi.\n\n"
            "📌 Buyurtma yuboriladigan ma'lumotni kiriting.\n\n"
            "Masalan PUBG uchun:\n"
            "Player ID"
        )
    else:
        text = (
            "👤 MANUAL yetkazish tanlandi.\n\n"
            "📌 Buyurtma uchun kerakli ma'lumotni yuboring."
        )

    await query.message.edit_text(text)


async def handle_order_data(update, context):
    if get_state(context, "order_step") != "player_data":
        return False

    player_data = update.message.text.strip()

    product_id = get_state(context, "selected_product_id")
    api_id = get_state(context, "selected_api_id")
    delivery_type = get_state(context, "delivery_type")

    conn = db()

    product = conn.execute("""
        SELECT *
        FROM products
        WHERE id=?
    """, (product_id,)).fetchone()

    if not product:
        conn.close()
        await update.message.reply_text("❌ Mahsulot topilmadi.")
        return True

    now = datetime.now().isoformat()

    cur = conn.execute("""
        INSERT INTO orders(
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
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
    """, (
        update.effective_user.id,
        api_id,
        product_id,
        product["name"],
        product["external_id"],
        player_data,
        delivery_type,
        product["api_price"],
        product["sale_price"],
        now,
        now
    ))

    order_id = cur.lastrowid

    conn.commit()
    conn.close()

    context.user_data.pop("order_step", None)
    context.user_data.pop("selected_product_id", None)
    context.user_data.pop("selected_api_id", None)
    context.user_data.pop("delivery_type", None)

    if delivery_type == "auto":
        result = await asyncio.to_thread(
            send_real_order,
            order_id
        )

        if result["ok"]:
            await update.message.reply_text(
                f"✅ Buyurtma yuborildi!\n\n"
                f"🆔 Buyurtma: #{order_id}\n"
                f"📦 {product['name']}\n"
                f"💰 {int(product['sale_price']):,} UZS\n"
                f"🚚 AUTO\n\n"
                f"📌 Provider ID: {result.get('provider_order_id', '-')}"
            )
        else:
            await update.message.reply_text(
                f"⚠️ Buyurtma yaratildi, lekin APIga yuborilmadi.\n\n"
                f"🆔 #{order_id}\n"
                f"Xato: {result['error']}"
            )
    else:
        await update.message.reply_text(
            f"✅ Buyurtma qabul qilindi!\n\n"
            f"🆔 Buyurtma: #{order_id}\n"
            f"📦 {product['name']}\n"
            f"💰 {int(product['sale_price']):,} UZS\n"
            f"🚚 MANUAL\n\n"
            "👤 Admin buyurtmani qo‘lda bajaradi."
        )

        if ADMIN_ID:
            await context.bot.send_message(
                ADMIN_ID,
                f"📦 Yangi MANUAL buyurtma!\n\n"
                f"🆔 #{order_id}\n"
                f"👤 User: {update.effective_user.id}\n"
                f"📦 {product['name']}\n"
                f"💰 {int(product['sale_price']):,} UZS\n"
                f"📌 Ma'lumot: {player_data}"
            )

    return True


# ============================================================
# REAL API ORDER
# ============================================================

def send_real_order(order_id):
    conn = db()

    order = conn.execute("""
        SELECT o.*, a.*
        FROM orders o
        JOIN api_accounts a ON a.id=o.api_id
        WHERE o.id=?
    """, (order_id,)).fetchone()

    conn.close()

    if not order:
        return {
            "ok": False,
            "error": "Order topilmadi"
        }

    if not order["order_url"]:
        return {
            "ok": False,
            "error": "Order endpointi sozlanmagan"
        }

    try:
        url = build_url(
            order["base_url"],
            order["order_url"]
        )

        payload = {
            "product_id": order["external_product_id"],
            "player_id": order["player_data"],
            "quantity": 1,
            "idempotency_key": str(uuid.uuid4())
        }

        response = requests.post(
            url,
            headers=api_headers(order),
            json=payload,
            timeout=30
        )

        data = response.json()

        if response.status_code >= 400:
            return {
                "ok": False,
                "error": f"HTTP {response.status_code}: {data}"
            }

        provider_id = (
            data.get("order_id")
            or data.get("id")
            or data.get("transaction_id")
            or ""
        )

        conn = db()

        conn.execute("""
            UPDATE orders
            SET status='sent',
                provider_order_id=?,
                updated_at=?
            WHERE id=?
        """, (
            str(provider_id),
            datetime.now().isoformat(),
            order_id
        ))

        conn.commit()
        conn.close()

        return {
            "ok": True,
            "provider_order_id": provider_id
        }

    except Exception as e:
        return {
            "ok": False,
            "error": str(e)
        }


# ============================================================
# CALLBACKS
# ============================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    save_user(user)

    data = query.data

    if data == "api_add":
        await api_add_start(query, context)
        return

    if data == "api_list":
        await show_api_list(update, context)
        return

    if data == "my_balance":
        await show_api_list_for_balance(
            query.message.chat_id,
            context
        )
        return

    if data == "profile":
        conn = db()

        api_count = conn.execute(
            "SELECT COUNT(*) c FROM api_accounts WHERE user_id=?",
            (user.id,)
        ).fetchone()["c"]

        order_count = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE user_id=?",
            (user.id,)
        ).fetchone()["c"]

        conn.close()

        await query.message.edit_text(
            f"👤 Profil\n\n"
            f"🆔 ID: {user.id}\n"
            f"👤 @{user.username or 'yo‘q'}\n"
            f"🔑 API: {api_count} ta\n"
            f"📦 Buyurtma: {order_count} ta",
            reply_markup=user_menu()
        )
        return

    if data == "help":
        await query.message.edit_text(
            "❓ Yordam\n\n"
            "🔑 API ulash — o‘z API'ingizni ulang.\n"
            "💰 Balans — API balansini tekshiring.\n"
            "🔄 API narxlarini yangilang.\n"
            "💵 Ustama — sotuv narxiga UZS qo‘shing.\n"
            "📦 Buyurtma — AUTO yoki MANUAL tanlang.",
            reply_markup=user_menu()
        )
        return

    if data == "order":
        await show_order_apis(
            query.message.chat_id,
            context
        )
        return

    if data.startswith("api_view:"):
        api_id = int(data.split(":")[1])
        api = get_api(api_id)

        if not api or api["user_id"] != user.id:
            await query.message.edit_text("❌ Ruxsat yo‘q.")
            return

        await query.message.edit_text(
            f"🔑 {api['name']}\n\n"
            f"🌐 URL: {api['base_url']}\n"
            f"💵 Ustama: +{int(api['markup_uzs']):,} UZS",
            reply_markup=api_control_keyboard(api_id)
        )
        return

    if data.startswith("api_balance:"):
        api_id = int(data.split(":")[1])
        api = get_api(api_id)

        if not api or api["user_id"] != user.id:
            await query.message.edit_text("❌ Ruxsat yo‘q.")
            return

        await query.message.edit_text(
            "🔄 API balansi tekshirilmoqda..."
        )

        result = await asyncio.to_thread(
            fetch_balance,
            api_id
        )

        if result["ok"]:
            await query.message.edit_text(
                f"💰 {api['name']} API Balansi\n\n"
                f"💵 {result['balance']}",
                reply_markup=api_control_keyboard(api_id)
            )
        else:
            await query.message.edit_text(
                f"❌ Balansni olishda xato:\n\n"
                f"{result['error']}",
                reply_markup=api_control_keyboard(api_id)
            )
        return

    if data.startswith("api_refresh:"):
        api_id = int(data.split(":")[1])
        api = get_api(api_id)

        if not api or api["user_id"] != user.id:
            await query.message.edit_text("❌ Ruxsat yo‘q.")
            return

        await query.message.edit_text(
            "🔄 API yangilanmoqda...\n\n"
            "💰 API narxlari olinmoqda..."
        )

        result = await asyncio.to_thread(
            refresh_catalog,
            api_id
        )

        if result["ok"]:
            await query.message.edit_text(
                f"✅ API narxlari olindi!\n\n"
                f"📦 {result['count']} ta mahsulot\n"
                f"💵 Ustama: +{int(result['markup']):,} UZS",
                reply_markup=api_control_keyboard(api_id)
            )
        else:
            await query.message.edit_text(
                f"❌ API narxlari olinmadi.\n\n"
                f"{result['error']}",
                reply_markup=api_control_keyboard(api_id)
            )
        return

    if data.startswith("api_markup:"):
        api_id = int(data.split(":")[1])
        api = get_api(api_id)

        if not api or api["user_id"] != user.id:
            await query.message.edit_text("❌ Ruxsat yo‘q.")
            return

        await api_markup_start(
            query,
            context,
            api_id
        )
        return

    if data.startswith("order_api:"):
        api_id = int(data.split(":")[1])
        api = get_api(api_id)

        if not api or api["user_id"] != user.id:
            await query.message.edit_text("❌ Ruxsat yo‘q.")
            return

        await show_products(
            query,
            context,
            api_id
        )
        return

    if data.startswith("product:"):
        product_id = int(data.split(":")[1])
        await order_product(
            query,
            context,
            product_id
        )
        return

    if data.startswith("delivery:"):
        delivery_type = data.split(":")[1]

        await delivery_selected(
            query,
            context,
            delivery_type
        )
        return

    # ================= ADMIN =================

    if data == "admin_users":
        if not is_admin(user.id):
            await query.message.edit_text("❌ Ruxsat yo‘q.")
            return

        conn = db()
        count = conn.execute(
            "SELECT COUNT(*) c FROM users"
        ).fetchone()["c"]
        conn.close()

        await query.message.edit_text(
            f"👥 Foydalanuvchilar: {count} ta",
            reply_markup=admin_menu()
        )
        return

    if data == "admin_orders":
        if not is_admin(user.id):
            await query.message.edit_text("❌ Ruxsat yo‘q.")
            return

        conn = db()

        rows = conn.execute("""
            SELECT id, user_id, product_name,
                   sale_price, delivery_type, status
            FROM orders
            ORDER BY id DESC
            LIMIT 20
        """).fetchall()

        conn.close()

        if not rows:
            text = "📦 Buyurtmalar yo‘q."
        else:
            text = "📦 So‘nggi buyurtmalar:\n\n"

            for r in rows:
                text += (
                    f"#{r['id']} | "
                    f"{r['product_name']} | "
                    f"{int(r['sale_price']):,} UZS | "
                    f"{r['delivery_type']} | "
                    f"{r['status']}\n"
                )

        await query.message.edit_text(
            text,
            reply_markup=admin_menu()
        )
        return

    if data == "admins":
        if not is_admin(user.id):
            await query.message.edit_text("❌ Ruxsat yo‘q.")
            return

        conn = db()

        rows = conn.execute("""
            SELECT user_id, username
            FROM admins
            ORDER BY user_id
        """).fetchall()

        conn.close()

        text = "👑 Qo‘shimcha adminlar:\n\n"

        if not rows:
            text += "Hozircha yo‘q."
        else:
            for r in rows:
                text += f"• @{r['username'] or 'username yo‘q'} — {r['user_id']}\n"

        await query.message.edit_text(
            text,
            reply_markup=admin_menu()
        )
        return

    if data == "admin_add":
        if not is_admin(user.id):
            await query.message.edit_text("❌ Ruxsat yo‘q.")
            return

        context.user_data["admin_action"] = "add"

        await query.message.edit_text(
            "➕ Admin qo‘shish\n\n"
            "Foydalanuvchining username'ini yuboring.\n\n"
            "Masalan:\n"
            "@username"
        )
        return

    if data == "admin_remove":
        if not is_admin(user.id):
            await query.message.edit_text("❌ Ruxsat yo‘q.")
            return

        context.user_data["admin_action"] = "remove"

        await query.message.edit_text(
            "➖ Adminni olib tashlash\n\n"
            "@username yuboring."
        )
        return

    if data == "admin_stats":
        if not is_admin(user.id):
            await query.message.edit_text("❌ Ruxsat yo‘q.")
            return

        conn = db()

        total = conn.execute(
            "SELECT COUNT(*) c FROM orders"
        ).fetchone()["c"]

        sent = conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE status='sent'"
        ).fetchone()["c"]

        revenue = conn.execute(
            "SELECT COALESCE(SUM(sale_price),0) s FROM orders"
        ).fetchone()["s"]

        conn.close()

        await query.message.edit_text(
            f"📊 Statistika\n\n"
            f"📦 Jami buyurtma: {total}\n"
            f"✅ APIga yuborilgan: {sent}\n"
            f"💰 Jami tushum: {int(revenue):,} UZS",
            reply_markup=admin_menu()
        )
        return


# ============================================================
# ADMIN USERNAME
# ============================================================

async def handle_admin_action(update, context):
    action = context.user_data.get("admin_action")

    if not action:
        return False

    if not is_admin(update.effective_user.id):
        return False

    username = update.message.text.strip().lstrip("@").lower()

    conn = db()

    user = conn.execute("""
        SELECT *
        FROM users
        WHERE LOWER(username)=?
    """, (username,)).fetchone()

    if not user:
        conn.close()

        await update.message.reply_text(
            "❌ Bu username bilan foydalanuvchi topilmadi.\n\n"
            "U avval botga /start bosgan bo‘lishi kerak."
        )
        return True

    if action == "add":
        conn.execute("""
            INSERT OR REPLACE INTO admins(
                user_id,
                username,
                added_at
            )
            VALUES (?, ?, ?)
        """, (
            user["user_id"],
            user["username"],
            datetime.now().isoformat()
        ))

        conn.commit()
        conn.close()

        context.user_data.pop("admin_action", None)

        await update.message.reply_text(
            f"✅ @{user['username']} admin qilib qo‘shildi."
        )

        try:
            from telegram import BotCommandScopeChat

            await context.bot.set_my_commands(
                [
                    BotCommand("start", "Botni ishga tushirish"),
                    BotCommand("help", "Yordam"),
                    BotCommand("profile", "Profil"),
                    BotCommand("balance", "Balans"),
                    BotCommand("admin", "Admin panel"),
                ],
                scope=BotCommandScopeChat(user["user_id"])
            )
        except Exception as e:
            logger.warning("Admin command update error: %s", e)

        return True

    if action == "remove":
        if user["user_id"] == ADMIN_ID:
            await update.message.reply_text(
                "❌ Asosiy adminni olib tashlab bo‘lmaydi."
            )
            return True

        conn.execute(
            "DELETE FROM admins WHERE user_id=?",
            (user["user_id"],)
        )

        conn.commit()
        conn.close()

        context.user_data.pop("admin_action", None)

        await update.message.reply_text(
            f"✅ @{user['username']} adminlikdan olib tashlandi."
        )

        return True

    return False


# ============================================================
# /ADMIN
# ============================================================

async def admin_command(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ Sizda admin huquqi yo‘q."
        )
        return

    await update.message.reply_text(
        "👑 Admin panel",
        reply_markup=admin_menu()
    )


# ============================================================
# TEXT ROUTER
# ============================================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await handle_admin_action(update, context):
        return

    if await handle_markup_text(update, context):
        return

    if await handle_api_add_text(update, context):
        return

    if await handle_order_data(update, context):
        return

    await update.message.reply_text(
        "⬇️ Menyudan foydalaning.",
        reply_markup=(
            admin_menu()
            if is_admin(update.effective_user.id)
            else user_menu()
        )
    )


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN Render Environment Variables ichida yo‘q."
        )

    if ADMIN_ID == 0:
        raise RuntimeError(
            "ADMIN_ID Render Environment Variables ichida yo‘q."
        )

    init_db()

    Thread(
        target=start_health_server,
        daemon=True
    ).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(setup_commands)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
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
        CommandHandler("order", order_command)
    )

    application.add_handler(
        CommandHandler("admin", admin_command)
    )

    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    logger.info("Bot started.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()

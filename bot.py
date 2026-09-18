import os
import json
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
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
from telegram.error import TelegramError


# ============================================================
# SETTINGS
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DB_FILE = "bot.db"
MANAGE_FILE = "manage.json"

PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ============================================================
# DEFAULT MANAGE.JSON
# ============================================================

DEFAULT_MANAGE = {
    "payment_card": "",
    "payment_name": "",
    "prices": {
        "1_day": 5000,
        "3_day": 13000,
        "7_day": 25000,
        "1_month": 43000
    },
    "welcome_text": "Assalomu Aleykum!",
    "support_text": "Yordam uchun administratorga murojaat qiling."
}


def load_manage():
    if not os.path.exists(MANAGE_FILE):
        save_manage(DEFAULT_MANAGE.copy())

    try:
        with open(MANAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        changed = False

        if "payment_card" not in data:
            data["payment_card"] = ""
            changed = True

        if "payment_name" not in data:
            data["payment_name"] = ""
            changed = True

        if "prices" not in data:
            data["prices"] = DEFAULT_MANAGE["prices"].copy()
            changed = True

        if "welcome_text" not in data:
            data["welcome_text"] = DEFAULT_MANAGE["welcome_text"]
            changed = True

        if "support_text" not in data:
            data["support_text"] = DEFAULT_MANAGE["support_text"]
            changed = True

        if changed:
            save_manage(data)

        return data

    except Exception:
        save_manage(DEFAULT_MANAGE.copy())
        return DEFAULT_MANAGE.copy()


def save_manage(data):
    with open(MANAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


manage = load_manage()


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
            balance INTEGER DEFAULT 0,
            subscription_until TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            bot_token TEXT NOT NULL,
            bot_id INTEGER,
            bot_username TEXT,
            bot_name TEXT,
            welcome_text TEXT DEFAULT 'Assalomu Aleykum!',
            active INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            photo_id TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            approved_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan TEXT NOT NULL,
            amount INTEGER NOT NULL,
            days INTEGER NOT NULL,
            started_at TEXT,
            expires_at TEXT,
            status TEXT DEFAULT 'active'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()
    conn.close()


# ============================================================
# TIME
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def now_string():
    return now_utc().isoformat()


def parse_time(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


# ============================================================
# USER FUNCTIONS
# ============================================================

def save_user(tg_user):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT user_id FROM users WHERE user_id = ?",
        (tg_user.id,)
    )

    exists = cur.fetchone()

    if exists:
        cur.execute("""
            UPDATE users
            SET username = ?, first_name = ?
            WHERE user_id = ?
        """, (
            tg_user.username or "",
            tg_user.first_name or "",
            tg_user.id
        ))
    else:
        cur.execute("""
            INSERT INTO users
            (user_id, username, first_name, balance, subscription_until, created_at)
            VALUES (?, ?, ?, 0, NULL, ?)
        """, (
            tg_user.id,
            tg_user.username or "",
            tg_user.first_name or "",
            now_string()
        ))

    conn.commit()
    conn.close()


def get_user(user_id):
    conn = db()
    cur = conn.cursor()

    cur.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,)
    )

    row = cur.fetchone()
    conn.close()

    return row


def get_balance(user_id):
    row = get_user(user_id)

    if not row:
        return 0

    return int(row["balance"] or 0)


def add_balance(user_id, amount):
    conn = db()

    conn.execute("""
        UPDATE users
        SET balance = balance + ?
        WHERE user_id = ?
    """, (amount, user_id))

    conn.commit()
    conn.close()


def remove_balance(user_id, amount):
    conn = db()

    conn.execute("""
        UPDATE users
        SET balance = balance - ?
        WHERE user_id = ?
    """, (amount, user_id))

    conn.commit()
    conn.close()


# ============================================================
# ADMIN
# ============================================================

def is_admin(user_id):
    return user_id == ADMIN_ID


# ============================================================
# USER BOT FUNCTIONS
# ============================================================

def get_user_bots(user_id):
    conn = db()

    cur = conn.cursor()

    cur.execute("""
        SELECT * FROM user_bots
        WHERE user_id = ?
        ORDER BY id DESC
    """, (user_id,))

    rows = cur.fetchall()

    conn.close()

    return rows


def get_bot(bot_id, user_id=None):
    conn = db()
    cur = conn.cursor()

    if user_id is None:
        cur.execute(
            "SELECT * FROM user_bots WHERE id = ?",
            (bot_id,)
        )
    else:
        cur.execute("""
            SELECT * FROM user_bots
            WHERE id = ? AND user_id = ?
        """, (bot_id, user_id))

    row = cur.fetchone()
    conn.close()

    return row


def save_user_bot(
    user_id,
    bot_token,
    bot_id,
    bot_username,
    bot_name
):
    conn = db()

    conn.execute("""
        INSERT INTO user_bots
        (
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
        user_id,
        bot_token,
        bot_id,
        bot_username,
        bot_name,
        manage.get("welcome_text", "Assalomu Aleykum!"),
        now_string(),
        now_string()
    ))

    conn.commit()
    conn.close()


def update_bot_welcome(bot_id, user_id, text):
    conn = db()

    conn.execute("""
        UPDATE user_bots
        SET welcome_text = ?, updated_at = ?
        WHERE id = ? AND user_id = ?
    """, (
        text,
        now_string(),
        bot_id,
        user_id
    ))

    conn.commit()
    conn.close()


# ============================================================
# SUBSCRIPTION
# ============================================================

PLANS = {
    "1_day": {
        "title": "1 kun",
        "days": 1
    },
    "3_day": {
        "title": "3 kun",
        "days": 3
    },
    "7_day": {
        "title": "7 kun",
        "days": 7
    },
    "1_month": {
        "title": "1 oy",
        "days": 30
    }
}


def buy_subscription(user_id, plan_key):
    data = load_manage()

    if plan_key not in PLANS:
        return False, "Noto‘g‘ri tarif."

    price = int(data["prices"][plan_key])
    days = PLANS[plan_key]["days"]

    balance = get_balance(user_id)

    if balance < price:
        return False, (
            f"❌ Balansingiz yetarli emas.\n\n"
            f"Narx: {price:,} so‘m\n"
            f"Balansingiz: {balance:,} so‘m"
        )

    user = get_user(user_id)

    old_expiry = parse_time(user["subscription_until"])

    current = now_utc()

    if old_expiry and old_expiry > current:
        start_time = old_expiry
    else:
        start_time = current

    expiry = start_time + timedelta(days=days)

    conn = db()

    conn.execute("""
        UPDATE users
        SET balance = balance - ?,
            subscription_until = ?
        WHERE user_id = ?
    """, (
        price,
        expiry.isoformat(),
        user_id
    ))

    conn.execute("""
        INSERT INTO subscriptions
        (
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
        user_id,
        plan_key,
        price,
        days,
        start_time.isoformat(),
        expiry.isoformat()
    ))

    conn.commit()
    conn.close()

    return True, (
        f"✅ Obuna muvaffaqiyatli sotib olindi!\n\n"
        f"📦 Tarif: {PLANS[plan_key]['title']}\n"
        f"💰 To‘lov: {price:,} so‘m\n"
        f"⏰ Tugash vaqti:\n"
        f"{expiry.strftime('%Y-%m-%d %H:%M')} UTC"
    )


# ============================================================
# KEYBOARDS
# ============================================================

def main_menu():
    keyboard = [
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
    ]

    return InlineKeyboardMarkup(keyboard)


def back_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ Bosh menyu",
                callback_data="home"
            )
        ]
    ])


def subscription_menu():
    data = load_manage()

    keyboard = [
        [
            InlineKeyboardButton(
                f"1 kun — {int(data['prices']['1_day']):,} so‘m",
                callback_data="buy_1_day"
            )
        ],
        [
            InlineKeyboardButton(
                f"3 kun — {int(data['prices']['3_day']):,} so‘m",
                callback_data="buy_3_day"
            )
        ],
        [
            InlineKeyboardButton(
                f"7 kun — {int(data['prices']['7_day']):,} so‘m",
                callback_data="buy_7_day"
            )
        ],
        [
            InlineKeyboardButton(
                f"1 oy — {int(data['prices']['1_month']):,} so‘m",
                callback_data="buy_1_month"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Orqaga",
                callback_data="home"
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)


def bot_list_menu(user_id):
    bots = get_user_bots(user_id)

    keyboard = []

    for bot in bots:
        username = bot["bot_username"] or bot["bot_name"] or "Bot"

        keyboard.append([
            InlineKeyboardButton(
                f"⚙️ @{username}",
                callback_data=f"editbot_{bot['id']}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "➕ Yangi bot qo‘shish",
            callback_data="create_bot"
        )
    ])

    keyboard.append([
        InlineKeyboardButton(
            "⬅️ Orqaga",
            callback_data="home"
        )
    ])

    return InlineKeyboardMarkup(keyboard)


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
                "💰 Narxlar",
                callback_data="admin_prices"
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
        ],
        [
            InlineKeyboardButton(
                "🚪 Admin paneldan chiqish",
                callback_data="admin_exit"
            )
        ]
    ])


# ============================================================
# /START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    save_user(user)

    context.user_data.clear()

    text = (
        "Assalomu Aleykum! 👋\n\n"
        "🤖 Bot xizmatlari paneliga xush kelibsiz.\n\n"
        "Kerakli bo‘limni tanlang:"
    )

    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=main_menu()
        )


# ============================================================
# /ADMIN
# ============================================================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_admin(user.id):
        await update.message.reply_text(
            "❌ Sizda admin huquqi yo‘q."
        )
        return

    context.user_data.clear()

    await update.message.reply_text(
        "👑 Admin panel\n\n"
        "Kerakli bo‘limni tanlang:",
        reply_markup=admin_menu()
    )


# ============================================================
# CALLBACK
# ============================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    user = query.from_user
    save_user(user)

    data = query.data

    # --------------------------------------------------------
    # HOME
    # --------------------------------------------------------

    if data == "home":
        context.user_data.clear()

        await query.edit_message_text(
            "Assalomu Aleykum! 👋\n\n"
            "🤖 Bot xizmatlari paneliga xush kelibsiz.\n\n"
            "Kerakli bo‘limni tanlang:",
            reply_markup=main_menu()
        )
        return

    # --------------------------------------------------------
    # PROFILE
    # --------------------------------------------------------

    if data == "profile":
        row = get_user(user.id)

        expiry = row["subscription_until"]

        if expiry:
            dt = parse_time(expiry)

            if dt:
                expiry_text = dt.strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
            else:
                expiry_text = expiry
        else:
            expiry_text = "Obuna yo‘q"

        bots = get_user_bots(user.id)

        text = (
            "👤 Profil\n\n"
            f"🆔 ID: {user.id}\n"
            f"👤 Ism: {user.first_name or '-'}\n"
            f"🔹 Username: @{user.username if user.username else '-'}\n"
            f"💰 Balans: {get_balance(user.id):,} so‘m\n"
            f"💳 Obuna: {expiry_text}\n"
            f"🤖 Botlar: {len(bots)} ta"
        )

        await query.edit_message_text(
            text,
            reply_markup=back_menu()
        )
        return

    # --------------------------------------------------------
    # BALANCE
    # --------------------------------------------------------

    if data == "balance":
        card = load_manage().get("payment_card", "")
        name = load_manage().get("payment_name", "")

        if card:
            payment_text = (
                "\n\n💳 Balans to‘ldirish uchun:\n"
                f"Karta: `{card}`\n"
            )

            if name:
                payment_text += f"👤 Qabul qiluvchi: {name}\n"

            payment_text += (
                "\nTo‘lov qilgandan keyin chekni "
                "administratorga yuboring."
            )
        else:
            payment_text = (
                "\n\n⚠️ Hozircha karta ma’lumotlari "
                "sozlanmagan."
            )

        await query.edit_message_text(
            f"💰 Sizning balansingiz:\n\n"
            f"{get_balance(user.id):,} so‘m"
            f"{payment_text}",
            reply_markup=back_menu(),
            parse_mode="Markdown"
        )
        return

    # --------------------------------------------------------
    # SUBSCRIPTION
    # --------------------------------------------------------

    if data == "subscription":
        await query.edit_message_text(
            "💳 Obuna sotib olish\n\n"
            "Kerakli tarifni tanlang:",
            reply_markup=subscription_menu()
        )
        return

    # --------------------------------------------------------
    # BUY SUBSCRIPTION
    # --------------------------------------------------------

    plan_map = {
        "buy_1_day": "1_day",
        "buy_3_day": "3_day",
        "buy_7_day": "7_day",
        "buy_1_month": "1_month"
    }

    if data in plan_map:
        success, message = buy_subscription(
            user.id,
            plan_map[data]
        )

        await query.edit_message_text(
            message,
            reply_markup=back_menu()
        )
        return

    # --------------------------------------------------------
    # CREATE BOT
    # --------------------------------------------------------

    if data == "create_bot":
        context.user_data["state"] = "waiting_bot_token"

        await query.edit_message_text(
            "🤖 Bot yaratish\n\n"
            "1️⃣ Telegram'da @BotFather orqali bot yarating.\n"
            "2️⃣ BotFather bergan TOKENni oling.\n"
            "3️⃣ Shu yerga TOKENni yuboring.\n\n"
            "⚠️ Tokenni boshqa odamlarga bermang.\n\n"
            "Misol:\n"
            "`123456789:AA...`",
            reply_markup=back_menu(),
            parse_mode="Markdown"
        )
        return

    # --------------------------------------------------------
    # SETTINGS
    # --------------------------------------------------------

    if data == "settings":
        bots = get_user_bots(user.id)

        if not bots:
            await query.edit_message_text(
                "⚙️ Botni sozlash\n\n"
                "Sizda hali bot mavjud emas.\n"
                "Avval 🤖 Bot yaratish bo‘limidan bot qo‘shing.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🤖 Bot yaratish",
                            callback_data="create_bot"
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

        await query.edit_message_text(
            "⚙️ Qaysi botni sozlamoqchisiz?",
            reply_markup=bot_list_menu(user.id)
        )
        return

    # --------------------------------------------------------
    # EDIT BOT
    # --------------------------------------------------------

    if data.startswith("editbot_"):
        try:
            bot_id = int(data.split("_")[1])
        except Exception:
            await query.edit_message_text(
                "❌ Bot ID noto‘g‘ri.",
                reply_markup=back_menu()
            )
            return

        bot = get_bot(bot_id, user.id)

        if not bot:
            await query.edit_message_text(
                "❌ Bot topilmadi.",
                reply_markup=back_menu()
            )
            return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📝 Xush kelibsiz matni",
                    callback_data=f"welcome_{bot_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Orqaga",
                    callback_data="settings"
                )
            ]
        ])

        await query.edit_message_text(
            f"⚙️ Botni sozlash\n\n"
            f"🤖 @{bot['bot_username'] or '-'}\n"
            f"📛 Nomi: {bot['bot_name'] or '-'}\n\n"
            "Kerakli sozlamani tanlang:",
            reply_markup=keyboard
        )
        return

    # --------------------------------------------------------
    # WELCOME TEXT
    # --------------------------------------------------------

    if data.startswith("welcome_"):
        try:
            bot_id = int(data.split("_")[1])
        except Exception:
            await query.edit_message_text(
                "❌ Xato.",
                reply_markup=back_menu()
            )
            return

        bot = get_bot(bot_id, user.id)

        if not bot:
            await query.edit_message_text(
                "❌ Bot topilmadi.",
                reply_markup=back_menu()
            )
            return

        context.user_data["state"] = "waiting_welcome"
        context.user_data["edit_bot_id"] = bot_id

        await query.edit_message_text(
            "📝 Yangi xush kelibsiz matnini yuboring.\n\n"
            "Masalan:\n"
            "Assalomu Aleykum! Botimizga xush kelibsiz.",
            reply_markup=back_menu()
        )
        return

    # ========================================================
    # ADMIN
    # ========================================================

    if data.startswith("admin_") or data == "admin_exit":

        if not is_admin(user.id):
            await query.edit_message_text(
                "❌ Sizda admin huquqi yo‘q.",
                reply_markup=back_menu()
            )
            return

    # --------------------------------------------------------
    # ADMIN EXIT
    # --------------------------------------------------------

    if data == "admin_exit":
        context.user_data.clear()

        await query.edit_message_text(
            "Assalomu Aleykum! 👋\n\n"
            "🤖 Bot xizmatlari paneliga xush kelibsiz.",
            reply_markup=main_menu()
        )
        return

    # --------------------------------------------------------
    # ADMIN CARD
    # --------------------------------------------------------

    if data == "admin_card":
        data_file = load_manage()

        card = data_file.get("payment_card", "")
        name = data_file.get("payment_name", "")

        await query.edit_message_text(
            "💳 Karta sozlamalari\n\n"
            f"Karta: {card or 'Kiritilmagan'}\n"
            f"Qabul qiluvchi: {name or 'Kiritilmagan'}\n\n"
            "Kartani o‘zgartirish uchun quyidagi tugmani bosing.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✏️ Karta o‘zgartirish",
                        callback_data="admin_card_edit"
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
        return

    # --------------------------------------------------------
    # ADMIN CARD EDIT
    # --------------------------------------------------------

    if data == "admin_card_edit":
        context.user_data["state"] = "admin_card"
        await query.edit_message_text(
            "💳 Yangi karta ma’lumotini quyidagi formatda yuboring:\n\n"
            "`8600123456789012`\n"
            "Ali Valiyev\n\n"
            "1-qator — karta raqami\n"
            "2-qator — ism-familiya",
            reply_markup=back_menu(),
            parse_mode="Markdown"
        )
        return

    # --------------------------------------------------------
    # ADMIN PRICES
    # --------------------------------------------------------

    if data == "admin_prices":
        prices = load_manage()["prices"]

        text = (
            "💰 Obuna narxlari:\n\n"
            f"1 kun: {int(prices['1_day']):,} so‘m\n"
            f"3 kun: {int(prices['3_day']):,} so‘m\n"
            f"7 kun: {int(prices['7_day']):,} so‘m\n"
            f"1 oy: {int(prices['1_month']):,} so‘m\n\n"
            "Narxni o‘zgartirish uchun tugmani bosing."
        )

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "1 kun",
                        callback_data="price_1_day"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "3 kun",
                        callback_data="price_3_day"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "7 kun",
                        callback_data="price_7_day"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "1 oy",
                        callback_data="price_1_month"
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
        return

    # --------------------------------------------------------
    # PRICE EDIT
    # --------------------------------------------------------

    if data.startswith("price_"):
        plan = data.replace("price_", "")

        if plan not in PLANS:
            await query.edit_message_text(
                "❌ Tarif topilmadi.",
                reply_markup=back_menu()
            )
            return

        context.user_data["state"] = "admin_price"
        context.user_data["price_plan"] = plan

        current_price = load_manage()["prices"][plan]

        await query.edit_message_text(
            f"💰 {PLANS[plan]['title']} narxi\n\n"
            f"Hozirgi narx: {int(current_price):,} so‘m\n\n"
            "Yangi narxni faqat raqam bilan yuboring.\n\n"
            "Masalan:\n"
            "`5000`",
            reply_markup=back_menu(),
            parse_mode="Markdown"
        )
        return

    # --------------------------------------------------------
    # ADMIN USERS
    # --------------------------------------------------------

    if data == "admin_users":
        conn = db()

        cur = conn.cursor()

        cur.execute(
            "SELECT COUNT(*) AS count FROM users"
        )

        count = cur.fetchone()["count"]

        conn.close()

        await query.edit_message_text(
            f"👥 Foydalanuvchilar: {count} ta",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Admin panel",
                        callback_data="admin_panel"
                    )
                ]
            ])
        )
        return

    # --------------------------------------------------------
    # ADMIN STATS
    # --------------------------------------------------------

    if data == "admin_stats":
        conn = db()

        cur = conn.cursor()

        cur.execute(
            "SELECT COUNT(*) AS count FROM users"
        )
        users = cur.fetchone()["count"]

        cur.execute(
            "SELECT COUNT(*) AS count FROM user_bots"
        )
        bots = cur.fetchone()["count"]

        cur.execute(
            "SELECT COUNT(*) AS count FROM subscriptions"
        )
        subs = cur.fetchone()["count"]

        cur.execute(
            "SELECT COALESCE(SUM(amount),0) AS total "
            "FROM subscriptions"
        )
        income = cur.fetchone()["total"]

        conn.close()

        await query.edit_message_text(
            "📊 Statistika\n\n"
            f"👥 Foydalanuvchilar: {users}\n"
            f"🤖 Botlar: {bots}\n"
            f"💳 Obunalar: {subs}\n"
            f"💰 Obuna tushumi: {int(income):,} so‘m",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "⬅️ Admin panel",
                        callback_data="admin_panel"
                    )
                ]
            ])
        )
        return

    # --------------------------------------------------------
    # ADMIN PANEL
    # --------------------------------------------------------

    if data == "admin_panel":
        await query.edit_message_text(
            "👑 Admin panel",
            reply_markup=admin_menu()
        )
        return


# ============================================================
# TEXT / PHOTO MESSAGE HANDLER
# ============================================================

async def message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    save_user(user)

    state = context.user_data.get("state")

    # --------------------------------------------------------
    # ADMIN CARD
    # --------------------------------------------------------

    if state == "admin_card":

        if not is_admin(user.id):
            context.user_data.clear()
            await update.message.reply_text(
                "❌ Sizda admin huquqi yo‘q."
            )
            return

        if not update.message.text:
            await update.message.reply_text(
                "❌ Karta ma’lumotini matn ko‘rinishida yuboring."
            )
            return

        lines = update.message.text.strip().splitlines()

        card = lines[0].strip()

        name = ""

        if len(lines) >= 2:
            name = lines[1].strip()

        if not card.isdigit():
            await update.message.reply_text(
                "❌ Karta raqami faqat raqamlardan iborat bo‘lsin."
            )
            return

        data = load_manage()

        data["payment_card"] = card
        data["payment_name"] = name

        save_manage(data)

        context.user_data.clear()

        await update.message.reply_text(
            "✅ Karta ma’lumotlari saqlandi.",
            reply_markup=admin_menu()
        )

        return

    # --------------------------------------------------------
    # ADMIN PRICE
    # --------------------------------------------------------

    if state == "admin_price":

        if not is_admin(user.id):
            context.user_data.clear()
            await update.message.reply_text(
                "❌ Sizda admin huquqi yo‘q."
            )
            return

        if not update.message.text:
            await update.message.reply_text(
                "❌ Narxni raqam bilan yuboring."
            )
            return

        value = update.message.text.strip().replace(",", "").replace(" ", "")

        if not value.isdigit():
            await update.message.reply_text(
                "❌ Faqat raqam yuboring.\n\nMasalan: 5000"
            )
            return

        amount = int(value)

        if amount <= 0:
            await update.message.reply_text(
                "❌ Narx 0 dan katta bo‘lishi kerak."
            )
            return

        plan = context.user_data.get("price_plan")

        if plan not in PLANS:
            context.user_data.clear()
            await update.message.reply_text(
                "❌ Tarif topilmadi."
            )
            return

        data = load_manage()

        data["prices"][plan] = amount

        save_manage(data)

        context.user_data.clear()

        await update.message.reply_text(
            f"✅ {PLANS[plan]['title']} narxi "
            f"{amount:,} so‘m qilib saqlandi.",
            reply_markup=admin_menu()
        )

        return

    # --------------------------------------------------------
    # BOT TOKEN
    # --------------------------------------------------------

    if state == "waiting_bot_token":

        if not update.message.text:
            await update.message.reply_text(
                "❌ Bot tokenini matn ko‘rinishida yuboring."
            )
            return

        token = update.message.text.strip()

        if ":" not in token:
            await update.message.reply_text(
                "❌ Token formati noto‘g‘ri.\n\n"
                "BotFather bergan tokenni yuboring."
            )
            return

        await update.message.reply_text(
            "⏳ Bot token tekshirilmoqda..."
        )

        try:
            from telegram import Bot

            test_bot = Bot(token=token)

            me = await test_bot.get_me()

            bot_id = me.id
            bot_username = me.username or ""
            bot_name = me.first_name or ""

            await test_bot.close()

        except Exception as e:
            logger.exception("Bot token validation error")

            await update.message.reply_text(
                "❌ Token ishlamadi.\n\n"
                "BotFather bergan tokenni to‘g‘ri yuborganingizni "
                "tekshiring."
            )
            return

        save_user_bot(
            user.id,
            token,
            bot_id,
            bot_username,
            bot_name
        )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ Bot muvaffaqiyatli qo‘shildi!\n\n"
            f"🤖 Bot: @{bot_username or '-'}\n"
            f"📛 Nomi: {bot_name or '-'}\n\n"
            "Endi ⚙️ Botni sozlash bo‘limidan sozlashingiz mumkin.",
            reply_markup=main_menu()
        )

        return

    # --------------------------------------------------------
    # WELCOME TEXT
    # --------------------------------------------------------

    if state == "waiting_welcome":

        if not update.message.text:
            await update.message.reply_text(
                "❌ Matn yuboring."
            )
            return

        bot_id = context.user_data.get("edit_bot_id")

        if not bot_id:
            context.user_data.clear()
            await update.message.reply_text(
                "❌ Bot topilmadi."
            )
            return

        update_bot_welcome(
            bot_id,
            user.id,
            update.message.text
        )

        context.user_data.clear()

        await update.message.reply_text(
            "✅ Xush kelibsiz matni saqlandi.",
            reply_markup=main_menu()
        )

        return

    # --------------------------------------------------------
    # DEFAULT
    # --------------------------------------------------------

    await update.message.reply_text(
        "Kerakli bo‘limni menyudan tanlang.",
        reply_markup=main_menu()
    )


# ============================================================
# HELP
# ============================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Yordam\n\n"
        "🤖 Bot yaratish — bot tokenini ulash.\n"
        "⚙️ Botni sozlash — ulangan botni sozlash.\n"
        "💳 Obuna sotib olish — xizmat muddatini olish.\n"
        "👤 Profil — hisob ma’lumotlari.\n"
        "💰 Balans — balansni ko‘rish va to‘ldirish.\n\n"
        "Muammo bo‘lsa administratorga murojaat qiling.",
        reply_markup=main_menu()
    )


# ============================================================
# COMMAND MENU
# ============================================================

async def setup_commands(application):
    user_commands = [
        BotCommand("start", "Botni ishga tushirish"),
        BotCommand("help", "Yordam"),
        BotCommand("profile", "Profil"),
        BotCommand("balance", "Balans"),
        BotCommand("order", "Buyurtma")
    ]

    await application.bot.set_my_commands(user_commands)

    if ADMIN_ID:
        admin_commands = [
            BotCommand("start", "Botni ishga tushirish"),
            BotCommand("help", "Yordam"),
            BotCommand("profile", "Profil"),
            BotCommand("balance", "Balans"),
            BotCommand("order", "Buyurtma"),
            BotCommand("admin", "Admin panel")
        ]

        try:
            await application.bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(ADMIN_ID)
            )
        except Exception as e:
            logger.warning(
                "Admin command scope error: %s",
                e
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
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    logger.error(
        "Bot error: %s",
        context.error
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

    health_thread = Thread(
        target=start_health_server,
        daemon=True
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
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("profile", start)
    )

    application.add_handler(
        CommandHandler("balance", start)
    )

    application.add_handler(
        CommandHandler("order", start)
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
            message_handler
        )
    )

    application.add_error_handler(error_handler)

    async def post_init(app):
        await setup_commands(app)

    application.post_init = post_init

    logger.info("Bot starting...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()

import os
import json
import sqlite3
import asyncio
import logging
import threading
from datetime import datetime
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

MAIN_BOT_TOKEN = os.getenv("BOT_TOKEN", "8785996842:AAG3fXo0-BG20Heg6tNxJhTdu9PvwAQG9pE").strip()

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "05692925792"))
except Exception:
    ADMIN_ID = 0

DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bots.db")
)

try:
    PORT = int(os.getenv("PORT", "10000"))
except Exception:
    PORT = 10000


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
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(
        os.path.dirname(DB_PATH),
        exist_ok=True
    )

    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            bot_id INTEGER,
            username TEXT DEFAULT '',
            name TEXT DEFAULT '',
            start_text TEXT DEFAULT
                'Assalomu Aleykum! 👋',
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS custom_buttons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            active INTEGER DEFAULT 1
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            bot_id INTEGER PRIMARY KEY,
            welcome_text TEXT DEFAULT
                'Assalomu Aleykum! 👋'
        )
    """)

    conn.commit()
    conn.close()


# ============================================================
# USERS
# ============================================================

def save_user(user):
    conn = db()

    conn.execute("""
        INSERT INTO users
        (user_id, username, first_name)
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
        SELECT id FROM bots
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
        (bot_id, title, text)
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


def set_start_text(bot_db_id, text):
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
# MAIN BOT MENU
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
# MAIN START
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
        "🤖 Bu platforma orqali o‘zingizga Telegram bot "
        "yaratishingiz va uni sozlashingiz mumkin.",
        reply_markup=main_menu()
    )


# ============================================================
# CREATE BOT
# ============================================================

async def create_bot_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    context.user_data.clear()
    context.user_data["state"] = "waiting_bot_token"

    await query.message.reply_text(
        "🚀 Yangi bot yaratish\n\n"
        "1️⃣ Telegram'da @BotFather ni oching.\n"
        "2️⃣ /newbot yuboring.\n"
        "3️⃣ Bot nomi va username tanlang.\n"
        "4️⃣ BotFather bergan tokenni nusxalang.\n\n"
        "📌 Keyin shu yerga tokenni yuboring."
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
# RECEIVE MAIN BOT TEXT
# ============================================================

async def receive_main_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
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
                "❌ Token noto‘g‘ri.\n\n"
                "BotFather bergan tokenni to‘liq yuboring."
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
            "✅ BOT MUVAFFAQIYATLI QO‘SHILDI!\n\n"
            f"🤖 Nomi: {me.first_name}\n"
            f"👤 Username: "
            f"@{me.username if me.username else 'yo‘q'}\n\n"
            "Endi botingizni sozlashingiz mumkin.",
            reply_markup=settings_menu(bot_db_id)
        )

        if text not in running_bots:
            task = asyncio.create_task(
                run_child_bot(text)
            )
            running_tasks[text] = task

        return

    # --------------------------------------------------------
    # SERVICE NAME
    # --------------------------------------------------------

    if state == "service_name":
        context.user_data["service_name"] = text
        context.user_data["state"] = "service_description"

        await update.message.reply_text(
            "📝 Xizmat tavsifini yuboring.\n\n"
            "Masalan:\n"
            "Telegram Premium 1 oy"
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
            "Masalan:\n"
            "10000"
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
                "❌ Narx faqat raqam bo‘lishi kerak.\n\n"
                "Masalan: 10000"
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
    # CUSTOM BUTTON TITLE
    # --------------------------------------------------------

    if state == "button_title":

        context.user_data["button_title"] = text
        context.user_data["state"] = "button_text"

        await update.message.reply_text(
            "📝 Tugma bosilganda chiqadigan matnni yuboring."
        )
        return

    # --------------------------------------------------------
    # CUSTOM BUTTON TEXT
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
                "❌ Kod formati noto‘g‘ri.\n\n"
                "JSON formatidan foydalaning."
            )
            return

        if not isinstance(data, dict):
            await update.message.reply_text(
                "❌ JSON obyekt bo‘lishi kerak."
            )
            return

        # Xizmatlarni qo‘shish
        services = data.get("services", [])

        if isinstance(services, list):

            added = 0

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

                if not name:
                    continue

                if price < 0:
                    continue

                add_service(
                    bot_id,
                    name,
                    description,
                    price
                )

                added += 1

        # Start text
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
# MY BOTS
# ============================================================

async def my_bots_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
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

        title = (
            f"🤖 @{username}"
            if username
            else f"🤖 {bot['name']}"
        )

        buttons.append([
            InlineKeyboardButton(
                title,
                callback_data=f"manage:{bot['id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🤖 Yangi bot yaratish",
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
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
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
        f"@{bot['username']}",
        reply_markup=settings_menu(bot_id)
    )


# ============================================================
# SERVICES
# ============================================================

async def services_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    bot_id = int(
        query.data.split(":")[1]
    )

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
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    bot_id = int(
        query.data.split(":")[1]
    )

    context.user_data.clear()
    context.user_data["bot_id"] = bot_id
    context.user_data["state"] = "service_name"

    await query.message.reply_text(
        "➕ Xizmat qo‘shish\n\n"
        "Xizmat nomini yuboring.\n\n"
        "Masalan:\n"
        "Telegram Premium 1 oy"
    )


# ============================================================
# EDIT SERVICES
# ============================================================

async def edit_services_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    bot_id = int(
        query.data.split(":")[1]
    )

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
                callback_data=f"editone:{service['id']}:{bot_id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "⬅️ Orqaga",
            callback_data=f"manage:{bot_id}"
        )
    ])

    await query.message.reply_text(
        "✏️ O‘zgartirmoqchi bo‘lgan xizmatni tanlang:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


# ============================================================
# EDIT ONE
# ============================================================

async def edit_one_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")

    service_id = int(parts[1])
    bot_id = int(parts[2])

    conn = db()

    service = conn.execute("""
        SELECT *
        FROM services
        WHERE id = ?
    """, (service_id,)).fetchone()

    conn.close()

    if not service:
        await query.message.reply_text(
            "❌ Xizmat topilmadi."
        )
        return

    context.user_data.clear()
    context.user_data["state"] = "edit_service_name"
    context.user_data["service_id"] = service_id
    context.user_data["bot_id"] = bot_id
    context.user_data["old_description"] = service["description"]

    await query.message.reply_text(
        "✏️ Yangi xizmat nomini yuboring.\n\n"
        f"Eski nom: {service['name']}"
    )


# ============================================================
# EDIT SERVICE TEXT HANDLER
# ============================================================

async def edit_service_text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    state = context.user_data.get("state")

    if state == "edit_service_name":

        context.user_data["new_name"] = (
            update.message.text.strip()
        )

        context.user_data["state"] = (
            "edit_service_description"
        )

        await update.message.reply_text(
            "📝 Yangi tavsifni yuboring.\n\n"
            "Agar tavsif kerak bo‘lmasa: -"
        )

        return True

    if state == "edit_service_description":

        desc = update.message.text.strip()

        if desc == "-":
            desc = ""

        context.user_data["new_description"] = desc
        context.user_data["state"] = "edit_service_price"

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

        service_id = context.user_data["service_id"]
        bot_id = context.user_data["bot_id"]

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
# DELETE SERVICES
# ============================================================

async def delete_services_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    bot_id = int(
        query.data.split(":")[1]
    )

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
                f"🗑 {service['name']}",
                callback_data=f"deleteone:{service['id']}:{bot_id}"
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
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")

    service_id = int(parts[1])
    bot_id = int(parts[2])

    delete_service(service_id)

    await query.message.reply_text(
        "✅ Xizmat o‘chirildi.",
        reply_markup=settings_menu(bot_id)
    )


# ============================================================
# START TEXT
# ============================================================

async def start_text_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    bot_id = int(
        query.data.split(":")[1]
    )

    context.user_data.clear()
    context.user_data["state"] = "start_text"
    context.user_data["bot_id"] = bot_id

    current = get_start_text(bot_id)

    await query.message.reply_text(
        "👋 Yangi Start xabarini yuboring.\n\n"
        f"Hozirgi xabar:\n{current}"
    )


# ============================================================
# ADD CUSTOM BUTTON
# ============================================================

async def add_button_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    bot_id = int(
        query.data.split(":")[1]
    )

    context.user_data.clear()
    context.user_data["state"] = "button_title"
    context.user_data["bot_id"] = bot_id

    await query.message.reply_text(
        "🔘 Tugma nomini yuboring.\n\n"
        "Masalan:\n"
        "📞 Admin"
    )


# ============================================================
# CODE MODE
# ============================================================

async def code_mode_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    bot_id = int(
        query.data.split(":")[1]
    )

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
        "💻 Kod orqali dasturlash\n\n"
        "Bu yerda xavfsiz konfiguratsiya kodi "
        "yuboriladi.\n\n"
        "Masalan:\n\n"
        + json.dumps(
            example,
            ensure_ascii=False,
            indent=2
        )
        + "\n\n"
        "Shu formatda JSON yuboring."
    )


# ============================================================
# CHILD BOT MENU
# ============================================================

def child_menu(bot_db_id):
    buttons = [
        [
            InlineKeyboardButton(
                "🛍 Xizmatlar",
                callback_data=f"child_services:{bot_db_id}"
            )
        ]
    ]

    custom = get_buttons(bot_db_id)

    for button in custom:
        buttons.append([
            InlineKeyboardButton(
                button["title"],
                callback_data=f"custom:{button['id']}:{bot_db_id}"
            )
        ])

    return InlineKeyboardMarkup(buttons)


# ============================================================
# CHILD /START
# ============================================================

async def child_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    bot_db_id = context.application.bot_data[
        "bot_db_id"
    ]

    text = get_start_text(bot_db_id)

    await update.message.reply_text(
        text,
        reply_markup=child_menu(bot_db_id)
    )


# ============================================================
# CHILD SERVICES
# ============================================================

async def child_services_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    bot_db_id = context.application.bot_data[
        "bot_db_id"
    ]

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
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")

    service_id = int(parts[1])

    conn = db()

    service = conn.execute("""
        SELECT *
        FROM services
        WHERE id = ?
        AND active = 1
    """, (service_id,)).fetchone()

    conn.close()

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
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")

    button_id = int(parts[1])

    conn = db()

    row = conn.execute("""
        SELECT *
        FROM custom_buttons
        WHERE id = ?
        AND active = 1
    """, (button_id,)).fetchone()

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
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "ℹ️ Yordam\n\n"
        "/start — Asosiy menyu\n"
        "/help — Yordam"
    )


# ============================================================
# RUN CHILD BOT
# ============================================================

async def run_child_bot(token):

    if token in running_bots:
        return

    app = None

    try:

        # Token qaysi botga tegishli?
        conn = db()

        row = conn.execute("""
            SELECT id
            FROM bots
            WHERE token = ?
        """, (token,)).fetchone()

        conn.close()

        if not row:
            return

        bot_db_id = row["id"]

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
            CallbackQueryHandler(
                child_services_callback,
                pattern=r"^child_services:"
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
# RESTORE CHILD BOTS
# ============================================================

async def restore_saved_bots():

    rows = get_active_bots()

    logger.info(
        "Restoring %s child bots",
        len(rows)
    )

    for row in rows:

        token = row["token"]

        if token in running_bots:
            continue

        task = asyncio.create_task(
            run_child_bot(token)
        )

        running_tasks[token] = task

        await asyncio.sleep(1)


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    if not query:
        return

    data = query.data

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

    elif data == "back_main":
        await query.answer()

        await query.message.reply_text(
            "Asosiy menyu:",
            reply_markup=main_menu()
        )

    elif data.startswith("manage:"):
        await manage_bot_callback(
            update,
            context
        )

    elif data.startswith("services:"):
        await services_callback(
            update,
            context
        )

    elif data.startswith("add_service:"):
        await add_service_callback(
            update,
            context
        )

    elif data.startswith("edit_services:"):
        await edit_services_callback(
            update,
            context
        )

    elif data.startswith("editone:"):
        await edit_one_callback(
            update,
            context
        )

    elif data.startswith("delete_services:"):
        await delete_services_callback(
            update,
            context
        )

    elif data.startswith("deleteone:"):
        await delete_one_callback(
            update,
            context
        )

    elif data.startswith("start_text:"):
        await start_text_callback(
            update,
            context
        )

    elif data.startswith("add_button:"):
        await add_button_callback(
            update,
            context
        )

    elif data.startswith("code_mode:"):
        await code_mode_callback(
            update,
            context
        )

    else:
        await query.answer()


# ============================================================
# MAIN TEXT ROUTER
# ============================================================

async def main_text_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    state = context.user_data.get("state")

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
        ("0.0.0.0", PORT),
        HealthHandler
    )

    logger.info(
        "Health server running on port %s",
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

    init_db()

    # Render Web Service port
    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()

    app = (
        Application.builder()
        .token(MAIN_BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

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

    await restore_saved_bots()

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
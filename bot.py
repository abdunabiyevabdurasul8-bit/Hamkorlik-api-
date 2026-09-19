import os
import json
import sqlite3
import logging
import asyncio
from datetime import datetime

import requests

from telegram import (
    Update,
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommandScopeDefault,
    MenuButtonDefault,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# DONUZ MULTI-BOT RESELLER PLATFORM
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_FILE = os.getenv("DB_FILE", "donuz.db").strip()
SOS_USERNAME = os.getenv("SOS_USERNAME", "@donuz1").strip()

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip())
except Exception:
    ADMIN_ID = 0

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

log = logging.getLogger("DONUZ")

running_bots = {}
master_app = None


# ============================================================
# DATABASE
# ============================================================

def db():
    con = sqlite3.connect(
        DB_FILE,
        check_same_thread=False,
        timeout=30
    )
    con.row_factory = sqlite3.Row

    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass

    return con


def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def q(sql, params=(), fetchone=False, fetchall=False):
    con = db()

    try:
        cur = con.execute(sql, params)
        con.commit()

        if fetchone:
            return cur.fetchone()

        if fetchall:
            return cur.fetchall()

        return cur.lastrowid

    finally:
        con.close()


def init_db():
    con = db()
    cur = con.cursor()

    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS bot_owners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER NOT NULL UNIQUE,
            bot_token TEXT NOT NULL UNIQUE,
            bot_username TEXT,
            bot_name TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            telegram_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            balance REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(owner_id, telegram_id)
        );

        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            price REAL NOT NULL DEFAULT 0,
            delivery_mode TEXT NOT NULL DEFAULT 'manual',
            api_action TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS api_settings (
            owner_id INTEGER PRIMARY KEY,
            api_url TEXT DEFAULT '',
            api_key TEXT DEFAULT '',
            catalog_url TEXT DEFAULT '',
            balance_url TEXT DEFAULT '',
            order_url TEXT DEFAULT '',
            payment_url TEXT DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            target TEXT NOT NULL,
            amount REAL NOT NULL,
            delivery_mode TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            provider_response TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            method TEXT NOT NULL DEFAULT 'manual',
            receipt TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS states (
            owner_id INTEGER NOT NULL,
            telegram_id INTEGER NOT NULL,
            state TEXT NOT NULL,
            data TEXT DEFAULT '{}',
            PRIMARY KEY(owner_id, telegram_id)
        );
        """
    )

    con.commit()
    con.close()


# ============================================================
# STATES
# ============================================================

def set_state(owner_id, telegram_id, state, data=None):
    q(
        """
        INSERT INTO states(owner_id, telegram_id, state, data)
        VALUES(?,?,?,?)
        ON CONFLICT(owner_id,telegram_id)
        DO UPDATE SET
            state=excluded.state,
            data=excluded.data
        """,
        (
            owner_id,
            telegram_id,
            state,
            json.dumps(data or {}, ensure_ascii=False),
        ),
    )


def get_state(owner_id, telegram_id):
    r = q(
        """
        SELECT state, data
        FROM states
        WHERE owner_id=? AND telegram_id=?
        """,
        (owner_id, telegram_id),
        fetchone=True,
    )

    if not r:
        return None, {}

    try:
        return r["state"], json.loads(r["data"] or "{}")
    except Exception:
        return r["state"], {}


def clear_state(owner_id, telegram_id):
    q(
        """
        DELETE FROM states
        WHERE owner_id=? AND telegram_id=?
        """,
        (owner_id, telegram_id),
    )


# ============================================================
# KEYBOARDS
# ============================================================

def master_kb():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("🤖 Botimni ulash"),
                KeyboardButton("⚙️ Mening panelim"),
            ],
            [
                KeyboardButton("📖 Qo‘llanma"),
                KeyboardButton("🆘 SOS"),
            ],
        ],
        resize_keyboard=True,
    )


def owner_panel_kb():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("🛒 Xizmatlarim"),
                KeyboardButton("➕ Xizmat qo‘shish"),
            ],
            [
                KeyboardButton("📦 Buyurtmalar"),
                KeyboardButton("👥 Foydalanuvchilar"),
            ],
            [
                KeyboardButton("🔌 API ulash"),
                KeyboardButton("💰 API Balans"),
            ],
            [
                KeyboardButton("🔄 Katalog yangilash"),
                KeyboardButton("⚙️ API sozlamalari"),
            ],
            [
                KeyboardButton("💳 To‘lovlar"),
                KeyboardButton("🆘 SOS"),
            ],
            [
                KeyboardButton("⬅️ Asosiy menyu"),
            ],
        ],
        resize_keyboard=True,
    )


def customer_kb():
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("🛒 Xizmatlar"),
                KeyboardButton("💰 Balans"),
            ],
            [
                KeyboardButton("➕ Balans to‘ldirish"),
                KeyboardButton("📦 Buyurtmalarim"),
            ],
            [
                KeyboardButton("🆘 SOS"),
            ],
        ],
        resize_keyboard=True,
    )


# ============================================================
# TELEGRAM MENU
# ============================================================

async def hide_bot_commands(bot):
    """
    Telegram komandalar menyusini bo'shatadi.
    Shuning uchun /start kabi komandalar chap menyuda chiqmaydi.
    """

    try:
        await bot.delete_my_commands(
            scope=BotCommandScopeDefault()
        )
    except Exception as e:
        log.warning("delete commands: %s", e)

    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonDefault()
        )
    except Exception as e:
        log.warning("menu button: %s", e)


# ============================================================
# OWNER FUNCTIONS
# ============================================================

def get_owner_by_master_user(tg_id):
    return q(
        """
        SELECT *
        FROM bot_owners
        WHERE owner_user_id=? AND active=1
        """,
        (tg_id,),
        fetchone=True,
    )


def get_owner_by_token(token):
    return q(
        """
        SELECT *
        FROM bot_owners
        WHERE bot_token=? AND active=1
        """,
        (token,),
        fetchone=True,
    )


def ensure_customer(owner_id, tg_user):
    q(
        """
        INSERT INTO users(
            owner_id,
            telegram_id,
            username,
            first_name,
            created_at
        )
        VALUES(?,?,?,?,?)

        ON CONFLICT(owner_id,telegram_id)
        DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
        """,
        (
            owner_id,
            tg_user.id,
            tg_user.username or "",
            tg_user.first_name or "",
            now(),
        ),
    )

    return q(
        """
        SELECT *
        FROM users
        WHERE owner_id=? AND telegram_id=?
        """,
        (owner_id, tg_user.id),
        fetchone=True,
    )


def owner_api(owner_id):
    return q(
        """
        SELECT *
        FROM api_settings
        WHERE owner_id=?
        """,
        (owner_id,),
        fetchone=True,
    )


# ============================================================
# MASTER START
# ============================================================

async def master_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    own = get_owner_by_master_user(user.id)

    if own:
        await update.message.reply_text(
            f"🤖 @{own['bot_username'] or 'noma’lum'}\n\n"
            "⚙️ Bot egasi paneli",
            reply_markup=owner_panel_kb(),
        )
        return

    await update.message.reply_text(
        "Assalomu alaykum! 👋\n\n"
        "🐷 DONUZ platformasiga xush kelibsiz.\n\n"
        "Bu platforma orqali o‘zingizga shaxsiy Telegram bot "
        "yaratishingiz va xizmatlaringizni boshqarishingiz mumkin.\n\n"
        "🤖 Botni ulang va o‘z panelingizdan boshqaring.",
        reply_markup=master_kb(),
    )


# ============================================================
# CONNECT BOT
# ============================================================

async def connect_bot_start(update, context):
    user_id = update.effective_user.id

    if get_owner_by_master_user(user_id):
        await update.message.reply_text(
            "⚠️ Sizda allaqachon ulangan bot mavjud.\n\n"
            "⚙️ Mening panelim orqali boshqarishingiz mumkin.",
            reply_markup=owner_panel_kb(),
        )
        return

    set_state(
        0,
        user_id,
        "WAIT_BOT_TOKEN"
    )

    await update.message.reply_text(
        "🤖 Bot ulash\n\n"
        "1️⃣ @BotFather orqali bot yarating.\n"
        "2️⃣ BotFather bergan tokenni shu yerga yuboring.\n\n"
        "Masalan:\n"
        "123456789:AAxxxxxxxxxxxxxxxxxxxxxxxx\n\n"
        "⚠️ Tokenni faqat shu yerga yuboring."
    )


async def process_bot_token(update, context, token):
    uid = update.effective_user.id

    token = token.strip()

    if ":" not in token or len(token) < 20:
        await update.message.reply_text(
            "❌ Token formati noto‘g‘ri.\n\n"
            "BotFather bergan tokenni to‘liq yuboring."
        )
        return

    if get_owner_by_token(token):
        clear_state(0, uid)

        await update.message.reply_text(
            "❌ Bu bot allaqachon platformaga ulangan.",
            reply_markup=master_kb(),
        )
        return

    try:
        async with Bot(token=token) as bot:
            me = await bot.get_me()

            if not me or not me.is_bot:
                raise ValueError("Not a bot")

            username = me.username or ""
            name = me.first_name or ""

    except Exception:
        log.exception("Bot token verification failed")

        await update.message.reply_text(
            "❌ Bot tokeni ishlamadi.\n\n"
            "Tokenni @BotFather'dan qayta tekshirib yuboring."
        )
        return

    # Bir ownerga faqat bitta bot
    existing_owner = get_owner_by_master_user(uid)

    if existing_owner:
        clear_state(0, uid)

        await update.message.reply_text(
            "❌ Sizda allaqachon bot ulangan.",
            reply_markup=owner_panel_kb(),
        )
        return

    try:
        owner_id = q(
            """
            INSERT INTO bot_owners(
                owner_user_id,
                bot_token,
                bot_username,
                bot_name,
                active,
                created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                uid,
                token,
                username,
                name,
                1,
                now(),
            ),
        )

    except sqlite3.IntegrityError:
        await update.message.reply_text(
            "❌ Bu bot yoki akkaunt allaqachon ulangan."
        )
        clear_state(0, uid)
        return

    clear_state(0, uid)

    await update.message.reply_text(
        "✅ BOT MUVAFFAQIYATLI Ulandi!\n\n"
        f"🤖 Bot: @{username}\n"
        f"📛 Nomi: {name}\n\n"
        "Endi botingiz ishga tushadi.\n\n"
        "⚙️ Bot egasi panelidan xizmat qo‘shishingiz mumkin.",
        reply_markup=owner_panel_kb(),
    )

    try:
        await start_customer_bot(owner_id, token)
    except Exception:
        log.exception("Could not start newly connected bot")

        await update.message.reply_text(
            "⚠️ Bot bazaga ulandi, lekin ishga tushirishda xatolik bo‘ldi.\n"
            "Server loglarini tekshiring."
        )


# ============================================================
# MASTER TEXT
# ============================================================

async def receive_master_text(update, context):
    user = update.effective_user
    text = (update.message.text or "").strip()

    # Token ulash jarayoni
    state, data = get_state(0, user.id)

    if state == "WAIT_BOT_TOKEN":
        await process_bot_token(update, context, text)
        return

    if text == "🤖 Botimni ulash":
        await connect_bot_start(update, context)
        return

    if text == "⚙️ Mening panelim":
        own = get_owner_by_master_user(user.id)

        if not own:
            await update.message.reply_text(
                "Avval botingizni ulang.",
                reply_markup=master_kb(),
            )
        else:
            await update.message.reply_text(
                "⚙️ Bot egasi paneli",
                reply_markup=owner_panel_kb(),
            )
        return

    if text == "📖 Qo‘llanma":
        await update.message.reply_text(
            "📖 DONUZ qo‘llanmasi\n\n"
            "1️⃣ @BotFather orqali Telegram bot yarating.\n"
            "2️⃣ Bot tokenini DONUZ'ga yuboring.\n"
            "3️⃣ Bot avtomatik ulanadi.\n"
            "4️⃣ ⚙️ Mening panelimga kiring.\n"
            "5️⃣ ➕ Xizmat qo‘shish orqali xizmat yarating.\n"
            "6️⃣ API ulash orqali provayder API'sini ulang.\n"
            "7️⃣ Foydalanuvchilar ulangan bot orqali xizmat sotib oladi.\n\n"
            "🤖 Har bir ulangan botning foydalanuvchilari va "
            "buyurtmalari alohida saqlanadi."
        )
        return

    if text == "🆘 SOS":
        await update.message.reply_text(
            f"🆘 Yordam: {SOS_USERNAME}"
        )
        return

    own = get_owner_by_master_user(user.id)

    if own:
        await owner_panel_text(
            update,
            context,
            own,
            text,
        )
        return

    await update.message.reply_text(
        "Menyudan tanlang.",
        reply_markup=master_kb(),
    )


# ============================================================
# OWNER PANEL
# ============================================================

async def owner_panel_text(update, context, own, text):
    oid = own["id"]
    uid = update.effective_user.id

    state, data = get_state(oid, uid)

    if text == "⬅️ Asosiy menyu":
        clear_state(oid, uid)

        await update.message.reply_text(
            "Asosiy menyu",
            reply_markup=master_kb(),
        )
        return

    if text == "🆘 SOS":
        await update.message.reply_text(
            f"🆘 Yordam: {SOS_USERNAME}"
        )
        return

    if text == "🛒 Xizmatlarim":
        rows = q(
            """
            SELECT *
            FROM services
            WHERE owner_id=?
            ORDER BY id DESC
            """,
            (oid,),
            fetchall=True,
        )

        if not rows:
            await update.message.reply_text(
                "🛒 Hozircha xizmatlar yo‘q."
            )
            return

        result = "🛒 Xizmatlarim\n\n"

        for r in rows:
            if r["delivery_mode"] == "api":
                mode = "🤖 API"
            elif r["delivery_mode"] == "manual":
                mode = "👨‍💼 Admin"
            else:
                mode = "🔁 API + Admin"

            result += (
                f"#{r['id']} — {r['name']}\n"
                f"💰 {r['price']:,.0f} so‘m\n"
                f"{mode}\n\n"
            )

        await update.message.reply_text(result[:4000])
        return

    if text == "➕ Xizmat qo‘shish":
        set_state(
            oid,
            uid,
            "SERVICE_NAME"
        )

        await update.message.reply_text(
            "🛒 Yangi xizmat\n\n"
            "Xizmat nomini yuboring:"
        )
        return

    if text == "📦 Buyurtmalar":
        rows = q(
            """
            SELECT
                o.*,
                s.name,
                u.telegram_id,
                u.username
            FROM orders o
            JOIN services s ON s.id=o.service_id
            JOIN users u ON u.id=o.user_id
            WHERE o.owner_id=?
            ORDER BY o.id DESC
            LIMIT 30
            """,
            (oid,),
            fetchall=True,
        )

        if not rows:
            await update.message.reply_text(
                "📦 Buyurtmalar yo‘q."
            )
            return

        result = "📦 Oxirgi buyurtmalar\n\n"

        for r in rows:
            result += (
                f"#{r['id']} — {r['name']}\n"
                f"👤 @{r['username'] or '-'} / {r['telegram_id']}\n"
                f"🎯 {r['target']}\n"
                f"💰 {r['amount']:,.0f} so‘m\n"
                f"📌 {r['status']}\n\n"
            )

        await update.message.reply_text(result[:4000])
        return

    if text == "👥 Foydalanuvchilar":
        rows = q(
            """
            SELECT *
            FROM users
            WHERE owner_id=?
            ORDER BY id DESC
            LIMIT 50
            """,
            (oid,),
            fetchall=True,
        )

        if not rows:
            await update.message.reply_text(
                "👥 Foydalanuvchilar yo‘q."
            )
            return

        result = "👥 Foydalanuvchilar\n\n"

        for r in rows:
            result += (
                f"ID: {r['telegram_id']}\n"
                f"Username: @{r['username'] or '-'}\n"
                f"Balans: {r['balance']:,.0f} so‘m\n"
                f"Qo‘shilgan: {r['created_at']}\n\n"
            )

        await update.message.reply_text(result[:4000])
        return

    if text == "🔌 API ulash":
        set_state(
            oid,
            uid,
            "API_URL"
        )

        await update.message.reply_text(
            "🔌 API ulash\n\n"
            "API asosiy URL manzilini yuboring.\n\n"
            "Masalan:\n"
            "https://example.com/api/v1"
        )
        return

    if text == "💰 API Balans":
        await api_balance(update, oid)
        return

    if text == "🔄 Katalog yangilash":
        await refresh_catalog(update, oid)
        return

    if text == "⚙️ API sozlamalari":
        a = owner_api(oid)

        if not a:
            await update.message.reply_text(
                "❌ API ulanmagan."
            )
            return

        await update.message.reply_text(
            "⚙️ API sozlamalari\n\n"
            f"🔌 API URL: {a['api_url'] or '-'}\n"
            f"📦 Catalog URL: {a['catalog_url'] or '-'}\n"
            f"💰 Balance URL: {a['balance_url'] or '-'}\n"
            f"📦 Order URL: {a['order_url'] or '-'}\n"
            f"💳 Payment URL: {a['payment_url'] or '-'}"
        )
        return

    if text == "💳 To‘lovlar":
        rows = q(
            """
            SELECT
                p.*,
                u.telegram_id,
                u.username
            FROM payments p
            JOIN users u ON u.id=p.user_id
            WHERE p.owner_id=?
            ORDER BY p.id DESC
            LIMIT 30
            """,
            (oid,),
            fetchall=True,
        )

        if not rows:
            await update.message.reply_text(
                "💳 To‘lovlar yo‘q."
            )
            return

        result = "💳 To‘lovlar\n\n"

        for r in rows:
            result += (
                f"#{r['id']}\n"
                f"👤 @{r['username'] or '-'}\n"
                f"💰 {r['amount']:,.0f} so‘m\n"
                f"📌 {r['status']}\n\n"
            )

        await update.message.reply_text(result[:4000])
        return

    await owner_state_handler(
        update,
        context,
        own,
        text,
        state,
        data,
    )


# ============================================================
# OWNER STATE HANDLER
# ============================================================

async def owner_state_handler(
    update,
    context,
    own,
    text,
    state,
    data
):
    oid = own["id"]
    uid = update.effective_user.id

    # --------------------------------------------------------
    # SERVICE NAME
    # --------------------------------------------------------

    if state == "SERVICE_NAME":
        data["name"] = text

        set_state(
            oid,
            uid,
            "SERVICE_DESC",
            data,
        )

        await update.message.reply_text(
            "Xizmat tavsifini yuboring.\n"
            "Kerak bo‘lmasa '-' yozing."
        )
        return

    # --------------------------------------------------------
    # SERVICE DESCRIPTION
    # --------------------------------------------------------

    if state == "SERVICE_DESC":
        data["description"] = (
            ""
            if text == "-"
            else text
        )

        set_state(
            oid,
            uid,
            "SERVICE_PRICE",
            data,
        )

        await update.message.reply_text(
            "💰 Xizmat narxini so‘mda yuboring.\n\n"
            "Masalan:\n"
            "10000"
        )
        return

    # --------------------------------------------------------
    # SERVICE PRICE
    # --------------------------------------------------------

    if state == "SERVICE_PRICE":
        try:
            price = float(
                text.replace(",", "")
                .replace(" ", "")
            )

            if price < 0:
                raise ValueError

        except Exception:
            await update.message.reply_text(
                "❌ Narx noto‘g‘ri.\n"
                "Masalan: 10000"
            )
            return

        data["price"] = price

        set_state(
            oid,
            uid,
            "SERVICE_MODE",
            data,
        )

        await update.message.reply_text(
            "Yetkazib berish turini tanlang:\n\n"
            "1 — 👨‍💼 Admin orqali\n"
            "2 — 🤖 API orqali\n"
            "3 — 🔁 API + Admin"
        )
        return

    # --------------------------------------------------------
    # SERVICE MODE
    # --------------------------------------------------------

    if state == "SERVICE_MODE":
        modes = {
            "1": "manual",
            "2": "api",
            "3": "both",
        }

        if text not in modes:
            await update.message.reply_text(
                "Faqat 1, 2 yoki 3 yuboring."
            )
            return

        data["mode"] = modes[text]

        set_state(
            oid,
            uid,
            "SERVICE_ACTION",
            data,
        )

        await update.message.reply_text(
            "🔌 API action nomini yuboring.\n\n"
            "Masalan:\n"
            "pubg_mobile\n"
            "telegram_stars\n"
            "premium\n\n"
            "Kerak bo‘lmasa '-' yozing."
        )
        return

    # --------------------------------------------------------
    # SERVICE API ACTION
    # --------------------------------------------------------

    if state == "SERVICE_ACTION":
        data["api_action"] = (
            ""
            if text == "-"
            else text
        )

        q(
            """
            INSERT INTO services(
                owner_id,
                name,
                description,
                price,
                delivery_mode,
                api_action,
                active,
                created_at
            )
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                oid,
                data["name"],
                data["description"],
                data["price"],
                data["mode"],
                data["api_action"],
                1,
                now(),
            ),
        )

        clear_state(oid, uid)

        await update.message.reply_text(
            "✅ Xizmat muvaffaqiyatli qo‘shildi.",
            reply_markup=owner_panel_kb(),
        )
        return

    # --------------------------------------------------------
    # API URL
    # --------------------------------------------------------

    if state == "API_URL":
        data["api_url"] = text.rstrip("/")

        set_state(
            oid,
            uid,
            "API_KEY",
            data,
        )

        await update.message.reply_text(
            "🔑 API Key yuboring:"
        )
        return

    # --------------------------------------------------------
    # API KEY
    # --------------------------------------------------------

    if state == "API_KEY":
        data["api_key"] = text

        set_state(
            oid,
            uid,
            "CATALOG_URL",
            data,
        )

        await update.message.reply_text(
            "📦 Katalog endpointini yuboring.\n\n"
            "Masalan:\n"
            "/catalog\n\n"
            "yoki:\n"
            "https://example.com/api/catalog\n\n"
            "Kerak bo‘lmasa '-'"
        )
        return

    # --------------------------------------------------------
    # CATALOG
    # --------------------------------------------------------

    if state == "CATALOG_URL":
        data["catalog_url"] = (
            ""
            if text == "-"
            else text
        )

        set_state(
            oid,
            uid,
            "BALANCE_URL",
            data,
        )

        await update.message.reply_text(
            "💰 API balans endpointini yuboring.\n"
            "Kerak bo‘lmasa '-'"
        )
        return

    # --------------------------------------------------------
    # BALANCE
    # --------------------------------------------------------

    if state == "BALANCE_URL":
        data["balance_url"] = (
            ""
            if text == "-"
            else text
        )

        set_state(
            oid,
            uid,
            "ORDER_URL",
            data,
        )

        await update.message.reply_text(
            "📦 Buyurtma endpointini yuboring.\n"
            "Kerak bo‘lmasa '-'"
        )
        return

    # --------------------------------------------------------
    # ORDER
    # --------------------------------------------------------

    if state == "ORDER_URL":
        data["order_url"] = (
            ""
            if text == "-"
            else text
        )

        set_state(
            oid,
            uid,
            "PAYMENT_URL",
            data,
        )

        await update.message.reply_text(
            "💳 To‘lov endpointini yuboring.\n"
            "Kerak bo‘lmasa '-'"
        )
        return

    # --------------------------------------------------------
    # PAYMENT
    # --------------------------------------------------------

    if state == "PAYMENT_URL":
        data["payment_url"] = (
            ""
            if text == "-"
            else text
        )

        q(
            """
            INSERT INTO api_settings(
                owner_id,
                api_url,
                api_key,
                catalog_url,
                balance_url,
                order_url,
                payment_url,
                updated_at
            )
            VALUES(?,?,?,?,?,?,?,?)

            ON CONFLICT(owner_id)
            DO UPDATE SET
                api_url=excluded.api_url,
                api_key=excluded.api_key,
                catalog_url=excluded.catalog_url,
                balance_url=excluded.balance_url,
                order_url=excluded.order_url,
                payment_url=excluded.payment_url,
                updated_at=excluded.updated_at
            """,
            (
                oid,
                data["api_url"],
                data["api_key"],
                data["catalog_url"],
                data["balance_url"],
                data["order_url"],
                data["payment_url"],
                now(),
            ),
        )

        clear_state(oid, uid)

        await update.message.reply_text(
            "✅ API sozlamalari saqlandi.",
            reply_markup=owner_panel_kb(),
        )
        return

    await update.message.reply_text(
        "Menyudan tanlang.",
        reply_markup=owner_panel_kb(),
    )


# ============================================================
# API HELPERS
# ============================================================

def endpoint(base, value):
    if not value:
        return ""

    if value.startswith("http://"):
        return value

    if value.startswith("https://"):
        return value

    return (
        base.rstrip("/")
        + "/"
        + value.lstrip("/")
    )


def api_headers(a):
    return {
        "Authorization": f"Bearer {a['api_key']}",
        "X-API-Key": a["api_key"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# ============================================================
# API BALANCE
# ============================================================

async def api_balance(update, owner_id):
    a = owner_api(owner_id)

    if not a or not a["balance_url"]:
        await update.message.reply_text(
            "❌ API balans endpointi sozlanmagan."
        )
        return

    try:
        url = endpoint(
            a["api_url"],
            a["balance_url"],
        )

        r = await asyncio.to_thread(
            requests.get,
            url,
            headers=api_headers(a),
            timeout=20,
        )

        await update.message.reply_text(
            f"💰 API balans javobi:\n\n"
            f"{r.text[:3000]}"
        )

    except Exception as e:
        log.exception("API balance error")

        await update.message.reply_text(
            "❌ API balansini olishda xatolik."
        )


# ============================================================
# CATALOG
# ============================================================

async def refresh_catalog(update, owner_id):
    a = owner_api(owner_id)

    if not a or not a["catalog_url"]:
        await update.message.reply_text(
            "❌ Katalog endpointi sozlanmagan."
        )
        return

    try:
        url = endpoint(
            a["api_url"],
            a["catalog_url"],
        )

        r = await asyncio.to_thread(
            requests.get,
            url,
            headers=api_headers(a),
            timeout=30,
        )

        if r.status_code >= 400:
            await update.message.reply_text(
                f"❌ API HTTP {r.status_code}\n\n"
                f"{r.text[:2000]}"
            )
            return

        await update.message.reply_text(
            "✅ Katalog API'dan olindi.\n\n"
            "⚠️ Universal platforma bo‘lgani uchun "
            "provayder JSON formati avtomatik ravishda "
            "DONUZ xizmatlariga aylantirilmaydi.\n\n"
            f"{r.text[:2500]}"
        )

    except Exception:
        log.exception("Catalog error")

        await update.message.reply_text(
            "❌ Katalogni yangilashda xatolik."
        )


# ============================================================
# API ORDER
# ============================================================

async def api_order(owner_id, order_id):
    a = owner_api(owner_id)

    if not a or not a["order_url"]:
        return False, "API order endpointi sozlanmagan."

    order = q(
        """
        SELECT
            o.*,
            s.name,
            s.api_action
        FROM orders o
        JOIN services s ON s.id=o.service_id
        WHERE o.id=?
        """,
        (order_id,),
        fetchone=True,
    )

    if not order:
        return False, "Buyurtma topilmadi."

    url = endpoint(
        a["api_url"],
        a["order_url"],
    )

    payload = {
        "order_id": order["id"],
        "service": order["name"],
        "action": order["api_action"] or "",
        "target": order["target"],
        "quantity": order["quantity"],
        "amount": order["amount"],
    }

    try:
        r = await asyncio.to_thread(
            requests.post,
            url,
            headers=api_headers(a),
            json=payload,
            timeout=30,
        )

        body = r.text[:5000]

        if 200 <= r.status_code < 300:
            q(
                """
                UPDATE orders
                SET status='processing',
                    provider_response=?
                WHERE id=?
                """,
                (body, order_id),
            )

            return True, body

        q(
            """
            UPDATE orders
            SET status='api_error',
                provider_response=?
            WHERE id=?
            """,
            (body, order_id),
        )

        return False, body

    except Exception as e:
        q(
            """
            UPDATE orders
            SET status='api_error',
                provider_response=?
            WHERE id=?
            """,
            (str(e), order_id),
        )

        return False, str(e)


# ============================================================
# CUSTOMER BOT
# ============================================================

async def customer_start(update, context):
    own = context.application.bot_data["owner"]

    user = ensure_customer(
        own["id"],
        update.effective_user,
    )

    await update.message.reply_text(
        f"Assalomu alaykum, "
        f"{update.effective_user.first_name}! 👋\n\n"
        "Xizmatlardan foydalanish uchun "
        "menyudan tanlang.",
        reply_markup=customer_kb(),
    )


async def customer_text(update, context):
    own = context.application.bot_data["owner"]
    oid = own["id"]

    tg = update.effective_user

    user = ensure_customer(
        oid,
        tg,
    )

    text = (update.message.text or "").strip()

    # --------------------------------------------------------
    # SERVICES
    # --------------------------------------------------------

    if text == "🛒 Xizmatlar":
        rows = q(
            """
            SELECT *
            FROM services
            WHERE owner_id=? AND active=1
            ORDER BY id
            """,
            (oid,),
            fetchall=True,
        )

        if not rows:
            await update.message.reply_text(
                "🛒 Hozircha xizmatlar mavjud emas."
            )
            return

        buttons = []

        for r in rows:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"🛒 {r['name']} — "
                        f"{r['price']:,.0f} so‘m",
                        callback_data=f"svc:{r['id']}",
                    )
                ]
            )

        await update.message.reply_text(
            "🛒 Xizmatlar:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # --------------------------------------------------------
    # BALANCE
    # --------------------------------------------------------

    if text == "💰 Balans":
        await update.message.reply_text(
            f"💰 Balansingiz:\n\n"
            f"{user['balance']:,.0f} so‘m"
        )
        return

    # --------------------------------------------------------
    # TOP UP
    # --------------------------------------------------------

    if text == "➕ Balans to‘ldirish":
        await update.message.reply_text(
            "💳 Balans to‘ldirish turi:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "👨‍💼 Qo‘lda qabul qilish",
                            callback_data="pay:manual",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🤖 API orqali qabul qilish",
                            callback_data="pay:api",
                        )
                    ],
                ]
            ),
        )
        return

    # --------------------------------------------------------
    # ORDERS
    # --------------------------------------------------------

    if text == "📦 Buyurtmalarim":
        rows = q(
            """
            SELECT
                o.*,
                s.name
            FROM orders o
            JOIN services s ON s.id=o.service_id
            WHERE o.owner_id=? AND o.user_id=?
            ORDER BY o.id DESC
            LIMIT 20
            """,
            (
                oid,
                user["id"],
            ),
            fetchall=True,
        )

        if not rows:
            await update.message.reply_text(
                "📦 Sizda buyurtmalar yo‘q."
            )
            return

        result = "📦 Buyurtmalarim\n\n"

        for r in rows:
            result += (
                f"#{r['id']} — {r['name']}\n"
                f"🎯 {r['target']}\n"
                f"💰 {r['amount']:,.0f} so‘m\n"
                f"📌 {r['status']}\n\n"
            )

        await update.message.reply_text(
            result[:4000]
        )
        return

    # --------------------------------------------------------
    # SOS
    # --------------------------------------------------------

    if text == "🆘 SOS":
        await update.message.reply_text(
            f"🆘 Yordam: {SOS_USERNAME}"
        )
        return

    # --------------------------------------------------------
    # STATE
    # --------------------------------------------------------

    state, data = get_state(
        oid,
        tg.id,
    )

    # --------------------------------------------------------
    # ORDER TARGET
    # --------------------------------------------------------

    if state == "ORDER_TARGET":

        service = q(
            """
            SELECT *
            FROM services
            WHERE id=? AND owner_id=? AND active=1
            """,
            (
                data["service_id"],
                oid,
            ),
            fetchone=True,
        )

        if not service:
            clear_state(oid, tg.id)

            await update.message.reply_text(
                "❌ Xizmat topilmadi."
            )
            return

        amount = float(service["price"])

        if float(user["balance"]) < amount:
            clear_state(oid, tg.id)

            await update.message.reply_text(
                "❌ Balans yetarli emas.\n\n"
                f"Kerak: {amount:,.0f} so‘m\n"
                f"Balans: {user['balance']:,.0f} so‘m"
            )
            return

        # Balansni yechish
        q(
            """
            UPDATE users
            SET balance=balance-?
            WHERE id=?
            """,
            (
                amount,
                user["id"],
            ),
        )

        order_id = q(
            """
            INSERT INTO orders(
                owner_id,
                user_id,
                service_id,
                quantity,
                target,
                amount,
                delivery_mode,
                status,
                created_at
            )
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                oid,
                user["id"],
                service["id"],
                1,
                text,
                amount,
                service["delivery_mode"],
                "pending",
                now(),
            ),
        )

        clear_state(oid, tg.id)

        # API
        if service["delivery_mode"] in ("api", "both"):

            ok, response = await api_order(
                oid,
                order_id,
            )

            if ok:
                await update.message.reply_text(
                    f"✅ Buyurtma qabul qilindi!\n\n"
                    f"📦 Buyurtma: #{order_id}\n"
                    "🤖 API orqali qayta ishlanmoqda."
                )

                if service["delivery_mode"] == "both":
                    await notify_owner(
                        oid,
                        order_id,
                        "API ishladi. Nazorat uchun ham ko‘rsatildi.",
                    )

            else:

                if service["delivery_mode"] == "api":

                    q(
                        """
                        UPDATE users
                        SET balance=balance+?
                        WHERE id=?
                        """,
                        (
                            amount,
                            user["id"],
                        ),
                    )

                    q(
                        """
                        UPDATE orders
                        SET status='cancelled'
                        WHERE id=?
                        """,
                        (order_id,),
                    )

                    await update.message.reply_text(
                        "❌ API buyurtmani qabul qilmadi.\n\n"
                        "💰 Pul balansingizga qaytarildi."
                    )

                else:

                    await notify_owner(
                        oid,
                        order_id,
                        "API ishlamadi. Admin orqali bajarish kerak.",
                    )

                    await update.message.reply_text(
                        f"✅ Buyurtma #{order_id} qabul qilindi.\n"
                        "👨‍💼 Admin orqali bajariladi."
                    )

        # Manual
        else:

            await notify_owner(
                oid,
                order_id,
                "Yangi qo‘lda buyurtma.",
            )

            await update.message.reply_text(
                f"✅ Buyurtma #{order_id} qabul qilindi.\n"
                "👨‍💼 Admin orqali yetkazib beriladi."
            )

        return

    await update.message.reply_text(
        "Menyudan tanlang.",
        reply_markup=customer_kb(),
    )


# ============================================================
# CUSTOMER CALLBACK
# ============================================================

async def customer_callback(update, context):
    query = update.callback_query

    await query.answer()

    own = context.application.bot_data["owner"]

    oid = own["id"]

    tg = query.from_user

    ensure_customer(
        oid,
        tg,
    )

    data = query.data

    # --------------------------------------------------------
    # SERVICE
    # --------------------------------------------------------

    if data.startswith("svc:"):

        try:
            sid = int(data.split(":")[1])
        except Exception:
            await query.edit_message_text(
                "❌ Xizmat noto‘g‘ri."
            )
            return

        service = q(
            """
            SELECT *
            FROM services
            WHERE id=?
            AND owner_id=?
            AND active=1
            """,
            (
                sid,
                oid,
            ),
            fetchone=True,
        )

        if not service:
            await query.edit_message_text(
                "❌ Xizmat topilmadi."
            )
            return

        await query.edit_message_text(
            f"🛒 {service['name']}\n\n"
            f"{service['description'] or 'Xizmat'}\n\n"
            f"💰 Narx: "
            f"{service['price']:,.0f} so‘m",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🛒 Buyurtma berish",
                            callback_data=f"buy:{sid}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ Orqaga",
                            callback_data="back:services",
                        )
                    ],
                ]
            ),
        )
        return

    # --------------------------------------------------------
    # BUY
    # --------------------------------------------------------

    if data.startswith("buy:"):

        try:
            sid = int(data.split(":")[1])
        except Exception:
            await query.message.reply_text(
                "❌ Xizmat noto‘g‘ri."
            )
            return

        service = q(
            """
            SELECT *
            FROM services
            WHERE id=?
            AND owner_id=?
            AND active=1
            """,
            (
                sid,
                oid,
            ),
            fetchone=True,
        )

        if not service:
            await query.message.reply_text(
                "❌ Xizmat topilmadi."
            )
            return

        set_state(
            oid,
            tg.id,
            "ORDER_TARGET",
            {
                "service_id": sid
            },
        )

        await query.message.reply_text(
            f"🎯 {service['name']}\n\n"
            "Buyurtma uchun kerakli ID / username / "
            "ma’lumotni yuboring:"
        )
        return

    # --------------------------------------------------------
    # BACK
    # --------------------------------------------------------

    if data == "back:services":

        rows = q(
            """
            SELECT *
            FROM services
            WHERE owner_id=? AND active=1
            ORDER BY id
            """,
            (oid,),
            fetchall=True,
        )

        if not rows:
            await query.edit_message_text(
                "Hozircha xizmatlar mavjud emas."
            )
            return

        buttons = [
            [
                InlineKeyboardButton(
                    f"🛒 {r['name']} — "
                    f"{r['price']:,.0f}",
                    callback_data=f"svc:{r['id']}",
                )
            ]
            for r in rows
        ]

        await query.edit_message_text(
            "🛒 Xizmatlar:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # --------------------------------------------------------
    # MANUAL PAYMENT
    # --------------------------------------------------------

    if data == "pay:manual":

        set_state(
            oid,
            tg.id,
            "PAY_AMOUNT_MANUAL",
        )

        await query.message.reply_text(
            "💳 To‘ldirmoqchi bo‘lgan summani yuboring.\n\n"
            "Masalan:\n"
            "50000"
        )
        return

    # --------------------------------------------------------
    # API PAYMENT
    # --------------------------------------------------------

    if data == "pay:api":

        a = owner_api(oid)

        if not a or not a["payment_url"]:
            await query.message.reply_text(
                "❌ Avtomatik to‘lov API'si sozlanmagan.\n\n"
                "👨‍💼 Qo‘lda to‘lov usulidan foydalaning."
            )
            return

        set_state(
            oid,
            tg.id,
            "PAY_AMOUNT_API",
        )

        await query.message.reply_text(
            "💳 Summani yuboring.\n\n"
            "Masalan:\n"
            "50000"
        )
        return


# ============================================================
# PAYMENTS
# ============================================================

async def payment_text(update, context):
    own = context.application.bot_data["owner"]

    oid = own["id"]

    uid = update.effective_user.id

    state, data = get_state(
        oid,
        uid,
    )

    if state not in (
        "PAY_AMOUNT_MANUAL",
        "PAY_AMOUNT_API",
    ):
        return False

    try:
        amount = float(
            update.message.text
            .replace(" ", "")
            .replace(",", "")
        )

        if amount <= 0:
            raise ValueError

    except Exception:
        await update.message.reply_text(
            "❌ Summani to‘g‘ri kiriting."
        )
        return True

    user = ensure_customer(
        oid,
        update.effective_user,
    )

    method = (
        "manual"
        if state == "PAY_AMOUNT_MANUAL"
        else "api"
    )

    payment_id = q(
        """
        INSERT INTO payments(
            owner_id,
            user_id,
            amount,
            method,
            status,
            created_at
        )
        VALUES(?,?,?,?,?,?)
        """,
        (
            oid,
            user["id"],
            amount,
            method,
            "pending",
            now(),
        ),
    )

    clear_state(oid, uid)

    if method == "manual":

        await notify_owner_payment(
            oid,
            payment_id,
        )

        await update.message.reply_text(
            f"✅ To‘lov so‘rovi #{payment_id} yaratildi.\n\n"
            "👨‍💼 Admin tasdiqlagandan keyin "
            "balansingiz to‘ldiriladi."
        )

    else:

        a = owner_api(oid)

        try:
            url = endpoint(
                a["api_url"],
                a["payment_url"],
            )

            payload = {
                "payment_id": payment_id,
                "user_id": uid,
                "amount": amount,
            }

            r = await asyncio.to_thread(
                requests.post,
                url,
                headers=api_headers(a),
                json=payload,
                timeout=30,
            )

            q(
                """
                UPDATE payments
                SET receipt=?
                WHERE id=?
                """,
                (
                    r.text[:5000],
                    payment_id,
                ),
            )

            await update.message.reply_text(
                f"🤖 Avtomatik to‘lov yaratildi.\n\n"
                f"#{payment_id}\n\n"
                f"{r.text[:1500]}"
            )

        except Exception as e:

            q(
                """
                UPDATE payments
                SET status='failed',
                    receipt=?
                WHERE id=?
                """,
                (
                    str(e),
                    payment_id,
                ),
            )

            log.exception("Payment API error")

            await update.message.reply_text(
                "❌ To‘lov API'sida xatolik."
            )

    return True


# ============================================================
# OWNER NOTIFICATIONS
# ============================================================

async def notify_owner(owner_id, order_id, extra=""):
    own = q(
        """
        SELECT *
        FROM bot_owners
        WHERE id=?
        """,
        (owner_id,),
        fetchone=True,
    )

    if not own or not master_app:
        return

    try:
        await master_app.bot.send_message(
            chat_id=own["owner_user_id"],
            text=(
                f"📦 Yangi buyurtma #{order_id}\n\n"
                f"{extra}\n\n"
                "⚙️ Bot egasi panelidagi "
                "📦 Buyurtmalar bo‘limidan ko‘ring."
            ),
        )

    except Exception as e:
        log.warning(
            "Owner notification error: %s",
            e,
        )


async def notify_owner_payment(owner_id, payment_id):
    own = q(
        """
        SELECT *
        FROM bot_owners
        WHERE id=?
        """,
        (owner_id,),
        fetchone=True,
    )

    if not own or not master_app:
        return

    payment = q(
        """
        SELECT
            p.*,
            u.telegram_id,
            u.username
        FROM payments p
        JOIN users u ON u.id=p.user_id
        WHERE p.id=?
        """,
        (payment_id,),
        fetchone=True,
    )

    if not payment:
        return

    try:
        await master_app.bot.send_message(
            chat_id=own["owner_user_id"],
            text=(
                f"💳 Yangi balans to‘ldirish "
                f"so‘rovi #{payment_id}\n\n"
                f"👤 @{payment['username'] or '-'}\n"
                f"🆔 {payment['telegram_id']}\n"
                f"💰 {payment['amount']:,.0f} so‘m\n\n"
                "⚙️ To‘lovlar bo‘limidan ko‘ring."
            ),
        )

    except Exception as e:
        log.warning(
            "Payment notification error: %s",
            e,
        )


# ============================================================
# CUSTOMER BOT START
# ============================================================

async def start_customer_bot(owner_id, token):
    if owner_id in running_bots:
        log.info(
            "Bot already running: %s",
            owner_id,
        )
        return

    own = q(
        """
        SELECT *
        FROM bot_owners
        WHERE id=? AND active=1
        """,
        (owner_id,),
        fetchone=True,
    )

    if not own:
        return

    try:
        app = (
            Application.builder()
            .token(token)
            .build()
        )

        app.bot_data["owner"] = dict(own)

        # Bot komandalarini olib tashlash
        try:
            await hide_bot_commands(app.bot)
        except Exception:
            pass

        # Payment state prioritetda
        async def wrapped_text(update, context):
            try:
                handled = await payment_text(
                    update,
                    context,
                )

                if handled:
                    return

                await customer_text(
                    update,
                    context,
                )

            except Exception:
                log.exception(
                    "Customer text handler error"
                )

                try:
                    await update.message.reply_text(
                        "❌ Xatolik yuz berdi. "
                        "Qayta urinib ko‘ring."
                    )
                except Exception:
                    pass

        app.add_handler(
            CommandHandler(
                "start",
                customer_start,
            )
        )

        app.add_handler(
            CallbackQueryHandler(
                customer_callback
            )
        )

        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                wrapped_text,
            )
        )

        await app.initialize()

        # Telegram tokenini yana bir bor tekshirish
        me = await app.bot.get_me()

        if not me or not me.is_bot:
            raise RuntimeError(
                "Bot token verification failed"
            )

        await app.start()

        await app.updater.start_polling(
            drop_pending_updates=True
        )

        running_bots[owner_id] = app

        log.info(
            "Customer bot started: @%s",
            me.username,
        )

    except Exception:
        log.exception(
            "Could not start customer bot %s",
            owner_id,
        )

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

        raise


# ============================================================
# START ALL SAVED BOTS
# ============================================================

async def start_all_customer_bots():
    rows = q(
        """
        SELECT *
        FROM bot_owners
        WHERE active=1
        ORDER BY id
        """,
        fetchall=True,
    )

    for row in rows:

        try:
            await start_customer_bot(
                row["id"],
                row["bot_token"],
            )

        except Exception as e:
            log.error(
                "Saved bot #%s could not start: %s",
                row["id"],
                e,
            )


# ============================================================
# POST INIT
# ============================================================

async def post_init(app):
    global master_app

    master_app = app

    # Master bot command menyusini tozalash
    try:
        await hide_bot_commands(
            app.bot
        )
    except Exception:
        pass

    await start_all_customer_bots()


# ============================================================
# POST SHUTDOWN
# ============================================================

async def post_shutdown(app):
    for owner_id, child_app in list(
        running_bots.items()
    ):

        try:
            if child_app.updater:
                await child_app.updater.stop()
        except Exception:
            pass

        try:
            await child_app.stop()
        except Exception:
            pass

        try:
            await child_app.shutdown()
        except Exception:
            pass

    running_bots.clear()


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN .env/environment variable is missing."
        )

    if ADMIN_ID == 0:
        raise RuntimeError(
            "ADMIN_ID .env/environment variable is missing."
        )

    init_db()

    global master_app

    master_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    master_app.add_handler(
        CommandHandler(
            "start",
            master_start,
        )
    )

    master_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_master_text,
        )
    )

    log.info(
        "DONUZ multi-bot platform started."
    )

    master_app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()

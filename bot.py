import os
import asyncio
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)


# =====================================
# 1. ENVIRONMENT SOZLAMALARI
# =====================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
SHOP_ID = os.getenv("SHOP_ID")
SHOP_KEY = os.getenv("SHOP_KEY")

CARD_NUMBER = os.getenv("CARD_NUMBER")
CARD_OWNER = os.getenv("CARD_OWNER")
CARD_LAST4 = os.getenv("CARD_LAST4")

API_URL = "https://dcn268.wineclo.com/avtotolov/chek.php"

MIN_AMOUNT = 3000
WAIT_SECONDS = 300
CHECK_INTERVAL = 10

PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "payments.db")

required = {
    "BOT_TOKEN": BOT_TOKEN,
    "SHOP_ID": SHOP_ID,
    "SHOP_KEY": SHOP_KEY,
    "CARD_NUMBER": CARD_NUMBER,
    "CARD_OWNER": CARD_OWNER,
    "CARD_LAST4": CARD_LAST4,
}

missing = [key for key, value in required.items() if not value]

if missing:
    raise RuntimeError(
        "Environment sozlamalari yetishmayapti: "
        + ", ".join(missing)
    )

if not CARD_LAST4.isdigit() or len(CARD_LAST4) != 4:
    raise RuntimeError("CARD_LAST4 4 ta raqamdan iborat bo'lishi kerak")


# =====================================
# 2. LOGGING
# =====================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

log = logging.getLogger("AUTO_PAYMENT")


# =====================================
# 3. DATABASE
# =====================================

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

db = sqlite3.connect(
    DB_PATH,
    check_same_thread=False,
    timeout=30
)

db.row_factory = sqlite3.Row

db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=30000")

db.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL UNIQUE,
    original_amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
)
""")

db.commit()


def register_user(user_id):
    db.execute(
        "INSERT OR IGNORE INTO users(user_id) VALUES(?)",
        (user_id,)
    )
    db.commit()


def get_balance(user_id):
    row = db.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    return row["balance"] if row else 0


def create_payment(user_id, original_amount):
    register_user(user_id)

    final_amount = original_amount

    # Summani boshqa to'lovlar bilan takrorlamaslik
    while db.execute(
        "SELECT id FROM payments WHERE amount=?",
        (final_amount,)
    ).fetchone():
        final_amount += 1

    cursor = db.execute("""
        INSERT INTO payments(
            user_id,
            amount,
            original_amount,
            status,
            created_at
        )
        VALUES (?, ?, ?, 'pending', ?)
    """, (
        user_id,
        final_amount,
        original_amount,
        datetime.now().isoformat()
    ))

    db.commit()

    return cursor.lastrowid, final_amount


def get_payment(payment_id):
    return db.execute(
        "SELECT * FROM payments WHERE id=?",
        (payment_id,)
    ).fetchone()


def cancel_payment(payment_id, user_id):
    cursor = db.execute("""
        UPDATE payments
        SET status='cancelled'
        WHERE id=? AND user_id=? AND status='pending'
    """, (payment_id, user_id))

    db.commit()
    return cursor.rowcount > 0


def confirm_payment(payment_id):
    """
    To'lovni faqat bir marta tasdiqlaydi.
    Balans va payment status bitta tranzaksiyada yangilanadi.
    """

    db.execute("BEGIN IMMEDIATE")

    try:
        row = db.execute("""
            SELECT user_id, original_amount
            FROM payments
            WHERE id=? AND status='pending'
        """, (payment_id,)).fetchone()

        if not row:
            db.rollback()
            return None

        db.execute("""
            UPDATE users
            SET balance = balance + ?
            WHERE user_id=?
        """, (
            row["original_amount"],
            row["user_id"]
        ))

        db.execute("""
            UPDATE payments
            SET status='success'
            WHERE id=? AND status='pending'
        """, (payment_id,))

        db.commit()

        return row["user_id"], row["original_amount"]

    except Exception:
        db.rollback()
        raise


# =====================================
# 4. BOT VA FSM
# =====================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class TopupState(StatesGroup):
    waiting_amount = State()


def main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💰 Balansim",
                    callback_data="my_balance"
                )
            ],
            [
                InlineKeyboardButton(
                    text="➕ Balans to'ldirish",
                    callback_data="topup"
                )
            ]
        ]
    )


# =====================================
# 5. START
# =====================================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()

    register_user(message.from_user.id)

    await message.answer(
        "👋 Xush kelibsiz!\n\n"
        "💳 Avtomatik to'lov botiga xush kelibsiz.",
        reply_markup=main_keyboard()
    )


# =====================================
# 6. BALANS
# =====================================

@dp.callback_query(F.data == "my_balance")
async def my_balance(call: CallbackQuery):
    balance = get_balance(call.from_user.id)

    await call.message.edit_text(
        f"💰 Balansingiz: {balance:,} so'm",
        reply_markup=main_keyboard()
    )

    await call.answer()


# =====================================
# 7. BALANS TO'LDIRISH
# =====================================

@dp.callback_query(F.data == "topup")
async def topup(call: CallbackQuery, state: FSMContext):
    await state.set_state(TopupState.waiting_amount)

    await call.message.edit_text(
        "💵 Balansni qancha summaga to'ldirmoqchisiz?\n\n"
        f"📌 Minimal summa: {MIN_AMOUNT:,} so'm\n\n"
        "Summani raqam bilan yuboring."
    )

    await call.answer()


@dp.message(TopupState.waiting_amount)
async def get_amount(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer(
            "❌ Summani faqat raqam bilan kiriting."
        )
        return

    original_amount = int(message.text)

    if original_amount < MIN_AMOUNT:
        await message.answer(
            f"❌ Minimal summa {MIN_AMOUNT:,} so'm."
        )
        return

    payment_id, final_amount = create_payment(
        message.from_user.id,
        original_amount
    )

    await state.clear()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ Bekor qilish",
                    callback_data=f"cancel:{payment_id}"
                )
            ]
        ]
    )

    await message.answer(
        "📋 TO'LOV MA'LUMOTLARI\n\n"
        f"💵 To'lov summasi: {final_amount:,} so'm\n"
        f"💳 Karta: {CARD_NUMBER}\n"
        f"👤 Karta egasi: {CARD_OWNER}\n\n"
        f"⚠️ Aynan {final_amount} so'm yuboring.\n"
        "⏳ To'lovni kutish: 5 daqiqa.\n\n"
        "🔄 To'lov avtomatik tekshiriladi.",
        reply_markup=keyboard
    )

    # Tekshiruvni alohida vazifada ishga tushirish
    asyncio.create_task(
        check_payment(
            user_id=message.from_user.id,
            payment_id=payment_id,
            final_amount=final_amount
        )
    )


# =====================================
# 8. API ORQALI AVTOMATIK TEKSHIRISH
# =====================================

async def check_payment(user_id, payment_id, final_amount):
    started = asyncio.get_running_loop().time()

    timeout = aiohttp.ClientTimeout(total=10)

    log.info(
        "Payment check started: ID=%s amount=%s",
        payment_id,
        final_amount
    )

    try:
        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            while (
                asyncio.get_running_loop().time() - started
                < WAIT_SECONDS
            ):

                row = get_payment(payment_id)

                if not row or row["status"] != "pending":
                    return

                payload = {
                    "amount": str(final_amount),
                    "shop_id": str(SHOP_ID),
                    "shop_key": str(SHOP_KEY),
                    "card": str(CARD_LAST4)
                }

                try:
                    async with session.post(
                        API_URL,
                        data=payload
                    ) as response:

                        response_text = await response.text()

                        if response.status == 200:
                            try:
                                result = await response.json(
                                    content_type=None
                                )
                            except Exception:
                                log.error(
                                    "API JSON javobi noto'g'ri: %s",
                                    response_text[:500]
                                )
                                result = {}

                            log.info(
                                "API response payment=%s: %s",
                                payment_id,
                                result
                            )

                            if result.get("status") == "success":

                                confirmed = confirm_payment(payment_id)

                                if confirmed:
                                    paid_user, credited = confirmed

                                    log.info(
                                        "Payment successful: %s",
                                        payment_id
                                    )

                                    await bot.send_message(
                                        paid_user,
                                        "✅ TO'LOV MUVAFFAQIYATLI!\n\n"
                                        f"💰 Balansga qo'shildi: "
                                        f"{credited:,} so'm\n"
                                        f"🆔 To'lov ID: {payment_id}\n\n"
                                        f"💳 Joriy balans: "
                                        f"{get_balance(paid_user):,} so'm",
                                        reply_markup=main_keyboard()
                                    )

                                return

                        else:
                            log.warning(
                                "API HTTP status: %s",
                                response.status
                            )

                except asyncio.CancelledError:
                    raise

                except Exception:
                    log.exception(
                        "API tekshirishda xatolik: payment=%s",
                        payment_id
                    )

                await asyncio.sleep(CHECK_INTERVAL)

        # 5 daqiqa o'tgach hali to'lanmagan bo'lsa
        row = get_payment(payment_id)

        if row and row["status"] == "pending":
            db.execute("""
                UPDATE payments
                SET status='expired'
                WHERE id=? AND status='pending'
            """, (payment_id,))
            db.commit()

            await bot.send_message(
                user_id,
                "⏳ To'lov vaqti tugadi.\n\n"
                "Balansga pul qo'shilmadi.\n"
                "Qaytadan to'lov yaratishingiz mumkin.",
                reply_markup=main_keyboard()
            )

    except asyncio.CancelledError:
        raise

    except Exception:
        log.exception("Payment task xatoligi")


# =====================================
# 9. TO'LOVNI BEKOR QILISH
# =====================================

@dp.callback_query(F.data.startswith("cancel:"))
async def cancel(call: CallbackQuery):
    try:
        payment_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer(
            "Noto'g'ri so'rov.",
            show_alert=True
        )
        return

    success = cancel_payment(
        payment_id,
        call.from_user.id
    )

    if success:
        await call.message.edit_text(
            "❌ To'lov bekor qilindi.",
            reply_markup=main_keyboard()
        )
        await call.answer("Bekor qilindi.")
    else:
        await call.answer(
            "To'lov topilmadi yoki allaqachon yakunlangan.",
            show_alert=True
        )


# =====================================
# 10. RENDER WEB SERVICE
# =====================================

async def health(request):
    return web.json_response({
        "status": "ok",
        "bot": "running",
        "service": "auto-payment"
    })


async def root(request):
    return web.json_response({
        "message": "Automatic payment bot is running",
        "status": "online"
    })


async def main():
    # Web server
    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    log.info("WEB SERVER STARTED ON PORT %s", PORT)

    try:
        # Eski webhookni o'chirib polling boshlash
        await bot.delete_webhook(drop_pending_updates=True)

        log.info("TELEGRAM BOT STARTING")

        await dp.start_polling(
            bot,
            handle_signals=False
        )

    finally:
        await runner.cleanup()
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())

import os
import asyncio
import logging
import sqlite3
from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage


# =============== SOZLAMALAR ===============

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

if not all([
    BOT_TOKEN, SHOP_ID, SHOP_KEY,
    CARD_NUMBER, CARD_OWNER, CARD_LAST4
]):
    raise RuntimeError("Render Environment sozlamalari to'liq emas!")


# =============== BAZA ===============

db = sqlite3.connect("payments.db", check_same_thread=False)
db.row_factory = sqlite3.Row

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
    shop_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    original_amount INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(amount)
)
""")

db.commit()


def get_balance(user_id):
    row = db.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()
    return row["balance"] if row else 0


def create_payment(user_id, original_amount):
    db.execute(
        "INSERT OR IGNORE INTO users(user_id) VALUES(?)",
        (user_id,)
    )

    # Har bir faol yoki avval muvaffaqiyatli bo'lgan
    # to'lov uchun noyob summa tanlanadi.
    final_amount = original_amount

    while db.execute(
        "SELECT 1 FROM payments WHERE amount=?",
        (final_amount,)
    ).fetchone():
        final_amount += 1

    cur = db.execute("""
        INSERT INTO payments(
            user_id, shop_id, amount,
            original_amount, status, created_at
        )
        VALUES (?, ?, ?, ?, 'pending', ?)
    """, (
        user_id,
        SHOP_ID,
        final_amount,
        original_amount,
        datetime.now().isoformat()
    ))

    db.commit()
    return cur.lastrowid, final_amount


def payment_status(payment_id):
    row = db.execute(
        "SELECT status FROM payments WHERE id=?",
        (payment_id,)
    ).fetchone()
    return row["status"] if row else None


def cancel_payment(payment_id):
    db.execute("""
        UPDATE payments SET status='cancelled'
        WHERE id=? AND status='pending'
    """, (payment_id,))
    db.commit()


def confirm_payment(payment_id):
    # Bir tranzaksiyada statusni o'zgartiramiz
    # va balansga pul qo'shamiz.
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
            INSERT OR IGNORE INTO users(user_id)
            VALUES(?)
        """, (row["user_id"],))

        db.execute("""
            UPDATE users
            SET balance = balance + ?
            WHERE user_id=?
        """, (row["original_amount"], row["user_id"]))

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


# =============== BOT ===============

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class TopupState(StatesGroup):
    waiting_amount = State()


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💰 Balansim",
            callback_data="my_balance"
        )],
        [InlineKeyboardButton(
            text="➕ Balans to'ldirish",
            callback_data="topup"
        )]
    ])


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()

    db.execute(
        "INSERT OR IGNORE INTO users(user_id) VALUES(?)",
        (message.from_user.id,)
    )
    db.commit()

    await message.answer(
        "👋 Xush kelibsiz!\n\n"
        "💳 Bu bot orqali balansingizni to'ldirishingiz mumkin.",
        reply_markup=main_keyboard()
    )


@dp.callback_query(F.data == "my_balance")
async def my_balance(call: CallbackQuery):
    balance = get_balance(call.from_user.id)

    await call.message.edit_text(
        f"💰 Sizning balansingiz: {balance:,} so'm",
        reply_markup=main_keyboard()
    )
    await call.answer()


@dp.callback_query(F.data == "topup")
async def topup(call: CallbackQuery, state: FSMContext):
    await state.set_state(TopupState.waiting_amount)

    await call.message.edit_text(
        f"💵 Qancha pul kiritmoqchisiz?\n"
        f"📌 Minimal: {MIN_AMOUNT:,} so'm\n\n"
        "Summani raqam bilan yuboring."
    )
    await call.answer()


@dp.message(TopupState.waiting_amount)
async def get_amount(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("❌ Summani faqat raqam bilan kiriting.")
        return

    original_amount = int(message.text)

    if original_amount < MIN_AMOUNT:
        await message.answer(
            f"❌ Minimal to'lov {MIN_AMOUNT:,} so'm."
        )
        return

    payment_id, final_amount = create_payment(
        message.from_user.id,
        original_amount
    )

    await state.clear()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="❌ Bekor qilish",
            callback_data=f"cancel:{payment_id}"
        )]
    ])

    pay_message = await message.answer(
        "📋 <b>To'lov ma'lumotlari</b>\n\n"
        f"💵 To'lov summasi: <code>{final_amount}</code> so'm\n"
        f"💳 Karta: <code>{CARD_NUMBER}</code>\n"
        f"👤 Karta egasi: {CARD_OWNER}\n\n"
        f"⚠️ Aynan <code>{final_amount}</code> so'm yuboring.\n"
        "⏳ Tekshirish muddati: 5 daqiqa.",
        parse_mode="HTML",
        reply_markup=keyboard
    )

    await check_payment(
        message,
        pay_message,
        payment_id,
        final_amount,
        original_amount
    )


async def check_payment(
    message, pay_message,
    payment_id, final_amount, original_amount
):
    started = asyncio.get_running_loop().time()

    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while (
            asyncio.get_running_loop().time() - started
            < WAIT_SECONDS
        ):
            if payment_status(payment_id) != "pending":
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

                    if response.status == 200:
                        result = await response.json(
                            content_type=None
                        )

                        if result.get("status") == "success":
                            confirmed = confirm_payment(payment_id)

                            if confirmed:
                                user_id, paid_amount = confirmed

                                try:
                                    await pay_message.delete()
                                except Exception:
                                    pass

                                await message.answer(
                                    "✅ <b>To'lov muvaffaqiyatli!</b>\n\n"
                                    f"💰 Balansga qo'shildi: "
                                    f"{paid_amount:,} so'm\n"
                                    f"🆔 To'lov ID: {payment_id}\n\n"
                                    f"Joriy balans: "
                                    f"{get_balance(user_id):,} so'm",
                                    parse_mode="HTML",
                                    reply_markup=main_keyboard()
                                )
                            return

            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("To'lov API tekshiruvida xatolik")

            await asyncio.sleep(CHECK_INTERVAL)

    if payment_status(payment_id) == "pending":
        cancel_payment(payment_id)

        try:
            await pay_message.edit_text(
                "⏳ To'lov vaqti tugadi.\n"
                "Balansga pul qo'shilmadi.\n"
                "Qaytadan urinib ko'ring.",
                reply_markup=main_keyboard()
            )
        except Exception:
            pass


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel(call: CallbackQuery):
    try:
        payment_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Noto'g'ri so'rov.", show_alert=True)
        return

    row = db.execute("""
        SELECT user_id FROM payments WHERE id=?
    """, (payment_id,)).fetchone()

    if not row or row["user_id"] != call.from_user.id:
        await call.answer("To'lov topilmadi.", show_alert=True)
        return

    cancel_payment(payment_id)

    await call.message.edit_text(
        "❌ To'lov bekor qilindi.",
        reply_markup=main_keyboard()
    )
    await call.answer()


async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

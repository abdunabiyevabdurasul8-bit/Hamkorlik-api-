@dp.callback_query(F.data == "uzcard")
async def stars_pay(c: CallbackQuery, state: FSMContext):
    await state.set_state(TopupState.waiting_amount)
    
    # Eskisini o'chirib, yangisini yuboramiz
    try: await c.message.delete()
    except: pass
    
    msg = await c.message.answer("💵 Balansizni necha so'mga to'ldirmoqchisiz?\n📰 Minimal miqdor: 3000 so'm")
    await state.update_data(last_msg_id=msg.message_id)
    await c.answer()

@dp.message(TopupState.waiting_amount)
async def get_amount(m: Message, state: FSMContext):
    data = await state.get_data()
    last_msg_id = data.get("last_msg_id")

    # Foydalanuvchi yuborgan raqamni va botning eski xabarini o'chirish
    try:
        await m.delete()
        if last_msg_id:
            await m.bot.delete_message(m.chat.id, last_msg_id)
    except: pass

    if not m.text.isdigit() or int(m.text) < 3000:
        err = await m.answer("❌ Minimal 3000 so‘m kiriting!")
        await state.update_data(last_msg_id=err.message_id)
        return

    # --- KONFIGURATSIYA ---
    SHOP_ID = "1"
    SHOP_KEY = "0f12d211c6fb" # Bazadagi bilan bir xil bo'lishi shart
    KARTA = "9860 0366 4528 4641"
    original_amount = int(m.text)
    
    # Noyob summa yaratish
    if original_amount not in active_amounts: active_amounts[original_amount] = 0
    final_amount = original_amount + active_amounts[original_amount]
    active_amounts[original_amount] += 1

    await state.update_data(amount=final_amount, original=original_amount)
    await state.set_state(TopupState.waiting_payment)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_pay")]
    ])

    pay_msg = await m.answer(
        f"📋 <b>To‘lov ma'lumotlari:</b>\n\n"
        f"💵 To'lanishi kerak: <code>{final_amount}</code> so‘m\n"
        f"💳 Karta raqami: <code>{KARTA}</code>\n"
        f"👤 Ega: Bekchonov\n\n"
        f"⚠️ <b>Muhim:</b> To'lovni aynan <code>{final_amount}</code> so'm qilib o'tkazing.\n"
        f"⏳ To‘lovni kutish: 5 daqiqa",
        parse_mode="HTML",
        reply_markup=kb
    )

    # --- API TEKSHIRUV LOOP ---
    start_time = asyncio.get_event_loop().time()
    
    while asyncio.get_event_loop().time() - start_time < 300:
        await asyncio.sleep(10)
        
        if await state.get_state() != TopupState.waiting_payment:
            return 

        try:
            async with aiohttp.ClientSession() as session:
                url = "https://dcn268.wineclo.com/avtotolov/chek.php"
                # PHP-ga kerakli hamma ma'lumotni POST orqali yuboramiz
                payload = {
                    "amount": str(final_amount),
                    "shop_id": str(SHOP_ID),
                    "shop_key": str(SHOP_KEY),
                    "card": "4641" # Karta oxirgi 4 tasi
                }
                
                async with session.post(url, data=payload, timeout=5) as resp:
                    if resp.status == 200:
                        res_data = await resp.json()
                        if res_data.get("status") == "success":
                            # Balansni oshirish
                            async with db_pool.acquire() as conn:
                                async with conn.cursor() as cur:
                                    await cur.execute("UPDATE users SET balance = balance + %s WHERE id = %s", (original_amount, m.from_user.id))
                                    await cur.execute("INSERT INTO payments(user_id, shop_id, amount, method, date, card) VALUES(%s, %s, %s, %s, %s, %s)", 
                                                    (m.from_user.id, SHOP_ID, original_amount, "Uzcard", datetime.now().strftime("%d.%m.%Y %H:%M"), "5567"))
                                    await conn.commit()

                            try: await pay_msg.delete()
                            except: pass

                            await m.answer(f"✅ To'lov qabul qilindi!\n💰 Miqdor: {original_amount} UZS")
                            await state.clear()
                            return
        except Exception as e:
            logging.error(f"Loop Error: {e}")

    # Vaqt tugasa
    try: await pay_msg.edit_text("⏳ Vaqt tugadi. Qaytadan urinib ko'ring.")
    except: pass
    await state.clear()

@dp.callback_query(F.data == "cancel_pay")
async def cancel_payment(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    orig = data.get("original")
    
    # Miqdorni qaytarish logikasi
    if orig and orig in active_amounts and active_amounts[orig] > 0:
        active_amounts[orig] -= 1
        
    await state.clear()
    
    # Eski xabarni o'chirish
    try: 
        await c.message.delete()
    except: 
        pass

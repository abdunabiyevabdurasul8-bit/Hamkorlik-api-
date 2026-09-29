# Donuz Bot — Render PostgreSQL

Bu versiyada `DATABASE_URL` berilsa asosiy bot ma'lumotlari PostgreSQL'da saqlanadi. `DATABASE_URL` bo'lmasa eski SQLite rejimi ishlaydi.

## Render

Build Command:
```
pip install -r requirements.txt
```

Start Command:
```
python bot.py
```

Environment Variables:
- `BOT_TOKEN` — asosiy Telegram bot tokeni
- `ADMIN_ID` — admin Telegram ID
- `DATABASE_URL` — Render PostgreSQL Internal Database URL
- `PLAYPAY_API_KEY` — PlayPay API key (agar ishlatilsa)
- `PAYSTARS_API_KEY` — PayStars API key (agar ishlatilsa)
- `AKTIVSIM_API_KEY` — AktivSIM/Donuz API key (agar ishlatilsa)
- `BOT_TOKEN_ENCRYPTION_KEY` — ixtiyoriy, child bot tokenlarini shifrlash uchun Fernet key
- `GRAND_MOBILE_NICK_API_URL` — ixtiyoriy nickname API
- `GRAND_MOBILE_NICK_API_KEY` — ixtiyoriy nickname API key

## Muhim

Render Free PostgreSQL hozir 1 GB bilan beriladi, lekin yangi Free PostgreSQL bazasi 30 kundan keyin expire bo'ladi. Shuning uchun bu test/hobby uchun bepul variant; doimiy production saqlash uchun keyin pullik yoki boshqa persistent PostgreSQL kerak bo'ladi.

Kod yangilanganda `DATABASE_URL` o'zgarmasa, foydalanuvchilar, balanslar, buyurtmalar va boshqa asosiy ma'lumotlar PostgreSQL'da qoladi. GitHub'da token/API key saqlamang.

### Mavjud SQLite ma'lumotlarini ko'chirish

Agar eski `bot.db`da muhim ma'lumot bo'lsa, uni PostgreSQL'ga ko'chirishdan oldin alohida migratsiya qilish kerak. Bu ZIP ichidagi yangi kod eski SQLite faylini avtomatik o'chirmaydi.

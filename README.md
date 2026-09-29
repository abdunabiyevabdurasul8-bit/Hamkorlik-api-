# Donuz / PlayPay Telegram Bot

## Render
Build Command:
`pip install -r requirements.txt`

Start Command:
`python bot.py`

## Required Environment Variables
- BOT_TOKEN = Telegram bot token
- ADMIN_ID = admin Telegram user ID
- PLAYPAY_API_KEY = PlayPay API key

## Optional Environment Variables
- PAYMENT_CARD
- PAYSTARS_API_KEY
- PAYSTARS_API
- PAYSTARS_MARKUP_PERCENT
- AKTIVSIM_API_KEY
- AKTIVSIM_MARKUP_PERCENT
- DONUZ_API_KEY
- GRAND_MOBILE_NICK_API_URL
- GRAND_MOBILE_NICK_API_KEY
- BOT_TOKEN_ENCRYPTION_KEY
- DB_PATH
- SUB_PRICE_7
- SUB_PRICE_30
- SUB_PRICE_90
- SUB_PRICE_365

Do not put real API keys or BOT_TOKEN in GitHub.

## Grand Mobile
Manual packages are built into the bot. Grand Mobile orders are sent to the admin with:
- TUSHDI
- BEKOR QILDI

If a nickname-check API is configured, the bot checks the Grand Mobile ID before ordering.

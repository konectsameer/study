import os
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

from telegram import Update, Bot
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder

from supabase import create_client
import google.generativeai as genai

# ---------------------------------------------------------
#  CONFIG: Load from environment variables (Render)
# ---------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
GEMINI_KEY = os.getenv("GEMINI_KEY")

if not TELEGRAM_TOKEN or not SUPABASE_URL or not SUPABASE_KEY or not GEMINI_KEY:
    raise Exception("Environment variables missing. Check TELEGRAM_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GEMINI_KEY.")

# Init Supabase + Gemini
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_KEY)

# ---------------------------------------------------------
#  FastAPI server (Render will serve this)
# ---------------------------------------------------------
app = FastAPI()
bot = Bot(token=TELEGRAM_TOKEN)

# Build Telegram application (for processing updates)
application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()


# ---------------------------------------------------------
#  Example processing logic
# ---------------------------------------------------------
async def process_message(update: Update):
    """Basic text processing using Gemini."""
    user_input = update.message.text

    model = genai.GenerativeModel("gemini-1.5-flash")
    response = model.generate_content(user_input)

    reply = response.text or "I could not generate a response."

    await update.message.reply_text(reply)


# Add handler
from telegram.ext import MessageHandler, filters
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_message))


# ---------------------------------------------------------
#  Telegram Webhook Receiver Endpoint
# ---------------------------------------------------------
@app.post("/webhook")
async def webhook_handler(request: Request):
    """Handle Telegram webhook updates."""
    data = await request.json()
    update = Update.de_json(data, bot)

    await application.initialize()
    await application.process_update(update)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------
#  Root test endpoint
# ---------------------------------------------------------
@app.get("/")
async def home():
    return {"status": "Bot running (webhook mode)"}


# ---------------------------------------------------------
#  Auto-set Telegram webhook on startup
# ---------------------------------------------------------
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # MUST match your Render domain + /webhook

@app.on_event("startup")
async def set_webhook():
    if not WEBHOOK_URL:
        raise Exception("WEBHOOK_URL env variable missing.")

    await bot.delete_webhook()
    await bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")

    logging.info("Webhook set to: %s/webhook", WEBHOOK_URL)


# ---------------------------------------------------------
#  Run FastAPI server (Render uses this)
# ---------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("bot:app", host="0.0.0.0", port=port)

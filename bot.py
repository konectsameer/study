import os
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from supabase import create_client
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import google.generativeai as genai
import pdfplumber
import base64
import requests

# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# Load ENV Variables
# ---------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
GEMINI_KEY = os.getenv("GEMINI_KEY")

if not TELEGRAM_TOKEN or not SUPABASE_URL or not SUPABASE_KEY or not GEMINI_KEY:
    raise Exception("Missing required environment variables!")

# Supabase client
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Gemini client
genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ---------------------------------------------------------
# FastAPI app (for webhook)
# ---------------------------------------------------------
app_fast = FastAPI()

# ---------------------------------------------------------
# Send Inline Buttons
# ---------------------------------------------------------
def mode_buttons():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Flashcards", callback_data="flashcards"),
                InlineKeyboardButton("Notes", callback_data="notes"),
                InlineKeyboardButton("Quiz", callback_data="quiz"),
            ]
        ]
    )


async def send_mode_selector(chat_id, context):
    await context.bot.send_message(
        chat_id=chat_id,
        text="Choose how you want me to process your input:",
        reply_markup=mode_buttons(),
    )


# ---------------------------------------------------------
# Extract text from PDF
# ---------------------------------------------------------
def extract_pdf_text(file_path):
    text = ""
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text += page.extract_text() or ""
    return text.strip()


# ---------------------------------------------------------
# Process Text into Flashcards / Notes / Quiz
# ---------------------------------------------------------
async def process_with_gemini(text, mode):
    prompt = ""

    if mode == "flashcards":
        prompt = f"""
        Convert the following content into clean flashcards.
        Return JSON list: [{{"front": "...", "back": "..."}}]
        
        CONTENT:
        {text}
        """

    elif mode == "notes":
        prompt = f"""
        Convert the following content into clean study notes.
        Provide markdown formatted bullet points.
        
        CONTENT:
        {text}
        """

    elif mode == "quiz":
        prompt = f"""
        Generate 5 quiz questions with answers.
        Format:
        Q1:
        A1:

        CONTENT:
        {text}
        """

    response = model.generate_content(prompt)
    return response.text


# ---------------------------------------------------------
# Save to Supabase
# ---------------------------------------------------------
def save_to_supabase(user_id, mode, raw_text, output):
    supabase.table("flashcards").insert(
        {
            "user_id": user_id,
            "task": mode,
            "raw_text": raw_text,
            "result": output,
        }
    ).execute()


# ---------------------------------------------------------
# Handle normal text / images / PDFs
# ---------------------------------------------------------
async def incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id

    # Show buttons
    await send_mode_selector(chat_id, context)

    # Save last user message for processing after button click
    context.user_data["last_input"] = update.message

    await context.bot.send_message(
        chat_id=chat_id,
        text="Now select Flashcards / Notes / Quiz 👆",
    )


# ---------------------------------------------------------
# Callback button handler
# ---------------------------------------------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    mode = query.data  # flashcards / notes / quiz
    user_message = context.user_data.get("last_input")

    if not user_message:
        await query.edit_message_text("Send something first!")
        return

    raw_text = ""

    # TEXT
    if user_message.text:
        raw_text = user_message.text

    # IMAGES
    elif user_message.photo:
        file_id = user_message.photo[-1].file_id
        file = await context.bot.get_file(file_id)
        img_data = requests.get(file.file_path).content
        image_b64 = base64.b64encode(img_data).decode()
        raw_text = model.generate_content(
            f"Extract text from this image encoded as base64:\n{image_b64}"
        ).text

    # PDF FILE
    elif user_message.document:
        file_id = user_message.document.file_id
        tg_file = await context.bot.get_file(file_id)
        pdf_bytes = requests.get(tg_file.file_path).content

        with open("temp.pdf", "wb") as f:
            f.write(pdf_bytes)

        raw_text = extract_pdf_text("temp.pdf")

    else:
        await query.edit_message_text("Unsupported message type.")
        return

    # Process with Gemini
    result = await process_with_gemini(raw_text, mode)

    # Save in Supabase
    save_to_supabase(str(user_message.chat_id), mode, raw_text, result)

    await query.edit_message_text(
        f"**Your {mode.capitalize()} are ready:**\n\n{result}",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------
# Telegram Webhook Entry Point
# ---------------------------------------------------------
@app_fast.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------
# Build Telegram Application (no polling!)
# ---------------------------------------------------------
bot_app = (
    Application.builder()
    .token(TELEGRAM_TOKEN)
    .build()
)

bot_app.add_handler(MessageHandler(filters.ALL, incoming_message))
bot_app.add_handler(CallbackQueryHandler(button_handler))


# ---------------------------------------------------------
# Root Endpoint
# ---------------------------------------------------------
@app_fast.get("/")
def home():
    return {"status": "running"}


# ---------------------------------------------------------
# Start Message
# ---------------------------------------------------------
print("Bot with webhook is ready!")


# ---------------------------------------------------------
# Root Endpoint
# ---------------------------------------------------------
@app_fast.get("/")
def home():
    return {"status": "running"}


# ---------------------------------------------------------
# Uvicorn Startup (Render fix)
# ---------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    import os

    port = int(os.environ.get("PORT", 8000))
    print(f"Starting server on port {port}...")

    uvicorn.run(
        "bot:app_fast",
        host="0.0.0.0",
        port=port,
        workers=1
    )


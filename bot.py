"""
Study Assistant Telegram Bot
- Supports Text, Image, PDF
- Offers inline buttons: Flashcards / Notes / Quiz
- Uses pytesseract + pdfplumber for OCR/text extraction
- Sends prompts to Gemini (via google.generativeai wrapper)
- Saves results to Supabase
"""

import os
import io
import logging
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from supabase import create_client
import google.generativeai as genai

# OCR / PDF libs
from PIL import Image
import pytesseract
import pdfplumber

# basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- CONFIG: read from environment ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")         # set to your telegram bot token
SUPABASE_URL = os.getenv("SUPABASE_URL")             # set to your supabase url
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")# set to your supabase service-role key (or anon key if only insert)
GEMINI_KEY = os.getenv("GEMINI_KEY")   

if not (TELEGRAM_TOKEN and SUPABASE_URL and SUPABASE_KEY and GEMINI_KEY):
    logger.warning("One or more required environment variables are missing. "
                   "Set TELEGRAM_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GEMINI_KEY")

# init supabase client
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# init gemini
genai.configure(api_key=GEMINI_KEY)
GENAI_MODEL = "gemini-pro"  # adjust if needed

# ---------- Helper utilities ----------

def extract_text_from_image_bytes(image_bytes: bytes) -> str:
    """Use pytesseract to extract text from image bytes."""
    try:
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        text = pytesseract.image_to_string(im)
        return text.strip()
    except Exception as e:
        logger.exception("Image OCR failed")
        return ""

def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """Extract text from PDF using pdfplumber (joins pages)."""
    try:
        text_parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
        return "\n\n".join(p for p in text_parts if p.strip())
    except Exception as e:
        logger.exception("PDF text extraction failed")
        return ""

def call_gemini(prompt: str, max_output_tokens: int = 1024) -> str:
    """Simple wrapper to call Gemini (text generation)."""
    try:
        resp = genai.generate(
            model=GENAI_MODEL,
            prompt=prompt,
            max_output_tokens=max_output_tokens,
        )
        # gemini wrapper returns text in resp.text or resp.candidates[0].output
        text = getattr(resp, "text", None) or (resp.candidates[0].output if getattr(resp, "candidates", None) else None)
        return text or ""
    except Exception as e:
        logger.exception("Gemini request failed")
        return "Error: AI generation failed."

def save_to_supabase(user_id: str, task: str, raw_text: str, generated: str):
    """Insert a record into Supabase table `flashcards` (create if needed)."""
    try:
        payload = {
            "user_id": str(user_id),
            "task": task,
            "raw_text": raw_text,
            "result_text": generated,
        }
        res = supabase.table("flashcards").insert(payload).execute()
        logger.info("Saved to supabase: %s", res)
        return res
    except Exception as e:
        logger.exception("Supabase insert failed")
        return None

# ---------- Telegram handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! Send me text, an image, or a PDF and I'll help convert it into flashcards, notes, or a quiz.")

async def ask_mode(chat_id: int, context: ContextTypes.DEFAULT_TYPE, store_key="raw_input"):
    keyboard = [
        [
            InlineKeyboardButton("Flashcards", callback_data=f"mode|flashcards"),
            InlineKeyboardButton("Notes", callback_data=f"mode|notes"),
            InlineKeyboardButton("Quiz", callback_data=f"mode|quiz"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id=chat_id, text="Choose how you want me to process your input:", reply_markup=reply_markup)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text or ""
    chat_id = update.message.chat_id
    user_id = update.effective_user.id

    # store raw input in user_data for next callback selection
    context.user_data["raw_input"] = user_text
    context.user_data["user_id"] = user_id

    # ask mode
    await ask_mode(chat_id, context)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # get highest-resolution photo
    photo_list = update.message.photo
    if not photo_list:
        await update.message.reply_text("No photo found.")
        return

    file_obj = await photo_list[-1].get_file()  # last is highest res
    file_bytes = await file_obj.download_as_bytearray()

    extracted_text = extract_text_from_image_bytes(bytes(file_bytes))
    if not extracted_text:
        await update.message.reply_text("Couldn't extract text from the image.")
        return

    context.user_data["raw_input"] = extracted_text
    context.user_data["user_id"] = update.effective_user.id

    await ask_mode(update.message.chat_id, context)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # if PDF, extract text
    doc = update.message.document
    if not doc:
        await update.message.reply_text("No document attached.")
        return

    # download binary
    file_obj = await doc.get_file()
    file_bytes = await file_obj.download_as_bytearray()
    mime = (doc.mime_type or "").lower()

    extracted_text = ""
    if "pdf" in mime or doc.file_name.lower().endswith(".pdf"):
        extracted_text = extract_text_from_pdf_bytes(bytes(file_bytes))
    else:
        # for other docs try as plain text
        try:
            extracted_text = file_bytes.decode("utf-8", errors="ignore")
        except Exception:
            extracted_text = ""

    if not extracted_text:
        await update.message.reply_text("Couldn't extract text from the document.")
        return

    context.user_data["raw_input"] = extracted_text
    context.user_data["user_id"] = update.effective_user.id
    await ask_mode(update.message.chat_id, context)

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # callback payload format: "mode|flashcards"
    payload = query.data or ""
    parts = payload.split("|")
    if len(parts) >= 2 and parts[0] == "mode":
        mode = parts[1]
    else:
        # unknown callback
        await query.edit_message_text("Unrecognized action.")
        return

    raw_input = context.user_data.get("raw_input", "")
    user_id = context.user_data.get("user_id", update.effective_user.id)

    if not raw_input:
        await query.edit_message_text("No input found to process. Please send me a text, image, or PDF first.")
        return

    # Build prompt depending on mode
    if mode == "flashcards":
        prompt = f"Create clear, concise flashcards from the material below. Output as question → answer pairs, each pair separated by a blank line.\n\nMaterial:\n{raw_input}"
    elif mode == "notes":
        prompt = f"Create structured study notes from the text below. Use headings, bullet points and concise explanations.\n\nMaterial:\n{raw_input}"
    elif mode == "quiz":
        prompt = f"Create an exam-style quiz from the material below. Provide 8-12 multiple choice questions with four options each and mark the correct answer.\n\nMaterial:\n{raw_input}"
    else:
        prompt = f"Process this text ({mode}):\n\n{raw_input}"

    # Call Gemini
    await query.edit_message_text(f"Generating {mode} — please wait...")
    ai_text = call_gemini(prompt)

    # Save to Supabase
    save_to_supabase(user_id=user_id, task=mode, raw_text=raw_input, generated=ai_text)

    # Reply with generated content (edit original message)
    # If the content is long, send as file
    if len(ai_text) > 1900:
        # send as text file
        bio = io.BytesIO(ai_text.encode("utf-8"))
        bio.name = f"{mode}.txt"
        await context.bot.send_document(chat_id=update.effective_chat.id, document=InputFile(bio))
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"{mode.capitalize()} saved to Supabase.")
    else:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"*{mode.capitalize()}*\n\n{ai_text}", parse_mode="Markdown")

    # clear stored input
    context.user_data.pop("raw_input", None)

# ---------- main ----------
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    print("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()


import os
import logging
import tempfile
import asyncio
from typing import Optional
from fastapi import FastAPI, Request
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    File as TgFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    Application,
)

# Optional: generative model library (Gemini). We'll attempt to import if available.
try:
    import google.generativeai as genai  # optional
    HAS_GENAI = True
except Exception:
    HAS_GENAI = False

# Optional NLP libs
try:
    import pytesseract
    from PIL import Image
    HAS_TESSERACT = True
except Exception:
    HAS_TESSERACT = False

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except Exception:
    HAS_PDFPLUMBER = False

# ---------------------------------------------------------------------
# Config & Logging
# ---------------------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")  # optional

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN env var is required")

if HAS_GENAI and GEMINI_API_KEY:
    # configure genai if library present
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        # continue — we'll fall back if genai fails
        logging.warning("Could not configure google.generativeai: %s", e)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s:%(name)s: %(message)s"
)
logger = logging.getLogger("study-assistant-bot")

# ---------------------------------------------------------------------
# FastAPI and Telegram Application
# ---------------------------------------------------------------------
app = FastAPI()

# Build the telegram Application (async)
application: Application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# ---------------------------------------------------------------------
# Utilities: run CPU work in threadpool
# ---------------------------------------------------------------------
async def run_in_thread(fn, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


# ---------------------------------------------------------------------
# Utility: call a generative model (Gemini) or fallback
# ---------------------------------------------------------------------
async def call_generative_model(prompt: str, mode: str = "notes") -> str:
    """
    Try to call google.generativeai (Gemini) if available and API key present.
    Otherwise return a simple canned/fallback response.
    mode: 'flashcards' | 'notes' | 'quiz'
    """
    logger.info("call_generative_model mode=%s prompt=%s", mode, (prompt[:80] + "...") if len(prompt) > 80 else prompt)
    if HAS_GENAI and GEMINI_API_KEY:
        try:
            # example using google.generativeai v0.x API - adapt as your package version requires
            response = genai.generate_text(model="models/text-bison-001", input=prompt)
            text = response.text if hasattr(response, "text") else str(response)
            return text
        except Exception as e:
            logger.exception("Generative model call failed: %s", e)
            return f"(Generative model failed) {e}"
    # fallback
    if mode == "flashcards":
        return "Flashcard 1: Q - What is Newton's first law? A - Inertia. \nFlashcard 2: Q - ... (fallback)."
    if mode == "quiz":
        return "Quiz (fallback): 1) What is inertia? A) ... (fallback)."
    return "Notes (fallback): This is a set of notes extracted from the input."

# ---------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("Flashcards", callback_data="flashcards"),
            InlineKeyboardButton("Notes", callback_data="notes"),
            InlineKeyboardButton("Quiz", callback_data="quiz"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text(
            "Choose how you want me to process your input:",
            reply_markup=reply_markup,
        )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callback query"""
    query = update.callback_query
    if not query:
        return
    await query.answer()  # acknowledge
    choice = query.data
    # Store user choice into context.user_data for later use
    # (so if user sends an image afterward, we know which flow to use)
    if context.user_data is None:
        context.user_data = {}
    context.user_data["mode"] = choice
    logger.info("User selected mode=%s", choice)
    await query.edit_message_text(text=f"Selected: {choice}. Now send me text, an image, or a PDF.")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main message handler: handles text, photo, document(PDF)"""
    if update.message is None:
        return
    user_id = update.message.from_user.id if update.message.from_user else None
    logger.info("Message from user %s", user_id)

    # If user has not selected a mode yet — show keyboard again
    mode = context.user_data.get("mode") if context.user_data else None
    if not mode:
        # send inline keyboard again
        keyboard = [
            [
                InlineKeyboardButton("Flashcards", callback_data="flashcards"),
                InlineKeyboardButton("Notes", callback_data="notes"),
                InlineKeyboardButton("Quiz", callback_data="quiz"),
            ]
        ]
        await update.message.reply_text("Please choose how you'd like me to process your input:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # Text message
    if update.message.text:
        text = update.message.text
        # Prepare prompt for generative model based on selected mode
        prompt = f"Mode: {mode}\nUser text:\n{text}"
        # call model
        model_response = await call_generative_model(prompt, mode=mode)
        await update.message.reply_text(model_response)
        return

    # Photo
    if update.message.photo:
        if not HAS_TESSERACT:
            await update.message.reply_text("OCR is not available on this instance (pytesseract missing).")
            return
        # choose highest resolution photo
        photo = update.message.photo[-1]
        file_id = photo.file_id
        await update.message.reply_text("Received image. Downloading and running OCR...")
        text = await download_and_ocr(file_id)
        prompt = f"Mode: {mode}\nExtracted text from image:\n{text}"
        model_response = await call_generative_model(prompt, mode=mode)
        await update.message.reply_text(model_response)
        return

    # Document (PDF) and maybe other types
    if update.message.document:
        doc = update.message.document
        mime = doc.mime_type or ""
        logger.info("Document mime_type=%s file_name=%s", mime, doc.file_name)
        if "pdf" in mime or (doc.file_name and doc.file_name.lower().endswith(".pdf")):
            if not HAS_PDFPLUMBER:
                await update.message.reply_text("PDF text extraction is not available on this instance (pdfplumber missing).")
                return
            await update.message.reply_text("PDF received. Downloading and extracting text...")
            text = await download_and_extract_pdf(doc.file_id)
            prompt = f"Mode: {mode}\nExtracted text from PDF:\n{text[:5000]}"  # limit size
            model_response = await call_generative_model(prompt, mode=mode)
            await update.message.reply_text(model_response)
        else:
            await update.message.reply_text("Document received but unsupported mime type for automatic processing.")
        return

    # Other message types
    await update.message.reply_text("I didn't understand that. Send text, an image, or a PDF.")


# ---------------------------------------------------------------------
# Download + OCR / PDF extraction helpers
# ---------------------------------------------------------------------
async def download_and_ocr(file_id: str) -> str:
    """
    Download file from Telegram and run pytesseract OCR (image).
    Returns extracted text.
    """
    bot = application.bot
    file: TgFile = await bot.get_file(file_id)
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        # download to disk (async)
        await file.download_to_drive(custom_path=tmp_path)
        # run OCR in thread (pytesseract requires blocking PIL ops)
        def do_ocr(path):
            img = Image.open(path)
            text = pytesseract.image_to_string(img)
            return text

        text = await run_in_thread(do_ocr, tmp_path)
        return text or "(no text found)"
    except Exception as e:
        logger.exception("OCR failed: %s", e)
        return f"(OCR failed: {e})"
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


async def download_and_extract_pdf(file_id: str) -> str:
    """
    Download PDF file and extract text via pdfplumber
    """
    bot = application.bot
    file: TgFile = await bot.get_file(file_id)
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        await file.download_to_drive(custom_path=tmp_path)

        def extract(path):
            text_parts = []
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    try:
                        txt = page.extract_text() or ""
                    except Exception:
                        txt = ""
                    text_parts.append(txt)
            return "\n\n".join(text_parts)

        text = await run_in_thread(extract, tmp_path)
        return text or "(no text extracted)"
    except Exception as e:
        logger.exception("PDF extraction failed: %s", e)
        return f"(PDF extraction failed: {e})"
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


# ---------------------------------------------------------------------
# Register handlers to application
# ---------------------------------------------------------------------
application.add_handler(CommandHandler("start", start_handler))
application.add_handler(CallbackQueryHandler(callback_handler))
application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))


# ---------------------------------------------------------------------
# FastAPI webhook route
# ---------------------------------------------------------------------
@app.post("/webhook")
async def webhook(request: Request):
    """
    Telegram will POST updates here.
    We must:
      - parse JSON
      - build telegram.Update and pass to application
      - return quickly returning {"ok": True}
    """
    try:
        data = await request.json()
    except Exception as e:
        logger.exception("Failed to parse webhook JSON: %s", e)
        return {"ok": False}

    try:
        update = Update.de_json(data, application.bot)
        # process update inside telegram application
        await application.process_update(update)
    except Exception as e:
        logger.exception("Processing update failed: %s", e)
        # Do not crash on a single update; Telegram expects a 200 with JSON
        return {"ok": False}

    return {"ok": True}


# ---------------------------------------------------------------------
# simple root endpoint to verify app is running
# ---------------------------------------------------------------------
@app.get("/")
async def root():
    return {"status": "ok"}


# ---------------------------------------------------------------------
# Run by uvicorn in local dev only
# ---------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logger.info("Starting uvicorn on port %s", port)
    uvicorn.run("bot:app", host="0.0.0.0", port=port, log_level="info")

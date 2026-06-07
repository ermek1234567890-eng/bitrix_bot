"""
Минимальный тестовый бот - просто ответит OK на любое сообщение
"""
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import os
from dotenv import load_dotenv
import logging

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_TOKEN", "")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    log.info(f"START command from user {update.effective_user.id}")
    await update.message.reply_text("✅ Bot is working!")

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    log.info(f"Message from {update.effective_user.id}: {update.message.text}")
    await update.message.reply_text(f"You said: {update.message.text}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.error(f"Error: {context.error}")

if __name__ == "__main__":
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    log.info("Test bot started...")
    app.run_polling(drop_pending_updates=True)

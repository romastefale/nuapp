"""Secretary Bot - Telegram Business automated assistant.

Asynchronous Telegram bot that listens to Telegram Business connections
and replies on behalf of the connected business account.
"""

import logging
import os
import sys

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment configuration (fail-fast)
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    logger.critical("Missing required environment variable: TELEGRAM_TOKEN")
    sys.exit(1)

# Default reply sent on every incoming business message.
AUTO_REPLY_TEXT = (
    "Oi! Tudo bem? Vi sua mensagem aqui, "
    "estou só finalizando uma coisa e já te respondo direitinho. 🙏"
)


async def handle_business_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle incoming Telegram Business messages with guard clauses."""

    # Guard 1: ignore updates that are not business messages.
    business_message = update.business_message
    if business_message is None:
        return

    # Guard 2: ignore messages sent by other bots.
    sender = business_message.from_user
    if sender is None or sender.is_bot:
        return

    # Guard 3: prevent infinite reply loops (skip messages from ourselves).
    bot_id = context.bot.id
    if sender.id == bot_id:
        return

    # Extract routing info: chat and business connection are both mandatory.
    chat_id = business_message.chat_id
    business_connection_id = business_message.business_connection_id
    if not business_connection_id:
        logger.warning(
            "Business message %s missing business_connection_id; skipping.",
            business_message.message_id,
        )
        return

    logger.info(
        "Incoming business message from user=%s chat=%s connection=%s",
        sender.id,
        chat_id,
        business_connection_id,
    )

    # Resilient send: log and swallow exceptions to keep the event loop alive.
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=AUTO_REPLY_TEXT,
            business_connection_id=business_connection_id,
        )
    except Exception as exc:  # noqa: BLE001 - we want to log any failure
        logger.exception("Failed to send business reply: %s", exc)


def main() -> None:
    """Build the application and start long polling."""
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # A single generic handler covers all message types; the function itself
    # filters down to business messages via guard clauses.
    application.add_handler(MessageHandler(filters.ALL, handle_business_message))

    logger.info("Starting Secretary Bot polling loop...")
    # allowed_updates=ALL_TYPES is required so business_connection / business_message
    # updates are not silently dropped by Telegram.
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

"""Secretary Bot - Telegram Business relay assistant.

Receives messages addressed to a connected Telegram Business account and
relays them into a private group where the user can collaborate with the
Mira AI bot. Whatever the user replies (in-group, as a Telegram reply to
the relayed message) is then sent back to the original customer through
the business connection.
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment (fail-fast)
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    logger.critical("Missing required environment variable: TELEGRAM_TOKEN")
    sys.exit(1)


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


# Private group where the user + Mira AI bot live. Optional at boot — the
# bot can run without it and the /id command helps you discover the value.
GROUP_CHAT_ID: int | None = _parse_int(os.environ.get("GROUP_CHAT_ID"))

# Prefix that wakes the Mira AI bot in the group. The relay message starts
# with this so Mira automatically answers with a suggested reply. The bot
# then forwards Mira's reply back to the customer automatically.
MIRA_PROMPT = os.environ.get(
    "MIRA_PROMPT",
    "Mira, responda essa mensagem em até 1 frase curta, casual e neutra, "
    "em português, no mesmo tom de quem escreveu. Não peça desculpas, "
    "não diga que demorou, não use saudações longas, não comente o "
    "assunto antigo. Exemplos do estilo desejado: \"oi\", \"oi, tudo bem?\", "
    "\"tá bom\", \"manda aí\". Escreva apenas a resposta final, sem aspas "
    "e sem explicação.",
)

# ---------------------------------------------------------------------------
# Lightweight JSON state (mapping group msg_id -> customer routing info)
# ---------------------------------------------------------------------------
STATE_FILE = Path("state.json")


def _load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Could not parse state.json; starting fresh.")
    return {"forwards": {}, "owner_user_id": None}


def _save_state(state: dict[str, Any]) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.exception("Failed to persist state.json")


STATE: dict[str, Any] = _load_state()


def _remember_forward(group_msg_id: int, payload: dict[str, Any]) -> None:
    STATE["forwards"][str(group_msg_id)] = payload
    _save_state(STATE)


def _lookup_forward(group_msg_id: int) -> dict[str, Any] | None:
    return STATE["forwards"].get(str(group_msg_id))


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the current chat's ID. Useful for discovering GROUP_CHAT_ID."""
    chat = update.effective_chat
    if chat is None:
        return
    text = (
        f"Chat ID: <code>{chat.id}</code>\n"
        f"Tipo: {chat.type}\n"
        f"Título: {chat.title or chat.full_name or '-'}"
    )
    await context.bot.send_message(chat_id=chat.id, text=text, parse_mode="HTML")


async def on_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture the business account owner's user_id so we can filter outgoing msgs."""
    bc = update.business_connection
    if bc is None or bc.user is None:
        return
    STATE["owner_user_id"] = bc.user.id
    _save_state(STATE)
    logger.info(
        "Business connection %s active=%s owner=%s",
        bc.id,
        bc.is_enabled,
        bc.user.id,
    )


async def _resolve_owner_id(
    context: ContextTypes.DEFAULT_TYPE, business_connection_id: str
) -> int | None:
    """Return the business account owner's user id, learning it once if needed."""
    owner_id = STATE.get("owner_user_id")
    if owner_id:
        return owner_id
    try:
        bc = await context.bot.get_business_connection(business_connection_id)
    except Exception:
        logger.exception("Could not fetch business connection %s", business_connection_id)
        return None
    if bc and bc.user:
        STATE["owner_user_id"] = bc.user.id
        _save_state(STATE)
        logger.info("Learned business owner user_id=%s", bc.user.id)
        return bc.user.id
    return None


async def handle_business_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Relay an incoming business message to the configured group."""
    msg = update.business_message
    if msg is None:
        return
    sender = msg.from_user
    if sender is None or sender.is_bot:
        return
    if sender.id == context.bot.id:
        return

    business_connection_id = msg.business_connection_id
    if not business_connection_id:
        return

    # Skip messages the user (business owner) sent themselves in the chat.
    owner_id = await _resolve_owner_id(context, business_connection_id)
    if owner_id and sender.id == owner_id:
        logger.info("Ignored outgoing message from business owner (user_id=%s)", owner_id)
        return

    if GROUP_CHAT_ID is None:
        logger.warning(
            "GROUP_CHAT_ID not set. Send /id in your private group and set it as a secret."
        )
        return

    sender_name = sender.full_name
    sender_handle = f" (@{sender.username})" if sender.username else ""
    body = msg.text or msg.caption or "(mensagem sem texto — mídia recebida)"

    # IMPORTANT: the AI trigger ("Mira, …") MUST be the first thing in the
    # message — the AI bot in the group only fires when its name appears at
    # the very beginning. Customer context goes after.
    relay_text = (
        f"{MIRA_PROMPT}\n\n"
        f"📩 De <b>{sender_name}</b>{sender_handle}:\n"
        f"<blockquote>{_html_escape(body)}</blockquote>"
    )

    try:
        sent = await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=relay_text,
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Failed to relay business message to group")
        return

    _remember_forward(
        sent.message_id,
        {
            "chat_id": msg.chat_id,
            "business_connection_id": business_connection_id,
            "customer_name": sender_name,
            "customer_user_id": sender.id,
        },
    )
    logger.info(
        "Relayed business msg from user=%s -> group msg %s",
        sender.id,
        sent.message_id,
    )


async def handle_group_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route replies inside the group back to the customer.

    Accepts BOTH Mira's automatic suggestion (a bot reply to the relayed
    message) and the user's own manual reply. First one wins per forward.
    """
    msg = update.message
    if msg is None or msg.from_user is None:
        return
    if GROUP_CHAT_ID is None or msg.chat_id != GROUP_CHAT_ID:
        return
    # Never echo our own bot's messages.
    if msg.from_user.id == context.bot.id:
        return
    reply_to = msg.reply_to_message
    if reply_to is None:
        return
    entry = _lookup_forward(reply_to.message_id)
    if entry is None:
        return
    if entry.get("answered"):
        # Already responded to this customer message; ignore extras.
        return

    text = msg.text or msg.caption
    if not text:
        if not msg.from_user.is_bot:
            await msg.reply_text("❌ Por enquanto só dá pra responder com texto.")
        return

    source_is_mira = msg.from_user.is_bot

    try:
        await context.bot.send_message(
            chat_id=entry["chat_id"],
            text=text,
            business_connection_id=entry["business_connection_id"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to deliver reply to customer: %s", exc)
        if not source_is_mira:
            await msg.reply_text(f"❌ Falha ao enviar: {exc}")
        return

    entry["answered"] = True
    _remember_forward(reply_to.message_id, entry)

    source = "IA" if source_is_mira else "você"
    try:
        await msg.reply_text(
            f"✅ Enviado para {entry['customer_name']} (por {source})"
        )
    except Exception:
        logger.exception("Failed to post confirmation in group")


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # /id works anywhere — DM, group, business chat.
    application.add_handler(CommandHandler("id", cmd_id))

    # Learn the business account owner's id from connection updates.
    # Placed in its own dispatch group so it doesn't swallow other handlers.
    application.add_handler(TypeHandler(Update, on_business_connection), group=-1)

    # Group 0: catch business messages (sent from a counterparty inside a
    # business chat) and relay them to the configured group.
    application.add_handler(
        MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_message),
        group=0,
    )

    # Group 1 (separate dispatch group): handle replies posted inside the
    # configured private group so they get routed back to the customer.
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.REPLY, handle_group_reply),
        group=1,
    )
    application.add_handler(
        MessageHandler(filters.ChatType.SUPERGROUP & filters.REPLY, handle_group_reply),
        group=1,
    )

    logger.info("Starting Secretary Bot polling loop...")
    if GROUP_CHAT_ID is None:
        logger.warning(
            "GROUP_CHAT_ID not set yet. Send /id inside your private group "
            "to discover it, then add it as a secret and restart."
        )
    else:
        logger.info("Relaying business messages to group %s", GROUP_CHAT_ID)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

"""Secretary Bot - Telegram Business relay assistant.

Receives messages addressed to a connected Telegram Business account and
relays them into a private group where the user can collaborate with the
Mira AI bot.

Two delivery paths back to the customer:

1. **Bot API path (PTB)** — when the user (or any bot) sends a Telegram
   reply to one of our relay messages, the Bot API delivers that update
   to us. ``handle_group_message`` forwards the right text back to the
   customer via ``business_connection_id``.

2. **MTProto path (Telethon)** — the Bot API never delivers messages
   authored by other bots that simply post in a group (Bot-to-Bot mode is
   opaque and unreliable). To capture Mira's replies deterministically we
   run a parallel Telethon listener under the user's own account, which
   sees every group message just like a normal client. When Mira posts a
   reply to one of our relays the listener forwards her text back to the
   customer through the bot's business connection.

The MTProto listener is optional — if ``TELEGRAM_API_ID``,
``TELEGRAM_API_HASH`` or ``TELEGRAM_SESSION_STRING`` is missing, the bot
runs in Bot-API-only mode without crashing.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
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

# MTProto listener credentials (optional). When set, a parallel Telethon
# client logs in with the user's own account and intercepts Mira's replies
# in the group — bypassing all Bot-API delivery restrictions.
MTPROTO_API_ID: int | None = _parse_int(os.environ.get("TELEGRAM_API_ID"))
MTPROTO_API_HASH: str | None = os.environ.get("TELEGRAM_API_HASH") or None
MTPROTO_SESSION_STRING: str | None = os.environ.get("TELEGRAM_SESSION_STRING") or None

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


def _oldest_unanswered_forward() -> tuple[int, dict[str, Any]] | None:
    """Return (group_msg_id, payload) for the oldest still-unanswered forward."""
    pending = [
        (int(mid), payload)
        for mid, payload in STATE["forwards"].items()
        if not payload.get("answered")
    ]
    if not pending:
        return None
    pending.sort(key=lambda item: item[0])
    return pending[0]


# ---------------------------------------------------------------------------
# Handlers — Bot API (python-telegram-bot)
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


async def log_every_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Diagnostic: log every incoming update so we can see what reaches the bot."""
    try:
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        reply_to_id = msg.reply_to_message.message_id if (msg and msg.reply_to_message) else None
        reply_to_from = (
            msg.reply_to_message.from_user.id
            if (msg and msg.reply_to_message and msg.reply_to_message.from_user)
            else None
        )
        text_preview = ((msg.text or msg.caption) if msg else None) or ""
        logger.info(
            "RAW update_id=%s type=%s chat=%s(%s) from=%s(@%s,bot=%s) "
            "reply_to=%s reply_to_from=%s text=%r",
            update.update_id,
            type(update).__name__ if not msg else "Message",
            chat.id if chat else None,
            chat.type if chat else None,
            user.id if user else None,
            user.username if user else None,
            user.is_bot if user else None,
            reply_to_id,
            reply_to_from,
            text_preview[:120],
        )
    except Exception:
        logger.exception("log_every_update failed")


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


async def handle_group_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route in-group replies back to the customer via Bot API.

    Triggered when the user (a human) replies to one of our relay messages
    inside the configured group. The MTProto listener handles Mira-bot
    replies separately.
    """
    msg = update.message
    if msg is None or msg.from_user is None:
        return
    if GROUP_CHAT_ID is None or msg.chat_id != GROUP_CHAT_ID:
        return
    if msg.from_user.id == context.bot.id:
        return

    reply_to = msg.reply_to_message
    if reply_to is None:
        return
    entry = _lookup_forward(reply_to.message_id)
    if entry is None or entry.get("answered"):
        return

    text_to_send = msg.text or msg.caption
    if not text_to_send:
        if not msg.from_user.is_bot:
            await msg.reply_text("❌ Por enquanto só dá pra responder com texto.")
        return

    source_label = "IA" if msg.from_user.is_bot else "você"

    try:
        await context.bot.send_message(
            chat_id=entry["chat_id"],
            text=text_to_send,
            business_connection_id=entry["business_connection_id"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to deliver reply to customer: %s", exc)
        await msg.reply_text(f"❌ Falha ao enviar: {exc}")
        return

    entry["answered"] = True
    _remember_forward(reply_to.message_id, entry)
    logger.info(
        "Delivered reply to customer chat=%s (forward %s, source=%s)",
        entry["chat_id"],
        reply_to.message_id,
        source_label,
    )

    try:
        await msg.reply_text(
            f"✅ Enviado para {entry['customer_name']} (por {source_label})"
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
# MTProto listener (Telethon) — sees Mira's replies in the group
# ---------------------------------------------------------------------------
def _mtproto_enabled() -> bool:
    return bool(MTPROTO_API_ID and MTPROTO_API_HASH and MTPROTO_SESSION_STRING)


async def _start_mtproto_listener(application: Application):
    """Start a Telethon client that watches GROUP_CHAT_ID for Mira's replies.

    Returns the running client, or None if MTProto is not configured.
    """
    if not _mtproto_enabled():
        logger.warning(
            "MTProto listener disabled — set TELEGRAM_API_ID, TELEGRAM_API_HASH "
            "and TELEGRAM_SESSION_STRING to enable Mira reply interception."
        )
        return None
    if GROUP_CHAT_ID is None:
        logger.warning("MTProto listener disabled — GROUP_CHAT_ID not set.")
        return None

    from telethon import TelegramClient, events
    from telethon.sessions import StringSession

    client = TelegramClient(
        StringSession(MTPROTO_SESSION_STRING), MTPROTO_API_ID, MTPROTO_API_HASH
    )
    bot_id = (await application.bot.get_me()).id

    @client.on(events.NewMessage(chats=GROUP_CHAT_ID))
    async def _on_group_message(event):  # noqa: ANN001
        try:
            msg = event.message
            if not msg.reply_to or not msg.reply_to.reply_to_msg_id:
                return  # Mira always uses Telegram-reply to our relay
            relay_id = msg.reply_to.reply_to_msg_id
            entry = _lookup_forward(relay_id)
            if entry is None or entry.get("answered"):
                return
            sender = await event.get_sender()
            if sender is None:
                return
            sender_is_bot = bool(getattr(sender, "bot", False))
            sender_id = getattr(sender, "id", None)
            if not sender_is_bot:
                return  # human replies are handled by the Bot API path
            if sender_id == bot_id:
                return  # never echo ourselves
            text = msg.text or msg.message
            if not text:
                return
            logger.info(
                "MTProto: bot %s replied to relay %s — forwarding to customer",
                sender_id,
                relay_id,
            )
            try:
                await application.bot.send_message(
                    chat_id=entry["chat_id"],
                    text=text,
                    business_connection_id=entry["business_connection_id"],
                )
            except Exception:
                logger.exception("MTProto: failed to forward Mira's reply")
                return
            entry["answered"] = True
            _remember_forward(relay_id, entry)
            try:
                await application.bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    text=f"✅ Enviado para {entry['customer_name']} (por IA)",
                    reply_to_message_id=relay_id,
                )
            except Exception:
                logger.exception("MTProto: failed to post confirmation in group")
        except Exception:
            logger.exception("MTProto handler crashed")

    await client.start()
    me = await client.get_me()
    logger.info(
        "MTProto listener active as %s (id=%s) for group %s",
        getattr(me, "username", None) or getattr(me, "first_name", "?"),
        getattr(me, "id", "?"),
        GROUP_CHAT_ID,
    )
    return client


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def _post_init(application: Application) -> None:
    """Log bot identity flags on startup so we can verify BotFather settings."""
    try:
        me = await application.bot.get_me()
        logger.info("Bot getMe: %s", me.to_dict())
    except Exception:
        logger.exception("post_init getMe failed")


def _build_application() -> Application:
    application = (
        ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(_post_init).build()
    )

    # /id works anywhere — DM, group, business chat.
    application.add_handler(CommandHandler("id", cmd_id))

    # Diagnostic: log every incoming update (does not block any handler).
    application.add_handler(TypeHandler(Update, log_every_update), group=-2)

    # Learn the business account owner's id from connection updates.
    application.add_handler(TypeHandler(Update, on_business_connection), group=-1)

    # Group 0: catch business messages (from a customer in a business chat).
    application.add_handler(
        MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_message),
        group=0,
    )

    # Group 1: handle in-group user replies (humans only).
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.REPLY, handle_group_message),
        group=1,
    )
    return application


async def _amain() -> None:
    application = _build_application()
    logger.info("Starting Secretary Bot polling loop...")
    if GROUP_CHAT_ID is None:
        logger.warning(
            "GROUP_CHAT_ID not set yet. Send /id inside your private group "
            "to discover it, then add it as a secret and restart."
        )
    else:
        logger.info("Relaying business messages to group %s", GROUP_CHAT_ID)

    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    mtproto_client = await _start_mtproto_listener(application)

    try:
        # Run until SIGINT/SIGTERM cancels us.
        if mtproto_client is not None:
            await mtproto_client.run_until_disconnected()
        else:
            await asyncio.Event().wait()
    finally:
        logger.info("Shutting down...")
        if mtproto_client is not None:
            try:
                await mtproto_client.disconnect()
            except Exception:
                logger.exception("Error disconnecting MTProto client")
        try:
            await application.updater.stop()
        except Exception:
            logger.exception("Error stopping updater")
        try:
            await application.stop()
        except Exception:
            logger.exception("Error stopping application")
        try:
            await application.shutdown()
        except Exception:
            logger.exception("Error during application shutdown")


def main() -> None:
    try:
        asyncio.run(_amain())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()

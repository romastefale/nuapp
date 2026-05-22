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
    """Route user replies in the group back to the customer.

    Two supported paths (user must use Telegram's *reply* feature):

    1. Reply to our own relayed message  →  send the user's typed text
       to the corresponding customer (manual / edited reply).
    2. Reply to Mira's (or any other bot's) message in the group  →
       send THAT bot's text to the customer, mapped to the oldest still
       unanswered forward. The user's own text in this reply is ignored
       (so a "ok" / "+" / "vai" is enough to confirm).

    Note: the Bot API never delivers messages authored by other bots
    directly to our bot, so we can't intercept Mira's reply on her own
    — the user has to "approve" it with a reply.
    """
    msg = update.message
    if msg is None or msg.from_user is None:
        return
    if GROUP_CHAT_ID is None or msg.chat_id != GROUP_CHAT_ID:
        return
    if msg.from_user.id == context.bot.id:
        return

    from_user = msg.from_user
    sender_is_bot = from_user.is_bot
    reply_to = msg.reply_to_message
    logger.info(
        "Group msg id=%s from=%s(@%s,bot=%s) reply_to_id=%s text=%r",
        msg.message_id,
        from_user.id,
        from_user.username,
        sender_is_bot,
        reply_to.message_id if reply_to else None,
        (msg.text or msg.caption or "")[:80],
    )

    target_group_msg_id: int | None = None
    entry: dict[str, Any] | None = None
    text_to_send: str | None = None
    source_label: str

    if reply_to is not None and _lookup_forward(reply_to.message_id) is not None:
        # Path A: reply to one of our relay messages.
        candidate = _lookup_forward(reply_to.message_id)
        assert candidate is not None
        if candidate.get("answered"):
            logger.info("Forward %s already answered — ignoring.", reply_to.message_id)
            return
        target_group_msg_id = reply_to.message_id
        entry = candidate
        text_to_send = msg.text or msg.caption
        source_label = "IA" if sender_is_bot else "você"
        if not text_to_send:
            if not sender_is_bot:
                await msg.reply_text("❌ Por enquanto só dá pra responder com texto.")
            return
    elif sender_is_bot:
        # Path B: Mira (or any other bot) posted in the group. Route her text
        # to the oldest still-unanswered forward. Works whether or not she
        # used Telegram's "reply" feature. Requires Bot-to-Bot Communication
        # Mode enabled on our bot in BotFather (plus admin rights or Group
        # Privacy OFF) so Telegram actually delivers her messages to us.
        pending = _oldest_unanswered_forward()
        if pending is None:
            logger.info("Bot %s posted in group but no pending forward — ignoring.", from_user.username)
            return
        target_group_msg_id, entry = pending
        text_to_send = msg.text or msg.caption
        source_label = "IA"
        if not text_to_send:
            logger.info("Bot %s posted without text — ignoring.", from_user.username)
            return
        logger.info(
            "Auto-forwarding bot %s's text for oldest pending forward %s.",
            from_user.username or from_user.id,
            target_group_msg_id,
        )
    else:
        # User wrote in the group but not as a reply to our relay — ignore.
        return

    assert entry is not None and target_group_msg_id is not None and text_to_send

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
    _remember_forward(target_group_msg_id, entry)
    logger.info(
        "Delivered reply to customer chat=%s (forward %s, source=%s)",
        entry["chat_id"],
        target_group_msg_id,
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
# Entry point
# ---------------------------------------------------------------------------
async def _post_init(application: Any) -> None:
    """Log bot identity flags on startup so we can verify BotFather settings."""
    try:
        me = await application.bot.get_me()
        logger.info("Bot getMe: %s", me.to_dict())
    except Exception:
        logger.exception("post_init getMe failed")


def main() -> None:
    application = (
        ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(_post_init).build()
    )

    # /id works anywhere — DM, group, business chat.
    application.add_handler(CommandHandler("id", cmd_id))

    # Diagnostic: log every incoming update (does not block any handler).
    application.add_handler(TypeHandler(Update, log_every_update), group=-2)

    # Learn the business account owner's id from connection updates.
    # Placed in its own dispatch group so it doesn't swallow other handlers.
    application.add_handler(TypeHandler(Update, on_business_connection), group=-1)

    # Group 0: catch business messages (sent from a counterparty inside a
    # business chat) and relay them to the configured group.
    application.add_handler(
        MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, handle_business_message),
        group=0,
    )

    # Group 1 (separate dispatch group): handle messages posted inside the
    # configured private group so they get routed back to the customer.
    # We do NOT filter by REPLY here because some bots (e.g. Mira) answer
    # with a plain message instead of using Telegram's reply feature.
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS, handle_group_message),
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

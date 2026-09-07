"""
Telegram bot entry point for Supermarket Ops Agent.

Architecture:
- One conversation_history dict per Telegram chat_id (in-memory within a process).
  The DB is the durable source of truth; history is only the current session context.
- Each incoming message → run_agent_turn() → send text reply + any document artifacts.
- Idempotency: Telegram update_id is used as the idempotency seed for bill operations.
- Allowed user IDs (optional): set ALLOWED_USER_IDS env var as comma-separated integers.
"""
import asyncio
import hashlib
import logging
import os
from collections import defaultdict

from dotenv import load_dotenv
from telegram import Update, Bot
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    ContextTypes, filters,
)

load_dotenv()

from db.migrations import run_migrations, seed_products
from agent.loop import run_agent_turn

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
ALLOWED_IDS_STR = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_IDS     = (
    {int(uid.strip()) for uid in ALLOWED_IDS_STR.split(",") if uid.strip()}
    if ALLOWED_IDS_STR else set()
)

# ── Per-chat conversation history ───────────────────────────────────────────────
# Keys: chat_id (int) → list of Gemini types.Content objects
conversation_histories: dict[int, list] = defaultdict(list)

# ── Processed update deduplication ──────────────────────────────────────────────
# Guard against Telegram re-delivering the same update (new update_id is assigned
# on each re-delivery, so we track the *message_id* which is stable for a given
# Telegram message, plus the chat_id as namespace).
#
# Cap at MAX_SEEN_IDS entries; once full, drop the oldest half (simple eviction).
# This is in-memory only — sufficient for Telegram's short re-delivery window
# (typically seconds to a few minutes); a process restart yields a clean slate
# which is safe since Telegram stops re-delivering after ACK.
MAX_SEEN_IDS = 10_000
_seen_message_ids: set[tuple[int, int]] = set()   # {(chat_id, message_id)}
_seen_message_ids_ordered: list[tuple[int, int]] = []


def _make_idem_key(update_id: int, suffix: str = "") -> str:
    """Derive a deterministic idempotency key from a Telegram update_id."""
    raw = f"tg:{update_id}:{suffix}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle every incoming text message."""
    chat_id    = update.effective_chat.id
    user_id    = update.effective_user.id if update.effective_user else None
    message    = update.message
    text       = message.text if message else None
    message_id = message.message_id if message else None

    if not text:
        return

    # ── Deduplication: drop Telegram re-deliveries ───────────────────────────
    # Telegram re-delivers updates with a *new* update_id but the same
    # message_id.  Keying on (chat_id, message_id) catches re-deliveries
    # without blocking legitimately distinct messages from the same chat.
    global _seen_message_ids, _seen_message_ids_ordered
    dedup_key = (chat_id, message_id)
    if dedup_key in _seen_message_ids:
        logger.warning(
            f"Duplicate update ignored: chat_id={chat_id} message_id={message_id} "
            f"update_id={update.update_id}"
        )
        return
    # Register before processing so a concurrent re-delivery is also dropped.
    _seen_message_ids.add(dedup_key)
    _seen_message_ids_ordered.append(dedup_key)
    # Evict oldest half when the set grows too large.
    if len(_seen_message_ids) > MAX_SEEN_IDS:
        evict = _seen_message_ids_ordered[:MAX_SEEN_IDS // 2]
        for k in evict:
            _seen_message_ids.discard(k)
        _seen_message_ids_ordered = _seen_message_ids_ordered[MAX_SEEN_IDS // 2:]

    # Access control
    if ALLOWED_IDS and user_id not in ALLOWED_IDS:
        logger.warning(f"Rejected message from user_id={user_id}")
        await update.message.reply_text("\u26d4 You are not authorised to use this bot.")
        return

    logger.info(f"Message from chat_id={chat_id} user={user_id}: {text[:80]}")

    # ── Inject deterministic idempotency key ─────────────────────────────────
    # The model is responsible for choosing idempotency keys when it calls
    # start_bill / finalize_bill.  Left to itself, the model invents keys that
    # differ across Telegram re-deliveries (new update_id → new key → new bill).
    #
    # We inject a [CONTEXT] prefix containing a stable key derived from
    # (chat_id, message_id) so the model has a deterministic anchor.  The
    # message_id is stable: Telegram assigns it once and re-delivers preserve it.
    idem_seed = _make_idem_key(message_id, suffix=str(chat_id))
    augmented_text = (
        f"[CONTEXT idem_key={idem_seed}]\n"
        f"Use idem_key above as the idempotency_key for start_bill and finalize_bill "
        f"in this turn.  Do NOT invent a different key.\n\n"
        f"{text}"
    )

    # Show typing indicator
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        result = run_agent_turn(
            user_message=augmented_text,
            conversation_history=conversation_histories[chat_id],
        )
    except Exception as exc:
        logger.error(f"Agent loop error for chat_id={chat_id}: {exc}", exc_info=True)
        await update.message.reply_text(
            "⚠️ Something went wrong on my end. Please try again."
        )
        return

    # Update conversation history
    conversation_histories[chat_id] = result["history"]

    # Send text reply (split if > 4096 chars — Telegram limit)
    reply = result["text"]
    if reply:
        for chunk_start in range(0, max(len(reply), 1), 4000):
            chunk = reply[chunk_start:chunk_start + 4000]
            await update.message.reply_text(chunk, parse_mode="Markdown")

    # Send any generated files (PDF invoice, PPTX deck)
    for artifact in result.get("artifacts", []):
        try:
            await context.bot.send_document(
                chat_id=chat_id,
                document=artifact["bytes"],
                filename=artifact["filename"],
                caption=f"📎 {artifact['filename']}",
            )
        except Exception as e:
            logger.error(f"Failed to send artifact {artifact['filename']}: {e}")
            await update.message.reply_text(
                f"⚠️ Generated {artifact['filename']} but couldn't send it: {e}"
            )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    await update.message.reply_text(
        "🛒 *Supermarket Ops Agent* ready!\n\n"
        "I manage your kirana store — billing, stock, khata, invoices, analysis.\n"
        "Just type naturally, like:\n"
        "• `got 50 packs maggi today`\n"
        "• `make bill: 2 amul butter, 1 tata salt`\n"
        "• `ramu paid 500 rupees`\n"
        "• `day close`\n"
        "• `send me analysis deck for this week`",
        parse_mode="Markdown",
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear conversation history for this chat (doesn't affect DB)."""
    chat_id = update.effective_chat.id
    conversation_histories[chat_id] = []
    await update.message.reply_text("🔄 Conversation reset. DB data is intact.")


def main():
    # Run DB migrations + seed on startup
    run_migrations()
    seed_products()

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("reset",  cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started — polling for updates.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

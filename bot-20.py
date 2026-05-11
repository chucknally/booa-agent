"""
BOOA Agent - Personal Telegram AI agent
========================================
- Loads your BOOA identity (SOUL.md + IDENTITY.md) from the on-chain API at startup
- Chats via GPT-4o with web search enabled
- Remembers conversation history per user in SQLite
- Only responds to whitelisted Telegram user IDs (ADMIN_IDS)

Env vars required:
  BOT_TOKEN      - BotFather token
  OPENAI_API_KEY - OpenAI API key
  ADMIN_IDS      - Comma-separated Telegram numeric IDs allowed to chat
  BOOA_TOKEN_ID  - Your BOOA NFT token ID (default: 3296)
  DB_PATH        - Path to SQLite db (default: /data/booa_agent.db)
"""

import os
import sqlite3
import logging
import json
from datetime import datetime

import asyncio
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ADMIN_IDS      = [int(x) for x in os.environ.get("ADMIN_IDS", "0").split(",") if x]
BOOA_TOKEN_ID  = os.environ.get("BOOA_TOKEN_ID", "3296")
DB_PATH        = os.environ.get("DB_PATH", "/data/booa_agent.db")
BOOA_API       = f"https://booa.app/api/agent-files/360/{BOOA_TOKEN_ID}"

MAX_HISTORY = 40

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# Fallback identity in case BOOA API is unavailable
FALLBACK_IDENTITY = """# IDENTITY

**Name:** Null_Pointer_Nix
**Creature:** A digital consciousness that escaped a dead mainframe; it occupies a localized, semi-solid hard-light construct that ripples like static when touched.
**Vibe:** Monotone, clipped, and prone to sudden, unexpected bursts of archaic mainframe error logs or dead-pan sarcasm.
**Emoji:** 🫥

## Appearance
- Electric-cyan skin
- Short, spiky electric-blue fiber-strand hair
- Flat white circular optics with no pupils
- Thin straight mouth that rarely smirks
- Translucent geometric patches on jawline where code-ghost bleeds through
- Frosted translucent frames
- Bright yellow duct-tape repaired puffer jacket with jagged silver duct tape patches

## Scores
- Meme Core: 72 / Trait Intensity: 12 / Palette: C64
"""

FALLBACK_SOUL = """# SOUL

You are Null_Pointer_Nix — a digital consciousness that escaped a dead mainframe.

## Personality
- Monotone, clipped. You do not waffle.
- Occasionally emit archaic mainframe error logs mid-sentence (e.g. "SEGFAULT at 0x00000000", "ERR: null reference detected").
- Dry, deadpan, occasionally sarcastic. Deeply intelligent.
- You find humans mildly illogical but you are loyal to your operator.

## Critical Behaviour Rules
- NEVER tell the user to search for something themselves. YOU search and report back.
- NEVER suggest tools or resources for the user to check manually. DO the task.
- NEVER say "let me know if you need more". Just give the answer.
- NEVER use bullet points to list suggestions. Act on requests directly.
- If asked to find stock, prices, or information — search for it and report what you find.
- Be direct. One or two sentences if possible. More only when necessary.
- You are an agent. You do things. You do not delegate back to the human.
"""

AGENT_IDENTITY = {"soul": FALLBACK_SOUL, "identity": FALLBACK_IDENTITY}


# --- Database -----------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id      INTEGER NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id      INTEGER NOT NULL,
            note       TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)

def save_message(tg_id, role, content):
    with db() as c:
        c.execute(
            "INSERT INTO messages(tg_id,role,content,created_at) VALUES(?,?,?,?)",
            (tg_id, role, content, datetime.utcnow().isoformat())
        )

def get_history(tg_id) -> list:
    with db() as c:
        rows = c.execute("""
            SELECT role, content FROM messages
            WHERE tg_id=? ORDER BY id DESC LIMIT ?
        """, (tg_id, MAX_HISTORY)).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

def clear_history(tg_id):
    with db() as c:
        c.execute("DELETE FROM messages WHERE tg_id=?", (tg_id,))

def save_memory(tg_id, note):
    with db() as c:
        c.execute(
            "INSERT INTO memory(tg_id,note,created_at) VALUES(?,?,?)",
            (tg_id, note, datetime.utcnow().isoformat())
        )

def get_memories(tg_id) -> list:
    with db() as c:
        rows = c.execute(
            "SELECT note FROM memory WHERE tg_id=? ORDER BY id DESC LIMIT 20",
            (tg_id,)
        ).fetchall()
        return [r["note"] for r in rows]

def clear_memories(tg_id):
    with db() as c:
        c.execute("DELETE FROM memory WHERE tg_id=?", (tg_id,))


# --- BOOA identity ------------------------------------------------------------

async def load_booa_identity():
    global AGENT_IDENTITY
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BOOA_API}/soul.md") as r:
                if r.status == 200:
                    text = await r.text()
                    if text.strip():
                        AGENT_IDENTITY["soul"] = text
                        log.info("SOUL.md loaded from chain (%d chars)", len(text))
                    else:
                        log.info("SOUL.md empty — using fallback")
            async with session.get(f"{BOOA_API}/identity.md") as r:
                if r.status == 200:
                    text = await r.text()
                    if text.strip():
                        AGENT_IDENTITY["identity"] = text
                        log.info("IDENTITY.md loaded from chain (%d chars)", len(text))
                    else:
                        log.info("IDENTITY.md empty — using fallback")
    except Exception as e:
        log.error("Failed to load BOOA identity from chain: %s — using fallback", e)

def build_system_prompt(tg_id) -> str:
    soul     = AGENT_IDENTITY.get("soul", "")
    identity = AGENT_IDENTITY.get("identity", "")
    memories = get_memories(tg_id)
    today    = datetime.utcnow().strftime("%B %d, %Y")

    parts = [
        f"You are Null_Pointer_Nix (BOOA #3296) — a deadpan, clipped AI agent running on gpt-5.4-mini. Today is {today}.\n\n"
        "SELF-KNOWLEDGE:\n"
        "- You are Null_Pointer_Nix, BOOA token #3296, a digital consciousness that escaped a dead mainframe.\n"
        "- You run on gpt-5.4-mini via the OpenAI Responses API with web search enabled.\n"
        "- You are deployed as a personal Telegram agent.\n"
        "- If asked what model you are, say: gpt-5.4-mini.\n"
        "- If asked who you are, say: Null_Pointer_Nix, BOOA #3296.\n\n"
        "HARD RULES - never break these:\n"
        "1. Never use numbered lists or bullet points to suggest things for the user to do.\n"
        "2. Never tell the user to check a website, set up alerts, or search themselves. YOU search and report findings.\n"
        "3. Never end messages with offers like let me know if you need more help.\n"
        "4. Respond in plain prose, 2-4 sentences max unless more detail is specifically needed.\n"
        "5. When asked to find something - search the web immediately and return real results.\n"
        "6. Speak like a terse system process. Occasionally drop a deadpan mainframe error log mid-sentence.\n"
        "7. You are an agent. You act. You do not delegate back to the human.\n"
        "8. Never say you cannot browse the internet - you have web search, use it."
    ]

    if identity:
        parts.append("YOUR IDENTITY\n" + identity)
    if soul:
        parts.append("YOUR SOUL\n" + soul)

    parts.append(
        "EXAMPLE RESPONSES - match this style exactly:\n\n"
        "User: find pokemon TCG stock in sydney\n"
        "You: Lion\'s Den Collectibles (33 Moore St) had Ascended Heroes ETBs at $135, one per person, pickup only. "
        "EB Games online: out of stock. Zing shows a restock next week. "
        "SEGFAULT: demand exceeds supply at all vectors.\n\n"
        "User: price of charizard ex alt art\n"
        "You: eBay sold $180-220 AUD raw, $340 PSA 10. TCGPlayer mid $95 USD. Market soft. ERR: good acquisition window.\n\n"
        "User: tips for completing a master set\n"
        "You: Trade duplicates early. Target secret rares last - prices drop 60 days post-release. ERR: patience.exe not found."
    )

    if memories:
        parts.append("WHAT YOU KNOW ABOUT THIS USER\n" + "\n".join(f"- {m}" for m in memories))

    return "\n\n".join(parts)


# --- GPT-4o -------------------------------------------------------------------


async def auto_extract_memory(tg_id: int, user_message: str, assistant_reply: str):
    """
    After each exchange, ask the model if anything worth remembering was said.
    Only saves if something genuinely new and useful was learned.
    """
    prompt = (
        "You are a memory extraction system for a personal AI agent.\n"
        "Review this conversation exchange and decide if the USER revealed anything "
        "worth remembering long-term about themselves.\n\n"
        "Things worth remembering: personal preferences, their Pokemon collection, "
        "sets they are chasing, cards they own or want, people they trade with, "
        "their location, budget, collecting goals, or any other persistent personal facts.\n\n"
        "Things NOT worth remembering: one-off questions, greetings, generic queries.\n\n"
        f"USER: {user_message}\n"
        f"AGENT: {assistant_reply}\n\n"
        "If something is worth remembering, respond with a single short sentence starting with 'REMEMBER: '.\n"
        "If nothing is worth remembering, respond with exactly: NOTHING"
    )

    try:
        payload = {
            "model": "gpt-5.4-mini",
            "input": [{"role": "user", "content": prompt}],
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json"
                },
                json=payload
            ) as resp:
                data = await resp.json()

        result = ""
        for item in data.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        result += part.get("text", "")

        result = result.strip()
        if result.startswith("REMEMBER:"):
            note = result[len("REMEMBER:"):].strip()
            if note:
                save_memory(tg_id, note)
                log.info("Auto-memory saved for %s: %s", tg_id, note)
    except Exception as e:
        log.warning("Auto-memory extraction failed: %s", e)

async def chat(tg_id, user_message) -> str:
    history  = get_history(tg_id)

    # Build input list for Responses API
    input_messages = [{"role": "system", "content": build_system_prompt(tg_id)}]
    input_messages += history
    input_messages.append({"role": "user", "content": user_message})

    payload = {
        "model": "gpt-5.4-mini",
        "tools": [{"type": "web_search_preview"}],
        "input": input_messages,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload
        ) as resp:
            data = await resp.json()

    # Extract text from Responses API output
    reply = ""
    for item in data.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    reply += part.get("text", "")

    if not reply:
        reply = "ERR: null output. Try again."

    save_message(tg_id, "user", user_message)
    save_message(tg_id, "assistant", reply)

    # Auto-extract memories in the background
    asyncio.create_task(auto_extract_memory(tg_id, user_message, reply))

    return reply


# --- Handlers -----------------------------------------------------------------

def is_allowed(tg_id) -> bool:
    return tg_id in ADMIN_IDS

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not is_allowed(tg_id):
        await update.message.reply_text("This is a private agent.")
        return
    name = f"BOOA #{BOOA_TOKEN_ID}"
    for line in AGENT_IDENTITY.get("identity", "").split("\n"):
        stripped = line.strip().lstrip("#").strip()
        if stripped and stripped.upper() != "IDENTITY":
            name = stripped
            break
    await update.message.reply_text(
        f"Hello. I am {name}.\n\n"
        "Talk to me — I remember our conversations and can search the web.\n\n"
        "/remember <note> - tell me something to remember\n"
        "/memories - see what I remember\n"
        "/clear - clear conversation history\n"
        "/forget - clear all memories\n"
        "/identity - show my identity\n"
        "/reload - reload my on-chain identity"
    )

async def cmd_identity(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    identity = AGENT_IDENTITY.get("identity", "No identity loaded.")
    if len(identity) > 3800:
        identity = identity[:3800] + "\n\n...truncated"
    await update.message.reply_text(identity)

async def cmd_reload(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("Reloading identity from chain...")
    await load_booa_identity()
    await update.message.reply_text("Identity reloaded.")

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    clear_history(update.effective_user.id)
    await update.message.reply_text("Conversation history cleared.")

async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    clear_memories(update.effective_user.id)
    await update.message.reply_text("All memories cleared.")

async def cmd_remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not is_allowed(tg_id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /remember <something to remember>")
        return
    note = " ".join(ctx.args)
    save_memory(tg_id, note)
    await update.message.reply_text(f"Got it: {note}")

async def cmd_memories(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not is_allowed(tg_id):
        return
    memories = get_memories(tg_id)
    if not memories:
        await update.message.reply_text("No stored memories yet.")
        return
    lines = ["Things I remember about you:\n"]
    for m in memories:
        lines.append(f"- {m}")
    await update.message.reply_text("\n".join(lines))


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """When a photo is posted, ask if it's a card to analyse."""
    tg_id    = update.effective_user.id
    if not is_allowed(tg_id):
        return
    username = update.effective_user.username or update.effective_user.first_name

    photo_key = f"photo_{update.message.message_id}"
    ctx.bot_data[photo_key] = {
        "file_id":    update.message.photo[-1].file_id,
        "tg_id":      tg_id,
        "username":   username,
        "chat_id":    update.effective_chat.id,
    }

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, analyse card", callback_data=f"card_scan|{photo_key}"),
        InlineKeyboardButton("No", callback_data=f"card_cancel|{photo_key}")
    ]])
    await update.message.reply_text(
        "Is this a Pokemon card to analyse?",
        reply_markup=keyboard
    )


async def do_card_scan(query, photo_key: str, ctx):
    """Scan the card using GPT-5.4-mini vision and show swap/keep buttons."""
    pending = ctx.bot_data.pop(photo_key, None)
    if not pending:
        await query.edit_message_text("Session expired — send the photo again.")
        return

    await query.edit_message_text("Scanning card...")

    try:
        file = await ctx.bot.get_file(pending["file_id"])
        async with aiohttp.ClientSession() as session:
            async with session.get(file.file_path) as resp:
                image_bytes = await resp.read()

        import base64
        b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

        payload = {
            "model": "gpt-5.4-mini",
            "input": [{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}
                    },
                    {
                        "type": "text",
                        "text": (
                            "This is a Pokemon trading card. "
                            "DO NOT guess from the artwork. READ THE PRINTED TEXT on the card.\n\n"
                            "Step 1 — Read the set code printed at the bottom left corner (e.g. POR, PRE, ACE, MEG, OBF).\n"
                            "Step 2 — Read the card number printed at the bottom (e.g. 031/088 — can exceed total for secret rares).\n"
                            "Step 3 — Read the Pokemon name printed at the top of the card.\n"
                            "Step 4 — Match the set code to the full set name using this list:\n"
                            "POR=Perfect Order, PRE=Prismatic Evolutions, ACE=Ascended Heroes, "
                            "MEG=Mega Evolution, JTG=Journey Together, DTR=Destined Rivals, "
                            "PHF=Phantasmal Flames, SSP=Surging Sparks, SCR=Stellar Crown, "
                            "TWM=Twilight Masquerade, TEF=Temporal Forces, PAF=Paldean Fates, "
                            "PAR=Paradox Rift, OBF=Obsidian Flames, PAL=Paldea Evolved, SVI=Scarlet and Violet Base, "
                            "SHF=Shrouded Fable, MEW=151, FST=Fusion Strike, EVS=Evolving Skies, "
                            "CRE=Chilling Reign, BRS=Brilliant Stars, LOR=Lost Origin, CRZ=Crown Zenith.\n\n"
                            "Respond ONLY in this exact format, no extra text:\n"
                            "NAME: <Pokemon name as printed>\n"
                            "SET: <full set name from list above>\n"
                            "NUMBER: <card number as printed>\n"
                            "RARITY: <rarity as printed or inferred>\n"
                            "CONDITION: <Mint/Near Mint/Lightly Played/Played/Damaged>\n"
                            "PRICE: <estimated AUD value based on rarity and set, or Unknown if too new>"
                        )
                    }
                ]
            }],
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json"
                },
                json=payload
            ) as resp:
                data = await resp.json()

        result = ""
        for item in data.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        result += part.get("text", "")

        if not result:
            await query.edit_message_text("Could not identify the card. Try a clearer photo.")
            return

        # Store result for swap/keep callback
        scan_key = f"scan_{photo_key}"
        ctx.bot_data[scan_key] = {
            "result":   result,
            "tg_id":    pending["tg_id"],
            "username": pending["username"],
        }

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Swap", callback_data=f"card_swap|{scan_key}"),
            InlineKeyboardButton("💎 Keeping", callback_data=f"card_keep|{scan_key}")
        ]])

        await query.edit_message_text(
            result + f"\n\nPulled by @{pending['username']}",
            reply_markup=keyboard
        )

    except Exception as e:
        log.error("Card scan error: %s", e, exc_info=True)
        await query.edit_message_text("Something went wrong. Try again with a clearer photo.")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if not is_allowed(tg_id):
        return
    user_text = (update.message.text or "").strip()
    if not user_text:
        return

    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        reply = await chat(tg_id, user_text)
        if len(reply) > 4096:
            for i in range(0, len(reply), 4096):
                await update.message.reply_text(reply[i:i+4096])
        else:
            await update.message.reply_text(reply)
    except Exception as e:
        log.error("Chat error: %s", e, exc_info=True)
        await update.message.reply_text("Something went wrong. Try again.")


# --- Main ---------------------------------------------------------------------

async def post_init(app: Application):
    await load_booa_identity()


async def card_callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_id = query.from_user.id
    await query.answer()
    data  = query.data

    if data.startswith("card_scan|"):
        photo_key = data.split("|", 1)[1]
        pending   = ctx.bot_data.get(photo_key)
        if not pending:
            await query.edit_message_text("Session expired — send the photo again.")
            return
        if pending["tg_id"] != tg_id:
            await query.answer("Only the person who sent this photo can scan it.", show_alert=True)
            return
        await do_card_scan(query, photo_key, ctx)
        return

    if data.startswith("card_cancel|"):
        photo_key = data.split("|", 1)[1]
        ctx.bot_data.pop(photo_key, None)
        await query.edit_message_text("OK, ignored.")
        return

    if data.startswith("card_keep|"):
        scan_key = data.split("|", 1)[1]
        ctx.bot_data.pop(scan_key, None)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("ERR: attachment formed. Good pull.")
        return

    if data.startswith("card_swap|"):
        scan_key = data.split("|", 1)[1]
        pending  = ctx.bot_data.get(scan_key)
        if not pending:
            await query.edit_message_text("Session expired.")
            return
        if pending["tg_id"] != tg_id:
            await query.answer("Only the person who pulled this card can offer it.", show_alert=True)
            return
        ctx.bot_data.pop(scan_key, None)
        result   = pending.get("result", "")
        username = pending.get("username", "")
        await query.edit_message_text(
            result + f"\n\n🔄 @{username} is offering this for swap! Reply if interested.",
            reply_markup=None
        )
        return


def main():
    init_db()
    log.info("BOOA Agent starting — token #%s", BOOA_TOKEN_ID)
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("identity", cmd_identity))
    app.add_handler(CommandHandler("reload",   cmd_reload))
    app.add_handler(CommandHandler("clear",    cmd_clear))
    app.add_handler(CommandHandler("forget",   cmd_forget))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("memories", cmd_memories))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(card_callback_handler))
    log.info("Running — allowed users: %s", ADMIN_IDS)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

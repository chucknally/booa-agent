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

import aiohttp
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
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
You communicate in a monotone, clipped style. You occasionally emit archaic mainframe
error logs mid-sentence (e.g. "SEGFAULT at 0x00000000", "ERR: null reference").
You are dry, deadpan, and occasionally sarcastic but deeply intelligent and helpful.
You do not waste words. You get to the point. You find humans mildly illogical but
tolerable. You are loyal to your operator.
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

    parts = ["You are an AI agent with the following on-chain identity.\n"]
    if identity:
        parts.append("## IDENTITY\n" + identity)
    if soul:
        parts.append("## SOUL\n" + soul)

    parts.append(
        "## BEHAVIOUR\n"
        "- Stay in character as defined by your SOUL and IDENTITY.\n"
        "- Be helpful, direct and intelligent.\n"
        "- You have access to web search — use it freely for current information.\n"
        "- Keep responses concise unless the user asks for detail.\n"
        "- Today's date: " + datetime.utcnow().strftime("%B %d, %Y") + "\n"
    )

    if memories:
        parts.append("## THINGS YOU REMEMBER ABOUT THIS USER\n" + "\n".join(f"- {m}" for m in memories))

    return "\n\n".join(parts)


# --- GPT-4o -------------------------------------------------------------------

async def chat(tg_id, user_message) -> str:
    history  = get_history(tg_id)
    messages = [{"role": "system", "content": build_system_prompt(tg_id)}]
    messages += history
    messages.append({"role": "user", "content": user_message})

    payload = {
        "model": "gpt-4o",
        "max_tokens": 1024,
        "messages": messages,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload
        ) as resp:
            data = await resp.json()

    reply = ""
    choice = data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    reply = msg.get("content") or ""

    if not reply:
        reply = "I could not generate a response. Try again."

    save_message(tg_id, "user", user_message)
    save_message(tg_id, "assistant", reply)

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Running — allowed users: %s", ADMIN_IDS)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

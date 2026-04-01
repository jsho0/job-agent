#!/usr/bin/env python3
"""
Job Scout Discord Bot - persistent process managed by systemd
Watches for reactions on job alert messages and triggers tailoring
"""

import os
import asyncio
import subprocess
import logging
import sqlite3
import tempfile
from datetime import datetime, timezone
from dotenv import load_dotenv
import discord

try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

load_dotenv()

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
REQUESTS_CHANNEL_ID = int(os.getenv("REQUESTS_CHANNEL_ID", "0"))
DISCORD_USER_ID = int(os.getenv("DISCORD_USER_ID", "0"))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(SCRIPT_DIR, "venv", "bin", "python")
DB_PATH = os.path.join(SCRIPT_DIR, "jobs.db")
INGEST_URL = os.getenv("INGEST_URL", "https://job-agent-henna.vercel.app")
INGEST_API_KEY = os.getenv("INGEST_API_KEY")
MEMORY_REPO_PATH = os.getenv("MEMORY_REPO_PATH")  # local clone of job-agent-memory repo

STATUSES = ["applied", "phone_screen", "interview", "offer", "rejected", "withdrawn"]

STATUS_EMOJI = {
    "applied": "📨",
    "phone_screen": "📞",
    "interview": "🎯",
    "offer": "🎉",
    "rejected": "❌",
    "withdrawn": "🚫",
}

HELP_TEXT = """**Job Scout Commands:**
`!applied <job_url> <Company> - <Role>` — log a new application
`!status <job_url> <status>` — update status (phone_screen, interview, offer, rejected, withdrawn)
`!tracker` — show your full application pipeline
`!stats` — show application stats + rejection patterns
`!note <job_url> <note>` — add a note to an application
`!help` — show this message

**Reactions on job alerts:**
✅ — generate tailored resume + cover letter
📨 — mark as applied
❌ — dismiss (bot will ask why, helps it learn your preferences)

**#requests channel:**
Post a job URL or PDF and the bot will ask what you want.
Then reply with your instructions, for example:
  "tailor my resume and cover letter"
  "tailor everything, emphasize my Python automation experience"
  "just the cover letter, startup tone"
Reply `skip` to use defaults."""

# Tracks pending rejection reason requests
# When user reacts ❌, bot asks for reason and waits for next message
pending_rejections = {}

# Tracks pending #requests jobs waiting for user's instructions
# user_id -> {"job_url": str|None, "jd_file": str|None, "label": str}
pending_requests = {}

logging.basicConfig(
    filename="/var/log/jobscout_bot.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True

client = discord.Client(intents=intents)


# ── DB helpers ────────────────────────────────────────────────────────────────

def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_url TEXT,
            title TEXT,
            company TEXT,
            reason TEXT,
            date_rejected TEXT
        )
    """)
    conn.commit()
    return conn


def log_application(job_url, title, company, notes=""):
    conn = db_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO applications (job_url, title, company, status, date_added, notes)
           VALUES (?, ?, ?, 'applied', ?, ?)""",
        (job_url, title, company, now, notes)
    )
    conn.commit()
    conn.close()


def log_rejection(job_url, title, company, reason):
    conn = db_conn()
    conn.execute(
        "INSERT INTO rejections (job_url, title, company, reason, date_rejected) VALUES (?, ?, ?, ?, ?)",
        (job_url, title, company, reason, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()


def get_recent_rejections(limit=20):
    conn = db_conn()
    rows = conn.execute(
        "SELECT title, company, reason FROM rejections WHERE reason IS NOT NULL AND reason != '' ORDER BY date_rejected DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return rows


def update_status(job_url, status):
    conn = db_conn()
    cur = conn.execute("UPDATE applications SET status = ? WHERE job_url = ?", (status, job_url))
    updated = cur.rowcount
    conn.commit()
    conn.close()
    return updated > 0


def add_note(job_url, note):
    conn = db_conn()
    cur = conn.execute("UPDATE applications SET notes = ? WHERE job_url = ?", (note, job_url))
    updated = cur.rowcount
    conn.commit()
    conn.close()
    return updated > 0


def get_tracker():
    conn = db_conn()
    rows = conn.execute(
        "SELECT title, company, status, date_added, notes, job_url FROM applications ORDER BY date_added DESC"
    ).fetchall()
    conn.close()
    return rows


def get_stats():
    conn = db_conn()
    app_rows = conn.execute("SELECT status, COUNT(*) FROM applications GROUP BY status").fetchall()
    rejection_count = conn.execute("SELECT COUNT(*) FROM rejections").fetchone()[0]
    top_reasons = conn.execute(
        """SELECT reason, COUNT(*) as c FROM rejections
           WHERE reason IS NOT NULL AND reason != ''
           GROUP BY reason ORDER BY c DESC LIMIT 5"""
    ).fetchall()
    conn.close()
    return dict(app_rows), rejection_count, top_reasons


# ── Helpers ───────────────────────────────────────────────────────────────────

import re as _re
import requests as _requests

def sync_to_web_ui(job_url, status, title="", company="", description=""):
    """Mirror a status change to the web UI's Supabase database."""
    if not INGEST_API_KEY or not job_url:
        return
    try:
        base = INGEST_URL.rstrip("/")
        resp = _requests.post(
            f"{base}/api/jobs/sync",
            json={"url": job_url, "status": status, "title": title, "company": company, "description": description},
            headers={"x-api-key": INGEST_API_KEY},
            timeout=8,
        )
        logging.info(f"Web UI sync: {status} for {job_url} → {resp.status_code}")
    except Exception as e:
        logging.warning(f"sync_to_web_ui error: {e}")


def _write_rejection_to_vault(title, company, reason):
    """Append a dismissal row to job-agent-memory repo and push. Runs in executor (non-blocking)."""
    if not MEMORY_REPO_PATH:
        return
    md_path = os.path.join(MEMORY_REPO_PATH, "rejection-patterns.md")
    if not os.path.exists(md_path):
        logging.warning(f"Vault rejection-patterns.md not found at {md_path}")
        return
    try:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = f"| {date} | {title} | {company} | {reason} |\n"
        with open(md_path, "a", encoding="utf-8") as f:
            f.write(row)
        subprocess.run(["git", "-C", MEMORY_REPO_PATH, "add", "rejection-patterns.md"], check=True, capture_output=True)
        subprocess.run(["git", "-C", MEMORY_REPO_PATH, "commit", "-m", f"bot: dismissed {company} ({reason[:50]})"], check=True, capture_output=True)
        subprocess.run(["git", "-C", MEMORY_REPO_PATH, "push"], check=True, capture_output=True)
        logging.info(f"Vault updated: dismissed {title} @ {company}")
    except Exception as e:
        logging.warning(f"Vault write failed: {e}")


def _extract_job_info(message, channel_id):
    """Extract (job_url, title, company) from an embed or plain message."""
    # Try embeds first (job alert channel)
    if message.embeds:
        embed = message.embeds[0]
        job_url = embed.url or ""
        title, company = "Unknown Role", "Unknown Company"
        if embed.title and " @ " in embed.title:
            title, company = embed.title.split(" @ ", 1)
        elif embed.title:
            title = embed.title
        return job_url, title, company

    # Plain text (requests channel) -- extract URL
    if channel_id == REQUESTS_CHANNEL_ID and message.content:
        url_match = _re.search(r'https?://\S+', message.content)
        if url_match:
            return url_match.group(0), "Role (from #requests)", "Company"

    return None, "Unknown Role", "Unknown Company"


# ── Discord events ────────────────────────────────────────────────────────────

@client.event
async def on_ready():
    logging.info(f"Bot online as {client.user}")
    print(f"Job Scout bot online as {client.user}")


@client.event
async def on_raw_reaction_add(payload):
    if payload.user_id == client.user.id:
        return
    if payload.channel_id not in (CHANNEL_ID, REQUESTS_CHANNEL_ID):
        return

    emoji = str(payload.emoji)
    channel = client.get_channel(payload.channel_id)
    if not channel:
        channel = await client.fetch_channel(payload.channel_id)

    message = await channel.fetch_message(payload.message_id)

    # ── Tailor resume ──────────────────────────────────────────────────────
    if emoji == "✅":
        job_url = None
        role_name = "this role"
        custom_prompt = ""

        # #requests channel: handled conversationally via on_message now
        if payload.channel_id == REQUESTS_CHANNEL_ID:
            await channel.send(
                f"<@{payload.user_id}> Just post a URL or PDF in this channel and I'll ask what you need."
            )
            return
        else:
            # Job alerts channel: embeds from scrapers
            if not message.embeds:
                return
            embed = message.embeds[0]
            job_url = embed.url
            role_name = embed.title or "this role"

        if not job_url:
            await channel.send(f"<@{payload.user_id}> Could not find the job URL in that post.")
            return

        await channel.send(
            f"<@{payload.user_id}> On it! Generating tailored resume and cover letter for:\n"
            f"> **{role_name}**\n\nGive me 30-60 seconds..."
        )
        logging.info(f"Tailoring triggered for: {job_url} (custom_prompt: {custom_prompt[:100]})")

        # Build command args
        tailor_cmd = [VENV_PYTHON, os.path.join(SCRIPT_DIR, "tailor.py"), job_url, str(payload.channel_id)]
        if custom_prompt:
            tailor_cmd.extend(["--prompt", custom_prompt])

        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    tailor_cmd,
                    capture_output=True, text=True, timeout=180
                )
            )
            if result.returncode != 0:
                logging.error(f"tailor.py stderr: {result.stderr}")
                await channel.send(f"<@{payload.user_id}> Something went wrong. Check `/var/log/jobscout_tailor.log`")
        except subprocess.TimeoutExpired:
            await channel.send(f"<@{payload.user_id}> Tailoring timed out. Try again.")
        except Exception as e:
            logging.error(f"Tailor error: {e}")
            await channel.send(f"<@{payload.user_id}> Unexpected error: {e}")

    # ── Mark applied ───────────────────────────────────────────────────────
    elif emoji == "📨":
        job_url, title, company = _extract_job_info(message, payload.channel_id)
        if not job_url:
            return
        # Extract description from embed if available
        description = ""
        if message.embeds and message.embeds[0].description:
            description = message.embeds[0].description
        log_application(job_url, title.strip(), company.strip())
        sync_to_web_ui(job_url, "applied", title.strip(), company.strip(), description)
        await channel.send(
            f"<@{payload.user_id}> 📨 Logged application!\n"
            f"> **{title.strip()}** at **{company.strip()}**\n"
            f"Use `!status <url> phone_screen` when you hear back."
        )
        logging.info(f"Application logged: {title} @ {company}")

    # ── Dismiss + learn ────────────────────────────────────────────────────
    elif emoji == "❌":
        job_url, title, company = _extract_job_info(message, payload.channel_id)
        if not job_url:
            await channel.send(f"<@{payload.user_id}> Dismissed.")
            return

        # Sync to web UI immediately (before asking for reason)
        description = ""
        if message.embeds and message.embeds[0].description:
            description = message.embeds[0].description
        sync_to_web_ui(job_url, "dismissed", title.strip(), company.strip(), description)

        # Ask for reason, store pending state
        prompt_msg = await channel.send(
            f"<@{payload.user_id}> Dismissed **{title.strip()}** at **{company.strip()}**.\n"
            f"Why are you passing? Reply with a reason (or `skip` to skip). This helps me learn your preferences."
        )

        pending_rejections[payload.user_id] = {
            "job_url": job_url,
            "title": title.strip(),
            "company": company.strip(),
            "prompt_msg_id": prompt_msg.id,
        }
        logging.info(f"Rejection pending for: {title} @ {company}")


async def _extract_pdf_text(attachment) -> str | None:
    """Download a PDF attachment and extract its text."""
    if not HAS_PYPDF:
        return None
    try:
        data = await attachment.read()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(data)
            tmp_path = f.name
        reader = PdfReader(tmp_path)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        os.unlink(tmp_path)
        return text.strip() or None
    except Exception as e:
        logging.warning(f"PDF extraction failed: {e}")
        return None


async def _run_tailor(channel, user_id, job_url, custom_prompt, jd_file=None):
    """Invoke tailor.py as a subprocess and handle result."""
    tailor_cmd = [
        VENV_PYTHON,
        os.path.join(SCRIPT_DIR, "tailor.py"),
        job_url or "no-url",
        str(channel.id),
    ]
    if custom_prompt:
        tailor_cmd.extend(["--prompt", custom_prompt])
    if jd_file:
        tailor_cmd.extend(["--jd-file", jd_file])

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(tailor_cmd, capture_output=True, text=True, timeout=180),
        )
        if result.returncode != 0:
            logging.error(f"tailor.py stderr: {result.stderr}")
            await channel.send(f"<@{user_id}> Something went wrong generating your documents.")
    except subprocess.TimeoutExpired:
        await channel.send(f"<@{user_id}> Tailoring timed out. Try again.")
    except Exception as e:
        logging.error(f"Tailor error: {e}")
        await channel.send(f"<@{user_id}> Unexpected error: {e}")
    finally:
        if jd_file and os.path.exists(jd_file):
            os.unlink(jd_file)


@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if message.channel.id not in (CHANNEL_ID, REQUESTS_CHANNEL_ID):
        return

    content = message.content.strip()

    # ── Handle pending rejection reason ───────────────────────────────────
    if message.author.id in pending_rejections and not content.startswith("!"):
        pending = pending_rejections.pop(message.author.id)
        reason = "" if content.lower() == "skip" else content

        log_rejection(
            pending["job_url"],
            pending["title"],
            pending["company"],
            reason
        )

        if reason:
            await message.channel.send(
                f"Got it. Noted: \"{reason}\"\nI'll use this to improve future recommendations."
            )
            loop = asyncio.get_event_loop()
            loop.run_in_executor(
                None,
                lambda: _write_rejection_to_vault(pending["title"], pending["company"], reason)
            )
        else:
            await message.channel.send("Skipped. Moving on.")

        logging.info(f"Rejection logged: {pending['title']} @ {pending['company']} -- {reason or 'no reason'}")
        return

    # ── #requests channel: conversational flow ────────────────────────────
    if message.channel.id == REQUESTS_CHANNEL_ID and not content.startswith("!"):

        # Step 2: user is replying with their instructions
        if message.author.id in pending_requests:
            pending = pending_requests.pop(message.author.id)
            custom_prompt = content if content.lower() != "skip" else ""
            label = pending["label"]

            await message.channel.send(
                f"<@{message.author.id}> Got it! Working on it now...\n"
                f"> **{label}**"
                + (f"\n> Instructions: {custom_prompt[:120]}" if custom_prompt else "")
                + "\n\nGive me 30-60 seconds..."
            )
            logging.info(f"Requests job triggered: {label} | prompt: {custom_prompt[:100]}")
            await _run_tailor(
                message.channel,
                message.author.id,
                pending.get("job_url"),
                custom_prompt,
                jd_file=pending.get("jd_file"),
            )
            return

        # Step 1: user posted a URL or PDF - detect and ask what they want
        job_url = None
        jd_file = None
        label = None

        # Check for PDF attachment
        for attachment in message.attachments:
            if attachment.filename.lower().endswith(".pdf"):
                await message.channel.send(
                    f"<@{message.author.id}> Got your PDF. Extracting job description..."
                )
                text = await _extract_pdf_text(attachment)
                if not text:
                    await message.channel.send(
                        f"<@{message.author.id}> Couldn't read that PDF. Try copy-pasting the job description as text, or send a URL."
                    )
                    return
                # Save to temp file for tailor.py
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".txt", delete=False, encoding="utf-8"
                ) as f:
                    f.write(text)
                    jd_file = f.name
                label = attachment.filename
                break

        # Check for URL in message text
        if not jd_file:
            url_match = _re.search(r'https?://\S+', content)
            if url_match:
                job_url = url_match.group(0)
                label = job_url

        if not job_url and not jd_file:
            # Not a URL or PDF - ignore (could be a general message)
            return

        # Store pending state and ask what they want
        pending_requests[message.author.id] = {
            "job_url": job_url,
            "jd_file": jd_file,
            "label": label,
        }

        await message.channel.send(
            f"<@{message.author.id}> Got it. What do you want?\n\n"
            f"Examples:\n"
            f"  • `tailor my resume and cover letter`\n"
            f"  • `tailor my resume, emphasize my Python automation experience`\n"
            f"  • `just the cover letter, make it startup-casual`\n"
            f"  • `tailor everything, note I already have a referral there`\n\n"
            f"Or reply `skip` to use defaults."
        )
        return

    # ── Commands ──────────────────────────────────────────────────────────
    if not content.startswith("!"):
        return

    parts = content.split(maxsplit=2)
    cmd = parts[0].lower()

    if cmd == "!help":
        await message.channel.send(HELP_TEXT)

    elif cmd == "!applied":
        if len(parts) < 3:
            await message.channel.send("Usage: `!applied <job_url> <Company> - <Role>`")
            return
        job_url = parts[1]
        label = parts[2]
        company, title = (label.split(" - ", 1) if " - " in label else (label, "Unknown Role"))
        log_application(job_url, title.strip(), company.strip())
        await message.channel.send(
            f"<@{message.author.id}> Logged! 📨 **{title.strip()}** at **{company.strip()}**"
        )

    elif cmd == "!status":
        if len(parts) < 3:
            await message.channel.send(f"Usage: `!status <job_url> <status>`\nValid: {', '.join(STATUSES)}")
            return
        job_url = parts[1]
        status = parts[2].lower().replace(" ", "_")
        if status not in STATUSES:
            await message.channel.send(f"Invalid status. Choose from: {', '.join(STATUSES)}")
            return
        if update_status(job_url, status):
            sync_to_web_ui(job_url, status)
            await message.channel.send(f"{STATUS_EMOJI.get(status, '')} Status updated to **{status}**")
        else:
            await message.channel.send("Job URL not found. Log it first with `!applied`")

    elif cmd == "!note":
        if len(parts) < 3:
            await message.channel.send("Usage: `!note <job_url> <your note>`")
            return
        if add_note(parts[1], parts[2]):
            await message.channel.send("Note saved.")
        else:
            await message.channel.send("Job URL not found.")

    elif cmd == "!tracker":
        rows = get_tracker()
        if not rows:
            await message.channel.send("No applications logged yet.")
            return
        lines = ["**Your Application Pipeline:**\n"]
        for title, company, status, date_added, notes, job_url in rows:
            date_str = date_added[:10] if date_added else "?"
            line = f"{STATUS_EMOJI.get(status, '')} **{company}** - {title} | {status} | {date_str}"
            if notes:
                line += f"\n   > {notes}"
            lines.append(line)
        msg = "\n".join(lines)
        for chunk in [msg[i:i+1900] for i in range(0, len(msg), 1900)]:
            await message.channel.send(chunk)

    elif cmd == "!stats":
        app_stats, rejection_count, top_reasons = get_stats()
        if not app_stats and not rejection_count:
            await message.channel.send("No data yet.")
            return
        total = sum(app_stats.values())
        lines = [f"**Application Stats ({total} applied, {rejection_count} dismissed):**"]
        for status in STATUSES:
            count = app_stats.get(status, 0)
            if count:
                lines.append(f"{STATUS_EMOJI.get(status, '')} {status}: **{count}**")
        if top_reasons:
            lines.append("\n**Top reasons you've passed on roles:**")
            for reason, count in top_reasons:
                lines.append(f"  - \"{reason}\" ({count}x)")
        await message.channel.send("\n".join(lines))


if __name__ == "__main__":
    if not BOT_TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN not set in .env")
        exit(1)
    if CHANNEL_ID == 0:
        print("ERROR: DISCORD_CHANNEL_ID not set in .env")
        exit(1)
    client.run(BOT_TOKEN)

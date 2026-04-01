#!/usr/bin/env python3
"""
Job Scout Scraper - runs every 30 min via cron
Uses JobSpy for multi-board scraping with Claude relevance filtering
"""

import os
import sqlite3
import json
import logging
import re
import time
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from jobspy import scrape_jobs
from discord_webhook import DiscordWebhook, DiscordEmbed
import anthropic

load_dotenv()

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
INGEST_URL = os.getenv("INGEST_URL", "https://job-agent-henna.vercel.app/api/jobs/ingest")
INGEST_API_KEY = os.getenv("INGEST_API_KEY")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jobs.db")
MEMORY_REPO_PATH = os.getenv("MEMORY_REPO_PATH")  # local clone of job-agent-memory repo

# Hard filter: job descriptions mentioning 5+ years experience.
# Runs on the FULL description after fetch_full_job_details(), before Claude.
EXPERIENCE_FILTER_RE = re.compile(
    r'(?<![0-9-])([5-9]|\d{2})\+?\s*(?:to\s*\d+\s*)?years?\s*(?:of\s*)?(?:experience|exp\.?|required|minimum|min\.?)\b'
    r'|minimum\s+(?:of\s+)?([5-9]|\d{2})\s+years?'
    r'|(?<![0-9-])([5-9]|\d{2})\s*\+\s*(?:years?|yrs?)\b',
    re.IGNORECASE
)

logging.basicConfig(
    filename="/var/log/jobscraper.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %I:%M:%S %p"
)

# Load config from scraper_config.json — edit that file to tune search terms, locations, etc.
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper_config.json")
with open(_CONFIG_PATH) as _f:
    _cfg = json.load(_f)

SEARCH_TERMS = _cfg["search_terms"]
INTERN_SEARCH_TERMS = _cfg["intern_search_terms"]
PROMPT_ENG_SEARCH_TERMS = _cfg["prompt_eng_search_terms"]
COMMS_SEARCH_TERMS = _cfg["comms_search_terms"]
HIGH_CONVERSION_COMPANIES = _cfg["high_conversion_companies"]
LOCATIONS = _cfg["locations"]
WATCHLIST_COMPANIES = _cfg["watchlist_companies"]
EXCLUDE_INDUSTRIES = _cfg["exclude_industries"]
REPOST_SIGNALS = _cfg["repost_signals"]
NONSALES_TITLES = _cfg["nonsales_titles"]


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_url TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            date_found TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            job_url TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            status TEXT DEFAULT 'applied',
            date_added TEXT,
            notes TEXT
        )
    """)
    conn.commit()
    return conn


def is_seen(conn, job_url):
    return conn.execute("SELECT 1 FROM seen_jobs WHERE job_url = ?", (job_url,)).fetchone() is not None


def mark_seen(conn, job_url, title="", company=""):
    conn.execute(
        "INSERT OR IGNORE INTO seen_jobs (job_url, title, company, date_found) VALUES (?, ?, ?, ?)",
        (job_url, str(title), str(company), datetime.now(timezone.utc).isoformat())
    )
    conn.commit()


def fetch_full_job_details(job_url):
    """Fetch full description and applicant count from the actual job page."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(job_url, headers=headers, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")

        full_desc = None
        for selector in [
            ".show-more-less-html__markup",
            ".description__text",
            ".job-description",
            "[data-testid='job-description']",
            ".jobsearch-jobDescriptionText",
        ]:
            el = soup.select_one(selector)
            if el and len(el.get_text(strip=True)) > 200:
                full_desc = el.get_text(separator="\n", strip=True)
                break

        applicant_count = None
        page_text = soup.get_text()
        for pattern in [
            r"(\d[\d,]+)\s+applicants?",
            r"Over\s+(\d[\d,]+)\s+applicants?",
            r"Be among the first\s+(\d+)",
            r"(\d+)\s+people\s+clicked\s+apply",
            r"(\d+)\s+clicked\s+apply",
        ]:
            match = re.search(pattern, page_text, re.IGNORECASE)
            if match:
                try:
                    applicant_count = int(match.group(1).replace(",", ""))
                except ValueError:
                    pass
                break

        # LinkedIn embeds "Reposted X minutes/hours/days ago" in the page HTML for reposts.
        # Match the specific pattern rather than any occurrence of "reposted" to avoid false positives.
        page_repost = bool(re.search(r"reposted\s+\d+\s+(minute|hour|day|week)s?\s+ago", resp.text, re.IGNORECASE))

        return full_desc, applicant_count, page_repost
    except Exception as e:
        logging.warning(f"Failed to fetch job details from {job_url}: {e}")
        return None, None, False


def quick_filter(job):
    title = str(job.get("title") or "").lower()
    company = str(job.get("company") or "").lower()
    title_company = title + " " + company

    if any(term in title_company for term in EXCLUDE_INDUSTRIES):
        return False, "excluded industry"
    if any(t in title for t in NONSALES_TITLES):
        return False, "non-sales title"

    min_sal = job.get("min_amount")
    max_sal = job.get("max_amount")
    if min_sal and max_sal:
        try:
            if float(max_sal) < 40000:
                return False, f"salary too low: {max_sal}"
        except (ValueError, TypeError):
            pass

    return True, "ok"


def detect_repost(job):
    """Return True if the job shows signs of being a repost.
    Reposts get a fresh timestamp but are stale -- don't treat them as newly listed.
    """
    title = str(job.get("title") or "").lower()
    desc = str(job.get("description") or "").lower()[:600]
    combined = title + " " + desc
    return any(signal in combined for signal in REPOST_SIGNALS)


def passes_or_filter(job, is_repost=False):
    """Pass if posted < 2 hours ago OR applicants < 10.
    Reposts are never treated as fresh regardless of their timestamp.
    Returns (passes, age_hours, applicant_count).
    """
    applicant_count = job.get("applicants")
    age_hours = None

    date_posted = job.get("date_posted")
    if date_posted:
        try:
            now = datetime.now(timezone.utc)
            dp = date_posted
            if hasattr(dp, "tzinfo") and dp.tzinfo is None:
                dp = dp.replace(tzinfo=timezone.utc)
            age_hours = (now - dp).total_seconds() / 3600
        except Exception:
            pass

    # Reposts get a fresh timestamp but are stale -- ignore age for them
    age_under_2hrs = not is_repost and age_hours is not None and age_hours <= 2
    few_applicants = applicant_count is not None and applicant_count < 10

    if age_under_2hrs or few_applicants:
        return True, age_hours, applicant_count

    # Job is old (or reposted) -- require confirmed low applicant count to proceed.
    # Unknown applicant count on a non-fresh job is assumed stale.
    if age_hours is not None and age_hours > 2:
        return False, age_hours, applicant_count

    # Age truly unknown -- let Claude decide, but never give reposts a free pass
    if is_repost:
        return False, age_hours, applicant_count
    return True, age_hours, applicant_count


def is_repost_by_title(conn, title, company):
    """Return True if the same title+company was seen in the last 7 days (catches URL-refreshed reposts)."""
    row = conn.execute(
        """SELECT 1 FROM seen_jobs
           WHERE lower(title) = lower(?) AND lower(company) = lower(?)
           AND date_found > datetime('now', '-7 days')""",
        (str(title), str(company))
    ).fetchone()
    return row is not None


def get_rejection_context():
    """Pull recent rejection reasons from DB to inform Claude."""
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            """SELECT title, company, reason FROM rejections
               WHERE reason IS NOT NULL AND reason != ''
               ORDER BY date_rejected DESC LIMIT 15"""
        ).fetchall()
        conn.close()
        if not rows:
            return ""
        lines = ["Josh has recently passed on these roles and here's why (use this to calibrate):"]
        for title, company, reason in rows:
            lines.append(f"  - {title} @ {company}: \"{reason}\"")
        return "\n".join(lines)
    except Exception:
        return ""


def get_vault_rejection_context():
    """Read recent rows from job-agent-memory rejection-patterns.md to supplement SQLite context."""
    if not MEMORY_REPO_PATH:
        return ""
    md_path = os.path.join(MEMORY_REPO_PATH, "rejection-patterns.md")
    if not os.path.exists(md_path):
        return ""
    try:
        with open(md_path, encoding="utf-8") as f:
            lines = f.readlines()
        # Parse markdown table rows (skip header lines starting with | Date | or |---)
        rows = [
            l.strip() for l in lines
            if l.startswith("|") and not l.startswith("| Date") and not l.startswith("|---")
        ]
        if not rows:
            return ""
        recent = rows[-20:]  # last 20 dismissals
        parsed = []
        for row in recent:
            parts = [p.strip() for p in row.strip("|").split("|")]
            if len(parts) >= 4:
                _, title, company, reason = parts[0], parts[1], parts[2], parts[3]
                if reason:
                    parsed.append(f"  - {title} @ {company}: \"{reason}\"")
        if not parsed:
            return ""
        return "Additional dismissals from memory vault:\n" + "\n".join(parsed)
    except Exception:
        return ""


def claude_relevance_check(job):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    rejection_context = get_rejection_context()
    vault_context = get_vault_rejection_context()
    combined_context = "\n".join(filter(None, [rejection_context, vault_context]))

    prompt = f"""You are filtering job postings for Josh Sachs, a new grad CS student graduating June 2026 from UC Santa Cruz.

Target profile:
- Roles: SDR, BDR, Sales Development, Sales Engineer, Solutions Engineer, Account Development, Technical Sales, entry-level AE
- Industry: Tech, SaaS, AI, Automation only (no healthcare, insurance, staffing agencies)
- Location: Bay Area (SF, Oakland, East Bay, South Bay/Peninsula) or Remote
- Pay: $55k+ base with path to $100k+ via OTE or fast promotion
- Experience: suitable for 0-2 years (does NOT need to say "new grad" explicitly)
- Internships: include sales/GTM/BD internships at well-known tech companies with high intern-to-FT conversion
- Exclude: roles requiring 3+ years experience, non-English language requirements, non-tech industries

{combined_context}

Job posting:
Title: {job.get("title")}
Company: {job.get("company")}
Location: {job.get("location")}
Description (first 3500 chars): {str(job.get("description") or "")[:3500]}

Respond with JSON only:
{{
  "relevant": true or false,
  "reason": "one sentence max",
  "fit_score": 1 to 10,
  "experience_required": "e.g. 0-1 years or 5+ years",
  "location_ok": true or false
}}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        logging.warning(f"Claude check failed: {e}")
        return {"relevant": False, "reason": "api error", "fit_score": 0}


def claude_prompt_eng_check(job):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""You are evaluating whether a job posting is a good fit for Josh Sachs, a CS student graduating June 2026 from UC Santa Cruz who is interested in Prompt Engineer / AI Engineer roles.

Josh's relevant qualifications:
- B.A. Computer Science, UC Santa Cruz (graduating June 2026)
- Python (strong), JavaScript, C, C++
- Built and shipped automation tools using LLM APIs (Anthropic Claude API)
- Experience with workflow automation: Zapier, Google Sheets API, Gmail API
- Built AI-powered job scout tool using Claude for relevance scoring
- Basic Machine Learning coursework
- 2 internships involving automation engineering and sales engineering
- No prior dedicated "AI/ML engineer" role, but hands-on LLM API usage

What makes Josh a fit for prompt engineer roles:
- Direct experience writing prompts for Claude (structured JSON output, multi-criteria filtering)
- Built production tools with LLM APIs
- CS fundamentals (data structures, systems, databases)
- Strong Python

What would disqualify Josh:
- Roles requiring 3+ years experience
- Roles requiring ML research background (PhD/MS level)
- Roles requiring deep model training/fine-tuning expertise
- Non-tech industry
- Non-Bay Area and not remote

Job posting:
Title: {job.get("title")}
Company: {job.get("company")}
Location: {job.get("location")}
Description (first 3500 chars): {str(job.get("description") or "")[:3500]}

Assess whether Josh is genuinely qualified and whether this is worth applying to.
Respond with JSON only:
{{
  "relevant": true or false,
  "reason": "one sentence max",
  "fit_score": 1 to 10,
  "experience_required": "e.g. 0-1 years or 3+ years",
  "location_ok": true or false
}}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        logging.warning(f"Claude prompt eng check failed: {e}")
        return {"relevant": False, "reason": "api error", "fit_score": 0}


def claude_comms_check(job):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""You are evaluating whether a job posting is a good fit for Josh Sachs, a CS student graduating June 2026 from UC Santa Cruz who is targeting communications and community roles at tech companies.

Josh's relevant qualifications for comms roles:
- B.A. Computer Science, UC Santa Cruz (graduating June 2026) -- gives technical credibility
- VP of Member Integration at Alpha Kappa Psi: ran a 7-week professional development program, led fundraising raising $6.5K in 7 weeks, alumni workshops, interview prep
- Authored daily analytics briefs for founders at Cush Real Estate (translating technical metrics into business recommendations)
- Client-facing communication, demo scheduling, cold calling at Shockproof
- Public speaking, leadership, team development
- Python/JavaScript -- can write scripts, build tooling, work with developer audiences

Best-fit roles for Josh:
- Developer Advocate / Developer Relations (strong fit: CS + communication skills)
- Marketing Communications Coordinator at a tech/SaaS company
- Community Manager at a tech company
- Content Marketing at a tech/SaaS/AI company
- Technical Writer entry level

What would disqualify Josh:
- Roles requiring 3+ years experience
- PR/comms at non-tech industries (healthcare, finance, law)
- Roles requiring an existing large social media following or journalism background
- Senior-level strategy roles

Location: Bay Area or Remote only.
Pay: $50k+ base.

Job posting:
Title: {job.get("title")}
Company: {job.get("company")}
Location: {job.get("location")}
Description (first 3500 chars): {str(job.get("description") or "")[:3500]}

Respond with JSON only:
{{
  "relevant": true or false,
  "reason": "one sentence max",
  "fit_score": 1 to 10,
  "experience_required": "e.g. 0-1 years or 3+ years",
  "location_ok": true or false
}}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as e:
        logging.warning(f"Claude comms check failed: {e}")
        return {"relevant": False, "reason": "api error", "fit_score": 0}


def send_discord_alert(job, analysis, is_intern=False, is_prompt_eng=False, is_repost=False, is_comms=False):
    mention = f"<@{DISCORD_USER_ID}>" if DISCORD_USER_ID else "@here"

    applicants = job.get("applicants")
    date_posted = job.get("date_posted")
    company_lower = str(job.get("company") or "").lower()
    is_watchlist = any(w in company_lower for w in WATCHLIST_COMPANIES)

    age_hours = None
    if date_posted:
        try:
            now = datetime.now(timezone.utc)
            dp = date_posted
            if hasattr(dp, "tzinfo") and dp.tzinfo is None:
                dp = dp.replace(tzinfo=timezone.utc)
            age_hours = (now - dp).total_seconds() / 3600
        except Exception:
            pass

    is_hot = not is_repost and age_hours is not None and age_hours <= 1

    if is_watchlist:
        color = "FFD700"
    elif is_hot:
        color = "00FF7F"
    else:
        color = "03b2f8"

    flags = []
    if is_repost:
        flags.append("♻️ REPOST -- timestamp unreliable, likely stale")
    elif is_hot:
        flags.append("🔥 FRESH -- under 1 hour old")
    elif age_hours is not None and age_hours <= 2:
        flags.append(f"🔥 FRESH -- posted {age_hours:.0f}h ago")
    if is_watchlist:
        flags.append("⭐ WATCHLIST COMPANY")
    if is_intern:
        flags.append("🎓 INTERNSHIP -- high conversion company")
    if is_prompt_eng:
        flags.append("🤖 AI/PROMPT ENGINEER ROLE")
    if is_comms:
        flags.append("📣 COMMS/DEVREL ROLE")
    if applicants is not None:
        try:
            n = int(applicants)
            flags.append(f"👀 {n} applicant{'s' if n != 1 else ''}")
        except (ValueError, TypeError):
            pass

    posted_str = f"{age_hours:.1f}h ago" if age_hours is not None else str(date_posted) if date_posted else "Unknown"
    fit_score = analysis.get("fit_score", "?")
    description_preview = str(job.get("description") or "")[:300] + "..."

    embed_description = ("\n".join(flags) + "\n\n" if flags else "") + description_preview

    webhook = DiscordWebhook(
        url=WEBHOOK_URL,
        content=f"{mention} New job match! Fit score: {fit_score}/10"
    )

    embed = DiscordEmbed(
        title=f"{job.get('title', 'Unknown Role')} @ {job.get('company', 'Unknown')}",
        url=job.get("job_url", ""),
        description=embed_description,
        color=color
    )

    embed.add_embed_field(name="Company", value=str(job.get("company", "N/A")), inline=True)
    embed.add_embed_field(name="Location", value=str(job.get("location", "N/A")), inline=True)
    embed.add_embed_field(name="Posted", value=posted_str, inline=True)
    embed.add_embed_field(name="Applicants", value=str(applicants) if applicants is not None else "N/A", inline=True)
    embed.add_embed_field(name="Source", value=str(job.get("site", "N/A")).capitalize(), inline=True)
    embed.add_embed_field(name="Why it fits", value=str(analysis.get("reason", "N/A")), inline=False)
    embed.set_footer(text="✅ tailor resume  |  📨 mark applied  |  ❌ dismiss")

    webhook.add_embed(embed)
    webhook.execute()


def post_to_job_agent(job, analysis):
    """POST a filtered job to the job-agent web UI ingest endpoint."""
    if not INGEST_API_KEY:
        return
    try:
        payload = {
            "title": str(job.get("title") or ""),
            "company": str(job.get("company") or ""),
            "url": str(job.get("job_url") or ""),
            "description": str(job.get("description") or ""),
            "source": str(job.get("site") or "linkedin"),
        }
        resp = requests.post(
            INGEST_URL,
            json=payload,
            headers={"x-api-key": INGEST_API_KEY},
            timeout=10,
        )
        if resp.status_code == 201:
            logging.info(f"Ingested to job-agent: {payload['title']} @ {payload['company']}")
        else:
            logging.warning(f"Ingest failed ({resp.status_code}): {resp.text[:200]}")
    except Exception as e:
        logging.warning(f"post_to_job_agent error: {e}")


def process_jobs(jobs_df, conn, is_intern=False, is_remote=False, is_prompt_eng=False, is_comms=False):
    alerted = 0
    for _, job in jobs_df.iterrows():
        job_dict = job.to_dict()
        job_url = str(job_dict.get("job_url", ""))
        if not job_url or is_seen(conn, job_url):
            continue

        passed, reason = quick_filter(job_dict)
        if not passed:
            logging.info(f"Quick filtered: {job_dict.get('title')} @ {job_dict.get('company')} -- {reason}")
            mark_seen(conn, job_url, job_dict.get("title"), job_dict.get("company"))
            continue

        # Fetch full description, applicant count, and repost flag from page HTML
        full_desc, applicant_count, page_repost = fetch_full_job_details(job_url)
        if full_desc:
            job_dict["description"] = full_desc
        if applicant_count is not None:
            job_dict["applicants"] = applicant_count

        # Hard experience filter: reject before Claude if full description contains 5+ years requirement
        desc_text = str(job_dict.get("description") or "")
        if desc_text and EXPERIENCE_FILTER_RE.search(desc_text):
            logging.info(f"Experience filtered: {job_dict.get('title')} @ {job_dict.get('company')} -- 5+ years requirement found")
            mark_seen(conn, job_url, job_dict.get("title"), job_dict.get("company"))
            continue

        title_repost = is_repost_by_title(conn, job_dict.get("title", ""), job_dict.get("company", ""))
        repost = page_repost or detect_repost(job_dict)
        if repost:
            logging.info(f"Repost detected: {job_dict.get('title')} @ {job_dict.get('company')} -- skipping freshness fast-pass")
        elif title_repost:
            logging.info(f"Title repost (same title+company seen in 7 days): {job_dict.get('title')} @ {job_dict.get('company')} -- noted but not hard-blocked")

        # OR filter
        passes, age_hours, applicant_count = passes_or_filter(job_dict, is_repost=repost)
        if not passes:
            logging.info(f"OR filtered ({age_hours:.1f}h old, {applicant_count} applicants): {job_dict.get('title')} @ {job_dict.get('company')}")
            mark_seen(conn, job_url, job_dict.get("title"), job_dict.get("company"))
            continue

        # Intern gate: only high-conversion companies
        if is_intern:
            company_lower = str(job_dict.get("company") or "").lower()
            if not any(c in company_lower for c in HIGH_CONVERSION_COMPANIES):
                mark_seen(conn, job_url, job_dict.get("title"), job_dict.get("company"))
                continue

        if is_prompt_eng:
            analysis = claude_prompt_eng_check(job_dict)
        elif is_comms:
            analysis = claude_comms_check(job_dict)
        else:
            analysis = claude_relevance_check(job_dict)

        min_score = 4 if is_intern else (5 if is_remote else 5)
        if not analysis.get("relevant") or analysis.get("fit_score", 0) < min_score:
            logging.info(f"Claude filtered: {job_dict.get('title')} @ {job_dict.get('company')} -- {analysis.get('reason')}")
            mark_seen(conn, job_url, job_dict.get("title"), job_dict.get("company"))
            continue

        send_discord_alert(job_dict, analysis, is_intern=is_intern, is_prompt_eng=is_prompt_eng, is_repost=repost, is_comms=is_comms)
        post_to_job_agent(job_dict, analysis)
        mark_seen(conn, job_url, job_dict.get("title"), job_dict.get("company"))
        alerted += 1
        logging.info(f"Alerted: {job_dict.get('title')} @ {job_dict.get('company')} (score {analysis.get('fit_score')})")

    return alerted


def run():
    logging.info("=== Scraper run started ===")
    conn = init_db()
    total_alerted = 0

    # Bay Area searches
    for search_term in SEARCH_TERMS:
        for location in LOCATIONS:
            try:
                jobs_df = scrape_jobs(
                    site_name=["linkedin"],
                    search_term=search_term,
                    location=location,
                    results_wanted=50,
                    hours_old=24,
                    job_type="fulltime",
                )
                if jobs_df is not None and not jobs_df.empty:
                    logging.info(f"'{search_term}' in {location}: {len(jobs_df)} results")
                    total_alerted += process_jobs(jobs_df, conn)
            except Exception as e:
                logging.error(f"Error scraping '{search_term}' in {location}: {e}")
            time.sleep(2)

    # Intern searches
    for search_term in INTERN_SEARCH_TERMS:
        for location in LOCATIONS:
            try:
                jobs_df = scrape_jobs(
                    site_name=["linkedin"],
                    search_term=search_term,
                    location=location,
                    results_wanted=25,
                    hours_old=24,
                    job_type="fulltime",

                )
                if jobs_df is not None and not jobs_df.empty:
                    logging.info(f"'{search_term}' (intern) in {location}: {len(jobs_df)} results")
                    total_alerted += process_jobs(jobs_df, conn, is_intern=True)
            except Exception as e:
                logging.error(f"Error scraping '{search_term}' intern in {location}: {e}")
            time.sleep(2)

    # Prompt Engineer / AI Engineer searches (Bay Area + remote)
    for search_term in PROMPT_ENG_SEARCH_TERMS:
        for location in LOCATIONS:
            try:
                jobs_df = scrape_jobs(
                    site_name=["linkedin"],
                    search_term=search_term,
                    location=location,
                    results_wanted=30,
                    hours_old=24,
                    job_type="fulltime",

                )
                if jobs_df is not None and not jobs_df.empty:
                    logging.info(f"'{search_term}' (prompt eng) in {location}: {len(jobs_df)} results")
                    total_alerted += process_jobs(jobs_df, conn, is_prompt_eng=True)
            except Exception as e:
                logging.error(f"Error scraping '{search_term}' prompt eng in {location}: {e}")
            time.sleep(2)
    # Prompt Engineer remote
    for search_term in ["Prompt Engineer remote", "AI Engineer remote entry level"]:
        try:
            jobs_df = scrape_jobs(
                site_name=["linkedin"],
                search_term=search_term,
                location="United States",
                results_wanted=30,
                hours_old=24,
                job_type="fulltime",
                is_remote=True,

            )
            if jobs_df is not None and not jobs_df.empty:
                logging.info(f"'{search_term}' (prompt eng remote): {len(jobs_df)} results")
                total_alerted += process_jobs(jobs_df, conn, is_prompt_eng=True, is_remote=True)
        except Exception as e:
            logging.error(f"Error scraping remote prompt eng '{search_term}': {e}")
        time.sleep(2)

    # Comms / DevRel searches (Bay Area + remote)
    for search_term in COMMS_SEARCH_TERMS:
        for location in LOCATIONS:
            try:
                jobs_df = scrape_jobs(
                    site_name=["linkedin"],
                    search_term=search_term,
                    location=location,
                    results_wanted=30,
                    hours_old=24,
                    job_type="fulltime",

                )
                if jobs_df is not None and not jobs_df.empty:
                    logging.info(f"'{search_term}' (comms) in {location}: {len(jobs_df)} results")
                    total_alerted += process_jobs(jobs_df, conn, is_comms=True)
            except Exception as e:
                logging.error(f"Error scraping '{search_term}' comms in {location}: {e}")
            time.sleep(2)
    for search_term in ["Developer Advocate remote", "Developer Relations remote", "Community Manager remote tech"]:
        try:
            jobs_df = scrape_jobs(
                site_name=["linkedin"],
                search_term=search_term,
                location="United States",
                results_wanted=30,
                hours_old=24,
                job_type="fulltime",
                is_remote=True,

            )
            if jobs_df is not None and not jobs_df.empty:
                logging.info(f"'{search_term}' (comms remote): {len(jobs_df)} results")
                total_alerted += process_jobs(jobs_df, conn, is_comms=True, is_remote=True)
        except Exception as e:
            logging.error(f"Error scraping remote comms '{search_term}': {e}")
        time.sleep(2)

    # Remote searches
    for search_term in ["SDR remote", "BDR remote", "Sales Development Representative remote", "Sales Engineer remote"]:
        try:
            jobs_df = scrape_jobs(
                site_name=["linkedin"],
                search_term=search_term,
                location="United States",
                results_wanted=30,
                hours_old=24,
                job_type="fulltime",
                is_remote=True,

            )
            if jobs_df is not None and not jobs_df.empty:
                logging.info(f"'{search_term}' (remote): {len(jobs_df)} results")
                total_alerted += process_jobs(jobs_df, conn, is_remote=True)
        except Exception as e:
            logging.error(f"Error scraping remote '{search_term}': {e}")
        time.sleep(2)

    conn.close()
    logging.info(f"=== Scraper run complete. Jobs alerted: {total_alerted} ===")


if __name__ == "__main__":
    run()

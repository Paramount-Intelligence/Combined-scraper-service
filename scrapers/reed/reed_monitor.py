import sys
import time
import smtplib
import re
import hashlib
from pymongo import MongoClient, UpdateOne
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from selenium.webdriver.chrome.options import Options
import os
from dotenv import load_dotenv

# Ensure UTF-8 output on all platforms (fixes Windows emoji crash)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# Load .env file from this script's directory, falling back to the parent directory if needed
script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(script_dir, ".env"))
parent_env = os.path.join(os.path.dirname(os.path.dirname(script_dir)), ".env")
if os.path.exists(parent_env):
    load_dotenv(dotenv_path=parent_env)

PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time (UTC+5)

# ============================
# CONFIGURATION
# ============================
class Config:
    PLATFORM_NAME = "reed"
    PROJECTS_COLLECTION = "projects"  # Shared MongoDB collection

    SMTP_SERVER  = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT    = int(os.getenv("SMTP_PORT", 587))
    SENDER_EMAIL    = os.getenv("SENDER_EMAIL")
    SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
    RECIPIENT_EMAILS = [
        e.strip() for e in os.getenv("RECIPIENT_EMAILS", "").split(",") if e.strip()
    ]

    CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL", 60))
    MAX_AGE_MINUTES = int(os.getenv("MAX_AGE_MINUTES", 360))
    HEADLESS     = os.getenv("HEADLESS", "True").lower() == "true"
    MONGO_URI    = os.getenv("MONGO_URI", "mongodb://localhost:27017/")

    BASE_URL   = "https://www.reed.co.uk"
    TARGET_URL = os.getenv("TARGET_URL", "https://www.reed.co.uk/jobs/work-from-home-jobs?sortby=DisplayDate")

# CLI Options
DEBUG_MODE = "--debug" in sys.argv
ONCE_MODE  = "--once"  in sys.argv
TEST_MODE  = "--test"  in sys.argv

def debug_print(msg):
    if DEBUG_MODE:
        print(msg)

def dump_page_structure(driver):
    """Dump information about page structure for diagnostic purposes when elements aren't found."""
    print("\n" + "="*60)
    print("🔍 DIAGNOSTICS: REED PAGE STRUCTURE DUMP")
    print("="*60)
    print(f"  URL: {driver.current_url}")

    card_candidates = [
        "article[data-qa='job-card']",
        "article[class*='jobCard']",
        "[class*='jobCard']",
        "article", ".card", "[class*='card']", "li[class]"
    ]
    print("\n📦 Card Containers:")
    for sel in card_candidates:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            if elems:
                sample = elems[0]
                cls = sample.get_attribute("class") or ""
                tag = sample.tag_name
                txt = sample.text[:80].replace("\n", " ") if sample.text else "(empty)"
                print(f"  [{len(elems)}] {sel}  → <{tag} class='{cls[:60]}'> text='{txt}'")
        except Exception:
            pass

    print("\n📝 Headers / Titles:")
    for sel in ["h1", "h2", "[data-qa='job-card-title']", "[class*='title']"]:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            if elems:
                for e in elems[:3]:
                    txt = e.text.strip()[:80] if e.text else ""
                    if txt:
                        print(f"  <{e.tag_name} class='{(e.get_attribute('class') or '')[:40]}'> → {txt}")
        except Exception:
            pass
    print("="*60 + "\n")

# ============================
# COOKIE CONSENT
# ============================
def dismiss_cookie_banner(driver):
    """Dismiss the OneTrust/cookie consent banner if present."""
    for sel in [
        "#onetrust-accept-btn-handler",
        "button#onetrust-accept-btn-handler",
        "button[aria-label*='Accept']",
        "button[title*='Accept All']",
        "button[class*='cookie'][class*='accept']",
    ]:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(1)
            debug_print(f"  Dismissed cookie banner via '{sel}'")
            return
        except (NoSuchElementException, Exception):
            continue

# ============================
# JOB EXTRACTION
# ============================
CARD_SELECTORS = [
    "article[data-qa='job-card']",
    "article[class*='jobCard']",
    "div[class*='jobCard']",
]

def extract_project_data(card):
    """Extract job info from a Reed job card."""
    try:
        # ── ID ────────────────────────────────────────────────────────────────
        numeric_id = None
        data_id = card.get_attribute("data-id") or ""
        m = re.match(r'job(\d+)', data_id)
        if m:
            numeric_id = m.group(1)

        # ── Title & URL ──────────────────────────────────────────────────────
        title = ""
        href = ""
        for sel in ["[data-qa='job-card-title']", "h2 a", "a[href*='/jobs/']"]:
            try:
                elem = card.find_element(By.CSS_SELECTOR, sel)
                title = elem.text.strip()
                href = elem.get_attribute("href") or ""
                if title and href:
                    break
            except NoSuchElementException:
                continue

        if not title:
            return None

        if not numeric_id and href:
            m2 = re.search(r'/jobs/[^/?]+/(\d+)', href)
            if m2:
                numeric_id = m2.group(1)

        if not numeric_id:
            numeric_id = hashlib.md5(title.encode()).hexdigest()[:12]

        url = href.split("?")[0] if href else ""
        if url.startswith("/"):
            url = Config.BASE_URL + url
        if not url:
            url = f"{Config.BASE_URL}/jobs/{numeric_id}"

        # ── Posted date / Company ───────────────────────────────────────────────
        time_posted = "Recently"
        company = ""
        try:
            posted_elem = card.find_element(By.CSS_SELECTOR, "[data-qa='job-posted-by']")
            full_text = posted_elem.text.strip()
            parts = re.split(r'\s+by\s+', full_text, maxsplit=1)
            if parts and parts[0].strip():
                time_posted = parts[0].strip()
            if len(parts) > 1:
                company = parts[1].strip()
        except NoSuchElementException:
            pass

        # ── Salary / Location / Job Type ─────────────────────────────────────────
        salary = "Not specified"
        location = ""
        job_type = ""
        try:
            for li in card.find_elements(By.CSS_SELECTOR, "[data-qa='job-metadata'] li"):
                qa = li.get_attribute("data-qa") or ""
                txt = li.text.strip()
                if not txt:
                    continue
                if qa == "job-metadata-salary":
                    salary = txt
                elif qa == "job-metadata-location":
                    location = txt
                else:
                    job_type = txt
        except Exception:
            pass

        return {
            "id":          f"reed-{numeric_id}",
            "title":       title,
            "description": "",  # Filled from detail page
            "location":    location,
            "salary":      salary,
            "job_type":    job_type,
            "company":     company,
            "time_posted": time_posted,
            "url":         url,
            "detected_at": datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:
        debug_print(f"  ⚠️ Error parsing card: {e}")
        return None

def find_job_cards(driver):
    """Locate all job card elements on the listing page."""
    for sel in CARD_SELECTORS:
        try:
            cards = driver.find_elements(By.CSS_SELECTOR, sel)
            if cards:
                debug_print(f"  Located {len(cards)} cards using selector: '{sel}'")
                return cards
        except Exception:
            pass
    return []

def scan_for_jobs(driver):
    """Scrape the IT jobs listing page for job cards."""
    try:
        driver.get(Config.TARGET_URL)
        time.sleep(3)
        dismiss_cookie_banner(driver)

        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(2)

        cards = find_job_cards(driver)
        if not cards:
            print("⚠️  No job cards found with default selectors.")
            dump_page_structure(driver)
            return []

        jobs = []
        for card in cards:
            j = extract_project_data(card)
            if j and j.get("title") and j.get("id"):
                jobs.append(j)

        print(f"✅ Extracted {len(jobs)} valid jobs from {len(cards)} cards")
        return jobs
    except TimeoutException:
        print("⏳ Timeout waiting for Reed listing page to load")
        return []
    except Exception as e:
        print(f"❌ Error scanning Reed: {e}")
        return []

# ============================
# DETAIL SCANNER
# ============================
def fetch_job_details(driver, url):
    """Navigate to a job's detail page to pull the full description."""
    details = {"description": ""}
    try:
        driver.get(url)
        time.sleep(3)
        dismiss_cookie_banner(driver)

        for sel in ["[data-qa='job-description']", "[class*='jobDescription']", "main"]:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                txt = el.text.strip()
                if len(txt) > 50:
                    txt = re.sub(r'\s*\n\s*', '\n', txt).strip()
                    details["description"] = txt[:4000]
                    break
            except NoSuchElementException:
                continue
    except Exception as e:
        print(f"  ⚠️ Detail fetch failed for {url}: {e}")
    return details

# ============================
# JOB DATABASE (MongoDB)
# ============================
_mongo_client = None

def _get_collection():
    """Shared database collection 'projects'."""
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(Config.MONGO_URI)
    return _mongo_client["office_monitor"][Config.PROJECTS_COLLECTION]

def init_db():
    """Ensure a unique index on 'project_id' exists."""
    try:
        _get_collection().create_index("project_id", unique=True, name="idx_project_id_unique")
    except Exception:
        pass

def db_is_cold_start():
    """Returns True if database has no Reed records."""
    doc = _get_collection().find_one({"platform": Config.PLATFORM_NAME}, {"_id": 1})
    return doc is None

def get_seen_ids():
    """Retrieve set of job IDs already stored for Reed."""
    try:
        docs = _get_collection().find({"platform": Config.PLATFORM_NAME}, {"project_id": 1, "_id": 0})
        return {d["project_id"] for d in docs if d.get("project_id")}
    except Exception as e:
        print(f"  ⚠️ Error loading seen IDs: {e}")
        return set()

def insert_project(job, emailed=True):
    """Insert one job into the shared MongoDB collection."""
    try:
        doc = {
            "project_id":  job.get("id"),
            "title":       job.get("title"),
            "description": job.get("description"),
            "location":    job.get("location"),
            "salary":      job.get("salary"),
            "job_type":    job.get("job_type"),
            "company":     job.get("company"),
            "time_posted": job.get("time_posted"),
            "url":         job.get("url"),
            "detected_at": job.get("detected_at"),
            "platform":    Config.PLATFORM_NAME,
            "emailed":     bool(emailed),
        }
        _get_collection().update_one(
            {"project_id": doc["project_id"]},
            {"$setOnInsert": doc},
            upsert=True
        )
    except Exception as e:
        print(f"⚠️ DB insert failed: {e}")

def bulk_insert_projects(jobs, emailed=False):
    """Seed DB with multiple jobs silently (used on cold start)."""
    try:
        ops = []
        for j in jobs:
            if not j.get("id"):
                continue
            doc = {
                "project_id":  j.get("id"),
                "title":       j.get("title"),
                "description": j.get("description"),
                "location":    j.get("location"),
                "salary":      j.get("salary"),
                "job_type":    j.get("job_type"),
                "company":     j.get("company"),
                "time_posted": j.get("time_posted"),
                "url":         j.get("url"),
                "detected_at": j.get("detected_at"),
                "platform":    Config.PLATFORM_NAME,
                "emailed":     bool(emailed),
            }
            ops.append(UpdateOne({"project_id": doc["project_id"]}, {"$setOnInsert": doc}, upsert=True))
        if ops:
            result = _get_collection().bulk_write(ops, ordered=False)
            print(f"  DB: Seeded {result.upserted_count} records to shared collection (platform: {Config.PLATFORM_NAME})")
    except Exception as e:
        print(f"⚠️ DB bulk seed failed: {e}")

# ============================
# AGE FILTERING
# ============================
def parse_posted_minutes(time_str):
    """Convert 'time_posted' string → minutes elapsed. Returns None if unparseable.
    Handles relative strings ('X hr/hrs ago', 'X day(s) ago', 'Just now', 'Today',
    'Yesterday') and absolute dates ('21 May').
    """
    if not time_str:
        return None
    s = time_str.strip().lower()

    if any(w in s for w in ("just now", "today", "moments ago")):
        return 0
    if "yesterday" in s:
        return 1440

    m = re.search(r'(\d+)\s*(min|hour|hr|day|week|month)', s)
    if m:
        val = int(m.group(1))
        unit = m.group(2)
        mult = {"min": 1, "hour": 60, "hr": 60, "day": 1440, "week": 10080, "month": 43200}[unit]
        return val * mult

    # Absolute date e.g. "21 May"
    m2 = re.search(r'(\d{1,2})\s+([A-Za-z]{3,})', time_str.strip())
    if m2:
        try:
            day = int(m2.group(1))
            month_str = m2.group(2)[:3].title()
            now = datetime.now(PKT)
            posted = datetime.strptime(f"{day} {month_str} {now.year}", "%d %b %Y").replace(tzinfo=PKT)
            if posted > now:
                posted = posted.replace(year=now.year - 1)
            return int((now - posted).total_seconds() // 60)
        except Exception:
            pass

    return None

def filter_new_jobs(all_jobs, seen_ids):
    """Remove already-seen IDs and jobs older than MAX_AGE_MINUTES."""
    result = []
    for j in all_jobs:
        if not j.get("id") or j["id"] in seen_ids:
            continue
        age = parse_posted_minutes(j.get("time_posted", ""))
        if age is not None and age > Config.MAX_AGE_MINUTES:
            print(f"  [SKIP - too old] {j['title'][:50]} (posted {j['time_posted']})")
            continue
        result.append(j)
    return result

# ============================
# EMAIL INTEGRATION
# ============================
def _esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _section_header(icon, title, color):
    return (
        f'<tr><td colspan="2" style="padding:14px 16px 6px;background:{color};'
        f'color:#fff;font-size:12px;font-weight:bold;'
        f'text-transform:uppercase;letter-spacing:1px;">'
        f'{icon}&nbsp; {title}</td></tr>'
    )

def _row(label, value, alt=False, bold_value=False):
    if not value:
        return ""
    bg   = "background:#f8f9fa;" if alt else "background:#fff;"
    bold = "font-weight:bold;" if bold_value else ""
    return (
        f"<tr>"
        f"<td style='padding:9px 16px;color:#555;width:200px;{bg}border-bottom:1px solid #eee;'>"
        f"<strong>{_esc(label)}</strong></td>"
        f"<td style='padding:9px 16px;{bg}{bold}border-bottom:1px solid #eee;'>{_esc(str(value))}</td>"
        f"</tr>"
    )

def create_email_html(job):
    title       = job.get("title", "Untitled Job")
    url         = job.get("url", Config.TARGET_URL)
    detected_at = job.get("detected_at", "")
    job_id      = job.get("id", "")
    description = job.get("description", "")
    location    = job.get("location", "") or "Not specified"
    salary      = job.get("salary", "") or "Not specified"
    job_type    = job.get("job_type", "")
    company     = job.get("company", "") or "Not specified"
    time_posted = job.get("time_posted", "")

    hdr_grad   = "linear-gradient(135deg,#d6001c,#ff4d4d)"
    sec_desc   = "#d6001c"
    sec_detail = "#b3001b"
    sec_meta   = "#6b7280"
    btn_color  = "#d6001c"

    desc_section = ""
    if description:
        paragraphs = _esc(description).replace("\n\n", "|||").replace("\n", "<br>")
        paras = [f"<p style='margin:0 0 10px;'>{p}</p>" for p in paragraphs.split("|||") if p.strip()]
        desc_section = (
            _section_header('📋', 'Job Description', sec_desc) +
            f"<tr><td colspan='2' style='padding:14px 16px;background:#f9fafb;"
            f"font-size:14px;line-height:1.75;color:#333;border-bottom:2px solid #e5e7eb;'>"
            f"{''.join(paras)}</td></tr>"
        )

    detail_rows = (
        _row("Company",  company,  alt=False) +
        _row("Location", location, alt=True) +
        _row("Job Type", job_type, alt=False) +
        _row("Salary",   salary,   alt=True, bold_value=True)
    )
    detail_section = _section_header('📦', 'Job Details', sec_detail) + detail_rows

    meta_rows = (
        _row("Posted",      time_posted, alt=False) +
        _row("Detected at", detected_at, alt=True) +
        _row("Job ID",      job_id,      alt=False)
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,Helvetica,sans-serif;color:#333;">
  <div style="max-width:700px;margin:30px auto;background:#fff;border-radius:10px;
       overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.12);">

    <div style="background:{hdr_grad};padding:24px 28px;">
      <p style="margin:0;color:rgba(255,255,255,0.75);font-size:11px;
          letter-spacing:1.5px;text-transform:uppercase;">Reed Monitor Alert</p>
      <h2 style="margin:6px 0 0;color:#fff;font-size:24px;font-weight:700;">🚀 New Reed IT Job</h2>
    </div>

    <div style="padding:22px 28px 4px;">
      <h3 style="margin:0 0 10px;color:#1a252f;font-size:20px;line-height:1.4;">{_esc(title)}</h3>
    </div>

    <div style="padding:0 28px 28px;">
      <table style="width:100%;border-collapse:collapse;font-size:14px;
             border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
        {detail_section}
        {desc_section}
        {_section_header('🕒', 'Detection Info', sec_meta)}
        {meta_rows}
      </table>
      <div style="text-align:center;margin-top:28px;">
        <a href="{url}" style="display:inline-block;background:{btn_color};color:#fff;
                  padding:14px 36px;text-decoration:none;border-radius:6px;
                  font-weight:bold;font-size:15px;letter-spacing:0.3px;">
          View Job on Reed →
        </a>
      </div>
    </div>

    <div style="background:#f8f9fa;padding:14px 28px;border-top:1px solid #eee;
         font-size:12px;color:#999;text-align:center;">
      Reed Monitor &nbsp;|&nbsp; Automated alert &nbsp;|&nbsp; {detected_at}
    </div>
  </div>
</body></html>"""

def send_notification(job):
    """Send SMTP email notification."""
    if os.getenv("SEND_EMAILS", "True").lower() == "false":
        print(f"🤫 Emails are disabled. Skipping notification for '{job.get('title', 'Unknown')[:30]}'")
        return True
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🔔 Reed IT Jobs: {job.get('title', 'New Job')}"
        msg["From"]    = Config.SENDER_EMAIL
        msg["To"]      = ", ".join(Config.RECIPIENT_EMAILS)
        msg.attach(MIMEText(create_email_html(job), "html"))

        with smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT) as server:
            server.starttls()
            server.login(Config.SENDER_EMAIL, Config.SENDER_PASSWORD)
            server.send_message(msg)

        print(f"📧 Email sent: {job.get('title', 'Unknown')[:50]}...")
        return True
    except Exception as e:
        print(f"❌ Email notification failed: {e}")
        return False

# ============================
# DRIVER SETUP
# ============================
def _find_binary(env_var, candidates):
    import shutil
    val = os.getenv(env_var, "")
    if val and os.path.exists(val):
        return val
    for path in candidates:
        if os.path.exists(path):
            return path
    found = shutil.which(candidates[-1].split('/')[-1])
    return found or ""

def initialize_driver():
    """Launch Chrome WebDriver with anti-bot overrides."""
    options = Options()
    if Config.HEADLESS:
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

    chrome_bin = _find_binary("CHROME_BIN", [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ])
    if chrome_bin:
        options.binary_location = chrome_bin

    from selenium.webdriver.chrome.service import Service

    system_path = _find_binary("CHROMEDRIVER_PATH", [
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
    ])

    if system_path:
        service = Service(system_path)
    else:
        try:
            from webdriver_manager.chrome import ChromeDriverManager
            from webdriver_manager.core.os_manager import ChromeType
            is_chromium = "chromium" in (chrome_bin or "").lower()
            mgr = ChromeDriverManager(chrome_type=ChromeType.CHROMIUM if is_chromium else ChromeType.GOOGLE)
            driver_path = mgr.install()
            service = Service(driver_path)
        except Exception:
            service = Service()

    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd("Network.setUserAgentOverride", {
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    return driver

# ============================
# MAIN LOOP
# ============================
def main():
    print("=" * 50)
    print("🚀 Reed IT Jobs Monitor")
    print("=" * 50)
    print(f"  Target    : {Config.TARGET_URL}")
    print(f"  Interval  : {Config.CHECK_INTERVAL}s")
    print(f"  Max age   : {Config.MAX_AGE_MINUTES} min")
    print(f"  Recipients: {', '.join(Config.RECIPIENT_EMAILS)}")
    print()

    if TEST_MODE:
        print("🧪 RUNNING IN TEST MODE — MongoDB operations skipped, sends 1 test email\n")

    driver = initialize_driver()
    try:
        if TEST_MODE:
            seen_ids = set()
        else:
            cold_start = db_is_cold_start()
            init_db()
            seen_ids = get_seen_ids()
            print(f"📁 Database loaded — {len(seen_ids)} Reed records detected")

            if cold_start:
                print("⚙️  Cold start: seeding database silently with current page listings...")
                seed_jobs = scan_for_jobs(driver)
                if seed_jobs:
                    print(f"  → Seeding {len(seed_jobs)} jobs. Fetching details for each...")
                    for idx, job in enumerate(seed_jobs):
                        print(f"    [{idx+1}/{len(seed_jobs)}] Fetching details for '{job['title'][:40]}'...")
                        details = fetch_job_details(driver, job["url"])
                        job.update(details)
                    bulk_insert_projects(seed_jobs, emailed=False)
                    print(f"✅ Seeding complete. {len(seed_jobs)} jobs cached. Monitoring for future new posts.")
                    seen_ids = get_seen_ids()
                else:
                    print("⚠️  No jobs found to seed on startup. Skipping...")

        check_count = 0
        while True:
            try:
                check_count += 1
                print(f"\n{'='*30}")
                print(f"🔄 Check #{check_count} — {datetime.now(PKT).strftime('%H:%M:%S')} PKT")
                print(f"{'='*30}")

                all_jobs = scan_for_jobs(driver)
                if not all_jobs:
                    print("⚠️  No jobs found in this scan.")
                    if ONCE_MODE:
                        break
                    time.sleep(Config.CHECK_INTERVAL)
                    continue

                new_jobs = filter_new_jobs(all_jobs, seen_ids)

                if TEST_MODE and all_jobs and not seen_ids:
                    job = all_jobs[0]
                    print(f"🧪 Test mode: fetching details for '{job['title'][:40]}'")
                    details = fetch_job_details(driver, job["url"])
                    job.update(details)
                    print(f"🧪 Test mode: sending alert for job '{job['title'][:40]}'")
                    send_notification(job)
                    for j in all_jobs:
                        seen_ids.add(j["id"])
                elif new_jobs:
                    print(f"🎯 Found {len(new_jobs)} new job(s)!")
                    for job in new_jobs:
                        print(f"  → Scraped job '{job['title'][:50]}'. Fetching details...")
                        details = fetch_job_details(driver, job["url"])
                        job.update(details)

                        emailed = send_notification(job)
                        if not TEST_MODE:
                            insert_project(job, emailed=emailed)
                        seen_ids.add(job["id"])
                else:
                    print("⏳ No new jobs detected.")

                print(f"📊 Stats: {len(all_jobs)} visible, {len(seen_ids)} total seen")

                if ONCE_MODE:
                    print("\n✅ Once mode complete. Exiting...")
                    break

                time.sleep(Config.CHECK_INTERVAL)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"⚠️ Check cycle failed: {e}. Reinitializing driver...")
                try:
                    driver.quit()
                except Exception:
                    pass
                time.sleep(Config.CHECK_INTERVAL)
                driver = initialize_driver()

    except KeyboardInterrupt:
        print("\n⏹️ Monitor stopped by user.")
    except Exception as e:
        print(f"\n💥 Fatal crash: {e}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        print("✅ Reed Monitor stopped.")

if __name__ == "__main__":
    main()

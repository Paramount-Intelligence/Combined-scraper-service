import sys
import time
import smtplib
import json
import os
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
from dotenv import load_dotenv

# Ensure UTF-8 output on all platforms (fixes Windows emoji crash)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# Load .env file
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time (UTC+5)

# ============================
# CONFIGURATION
# ============================
class Config:
    PLATFORM_NAME = "mbopartners"
    SESSION_KEY = "mbop_cookies"
    PROJECTS_COLLECTION = "projects"  # Shared MongoDB collection
    DB_NAME = "office_monitor"
    COLLECTION_NAME = "sessions"
    
    SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SENDER_EMAIL = os.getenv("SENDER_EMAIL")
    SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
    RECIPIENT_EMAILS = [
        e.strip() for e in os.getenv("RECIPIENT_EMAILS", "").split(",") if e.strip()
    ]
    
    CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 60))
    HEADLESS = os.getenv("HEADLESS", "True").lower() == "true"
    COOKIES_FILE = "mbop_cookies.json"
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
    TARGET_URL = "https://connect.mbopartners.com/opportunities/associate/list"
    BASE_URL = "https://connect.mbopartners.com"

# CLI Options
DEBUG_MODE = "--debug" in sys.argv
ONCE_MODE  = "--once"  in sys.argv
TEST_MODE  = "--test"  in sys.argv

def debug_print(msg):
    if DEBUG_MODE:
        print(msg)

# ============================
# SESSION MANAGEMENT
# ============================
_mongo_client = None

def _get_session_collection():
    """MongoDB collection for storing sessions."""
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(Config.MONGO_URI)
    return _mongo_client[Config.DB_NAME][Config.COLLECTION_NAME]

def load_cookies(driver):
    """Load cookies and localStorage from MongoDB and register CDP script injection."""
    session_data = None
    try:
        doc = _get_session_collection().find_one({"_id": Config.SESSION_KEY})
        if doc and doc.get("cookies"):
            session_data = doc
            print("  Loaded cookies from MongoDB")
    except Exception as e:
        print(f"  ⚠️ Could not load cookies from MongoDB: {e}")
        
    if not session_data:
        if os.path.exists(Config.COOKIES_FILE):
            try:
                with open(Config.COOKIES_FILE, 'r') as f:
                    session_data = json.load(f)
                print("  Loaded cookies from local file")
            except:
                pass
                
    if not session_data or not session_data.get("cookies"):
        return False
        
    try:
        # Enable CDP domains
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Page.enable", {})
        
        # Apply cookies via CDP Network.setCookie
        for cookie in session_data["cookies"]:
            domain = cookie.get("domain", "")
            if not domain:
                domain = ".mbopartners.com"
                
            cdp_cookie = {
                "name": cookie["name"],
                "value": cookie["value"],
                "domain": domain,
                "path": cookie.get("path", "/"),
                "secure": cookie.get("secure", False)
            }
            if cookie.get("expiry") is not None:
                try:
                    cdp_cookie["expires"] = int(cookie["expiry"])
                except:
                    pass
            try:
                driver.execute_cdp_cmd("Network.setCookie", cdp_cookie)
            except Exception:
                pass
                
        # Register LocalStorage pre-emptive injection via script evaluation
        if session_data.get("local_storage"):
            js_injection = ""
            for key, val in session_data["local_storage"].items():
                if key in ["getItem", "setItem", "removeItem", "clear", "key", "length"]:
                    continue
                str_val = json.dumps(val) if not isinstance(val, str) else val
                escaped_val = str_val.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")
                js_injection += f"window.localStorage.setItem('{key}', '{escaped_val}');\n"
                
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": js_injection
            })
            
        return True
    except Exception as e:
        print(f"  ⚠️ Error applying session: {e}")
        return False

def is_logged_in(driver):
    """Check if we are successfully logged in and loaded the opportunities page."""
    try:
        current_url = driver.current_url.lower()
        if "auth.mbopartners.com" in current_url or "signin" in current_url:
            return False
        return "opportunities" in current_url
    except:
        return False

# ============================
# PROJECT EXTRACTION
# ============================
def scan_for_projects(driver):
    """Scrape the MBO Partners portal for opportunities by navigating details."""
    try:
        if not is_logged_in(driver):
            debug_print(f"Navigating to: {Config.TARGET_URL}")
            driver.get(Config.TARGET_URL)
            time.sleep(10)
            
        if not is_logged_in(driver):
            print("❌ Authentication failed or session expired. Redirected to login page.")
            return []
            
        headings = driver.find_elements(By.CSS_SELECTOR, "h4.opportunity-heading")
        if not headings:
            print("⚠️ No opportunities found on current page.")
            return []
            
        total_cards = len(headings)
        print(f"✅ Located {total_cards} opportunity cards. Extracting details...")
        
        projects = []
        
        # Limit scanning to first 15 opportunities to prevent excessive navigate loops
        scan_limit = min(total_cards, 15)
        
        for i in range(scan_limit):
            try:
                # Re-fetch headings to prevent stale element issues
                headings = driver.find_elements(By.CSS_SELECTOR, "h4.opportunity-heading")
                if i >= len(headings):
                    break
                    
                heading = headings[i]
                title = heading.text.strip()
                if not title:
                    title = heading.get_attribute("title") or "Untitled Opportunity"
                title = title.strip()
                
                # Navigate to details
                driver.execute_script("arguments[0].click();", heading)
                time.sleep(5)
                
                current_url = driver.current_url
                body_text = driver.find_element(By.TAG_NAME, "body").text
                
                # Parse ID from detail URL
                project_id = ""
                m_id = re.search(r'opportunities/details/([a-zA-Z0-9]+)', current_url)
                if m_id:
                    project_id = m_id.group(1)
                    
                job_id = ""
                m_job = re.search(r'Job ID:\s*(\S+)', body_text)
                if m_job:
                    job_id = m_job.group(1)
                    
                final_id = job_id or project_id or hashlib.md5(title.encode()).hexdigest()[:12]
                
                location = ""
                client = ""
                lines = body_text.splitlines()
                for line_idx, line in enumerate(lines):
                    if "Job ID:" in line and line_idx + 1 < len(lines):
                        next_line = lines[line_idx+1].strip()
                        if " - " in next_line:
                            parts = next_line.split(" - ", 1)
                            client = parts[0].strip()
                            location = parts[1].strip()
                        else:
                            location = next_line
                        break
                        
                budget = "Not specified"
                m_budget = re.search(r'(\$[0-9,.-]+\s*(?:hourly|fixed|daily)[^%\n]*)', body_text, re.IGNORECASE)
                if m_budget:
                    budget = m_budget.group(1).strip()
                else:
                    m_budget2 = re.search(r'(\$[0-9,.-]+\s*USD)', body_text)
                    if m_budget2:
                        budget = m_budget2.group(1).strip()
                        
                duration = ""
                m_dur = re.search(r'Date:\s*\n*(.+)', body_text, re.IGNORECASE)
                if m_dur:
                    duration = m_dur.group(1).strip()
                    
                description = ""
                if "Description" in body_text:
                    description = body_text.split("Description", 1)[1].strip()
                else:
                    description = body_text[:1500]
                    
                project = {
                    "id": final_id,
                    "title": title,
                    "description": description,
                    "location": location,
                    "client": client,
                    "budget": budget,
                    "duration": duration,
                    "status": "Open",
                    "url": current_url,
                    "detected_at": datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S")
                }
                
                if project.get("title") and project.get("id"):
                    projects.append(project)
                    debug_print(f"  Scraped card [{i+1}/{scan_limit}]: '{title}' ({final_id})")
                    
                # Navigate back
                driver.execute_script("window.history.back();")
                time.sleep(4)
                
            except Exception as card_err:
                debug_print(f"  ⚠️ Error parsing card {i}: {card_err}")
                try:
                    driver.get(Config.TARGET_URL)
                    time.sleep(5)
                except:
                    pass
                    
        print(f"✅ Extracted {len(projects)} valid projects")
        return projects
    except Exception as e:
        print(f"❌ Error scanning MBO Partners Connect: {e}")
        return []

# ============================
# PROJECT DATABASE (MongoDB)
# ============================
_mongo_projects_client = None

def _get_projects_collection():
    """Shared database collection 'projects'."""
    global _mongo_projects_client
    if _mongo_projects_client is None:
        _mongo_projects_client = MongoClient(Config.MONGO_URI)
    return _mongo_projects_client[Config.DB_NAME][Config.PROJECTS_COLLECTION]

def init_db():
    """Initialize MongoDB project unique indices."""
    try:
        _get_projects_collection().create_index("project_id", unique=True, name="idx_project_id_unique")
    except Exception:
        pass

def db_is_cold_start():
    """Returns True if database has no MBO Partners records."""
    doc = _get_projects_collection().find_one({"platform": Config.PLATFORM_NAME}, {"_id": 1})
    return doc is None

def get_seen_ids():
    """Retrieve set of project IDs already stored for MBO Partners."""
    try:
        docs = _get_projects_collection().find({"platform": Config.PLATFORM_NAME}, {"project_id": 1, "_id": 0})
        return {d["project_id"] for d in docs if d.get("project_id")}
    except Exception as e:
        print(f"  ⚠️ Error loading seen IDs: {e}")
        return set()

def insert_project(project, emailed=True):
    """Insert one project into MongoDB shared collection."""
    try:
        doc = {
            "project_id":       project.get("id"),
            "title":            project.get("title"),
            "description":      project.get("description"),
            "location":         project.get("location"),
            "budget":           project.get("budget"),
            "duration":         project.get("duration"),
            "status":           project.get("status"),
            "url":              project.get("url"),
            "detected_at":      project.get("detected_at"),
            "platform":         Config.PLATFORM_NAME,
            "emailed":          bool(emailed),
            
            # Platform specific details
            "client":           project.get("client", ""),
        }
        _get_projects_collection().update_one(
            {"project_id": doc["project_id"]},
            {"$setOnInsert": doc},
            upsert=True
        )
    except Exception as e:
        print(f"⚠️ DB insert failed: {e}")

def bulk_insert_projects(projects, emailed=False):
    """Seed DB with multiple projects silently (used on cold start)."""
    try:
        ops = []
        for p in projects:
            if not p.get("id"):
                continue
            doc = {
                "project_id":  p.get("id"),
                "title":       p.get("title"),
                "description": p.get("description"),
                "location":    p.get("location"),
                "budget":      p.get("budget"),
                "duration":    p.get("duration"),
                "status":      p.get("status"),
                "url":         p.get("url"),
                "detected_at": p.get("detected_at"),
                "platform":    Config.PLATFORM_NAME,
                "emailed":     bool(emailed),
                "client":      p.get("client", ""),
            }
            ops.append(UpdateOne({"project_id": doc["project_id"]}, {"$setOnInsert": doc}, upsert=True))
        if ops:
            result = _get_projects_collection().bulk_write(ops, ordered=False)
            print(f"  DB: Seeded {result.upserted_count} records to shared collection (platform: {Config.PLATFORM_NAME})")
    except Exception as e:
        print(f"⚠️ DB bulk seed failed: {e}")

def filter_new_projects(all_projects, seen_ids):
    """Filter out projects that were already captured."""
    return [p for p in all_projects if p.get("id") and p["id"] not in seen_ids]

# ============================
# EMAIL NOTIFICATIONS
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

def create_email_html(project):
    title         = project.get('title', 'Untitled Project')
    url           = project.get('url', Config.TARGET_URL)
    detected_at   = project.get('detected_at', '')
    project_id    = project.get('id', '')
    location      = project.get('location', '')
    client        = project.get('client', '')
    duration      = project.get('duration', '')
    budget        = project.get('budget', '') or 'Not provided'

    hdr_grad   = "linear-gradient(135deg, #007F9C, #009CA6)"
    sec_logist = "#009CA6"
    sec_budget = "#1d4ed8"
    btn_color  = "#009CA6"

    logistics_rows = (
        _row("Client / Partner",        client or "Not specified",        alt=False) +
        _row("Location",                location or "Not specified",      alt=True) +
        _row("Dates / Timeline",        duration or "Not specified",      alt=False)
    )
    logistics_section = _section_header('📦', 'Details', sec_logist) + logistics_rows

    budget_section = (
        _section_header('💰', 'Compensation', sec_budget) +
        _row("Estimated Rate", budget, bold_value=True)
    )

    meta_rows = (
        _row("Detected at", detected_at, alt=False) +
        _row("Project ID",  project_id, alt=True)
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,Helvetica,sans-serif;color:#333;">
  <div style="max-width:700px;margin:30px auto;background:#fff;border-radius:10px;
       overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.12);">

    <div style="background:{hdr_grad};padding:24px 28px;">
      <p style="margin:0;color:rgba(255,255,255,0.75);font-size:11px;
          letter-spacing:1.5px;text-transform:uppercase;">MBO Partners Opportunity Monitor</p>
      <h2 style="margin:6px 0 0;color:#fff;font-size:24px;font-weight:700;">🚀 New Opportunity Alert</h2>
    </div>

    <div style="padding:22px 28px 4px;">
      <h3 style="margin:0 0 10px;color:#1a252f;font-size:20px;line-height:1.4;">{_esc(title)}</h3>
    </div>

    <div style="padding:0 28px 28px;">
      <table style="width:100%;border-collapse:collapse;font-size:14px;
             border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
        {logistics_section}
        {budget_section}
        {_section_header('🕒', 'Detection Info', '#6b7280')}
        {meta_rows}
      </table>
      <div style="text-align:center;margin-top:28px;">
        <a href="{url}" style="display:inline-block;background:{btn_color};color:#fff;
                  padding:14px 36px;text-decoration:none;border-radius:6px;
                  font-weight:bold;font-size:15px;letter-spacing:0.3px;">
          View Full Opportunity on MBO Partners →
        </a>
      </div>
    </div>

    <div style="background:#f8f9fa;padding:14px 28px;border-top:1px solid #eee;
         font-size:12px;color:#999;text-align:center;">
      MBO Partners Monitor &nbsp;|&nbsp; Automated alert &nbsp;|&nbsp; {detected_at}
    </div>
  </div>
</body></html>"""

def send_notification(project):
    """Send email notification for a new MBO Partners opportunity."""
    if os.getenv("SEND_EMAILS", "True").lower() == "false":
        print(f"🤫 Emails are disabled. Skipping notification for '{project.get('title', 'Unknown')[:30]}'")
        return True
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🔔 MBO Partners: {project.get('title', 'New Opportunity')}"
        msg["From"] = Config.SENDER_EMAIL
        msg["To"] = ", ".join(Config.RECIPIENT_EMAILS)
        
        msg.attach(MIMEText(create_email_html(project), "html"))
        
        with smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT) as server:
            server.starttls()
            server.login(Config.SENDER_EMAIL, Config.SENDER_PASSWORD)
            server.send_message(msg)
            
        print(f"📧 Email sent: {project.get('title', 'Unknown')[:50]}...")
        return True
    except Exception as e:
        print(f"❌ Email failed: {e}")
        return False

# ============================
# DRIVER INITIALIZATION
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
    """Initialize Chrome WebDriver."""
    options = Options()
    if Config.HEADLESS:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    options.add_argument(f"user-agent={user_agent}")

    chrome_bin = _find_binary("CHROME_BIN", [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ])
    if chrome_bin:
        options.binary_location = chrome_bin

    system_path = _find_binary("CHROMEDRIVER_PATH", [
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
        "/usr/lib/chromium-browser/chromedriver",
    ])
    
    from selenium.webdriver.chrome.service import Service
    if system_path:
        service = Service(system_path)
    else:
        service = Service()

    driver = webdriver.Chrome(service=service, options=options)
    return driver

# ============================
# MAIN MONITORING LOOP
# ============================
def main():
    print("=" * 50)
    print("🚀 MBO Partners Opportunity Monitor")
    print("=" * 50)
    
    driver = initialize_driver()
    
    try:
        print("Initializing session...")
        if not load_cookies(driver):
            print("❌ Failed to load cookies. Please run save_mbop_cookies.py first.")
            return
            
        print("Navigating to portal...")
        driver.get(Config.TARGET_URL)
        time.sleep(10)
        
        if not is_logged_in(driver):
            print("❌ Initial session validation failed. Cookies might be expired.")
            return
            
        cold_start = db_is_cold_start()
        init_db()
        seen_ids = get_seen_ids()
        print(f"📁 Database loaded — {len(seen_ids)} MBO Partners records detected\n")
        
        # ── COLD START / STARTUP SEEDING ─────────────────────────────────────
        if cold_start:
            print("⚙️  First run (cold start) — reconciling current page silently (no emails sent)...")
            seed_projects = scan_for_projects(driver)
            if seed_projects:
                bulk_insert_projects(seed_projects, emailed=False)
                seen_ids = get_seen_ids()
                print(f"✅ Reconciled — {len(seed_projects)} opportunities stored. Monitoring for future new posts.\n")
            else:
                print("⚠️  No opportunities found to seed on startup. Skipping...\n")
        else:
            print("⚙️  Restart — skipping silent reconciliation. Active monitoring will start immediately.\n")
            
        if ONCE_MODE:
            print("✅ Once mode complete. Exiting...")
            return
            
        # ── MONITORING LOOP ──────────────────────────────────────────────────
        check_count = 0
        while True:
            try:
                check_count += 1
                print(f"\n{'='*30}")
                print(f"🔄 Check #{check_count} — {datetime.now(PKT).strftime('%H:%M:%S')} PKT")
                print(f"{'='*30}")
                
                driver.get(Config.TARGET_URL)
                time.sleep(10)
                
                all_projects = scan_for_projects(driver)
                
                if not all_projects:
                    print("⚠️  No projects found in this scan.")
                else:
                    new_projects = filter_new_projects(all_projects, seen_ids)
                    
                    if TEST_MODE and all_projects and not seen_ids:
                        # Test mode fallback: send one test alert if no new items
                        project = all_projects[0]
                        print(f"🧪 Test mode: sending alert for project '{project['title']}'")
                        send_notification(project)
                    elif new_projects:
                        print(f"🎯 Found {len(new_projects)} new opportunity(s)!")
                        for project in new_projects:
                            print(f"  Sending alert for '{project['title']}'...")
                            if send_notification(project):
                                insert_project(project, emailed=True)
                                seen_ids.add(project["id"])
                    else:
                        print("⏳ No new opportunities detected.")
                        
            except Exception as loop_err:
                print(f"⚠️ Error in checks loop: {loop_err}")
                
            time.sleep(Config.CHECK_INTERVAL)
            
    except Exception as e:
        print(f"❌ Main error: {e}")
    finally:
        print("Closing browser.")
        driver.quit()

if __name__ == "__main__":
    main()

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
from selenium.webdriver.common.keys import Keys
from dotenv import load_dotenv

# Ensure UTF-8 output on all platforms (fixes Windows emoji crash)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# Load .env file from this script's directory
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time (UTC+5)

# ============================
# CONFIGURATION
# ============================
class Config:
    PLATFORM_NAME = "eond"
    SESSION_KEY = "eond_cookies"
    PROJECTS_COLLECTION = "projects"  # Shared MongoDB collection
    
    EOND_EMAIL    = os.getenv("EOND_EMAIL")
    EOND_PASSWORD = os.getenv("EOND_PASSWORD")
    
    SMTP_SERVER  = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT    = int(os.getenv("SMTP_PORT", 587))
    SENDER_EMAIL    = os.getenv("SENDER_EMAIL")
    SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
    RECIPIENT_EMAILS = [
        e.strip() for e in os.getenv("RECIPIENT_EMAILS", "").split(",") if e.strip()
    ]
    
    CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL", 60))
    MAX_AGE_MINUTES = int(os.getenv("MAX_AGE_MINUTES", 60))
    HEADLESS     = os.getenv("HEADLESS", "True").lower() == "true"
    COOKIES_FILE = "eond_cookies.json"
    MONGO_URI    = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
    
    BASE_URL    = "https://member.eond.eu"
    TARGET_URL  = "https://member.eond.eu/marketplace"

# CLI Options
DEBUG_MODE = "--debug" in sys.argv
ONCE_MODE  = "--once"  in sys.argv
TEST_MODE  = "--test"  in sys.argv

def debug_print(msg):
    if DEBUG_MODE:
        print(msg)

def clean_val(t):
    if not t: return ""
    return re.sub(r'\s+', ' ', t).strip()

def dump_page_structure(driver):
    """Dump information about page structure for diagnostic purposes when elements aren't found."""
    print("\n" + "="*60)
    print("🔍 DIAGNOSTICS: EOND PAGE STRUCTURE DUMP")
    print("="*60)
    print(f"  URL: {driver.current_url}")
    
    # Check common container structures
    card_candidates = [
        "article", ".card", "[class*='card']", "[class*='project']",
        "[class*='job']", "[class*='brief']", "[class*='opportunity']",
        "li[class]", "div[class*='item']"
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
                print(f"  [{len(elems)}] {sel}  → <{tag} class='{cls[:50]}'> text='{txt}'")
        except:
            pass
            
    # Check headers
    print("\n📝 Headers / Titles:")
    for sel in ["h1", "h2", "h3", "h4", "[class*='title']", "[class*='heading']"]:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            if elems:
                for e in elems[:3]:
                    txt = e.text.strip()[:80] if e.text else ""
                    if txt:
                        print(f"  <{e.tag_name} class='{(e.get_attribute('class') or '')[:40]}'> → {txt}")
        except:
            pass
    print("="*60 + "\n")

# ============================
# SESSION MANAGEMENT
# ============================
_mongo_client = None

def _get_session_collection():
    """MongoDB collection for storing sessions."""
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(Config.MONGO_URI)
    return _mongo_client["office_monitor"]["sessions"]

def save_cookies(driver):
    """Save cookies and localStorage to MongoDB and local backup file."""
    try:
        cookies = driver.get_cookies()
        local_storage = driver.execute_script("return window.localStorage;")
        
        session_data = {
            "cookies": cookies,
            "local_storage": local_storage,
            "saved_at": datetime.now(timezone.utc)
        }
        
        # Save to DB
        _get_session_collection().update_one(
            {"_id": Config.SESSION_KEY},
            {"$set": session_data},
            upsert=True
        )
        
        # Local JSON backup
        try:
            with open(Config.COOKIES_FILE, 'w') as f:
                json.dump({
                    "cookies": cookies,
                    "local_storage": local_storage,
                    "saved_at": datetime.now(timezone.utc).isoformat()
                }, f)
        except Exception:
            pass
            
        return True
    except Exception as e:
        print(f"  ⚠️ Could not save cookies to MongoDB: {e}")
        return False

def load_cookies(driver):
    """Load cookies and localStorage from MongoDB or local backup file."""
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
        driver.get(Config.BASE_URL)
        time.sleep(2)
        driver.delete_all_cookies()
        
        # Determine cookies domains
        for cookie in session_data["cookies"]:
            if 'domain' in cookie and ('eond.eu' in cookie['domain']):
                try:
                    driver.add_cookie(cookie)
                except Exception:
                    pass
                    
        # Apply local storage if saved
        if session_data.get("local_storage"):
            for key, val in session_data["local_storage"].items():
                try:
                    driver.execute_script("window.localStorage.setItem(arguments[0], arguments[1]);", key, val)
                except:
                    pass
        return True
    except Exception as e:
        print(f"  ⚠️ Error applying cookies: {e}")
        return False

def is_logged_in(driver):
    """Check if we are successfully logged in and on dashboard/overview/project page."""
    try:
        current_url = driver.current_url.lower()
        if "login" in current_url or "signin" in current_url or "auth" in current_url:
            return False
        return any(x in current_url for x in ["dashboard", "overview", "projects", "home", "search", "talent", "opportunity", "member"])
    except:
        return False

def perform_login(driver):
    """Log in to EonD using credentials."""
    try:
        print(f"  Navigating to EonD login URL: {Config.TARGET_URL}")
        driver.get(Config.TARGET_URL)
        time.sleep(5)

        if is_logged_in(driver):
            print("  Already authenticated.")
            return True

        # Dismiss any cookie consents
        for consent_sel in [
            "button[id*='cookie']", 
            "button[class*='cookie']",
            "button[aria-label*='Accept']",
            "button[title*='Accept All']"
        ]:
            try:
                btn = driver.find_element(By.CSS_SELECTOR, consent_sel)
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(1.5)
                break
            except:
                pass

        email_field = None
        for sel in ["input[type='email']", "input[name='email']", "input[id*='email']", "input[name='username']", "input[id*='username']"]:
            try:
                email_field = WebDriverWait(driver, 8).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                )
                break
            except:
                continue

        if not email_field:
            print("❌ Could not find email field.")
            dump_page_structure(driver)
            return False

        email_field.click()
        email_field.clear()
        email_field.send_keys(Config.EOND_EMAIL)
        time.sleep(0.5)

        password_field = None
        for sel in ["input[type='password']", "input[name='password']", "input[id*='password']"]:
            try:
                password_field = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                )
                break
            except:
                continue

        if not password_field:
            print("❌ Could not find password field.")
            return False

        password_field.click()
        password_field.clear()
        password_field.send_keys(Config.EOND_PASSWORD)
        time.sleep(0.5)

        # Submit login
        password_field.send_keys(Keys.ENTER)
        print("  Submitted login form via Enter")
        time.sleep(5)

        # Fallback submit button click
        if not is_logged_in(driver):
            for sel in ["button[type='submit']", "input[type='submit']", "button[id*='submit']", "button[class*='btn-primary']", "//button[contains(text(), 'Login') or contains(text(), 'Sign') or contains(text(), 'Enter')]"]:
                try:
                    if sel.startswith("//"):
                        btn = driver.find_element(By.XPATH, sel)
                    else:
                        btn = driver.find_element(By.CSS_SELECTOR, sel)
                    driver.execute_script("arguments[0].click();", btn)
                    print("  Clicked login button")
                    time.sleep(5)
                    break
                except:
                    continue

        # Wait up to 15 seconds for dashboard redirection
        for _ in range(15):
            time.sleep(1)
            if is_logged_in(driver):
                break
        else:
            print(f"❌ Login redirect failed. URL: {driver.current_url}")
            return False

        save_cookies(driver)
        print(f"✅ Login successful -> {driver.current_url}")
        return True
    except Exception as e:
        print(f"❌ Login error: {e}")
        return False

# ============================
# PROJECT EXTRACTION SELECTORS
# ============================
# ============================
# PROJECT EXTRACTION SELECTORS
# ============================
CARD_SELECTORS = ["div.MuiCard-root"]
TITLE_SELECTORS = [".MuiTypography-h6Bold"]


LINK_SELECTORS = [
    "a[href*='/project/']",
    "a[href*='/projects/']",
    "a[href*='/opportunity/']",
    "a[href*='/opportunities/']",
    "a[href*='/brief/']",
    "a.project-title-container",
    "a.toolbar-button"
]

def extract_card_info(card):
    """Extract card-level info from EonD marketplace list."""
    try:
        title = card.find_element(By.CSS_SELECTOR, ".MuiTypography-h6Bold").text.strip()
        
        # Subtitle/Snippet
        snippet = ""
        try:
            snippet = card.find_element(By.CSS_SELECTOR, ".MuiTypography-body2").text.strip()
        except:
            pass
            
        # Industry
        industry = ""
        try:
            industry = card.find_element(By.CSS_SELECTOR, ".MuiTypography-caption").text.strip()
        except:
            pass
            
        # Duration
        duration = ""
        try:
            card_text = card.text
            m_dur = re.search(r'Engagement length:\s*([^\n]+)', card_text)
            if m_dur:
                duration = m_dur.group(1).strip()
        except:
            pass
            
        # Generate signature
        signature = hashlib.md5((title + snippet).encode()).hexdigest()[:12]
        
        return {
            "title": title,
            "snippet": snippet,
            "industry": industry,
            "duration": duration,
            "signature": signature
        }
    except Exception as e:
        print(f"  ⚠️ Error extracting card info: {e}")
        return None

def scan_for_projects(driver):
    """Scrape the marketplace page for project cards."""
    try:
        if not is_logged_in(driver):
            driver.get(Config.TARGET_URL)
            time.sleep(5)
            
        # Wait for cards to load
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.MuiCard-root"))
        )
        time.sleep(2)
        
        cards = driver.find_elements(By.CSS_SELECTOR, "div.MuiCard-root")
        projects = []
        for card in cards:
            p = extract_card_info(card)
            if p and p.get("title"):
                projects.append(p)
                
        print(f"✅ Found {len(projects)} projects on EonD marketplace.")
        return projects
    except TimeoutException:
        print("⏳ Timeout waiting for EonD marketplace to load")
        return []
    except Exception as e:
        print(f"❌ Error scanning EonD: {e}")
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
    return _mongo_projects_client["office_monitor"][Config.PROJECTS_COLLECTION]

def init_db():
    """Initialize MongoDB project unique indices."""
    try:
        _get_projects_collection().create_index("project_id", unique=True, name="idx_project_id_unique")
    except Exception:
        pass

def db_is_cold_start():
    """Returns True if database has no EonD records."""
    doc = _get_projects_collection().find_one({"platform": Config.PLATFORM_NAME}, {"_id": 1})
    return doc is None

def get_seen_identifiers():
    """Retrieve sets of seen project IDs and seen signatures/titles."""
    seen_ids = set()
    seen_signatures = set()
    try:
        docs = _get_projects_collection().find({"platform": Config.PLATFORM_NAME}, {"project_id": 1, "signature": 1, "title": 1, "_id": 0})
        for d in docs:
            if d.get("project_id"):
                seen_ids.add(str(d["project_id"]))
            if d.get("signature"):
                seen_signatures.add(d["signature"])
            elif d.get("title"):
                seen_signatures.add(hashlib.md5(d["title"].encode()).hexdigest()[:12])
    except Exception as e:
        print(f"  ⚠️ Error loading seen identifiers: {e}")
    return seen_ids, seen_signatures

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
            "time_posted":      project.get("time_posted"),
            "status":           project.get("status"),
            "url":              project.get("url"),
            "detected_at":      project.get("detected_at"),
            "platform":         Config.PLATFORM_NAME,
            "emailed":          bool(emailed),
            
            # Platform specific details
            "skills":           project.get("skills", []),
            "start_date":       project.get("start_date", ""),
            "industry":         project.get("industry", ""),
            "client_type":      project.get("client_type", ""),
            "signature":        project.get("signature", "")
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
                "time_posted": p.get("time_posted"),
                "status":      p.get("status"),
                "url":         p.get("url"),
                "detected_at": p.get("detected_at"),
                "platform":    Config.PLATFORM_NAME,
                "emailed":     bool(emailed),
                "skills":      p.get("skills", []),
                "start_date":  p.get("start_date", ""),
                "industry":    p.get("industry", ""),
                "signature":   p.get("signature", "")
            }
            ops.append(UpdateOne({"project_id": doc["project_id"]}, {"$setOnInsert": doc}, upsert=True))
        if ops:
            result = _get_projects_collection().bulk_write(ops, ordered=False)
            print(f"  DB: Seeded {result.upserted_count} records to shared collection (platform: {Config.PLATFORM_NAME})")
    except Exception as e:
        print(f"⚠️ DB bulk seed failed: {e}")

def filter_new_projects(all_projects, seen_signatures):
    """Filter out projects that were already captured by checking signatures."""
    return [p for p in all_projects if p.get("signature") not in seen_signatures]

# ============================
# DETAIL SCANNERS
# ============================
def click_card_by_title(driver, title):
    """Locate the card matching the exact title and click its 'See details' button."""
    try:
        cards = driver.find_elements(By.CSS_SELECTOR, "div.MuiCard-root")
        for card in cards:
            try:
                t_elem = card.find_element(By.CSS_SELECTOR, ".MuiTypography-h6Bold")
                if t_elem.text.strip() == title:
                    btn = card.find_element(By.XPATH, ".//button[contains(text(), 'See details')]")
                    driver.execute_script("arguments[0].click();", btn)
                    return True
            except:
                pass
    except Exception as e:
        print(f"  ⚠️ Error clicking card: {e}")
    return False

def fetch_project_details(driver, title):
    """Click on the card matching project title, extract detailed info, and go back."""
    details = {
        "id": "",
        "url": "",
        "description": "",
        "skills": [],
        "start_date": "",
        "location": "Not specified",
        "industry": "",
        "budget": "Not specified",
        "duration": ""
    }
    
    try:
        if not click_card_by_title(driver, title):
            print(f"  ⚠️ Could not find card for '{title}' to fetch details.")
            return details
            
        try:
            WebDriverWait(driver, 10).until(
                lambda d: "/marketplace/" in d.current_url
            )
        except Exception as e:
            print(f"  ⚠️ URL did not change after details click: {driver.current_url}")
            return details
            
        details["url"] = driver.current_url
        m = re.search(r'/marketplace/(\d+)', driver.current_url)
        if m:
            details["id"] = m.group(1)
            
        time.sleep(3)
        body_text = driver.find_element(By.TAG_NAME, "body").text
        
        # Parse fields line-by-line
        lines = [line.strip() for line in body_text.splitlines() if line.strip()]
        location = "Not specified"
        start_date = ""
        industry = ""
        skills = []
        description_lines = []
        
        in_tags = False
        in_description = False
        
        for idx, line in enumerate(lines):
            if line.startswith("Type of cooperation:"):
                location = line.replace("Type of cooperation:", "").strip()
                continue
            if line.startswith("Starting date:"):
                start_date = line.replace("Starting date:", "").strip()
                continue
            if line == "Industry" and idx + 1 < len(lines):
                industry = lines[idx + 1]
                continue
            if line == "Tags":
                in_tags = True
                in_description = False
                continue
            if line.startswith("Project description and profile requirements") or line.startswith("Project description"):
                in_tags = False
                in_description = True
                continue
            if line.startswith("Referrals") or line.startswith("Apply") or line.startswith("Do you know someone"):
                in_tags = False
                in_description = False
                continue
                
            if in_tags:
                skills.append(line)
            elif in_description:
                description_lines.append(line)
                
        details["location"] = location
        details["start_date"] = start_date
        details["industry"] = industry
        details["skills"] = skills
        details["description"] = "\n".join(description_lines).strip()
        
        # Pull duration/budget if inside body text
        for line in lines:
            if "Engagement length:" in line:
                details["duration"] = line.replace("Engagement length:", "").strip()
                
    except Exception as e:
        print(f"  ⚠️ Detail fetch failed for '{title}': {e}")
    finally:
        try:
            driver.execute_script("window.history.back();")
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.MuiCard-root"))
            )
            time.sleep(2)
        except Exception as e:
            print(f"  ⚠️ Error returning to marketplace: {e}")
            driver.get(Config.TARGET_URL)
            time.sleep(5)
            
    return details

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

def create_email_html(project):
    title       = project.get("title", "Untitled Project")
    url         = project.get("url", Config.TARGET_URL)
    detected_at = project.get("detected_at", "")
    project_id  = project.get("id", "")
    location    = project.get("location", "") or "Remote / Not specified"
    budget      = project.get("budget", "") or "Not provided"
    duration    = project.get("duration", "")
    start_date  = project.get("start_date", "")
    skills      = project.get("skills", [])

    hdr_grad   = "linear-gradient(135deg,#0d1117,#21262d)"
    sec_detail = "#21262d"
    sec_budget = "#1f6feb"
    btn_color  = "#0d1117"

    skills_display = ", ".join(skills) if skills else ""

    detail_rows = (
        _row("Location",    location,                   alt=False) +
        _row("Duration",    duration,                   alt=True) +
        _row("Start Date",  start_date,                 alt=False) +
        _row("Skills/Tools", skills_display,            alt=True)
    )
    detail_section = _section_header('📦', 'Project Details', sec_detail) + detail_rows

    budget_section = (
        _section_header('💰', 'Compensation', sec_budget) +
        _row("Rate / Budget", budget, bold_value=True)
    )

    meta_rows = (
        _row("Detected at", detected_at, alt=True) +
        _row("Project ID",  project_id,  alt=False)
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,Helvetica,sans-serif;color:#333;">
  <div style="max-width:700px;margin:30px auto;background:#fff;border-radius:10px;
       overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.12);">

    <div style="background:{hdr_grad};padding:24px 28px;">
      <p style="margin:0;color:rgba(255,255,255,0.75);font-size:11px;
          letter-spacing:1.5px;text-transform:uppercase;">EonD Monitor Alert</p>
      <h2 style="margin:6px 0 0;color:#fff;font-size:24px;font-weight:700;">🚀 New EonD Project</h2>
    </div>

    <div style="padding:22px 28px 4px;">
      <h3 style="margin:0 0 10px;color:#1a252f;font-size:20px;line-height:1.4;">{_esc(title)}</h3>
    </div>

    <div style="padding:0 28px 28px;">
      <table style="width:100%;border-collapse:collapse;font-size:14px;
             border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
        {detail_section}
        {budget_section}
        {_section_header('🕒', 'Detection Info', '#6b7280')}
        {meta_rows}
      </table>
      <div style="text-align:center;margin-top:28px;">
        <a href="{url}" style="display:inline-block;background:{btn_color};color:#fff;
                  padding:14px 36px;text-decoration:none;border-radius:6px;
                  font-weight:bold;font-size:15px;letter-spacing:0.3px;">
          View Project on EonD →
        </a>
      </div>
    </div>

    <div style="background:#f8f9fa;padding:14px 28px;border-top:1px solid #eee;
         font-size:12px;color:#999;text-align:center;">
      EonD Monitor &nbsp;|&nbsp; Automated alert &nbsp;|&nbsp; {detected_at}
    </div>
  </div>
</body></html>"""

def send_notification(project):
    """Send SMTP email notification."""
    if os.getenv("SEND_EMAILS", "True").lower() == "false":
        print(f"🤫 Emails are disabled. Skipping notification for '{project.get('title', 'Unknown')[:30]}'")
        return True
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"🔔 EonD: {project.get('title', 'New Project')}"
        msg["From"]    = Config.SENDER_EMAIL
        msg["To"]      = ", ".join(Config.RECIPIENT_EMAILS)
        msg.attach(MIMEText(create_email_html(project), "html"))

        with smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT) as server:
            server.starttls()
            server.login(Config.SENDER_EMAIL, Config.SENDER_PASSWORD)
            server.send_message(msg)

        print(f"📧 Email sent: {project.get('title', 'Unknown')[:50]}...")
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

def setup_session(driver):
    """Attempt login via cached cookies or live login."""
    if load_cookies(driver):
        driver.get(Config.TARGET_URL)
        time.sleep(5)
        if is_logged_in(driver):
            print("✅ Session established via cached cookies")
            return True
        print("  Cookies expired or invalid. Authenticating...")
    
    return perform_login(driver)

# ============================
# MAIN LOOP
# ============================
def main():
    print("=" * 50)
    print("🚀 EonD Project Monitor")
    print("=" * 50)
    print(f"  Account   : {Config.EOND_EMAIL}")
    print(f"  Interval  : {Config.CHECK_INTERVAL}s")
    print(f"  Recipients: {', '.join(Config.RECIPIENT_EMAILS)}")
    print()

    if TEST_MODE:
        print("🧪 RUNNING IN TEST MODE — MongoDB operations skipped, sends 1 test email\n")

    driver = initialize_driver()
    try:
        if not setup_session(driver):
            print("❌ Failed to authenticate EonD session. Exiting...")
            return

        if TEST_MODE:
            seen_ids = set()
            seen_signatures = set()
        else:
            cold_start = db_is_cold_start()
            init_db()
            seen_ids, seen_signatures = get_seen_identifiers()
            print(f"📁 Database loaded — {len(seen_signatures)} EonD records detected")

            # Cold Start Seeding
            if cold_start:
                print("⚙️  Cold start: seeding database silently with current page listings...")
                seed_projects = scan_for_projects(driver)
                if seed_projects:
                    print(f"  → Seeding {len(seed_projects)} projects. Fetching details for each...")
                    for idx, project in enumerate(seed_projects):
                        print(f"    [{idx+1}/{len(seed_projects)}] Fetching details for '{project['title'][:40]}'...")
                        details = fetch_project_details(driver, project["title"])
                        project.update(details)
                        project["detected_at"] = datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S")
                        project["status"] = "Open"
                        project["time_posted"] = "Recently"
                    bulk_insert_projects(seed_projects, emailed=False)
                    print(f"✅ Seeding complete. {len(seed_projects)} projects cached. Monitoring for future new posts.")
                    seen_ids, seen_signatures = get_seen_identifiers()
                else:
                    print("⚠️  No projects found to seed on startup. Skipping...")

        check_count = 0
        while True:
            try:
                check_count += 1
                print(f"\n{'='*30}")
                print(f"🔄 Check #{check_count} — {datetime.now(PKT).strftime('%H:%M:%S')} PKT")
                print(f"{'='*30}")

                driver.get(Config.TARGET_URL)
                time.sleep(4)

                # Re-auth check
                if not is_logged_in(driver):
                    print("  ⚠️ Session expired. Logging in again...")
                    if not perform_login(driver):
                        print("  ❌ Re-login failed. Skipping cycle...")
                        time.sleep(Config.CHECK_INTERVAL)
                        continue
                    driver.get(Config.TARGET_URL)
                    time.sleep(4)

                all_projects = scan_for_projects(driver)
                if not all_projects:
                    print("⚠️  No projects found in this scan.")
                    if ONCE_MODE:
                        break
                    time.sleep(Config.CHECK_INTERVAL)
                    continue

                new_projects = filter_new_projects(all_projects, seen_signatures)

                if TEST_MODE and all_projects and not seen_signatures:
                    project = all_projects[0]
                    print(f"🧪 Test mode: fetching details for '{project['title'][:40]}'")
                    details = fetch_project_details(driver, project["title"])
                    project.update(details)
                    project["detected_at"] = datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S")
                    project["status"] = "Open"
                    project["time_posted"] = "Recently"
                    print(f"🧪 Test mode: sending alert for project '{project['title'][:40]}'")
                    send_notification(project)
                    seen_signatures.add(project["signature"])
                    if project.get("id"):
                        seen_ids.add(project["id"])
                elif new_projects:
                    print(f"🎯 Found {len(new_projects)} new project(s)!")
                    for project in new_projects:
                        print(f"  → Scraped brief '{project['title'][:50]}'. Fetching details...")
                        details = fetch_project_details(driver, project["title"])
                        project.update(details)
                        project["detected_at"] = datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S")
                        project["status"] = "Open"
                        project["time_posted"] = "Recently"

                        emailed = send_notification(project)
                        if not TEST_MODE:
                            insert_project(project, emailed=emailed)
                        seen_signatures.add(project["signature"])
                        if project.get("id"):
                            seen_ids.add(project["id"])
                else:
                    print("⏳ No new projects detected.")

                print(f"📊 Stats: {len(all_projects)} visible, {len(seen_signatures)} total seen")

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
                except:
                    pass
                time.sleep(Config.CHECK_INTERVAL)
                driver = initialize_driver()
                setup_session(driver)

    except KeyboardInterrupt:
        print("\n⏹️ Monitor stopped by user.")
    except Exception as e:
        print(f"\n💥 Fatal crash: {e}")
    finally:
        try:
            driver.quit()
        except:
            pass
        print("✅ EonD Monitor stopped.")

if __name__ == "__main__":
    main()

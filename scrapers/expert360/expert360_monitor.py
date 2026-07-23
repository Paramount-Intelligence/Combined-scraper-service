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
    sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', line_buffering=True)

# Load .env: local scraper dir first, then repo root
_script_dir = os.path.dirname(os.path.abspath(__file__))
_root_dir = os.path.abspath(os.path.join(_script_dir, "..", ".."))
if os.path.exists(os.path.join(_script_dir, ".env")):
    load_dotenv(dotenv_path=os.path.join(_script_dir, ".env"))
load_dotenv(dotenv_path=os.path.join(_root_dir, ".env"))

PKT = timezone(timedelta(hours=5))  # Pakistan Standard Time (UTC+5)

# ============================
# CONFIGURATION
# ============================
class Config:
    PLATFORM_NAME = "expert360"
    SESSION_KEY = "expert360_cookies"
    PROJECTS_COLLECTION = "projects"  # Shared MongoDB collection

    # Legacy Express naming kept; EXPERT360_* aliases also accepted
    EXPRESS_EMAIL = (
        os.getenv("EXPERT360_EMAIL")
        or os.getenv("EXPRESS_EMAIL")
    )
    EXPRESS_PASSWORD = (
        os.getenv("EXPERT360_PASSWORD")
        or os.getenv("EXPRESS_PASSWORD")
    )

    SMTP_SERVER  = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT    = int(os.getenv("SMTP_PORT", 587))
    SENDER_EMAIL    = os.getenv("SENDER_EMAIL")
    SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
    RECIPIENT_EMAILS = [
        e.strip() for e in os.getenv("RECIPIENT_EMAILS", "").split(",") if e.strip()
    ]

    # Expert360 bot challenges block standard headless Chrome; default headed unless overridden
    _e360_hl = os.getenv("EXPERT360_HEADLESS")
    if _e360_hl is None:
        HEADLESS = False
    else:
        HEADLESS = _e360_hl.lower() == "true"
    COOKIES_FILE = os.path.join(_script_dir, "expert360_cookies.json")
    MONGO_URI    = os.getenv("MONGO_URI", "mongodb://localhost:27017/")

    BASE_URL    = "https://app.expert360.com"
    TARGET_URL  = os.getenv("EXPERT360_TARGET_URL", "https://app.expert360.com/browse").strip()
    LOGIN_URL   = "https://app.expert360.com/login?next=%2Fbrowse"
    CHALLENGE_WAIT_SECONDS = int(os.getenv("EXPERT360_CHALLENGE_WAIT", "90"))


# CLI Options
DEBUG_MODE = "--debug" in sys.argv or True
TEST_MODE  = "--test"  in sys.argv
ONCE_MODE  = "--once"  in sys.argv

def debug_print(msg):
    if DEBUG_MODE:
        print(msg)

def clean_val(t):
    if not t: return ""
    return re.sub(r'\s+', ' ', t).strip()

def dump_page_structure(driver):
    """Dump information about page structure for diagnostic purposes when elements aren't found."""
    print("\n" + "="*60)
    print("🔍 DIAGNOSTICS: EXPERT360 PAGE STRUCTURE DUMP")
    print("="*60)
    print(f"  URL: {driver.current_url}")
    
    card_candidates = [
        "a[href*='/project/']", "a[href^='/project/']", "div[class*='project']",
        "div[class*='card']", "article", ".card"
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

def load_cookies(driver, wipe_existing=False):
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
                with open(Config.COOKIES_FILE, "r", encoding="utf-8") as f:
                    session_data = json.load(f)
                print("  Loaded cookies from local file")
            except Exception:
                pass

    if not session_data or not session_data.get("cookies"):
        return False

    try:
        driver.get(Config.BASE_URL)
        time.sleep(2)
        wait_out_security_challenge(driver, timeout=min(60, Config.CHALLENGE_WAIT_SECONDS))
        if wipe_existing:
            driver.delete_all_cookies()

        for cookie in session_data["cookies"]:
            if "domain" in cookie and ("expert360.com" in cookie["domain"]):
                try:
                    # Selenium rejects some cookie keys from Chrome export
                    c = {k: v for k, v in cookie.items() if k in (
                        "name", "value", "domain", "path", "expiry", "secure", "httpOnly", "sameSite"
                    )}
                    if "expiry" in c and isinstance(c["expiry"], float):
                        c["expiry"] = int(c["expiry"])
                    driver.add_cookie(c)
                except Exception:
                    pass

        if session_data.get("local_storage"):
            for key, val in session_data["local_storage"].items():
                try:
                    driver.execute_script(
                        "window.localStorage.setItem(arguments[0], arguments[1]);", key, val
                    )
                except Exception:
                    pass
        return True
    except Exception as e:
        print(f"  ⚠️ Error applying cookies: {e}")
        return False

def page_body_text(driver, limit=2000):
    try:
        return (driver.find_element(By.TAG_NAME, "body").text or "")[:limit]
    except Exception:
        return ""


def is_security_challenge(driver):
    """True when Expert360/Cloudflare interstitial is blocking the app."""
    try:
        body = page_body_text(driver).lower()
        title = (driver.title or "").lower()
        url = (driver.current_url or "").lower()
        markers = [
            "performing security verification",
            "checking your browser",
            "just a moment",
            "attention required",
            "cf-browser-verification",
            "challenge-platform",
            "enable javascript and cookies",
            "verify you are human",
        ]
        if any(m in body or m in title for m in markers):
            return True
        # Cloudflare challenge iframe / turnstile
        for sel in [
            "iframe[src*='challenge']",
            "iframe[src*='turnstile']",
            "#challenge-form",
            ".cf-challenge",
        ]:
            if driver.find_elements(By.CSS_SELECTOR, sel):
                return True
        if "cdn-cgi/challenge" in url:
            return True
        return False
    except Exception:
        return False


def wait_out_security_challenge(driver, timeout=None):
    """Poll until security challenge clears or timeout. Returns True if cleared."""
    timeout = timeout if timeout is not None else Config.CHALLENGE_WAIT_SECONDS
    if not is_security_challenge(driver):
        return True
    print(f"  🛡️ Security challenge detected — waiting up to {timeout}s...")
    deadline = time.time() + timeout
    last_log = 0
    while time.time() < deadline:
        time.sleep(2)
        if not is_security_challenge(driver):
            print("  ✅ Security challenge cleared")
            time.sleep(2)
            return True
        elapsed = int(timeout - (deadline - time.time()))
        if elapsed - last_log >= 15:
            print(f"  … still verifying ({elapsed}s) URL={driver.current_url}")
            last_log = elapsed
    print("  ❌ Security challenge did not clear in time")
    return False


def has_marketplace_cards(driver):
    try:
        return bool(driver.find_elements(By.CSS_SELECTOR, "a[href*='/project/']"))
    except Exception:
        return False


def is_logged_in(driver):
    """True only when browse/app is reachable and not stuck on challenge/login."""
    try:
        if is_security_challenge(driver):
            return False
        current_url = (driver.current_url or "").lower()
        if any(x in current_url for x in ("/login", "signin", "/auth", "sign-in")):
            return False
        # Marketplace cards are the strongest signal
        if has_marketplace_cards(driver):
            return True
        body = page_body_text(driver).lower()
        if "sign in" in body and "password" in body and "email" in body:
            return False
        # Authenticated shell without cards yet
        if ("browse" in current_url or "projects" in current_url or "dashboard" in current_url):
            if any(t in body for t in ("browse", "projects", "opportunities", "saved", "profile", "log out", "sign out")):
                return True
        return False
    except Exception:
        return False


def perform_login(driver):
    """Log in to Expert360 using credentials."""
    try:
        if not Config.EXPRESS_EMAIL or not Config.EXPRESS_PASSWORD:
            print("❌ EXPRESS_EMAIL / EXPRESS_PASSWORD (or EXPERT360_*) not set in .env")
            return False

        print(f"  Navigating to Expert360 login URL: {Config.LOGIN_URL}")
        driver.get(Config.LOGIN_URL)
        time.sleep(3)
        wait_out_security_challenge(driver)

        if is_logged_in(driver):
            print("  Already authenticated.")
            return True

        email_field = None
        for sel in ["input[type='email']", "input[name='email']", "input[id*='email']", "input[name='username']", "input[id*='username']"]:
            try:
                email_field = WebDriverWait(driver, 8).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                )
                break
            except Exception:
                continue

        if not email_field:
            print("❌ Could not find email field.")
            dump_page_structure(driver)
            return False

        email_field.click()
        email_field.clear()
        email_field.send_keys(Config.EXPRESS_EMAIL)
        time.sleep(0.5)

        password_field = None
        for sel in ["input[type='password']", "input[name='password']", "input[id*='password']"]:
            try:
                password_field = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, sel))
                )
                break
            except Exception:
                continue

        if not password_field:
            print("❌ Could not find password field.")
            return False

        password_field.click()
        password_field.clear()
        password_field.send_keys(Config.EXPRESS_PASSWORD)
        time.sleep(0.5)

        # Submit login
        password_field.send_keys(Keys.ENTER)
        print("  Submitted login form via Enter")
        time.sleep(5)
        wait_out_security_challenge(driver)

        # Fallback submit button click
        if not is_logged_in(driver):
            for sel in [
                "button[type='submit']",
                "input[type='submit']",
                "button[id*='submit']",
                "button[class*='btn-primary']",
                "button[class*='SignIn']",
                "//button[contains(text(), 'Sign In') or contains(text(), 'Login')]",
            ]:
                try:
                    if sel.startswith("//"):
                        btn = driver.find_element(By.XPATH, sel)
                    else:
                        btn = driver.find_element(By.CSS_SELECTOR, sel)
                    driver.execute_script("arguments[0].click();", btn)
                    print("  Clicked login button")
                    time.sleep(5)
                    wait_out_security_challenge(driver)
                    break
                except Exception:
                    continue

        # Wait up to 30 seconds for dashboard/browse redirection
        for _ in range(30):
            time.sleep(1)
            wait_out_security_challenge(driver, timeout=5)
            if is_logged_in(driver):
                break
            # Navigate to browse if login landed elsewhere
            if "login" not in (driver.current_url or "").lower() and not has_marketplace_cards(driver):
                driver.get(Config.TARGET_URL)
                time.sleep(3)
                wait_out_security_challenge(driver)
        else:
            if not is_logged_in(driver):
                print(f"❌ Login redirect failed. URL: {driver.current_url}")
                dump_page_structure(driver)
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
def extract_card_info(card):
    """Extract card-level info from Expert360 browse list."""
    try:
        href = card.get_attribute("href") or ""
        m = re.search(r'/project/([a-zA-Z0-9]+)', href)
        project_id = m.group(1) if m else hashlib.md5(href.encode()).hexdigest()[:12]
        
        # Title
        title = ""
        try:
            title = card.find_element(By.CSS_SELECTOR, "h3").text.strip()
        except:
            pass
        if not title:
            lines = [l.strip() for l in card.text.splitlines() if l.strip()]
            if lines:
                title = lines[0]
                
        # Full Card Text
        card_text = card.text
        lines = [l.strip() for l in card_text.splitlines() if l.strip()]
        
        rate = "Not specified"
        duration = "Not specified"
        location = "Not specified"
        job_type = "Not specified"
        time_posted = "Recently"
        
        for line in lines:
            if "$" in line:
                rate = line
            elif any(w in line.lower() for w in ["month", "week", "year"]) and any(c.isdigit() for c in line):
                duration = line
            elif any(w in line.lower() for w in ["on-site", "remote", "hybrid"]):
                location = line
            elif any(w in line.lower() for w in ["full time", "part time", "contract"]):
                job_type = line
            elif "ago" in line.lower():
                time_posted = line

        snippet = ""
        try:
            desc_candidates = card.find_elements(By.CSS_SELECTOR, "div, p, span")
            for desc_el in desc_candidates:
                txt = desc_el.text.strip()
                if len(txt) > 50 and len(txt) < 300 and txt != card_text:
                    snippet = txt
                    break
        except:
            pass
        if not snippet and len(lines) > 2:
            longest_line = max(lines[1:], key=len)
            if len(longest_line) > 30:
                snippet = longest_line

        return {
            "id": f"expert360-{project_id}",
            "title": title,
            "snippet": snippet,
            "budget": rate,
            "duration": duration,
            "location": location,
            "job_type": job_type,
            "time_posted": time_posted,
            "url": f"{Config.BASE_URL}/project/{project_id}" if "/project/" in href else href,
            "detected_at": datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S")
        }
    except Exception as e:
        print(f"  ⚠️ Error extracting card info: {e}")
        return None

def scan_for_projects(driver):
    """Scrape the browse page for project cards."""
    try:
        driver.get(Config.TARGET_URL)
        time.sleep(3)
        if not wait_out_security_challenge(driver):
            dump_page_structure(driver)
            return []

        if not is_logged_in(driver):
            print("  Session expired during scan — need re-login")
            return []

        # Wait for cards (challenge may have delayed SPA hydration)
        try:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/project/']"))
            )
        except TimeoutException:
            # Soft scroll / refresh once if shell loaded but cards missing
            print("  No cards yet — scrolling and refreshing once...")
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            driver.refresh()
            time.sleep(3)
            wait_out_security_challenge(driver)
            WebDriverWait(driver, 25).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/project/']"))
            )

        time.sleep(2)
        cards = driver.find_elements(By.CSS_SELECTOR, "a[href*='/project/']")
        projects = []
        seen_hrefs = set()

        for card in cards:
            href = card.get_attribute("href")
            if not href or href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            p = extract_card_info(card)
            if p and p.get("title"):
                projects.append(p)

        print(f"✅ Found {len(projects)} projects on Expert360 marketplace.")
        return projects
    except TimeoutException:
        print("⏳ Timeout waiting for Expert360 marketplace cards to load")
        dump_page_structure(driver)
        return []
    except Exception as e:
        print(f"❌ Error scanning Expert360: {e}")
        return []

# ============================
# DETAIL SCANNERS
# ============================
def fetch_project_details(driver, url):
    """Navigate to the project detail page, extract details, and return description."""
    details = {
        "description": "",
        "skills": []
    }
    try:
        driver.get(url)
        time.sleep(4)
        
        body_text = driver.find_element(By.TAG_NAME, "body").text
        lines = [line.strip() for line in body_text.splitlines() if line.strip()]
        
        description_lines = []
        in_desc = False
        skills = []
        
        for idx, line in enumerate(lines):
            if any(w in line.lower() for w in ["about the role", "project description", "role description", "responsibilities", "key requirements"]):
                in_desc = True
                continue
            if any(w in line.lower() for w in ["apply now", "about expert360", "similar roles", "share this"]):
                in_desc = False
                continue
            
            if in_desc:
                description_lines.append(line)
                
        if not description_lines:
            try:
                main_desc = driver.find_element(By.CSS_SELECTOR, "main, article, div[class*='description'], div[class*='project-details']")
                details["description"] = main_desc.text.strip()
            except:
                pass
        else:
            details["description"] = "\n".join(description_lines).strip()
            
        if len(details["description"]) > 4000:
            details["description"] = details["description"][:4000]
            
        try:
            skill_elems = driver.find_elements(By.CSS_SELECTOR, "span[class*='skill'], div[class*='skill'], span[class*='tag']")
            for el in skill_elems:
                txt = el.text.strip()
                if txt and len(txt) < 30 and txt not in skills:
                    skills.append(txt)
        except:
            pass
            
        details["skills"] = skills
        
    except Exception as e:
        print(f"  ⚠️ Detail fetch failed for {url}: {e}")
    return details

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
    """Returns True if database has no Expert360 records."""
    doc = _get_projects_collection().find_one({"platform": Config.PLATFORM_NAME}, {"_id": 1})
    return doc is None

def get_seen_ids():
    """Retrieve set of project IDs already stored for Expert360."""
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
            "time_posted":      project.get("time_posted"),
            "url":              project.get("url"),
            "detected_at":      project.get("detected_at"),
            "platform":         Config.PLATFORM_NAME,
            "emailed":          bool(emailed),
            "skills":           project.get("skills", []),
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
                "url":         p.get("url"),
                "detected_at": p.get("detected_at"),
                "platform":    Config.PLATFORM_NAME,
                "emailed":     bool(emailed),
                "skills":      p.get("skills", []),
            }
            ops.append(UpdateOne({"project_id": doc["project_id"]}, {"$setOnInsert": doc}, upsert=True))
        if ops:
            result = _get_projects_collection().bulk_write(ops, ordered=False)
            print(f"  DB: Seeded {result.upserted_count} records to shared collection (platform: {Config.PLATFORM_NAME})")
    except Exception as e:
        print(f"⚠️ DB bulk seed failed: {e}")

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
    location    = project.get("location", "") or "Not specified"
    budget      = project.get("budget", "") or "Not specified"
    duration    = project.get("duration", "") or "Not specified"
    job_type    = project.get("job_type", "") or "Not specified"
    time_posted = project.get("time_posted", "") or "Not specified"
    description = project.get("description", "") or project.get("snippet", "")
    skills      = project.get("skills", [])

    hdr_grad   = "linear-gradient(135deg,#0052cc,#0747a6)"
    sec_desc   = "#0052cc"
    sec_detail = "#0747a6"
    sec_meta   = "#6b7280"
    btn_color  = "#0052cc"

    desc_section = ""
    if description:
        paragraphs = _esc(description).replace("\n\n", "|||").replace("\n", "<br>")
        paras = [f"<p style='margin:0 0 10px;'>{p}</p>" for p in paragraphs.split("|||") if p.strip()]
        desc_section = (
            _section_header('📋', 'Project Description', sec_desc) +
            f"<tr><td colspan='2' style='padding:14px 16px;background:#f9fafb;"
            f"font-size:14px;line-height:1.75;color:#333;border-bottom:2px solid #e5e7eb;'>"
            f"{''.join(paras)}</td></tr>"
        )

    skills_display = ", ".join(skills) if skills else ""

    detail_rows = (
        _row("Location",    location,                   alt=False) +
        _row("Duration",    duration,                   alt=True) +
        _row("Rate / Budget", budget,                   alt=False, bold_value=True) +
        _row("Job Type",    job_type,                   alt=True) +
        _row("Skills/Tools", skills_display,            alt=False)
    )
    detail_section = _section_header('📦', 'Project Details', sec_detail) + detail_rows

    meta_rows = (
        _row("Posted",      time_posted, alt=False) +
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
          letter-spacing:1.5px;text-transform:uppercase;">Expert360 Monitor Alert</p>
      <h2 style="margin:6px 0 0;color:#fff;font-size:24px;font-weight:700;">🚀 New Expert360 Project</h2>
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
          View Project on Expert360 →
        </a>
      </div>
    </div>

    <div style="background:#f8f9fa;padding:14px 28px;border-top:1px solid #eee;
         font-size:12px;color:#999;text-align:center;">
      Expert360 Monitor &nbsp;|&nbsp; Automated alert &nbsp;|&nbsp; {detected_at}
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
        msg["Subject"] = f"🔔 Expert360: {project.get('title', 'New Project')}"
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
def _detect_chrome_major():
    """Best-effort installed Chrome major version (Windows registry / binary path)."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon")
        ver, _ = winreg.QueryValueEx(key, "version")
        return int(str(ver).split(".")[0])
    except Exception:
        pass
    for path in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ):
        try:
            if os.path.exists(path):
                # Parent dirs are version folders sometimes; FileVersion via powershell is heavier —
                # read adjacent version folder name.
                parent = os.path.dirname(path)
                for name in os.listdir(parent):
                    if name[0].isdigit() and "." in name:
                        return int(name.split(".")[0])
        except Exception:
            continue
    return None


def _clear_profile_locks(profile_dir):
    if not profile_dir or not os.path.isdir(profile_dir):
        return
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        path = os.path.join(profile_dir, name)
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


def initialize_driver():
    """
    Launch Chrome with anti-bot hardening.
    Prefers undetected-chromedriver for Expert360's security interstitial;
    falls back to shared chrome_helper if UC is unavailable.
    """
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

    profile_dir = os.path.join(_script_dir, ".chrome_profile")
    os.makedirs(profile_dir, exist_ok=True)
    _clear_profile_locks(profile_dir)
    chrome_major = _detect_chrome_major()
    print(
        f"  Starting Chrome (HEADLESS={Config.HEADLESS}, "
        f"chrome_major={chrome_major}, profile={profile_dir})"
    )

    # 1) Prefer undetected-chromedriver (bypasses Expert360 security verification)
    try:
        import undetected_chromedriver as uc

        last_err = None
        for attempt, use_profile in enumerate((True, False), start=1):
            try:
                options = uc.ChromeOptions()
                options.add_argument("--lang=en-US,en")
                options.add_argument("--window-size=1920,1080")
                options.add_argument("--no-first-run")
                options.add_argument("--no-default-browser-check")
                options.add_argument("--disable-popup-blocking")
                if use_profile:
                    _clear_profile_locks(profile_dir)
                    options.add_argument(f"--user-data-dir={profile_dir}")
                if Config.HEADLESS:
                    options.add_argument("--headless=new")
                    options.add_argument("--disable-gpu")

                kwargs = {
                    "options": options,
                    "headless": Config.HEADLESS,
                    "use_subprocess": True,
                }
                if chrome_major:
                    kwargs["version_main"] = chrome_major

                print(f"  UC attempt {attempt} (persistent_profile={use_profile})...")
                driver = uc.Chrome(**kwargs)
                print("  ✅ Using undetected-chromedriver")
                return driver
            except Exception as e:
                last_err = e
                print(f"  ⚠️ UC attempt {attempt} failed: {e}")
        raise last_err
    except Exception as e:
        print(f"  ⚠️ undetected-chromedriver unavailable ({e}); falling back to chrome_helper")

    # 2) Fallback: shared helper + CDP stealth (no locked custom profile)
    prev_headless = os.environ.get("HEADLESS")
    os.environ["HEADLESS"] = "True" if Config.HEADLESS else "False"
    try:
        from chrome_helper import build_driver
        extra = [
            "--lang=en-US,en",
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        ]
        driver = build_driver(extra_options=extra)
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.chrome = window.chrome || { runtime: {} };
                    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                """
            })
            driver.execute_cdp_cmd("Network.setUserAgentOverride", {
                "userAgent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                )
            })
        except Exception as e:
            print(f"  ⚠️ Stealth CDP hooks failed (continuing): {e}")
        return driver
    finally:
        if prev_headless is None:
            os.environ.pop("HEADLESS", None)
        else:
            os.environ["HEADLESS"] = prev_headless


def ensure_authenticated(driver):
    """Open browse, clear challenge, re-login if needed. Returns True if ready."""
    # First try persistent profile session without wiping cookies
    driver.get(Config.TARGET_URL)
    time.sleep(3)
    wait_out_security_challenge(driver)
    if is_logged_in(driver) or has_marketplace_cards(driver):
        print("  ✅ Authenticated via browser profile/session")
        return True

    print("  Profile session incomplete — applying saved cookies...")
    load_cookies(driver, wipe_existing=False)
    driver.get(Config.TARGET_URL)
    time.sleep(3)
    wait_out_security_challenge(driver)
    if is_logged_in(driver) or has_marketplace_cards(driver):
        print("  ✅ Authenticated via saved cookies")
        return True

    print("🔑 Session not found, expired, or blocked. Logging in...")
    if not perform_login(driver):
        return False

    driver.get(Config.TARGET_URL)
    time.sleep(3)
    wait_out_security_challenge(driver)
    return is_logged_in(driver) or has_marketplace_cards(driver)


# ============================
# MAIN RUNNER
# ============================
def main():
    print("=" * 50)
    print("🚀 Expert360 Monitor (One-Time Run)")
    print("=" * 50)
    print(f"  Target    : {Config.TARGET_URL}")
    print(f"  Email set : {bool(Config.EXPRESS_EMAIL)}")
    print(f"  Headless  : {Config.HEADLESS}")
    print(f"  Recipients: {', '.join(Config.RECIPIENT_EMAILS)}")
    print()

    driver = initialize_driver()
    try:
        if not ensure_authenticated(driver):
            # Headless challenges often never clear — one retry with headed Chrome
            if Config.HEADLESS:
                print("⚠️ Auth failed under headless. Retrying with headed Chrome...")
                try:
                    driver.quit()
                except Exception:
                    pass
                Config.HEADLESS = False
                driver = initialize_driver()
                if not ensure_authenticated(driver):
                    print("❌ Authentication / security challenge failed. Exiting.")
                    dump_page_structure(driver)
                    return
            else:
                print("❌ Authentication / security challenge failed. Exiting.")
                dump_page_structure(driver)
                return

        init_db()
        cold_start = db_is_cold_start()
        seen_ids = get_seen_ids()
        print(f"📁 Database loaded — {len(seen_ids)} Expert360 records detected")

        all_projects = scan_for_projects(driver)
        if not all_projects and not is_logged_in(driver):
            print("🔑 Lost session during scan — re-authenticating once...")
            if ensure_authenticated(driver):
                all_projects = scan_for_projects(driver)

        if not all_projects:
            print("⚠️ No projects found on the browse page.")
            dump_page_structure(driver)
            return

        new_projects = [p for p in all_projects if p["id"] not in seen_ids]
        print(f"📊 Stats: {len(all_projects)} visible, {len(seen_ids)} total seen, {len(new_projects)} new")

        if cold_start:
            print("⚙️ Cold start: seeding database silently with current page listings...")
            for idx, p in enumerate(all_projects):
                print(f"  [{idx+1}/{len(all_projects)}] Seeding details for '{p['title'][:40]}'...")
                details = fetch_project_details(driver, p["url"])
                p.update(details)
            bulk_insert_projects(all_projects, emailed=False)
            print(f"✅ Seeding complete. {len(all_projects)} jobs cached. Monitoring for future new posts.")
        elif new_projects:
            print(f"🎯 Found {len(new_projects)} new project(s)!")
            for idx, p in enumerate(new_projects):
                print(f"  → [{idx+1}/{len(new_projects)}] Fetching details for '{p['title'][:40]}'...")
                details = fetch_project_details(driver, p["url"])
                p.update(details)

                emailed = send_notification(p)
                insert_project(p, emailed=emailed)
                seen_ids.add(p["id"])
        else:
            print("⏳ No new projects detected.")

    except Exception as e:
        print(f"💥 Critical Failure during monitor run: {e}")
        raise
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        print("🏁 Expert360 Monitor run complete.")


if __name__ == "__main__":
    main()

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
    PLATFORM_NAME = "talmix"
    SESSION_KEY = "talmix_cookies"
    PROJECTS_COLLECTION = "projects"  # Shared MongoDB collection
    
    TALMIX_EMAIL    = os.getenv("TALMIX_EMAIL")
    TALMIX_PASSWORD = os.getenv("TALMIX_PASSWORD")
    
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
    COOKIES_FILE = "talmix_cookies.json"
    MONGO_URI    = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
    
    BASE_URL    = "https://go.high5hire.app"
    TARGET_URL  = "https://go.high5hire.app/projects/search"

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
    print("🔍 DIAGNOSTICS: TALMIX PAGE STRUCTURE DUMP")
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
            if 'domain' in cookie and ('high5hire.app' in cookie['domain'] or 'talmix.com' in cookie['domain']):
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
        return any(x in current_url for x in ["dashboard", "overview", "projects", "home", "search", "talent", "job"])
    except:
        return False

def perform_login(driver):
    """Log in to Talmix/High5 using credentials."""
    try:
        login_url = "https://go.high5hire.app/login"
        print(f"  Navigating to Talmix login URL: {login_url}")
        driver.get(login_url)
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
        email_field.send_keys(Config.TALMIX_EMAIL)
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
        password_field.send_keys(Config.TALMIX_PASSWORD)
        time.sleep(0.5)

        # Submit login
        password_field.send_keys(Keys.ENTER)
        print("  Submitted login form via Enter")
        time.sleep(5)

        # Fallback submit button click
        if not is_logged_in(driver):
            for sel in ["button[type='submit']", "input[type='submit']", "button[id*='submit']", "button[class*='btn-primary']", "//button[contains(text(), 'Login') or contains(text(), 'Sign')]"]:
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
CARD_SELECTORS = [
    "div.project-list-item",
    "div.project-card",
    "div[class*='project-card']",
    "div[class*='job-card']",
    "div.job-card",
    "div[class*='brief-card']",
    "div.brief-card",
    "article",
    "[class*='opportunity-card']",
    "div.card",
    "[class*='card']",
    "li[class*='project']",
    "li[class*='job']"
]

TITLE_SELECTORS = [
    ".project-title",
    "h3.project-title",
    "h3", "h4", "h2", "h5",
    ".title", "[class*='title']"
]

LINK_SELECTORS = [
    "a.project-title-container",
    "a.toolbar-button",
    "a[href*='/projects/']",
    "a[href*='/project/']"
]

def _first_text(parent, selectors, max_len=200):
    """Retrieve text of first matching element inside a parent."""
    for sel in selectors:
        try:
            if sel.startswith("//"):
                elems = parent.find_elements(By.XPATH, sel)
            else:
                elems = parent.find_elements(By.CSS_SELECTOR, sel)
            for e in elems:
                t = e.text.strip()
                if t:
                    lines = [l.strip() for l in t.splitlines() if l.strip()]
                    t = " ".join(lines)
                    return t[:max_len]
        except:
            pass
    return ""

def extract_project_data(card):
    """Extract project info from a Talmix/High5 card."""
    try:
        # ── Title ────────────────────────────────────────────────────────────
        title = _first_text(card, TITLE_SELECTORS, 150)
        if not title:
            return None

        # ── URL & project ID ─────────────────────────────────────────────────
        url = None
        project_id = None
        for sel in LINK_SELECTORS:
            try:
                link_elem = card.find_element(By.CSS_SELECTOR, sel)
                href = link_elem.get_attribute("href")
                if href:
                    url = href
                    # Extract from /projects/fc7c3aab-9a4c-4e55-aa2b-46c608b1bde3/title
                    m = re.search(r'/projects?/([a-zA-Z0-9-]+)', href)
                    if m:
                        project_id = m.group(1)
                        break
            except:
                continue

        # Secondary link fallback
        if not url:
            try:
                links = card.find_elements(By.TAG_NAME, "a")
                for a in links:
                    href = a.get_attribute("href") or ""
                    if any(x in href for x in ["brief", "project", "job", "opportunity"]):
                        url = href
                        m = re.search(r'/projects?/([a-zA-Z0-9-]+)', href)
                        if m:
                            project_id = m.group(1)
                        break
            except:
                pass

        # Hash fallback when no URL is found
        if not url:
            project_id = hashlib.md5(title.encode()).hexdigest()[:12]
            url = f"https://go.high5hire.app/projects/{project_id}"

        location = ""
        budget = "Not specified"
        duration = ""
        time_posted = "Recently"
        skills = []

        # ── Specific Selectors ───────────────────────────────────────────────
        try:
            loc_elem = card.find_element(By.CSS_SELECTOR, ".project-offsite, .project-location")
            location = loc_elem.text.strip()
        except:
            pass

        try:
            posted_elem = card.find_element(By.CSS_SELECTOR, ".project-posted .date")
            time_posted = posted_elem.text.strip()
        except:
            try:
                posted_elem = card.find_element(By.CSS_SELECTOR, ".project-posted")
                time_posted = posted_elem.text.replace("Posted:", "").strip()
            except:
                pass

        try:
            tag_elems = card.find_elements(By.CSS_SELECTOR, ".tag, .project-industry, .project-functions, .project-tags")
            skills = [t.text.strip() for t in tag_elems if t.text.strip()]
        except:
            pass

        # ── Fallback Helper: detect posting-age strings ──────────────────────
        def is_time_string(t):
            t_low = t.lower()
            return (
                "ago" in t_low
                or "•" in t
                or bool(re.search(
                    r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b.*\d{4}',
                    t_low
                ))
                or bool(re.search(r'\d+\s*(hour|min|day|week|month)s?\s*ago', t_low))
            )

        def clean_time(t):
            return re.sub(r'\s*•\s*', '', t).strip()

        # Fallback text element extraction if location or duration missing
        try:
            text_elems = card.find_elements(By.XPATH, ".//*[not(child::*)]")
            for elem in text_elems:
                t = elem.text.strip()
                if not t:
                    continue

                # Posting-age strings
                if not time_posted or time_posted == "Recently":
                    if is_time_string(t):
                        candidate = clean_time(t)
                        if candidate and len(candidate) < 60:
                            time_posted = candidate
                        continue

                # Budget: currency symbols, named currencies, or rate-type words
                if budget == "Not specified" and len(t) < 60:
                    if (
                        any(c in t for c in ("$", "€", "£", "¥"))
                        or any(w in t.upper() for w in ("EUR", "USD", "GBP", "CHF"))
                        or any(w in t.lower() for w in ("hourly", "daily", "fixed", "per hour", "per day", "rate", "budget"))
                    ):
                        budget = t

                # Duration: time-unit words (guard: "X ago" already handled above)
                elif not duration and len(t) < 60:
                    if any(w in t.lower() for w in ("week", "month", "year", "day")) and "ago" not in t.lower():
                        duration = t

                # Location: place/timezone keywords
                elif not location and len(t) < 150:
                    if any(w in t.lower() for w in (
                        "remote", "hybrid", "onsite", "on-site",
                        "utc", "gmt", "cet", "est", "pst",
                        "europe", "london", "dublin", "paris",
                        "germany", "france", "italy", "spain",
                        "united", "casablanca", "lisbon",
                    )):
                        location = t
        except:
            pass

        # Status
        status = "Open"
        try:
            for sel in [".project-status", "[class*='status-label']", "[class*='status']"]:
                try:
                    elems = card.find_elements(By.CSS_SELECTOR, sel)
                    texts = [e.text.strip() for e in elems if e.text.strip()]
                    if texts:
                        status = " → ".join(texts)
                        break
                except:
                    continue
        except:
            pass

        # Clean title
        title = re.sub(r'\s*\n\s*', ' ', title).strip()

        return {
            "id": project_id,
            "title": title,
            "description": "",  # Filled from detail page
            "location": location,
            "budget": budget,
            "duration": duration,
            "time_posted": time_posted,
            "status": status,
            "url": url,
            "skills": skills,
            "detected_at": datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:
        debug_print(f"  ⚠️ Error parsing card: {e}")
        return None

def find_project_cards(driver):
    """Locate all project card elements on the list page."""
    for sel in CARD_SELECTORS:
        try:
            if sel.startswith("//"):
                cards = driver.find_elements(By.XPATH, sel)
            else:
                cards = driver.find_elements(By.CSS_SELECTOR, sel)
            if cards:
                debug_print(f"  Located {len(cards)} cards using selector: '{sel}'")
                return cards
        except:
            pass
            
    # Ultimate fallback: check for any element containing links to project/job/brief
    try:
        links = driver.find_elements(By.XPATH, "//a[contains(@href, '/project/') or contains(@href, '/job/') or contains(@href, '/brief/') or contains(@href, '/opportunity/')]")
        cards = []
        seen_parents = set()
        for link in links:
            try:
                parent = link.find_element(By.XPATH, "./ancestor::div[contains(@class, 'card') or contains(@class, 'item') or @style or contains(@class, 'border')][1]")
                if parent.id not in seen_parents:
                    seen_parents.add(parent.id)
                    cards.append(parent)
            except:
                pass
        if cards:
            debug_print(f"  Fallback: located {len(cards)} parent containers")
            return cards
    except:
        pass
        
    return []

def scan_for_projects(driver):
    """Scrape the dashboard page for project cards."""
    try:
        current_url = driver.current_url
        if not is_logged_in(driver):
            driver.get(Config.TARGET_URL)
            time.sleep(5)

        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(3)

        cards = find_project_cards(driver)
        if not cards:
            print("⚠️  No project cards found with default selectors.")
            dump_page_structure(driver)
            return []

        projects = []
        for card in cards:
            p = extract_project_data(card)
            if p and p.get("title") and p.get("id"):
                projects.append(p)
                
        print(f"✅ Extracted {len(projects)} valid projects from {len(cards)} cards")
        return projects
    except TimeoutException:
        print("⏳ Timeout waiting for Talmix dashboard page to load")
        return []
    except Exception as e:
        print(f"❌ Error scanning Talmix: {e}")
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
    """Returns True if database has no Talmix records."""
    doc = _get_projects_collection().find_one({"platform": Config.PLATFORM_NAME}, {"_id": 1})
    return doc is None

def get_seen_ids():
    """Retrieve set of project IDs already stored for Talmix."""
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
            "status":           project.get("status"),
            "url":              project.get("url"),
            "detected_at":      project.get("detected_at"),
            "platform":         Config.PLATFORM_NAME,
            "emailed":          bool(emailed),
            
            # Platform specific details
            "skills":           project.get("skills", []),
            "start_date":       project.get("start_date", ""),
            "industry":         project.get("industry", ""),
            "client_type":      project.get("client_type", "")
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
# DETAIL SCANNERS
# ============================
def fetch_project_details(driver, url):
    """Navigate to a project's detail page to pull description and fields."""
    details = {
        "description": "", 
        "skills": [], 
        "start_date": "", 
        "client_type": "",
        "location": "",
        "budget": "",
        "duration": "",
        "status": ""
    }
    try:
        driver.get(url)
        # Wait up to 5 seconds for highlights to load, or fallback to 1.5s sleep
        try:
            WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".highlights-item")))
        except:
            time.sleep(1.5)
        
        def clean_val(t):
            if not t: return ""
            return re.sub(r'\s+', ' ', t).strip()
        
        # Pull description content specifically from the 'OBJECTIVES AND KEY DELIVERABLES' widget
        description = ""
        try:
            widgets = driver.find_elements(By.CSS_SELECTOR, ".pd-widget")
            for w in widgets:
                try:
                    hdr = w.find_element(By.CSS_SELECTOR, ".pd-widget__header").text.strip()
                    if "OBJECTIVES" in hdr.upper() or "DELIVERABLES" in hdr.upper() or "DESCRIPTION" in hdr.upper():
                        description = w.find_element(By.CSS_SELECTOR, ".pd-widget__body").text.strip()
                        break
                except:
                    pass
        except:
            pass
            
        # Fallback if specific widget not found
        if not description:
            for sel in [".description", "[class*='description']", ".content", "article", ".project-details", "main"]:
                try:
                    el = driver.find_element(By.CSS_SELECTOR, sel)
                    txt = el.text.strip()
                    if len(txt) > 80:
                        description = txt
                        break
                except:
                    pass
                
        if description:
            details["description"] = clean_val(description)

        # Extract structured details from body text if possible
        body_text = driver.find_element(By.TAG_NAME, "body").text
        
        # Start date
        m_start = re.search(r'(?:Start Date|Starts|Start):\s*([^\n]{2,50})', body_text, re.IGNORECASE)
        if m_start:
            details["start_date"] = clean_val(m_start.group(1))
            
        # Skills/Tools
        m_skills = re.search(r'(?:Skills|Required Skills|Tools|Stack):\s*([^\n]{2,100})', body_text, re.IGNORECASE)
        if m_skills:
            details["skills"] = [clean_val(s) for s in m_skills.group(1).split(",") if s.strip()]

        # Extract metadata from detail page
        location = ""
        budget = ""
        duration = ""

        # Extract highlights specifically using tooltips
        try:
            highlights = driver.find_elements(By.CSS_SELECTOR, ".highlights-item")
            for item in highlights:
                tooltip = item.get_attribute("mattooltip") or ""
                tooltip_low = tooltip.lower()
                
                label = ""
                try:
                    label = item.find_element(By.CSS_SELECTOR, ".highlights-item__label").text.strip()
                except:
                    pass
                    
                icon_val = ""
                try:
                    icon_val = item.find_element(By.CSS_SELECTOR, ".highlights-item__icon").text.strip()
                except:
                    pass
                    
                if "duration" in tooltip_low:
                    duration = clean_val(f"{icon_val} {label}")
                elif "location" in tooltip_low:
                    location = clean_val(label)
                elif "starting" in tooltip_low or "start" in tooltip_low:
                    details["start_date"] = clean_val(label)
        except:
            pass

        # Helper: detect posting-age strings
        def is_time_string(t):
            t_low = t.lower()
            return (
                "ago" in t_low
                or "•" in t
                or bool(re.search(
                    r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b.*\d{4}',
                    t_low
                ))
                or bool(re.search(r'\d+\s*(hour|min|day|week|month)s?\s*ago', t_low))
            )

        # Text element scanning for metadata (fallbacks)
        try:
            text_elems = driver.find_elements(By.XPATH, ".//*[not(child::*)]")
            for elem in text_elems:
                t = elem.text.strip()
                if not t or is_time_string(t):
                    continue

                # Budget: currency symbols, named currencies, or rate-type words
                if not budget and len(t) < 60:
                    if (
                        any(c in t for c in ("$", "€", "£", "¥"))
                        or any(w in t.upper() for w in ("EUR", "USD", "GBP", "CHF"))
                        or any(w in t.lower() for w in ("hourly", "daily", "fixed", "per hour", "per day", "budget", "rate"))
                    ):
                        budget = clean_val(t)

                # Duration: time-unit words
                elif not duration and len(t) < 60:
                    if any(w in t.lower() for w in ("week", "month", "year", "day")) and "ago" not in t.lower():
                        duration = clean_val(t)

                # Location: place/timezone keywords
                elif not location and len(t) < 150:
                    if any(w in t.lower() for w in (
                        "remote", "hybrid", "onsite", "on-site",
                        "utc", "gmt", "cet", "est", "pst",
                        "europe", "london", "dublin", "paris",
                        "germany", "france", "italy", "spain",
                        "united", "casablanca", "lisbon",
                    )):
                        location = clean_val(t)
        except:
            pass

        # Update details dict
        if location: details["location"] = location
        if budget: details["budget"] = budget
        if duration: details["duration"] = duration

    except Exception as e:
        print(f"  ⚠️ Detail fetch failed for {url}: {e}")
    # Removed back navigation sleep from finally block to run 3x faster
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
    description = project.get("description", "")
    location    = project.get("location", "") or "Remote / Not specified"
    budget      = project.get("budget", "") or "Not provided"
    duration    = project.get("duration", "")
    start_date  = project.get("start_date", "")
    skills      = project.get("skills", [])

    hdr_grad   = "linear-gradient(135deg,#1e3a8a,#3b82f6)"
    sec_desc   = "#1e3a8a"
    sec_detail = "#2563eb"
    sec_budget = "#059669"
    btn_color  = "#1e3a8a"



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
          letter-spacing:1.5px;text-transform:uppercase;">Talmix / High5 Monitor Alert</p>
      <h2 style="margin:6px 0 0;color:#fff;font-size:24px;font-weight:700;">🚀 New Talmix Project</h2>
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
          View Project on Talmix / High5 →
        </a>
      </div>
    </div>

    <div style="background:#f8f9fa;padding:14px 28px;border-top:1px solid #eee;
         font-size:12px;color:#999;text-align:center;">
      Talmix Monitor &nbsp;|&nbsp; Automated alert &nbsp;|&nbsp; {detected_at}
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
        msg["Subject"] = f"🔔 Talmix: {project.get('title', 'New Project')}"
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
    print("🚀 Talmix / High5 Project Monitor")
    print("=" * 50)
    print(f"  Account   : {Config.TALMIX_EMAIL}")
    print(f"  Interval  : {Config.CHECK_INTERVAL}s")
    print(f"  Recipients: {', '.join(Config.RECIPIENT_EMAILS)}")
    print()

    if TEST_MODE:
        print("🧪 RUNNING IN TEST MODE — MongoDB operations skipped, sends 1 test email\n")

    driver = initialize_driver()
    try:
        if not setup_session(driver):
            print("❌ Failed to authenticate Talmix session. Exiting...")
            return

        if TEST_MODE:
            seen_ids = set()
        else:
            cold_start = db_is_cold_start()
            init_db()
            seen_ids = get_seen_ids()
            print(f"📁 Database loaded — {len(seen_ids)} Talmix records detected")

            # Cold Start Seeding
            if cold_start:
                print("⚙️  Cold start: seeding database silently with current page listings...")
                seed_projects = scan_for_projects(driver)
                if seed_projects:
                    print(f"  → Seeding {len(seed_projects)} projects. Fetching details for each...")
                    for idx, project in enumerate(seed_projects):
                        print(f"    [{idx+1}/{len(seed_projects)}] Fetching details for '{project['title'][:40]}'...")
                        details = fetch_project_details(driver, project["url"])
                        project.update(details)
                    bulk_insert_projects(seed_projects, emailed=False)
                    print(f"✅ Seeding complete. {len(seed_projects)} projects cached. Monitoring for future new posts.")
                    seen_ids = get_seen_ids()
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

                new_projects = filter_new_projects(all_projects, seen_ids)

                if TEST_MODE and all_projects and not seen_ids:
                    project = all_projects[0]
                    print(f"🧪 Test mode: fetching details for '{project['title'][:40]}'")
                    details = fetch_project_details(driver, project["url"])
                    project.update(details)
                    print(f"🧪 Test mode: sending alert for project '{project['title'][:40]}'")
                    send_notification(project)
                    for p in all_projects:
                        seen_ids.add(p["id"])
                elif new_projects:
                    print(f"🎯 Found {len(new_projects)} new project(s)!")
                    for project in new_projects:
                        print(f"  → Scraped brief '{project['title'][:50]}'. Fetching details...")
                        details = fetch_project_details(driver, project["url"])
                        project.update(details)

                        emailed = send_notification(project)
                        if not TEST_MODE:
                            insert_project(project, emailed=emailed)
                        seen_ids.add(project["id"])
                else:
                    print("⏳ No new projects detected.")

                print(f"📊 Stats: {len(all_projects)} visible, {len(seen_ids)} total seen")

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
        print("✅ Talmix Monitor stopped.")

if __name__ == "__main__":
    main()

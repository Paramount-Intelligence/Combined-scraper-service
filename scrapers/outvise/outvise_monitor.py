"""
Outvise Expert Freelancer Monitor
=================================
Scrapes project opportunities from the Outvise expert portal and stores them
in the shared MongoDB `office_monitor.projects` collection.

Login  : https://www.outvise.com/login  (freelancer / expert account)
Creds  : OUTVISE_EMAIL / OUTVISE_PASSWORD from .env
Optional: OUTVISE_TARGET_URL = post-login opportunities page (auto-discovered if unset)

Usage:
    python outvise_monitor.py --once
    python outvise_monitor.py --once --backfill
    python outvise_monitor.py --once --debug
"""
import sys
import time
import smtplib
import json
import os
import re
import hashlib
from pymongo import MongoClient
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Load .env: local scraper dir first, then repo root
_script_dir = os.path.dirname(os.path.abspath(__file__))
_root_dir = os.path.abspath(os.path.join(_script_dir, "..", ".."))
if os.path.exists(os.path.join(_script_dir, ".env")):
    load_dotenv(dotenv_path=os.path.join(_script_dir, ".env"))
load_dotenv(dotenv_path=os.path.join(_root_dir, ".env"))

PKT = timezone(timedelta(hours=5))


class Config:
    PLATFORM_NAME = "outvise"
    SESSION_KEY = "outvise_cookies"
    PROJECTS_COLLECTION = "projects"

    OUTVISE_EMAIL = os.getenv("OUTVISE_EMAIL")
    OUTVISE_PASSWORD = os.getenv("OUTVISE_PASSWORD")

    SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SENDER_EMAIL = os.getenv("SENDER_EMAIL")
    SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
    RECIPIENT_EMAILS = [
        e.strip() for e in os.getenv("RECIPIENT_EMAILS", "").split(",") if e.strip()
    ]

    HEADLESS = os.getenv("HEADLESS", "True").lower() == "true"
    COOKIES_FILE = os.path.join(_script_dir, "outvise_cookies.json")
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")

    BASE_URL = "https://www.outvise.com"
    LOGIN_URL = "https://www.outvise.com/login"
    # Default to the opportunities wall (not opportunity_log)
    TARGET_URL = os.getenv(
        "OUTVISE_TARGET_URL", "https://www.outvise.com/walls/opportunities"
    ).strip()


DEBUG_MODE = "--debug" in sys.argv
TEST_MODE = "--test" in sys.argv
ONCE_MODE = "--once" in sys.argv


def log(msg):
    """Always-on log line with timestamp."""
    ts = datetime.now(PKT).strftime("%H:%M:%S")
    print(f"[{ts} PKT] {msg}", flush=True)


def debug_print(msg):
    if DEBUG_MODE:
        log(msg)


def clean_val(t):
    if not t:
        return ""
    return re.sub(r"\s+", " ", t).strip()


def dismiss_cookie_banner(driver):
    for el in driver.find_elements(By.CSS_SELECTOR, "button, a.cookie-close, a.btn"):
        t = (el.text or "").strip().upper()
        if t in ("OK", "ACCEPT", "ACCEPT ALL", "AGREE"):
            try:
                el.click()
                log("  Cookie banner dismissed")
                time.sleep(1)
                return
            except Exception:
                pass


def dump_page_structure(driver, label="PAGE"):
    log("=" * 60)
    log(f"DIAGNOSTICS: {label}")
    log(f"  URL  : {driver.current_url}")
    log(f"  Title: {driver.title}")
    try:
        body = driver.find_element(By.TAG_NAME, "body").text
        log(f"  Body sample ({min(len(body), 800)} chars):")
        for line in body[:800].splitlines()[:25]:
            if line.strip():
                log(f"    | {line.strip()[:120]}")
    except Exception as e:
        log(f"  Could not read body: {e}")

    log("  Candidate cards / links:")
    for sel in [
        "a[href*='opportunit']",
        "a[href*='project']",
        "a[href*='request']",
        "div[class*='card']",
        "div[class*='project']",
        "div[class*='opportunit']",
        "article",
        ".card",
        "tr",
    ]:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            if elems:
                sample = elems[0].text[:80].replace("\n", " ") if elems[0].text else "(empty)"
                log(f"    [{len(elems)}] {sel} → '{sample}'")
        except Exception:
            pass
    log("=" * 60)


# ============================
# SESSION MANAGEMENT
# ============================
_mongo_client = None


def _get_session_collection():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(Config.MONGO_URI)
    return _mongo_client["office_monitor"]["sessions"]


def save_cookies(driver):
    try:
        cookies = driver.get_cookies()
        local_storage = driver.execute_script("return window.localStorage;")
        session_data = {
            "cookies": cookies,
            "local_storage": local_storage,
            "saved_at": datetime.now(timezone.utc),
        }
        _get_session_collection().update_one(
            {"_id": Config.SESSION_KEY},
            {"$set": session_data},
            upsert=True,
        )
        try:
            with open(Config.COOKIES_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "cookies": cookies,
                        "local_storage": local_storage,
                        "saved_at": datetime.now(timezone.utc).isoformat(),
                    },
                    f,
                )
        except Exception:
            pass
        log("  Session cookies saved (MongoDB + local backup)")
        return True
    except Exception as e:
        log(f"  WARNING: Could not save cookies: {e}")
        return False


def load_cookies(driver):
    session_data = None
    try:
        doc = _get_session_collection().find_one({"_id": Config.SESSION_KEY})
        if doc and doc.get("cookies"):
            session_data = doc
            log("  Loaded cookies from MongoDB")
    except Exception as e:
        log(f"  WARNING: Could not load cookies from MongoDB: {e}")

    if not session_data and os.path.exists(Config.COOKIES_FILE):
        try:
            with open(Config.COOKIES_FILE, "r", encoding="utf-8") as f:
                session_data = json.load(f)
            log("  Loaded cookies from local file")
        except Exception:
            pass

    if not session_data or not session_data.get("cookies"):
        log("  No saved session found")
        return False

    try:
        driver.get(Config.BASE_URL)
        time.sleep(2)
        dismiss_cookie_banner(driver)
        driver.delete_all_cookies()
        for cookie in session_data["cookies"]:
            domain = cookie.get("domain") or ""
            if "outvise.com" in domain or not domain:
                try:
                    driver.add_cookie(cookie)
                except Exception:
                    pass
        if session_data.get("local_storage"):
            for key, val in session_data["local_storage"].items():
                try:
                    driver.execute_script(
                        "window.localStorage.setItem(arguments[0], arguments[1]);",
                        key,
                        val,
                    )
                except Exception:
                    pass
        return True
    except Exception as e:
        log(f"  WARNING: Error applying cookies: {e}")
        return False


def is_logged_in(driver):
    try:
        url = driver.current_url.lower()
        body = ""
        try:
            body = driver.find_element(By.TAG_NAME, "body").text.lower()
        except Exception:
            pass

        if "your email or password are incorrect" in body:
            return False
        if "/login" in url and "i already have a freelancer account" in body:
            return False
        if "login" in url or "signin" in url:
            # Still on login page
            if "freelancer account" in body or "client account" in body:
                return False

        # Positive signals
        if any(k in url for k in ("opportunit", "project", "dashboard", "request", "/home", "matching")):
            if "sign up" not in body[:200] or "log out" in body or "logout" in body:
                return True
        if any(k in body for k in ("log out", "logout", "my profile", "my opportunities", "open opportunities")):
            return True
        return False
    except Exception:
        return False


def perform_login(driver):
    """Log in to Outvise expert (freelancer) portal."""
    if not Config.OUTVISE_EMAIL or not Config.OUTVISE_PASSWORD:
        log("Login failed: OUTVISE_EMAIL / OUTVISE_PASSWORD not set in environment")
        return False

    try:
        log(f"Navigating to login: {Config.LOGIN_URL}")
        driver.get(Config.LOGIN_URL)
        time.sleep(4)
        dismiss_cookie_banner(driver)

        if is_logged_in(driver):
            log("Already authenticated")
            return True

        try:
            email_field = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "#email"))
            )
        except Exception:
            log("Login failed: could not find email field")
            dump_page_structure(driver, "LOGIN FORM MISSING EMAIL")
            return False

        try:
            password_field = driver.find_element(By.CSS_SELECTOR, "#password")
        except Exception:
            log("Login failed: could not find password field")
            return False

        email_field.clear()
        email_field.send_keys(Config.OUTVISE_EMAIL)
        time.sleep(0.3)
        password_field.clear()
        password_field.send_keys(Config.OUTVISE_PASSWORD)
        time.sleep(0.3)

        submitted = False
        for el in driver.find_elements(By.CSS_SELECTOR, "button[type='submit'], button.btn-primary"):
            t = (el.text or "").strip().lower()
            if "log" in t:
                driver.execute_script("arguments[0].click();", el)
                submitted = True
                log("Submitted login form (button click)")
                break
        if not submitted:
            password_field.send_keys(Keys.ENTER)
            log("Submitted login form (Enter)")

        # Wait for redirect / error
        for i in range(20):
            time.sleep(1)
            body = ""
            try:
                body = driver.find_element(By.TAG_NAME, "body").text.lower()
            except Exception:
                pass
            if "your email or password are incorrect" in body:
                log("Login failed: email or password are incorrect — check OUTVISE_EMAIL / OUTVISE_PASSWORD")
                return False
            if is_logged_in(driver):
                break
            if i % 5 == 4:
                log(f"  Still waiting for login redirect... URL={driver.current_url}")

        if not is_logged_in(driver):
            log(f"Login failed: redirect did not succeed. URL={driver.current_url}")
            dump_page_structure(driver, "AFTER LOGIN ATTEMPT")
            return False

        save_cookies(driver)
        log(f"Login successful -> {driver.current_url}")
        return True
    except Exception as e:
        log(f"Login failed: {e}")
        return False


def resolve_opportunities_url(driver):
    """Prefer explicit opportunities wall; never use opportunity_log."""
    if Config.TARGET_URL:
        log(f"Using OUTVISE_TARGET_URL: {Config.TARGET_URL}")
        return Config.TARGET_URL
    # Hard default — authenticated landing is usually already this URL
    default = f"{Config.BASE_URL}/walls/opportunities"
    log(f"Using default opportunities URL: {default}")
    return default


# ============================
# EXTRACTION
# ============================
_JUNK_TITLES = {
    "my opportunities", "my hub", "check it", "apply", "invite a friend",
    "configure more", "manage your account", "based on your skills",
}


def _project_id_from_url(url, title=""):
    m = re.search(r"/opportunity/(\d+)", url or "", re.I)
    if m:
        return f"outvise-{m.group(1)}"
    for pat in [
        r"/opportunit(?:y|ies)/([a-zA-Z0-9_-]+)",
        r"/project(?:s)?/([a-zA-Z0-9_-]+)",
    ]:
        m = re.search(pat, url or "", re.I)
        if m:
            return f"outvise-{m.group(1)}"
    seed = (url or "") + "|" + (title or "")
    return f"outvise-{hashlib.md5(seed.encode()).hexdigest()[:12]}"


def _parse_labeled_lines(lines):
    """Parse Outvise Mode:/Duration:/Region:/Fees: style labels."""
    out = {
        "remote_type": "",
        "duration": "",
        "location": "",
        "budget": "",
        "time_posted": "",
    }
    for line in lines:
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        m = re.match(r"(?i)^mode\s*:\s*(.*)$", line)
        if m:
            out["remote_type"] = m.group(1).strip() or out["remote_type"]
            continue
        m = re.match(r"(?i)^duration\s*:\s*(.*)$", line)
        if m:
            out["duration"] = m.group(1).strip() or out["duration"]
            continue
        m = re.match(r"(?i)^region\s*:\s*(.*)$", line)
        if m:
            out["location"] = m.group(1).strip() or out["location"]
            continue
        m = re.match(r"(?i)^(?:fees?|budget|rate)\s*:\s*(.*)$", line)
        if m:
            out["budget"] = m.group(1).strip() or out["budget"]
            continue
        m = re.match(r"(?i)^location\s*:\s*(.*)$", line)
        if m:
            out["location"] = out["location"] or m.group(1).strip()
            continue
        if "ago" in low or "good match" in low or "may interest" in low:
            out["time_posted"] = line
    return out


def extract_card_info(card):
    try:
        href = ""
        try:
            if card.tag_name.lower() == "a":
                href = card.get_attribute("href") or ""
            else:
                a = card.find_element(By.CSS_SELECTOR, "a[href*='/opportunity/']")
                href = a.get_attribute("href") or ""
        except Exception:
            pass
        href = (href or "").split("?")[0]
        if "/opportunity/" not in href or not re.search(r"/opportunity/\d+", href):
            return None

        lines = [l.strip() for l in (card.text or "").splitlines() if l.strip()]
        title = ""
        for line in lines:
            low = line.lower()
            if low in _JUNK_TITLES or low.startswith("mode:") or low.startswith("duration:") or low.startswith("region:"):
                continue
            if "ago" in low or "good match" in low or "may interest" in low:
                continue
            if low == "check it":
                continue
            if len(line) >= 5:
                title = line
                break
        if not title or title.lower() in _JUNK_TITLES:
            return None

        labeled = _parse_labeled_lines(lines)
        snippet = ""
        for line in lines[1:]:
            if len(line) > 50 and not re.match(r"(?i)^(mode|duration|region|fees?)\s*:", line):
                if line.lower() not in _JUNK_TITLES:
                    snippet = line
                    break

        return {
            "id": _project_id_from_url(href, title),
            "title": title[:200],
            "snippet": snippet[:500],
            "budget": labeled["budget"] or "Not specified",
            "duration": labeled["duration"] or "Not specified",
            "location": labeled["location"] or "Not specified",
            "remote_type": labeled["remote_type"] or "",
            "time_posted": labeled["time_posted"] or "Recently",
            "url": href,
            "detected_at": datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:
        debug_print(f"  card parse error: {e}")
        return None


def scan_for_projects(driver, opportunities_url):
    log(f"Scanning opportunities at: {opportunities_url}")
    try:
        driver.get(opportunities_url)
        time.sleep(5)
        dismiss_cookie_banner(driver)

        for _ in range(8):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)

        log(f"  Current URL: {driver.current_url}")
        # Prefer unique opportunity links with numeric IDs
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/walls/opportunity/']")
        log(f"  Found {len(links)} opportunity anchors")

        projects = []
        seen = set()
        for a in links:
            info = extract_card_info(a)
            if not info:
                continue
            if info["id"] in seen:
                continue
            seen.add(info["id"])
            projects.append(info)

        log(f"Extracted {len(projects)} valid projects")
        for i, p in enumerate(projects[:12], 1):
            log(f"  [{i}] {p['title'][:70]} | {p['url'][:80]}")
        if len(projects) > 12:
            log(f"  ... and {len(projects) - 12} more")
        if not projects:
            dump_page_structure(driver, "NO PROJECT CARDS")
        return projects
    except Exception as e:
        log(f"Scan failed: {e}")
        dump_page_structure(driver, "SCAN EXCEPTION")
        return []


def fetch_project_details(driver, url):
    details = {
        "description": "",
        "budget": "",
        "salary": "",
        "duration": "",
        "project_length": "",
        "location": "",
        "remote_type": "",
        "job_type": "",
        "engagement_type": "",
        "start_date": "",
        "timeline": "",
        "company": "",
        "status": "Open",
        "industry": "",
        "skills": [],
    }
    if not url or "/opportunity/" not in url:
        return details
    try:
        log(f"  Fetching details: {url[:100]}")
        driver.get(url)
        time.sleep(3)
        dismiss_cookie_banner(driver)

        body = driver.find_element(By.TAG_NAME, "body").text
        lines = [l.strip() for l in body.splitlines() if l.strip()]

        # Title from h1 if present
        try:
            h1 = clean_val(driver.find_element(By.CSS_SELECTOR, "h1").text)
            if h1 and h1.lower() not in _JUNK_TITLES:
                details["title"] = h1
        except Exception:
            pass

        labeled = _parse_labeled_lines(lines)
        details["remote_type"] = labeled["remote_type"]
        details["duration"] = labeled["duration"]
        details["project_length"] = labeled["duration"]
        details["location"] = labeled["location"]
        details["budget"] = labeled["budget"]
        details["salary"] = labeled["budget"]

        for line in lines:
            m = re.match(r"(?i)^start\s*:\s*(.+)$", line)
            if m:
                details["start_date"] = m.group(1).strip()
                details["timeline"] = m.group(1).strip()
            m = re.match(r"(?i)^engagement\s*:\s*(.+)$", line)
            if m:
                details["job_type"] = m.group(1).strip()
                details["engagement_type"] = m.group(1).strip()
            m = re.match(r"(?i)^fees?\s*:\s*(.+)$", line)
            if m:
                details["budget"] = m.group(1).strip()
                details["salary"] = m.group(1).strip()

        # Description block between "Description" and APPLY / INVITE
        desc = ""
        if re.search(r"(?im)^description\s*$", body) or "\nDescription\n" in body:
            chunk = re.split(r"(?i)\bdescription\b", body, maxsplit=1)[-1]
            for stopper in (
                "\nAPPLY\n",
                "I know someone perfect",
                "INVITE A FRIEND",
                "Recommend opportunities",
            ):
                if stopper in chunk:
                    chunk = chunk.split(stopper, 1)[0]
            # Drop leading labeled meta lines
            cleaned_lines = []
            for line in chunk.splitlines():
                s = line.strip()
                if not s:
                    if cleaned_lines:
                        cleaned_lines.append("")
                    continue
                if re.match(r"(?i)^(duration|region|mode|fees?|location|start|engagement)\s*:", s):
                    continue
                if s.lower() in _JUNK_TITLES:
                    continue
                cleaned_lines.append(s)
            desc = "\n".join(cleaned_lines).strip()
        details["description"] = desc[:5000]

        if details["description"]:
            first = details["description"].split("\n")[0]
            if re.search(r"(?i)\b(client|company|firm)\b", first):
                details["company"] = first[:120]

        log(
            f"    desc_len={len(details['description'])} "
            f"mode={details['remote_type'] or '-'} region={details['location'] or '-'} "
            f"duration={details['duration'] or '-'} fees={details['budget'] or '-'} "
            f"engagement={details['job_type'] or '-'}"
        )
    except Exception as e:
        log(f"  WARNING: Detail fetch failed for {url}: {e}")
    return details


# ============================
# DATABASE
# ============================
_mongo_projects_client = None


def _get_projects_collection():
    global _mongo_projects_client
    if _mongo_projects_client is None:
        _mongo_projects_client = MongoClient(Config.MONGO_URI)
    return _mongo_projects_client["office_monitor"][Config.PROJECTS_COLLECTION]


def init_db():
    try:
        _get_projects_collection().create_index("project_id", unique=True, name="idx_project_id_unique")
        log("MongoDB projects index ready")
    except Exception:
        pass


def db_is_cold_start():
    doc = _get_projects_collection().find_one({"platform": Config.PLATFORM_NAME}, {"_id": 1})
    return doc is None


def get_seen_ids():
    try:
        docs = _get_projects_collection().find(
            {"platform": Config.PLATFORM_NAME}, {"project_id": 1, "_id": 0}
        )
        return {d["project_id"] for d in docs if d.get("project_id")}
    except Exception as e:
        log(f"WARNING: Error loading seen IDs: {e}")
        return set()


def get_thin_or_junk_records():
    try:
        q = {
            "platform": Config.PLATFORM_NAME,
            "$or": [
                {"title": {"$in": ["My Opportunities", "My Hub", None, ""]}},
                {"url": {"$regex": r"/walls/opportunities/?$"}},
                {"description": {"$regex": r"^My Opportunities"}},
                {"description": {"$in": [None, ""]}},
                {"remote_type": {"$in": [None, ""]}},
                {"job_type": {"$in": [None, ""]}},
                {"location": {"$in": [None, "", "Not specified"]}},
            ],
        }
        return list(_get_projects_collection().find(q))
    except Exception as e:
        log(f"WARNING: thin-record query failed: {e}")
        return []


def delete_junk_records():
    """Remove bogus docs like title=My Opportunities pointing at the list page."""
    try:
        result = _get_projects_collection().delete_many(
            {
                "platform": Config.PLATFORM_NAME,
                "$or": [
                    {"title": {"$in": ["My Opportunities", "My Hub"]}},
                    {"url": {"$regex": r"/walls/opportunities/?$"}},
                    {"project_id": {"$regex": r"^outvise-[a-f0-9]{12}$"}, "url": {"$not": {"$regex": r"/opportunity/\d+"}}},
                ],
            }
        )
        if result.deleted_count:
            log(f"Deleted {result.deleted_count} junk Outvise record(s)")
    except Exception as e:
        log(f"WARNING: junk delete failed: {e}")


def _doc_from_project(project, emailed=None):
    doc = {
        "project_id": project.get("id"),
        "title": project.get("title"),
        "company": project.get("company") or "",
        "description": project.get("description") or project.get("snippet") or "",
        "location": project.get("location") or "",
        "budget": project.get("budget") or "",
        "salary": project.get("salary") or project.get("budget") or "",
        "duration": project.get("duration") or "",
        "project_length": project.get("project_length") or project.get("duration") or "",
        "job_type": project.get("job_type") or "",
        "industry": project.get("industry") or "",
        "remote_type": project.get("remote_type") or "",
        "start_date": project.get("start_date") or "",
        "status": project.get("status") or "Open",
        "time_posted": project.get("time_posted") or "",
        "url": project.get("url"),
        "detected_at": project.get("detected_at"),
        "platform": Config.PLATFORM_NAME,
        "skills": project.get("skills") or [],
        "engagement_type": project.get("engagement_type") or project.get("job_type") or "",
        "timeline": project.get("timeline") or project.get("start_date") or "",
    }
    if emailed is not None:
        doc["emailed"] = bool(emailed)
    return {k: v for k, v in doc.items() if v is not None}


def upsert_enriched_project(project, emailed=None):
    try:
        doc = _doc_from_project(project, emailed=emailed)
        pid = doc.pop("project_id", None) or project.get("id")
        set_doc = {k: v for k, v in doc.items() if v not in ("", None, [], "Not specified")}
        set_doc["project_id"] = pid
        set_doc["platform"] = Config.PLATFORM_NAME
        if emailed is not None:
            set_doc["emailed"] = bool(emailed)
        _get_projects_collection().update_one(
            {"project_id": pid},
            {"$set": set_doc},
            upsert=True,
        )
        log(f"  DB enrich OK: {pid}")
    except Exception as e:
        log(f"WARNING: DB enrich failed: {e}")


def insert_project(project, emailed=True):
    upsert_enriched_project(project, emailed=emailed)


def bulk_insert_projects(projects, emailed=False):
    for p in projects:
        upsert_enriched_project(p, emailed=emailed)


def enrich_project_list(driver, projects):
    enriched = []
    for idx, p in enumerate(projects):
        log(f"  [{idx + 1}/{len(projects)}] Enriching '{(p.get('title') or '')[:40]}'...")
        details = fetch_project_details(driver, p.get("url"))
        for k, v in details.items():
            if v:
                p[k] = v
        enriched.append(p)
    return enriched


# ============================
# EMAIL
# ============================
def _esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_notification(project):
    if os.getenv("SEND_EMAILS", "True").lower() == "false":
        log(f"Emails are disabled. Skipping notification for '{(project.get('title') or '')[:30]}'")
        return False
    if not Config.SENDER_EMAIL or not Config.SENDER_PASSWORD or not Config.RECIPIENT_EMAILS:
        return False
    try:
        title = project.get("title", "Untitled")
        url = project.get("url", "")
        html = f"""
        <html><body style="font-family:Arial,sans-serif;">
        <h2>New Outvise Opportunity</h2>
        <p><strong>{_esc(title)}</strong></p>
        <p>{_esc(project.get('description') or project.get('snippet') or '')[:800]}</p>
        <p>Location: {_esc(project.get('location'))}<br>
           Mode: {_esc(project.get('remote_type'))}<br>
           Budget: {_esc(project.get('budget'))}<br>
           Duration: {_esc(project.get('duration'))}</p>
        <p><a href="{_esc(url)}">View on Outvise</a></p>
        </body></html>
        """
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[Outvise] {title[:80]}"
        msg["From"] = Config.SENDER_EMAIL
        msg["To"] = ", ".join(Config.RECIPIENT_EMAILS)
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT) as server:
            server.starttls()
            server.login(Config.SENDER_EMAIL, Config.SENDER_PASSWORD)
            server.sendmail(Config.SENDER_EMAIL, Config.RECIPIENT_EMAILS, msg.as_string())
        log(f"  Email sent for '{title[:40]}'")
        return True
    except Exception as e:
        log(f"  WARNING: Email failed: {e}")
        return False


# ============================
# DRIVER + MAIN
# ============================
def initialize_driver():
    sys.path.insert(0, os.path.join(_script_dir, ".."))
    from chrome_helper import build_driver
    log(f"Starting Chrome (HEADLESS={Config.HEADLESS})")
    return build_driver()


def main():
    backfill = "--backfill" in sys.argv
    log("=" * 50)
    log("Outvise Monitor (One-Time Run)")
    log("=" * 50)
    log(f"  Login URL : {Config.LOGIN_URL}")
    log(f"  Target    : {Config.TARGET_URL}")
    log(f"  Email set : {bool(Config.OUTVISE_EMAIL)}")
    log(f"  Backfill  : {backfill}")
    log(f"  Recipients: {', '.join(Config.RECIPIENT_EMAILS) or '(none)'}")
    log("")

    driver = None
    try:
        driver = initialize_driver()
        has_session = load_cookies(driver)

        if has_session:
            target = Config.TARGET_URL
            log(f"Refreshing session via {target}")
            driver.get(target)
            time.sleep(4)
            dismiss_cookie_banner(driver)

        if not is_logged_in(driver):
            log("Session not found or expired. Logging in...")
            if not perform_login(driver):
                log("Authentication failed. Exiting.")
                sys.exit(1)

        init_db()
        delete_junk_records()
        cold_start = db_is_cold_start()
        seen_ids = get_seen_ids()
        log(f"Database loaded — {len(seen_ids)} Outvise records detected (cold_start={cold_start})")

        thin = get_thin_or_junk_records() if (backfill or not cold_start) else []
        # Drop junk titles from thin list — already deleted; remaining need enrich
        thin = [d for d in thin if d.get("url") and "/opportunity/" in (d.get("url") or "")]
        if thin:
            log(f"Enriching {len(thin)} existing thin Outvise record(s)...")
            for idx, doc in enumerate(thin):
                title = doc.get("title") or ""
                log(f"  [backfill {idx + 1}/{len(thin)}] {title[:40]}...")
                details = fetch_project_details(driver, doc.get("url"))
                project = {
                    "id": doc.get("project_id"),
                    "title": details.get("title") or title,
                    "url": doc.get("url"),
                    "detected_at": doc.get("detected_at"),
                    "time_posted": doc.get("time_posted"),
                    "budget": doc.get("budget"),
                    "snippet": doc.get("description"),
                }
                for k, v in details.items():
                    if v:
                        project[k] = v
                upsert_enriched_project(project)
            log(f"Backfill complete for {len(thin)} record(s).")

        opportunities_url = resolve_opportunities_url(driver)
        all_projects = scan_for_projects(driver, opportunities_url)

        if not all_projects:
            log("No projects found on the opportunities page.")
            return

        new_projects = [p for p in all_projects if p["id"] not in seen_ids]
        log(f"Stats: {len(all_projects)} visible, {len(seen_ids)} total seen, {len(new_projects)} new")

        if cold_start:
            log("Cold start: enriching all listings from detail pages...")
            all_projects = enrich_project_list(driver, all_projects)
            for p in all_projects:
                upsert_enriched_project(p, emailed=False)
            log(f"Seeding complete. {len(all_projects)} jobs cached.")
        elif new_projects:
            log(f"Found {len(new_projects)} new project(s)!")
            new_projects = enrich_project_list(driver, new_projects)
            for p in new_projects:
                emailed = send_notification(p)
                upsert_enriched_project(p, emailed=emailed)
                seen_ids.add(p["id"])
        else:
            log("No new projects detected.")

    except Exception as e:
        log(f"Critical Failure during monitor run: {e}")
        raise
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        log("Outvise Monitor run complete.")


if __name__ == "__main__":
    main()

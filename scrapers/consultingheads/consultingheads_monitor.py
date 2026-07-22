"""
ConsultingHeads Job Portal Monitor
==================================
Scrapes project/job opportunities from the ConsultingHeads candidate portal
and stores them in the shared MongoDB `office_monitor.projects` collection.

Login  : https://app.consultingheads.com/anmelden
Jobs   : https://app.consultingheads.com/jobs
Creds  : CONSULTINGHEADS_EMAIL / CONSULTINGHEADS_PASSWORD from .env
Optional: CONSULTINGHEADS_TARGET_URL (defaults to /jobs)

Usage:
    python consultingheads_monitor.py --once
    python consultingheads_monitor.py --once --debug
"""
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
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_script_dir = os.path.dirname(os.path.abspath(__file__))
_root_dir = os.path.abspath(os.path.join(_script_dir, "..", ".."))
if os.path.exists(os.path.join(_script_dir, ".env")):
    load_dotenv(dotenv_path=os.path.join(_script_dir, ".env"))
load_dotenv(dotenv_path=os.path.join(_root_dir, ".env"))

PKT = timezone(timedelta(hours=5))


class Config:
    PLATFORM_NAME = "consultingheads"
    SESSION_KEY = "consultingheads_cookies"
    PROJECTS_COLLECTION = "projects"

    EMAIL = os.getenv("CONSULTINGHEADS_EMAIL")
    PASSWORD = os.getenv("CONSULTINGHEADS_PASSWORD")

    SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SENDER_EMAIL = os.getenv("SENDER_EMAIL")
    SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
    RECIPIENT_EMAILS = [
        e.strip() for e in os.getenv("RECIPIENT_EMAILS", "").split(",") if e.strip()
    ]

    HEADLESS = os.getenv("HEADLESS", "True").lower() == "true"
    COOKIES_FILE = os.path.join(_script_dir, "consultingheads_cookies.json")
    MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")

    BASE_URL = "https://app.consultingheads.com"
    LOGIN_URL = "https://app.consultingheads.com/anmelden"
    TARGET_URL = os.getenv(
        "CONSULTINGHEADS_TARGET_URL", "https://app.consultingheads.com/jobs"
    ).strip()


DEBUG_MODE = "--debug" in sys.argv or True
ONCE_MODE = "--once" in sys.argv


def log(msg):
    ts = datetime.now(PKT).strftime("%H:%M:%S")
    print(f"[{ts} PKT] {msg}", flush=True)


def clean_val(t):
    if not t:
        return ""
    return re.sub(r"\s+", " ", t).strip()


def dismiss_overlays(driver):
    """Dismiss cookie consent (Alles akzeptieren) and similar overlays."""
    for el in driver.find_elements(By.CSS_SELECTOR, "button, a"):
        t = (el.text or "").strip().lower()
        if any(k in t for k in ("alles akzeptieren", "accept all", "akzeptieren", "agree", "ok")):
            # Prefer the strong accept-all over settings/reject
            if "einstellung" in t or "ablehnen" in t or "reject" in t:
                continue
            try:
                driver.execute_script("arguments[0].click();", el)
                log(f"  Cookie/consent dismissed: '{t}'")
                time.sleep(1.5)
                return True
            except Exception:
                pass
    return False


def dump_page_structure(driver, label="PAGE"):
    log("=" * 60)
    log(f"DIAGNOSTICS: {label}")
    log(f"  URL  : {driver.current_url}")
    log(f"  Title: {driver.title}")
    try:
        body = driver.find_element(By.TAG_NAME, "body").text
        for line in body[:800].splitlines()[:20]:
            if line.strip():
                log(f"    | {line.strip()[:120]}")
    except Exception as e:
        log(f"  body error: {e}")
    for sel in ["a[href*='/job/']", "div[class*='job']", "div[class*='card']", "article"]:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                sample = (els[0].text or "")[:80].replace("\n", " ")
                log(f"  [{len(els)}] {sel} -> {sample}")
        except Exception:
            pass
    log("=" * 60)


# ============================
# SESSION
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
            {"_id": Config.SESSION_KEY}, {"$set": session_data}, upsert=True
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
        dismiss_overlays(driver)
        driver.delete_all_cookies()
        for cookie in session_data["cookies"]:
            domain = cookie.get("domain") or ""
            if "consultingheads.com" in domain or not domain:
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

        if "anmelden" in url or "/login" in url:
            if "e-mail" in body and "passwort" in body and "anmelden" in body:
                return False
        if any(k in body for k in ("falsche", "ungültig", "incorrect", "invalid credentials")):
            if "anmelden" in url:
                return False
        if any(k in url for k in ("/dashboard", "/jobs", "/job/", "/kandidat", "/profil")):
            return True
        if "log out" in body or "abmelden" in body:
            return True
        return False
    except Exception:
        return False


def perform_login(driver):
    if not Config.EMAIL or not Config.PASSWORD:
        log("Login failed: CONSULTINGHEADS_EMAIL / CONSULTINGHEADS_PASSWORD not set")
        return False

    try:
        log(f"Navigating to login: {Config.LOGIN_URL}")
        driver.get(Config.LOGIN_URL)
        time.sleep(4)
        dismiss_overlays(driver)

        if is_logged_in(driver):
            log("Already authenticated")
            return True

        try:
            email_field = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "#email"))
            )
            password_field = driver.find_element(By.CSS_SELECTOR, "#password")
        except Exception:
            log("Login failed: could not find email/password fields")
            dump_page_structure(driver, "LOGIN FORM")
            return False

        email_field.clear()
        email_field.send_keys(Config.EMAIL)
        time.sleep(0.2)
        password_field.clear()
        password_field.send_keys(Config.PASSWORD)
        time.sleep(0.2)

        # Cookie banner can intercept normal click — use JS
        try:
            btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            driver.execute_script("arguments[0].click();", btn)
            log("Submitted login form (JS click on Anmelden)")
        except Exception:
            password_field.send_keys(Keys.ENTER)
            log("Submitted login form (Enter)")

        for i in range(20):
            time.sleep(1)
            body = ""
            try:
                body = driver.find_element(By.TAG_NAME, "body").text.lower()
            except Exception:
                pass
            if any(
                k in body
                for k in (
                    "diese zugangsdaten",
                    "falsche",
                    "ungültig",
                    "incorrect",
                    "credentials do not match",
                )
            ) and "anmelden" in driver.current_url.lower():
                log("Login failed: email or password are incorrect")
                return False
            if is_logged_in(driver):
                break
            if i % 5 == 4:
                log(f"  Waiting for login redirect... URL={driver.current_url}")

        if not is_logged_in(driver):
            log(f"Login failed: redirect did not succeed. URL={driver.current_url}")
            dump_page_structure(driver, "AFTER LOGIN")
            return False

        save_cookies(driver)
        log(f"Login successful -> {driver.current_url}")
        return True
    except Exception as e:
        log(f"Login failed: {e}")
        return False


# ============================
# EXTRACTION
# ============================
def _job_id_from_url(url):
    """URLs look like /job/some-slug-52973 — trailing digits are the job id."""
    m = re.search(r"/job/[^/?#]*?-(\d+)/?$", url or "")
    if m:
        return f"consultingheads-{m.group(1)}"
    m = re.search(r"/job/([^/?#]+)", url or "")
    if m:
        return f"consultingheads-{hashlib.md5(m.group(1).encode()).hexdigest()[:12]}"
    return f"consultingheads-{hashlib.md5((url or '').encode()).hexdigest()[:12]}"


def extract_listing_from_link(a_el):
    try:
        href = (a_el.get_attribute("href") or "").split("?")[0]
        if "/job/" not in href:
            return None
        text = clean_val(a_el.text)
        # Skip the "Mehr erfahren" duplicate links
        if text.lower() in ("mehr erfahren", "learn more", "read more", ""):
            return None

        # Try to get surrounding card text for metadata
        card_text = text
        try:
            parent = a_el.find_element(
                By.XPATH,
                "./ancestor::*[contains(@class,'card') or contains(@class,'job') "
                "or contains(@class,'item') or self::article or self::li][1]",
            )
            card_text = parent.text or text
        except Exception:
            pass

        lines = [l.strip() for l in card_text.splitlines() if l.strip()]
        title = text or (lines[0] if lines else "")
        if not title or len(title) < 5:
            return None

        budget = "Not specified"
        job_type = "Not specified"
        time_posted = "Recently"
        snippet = ""
        for line in lines[1:]:
            low = line.lower()
            if low in ("mehr erfahren", "learn more"):
                continue
            if any(w in low for w in ("verhandelbar", "negotiable", "€", "$", "tagessatz", "gehalt")):
                budget = line
            elif any(w in low for w in ("freelance", "festanstellung", "interim-mandat")):
                job_type = line
            elif re.search(r"\d{1,2}-\d{1,2}-\d{4}", line) or "ago" in low:
                time_posted = line
            elif len(line) > 60 and not snippet:
                snippet = line
            # skip "Monatlich" — it's a billing period label on the card, not contract type

        return {
            "id": _job_id_from_url(href),
            "title": title[:200],
            "snippet": snippet[:500],
            "budget": budget,
            "job_type": job_type,
            "duration": "Not specified",
            "location": "Not specified",
            "time_posted": time_posted,
            "url": href,
            "detected_at": datetime.now(PKT).strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:
        log(f"  card parse error: {e}")
        return None


def scan_for_projects(driver):
    log(f"Scanning jobs at: {Config.TARGET_URL}")
    try:
        driver.get(Config.TARGET_URL)
        time.sleep(5)
        dismiss_overlays(driver)

        # Infinite / lazy scroll until job-link count stabilizes
        prev_count = 0
        stable_rounds = 0
        for round_i in range(20):
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1.2)
            # Try common "load more" / pagination controls
            for el in driver.find_elements(By.CSS_SELECTOR, "button, a"):
                t = (el.text or "").strip().lower()
                if any(k in t for k in ("mehr laden", "load more", "weitere", "next", "nächste", "zeigen")):
                    try:
                        driver.execute_script("arguments[0].click();", el)
                        log(f"  Clicked load-more/pagination: '{t}'")
                        time.sleep(1.5)
                    except Exception:
                        pass
            count = len(driver.find_elements(By.CSS_SELECTOR, "a[href*='/job/']"))
            log(f"  Scroll round {round_i + 1}: {count} /job/ anchors")
            if count <= prev_count:
                stable_rounds += 1
                if stable_rounds >= 3:
                    break
            else:
                stable_rounds = 0
            prev_count = count

        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)

        log(f"  Current URL: {driver.current_url}")
        links = driver.find_elements(By.CSS_SELECTOR, "a[href*='/job/']")
        log(f"  Found {len(links)} anchors matching /job/")

        projects = []
        seen = set()
        for a in links:
            info = extract_listing_from_link(a)
            if not info:
                continue
            if info["id"] in seen:
                continue
            seen.add(info["id"])
            projects.append(info)

        log(f"Extracted {len(projects)} valid jobs")
        for i, p in enumerate(projects[:12], 1):
            log(f"  [{i}] {p['title'][:70]} | {p['url'][:90]}")
        if len(projects) > 12:
            log(f"  ... and {len(projects) - 12} more")

        if not projects:
            dump_page_structure(driver, "NO JOBS FOUND")
        return projects
    except Exception as e:
        log(f"Scan failed: {e}")
        dump_page_structure(driver, "SCAN EXCEPTION")
        return []


def fetch_project_details(driver, url):
    """Parse a ConsultingHeads job detail page into structured fields."""
    details = {
        "description": "",
        "budget": "",
        "salary": "",
        "duration": "",
        "project_length": "",
        "location": "",
        "job_type": "",
        "industry": "",
        "remote_type": "",
        "start_date": "",
        "company": "",
        "status": "Open",
        "skills": [],
        "engagement_type": "",
        "timeline": "",
    }
    if not url:
        return details
    try:
        log(f"  Fetching details: {url[:110]}")
        driver.get(url)
        time.sleep(3)
        dismiss_overlays(driver)

        body = driver.find_element(By.TAG_NAME, "body").text
        lines = [l.strip() for l in body.splitlines() if l.strip()]

        # Feature labels: VERTRAGSART / BRANCHE / JOB-ID
        try:
            for label_el in driver.find_elements(By.CSS_SELECTOR, ".job_feature_label, .label.job_feature_label"):
                key = clean_val(label_el.text).upper()
                val = ""
                try:
                    sib = label_el.find_element(By.XPATH, "./following-sibling::*[1]")
                    val = clean_val(sib.text)
                except Exception:
                    parent = clean_val(label_el.find_element(By.XPATH, "..").text)
                    val = clean_val(parent.replace(label_el.text, ""))
                if not val:
                    continue
                if "VERTRAGSART" in key:
                    details["job_type"] = val
                    details["engagement_type"] = val
                elif "BRANCHE" in key:
                    details["industry"] = val
        except Exception as e:
            log(f"    label parse warn: {e}")

        # Description: from Stellenbeschreibung until boilerplate
        desc = ""
        if "Stellenbeschreibung" in body:
            chunk = body.split("Stellenbeschreibung", 1)[1]
            for stopper in (
                "Jetzt bei consultingheads",
                "Über uns",
                "Recruiter:in",
                "Entdecke weitere",
                "Was wir tun",
            ):
                if stopper in chunk:
                    chunk = chunk.split(stopper, 1)[0]
            desc = chunk.strip()
        if not desc:
            # fallback: longest block
            candidates = []
            for sel in ["article", "main", ".job-description", "div[class*='description']", "p"]:
                for el in driver.find_elements(By.CSS_SELECTOR, sel):
                    txt = clean_val(el.text)
                    if 80 < len(txt) < 10000 and "consultingheads ist das" not in txt.lower():
                        candidates.append(txt)
            if candidates:
                desc = max(candidates, key=len)
        details["description"] = desc[:5000]

        # Eckdaten lines inside description / body
        for line in lines:
            low = line.lower()
            m = re.match(r"(?i)^start\s*:\s*(.+)$", line)
            if m:
                details["start_date"] = m.group(1).strip()
                details["timeline"] = details["timeline"] or m.group(1).strip()
                continue
            m = re.match(r"(?i)^dauer\s*:\s*(.+)$", line)
            if m:
                details["duration"] = m.group(1).strip()
                details["project_length"] = m.group(1).strip()
                continue
            m = re.match(r"(?i)^remote\s*/\s*on-?site\s*:\s*(.+)$", line)
            if m:
                val = m.group(1).strip()
                details["remote_type"] = val
                details["location"] = details["location"] or val
                continue
            m = re.match(r"(?i)^auslastung\s*:\s*(.+)$", line)
            if m:
                details["engagement_type"] = details["engagement_type"] or m.group(1).strip()
                continue
            m = re.match(r"(?i)^(?:tagessatz|gehalt|honorar|budget)\s*:?\s*(.+)$", line)
            if m:
                details["budget"] = m.group(1).strip()
                details["salary"] = m.group(1).strip()
                continue
            if "verhandelbar" in low and not details["budget"]:
                details["budget"] = "Verhandelbar"
                details["salary"] = "Verhandelbar"

        # Company hint from opening sentence (often anonymized)
        if details["description"]:
            first = details["description"].split("\n")[0].strip()
            m = re.search(
                r"(?i)(?:für|for)\s+(?:einen|eine|ein|a|an)\s+(.+?)\s+(?:suchen wir|suchen|looking for|is looking)",
                first,
            )
            if m:
                details["company"] = clean_val(m.group(1))[:120]
            elif re.search(r"(?i)(gmbh|ag|se|ltd|inc|group|konzern)", first):
                details["company"] = first[:120]

        # Must-have lines as rough skills
        skills = []
        for line in lines:
            if line.lower().startswith("must-have:"):
                skill = clean_val(line.split(":", 1)[-1])
                if skill and skill not in skills:
                    skills.append(skill[:80])
        details["skills"] = skills[:15]

        log(
            f"    desc_len={len(details['description'])} "
            f"type={details['job_type'] or '-'} industry={details['industry'] or '-'} "
            f"remote={details['remote_type'] or '-'} duration={details['duration'] or '-'} "
            f"start={details['start_date'] or '-'} company={details['company'][:40] or '-'}"
        )
    except Exception as e:
        log(f"  WARNING: Detail fetch failed: {e}")
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


def get_thin_records():
    """Existing CH docs that were seeded without real detail enrichment."""
    try:
        q = {
            "platform": Config.PLATFORM_NAME,
            "$or": [
                {"description": {"$exists": False}},
                {"description": None},
                {"description": ""},
                {"location": {"$in": [None, "", "Not specified"]}},
                {"duration": {"$in": [None, "", "Not specified"]}},
                {"industry": {"$in": [None, ""]}},
                {"remote_type": {"$in": [None, ""]}},
            ],
        }
        return list(_get_projects_collection().find(q))
    except Exception as e:
        log(f"WARNING: thin-record query failed: {e}")
        return []


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
        "engagement_type": project.get("engagement_type") or "",
        "timeline": project.get("timeline") or project.get("start_date") or "",
    }
    if emailed is not None:
        doc["emailed"] = bool(emailed)
    return {k: v for k, v in doc.items() if v is not None}


def insert_project(project, emailed=True):
    try:
        doc = _doc_from_project(project, emailed=emailed)
        _get_projects_collection().update_one(
            {"project_id": doc["project_id"]},
            {"$setOnInsert": doc},
            upsert=True,
        )
        log(f"  DB insert OK: {doc['project_id']}")
    except Exception as e:
        log(f"WARNING: DB insert failed: {e}")


def upsert_enriched_project(project, emailed=None):
    """Insert or enrich an existing record with detail-page fields (keeps inserted_to_sheet)."""
    try:
        doc = _doc_from_project(project, emailed=emailed)
        pid = doc.pop("project_id", None) or project.get("id")
        # Never blank out a good field with empty string on enrich
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


def bulk_insert_projects(projects, emailed=False):
    try:
        ops = []
        for p in projects:
            if not p.get("id"):
                continue
            doc = _doc_from_project(p, emailed=emailed)
            ops.append(
                UpdateOne({"project_id": doc["project_id"]}, {"$setOnInsert": doc}, upsert=True)
            )
        if ops:
            result = _get_projects_collection().bulk_write(ops, ordered=False)
            log(f"DB: Seeded {result.upserted_count} ConsultingHeads records")
    except Exception as e:
        log(f"WARNING: DB bulk seed failed: {e}")


def enrich_project_list(driver, projects):
    """Fetch detail pages and merge into project dicts."""
    enriched = []
    for idx, p in enumerate(projects):
        log(f"  [{idx + 1}/{len(projects)}] Enriching '{(p.get('title') or '')[:40]}'...")
        details = fetch_project_details(driver, p.get("url"))
        for k, v in details.items():
            if v:
                p[k] = v
        # List card often has Verhandelbar when detail has no rate
        if (not p.get("budget") or p.get("budget") == "Not specified") and details.get("budget"):
            p["budget"] = details["budget"]
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
        <h2>New ConsultingHeads Job</h2>
        <p><strong>{_esc(title)}</strong></p>
        <p>{_esc(project.get('description') or project.get('snippet') or '')[:800]}</p>
        <p>Type: {_esc(project.get('job_type'))}<br>
           Budget: {_esc(project.get('budget'))}<br>
           Location: {_esc(project.get('location'))}</p>
        <p><a href="{_esc(url)}">View on ConsultingHeads</a></p>
        </body></html>
        """
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[ConsultingHeads] {title[:80]}"
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
    log("ConsultingHeads Monitor (One-Time Run)")
    log("=" * 50)
    log(f"  Login URL : {Config.LOGIN_URL}")
    log(f"  Target    : {Config.TARGET_URL}")
    log(f"  Email set : {bool(Config.EMAIL)}")
    log(f"  Backfill  : {backfill}")
    log(f"  Recipients: {', '.join(Config.RECIPIENT_EMAILS) or '(none)'}")
    log("")

    driver = None
    try:
        driver = initialize_driver()
        has_session = load_cookies(driver)

        if has_session:
            log(f"Refreshing session via {Config.TARGET_URL}")
            driver.get(Config.TARGET_URL)
            time.sleep(4)
            dismiss_overlays(driver)

        if not is_logged_in(driver):
            log("Session not found or expired. Logging in...")
            if not perform_login(driver):
                log("Authentication failed. Exiting.")
                sys.exit(1)

        init_db()
        cold_start = db_is_cold_start()
        seen_ids = get_seen_ids()
        log(f"Database loaded — {len(seen_ids)} ConsultingHeads records detected (cold_start={cold_start})")

        # Always re-enrich thin/incomplete existing docs (or when --backfill)
        thin = get_thin_records() if (backfill or not cold_start) else []
        if backfill or thin:
            log(f"Enriching {len(thin)} existing thin record(s) from detail pages...")
            for idx, doc in enumerate(thin):
                url = doc.get("url")
                title = doc.get("title") or ""
                log(f"  [backfill {idx + 1}/{len(thin)}] {title[:40]}...")
                details = fetch_project_details(driver, url)
                project = {
                    "id": doc.get("project_id"),
                    "title": title,
                    "url": url,
                    "detected_at": doc.get("detected_at"),
                    "time_posted": doc.get("time_posted"),
                    "budget": doc.get("budget"),
                    "snippet": doc.get("description"),
                }
                for k, v in details.items():
                    if v:
                        project[k] = v
                if project.get("budget") in ("", None, "Not specified") and doc.get("budget"):
                    project["budget"] = doc["budget"]
                upsert_enriched_project(project)
            log(f"Backfill complete for {len(thin)} record(s).")

        all_projects = scan_for_projects(driver)
        if not all_projects:
            log("No projects found on the jobs page.")
            return

        new_projects = [p for p in all_projects if p["id"] not in seen_ids]
        log(f"Stats: {len(all_projects)} visible, {len(seen_ids)} total seen, {len(new_projects)} new")

        if cold_start:
            log("Cold start: enriching all listings from detail pages, then seeding...")
            all_projects = enrich_project_list(driver, all_projects)
            for p in all_projects:
                upsert_enriched_project(p, emailed=False)
            log(f"Seeding complete. {len(all_projects)} jobs cached. Monitoring for future new posts.")
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
        log("ConsultingHeads Monitor run complete.")


if __name__ == "__main__":
    main()

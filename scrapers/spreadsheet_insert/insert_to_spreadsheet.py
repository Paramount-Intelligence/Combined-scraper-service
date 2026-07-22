import os
import sys
import re
import json
import time
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pymongo import MongoClient
from groq import Groq

# Ensure UTF-8 output
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# Load .env file from this script's directory, falling back to the grandparent directory (root)
script_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(script_dir, ".env"))
grandparent_env = os.path.join(os.path.dirname(os.path.dirname(script_dir)), ".env")
if os.path.exists(grandparent_env):
    load_dotenv(dotenv_path=grandparent_env)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Initialize Groq client
if not GROQ_API_KEY:
    print("⚠️ WARNING: GROQ_API_KEY is not set in the environment or .env file.")
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Filter options
FILTER_ENGLISH_ONLY = os.getenv("FILTER_ENGLISH_ONLY", "True").lower() == "true"

def is_english(title: str, description: str) -> bool:
    """Detect if the project is in English using langdetect if available, with a regex fallback."""
    text = f"{title}\n{description}".strip()
    if not text:
        return True
    
    # 1. Try to use langdetect library
    try:
        from langdetect import detect
        # Clean text to avoid langdetect errors on purely numeric/special char inputs
        clean_text = re.sub(r'http\S+|[^\w\s]', '', text).strip()
        if len(clean_text) > 10:
            lang = detect(clean_text)
            return lang == 'en'
    except Exception:
        pass

    # 2. Heuristics fallback: CJK (Chinese, Japanese, Korean) character detection
    cjk_pattern = re.compile(r'[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7a3]')
    if cjk_pattern.search(text):
        return False

    # 3. Cyrillic character detection
    cyrillic_pattern = re.compile(r'[\u0400-\u04ff]')
    if cyrillic_pattern.search(text):
        return False

    # 4. Check density of ASCII characters as last resort
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    if len(text) > 0 and (ascii_chars / len(text)) < 0.60:
        return False

    return True

# Valid option lists for dropdown consistency
PLATFORM_CATEGORIES = [
    "Finance Modelling",
    "Growth Transformation",
    "Financial Planning and Analysis",
    "Business Simulation",
    "Visual Designer",
    "Merger and Acquisition",
    "Financial Controller",
    "Research And Development",
    "HR Strategy",
    "Research and Due Diligence",
    "Financial Reporting",
    "Retail Expert",
    "Reporting",
    "Organizational Structure",
    "Exports",
    "ERP",
    "Sales Lead",
    "Financial Consulting",
    "Transformation Consultant",
    "M&A Integration",
    "Project Engineering",
    "Market Access Strategy",
    "Brand Planning",
    "SOX Testing",
    "Oracle",
    "Pricing Models",
    "Value Creation",
    "Strategic Sourcing",
    "Profit & Loss (P&L)",
    "Quality Consultant",
    "Communications Specialist",
    "Pitch Deck Expert",
    "Costing Strategy",
    "Case Management",
    "Merger & Acquisition",
    "Carve-out Lead",
    "Benchmarking",
    "Technology Assessment",
    "Operator",
    "HR Lead",
    "Campaign Ops Expert",
    "Product Development",
    "HR Support",
    "Growth Assessment",
    "Support",
    "Cost Review",
    "Survey Analysis",
    "Assessment Consultant",
    "Market Consultant",
    "GTM Lead",
    "Fundraising Expert",
    "Logistics Optimization",
    "Finance Expert",
    "Trade Expert",
    "Marketing Expert",
    "Technology Optimization",
    "Commercial Expert",
    "Engagement Consultant",
    "Technology Implementation",
    "Data Analytics",
]

CATEGORIES = [
    "Business Process and Operations",
    "Data",
    "Finance and Accounting",
    "General Consulting",
    "GTM (Marketing + Sales)",
    "Information Technology",
    "Product Management",
    "Program and Project Management",
    "Research and Due Diligence",
    "Corporate Strategy and Development",
    "Subject Matter Expert"
]

UNIVERSAL_CATEGORIES = [
    "Business Process and Operations",
    "GTM (Marketing + Sales)",
    "Research and Due Diligence",
    "Corporate Strategy and Development",
    "Finance and Accounting",
    "Information Technology",
    "Subject Matter Expert",
    "Program and Project Management",
    "Data",
    "Product Management",
    "General Consulting"
]

INDUSTRIES = [
    "Financial Services",
    "Energy",
    "Materials",
    "Capital Goods",
    "Commercial & Professional Services",
    "Transportation",
    "Automotive",
    "Consumer Durables and Apparel",
    "Consumer Goods - Other",
    "Consumer Services",
    "Distribution",
    "Retail",
    "Healthcare Equipment and Svcs",
    "Pharma, BioTech, Life Sciences",
    "Banking",
    "Insurance",
    "Software and Services",
    "Technology Hardware",
    "Semiconductors and Equipment",
    "Telecommunications",
    "Media & Entertainment",
    "Utilities",
    "Real Estate Investment",
    "Real Estate Mgt and Dev",
    "OTHER",
    "Manufacturing",
    "Airlines & Aviation",
    "Technology",
    "Healthcare",
    "Industrials",
    "Public Sector"
]

INDUSTRIES_SECONDARY = [
    "Energy",
    "Pharma, BioTech, Life Sciences",
    "Consumer Goods - Other",
    "Software and Services",
    "Financial Services",
    "Retail",
    "Healthcare Equipment and Svcs",
    "Consumer Services",
    "Banking",
    "Utilities",
    "Capital Goods",
    "Insurance",
    "Materials"
]

ROLE_TYPES = ["Consultant", "Interim/Temporary", "OTHER"]

# Hard ceiling for consulting/freelance daily rates written to the spreadsheet (USD).
MAX_DAILY_RATE_USD = 2500.0
DEFAULT_DAILY_RATE_USD = 799.0

def sanitize_daily_rate_usd(val: float) -> float:
    """Clamp impossible LLM rate extractions; recover common dropped-decimal bugs."""
    if val <= MAX_DAILY_RATE_USD:
        return val
    # e.g. "$66.95 Hourly" misread as 6695 → 6695*8 = 53560; ÷100 restores ~535.60
    corrected = val / 100.0
    if 50.0 <= corrected <= MAX_DAILY_RATE_USD:
        print(f"    ⚠️ Daily rate ${val:,.2f} exceeds ${MAX_DAILY_RATE_USD:g}; corrected to ${corrected:,.2f} (÷100)")
        return corrected
    print(f"    ⚠️ Daily rate ${val:,.2f} exceeds ${MAX_DAILY_RATE_USD:g}; falling back to ${DEFAULT_DAILY_RATE_USD:g}")
    return DEFAULT_DAILY_RATE_USD

def query_groq_semantics(title, description, extra_fields=None):
    """Call Groq LLM to extract semantic classification and parameters in JSON format."""
    if not groq_client:
        return {}

    system_prompt = f"""You are a data extraction assistant. You will receive a job/project record from a freelance platform. Your job is to classify it and extract structured fields.

Return ONLY a valid JSON object — no markdown, no explanation, no extra text.

---

## Output Schema

{{
  "platform_category": string,
  "category": string,
  "industry": string,
  "industry_secondary": string,
  "role_type": string,
  "raw_rate_low": number or null,
  "raw_rate_high": number or null,
  "rate_currency": string or null ("USD", "GBP", "EUR", or null),
  "rate_period": string or null ("hourly", "daily", "monthly", "annually", or null),
  "duration_months_low": number,
  "duration_months_high": number,
  "utilization": number,
  "daily_rate_reasoning": string
}}

---

## Classification Fields

For each field (except platform_category), pick exactly one value from the allowed list. Do not invent new values.

- **platform_category** → A short, broad domain/category describing the project (e.g., "Data Analytics", "Finance Modelling", "HR Strategy"). You can pick one of these examples if it fits: {json.dumps(PLATFORM_CATEGORIES)}. If none of the examples fit, you must generate a new descriptive platform category describing the domain (keep it brief and capitalized like the examples). NEVER use "NaN", "None", null, or empty values.
- **category** → {json.dumps(CATEGORIES)}
- **industry** → {json.dumps(INDUSTRIES)}
- **industry_secondary** → {json.dumps(INDUSTRIES_SECONDARY)}
- **role_type** → {json.dumps(ROLE_TYPES)}

---

## Numeric Extraction Fields

### raw_rate_low / raw_rate_high / rate_currency / rate_period
Extract the raw numerical rate information exactly as stated in the fields or description.
- Set `raw_rate_low` and `raw_rate_high` to the raw numbers (no currency symbols, no commas). If no rate exists, set both to null.
- Preserve decimal points exactly (e.g. "$66.95" → 66.95, NOT 6695; "$77" → 77).
- Set `rate_currency` to one of: "USD", "GBP", "EUR" based on the symbol or text (e.g. £ -> GBP, € -> EUR, $ -> USD).
- Set `rate_period` to one of: "hourly", "daily", "monthly", "annually" based on how the rate is stated.
- Sanity check (max daily rate $2500 USD): after converting to daily USD (hourly×8, monthly÷20, annually÷260; GBP×1.27, EUR×1.08), each daily rate must be ≤ 2500. If your extracted numbers would imply a daily USD rate above 2500, you almost certainly dropped a decimal or misread the amount — re-read the source and correct it. Typical consulting/freelance daily rates are roughly 200–2500 USD.

### duration_months_low / duration_months_high
Extract contract length in months. If a range is specified (e.g. 3-6 months), set low to 3 and high to 6. If only one value is specified, use it for both low and high, never make up numbers from yourself. Default: 6.

### utilization
Full-time (≥8 hrs/day or 5 days/week) → 1.0
Part-time (~4 hrs/day) → 0.5
Light (~2 hrs/day) → 0.25
Default: 1.0
Note on utilization: Do not confuse on-site/remote/travel requirement percentages (e.g. 'on-site for 50% of the engagement', 'on-site for 3-4 weeks (50%)', or '50% travel') or standard workload variations (e.g. 'Team Lead manages workload weekly') with part-time/light utilization. These travel or split-location requirements still mean full-time (1.0) utilization. Only set utilization to 0.5 or 0.25 if the project explicitly specifies a part-time/reduced workload (e.g., '10 hours per week', '2 days per week', or 'part-time'). Otherwise, default to 1.0.

---

## daily_rate_reasoning
Explain where the raw values were found (e.g. "Found salary: '£45,000 per annum'").
"""

    record_dump = {k: v for k, v in (extra_fields or {}).items() if k != "_id"}
    user_content = f"Title: {title}\nDescription: {description}\n\nFull DB record:\n{json.dumps(record_dump, default=str, indent=2)}"
    
    max_retries = 7
    retry_delay = 10
    for attempt in range(max_retries):
        try:
            completion = groq_client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                response_format={"type": "json_object"},
                temperature=0.0
            )
            raw = completion.choices[0].message.content
            result = json.loads(raw)
            reasoning = result.get("daily_rate_reasoning", "No reasoning provided.")

            raw_low = result.get("raw_rate_low")
            raw_high = result.get("raw_rate_high")
            curr = result.get("rate_currency")
            per = result.get("rate_period")
            print(f"    🔍 LLM Extracted: {raw_low}-{raw_high} {curr}/{per} | Reasoning: {reasoning}")
            return result
        except Exception as e:
            err_str = str(e).lower()
            err_type = type(e).__name__.lower()
            is_rate_limit = (
                "rate_limit" in err_str or "429" in str(e) or
                "limit reached" in err_str or "too many requests" in err_str or
                "ratelimit" in err_type or "rate" in err_type
            )
            is_transient = (
                is_rate_limit or "503" in str(e) or "502" in str(e) or
                "service unavailable" in err_str or "connection" in err_str or
                "timeout" in err_str or "server" in err_type
            )
            if is_transient:
                wait_time = retry_delay * (2 ** attempt)
                # Cap at 120s to stay within orchestrator timeout budget
                wait_time = min(wait_time, 120)
                label = "rate limit" if is_rate_limit else "transient error"
                print(f"    ⚠️ Groq {label} (attempt {attempt + 1}/{max_retries}). Waiting {wait_time}s... error: {e}")
                time.sleep(wait_time)
            else:
                print(f"    ⚠️ Groq API call failed (non-retryable): {e}")
                return {}
    print(f"    ⚠️ Groq API retries exhausted after {max_retries} attempts. Using defaults.")
    return {}

def extract_country_or_na(project: dict) -> str:
    """Extract normalized country or N/A for remote jobs based on location fields."""
    loc = str(project.get("location", "")).lower()
    loc_pref = str(project.get("location_pref", "")).lower()
    rem = str(project.get("remote_type", "")).lower()
    job_type = str(project.get("job_type", "")).lower()
    
    all_text = f"{loc} {loc_pref} {rem} {job_type}"
    
    remote_terms = ["remote", "fully remote", "work from home", "wfh", "anywhere", "global", "worldwide"]
    for term in remote_terms:
        pattern = r'(?<![a-z])' + re.escape(term) + r'(?![a-z])'
        if re.search(pattern, all_text):
            return "N/A"

    country_map = {
        "usa": "United States", "us": "United States", "u.s.": "United States", "u.s.a.": "United States", "united states": "United States",
        "uk": "United Kingdom", "u.k.": "United Kingdom", "england": "United Kingdom", "united kingdom": "United Kingdom",
        "uae": "United Arab Emirates", "u.a.e.": "United Arab Emirates", "united arab emirates": "United Arab Emirates",
        "ksa": "Saudi Arabia", "saudi": "Saudi Arabia", "saudi arabia": "Saudi Arabia",
        "germany": "Germany", "france": "France", "india": "India", "pakistan": "Pakistan",
        "canada": "Canada", "australia": "Australia", "singapore": "Singapore",
        "netherlands": "Netherlands", "spain": "Spain", "italy": "Italy",
        "ireland": "Ireland", "malaysia": "Malaysia", "philippines": "Philippines"
    }

    sorted_keys = sorted(country_map.keys(), key=len, reverse=True)
    for term in sorted_keys:
        pattern = r'(?<![a-z])' + re.escape(term) + r'(?![a-z])'
        if re.search(pattern, all_text):
            return country_map[term]
            
    return ""

def determine_work_type(project: dict) -> str:
    """
    Classify Work Type from location metadata and description text.

    Soft onsite language (e.g. "occasionally on-site") must not force Onsite when
    the role is primarily remote / hybrid.
    """
    meta = " ".join([
        str(project.get("location", "")),
        str(project.get("location_pref", "")),
        str(project.get("remote_type", "")),
        str(project.get("job_type", "")),
    ]).lower()
    # Description / title / project_length: scrapers (esp. BTG) often bury
    # "primarily remote" outside structured location fields.
    narrative = " ".join([
        str(project.get("description", "")),
        str(project.get("title", "")),
        str(project.get("project_length", "")),
    ]).lower()
    all_text = f"{meta} {narrative}"

    has_hybrid = "hybrid" in all_text
    has_remote = any(w in all_text for w in [
        "remote", "wfh", "work from home", "work-from-home",
    ])
    # Occasional / light onsite or travel — not a pure Onsite role
    has_soft_onsite = bool(re.search(
        r"(occasionally|occasional|rarely|light|limited|potential|minimal)\s+"
        r"(on[\s-]?site|travel)|"
        r"(very\s+)?occasional(ly)?\s+(travel|on[\s-]?site)|"
        r"(light|potential|limited)\s*,?\s*(potential\s+)?travel",
        all_text,
    ))
    # Strong onsite only when not soft/occasional phrasing
    has_strong_onsite = bool(re.search(
        r"(?<!\w)(onsite|on-site|on site)(?!\w)",
        all_text,
    )) and not has_soft_onsite

    if has_hybrid:
        return "Hybrid"
    # Primarily remote + occasional on-site/travel → Hybrid (matches BTG labels)
    if has_remote and (has_soft_onsite or has_strong_onsite):
        return "Hybrid"
    if has_remote:
        return "Remote"
    if has_strong_onsite:
        return "Onsite"
    # Soft onsite alone (no remote keyword) → Hybrid, not Onsite
    if has_soft_onsite:
        return "Hybrid"
    return "Hybrid"

def map_record_to_row(project: dict) -> list:
    """Build spreadsheet row list from deterministic and semantic LLM logic."""
    # 1. Deterministic/Metadata parsing
    detected_at_str = project.get("detected_at", "")
    try:
        dt = datetime.strptime(detected_at_str, "%Y-%m-%d %H:%M:%S")
        scan_datetime = dt.strftime("%m/%d/%Y %H:%M:%S")
        week_num = dt.isocalendar()[1]
    except:
        scan_datetime = datetime.now().strftime("%m/%d/%Y %H:%M:%S")
        week_num = datetime.now().isocalendar()[1]

    # Calculate estimated posted date
    posted_date_est = ""
    time_posted = project.get("time_posted", "")
    if time_posted:
        try:
            m = re.search(r'(\d+)\s*(hour|day|week|month)s?\s*ago', time_posted, re.IGNORECASE)
            if m:
                val = int(m.group(1))
                unit = m.group(2).lower()
                now = datetime.now()
                if "hour" in unit:
                    est_dt = now
                elif "day" in unit:
                    est_dt = now - timedelta(days=val)
                elif "week" in unit:
                    est_dt = now - timedelta(weeks=val)
                elif "month" in unit:
                    est_dt = now - timedelta(days=val*30)
                posted_date_est = est_dt.strftime("%m/%d/%Y")
        except:
            pass
    if not posted_date_est:
        posted_date_est = datetime.now().strftime("%m/%d/%Y")

    # Work Type determination (metadata + description; soft onsite ≠ Onsite)
    work_type = determine_work_type(project)

    # Location cleaning
    clean_loc = extract_country_or_na(project)

    # 2. Call Groq for Semantic Classifications and Extraction
    title = project.get("title", "")
    desc = project.get("description", "")
    semantics = query_groq_semantics(title, desc, project)

    # Apply defaults if LLM did not return values or failed
    platform_category = semantics.get("platform_category")
    if platform_category:
        platform_category = str(platform_category).strip()
    if not platform_category or platform_category.lower() in ["nan", "none", "null", ""]:
        platform_category = "Support"

    category = semantics.get("category")
    if category not in CATEGORIES:
        category = "General Consulting"

    industry = semantics.get("industry")
    if industry not in INDUSTRIES:
        industry = "OTHER"

    industry_secondary = semantics.get("industry_secondary")
    if industry_secondary not in INDUSTRIES_SECONDARY:
        industry_secondary = "Consumer Goods - Other"

    role_type = semantics.get("role_type")
    if role_type not in ROLE_TYPES:
        role_type = "OTHER"

    # Python-based Daily Rate Math Calculations
    rate_low = DEFAULT_DAILY_RATE_USD
    rate_high = DEFAULT_DAILY_RATE_USD
    
    raw_low_val = semantics.get("raw_rate_low")
    raw_high_val = semantics.get("raw_rate_high")
    currency = semantics.get("rate_currency") or "USD"
    period = semantics.get("rate_period") or "daily"
    
    if raw_low_val is not None:
        try:
            val_low = float(raw_low_val)
            val_high = float(raw_high_val) if raw_high_val is not None else val_low
            
            # 1. Apply period conversion to daily rate
            if period == "hourly":
                val_low *= 8.0
                val_high *= 8.0
            elif period == "monthly":
                val_low /= 20.0
                val_high /= 20.0
            elif period == "annually":
                val_low /= 260.0
                val_high /= 260.0
                
            # 2. Apply currency conversion to USD
            if currency == "GBP":
                val_low *= 1.27
                val_high *= 1.27
            elif currency == "EUR":
                val_low *= 1.08
                val_high *= 1.08
                
            # 3. Cap / recover if LLM dropped a decimal (e.g. $66.95 → 6695 → $53,560/day)
            val_low = sanitize_daily_rate_usd(val_low)
            val_high = sanitize_daily_rate_usd(val_high)
                
            rate_low = round(val_low, 2)
            rate_high = round(val_high, 2)
        except Exception as e:
            pass

    try:
        dur_low = float(semantics.get("duration_months_low") or 6)
    except:
        dur_low = 6.0
    try:
        dur_high = float(semantics.get("duration_months_high") or 6)
    except:
        dur_high = 6.0

    try:
        utilization_val = float(semantics.get("utilization") or 1.0)
    except:
        utilization_val = 1.0

    # 3. Post-LLM Python potential value calculation
    # formula: duration months * daily rate * 20 working days * utilization
    WORKING_DAYS_PER_MONTH = 20
    pot_val_low = rate_low * dur_low * WORKING_DAYS_PER_MONTH * utilization_val
    pot_val_high = rate_high * dur_high * WORKING_DAYS_PER_MONTH * utilization_val

    # Format values back for spreadsheet columns
    rate_low_str = f"${int(rate_low):,}"
    rate_high_str = f"${int(rate_high):,}"
    duration_low_str = str(dur_low)
    duration_high_str = str(dur_high)
    utilization_str = str(utilization_val)
    value_low_str = f"${int(pot_val_low):,}"
    value_high_str = f"${int(pot_val_high):,}"

    # Source & Flat Platform mapping
    db_platform = project.get("platform", "fintalent")
    source_mapping = {
        "fintalent": "Fintalent",
        "catalant": "Catalant",
        "btg": "BTG",
        "movemeon": "Movemeon",
        "aquent": "Aquent",
        "eond": "EonD",
        "mbopartners": "MBO Partners",
        "outsized": "Outsized",
        "reed": "Reed",
        "talmix": "Talmix",
        "expert360": "Expert360",
        "outvise": "Outvise",
        "consultingheads": "ConsultingHeads",
    }
    source_name = source_mapping.get(db_platform.lower(), db_platform.title())
    flat_platform_name = db_platform.upper()

    row = [
        scan_datetime,                                  # Scan Date/Time
        posted_date_est,                                # Posted Date (est.)
        platform_category,                              # Platform Category
        category,                                       # Category
        title,                                          # Project
        desc,                                           # Description
        industry,                                       # Industry
        industry_secondary,                             # Industry - Secondary
        rate_low_str,                                   # Daily Rate - Low
        rate_high_str,                                  # Daily Rate
        duration_low_str,                               # Duration (Months) - Low
        duration_high_str,                              # Duration (Months)
        utilization_str,                                # Utilization %
        role_type,                                      # Role Type
        work_type,                                      # Work Type
        clean_loc,                                      # Location
        source_name,                                    # Source
        value_low_str,                                  # Potential Value - Low
        value_high_str,                                 # Potential Value
        project.get("url", ""),                         # Opportunity URL
        str(week_num),                                  # Week
        flat_platform_name                              # Flat Platform
    ]
    return row

# Send/mark progress every N mapped rows so a timeout or crash only loses the
# current chunk instead of the whole run.
CHUNK_SIZE = 20

# Default lookback window (days). Old orphans beyond this are never picked up.
LOOKBACK_DAYS = 3

def flush_chunk(collection, rows, ids, skipped_ids):
    """Post mapped rows to the webhook and mark all processed ids in MongoDB.

    Returns True if progress was saved (or nothing to send), False on webhook
    failure (flags left untouched so records are retried next run).
    """
    if rows:
        print(f"🚀 Sending chunk of {len(rows)} row(s) to webhook...")
        try:
            response = requests.post(WEBHOOK_URL, json={"rows": rows}, timeout=60)
            if response.status_code != 200:
                print(f"    ❌ Webhook returned unexpected status/body: {response.status_code} - {response.text}")
                print("    ⚠️ MongoDB flags left untouched for this chunk.")
                return False
            print("    ✅ Webhook accepted the chunk.")
        except Exception as e:
            print(f"    ❌ Failed to post chunk to webhook: {e}")
            print("    ⚠️ MongoDB flags left untouched for this chunk.")
            return False

    all_ids = ids + skipped_ids
    if all_ids:
        collection.update_many(
            {"_id": {"$in": all_ids}},
            {"$set": {"inserted_to_sheet": True}}
        )
        print(f"    💾 Marked {len(all_ids)} record(s) as inserted in MongoDB.")
    return True

def process_uninserted_records():
    """Main pipeline loop: pull new records, map, post to webhook in chunks."""
    print("🔌 Connecting to MongoDB...")
    client = MongoClient(MONGO_URI)
    db = client["office_monitor"]
    collection = db["projects"]

    # Allow target date to be specified as command-line argument
    if len(sys.argv) > 1:
        target_date_str = sys.argv[1]
        if target_date_str.lower() == "all":
            print("📅 Processing ALL uninserted records (ignoring date filter)")
            query = {
                "inserted_to_sheet": {"$ne": True},
                "platform": {"$ne": "reed"}
            }
        else:
            print(f"📅 Using command line specified target date: {target_date_str}")
            query = {
                "inserted_to_sheet": {"$ne": True},
                "detected_at": {"$regex": f"^{target_date_str}"},
                "platform": {"$ne": "reed"}
            }
    else:
        # Default: rolling window of the last LOOKBACK_DAYS days. Self-heals
        # failed/missed runs (yesterday's leftovers get picked up today) without
        # dragging in old orphans. detected_at is a "YYYY-MM-DD HH:MM:SS" string,
        # so lexicographic $gte comparison works.
        cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        target_date_str = f"last {LOOKBACK_DAYS} days (since {cutoff})"
        print(f"📅 Processing uninserted records from the {target_date_str}")
        query = {
            "inserted_to_sheet": {"$ne": True},
            "detected_at": {"$gte": cutoff},
            "platform": {"$ne": "reed"}
        }
    
    records = list(collection.find(query))
    if not records:
        print(f"💡 No new uninserted records found for {target_date_str}.")
        return

    print(f"📦 Found {len(records)} new project(s) to process.")
    
    rows = []
    inserted_ids = []
    skipped_ids = []
    total_sent = 0
    total_skipped = 0
    for i, rec in enumerate(records):
        title = rec.get("title", "Untitled")
        desc = rec.get("description", "")
        
        if FILTER_ENGLISH_ONLY and not is_english(title, desc):
            print(f"  → [{i+1}/{len(records)}] 🚫 Skipping non-English job: {title[:40]}...")
            skipped_ids.append(rec["_id"])
            total_skipped += 1
        else:
            print(f"  → [{i+1}/{len(records)}] Mapping & Classifying: {title[:40]}...")
            row = map_record_to_row(rec)
            print(f"    📋 Mapped: Platform Category='{row[2]}' | Category='{row[3]}' | Universal='{row[4]}' | Industry='{row[7]}' | Rate={row[9]}-{row[10]} | Duration={row[11]}-{row[12]} | Value={row[18]}-{row[19]}")
            rows.append(row)
            inserted_ids.append(rec["_id"])
            # Pace API calls to stay within Groq rate limits
            if i < len(records) - 1:
                time.sleep(1)

        # Persist progress every CHUNK_SIZE mapped rows
        if len(rows) >= CHUNK_SIZE:
            if not flush_chunk(collection, rows, inserted_ids, skipped_ids):
                print("🛑 Stopping run after webhook failure; unsent records will be retried next run.")
                return
            total_sent += len(rows)
            rows, inserted_ids, skipped_ids = [], [], []

    # Flush whatever is left (also marks trailing non-English skips)
    if rows or skipped_ids:
        if not flush_chunk(collection, rows, inserted_ids, skipped_ids):
            print("🛑 Final chunk failed; unsent records will be retried next run.")
            return
        total_sent += len(rows)

    if total_sent == 0 and total_skipped > 0:
        print(f"💡 No rows sent to spreadsheet (all {total_skipped} new records were filtered as non-English).")
    else:
        print(f"🎉 Finished processing. Sent {total_sent} row(s) to the sheet (skipped {total_skipped} non-English).")

if __name__ == "__main__":
    process_uninserted_records()

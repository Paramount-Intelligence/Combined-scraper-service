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

# Load .env file
local_env = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(local_env):
    load_dotenv(dotenv_path=local_env)
else:
    parent_dir = os.path.join(os.path.dirname(__file__), "..")
    env_path = None
    for root, dirs, files in os.walk(parent_dir):
        if ".env" in files:
            env_path = os.path.join(root, ".env")
            break
    if env_path:
        load_dotenv(dotenv_path=env_path)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Initialize Groq client
if not GROQ_API_KEY:
    print("⚠️ WARNING: GROQ_API_KEY is not set in the environment or .env file.")
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

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
  "universal_category": string,
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
- **universal_category** → {json.dumps(UNIVERSAL_CATEGORIES)}
- **industry** → {json.dumps(INDUSTRIES)}
- **industry_secondary** → {json.dumps(INDUSTRIES_SECONDARY)}
- **role_type** → {json.dumps(ROLE_TYPES)}

---

## Numeric Extraction Fields

### raw_rate_low / raw_rate_high / rate_currency / rate_period
Extract the raw numerical rate information exactly as stated in the fields or description.
- Set `raw_rate_low` and `raw_rate_high` to the raw numbers (no currency symbols, no commas). If no rate exists, set both to null.
- Set `rate_currency` to one of: "USD", "GBP", "EUR" based on the symbol or text (e.g. £ -> GBP, € -> EUR, $ -> USD).
- Set `rate_period` to one of: "hourly", "daily", "monthly", "annually" based on how the rate is stated.

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
    
    max_retries = 5
    retry_delay = 5
    for attempt in range(max_retries):
        try:
            completion = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
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
            
            # We will log the raw values extracted by LLM
            raw_low = result.get("raw_rate_low")
            raw_high = result.get("raw_rate_high")
            curr = result.get("rate_currency")
            per = result.get("rate_period")
            print(f"    🔍 LLM Extracted: {raw_low}-{raw_high} {curr}/{per} | Reasoning: {reasoning}")
            return result
        except Exception as e:
            if "rate_limit" in str(e).lower() or "429" in str(e) or "limit reached" in str(e).lower():
                print(f"    ⚠️ Groq rate limit hit (attempt {attempt + 1}/{max_retries}). Retrying in {retry_delay}s... error: {e}")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                print(f"    ⚠️ Groq API call failed: {e}")
                return {}
    return {}

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

    # Work Type determination
    work_type = "Hybrid"
    loc_lower = str(project.get("location", "")).lower()
    loc_pref_lower = str(project.get("location_pref", "")).lower()
    rem_lower = str(project.get("remote_type", "")).lower()
    job_type_lower = str(project.get("job_type", "")).lower()
    
    all_fields = loc_lower + " " + loc_pref_lower + " " + rem_lower + " " + job_type_lower
    if any(w in all_fields for w in ["hybrid"]):
        work_type = "Hybrid"
    elif any(w in all_fields for w in ["remote", "wfh", "work from home"]):
        work_type = "Remote"
    elif any(w in all_fields for w in ["onsite", "on-site", "on site"]):
        work_type = "Onsite"

    # Location cleaning
    raw_location = project.get("location", "") or project.get("location_pref", "") or ""
    clean_loc = re.sub(r'\b(person_pin_circle|location_on|place)\b', '', raw_location)
    clean_loc = re.sub(r'\s+', ' ', clean_loc).strip()
    if clean_loc.startswith(",") or clean_loc.startswith("-"):
        clean_loc = clean_loc[1:].strip()
    if not clean_loc:
        clean_loc = "NaN"

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

    universal_category_secondary = semantics.get("universal_category")
    if universal_category_secondary not in UNIVERSAL_CATEGORIES:
        universal_category_secondary = "General Consulting"

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
    rate_low = 799.0
    rate_high = 799.0
    
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
    }
    source_name = source_mapping.get(db_platform.lower(), db_platform.title())
    flat_platform_name = db_platform.upper()

    row = [
        scan_datetime,                                  # Scan Date/Time
        posted_date_est,                                # Posted Date (est.)
        platform_category,                              # Platform Category
        category,                                       # Category
        universal_category_secondary,                   # Universal Category - Secondary
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

def process_uninserted_records():
    """Main pipeline loop: pull new records, map, post to webhook in ONE batch."""
    print("🔌 Connecting to MongoDB...")
    client = MongoClient(MONGO_URI)
    db = client["office_monitor"]
    collection = db["projects"]

    # Allow target date to be specified as command-line argument
    if len(sys.argv) > 1:
        target_date_str = sys.argv[1]
        print(f"📅 Using command line specified target date: {target_date_str}")
    else:
        target_date_str = datetime.now().strftime("%Y-%m-%d")
        print(f"📅 Using default target date (today): {target_date_str}")

    query = {
        "inserted_to_sheet": {"$ne": True},
        "detected_at": {"$regex": f"^{target_date_str}"},
        "platform": {"$ne": "reed"}
    }
    
    records = list(collection.find(query))
    if not records:
        print(f"💡 No new uninserted records found for {target_date_str}.")
        return

    print(f"📦 Found {len(records)} new project(s) to process.")
    
    rows = []
    inserted_ids = []
    for rec in records:
        print(f"  → Mapping & Classifying: {rec.get('title', 'Untitled')[:40]}...")
        row = map_record_to_row(rec)
        print(f"    📋 Mapped: Platform Category='{row[2]}' | Category='{row[3]}' | Universal='{row[4]}' | Industry='{row[7]}' | Rate={row[9]}-{row[10]} | Duration={row[11]}-{row[12]} | Value={row[18]}-{row[19]}")
        rows.append(row)
        inserted_ids.append(rec["_id"])

    # Send ALL rows as a single batch payload
    print(f"🚀 Sending single batch payload of {len(rows)} records to webhook...")
    try:
        response = requests.post(WEBHOOK_URL, json={"rows": rows}, timeout=60)
        
        if response.status_code == 200:
            print("    ✅ Webhook accepted the batch payload.")
            
            # Update database to mark records as inserted
            collection.update_many(
                {"_id": {"$in": inserted_ids}},
                {"$set": {"inserted_to_sheet": True}}
            )
            print(f"🎉 Finished processing. Successfully processed and updated {len(inserted_ids)} records.")
        else:
             print(f"    ❌ Webhook returned unexpected status/body: {response.status_code} - {response.text}")
             print("    ⚠️ MongoDB flags left untouched to prevent data loss.")
             
    except Exception as e:
        print(f"    ❌ Failed to post batch payload to webhook: {e}")
        print("    ⚠️ MongoDB flags left untouched.")

if __name__ == "__main__":
    process_uninserted_records()

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
GROQ_CLASSIFICATION_MODEL = os.getenv(
    "GROQ_CLASSIFICATION_MODEL",
    "openai/gpt-oss-120b",
)

# Initialize Groq client
if not GROQ_API_KEY:
    print("⚠️ WARNING: GROQ_API_KEY is not set in the environment or .env file.")
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
if groq_client:
    print(f"🤖 Groq classification model: {GROQ_CLASSIFICATION_MODEL}")

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
    "Subject Matter Expert",
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
    "General Consulting",
]

# Normalized Category policy for Groq (Platform Category is separate and must not be copied).
CATEGORY_CLASSIFICATION_POLICY = """
## Normalized Category Policy (field name: category)

You must set `category` to exactly ONE value from the allowed Category list.
Never invent a new normalized Category.
Do not confuse fields:
- platform_category = source/domain label (may be free-form)
- category = our normalized internal Category (allowed list only)
- role_type = employment/engagement type
- Work Type (Remote/Hybrid/Onsite) is NOT your job here

Title and description carry the most weight. Platform Category / source breadcrumbs are supporting evidence only — do not copy them into `category`.

### Decision order
1. Identify the primary deliverable or responsibility.
2. Identify whether the buyer explicitly named a recognized function (Product Management, Project/Program Management, Information Security, Finance, Data, Corporate Strategy, etc.).
3. Determine whether the consultant is setting direction, managing delivery, performing technical work, conducting research, or providing broad consulting support.
4. Select the most specific supported category.
5. Use General Consulting when no other category is clearly supported.
6. Use Subject Matter Expert only when narrow expertise is the actual service being purchased.
7. Do not classify based on isolated keywords.
8. Do not classify based only on the project’s industry.
9. Do not classify based only on the Platform Category.
10. Do not return multiple categories.

### General Consulting vs Subject Matter Expert
Use Subject Matter Expert ONLY when the client is buying narrow, uncommon functional, scientific, technical, regulatory, geographic, or industry expertise outside normal consulting work.
Examples that may qualify: peptide formulation; satellite licensing regulation; shipping regulation in a specific country; highly specific financial regulations; narrow SAP module expertise where that exact domain is why they are hired; specialized scientific/clinical/engineering/regulatory disciplines.
Do NOT classify as Subject Matter Expert merely because the title/description contains: expert, SME, specialist, advisor, advisory, subject matter expert, deep expertise, industry expert. Those terms alone are insufficient.
Use General Consulting for broad/typical consulting that does not clearly fit another category, including: general analysis, team augmentation, requirements gathering, change management (organizational), general business analysis, training support, workshop facilitation, communications support, general transformation support, general consulting roles, analyst roles without a more specific functional category.
The dataset should contain substantially more General Consulting than Subject Matter Expert.
When uncertain between SME and General Consulting, select General Consulting.

### Information Technology vs Program and Project Management
Use Program and Project Management when the buyer explicitly requests: Project Manager, Program Manager, PM, PMO, IT Project Manager, IT Program Manager, Implementation Program Lead, Integration Management Office lead, program governance and delivery management.
An IT Project Manager remains Program and Project Management even if the project is technology-related.
Use Information Technology when the primary work involves: cybersecurity, information security, IT/enterprise architecture, infrastructure, cloud, systems engineering, software engineering, technology assessment, technical implementation, systems integration, testing, QA/UAT from a technical perspective, application support, IT operations, ERP configuration/implementation, building/evaluating/securing/operating technology.
Do not classify a technical role as Program and Project Management merely because it coordinates tasks or stakeholders.

### IT transformation
Corporate Strategy and Development only when deciding enterprise technology direction (enterprise technology strategy, future-state technology direction, technology investment priorities, enterprise transformation roadmap, build-versus-buy direction, target technology operating model, strategic platform decisions across the enterprise).
Program and Project Management when technology direction is already selected and the consultant manages implementation.
Information Technology when designing, assessing, integrating, configuring, securing, testing, or implementing the technology itself.

### Project management in finance
Finance and Accounting when a project manager must personally bring deep finance/accounting/controllership/audit/treasury/tax/FP&A/financial reporting expertise and that finance knowledge is central.
Program and Project Management when the person is primarily a general PM coordinating a finance-related initiative.
Do not use Finance and Accounting merely because Finance is a stakeholder or workstream.

### Product Management
Follow the buyer’s terminology. Strongly prefer Product Management when the buyer explicitly calls the role: Product Manager, Head of Product, Product Lead, Product Owner, Product Operations, Product Strategy, Product Development Lead.
Also Product Management when defining product vision, owning roadmap, prioritizing capabilities, building a new tool/platform/application/product, product discovery, connecting customer needs to requirements, product lifecycle decisions.
Information Technology when primarily technical implementation/configuration/integration/security/support/architecture.
Program and Project Management when managing delivery without owning product vision/roadmap/product decisions.

### Corporate Strategy and Development
Use when work sets direction for an enterprise, business unit, major portfolio, carveout, new venture, or transformation: enterprise/corporate/business-unit strategy, new business or market direction, enterprise operating model, carveout strategy, transformation strategy, enterprise capability roadmap, strategic portfolio choices, major build-versus-buy, long-term enterprise technology direction, CEO/CSO mandates.
Do not use when strategy is already decided and the consultant is executing implementation.

### Research and Due Diligence
Use for: Voice of the Customer, customer interviews/research, market interviews, primary research, competitive research, commercial/vendor/market due diligence, literature review, benchmarking to answer a defined research question, HEOR when core work is research/evidence generation/health economics/outcomes analysis.
Start HEOR under Research and Due Diligence; move to Program and Project Management only when primarily managing a large HEOR program rather than conducting/interpreting research.

### Business and functional requirements
Business/functional requirements are NOT automatically Information Technology.
Use Business Process and Operations or General Consulting for: business/functional/user requirements gathering, process mapping, workflow documentation, business analysis, current/future-state process analysis, translating stakeholder needs into requirements.
Use Information Technology only when substantially technical (solution/systems/API/data/security architecture, integration design, technical configuration, software development).

### Change management
General Consulting for non-technical organizational change management: stakeholder engagement, communications/adoption/sponsorship planning, organizational readiness, behavior change, training/capability building, workforce transition.
Program and Project Management only when clearly owning the broader program/project rather than only the change workstream.
Information Technology for IT systems change management: change tickets/requests, release management, ITSM, change control for systems, production changes, deployment governance, technical configuration changes.
Organizational change management ≠ IT systems change management.

### Data
Use Data for: data analytics, BI, Power BI, Tableau, Looker, data engineering/science, ML delivery, data architecture, taxonomy/ontology, analytics dashboards, data modeling/pipelines, reporting/insights, AI model development when primarily data science/ML.
Specific analytics tools such as Power BI normally map to Data.

### Enterprise Architect
Information Technology for Enterprise/Solution/Technical/Cloud/Application Architect and similar unless strong evidence the person sets enterprise-wide corporate strategy rather than technology architecture.
Do not classify Enterprise Architect as General Consulting.

### Training
General Consulting for most training (creation, delivery, learning content, capability building, workshop facilitation, adoption training).
Program and Project Management when managing a large-scale training program/portfolio/multi-workstream learning rollout but not personally building content.
Use another category only when training is clearly subordinate to that specialized function.

### Pricing
GTM (Marketing + Sales) when pricing is part of commercialization, product positioning, route to market, segmentation, promotion, revenue growth, or sales strategy.
Research and Due Diligence when primarily pricing research, benchmarking, willingness-to-pay analysis, market interviews, or evidence gathering.
Use the dominant purpose.

### Procurement
Start procurement, sourcing, supply management, vendor management, purchasing operations, and procurement transformation under Business Process and Operations; move only when clearly supported (e.g. procurement program manager → Program and Project Management; procurement due diligence → Research and Due Diligence).

### Business Analyst
Title “Business Analyst” alone does not mean Business Process and Operations. Start with General Consulting; move when responsibilities clearly support another category (process mapping → General Consulting or Business Process and Operations; dashboards → Data; product ownership → Product Management; technical systems analysis → Information Technology; finance analysis → Finance and Accounting; program coordination → Program and Project Management).

### Interim executives (defaults, then validate against actual work)
Interim CEO / Chief Strategy Officer → Corporate Strategy and Development
Interim CFO → Finance and Accounting
Interim CMO / CRO → GTM (Marketing + Sales)
Interim CTO / CIO → Information Technology
Interim Chief Scientific Officer → General Consulting
Interim Chief Human Capital / People Officer → Business Process and Operations

### category_reasoning / category_confidence
Provide a brief 1–2 sentence explanation of the primary responsibilities that drove the choice.
Set category_confidence between 0.0 and 1.0.
"""

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

def deterministic_category_fallback(title="", description="", extra_fields=None):
    """
    Conservative Category fallback when Groq fails or returns an invalid category.
    Does not treat expert/SME/advisor/specialist alone as Subject Matter Expert.
    """
    extra = extra_fields or {}
    text = " ".join([
        str(title or ""),
        str(description or ""),
        str(extra.get("industry", "") or ""),
        str(extra.get("job_type", "") or ""),
        str(extra.get("skills", "") or ""),
        str(extra.get("platform_category", "") or ""),
        str(extra.get("category_path", "") or ""),
        str(extra.get("breadcrumb", "") or ""),
    ]).lower()

    # Explicit interim / executive titles
    if re.search(r"\b(interim\s+)?cfo\b|chief financial officer", text):
        return "Finance and Accounting"
    if re.search(r"\b(interim\s+)?(cio|cto)\b|chief information officer|chief technology officer", text):
        return "Information Technology"
    if re.search(r"\b(interim\s+)?ceo\b|chief executive officer|chief strategy officer", text):
        return "Corporate Strategy and Development"
    if re.search(
        r"chief marketing officer|chief revenue officer|\b(interim\s+)?cmo\b|\b(interim\s+)?cro\b",
        text,
    ):
        return "GTM (Marketing + Sales)"
    if re.search(r"chief people officer|chief human capital officer", text):
        return "Business Process and Operations"

    # Explicit buyer product terminology
    if re.search(
        r"\b(head of product|product manager|product lead|product owner|"
        r"product operations|product strategy|product development lead)\b",
        text,
    ):
        return "Product Management"

    # Program / project management (incl. IT PM) before generic IT
    if re.search(
        r"\b(project manager|program manager|programme manager|\bpmo\b|"
        r"it project manager|it program manager|implementation program lead|"
        r"integration management office|training program manager)\b",
        text,
    ):
        return "Program and Project Management"
    if re.search(r"\b(project|program|programme)\s+manager\b|\bit\s*pm\b", text):
        return "Program and Project Management"

    # Enterprise / technical architects
    if re.search(
        r"\b(enterprise|solution|technical|cloud|application|data)\s+architect\b",
        text,
    ):
        return "Information Technology"

    # Data / analytics tools and roles
    if re.search(
        r"\b(power\s*bi|tableau|looker|data engineer|data scientist|"
        r"data analytics|machine learning|data pipeline|business intelligence)\b",
        text,
    ):
        return "Data"

    # Research / diligence
    if re.search(
        r"voice of (the )?customer|\bvoc\b|customer interviews|willingness[- ]to[- ]pay|"
        r"commercial due diligence|vendor due diligence|market diligence|"
        r"primary research|competitive research|pricing research",
        text,
    ):
        return "Research and Due Diligence"

    # Narrow SME domains only (not generic expert/SME/advisor/specialist keywords)
    if re.search(r"peptide formulation|satellite (licensing )?regulation|shipping regulation", text):
        return "Subject Matter Expert"

    # Enterprise technology strategy / direction (not implementation)
    if re.search(
        r"technology transformation strategy|enterprise technology (strategy|direction)|"
        r"future[- ]state technology|technology investment priorit|"
        r"build[- ]versus[- ]buy|target technology operating model|"
        r"(enterprise|corporate)\s+(technology\s+)?(strategy|transformation strategy)",
        text,
    ) and not re.search(r"\bimplementation\b", text):
        return "Corporate Strategy and Development"

    # IT security / engineering / systems / ERP / tech implementation (not org change)
    if re.search(
        r"\b(cybersecurity|information security|software engineering|cloud|"
        r"systems integration|erp (configuration|implementation)|"
        r"\bsap\b|s/?4\s*hana|technical implementation|"
        r"technology transformation implementation|"
        r"release management|change tickets?|it service management|"
        r"change control|deployment governance|technical configuration|"
        r"it change manager|systems engineering)\b",
        text,
    ):
        return "Information Technology"
    if re.search(r"\bimplementation (lead|consultant)\b", text) and re.search(
        r"\b(technology|erp|sap|system|cloud|software)\b", text
    ):
        return "Information Technology"

    # Procurement → BPO
    if re.search(
        r"\b(procurement|sourcing|supply management|vendor management|"
        r"purchasing operations|procurement transformation)\b",
        text,
    ):
        return "Business Process and Operations"

    # Pricing: GTM vs research
    if re.search(r"\bpricing\b", text):
        if re.search(
            r"willingness[- ]to[- ]pay|pricing research|benchmarking|market interviews",
            text,
        ):
            return "Research and Due Diligence"
        if re.search(
            r"\b(gtm|go[- ]to[- ]market|commerciali[sz]ation|sales strategy|"
            r"product positioning|revenue growth|route to market)\b",
            text,
        ):
            return "GTM (Marketing + Sales)"

    # Marketing / GTM without treating "expert" as SME
    if re.search(
        r"\b(marketing strategy|gtm|go[- ]to[- ]market|sales strategy|"
        r"commerciali[sz]ation)\b",
        text,
    ):
        return "GTM (Marketing + Sales)"

    return "General Consulting"


def resolve_normalized_category(semantics, title="", description="", extra_fields=None):
    """
    Prefer Groq's normalized Category when valid; otherwise conservative fallback.
    Returns (category, reasoning, confidence, source) where source is 'groq' or 'fallback'.
    """
    semantics = semantics or {}
    raw = semantics.get("category")
    if isinstance(raw, str):
        candidate = raw.strip()
    else:
        candidate = ""

    reasoning = str(semantics.get("category_reasoning") or "").strip()
    try:
        confidence = float(semantics.get("category_confidence"))
    except (TypeError, ValueError):
        confidence = None

    if candidate in CATEGORIES:
        if confidence is None:
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        if not reasoning:
            reasoning = "Groq returned an allowed Category without reasoning."
        return candidate, reasoning, confidence, "groq"

    fallback = deterministic_category_fallback(title, description, extra_fields)
    reason = (
        f"Groq category invalid or missing ({candidate!r}); "
        f"deterministic fallback selected {fallback}."
    )
    return fallback, reason, 0.0, "fallback"


def query_groq_semantics(title, description, extra_fields=None):
    """Call Groq LLM to extract semantic classification and parameters in JSON format."""
    if not groq_client:
        return {}

    system_prompt = f"""You are a data extraction assistant. You will receive a job/project record from a freelance platform. Your job is to classify it and extract structured fields.

Return ONLY a valid JSON object — no markdown, no explanation outside JSON fields, no chain-of-thought, no extra text.

---

## Output Schema

{{
  "platform_category": string,
  "category": string,
  "category_reasoning": string,
  "category_confidence": number,
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

- **platform_category** → A short, broad domain/category describing the project (e.g., "Data Analytics", "Finance Modelling", "HR Strategy"). You can pick one of these examples if it fits: {json.dumps(PLATFORM_CATEGORIES)}. If none of the examples fit, you must generate a new descriptive platform category describing the domain (keep it brief and capitalized like the examples). NEVER use "NaN", "None", null, or empty values. This is NOT the normalized Category field.
- **category** → exactly one of: {json.dumps(CATEGORIES)}
- **category_reasoning** → one or two concise sentences based on primary responsibilities (not lengthy analysis).
- **category_confidence** → number between 0.0 and 1.0
- **industry** → {json.dumps(INDUSTRIES)}
- **industry_secondary** → {json.dumps(INDUSTRIES_SECONDARY)}
- **role_type** → {json.dumps(ROLE_TYPES)}

{CATEGORY_CLASSIFICATION_POLICY}

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

    extra = extra_fields or {}
    platform_cat_hint = (
        extra.get("platform_category")
        or extra.get("category")
        or extra.get("industry")
        or ""
    )
    category_path = (
        extra.get("category_path")
        or extra.get("breadcrumb")
        or extra.get("skills")
        or ""
    )
    role_meta = {
        "job_type": extra.get("job_type"),
        "engagement_type": extra.get("engagement_type"),
        "remote_type": extra.get("remote_type"),
        "location": extra.get("location"),
        "company": extra.get("company"),
        "duration": extra.get("duration") or extra.get("project_length"),
        "budget": extra.get("budget") or extra.get("salary"),
    }
    record_dump = {k: v for k, v in extra.items() if k != "_id"}
    user_content = (
        f"Title: {title}\n"
        f"Description: {description}\n"
        f"Exact Platform Category (source label, supporting evidence only): {platform_cat_hint}\n"
        f"Source category path or breadcrumb: {category_path}\n"
        f"Role or employment metadata: {json.dumps(role_meta, default=str)}\n"
        f"Other relevant structured source fields:\n{json.dumps(record_dump, default=str, indent=2)}"
    )

    max_retries = 7
    retry_delay = 10
    for attempt in range(max_retries):
        try:
            completion = groq_client.chat.completions.create(
                model=GROQ_CLASSIFICATION_MODEL,
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
            cat = result.get("category")
            cat_reason = result.get("category_reasoning", "")
            cat_conf = result.get("category_confidence", "")
            print(f"    🏷️ LLM Category: {cat} (confidence={cat_conf}) | {cat_reason}")
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

    Prefer structured remote_type when unambiguous. ConsultingHeads labels like
    "Remote/On-Site: Remote" must not count the label's "On-Site" as onsite work.
    """
    remote_type = str(project.get("remote_type", "") or "").strip().lower()
    location = str(project.get("location", "") or "").strip().lower()
    location_pref = str(project.get("location_pref", "") or "").strip().lower()
    job_type = str(project.get("job_type", "") or "").strip().lower()

    def _has_remote(text: str) -> bool:
        return bool(re.search(
            r"(?<!\w)(fully\s+)?remote(?!\w)|(?<!\w)wfh(?!\w)|work[\s-]?from[\s-]?home",
            text,
        ))

    def _has_strong_onsite(text: str) -> bool:
        return bool(re.search(r"(?<!\w)(onsite|on-site|on site)(?!\w)", text))

    def _has_soft_onsite(text: str) -> bool:
        return bool(re.search(
            r"(occasionally|occasional|rarely|light|limited|potential|minimal)\s+"
            r"(on[\s-]?site|travel)|"
            r"(very\s+)?occasional(ly)?\s+(travel|on[\s-]?site)|"
            r"(light|potential|limited)\s*,?\s*(potential\s+)?travel",
            text,
        ))

    # Structured remote_type / location: trust clear values before narrative scan
    structured = f"{remote_type} {location} {location_pref}".strip()
    if structured:
        if "hybrid" in structured:
            return "Hybrid"
        struct_remote = _has_remote(structured)
        struct_soft = _has_soft_onsite(structured)
        struct_strong = _has_strong_onsite(structured) and not struct_soft
        if struct_remote and not struct_soft and not struct_strong:
            return "Remote"
        if struct_strong and not struct_remote:
            return "Onsite"
        if struct_remote and (struct_soft or struct_strong):
            return "Hybrid"

    meta = " ".join([location, location_pref, remote_type, job_type])
    narrative = " ".join([
        str(project.get("description", "") or ""),
        str(project.get("title", "") or ""),
        str(project.get("project_length", "") or ""),
    ]).lower()
    # Drop field-label "On-Site" in "Remote/On-Site: ..." so only the value counts
    narrative = re.sub(r"remote\s*/\s*on[\s-]?site\s*:", "remote:", narrative)

    all_text = f"{meta} {narrative}"

    has_hybrid = "hybrid" in all_text
    has_remote = _has_remote(all_text)
    has_soft_onsite = _has_soft_onsite(all_text)
    has_strong_onsite = _has_strong_onsite(all_text) and not has_soft_onsite

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

    category, category_reasoning, category_confidence, category_source = resolve_normalized_category(
        semantics, title, desc, project
    )
    print(
        f"    ✅ Category selected [{category_source}]: {category} "
        f"(confidence={category_confidence}) | {category_reasoning}"
    )

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

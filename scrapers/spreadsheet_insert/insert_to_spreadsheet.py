import os
import sys
import re
import json
import time
import requests
from datetime import datetime, timedelta
from typing import Literal, Optional
from dotenv import load_dotenv
from pymongo import MongoClient
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_PRIMARY_MODEL = os.getenv("GEMINI_PRIMARY_MODEL", "gemini-3.5-flash-lite")
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.6-flash")
# Backward-compatible alias used by older logs/tests
GEMINI_MODEL = os.getenv("GEMINI_MODEL", GEMINI_PRIMARY_MODEL)
ENABLE_MODEL_FALLBACK = os.getenv("ENABLE_MODEL_FALLBACK", "true").lower() == "true"
CATEGORY_CONFIDENCE_THRESHOLD = float(os.getenv("CATEGORY_CONFIDENCE_THRESHOLD", "0.70"))
AI_ATTEMPTS_PER_MODEL = int(os.getenv("AI_ATTEMPTS_PER_MODEL", "2"))
AI_REQUEST_DELAY_SECONDS = float(os.getenv("AI_REQUEST_DELAY_SECONDS", "2"))
RECORD_RETRY_ROUNDS = int(os.getenv("RECORD_RETRY_ROUNDS", "2"))
SPREADSHEET_CHUNK_SIZE = int(os.getenv("SPREADSHEET_CHUNK_SIZE", "5"))
MAX_RUN_SECONDS = int(os.getenv("MAX_RUN_SECONDS", "3300"))
SHUTDOWN_RESERVE_SECONDS = int(os.getenv("SHUTDOWN_RESERVE_SECONDS", "180"))
GEMINI_TIMEOUT_MS = int(os.getenv("GEMINI_TIMEOUT_MS", "90000"))
RETRY_PENDING_MAX_AGE_DAYS = int(os.getenv("RETRY_PENDING_MAX_AGE_DAYS", "30"))
# Fallback only when source JD/website and Gemini have no usable duration.
FALLBACK_DURATION_MONTHS = 12.0
# Repository-owned retries only (SDK auto-retries minimized via attempts=1)
CHUNK_SIZE = SPREADSHEET_CHUNK_SIZE

DURATION_SOURCE_FIELDS = [
    "duration",
    "project_duration",
    "project_length",
    "engagement_duration",
    "duration_text",
    "contract_duration",
    "estimated_duration",
]
INVALID_DURATION_VALUES = {
    "",
    "none",
    "null",
    "nan",
    "n/a",
    "tbd",
    "-",
    "not specified",
}

# Initialize Gemini client.
# Retry ownership: this module performs explicit per-model attempts (AI_ATTEMPTS_PER_MODEL).
# SDK automatic retries are minimized to attempts=1 to avoid stacked retry storms.
if not GEMINI_API_KEY:
    print("⚠️ WARNING: GEMINI_API_KEY is not set.")
gemini_client = None
if GEMINI_API_KEY:
    gemini_client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(
            timeout=GEMINI_TIMEOUT_MS,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
if gemini_client:
    print(
        f"🤖 Gemini primary={GEMINI_PRIMARY_MODEL} "
        f"fallback={GEMINI_FALLBACK_MODEL} "
        f"(fallback_enabled={ENABLE_MODEL_FALLBACK})"
    )


class AIClassificationError(Exception):
    """Raised when Gemini cannot produce usable semantics for a record."""

    def __init__(self, message, permanent=False):
        super().__init__(message)
        self.permanent = bool(permanent)


class PermanentGeminiConfigError(AIClassificationError):
    """API key / billing / schema configuration failure — stop the run."""

    def __init__(self, message):
        super().__init__(message, permanent=True)

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

# Normalized Category policy for Gemini (Platform Category is separate and must not be copied).
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

CategoryValue = Literal[
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

RoleTypeValue = Literal[
    "Consultant",
    "Interim/Temporary",
    "OTHER",
]

RateCurrencyValue = Literal["USD", "GBP", "EUR"]

RatePeriodValue = Literal["hourly", "daily", "monthly", "annually"]

IndustryValue = Literal[
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
    "Public Sector",
]

IndustrySecondaryValue = Literal[
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
    "Materials",
]


class ProjectSemantics(BaseModel):
    platform_category: str
    category: CategoryValue
    category_reasoning: str
    category_confidence: float = Field(ge=0.0, le=1.0)
    industry: IndustryValue
    industry_secondary: IndustrySecondaryValue
    role_type: RoleTypeValue
    raw_rate_low: Optional[float] = None
    raw_rate_high: Optional[float] = None
    rate_currency: Optional[RateCurrencyValue] = None
    rate_period: Optional[RatePeriodValue] = None
    duration_months_low: float
    duration_months_high: float
    utilization: float
    daily_rate_reasoning: str


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


def extract_source_platform_category(project: dict) -> str:
    """Return exact source Platform Category from scraped Mongo fields when present."""
    possible_fields = [
        "source_platform_category",
        "platform_category",
        "source_category",
        "project_category",
        "job_category",
        "functional_area",
        "category_path",
        "breadcrumb",
        "category",
    ]
    invalid_values = {"", "nan", "none", "null", "n/a"}
    for field in possible_fields:
        value = project.get(field)
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized and normalized.lower() not in invalid_values:
            return normalized
    return ""


def safe_float(value):
    """Parse a float; return None when missing or invalid."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _looks_like_experience_not_duration(text: str) -> bool:
    """Reject experience / seniority / education text that must not become engagement duration."""
    t = str(text or "").lower().strip()
    if not t:
        return True
    if re.search(
        r"\b(experience|experienced|seniority|years?\s+of\s+experience|"
        r"yrs?\s+of\s+experience|working\s+with)\b",
        t,
    ):
        return True
    if re.search(r"\b(mid|junior|senior)\s+level\b", t):
        return True
    if re.search(
        r"\b(education|degree|bachelor|master|phd|deadline|due date|closes on|founded)\b",
        t,
    ):
        return True
    # "5+ years" alone is almost always experience, not engagement length
    if re.search(r"\b\d+\s*\+\s*years?\b", t):
        if not re.search(r"\b(month|week|day|contract|engagement|duration)\b", t):
            return True
    return False


def _normalize_duration_unit(unit: str) -> str:
    u = (unit or "").lower().rstrip("s")
    if u.startswith("week"):
        return "week"
    if u.startswith("year"):
        return "year"
    if u.startswith("day"):
        return "day"
    return "month"


def _duration_pair_to_months(low: float, high: float, unit: str):
    kind = _normalize_duration_unit(unit)
    if low > high:
        low, high = high, low
    if kind == "week":
        return (low / 4.0, high / 4.0)
    if kind == "year":
        return (low * 12.0, high * 12.0)
    if kind == "day":
        return (low / 20.0, high / 20.0)
    return (low, high)


def parse_duration_to_months(duration_value: str):
    """
    Parse an explicit engagement-duration string into (low, high) months.
    Returns None when the text is missing, unparseable, or experience-like.
    """
    if duration_value is None:
        return None
    text = str(duration_value).strip()
    if not text or text.lower() in INVALID_DURATION_VALUES:
        return None
    if _looks_like_experience_not_duration(text):
        return None

    normalized = (
        text.replace("–", "-")
        .replace("—", "-")
        .replace("−", "-")
    )
    normalized = re.sub(r"\s+", " ", normalized)

    # Between 4 and 6 months
    m = re.search(
        r"(?i)\bbetween\s+(\d+(?:\.\d+)?)\s+and\s+(\d+(?:\.\d+)?)\s*"
        r"(months?|weeks?|years?|days?)\b",
        normalized,
    )
    if m:
        return _duration_pair_to_months(
            float(m.group(1)), float(m.group(2)), m.group(3)
        )

    # 3-5 months / 3 to 5 months / 6-12 weeks
    m = re.search(
        r"(?i)(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*"
        r"(months?|weeks?|years?|days?)\b",
        normalized,
    )
    if m:
        return _duration_pair_to_months(
            float(m.group(1)), float(m.group(2)), m.group(3)
        )

    # 6 Months / 12-month contract / Duration: 9 months
    m = re.search(
        r"(?i)(?:(?:duration|engagement(?:\s+length)?|contract(?:\s+length)?|"
        r"project(?:\s+length)?)\s*[:\-]?\s*)?"
        r"(\d+(?:\.\d+)?)\s*-?\s*(months?|weeks?|years?|days?)\b",
        normalized,
    )
    if m:
        val = float(m.group(1))
        return _duration_pair_to_months(val, val, m.group(2))

    return None


def _extract_labeled_duration_from_text(text: str) -> str:
    """Find an explicit Duration:/Engagement length: style value in free text."""
    if not text:
        return ""
    patterns = [
        r"(?i)\b(?:duration|engagement\s+length|contract\s+length|project\s+length|"
        r"engagement\s+duration|estimated\s+duration)\s*[:\-]\s*([^\n.;|]{1,80})",
        r"(?i)\b(?:duration|engagement)\s+of\s+"
        r"(\d+(?:\.\d+)?\s*(?:(?:-|to)\s*\d+(?:\.\d+)?)?\s*"
        r"(?:months?|weeks?|years?|days?))",
    ]
    for pat in patterns:
        match = re.search(pat, text)
        if not match:
            continue
        candidate = match.group(1).strip()
        if parse_duration_to_months(candidate):
            return candidate
    return ""


def extract_source_duration(project: dict) -> str:
    """
    Return the first valid duration explicitly captured from the
    source platform or structured project metadata.
    """
    project = project or {}
    for field in DURATION_SOURCE_FIELDS:
        value = project.get(field)
        if value is None:
            continue
        normalized = str(value).strip()
        if not normalized or normalized.lower() in INVALID_DURATION_VALUES:
            continue
        if parse_duration_to_months(normalized):
            return normalized

    for text_field in ("description", "summary", "title"):
        labeled = _extract_labeled_duration_from_text(
            str(project.get(text_field) or "")
        )
        if labeled:
            return labeled
    return ""


def resolve_duration_months(project: dict, semantics: dict):
    """
    Authoritative duration resolution.
    Priority: structured source → labeled text → Gemini → default.
    """
    source_duration_text = extract_source_duration(project)
    parsed_source = parse_duration_to_months(source_duration_text)
    if parsed_source:
        dur_low, dur_high = parsed_source
        print(
            f"    ⏱️ Duration source [source]: raw={source_duration_text!r} "
            f"→ {dur_low}-{dur_high} months"
        )
        return dur_low, dur_high, "source", source_duration_text

    dur_low = safe_float(semantics.get("duration_months_low"))
    dur_high = safe_float(semantics.get("duration_months_high"))
    if dur_low is not None:
        if dur_high is None:
            dur_high = dur_low
        print(
            f"    ⏱️ Duration source [gemini]: {dur_low}-{dur_high} months"
        )
        return float(dur_low), float(dur_high), "gemini", source_duration_text

    print(
        f"    ⚠️ No explicit duration found in source/JD. "
        f"Using fallback: {FALLBACK_DURATION_MONTHS:g} months"
    )
    return (
        FALLBACK_DURATION_MONTHS,
        FALLBACK_DURATION_MONTHS,
        "default",
        source_duration_text,
    )


def build_curated_record_dump(extra: dict) -> dict:
    """Compact metadata payload for Gemini — excludes empty values and full Mongo docs."""
    source_duration = extract_source_duration(extra)
    raw = {
        "platform": extra.get("platform"),
        "source_platform_category": extract_source_platform_category(extra),
        "category_path": extra.get("category_path"),
        "breadcrumb": extra.get("breadcrumb"),
        "job_type": extra.get("job_type"),
        "engagement_type": extra.get("engagement_type"),
        "placement_type": extra.get("placement_type"),
        "contract_type": extra.get("contract_type"),
        "remote_type": extra.get("remote_type"),
        "location": extra.get("location"),
        "location_pref": extra.get("location_pref"),
        "project_length": extra.get("project_length"),
        "duration": extra.get("duration"),
        "exact_source_duration": source_duration or None,
        "rate": extra.get("rate"),
        "salary": extra.get("salary"),
        "budget": extra.get("budget"),
        "skills": extra.get("skills"),
    }
    cleaned = {}
    for key, value in raw.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        cleaned[key] = value
    return cleaned


def deterministic_category_fallback(title="", description="", extra_fields=None):
    """
    Conservative Category fallback when Gemini fails or returns an invalid category.
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
    Prefer Gemini's normalized Category when valid; otherwise conservative fallback.
    Returns (category, reasoning, confidence, source) where source is 'gemini' or 'fallback'.
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
            reasoning = "Gemini returned an allowed Category without reasoning."
        return candidate, reasoning, confidence, "gemini"

    fallback = deterministic_category_fallback(title, description, extra_fields)
    reason = (
        f"Gemini category invalid or missing ({candidate!r}); "
        f"deterministic fallback selected {fallback}."
    )
    return fallback, reason, 0.0, "fallback"


def resolve_platform_category(project: dict, semantics: dict):
    """
    Catalant: keep exact scraped Platform Category (never overwrite with Gemini).
    Other platforms: use Gemini-generated Platform Category.
    Returns (platform_category, source_label).
    """
    semantics = semantics or {}
    db_platform = str(project.get("platform", "") or "").strip().lower()
    gemini_platform_category = str(semantics.get("platform_category") or "").strip()

    if db_platform == "catalant":
        source_platform_category = extract_source_platform_category(project)
        if source_platform_category:
            return source_platform_category, "catalant_source"
        print(
            "    ⚠️ Catalant source Platform Category "
            "was not captured. Using Unclassified."
        )
        return "Unclassified", "missing_catalant_source"

    if gemini_platform_category and gemini_platform_category.lower() not in {
        "nan", "none", "null", "",
    }:
        return gemini_platform_category, "gemini"
    return "Unclassified", "fallback"


def _is_permanent_gemini_error(error: Exception) -> bool:
    err_str = str(error).lower()
    permanent_markers = (
        "invalid api key",
        "api key not valid",
        "permission denied",
        "unauthenticated",
        "unauthorized",
        "forbidden",
        "billing",
        "invalid argument",
        "invalid_argument",
        "unsupported parameter",
        "unsupported schema",
        "missing gemini_api_key",
        "gemini_api_key is not set",
    )
    if any(m in err_str for m in permanent_markers):
        return True
    if any(code in err_str for code in ("401", "403", "400")) and not any(
        code in err_str for code in ("429", "500", "502", "503", "504")
    ):
        return True
    return False


def _is_transient_gemini_error(error: Exception) -> bool:
    if _is_permanent_gemini_error(error):
        return False
    if isinstance(error, ValidationError):
        return True
    err_str = str(error).lower()
    err_type = type(error).__name__.lower()
    transient_markers = (
        "429",
        "resource_exhausted",
        "resource exhausted",
        "500",
        "502",
        "503",
        "504",
        "timeout",
        "timed out",
        "connection reset",
        "connection aborted",
        "temporarily unavailable",
        "temporary",
        "unavailable",
        "deadline exceeded",
        "internal",
        "empty response",
        "invalid category",
        "schema validation",
        "failed projectsemantics",
        "validation",
    )
    if any(m in err_str for m in transient_markers):
        return True
    if "server" in err_type or "timeout" in err_type or "unavailable" in err_type:
        return True
    return False


def _gemini_retry_wait_seconds(error: Exception, attempt: int) -> float:
    """Prefer server-provided retry delay; else capped exponential backoff."""
    for attr in ("retry_delay", "retry_after", "retry_after_seconds"):
        val = getattr(error, attr, None)
        if val is None:
            continue
        try:
            if hasattr(val, "total_seconds"):
                secs = float(val.total_seconds())
            else:
                secs = float(val)
            if secs > 0:
                return min(secs, 90.0)
        except (TypeError, ValueError):
            pass
    err_str = str(error)
    m = re.search(r"retry[_ ]?delay[\"'=\s:]*([0-9]+(?:\.[0-9]+)?)s?", err_str, re.I)
    if m:
        try:
            return min(float(m.group(1)), 90.0)
        except ValueError:
            pass
    return float(min(15 * (2 ** attempt), 90))


def _build_semantics_prompts(title, description, extra_fields=None):
    system_prompt = f"""You are a data extraction assistant. You will receive a job/project record from a freelance platform. Your job is to classify it and extract structured fields.

Return ONLY a valid JSON object matching the required schema — no markdown, no explanation outside JSON fields, no chain-of-thought, no extra text.

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
Extract contract / engagement length in months.
For duration extraction, the explicit structured source-platform duration is authoritative.
Copy and convert that duration exactly.
Do not infer duration from experience requirements, seniority, deadlines, start dates, or unrelated numbers.
If a range is specified (e.g. 3-6 months), set low to 3 and high to 6.
If only one value is specified, use it for both low and high.
Never invent numbers. If no duration is stated in the source platform fields or JD, use 12 for both low and high.

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
    record_dump = build_curated_record_dump(extra)
    source_platform = str(extra.get("platform") or "").strip()
    source_pc = extract_source_platform_category(extra)
    source_duration = extract_source_duration(extra)
    user_content = (
        f"Title: {title}\n"
        f"Description: {description}\n"
        f"Source platform: {source_platform}\n"
        f"Exact source Platform Category: {source_pc}\n"
        f"Exact Source Duration: {source_duration or 'Not provided'}\n"
        f"Source category breadcrumb: {extra.get('category_path') or extra.get('breadcrumb') or ''}\n"
        f"Relevant employment / location / duration / rate metadata:\n"
        f"{json.dumps(record_dump, default=str, indent=2)}"
    )
    return system_prompt, user_content


def call_gemini_model(model_name: str, system_prompt: str, user_content: str) -> dict:
    """
    Call one Gemini model once (caller owns retries).
    Validates structured JSON via ProjectSemantics and allowed CATEGORIES.
    """
    if not gemini_client:
        raise PermanentGeminiConfigError("GEMINI_API_KEY is not set.")

    print(f"    🤖 Calling Gemini model: {model_name}")
    use_thinking = True
    last_error = None
    for thinking_try in range(2):
        try:
            config_kwargs = {
                "system_instruction": system_prompt,
                "response_mime_type": "application/json",
                "response_schema": ProjectSemantics,
            }
            if use_thinking:
                config_kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_level=types.ThinkingLevel.MINIMAL,
                )
            response = gemini_client.models.generate_content(
                model=model_name,
                contents=user_content,
                config=types.GenerateContentConfig(**config_kwargs),
            )

            usage = getattr(response, "usage_metadata", None)
            if usage:
                print(
                    f"    📊 Gemini usage: "
                    f"input={getattr(usage, 'prompt_token_count', None)}, "
                    f"output={getattr(usage, 'candidates_token_count', None)}, "
                    f"total={getattr(usage, 'total_token_count', None)}"
                )

            if not getattr(response, "text", None):
                raise ValueError("Gemini returned an empty response.")

            try:
                parsed = ProjectSemantics.model_validate_json(response.text)
            except ValidationError as ve:
                print("    ⚠️ Gemini response failed ProjectSemantics validation.")
                raise ve

            result = parsed.model_dump()
            category = str(result.get("category") or "").strip()
            if category not in CATEGORIES:
                raise ValueError(f"Invalid Category returned by the model: {category!r}")

            result["_gemini_model"] = model_name
            print(
                f"    🔍 Gemini Extracted: {result.get('raw_rate_low')}-{result.get('raw_rate_high')} "
                f"{result.get('rate_currency')}/{result.get('rate_period')} | "
                f"Reasoning: {result.get('daily_rate_reasoning', '')}"
            )
            print(
                f"    🏷️ Gemini Category: {category} "
                f"(confidence={result.get('category_confidence')}) | "
                f"{result.get('category_reasoning', '')}"
            )
            return result
        except Exception as e:
            last_error = e
            err_l = str(e).lower()
            if use_thinking and thinking_try == 0 and ("thinking" in err_l or "unsupported" in err_l):
                print(f"    ⚠️ Gemini thinking config unsupported; retrying without it: {e}")
                use_thinking = False
                continue
            raise
    raise last_error or RuntimeError("Gemini call failed")


def _call_model_with_bounded_attempts(model_name, system_prompt, user_content):
    """Up to AI_ATTEMPTS_PER_MODEL controlled attempts for one model."""
    last_error = None
    for attempt in range(AI_ATTEMPTS_PER_MODEL):
        try:
            return call_gemini_model(model_name, system_prompt, user_content)
        except Exception as e:
            last_error = e
            if _is_permanent_gemini_error(e):
                raise PermanentGeminiConfigError(str(e)) from e
            if attempt < AI_ATTEMPTS_PER_MODEL - 1 and _is_transient_gemini_error(e):
                wait_time = _gemini_retry_wait_seconds(e, attempt)
                print(
                    f"    ⚠️ Gemini transient error on {model_name} "
                    f"(attempt {attempt + 1}/{AI_ATTEMPTS_PER_MODEL}). "
                    f"Retrying in {wait_time}s: {e}"
                )
                time.sleep(wait_time)
                continue
            break
    raise last_error or AIClassificationError(f"{model_name} failed")


def query_gemini_semantics(title, description, extra_fields=None, prefer_fallback=False):
    """
    Primary/fallback Gemini classification.
    Raises AIClassificationError when both models fail (no silent empty defaults).
    """
    if not gemini_client:
        raise PermanentGeminiConfigError("GEMINI_API_KEY is not set.")

    system_prompt, user_content = _build_semantics_prompts(title, description, extra_fields)
    primary = GEMINI_PRIMARY_MODEL
    fallback = GEMINI_FALLBACK_MODEL

    if prefer_fallback:
        order = [fallback]
        if ENABLE_MODEL_FALLBACK and primary != fallback:
            order.append(primary)
    else:
        order = [primary]
        if ENABLE_MODEL_FALLBACK and fallback != primary:
            order.append(fallback)

    last_error = None
    for idx, model_name in enumerate(order):
        try:
            result = _call_model_with_bounded_attempts(model_name, system_prompt, user_content)
            confidence = float(result.get("category_confidence") or 0.0)
            # On first-pass primary only: escalate low confidence to fallback
            if (
                not prefer_fallback
                and idx == 0
                and model_name == primary
                and ENABLE_MODEL_FALLBACK
                and len(order) > 1
                and confidence < CATEGORY_CONFIDENCE_THRESHOLD
            ):
                print(
                    f"    ⚠️ Primary Category confidence {confidence:.2f} is below "
                    f"{CATEGORY_CONFIDENCE_THRESHOLD:.2f}"
                )
                print(f"    🔁 Escalating record to {fallback}")
                try:
                    return _call_model_with_bounded_attempts(fallback, system_prompt, user_content)
                except Exception as fb_err:
                    print(f"    ⚠️ Fallback escalation failed ({fb_err}); keeping primary result")
                    return result
            return result
        except PermanentGeminiConfigError:
            raise
        except Exception as e:
            last_error = e
            if idx < len(order) - 1:
                print(f"    ⚠️ Model failed for record: {e}")
                print(f"    🔁 Switching to fallback model: {order[idx + 1]}")
                continue
            break

    raise AIClassificationError(
        f"Both Gemini models failed: {last_error}"
    ) from last_error


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

def map_record_to_row(project: dict, prefer_fallback: bool = False) -> list:
    """Build spreadsheet row list from deterministic and semantic Gemini logic.

    Raises AIClassificationError when Gemini cannot classify the record.
    """
    # 1. Deterministic/Metadata parsing
    detected_at_str = project.get("detected_at", "")
    try:
        dt = datetime.strptime(detected_at_str, "%Y-%m-%d %H:%M:%S")
        scan_datetime = dt.strftime("%m/%d/%Y %H:%M:%S")
        week_num = dt.isocalendar()[1]
    except Exception:
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
        except Exception:
            pass
    if not posted_date_est:
        posted_date_est = datetime.now().strftime("%m/%d/%Y")

    # Work Type determination (metadata + description; soft onsite ≠ Onsite)
    work_type = determine_work_type(project)

    # Location cleaning
    clean_loc = extract_country_or_na(project)

    # 2. Call Gemini for Semantic Classifications and Extraction
    title = project.get("title", "")
    desc = project.get("description", "")
    semantics = query_gemini_semantics(
        title, desc, project, prefer_fallback=prefer_fallback
    )
    model_used = semantics.pop("_gemini_model", None)
    if model_used:
        print(f"    📌 Classification model used: {model_used}")
        project["_last_gemini_model"] = model_used

    platform_category, platform_category_source = resolve_platform_category(project, semantics)
    print(
        f"    🗂️ Platform Category selected "
        f"[{platform_category_source}]: "
        f"{platform_category}"
    )

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

    dur_low, dur_high, duration_source, _raw_source_duration = resolve_duration_months(
        project, semantics
    )

    try:
        utilization_val = float(semantics.get("utilization") or 1.0)
    except Exception:
        utilization_val = 1.0

    # 3. Post-LLM Python potential value calculation
    # formula: duration months * daily rate * 20 working days * utilization
    WORKING_DAYS_PER_MONTH = 20
    pot_val_low = rate_low * dur_low * WORKING_DAYS_PER_MONTH * utilization_val
    pot_val_high = rate_high * dur_high * WORKING_DAYS_PER_MONTH * utilization_val

    # Format values back for spreadsheet columns
    rate_low_str = f"${int(rate_low):,}"
    rate_high_str = f"${int(rate_high):,}"

    def _fmt_duration(val: float) -> str:
        return str(int(val)) if float(val).is_integer() else str(val)

    duration_low_str = _fmt_duration(dur_low)
    duration_high_str = _fmt_duration(dur_high)
    utilization_str = str(utilization_val)
    value_low_str = f"${int(pot_val_low):,}"
    value_high_str = f"${int(pot_val_high):,}"
    # Stash for clearer mapped-row logging (not part of spreadsheet schema)
    project["_duration_source"] = duration_source
    project["_mapped_rate_low"] = rate_low_str
    project["_mapped_rate_high"] = rate_high_str
    project["_mapped_duration_low"] = duration_low_str
    project["_mapped_duration_high"] = duration_high_str
    project["_mapped_utilization"] = utilization_str
    project["_mapped_value_low"] = value_low_str
    project["_mapped_value_high"] = value_high_str

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

# Default lookback window (days). Old orphans beyond this are never picked up
# unless they carry ai_classification_status=retry_pending within the retry age.
LOOKBACK_DAYS = 3

# Exit codes for run_all / Railway
EXIT_SUCCESS = 0
EXIT_PARTIAL_SUCCESS = 10
EXIT_RUNTIME_GUARD = 11
EXIT_PERMANENT_CONFIG = 12
EXIT_WEBHOOK_FAILURE = 13


def runtime_limit_approaching(run_started_at: float) -> bool:
    elapsed = time.monotonic() - run_started_at
    return elapsed >= (MAX_RUN_SECONDS - SHUTDOWN_RESERVE_SECONDS)


def _safe_error_message(error) -> str:
    msg = str(error or "")
    msg = re.sub(r"(api[_-]?key|authorization|bearer)\s*[:=]\s*\S+", r"\1=[REDACTED]", msg, flags=re.I)
    return msg[:500]


def save_retry_failure(collection, record, error):
    """Persist non-secret AI retry metadata; leave inserted_to_sheet untouched."""
    try:
        collection.update_one(
            {"_id": record["_id"]},
            {
                "$set": {
                    "ai_classification_status": "retry_pending",
                    "ai_last_error": _safe_error_message(error),
                    "ai_last_attempt_at": datetime.utcnow(),
                    "ai_last_primary_model": GEMINI_PRIMARY_MODEL,
                    "ai_last_fallback_model": GEMINI_FALLBACK_MODEL,
                },
                "$inc": {"ai_retry_count": 1},
            },
        )
    except Exception as e:
        print(f"    ⚠️ Failed to save retry metadata: {e}")


def mark_ai_retry_success(collection, record):
    try:
        collection.update_one(
            {"_id": record["_id"]},
            {
                "$set": {
                    "ai_classification_status": "completed",
                    "ai_last_error": None,
                    "ai_completed_at": datetime.utcnow(),
                }
            },
        )
    except Exception as e:
        print(f"    ⚠️ Failed to clear retry metadata: {e}")


def flush_pending_successes(collection, pending_rows, pending_success_ids, skipped_ids=None) -> bool:
    """
    Post only buffered successful rows. Mark only those IDs (+ intentional skips)
    after webhook HTTP success. Leave buffers intact on failure.
    """
    skipped_ids = skipped_ids if skipped_ids is not None else []
    if not pending_rows and not skipped_ids:
        return True

    if pending_rows:
        print(f"🚀 Sending chunk of {len(pending_rows)} row(s) to webhook...")
        try:
            response = requests.post(WEBHOOK_URL, json={"rows": pending_rows}, timeout=60)
            if response.status_code != 200:
                print(
                    f"    ❌ Webhook returned unexpected status/body: "
                    f"{response.status_code} - {response.text}"
                )
                print("    ⚠️ MongoDB flags left untouched for this chunk.")
                return False
            print("    ✅ Webhook accepted the chunk.")
        except Exception as e:
            print(f"    ❌ Failed to post chunk to webhook: {e}")
            print("    ⚠️ MongoDB flags left untouched for this chunk.")
            return False

    all_ids = list(pending_success_ids) + list(skipped_ids)
    if all_ids:
        now = datetime.utcnow()
        collection.update_many(
            {"_id": {"$in": all_ids}},
            {
                "$set": {
                    "inserted_to_sheet": True,
                    "ai_classification_status": "completed",
                    "ai_last_error": None,
                    "ai_completed_at": now,
                }
            },
        )
        print(f"    💾 Marked {len(all_ids)} record(s) as inserted in MongoDB.")

    pending_rows.clear()
    pending_success_ids.clear()
    skipped_ids.clear()
    return True


# Backward-compatible alias
def flush_chunk(collection, rows, ids, skipped_ids):
    return flush_pending_successes(collection, rows, ids, skipped_ids)


def process_uninserted_records():
    """Main pipeline: classify, flush on success/failure, deferred retry, runtime guard."""
    run_started_at = time.monotonic()
    run_status = "success"
    stats = {
        "loaded": 0,
        "inserted_first_pass": 0,
        "primary_successes": 0,
        "fallback_successes": 0,
        "deferred": 0,
        "deferred_retry_successes": 0,
        "still_pending": 0,
        "webhook_failures": 0,
        "skipped_non_english": 0,
        "runtime_guard": False,
    }

    print("🔌 Connecting to MongoDB...")
    client = MongoClient(MONGO_URI)
    db = client["office_monitor"]
    collection = db["projects"]

    if not GEMINI_API_KEY or gemini_client is None:
        print("RUN_STATUS=permanent_config")
        print("❌ GEMINI_API_KEY missing — aborting without silent defaults.")
        sys.exit(EXIT_PERMANENT_CONFIG)

    # Allow target date to be specified as command-line argument
    if len(sys.argv) > 1:
        target_date_str = sys.argv[1]
        if target_date_str.lower() == "all":
            print("📅 Processing ALL uninserted records (ignoring date filter)")
            query = {
                "inserted_to_sheet": {"$ne": True},
                "platform": {"$ne": "reed"},
            }
        else:
            print(f"📅 Using command line specified target date: {target_date_str}")
            query = {
                "inserted_to_sheet": {"$ne": True},
                "detected_at": {"$regex": f"^{target_date_str}"},
                "platform": {"$ne": "reed"},
            }
    else:
        cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        retry_cutoff = (
            datetime.now() - timedelta(days=RETRY_PENDING_MAX_AGE_DAYS)
        ).strftime("%Y-%m-%d")
        target_date_str = (
            f"last {LOOKBACK_DAYS} days (since {cutoff}) "
            f"+ retry_pending (since {retry_cutoff})"
        )
        print(f"📅 Processing uninserted records from the {target_date_str}")
        query = {
            "inserted_to_sheet": {"$ne": True},
            "platform": {"$ne": "reed"},
            "$or": [
                {"detected_at": {"$gte": cutoff}},
                {
                    "ai_classification_status": "retry_pending",
                    "detected_at": {"$gte": retry_cutoff},
                },
            ],
        }

    records = list(collection.find(query))
    stats["loaded"] = len(records)
    if not records:
        print(f"💡 No new uninserted records found for {target_date_str}.")
        print("RUN_STATUS=success")
        return

    print(f"📦 Found {len(records)} new project(s) to process.")

    pending_rows = []
    pending_success_ids = []
    skipped_ids = []
    retry_queue = []
    remaining_unprocessed = []

    # -------- First pass --------
    for i, rec in enumerate(records):
        if runtime_limit_approaching(run_started_at):
            print("⚠️ Runtime limit approaching. Stopping new AI calls.")
            print("💾 Flushing all completed rows before shutdown.")
            print("📌 Remaining records will stay uninserted for the next run.")
            stats["runtime_guard"] = True
            remaining_unprocessed.extend(records[i:])
            run_status = "runtime_guard"
            break

        title = rec.get("title", "Untitled")
        desc = rec.get("description", "")

        if FILTER_ENGLISH_ONLY and not is_english(title, desc):
            print(f"  → [{i+1}/{len(records)}] 🚫 Skipping non-English job: {title[:40]}...")
            skipped_ids.append(rec["_id"])
            stats["skipped_non_english"] += 1
            continue

        print(f"  → [{i+1}/{len(records)}] Mapping & Classifying: {title[:40]}...")
        try:
            row = map_record_to_row(rec, prefer_fallback=False)
            model_used = rec.get("_last_gemini_model") or GEMINI_PRIMARY_MODEL
            if model_used == GEMINI_FALLBACK_MODEL:
                stats["fallback_successes"] += 1
            else:
                stats["primary_successes"] += 1
            pending_rows.append(row)
            pending_success_ids.append(rec["_id"])
            rate_low = row[8]
            rate_high = row[9]
            duration_low = row[10]
            duration_high = row[11]
            utilization = row[12]
            value_low = row[17]
            value_high = row[18]
            print(
                f"    📋 Mapped: Platform Category='{row[2]}' | Category='{row[3]}' | "
                f"Industry='{row[6]}' | Rate={rate_low}-{rate_high} | "
                f"Duration={duration_low}-{duration_high} | Utilization={utilization} | "
                f"Value={value_low}-{value_high}"
            )
            if len(pending_rows) >= CHUNK_SIZE:
                before = len(pending_success_ids)
                if not flush_pending_successes(
                    collection, pending_rows, pending_success_ids, skipped_ids
                ):
                    stats["webhook_failures"] += 1
                    run_status = "webhook_failure"
                    print("RUN_STATUS=webhook_failure")
                    sys.exit(EXIT_WEBHOOK_FAILURE)
                stats["inserted_first_pass"] += before
            if i < len(records) - 1:
                time.sleep(AI_REQUEST_DELAY_SECONDS)
        except PermanentGeminiConfigError as error:
            print(f"    ❌ Permanent Gemini configuration error: {error}")
            if pending_rows or skipped_ids:
                flush_pending_successes(
                    collection, pending_rows, pending_success_ids, skipped_ids
                )
            remaining_unprocessed.extend(records[i:])
            run_status = "permanent_config"
            print("RUN_STATUS=permanent_config")
            _print_summary(stats, len(retry_queue) + len(remaining_unprocessed))
            sys.exit(EXIT_PERMANENT_CONFIG)
        except AIClassificationError as error:
            print(f"    ⚠️ AI classification failed: {error}")
            if pending_rows:
                before = len(pending_success_ids)
                if not flush_pending_successes(
                    collection, pending_rows, pending_success_ids, skipped_ids
                ):
                    stats["webhook_failures"] += 1
                    run_status = "webhook_failure"
                    print("RUN_STATUS=webhook_failure")
                    sys.exit(EXIT_WEBHOOK_FAILURE)
                stats["inserted_first_pass"] += before
            save_retry_failure(collection, rec, error)
            retry_queue.append(rec)
            stats["deferred"] += 1
            continue

    # Flush remaining first-pass successes
    if pending_rows or skipped_ids:
        before = len(pending_success_ids)
        if not flush_pending_successes(
            collection, pending_rows, pending_success_ids, skipped_ids
        ):
            stats["webhook_failures"] += 1
            run_status = "webhook_failure"
            print("RUN_STATUS=webhook_failure")
            sys.exit(EXIT_WEBHOOK_FAILURE)
        stats["inserted_first_pass"] += before

    # -------- Deferred retry pass --------
    if retry_queue and not stats["runtime_guard"] and run_status not in (
        "permanent_config",
        "webhook_failure",
    ):
        print(f"\n🔁 Starting deferred retry for {len(retry_queue)} record(s)...")
        for retry_round in range(RECORD_RETRY_ROUNDS):
            if not retry_queue:
                break
            if runtime_limit_approaching(run_started_at):
                print("⚠️ Runtime limit approaching during deferred retry.")
                stats["runtime_guard"] = True
                run_status = "runtime_guard"
                remaining_unprocessed.extend(retry_queue)
                retry_queue = []
                break

            current_queue = list(retry_queue)
            retry_queue = []
            print(f"  Retry round {retry_round + 1}/{RECORD_RETRY_ROUNDS}: {len(current_queue)} record(s)")

            for qi, rec in enumerate(current_queue):
                if runtime_limit_approaching(run_started_at):
                    print("⚠️ Runtime limit approaching. Stopping new AI calls.")
                    stats["runtime_guard"] = True
                    run_status = "runtime_guard"
                    remaining_unprocessed.extend(current_queue[qi:])
                    current_queue = []
                    break

                title = rec.get("title", "Untitled")
                print(f"  → Deferred retry: {title[:40]}...")
                try:
                    row = map_record_to_row(rec, prefer_fallback=True)
                    model_used = rec.get("_last_gemini_model") or GEMINI_FALLBACK_MODEL
                    print(f"    ✅ Deferred retry succeeded using {model_used}")
                    pending_rows.append(row)
                    pending_success_ids.append(rec["_id"])
                    stats["deferred_retry_successes"] += 1
                    if model_used == GEMINI_FALLBACK_MODEL:
                        stats["fallback_successes"] += 1
                    else:
                        stats["primary_successes"] += 1
                    mark_ai_retry_success(collection, rec)
                    if len(pending_rows) >= CHUNK_SIZE:
                        before = len(pending_success_ids)
                        if not flush_pending_successes(
                            collection, pending_rows, pending_success_ids, skipped_ids
                        ):
                            stats["webhook_failures"] += 1
                            run_status = "webhook_failure"
                            print("RUN_STATUS=webhook_failure")
                            sys.exit(EXIT_WEBHOOK_FAILURE)
                        stats["inserted_first_pass"] += before
                    time.sleep(AI_REQUEST_DELAY_SECONDS)
                except PermanentGeminiConfigError as error:
                    print(f"    ❌ Permanent Gemini configuration error: {error}")
                    if pending_rows or skipped_ids:
                        flush_pending_successes(
                            collection, pending_rows, pending_success_ids, skipped_ids
                        )
                    save_retry_failure(collection, rec, error)
                    remaining_unprocessed.append(rec)
                    remaining_unprocessed.extend(current_queue[qi + 1 :])
                    run_status = "permanent_config"
                    print("RUN_STATUS=permanent_config")
                    _print_summary(stats, len(remaining_unprocessed))
                    sys.exit(EXIT_PERMANENT_CONFIG)
                except AIClassificationError as error:
                    print(f"    ⚠️ Deferred retry still failing: {error}")
                    save_retry_failure(collection, rec, error)
                    retry_queue.append(rec)

            if pending_rows or skipped_ids:
                before = len(pending_success_ids)
                if not flush_pending_successes(
                    collection, pending_rows, pending_success_ids, skipped_ids
                ):
                    stats["webhook_failures"] += 1
                    run_status = "webhook_failure"
                    print("RUN_STATUS=webhook_failure")
                    sys.exit(EXIT_WEBHOOK_FAILURE)
                stats["inserted_first_pass"] += before

    # Anything still in retry_queue after rounds remains pending
    remaining_unprocessed.extend(retry_queue)
    stats["still_pending"] = len(remaining_unprocessed)

    if stats["runtime_guard"]:
        run_status = "runtime_guard"
    elif stats["still_pending"] > 0 and run_status == "success":
        run_status = "partial_success"
    elif stats["webhook_failures"]:
        run_status = "webhook_failure"

    _print_summary(stats, stats["still_pending"])
    print(f"RUN_STATUS={run_status}")

    if run_status == "success":
        sys.exit(EXIT_SUCCESS)
    if run_status == "partial_success":
        sys.exit(EXIT_PARTIAL_SUCCESS)
    if run_status == "runtime_guard":
        sys.exit(EXIT_RUNTIME_GUARD)
    if run_status == "permanent_config":
        sys.exit(EXIT_PERMANENT_CONFIG)
    if run_status == "webhook_failure":
        sys.exit(EXIT_WEBHOOK_FAILURE)
    sys.exit(EXIT_PARTIAL_SUCCESS)


def _print_summary(stats, still_pending):
    print("")
    print("=" * 50)
    print("Spreadsheet Insertion Summary")
    print("=" * 50)
    print(f"Records loaded: {stats.get('loaded', 0)}")
    print(f"Inserted during first pass / flushes: {stats.get('inserted_first_pass', 0)}")
    print(f"Primary-model successes: {stats.get('primary_successes', 0)}")
    print(f"Fallback-model successes: {stats.get('fallback_successes', 0)}")
    print(f"Deferred for retry: {stats.get('deferred', 0)}")
    print(f"Deferred retry successes: {stats.get('deferred_retry_successes', 0)}")
    print(f"Still pending for next run: {still_pending}")
    print(f"Webhook failures: {stats.get('webhook_failures', 0)}")
    print(f"Non-English skipped: {stats.get('skipped_non_english', 0)}")
    print(f"Runtime guard activated: {'Yes' if stats.get('runtime_guard') else 'No'}")
    print("=" * 50)


if __name__ == "__main__":
    process_uninserted_records()

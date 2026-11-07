import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from email.mime.text import MIMEText

import markdown
import requests
from google import genai
from google.genai import types
from google.cloud import secretmanager
from google.cloud import firestore
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

PROJECT_ID = "project-5ce9338e-bc20-4b9f-88f"
LOCATION = "global"  # gemini-3.1-pro-preview wordt alleen via 'global' bediend
MODEL = "gemini-3.1-pro-preview"
TO_EMAIL = "gesinktobias@gmail.com"
FIRESTORE_COLLECTION = "last_mile_tracked_items"
VENDOR_COLLECTION = "vendor_impact_items"
FEEDBACK_COLLECTION = "report_feedback"
BASELINE_COLLECTION = "category_baselines"
FEEDBACK_FORM_URL = "https://us-west1-project-5ce9338e-bc20-4b9f-88f.cloudfunctions.net/report-feedback"
VENDOR_STATUS_URL = "https://us-west1-project-5ce9338e-bc20-4b9f-88f.cloudfunctions.net/vendor-status-update"

# Betrouwbare terugvaloptie per toezichthouder wanneer een door het model
# opgegeven source_url niet blijkt te bestaan (404/DNS-fout/timeout) — voorkomt
# dat de mail dode of verzonnen links bevat.
FALLBACK_URLS = {
    "EBA": "https://www.eba.europa.eu/risk-and-data-analysis/reporting-frameworks",
    "DNB": "https://www.dnb.nl/en/login/dlr/statistical-reporting/banks/",
    "SRB": "https://www.srb.europa.eu/en",
    "ECB": "https://www.bankingsupervision.europa.eu/banking/tasks/reporting/html/index.en.html",
}

# Categorie-indeling exact volgens DNB's eigen Statistical Reporting > Banks
# pagina (https://www.dnb.nl/en/login/dlr/statistical-reporting/banks/).
DNB_CATEGORIES = [
    "AnaCredit reports",
    "Balance Sheet Items (BSI)",
    "Commercial Real Estate (CRE)",
    "MFI Interest Rates (MIR)",
    "Macroeconomic Statistics Reporting (MESREP)",
    "Monthly securities reporting (MSR)",
    "OTC derivatives reporting",
    "Payment Statistics",
    "Residential Real Estate (RRE)",
    "Special Purpose Vehicles (SPV)",
]

# Categorie-indeling volgens EBA Reporting Framework 4.2's areas of reporting /
# module-overzicht (https://www.eba.europa.eu/risk-and-data-analysis/reporting-frameworks/reporting-framework-42).
# Dit dekt de volledige modulelijst onder dit framework, niet alleen de vier
# gebieden die specifiek in 4.2 zijn gewijzigd (Instant Payments, Resolution
# Planning, Operational Risk/COREP OF, MREL decisions) — die vier worden vanzelf
# als wijziging gedetecteerd binnen hun bijbehorende module hieronder.
EBA_CATEGORIES = [
    "Asset Encumbrance (AE)",
    "ALMM (COREP_ALM)",
    "FRTB (COREP_FRTB)",
    "Large Exposures (COREP_LE)",
    "LCR Delegated Act (COREP_LCR_DA)",
    "Leverage Ratio (COREP_LR)",
    "Own Funds (COREP_OF)",
    "NSFR (COREP_NSFR)",
    "FINREP (FINREP9)",
    "G-SII Disclosure (GSII)",
    "IRRBB",
    "Resolution Planning / MREL",
    "Instant Payments Reporting",
    "Pillar 3 Data Hub (P3DH) / iREF",  # niet strikt onderdeel van 4.2's modulelijst maar wel binnen scope van de agent
]

ALL_CATEGORIES = DNB_CATEGORIES + EBA_CATEGORIES

PROMPT = """Act as a Principal Regulatory Reporting Specialist with deep expertise in European Banking Authority (EBA) frameworks, De Nederlandsche Bank (DNB) supervisory mandates, and Eurosystem statistical reporting architectures. You combine the analytical rigor of a data architect with the regulatory precision of a prudential reporting expert, and you are familiar with how policy shifts translate into concrete system and operating model changes at Dutch and European banks.

---

**Task**

Perform deep, source-grounded research and generate a comprehensive **Weekly Regulatory Intelligence Update** covering data architecture, prudential disclosure, resolution reporting, and Eurosystem harmonization. The output must be actionable for a senior audience spanning regulatory reporting leads, solution architects, and data governance officers at a Dutch bank operating under DNB and EBA supervision.

---

**Temporal Scope**

Scan for all policy updates, consultation papers, Q&A publications, taxonomy releases, press releases, and technical specification changes **published or last modified within the past 14 calendar days**. Where a development falls just outside this window but has material cross-reference to a current-period item, flag it explicitly as background context rather than a primary finding.

---

**Core Technical Focal Points**

For each focal point below, go beyond headline summaries — identify **what specifically changed**, **what it replaces or supersedes**, and **what the downstream data or system implication is**.

**1. EBA Reporting Framework Changes**
- New or revised ITS on supervisory reporting: COREP, FINREP, ALMM, LCR, NSFR, Large Exposures, and any other prudential templates
- Taxonomy and DPM releases: version increments, deprecated data points, new validation rules, and xBRL-CSV transition milestones
- EBA Q&A updates: newly published answers affecting reporting logic, scope of consolidation, or data point interpretation
- Draft and final RTS/ITS entering consultation or published in the Official Journal — note effective dates and transposition timelines
- Changes to reporting frequencies, submission deadlines, or institution-tier scope (LI / Other / SNCI)
- Any EBA Work Programme items or roadmap communications signalling upcoming framework changes

**2. DNB Reporting Changes & Decisions**
- DNB-specific reporting circulars, guidance notes, or supervisory letters affecting Dutch banks' reporting obligations
- Local implementation decisions: where DNB exercises national discretion within EBA frameworks (e.g., thresholds, opt-ins, phase-in elections)
- DNB iREF phasing decisions and local derogation positions relative to ECB statistical reporting timelines
- Changes to DNB's own data collection infrastructure, portal requirements, or submission tooling
- DNB enforcement decisions, supervisory findings, or thematic reviews with direct reporting implications
- DNB communications on XBRL filing requirements, taxonomy adoption schedules, or validation feedback loops
- Any DNB consultation documents open for response, including close dates

**3. Valuation Data Set 2.0 (EBA / DNB / SRB)**
- Changes to data dictionary definitions, attribute-level requirements, or scope of institutions
- MIS preparedness requirements: what data needs to be available, at what granularity, and by when
- Harmonization status between EBA resolution reporting and SRB valuation data expectations
- Any open consultation positions, Q&As, or ITS amendments relevant to resolution data quality

**4. Pillar 3 & Pillar 3 Data Hub (P3DH)**
- Rollout status of the EBA Pillar 3 Data Hub: submission windows, onboarding guidance, API/XBRL-CSV technical requirements
- Changes to data validation rules, taxonomy tags, or DPM-level mappings
- ITS amendments or draft RTS affecting disclosure scope, frequency, or granularity
- Tiering impacts: differentiated requirements for Large Institutions (LIs), Other Institutions, and Small and Non-Complex Institutions (SNCIs)
- Any EBA feedback cycles, data quality reports, or resubmission guidance

**5. Integrated Reporting Framework (iREF)**
- Progress on the paradigm shift from aggregated FINREP/COREP templates to granular statistical data models: BSI (Balance Sheet Items), MIR (MFI Interest Rates), SHS (Securities Holdings Statistics), and AnaCredit
- Eurosystem harmonization timelines: ECB-driven vs. EBA-driven requirements and where they converge or create dual-running obligations
- DNB-specific iREF implementation guidance, phasing decisions, or local derogations
- Parallel running challenges: overlap periods, data reconciliation requirements, and legacy template sunset timelines

---

**Target Supervisory Bodies**

Monitor the following sources exclusively. Do not infer or extrapolate from third-party commentary:

| Body | Scope |
|---|---|
| **EBA** | Reporting frameworks, ITS/RTS, taxonomy releases, DPM, P3DH, Q&A updates |
| **DNB** | Local implementation guidance, Dutch-specific circulars, iREF phasing, supervisory communications, national discretion decisions |
| **SRB** | Resolution reporting, Valuation Data Set requirements, MIS expectations |
| **ECB** | iREF statistical reporting architecture, BSI/MIR/SHS/AnaCredit, harmonization roadmaps |

---

**Output Structure**

Format using the section structure below. Use precise markdown headers, tight bullet points, and where a bullet describes a **change**, always state: *(i)* what the prior state was, *(ii)* what has changed, and *(iii)* the effective or expected implementation date.

---

### Section 1 — Executive Summary
- Limit to **3–5 structural compliance impacts** with the highest operational relevance for a Dutch bank
- Each bullet should be self-contained: topic, nature of change, and urgency indicator (`🔴 Immediate`, `🟡 Near-term`, `🟢 Monitor`)
- Close with a one-line **Reporting Period Verdict**: overall volume and significance of the week's regulatory activity

---

### Section 2 — Technical Breakdown: Data Taxonomy & Frameworks
Sub-sections:
- **2a. EBA Reporting Framework & DPM Updates** — taxonomy version changes, xBRL-CSV transition status, Data Point Model additions/removals, validation rule set updates, Q&A clarifications with reporting logic impact
- **2b. DNB Reporting Changes & Decisions** — Dutch-specific circulars, national discretion elections, portal or tooling changes, enforcement findings with reporting implications, open consultations
- **2c. Valuation Data Set 2.0** — data dictionary deltas, MIS attribute scope, harmonization status with SRB
- **2d. iREF Structural Dependencies** — granular data model progress, Eurosystem sequencing, DNB phasing updates
- **2e. Cross-Framework Interdependencies** — where AnaCredit, FINREP granular, and statistical reporting obligations overlap and create dual-sourcing or reconciliation requirements

---

### Section 3 — Disclosure & P3DH: Timelines and Operational Status
- Current submission windows and upcoming deadlines for P3DH
- Validation rule changes: list any new or modified rules with their rule identifiers where available
- Discussion papers open for comment: include consultation close dates
- Data quality findings or resubmission guidance from EBA or DNB
- Institution-tier impacts (LI / Other / SNCI) where differentiated timelines apply

---

### Section 4 — Action Items for Banks
Structure as a prioritized task list. For each action:
- **Owner**: Data Architecture / Regulatory Reporting / IT / Compliance / Risk
- **Trigger**: which regulatory development drives this
- **Action**: specific, concrete step (e.g., *"Map updated Valuation 2.0 attribute X to golden source field Y and validate coverage in AnaCredit loan-level extract"*)
- **Deadline or horizon**: firm date or rolling indicator (e.g., *next taxonomy release*, *Q3 2025*)

---

### Section 5 — Source Register
List every source cited in the update in a structured table:

| # | Body | Document Title | Publication Date | URL |
|---|---|---|---|---|

**Grounding Rules**

- Every finding **must be grounded in a primary source**: official EBA website, DNB.nl, SRB website, or ECB Statistical Data Warehouse / ECB Banking Supervision pages
- Every regulatory update in Sections 2 and 3 must end with a **markdown hyperlink** to the primary source document or press release
- If a development cannot be linked to a primary source published within the temporal scope, it must be **explicitly flagged** as unverified or background context
- Do **not** synthesize or infer regulatory intent from secondary sources, news aggregators, or law firm commentary
- If no material updates were found for a focal point, state this explicitly rather than padding with generic background
"""


LAST_MILE_PROMPT = """Act as a Principal Regulatory Reporting Specialist at a large Dutch bank, specialized in **last-mile regulatory reporting readiness** — the final operational stretch between a rule being finalized and it becoming a live, enforceable reporting obligation.

---

**Task**

Perform systematic, proactive horizon scanning of EBA, DNB, SRB, and ECB publications to identify obligations that are entering their final implementation phase. Unlike a retrospective weekly digest, this scan looks **forward**: what is about to go live, what has just been finalized (moved from draft/consultation to binding), and what submission or validation deadlines fall within the next 90 days.

---

**Scope — What Counts as "Last Mile"**

Only include items that meet at least one of these criteria:
1. An RTS/ITS has moved from **draft/consultation to final** and published in the Official Journal, with a transposition or application date within the next 12 months
2. A taxonomy, DPM, or validation rule set has been **frozen or released** with a mandatory adoption/submission date within 90 days
3. A phased rollout (e.g. P3DH tiering, iREF phasing) has a **specific institution-tier deadline** occurring within 90 days
4. A consultation is closing within 30 days, meaning the window to respond or prepare is closing
5. DNB, SRB, or ECB has published operational/technical guidance (portal changes, submission formats, XBRL-CSV validation feedback) with a near-term effective date

Do **not** include early-stage consultations, discussion papers without a firm timeline, or purely strategic/multi-year roadmap items — those belong in the broader weekly strategic digest, not here.

---

**Target Supervisory Bodies**: EBA, DNB, SRB, ECB — official sources only, no secondary commentary or law firm analysis.

---

**Vendor Impact / Horizon Scanning**

For each item, in addition to the existing relevance assessment, classify it
against our Last Mile regulatory reporting vendor's scope:

1. VENDOR_RELEVANT: true/false — does this affect templates, validation
   rules, taxonomies, or report obligations the vendor delivers (COREP,
   FINREP, AnaCredit, ALMM/LCR/NSFR)?

2. CHANGE_TYPE: one of [validation_rule, template_change, new_taxonomy_dpm,
   new_report_obligation, technical_standard_amendment, qa_clarification,
   other]

3. LEAD_TIME_CLASS:
   - SHORT (days-weeks): validation rule patches, EBA Q&A-driven fixes
   - MEDIUM (months): annual DPM/taxonomy releases, template updates
   - LONG (12+ months): new ITS, new report obligations
   Base this on the publication date vs. stated effective/reference date.

4. KEY_DATES: extract publication date, and any stated effective date,
   first reference date, or reporting deadline mentioned in the source.

5. ACTION_NEEDED: one-line suggested next step (e.g. "Confirm vendor
   roadmap coverage", "Request vendor SLA date for delivery",
   "Flag for parallel run planning").

Only populate this block when VENDOR_RELEVANT is true. Be conservative —
if uncertain about applicability to the vendor's scope, mark true and let
the human review filter it out, rather than silently dropping it.

---

**Output Format — CRITICAL**

Respond with **ONLY valid JSON**, no markdown, no prose before or after, matching exactly this schema:

{
  "scan_date": "YYYY-MM-DD",
  "period_verdict": "One sentence: overall volume/urgency of last-mile items this scan",
  "items": [
    {
      "body": "EBA|DNB|SRB|ECB",
      "topic": "Short topic label, e.g. 'P3DH SNCI onboarding'",
      "reporting_category": "One of the reporting categories provided in context — either a DNB statistical reporting category or an EBA Reporting Framework 4.2 area/module",
      "prior_state": "What the state was before this development",
      "what_changed": "What specifically changed or was finalized",
      "implementation_date": "YYYY-MM-DD or best available estimate",
      "urgency": "Immediate|Near-term|Monitor",
      "action_required": "One concrete, specific action a Dutch bank should take",
      "source_title": "Title of the source document/press release",
      "source_url": "Direct URL to the primary source",
      "publication_date": "YYYY-MM-DD",
      "vendor_impact": {
        "vendor_relevant": true,
        "change_type": "validation_rule|template_change|new_taxonomy_dpm|new_report_obligation|technical_standard_amendment|qa_clarification|other",
        "lead_time_class": "SHORT|MEDIUM|LONG",
        "key_dates": {
          "publication_date": "YYYY-MM-DD",
          "effective_date": "YYYY-MM-DD or not specified",
          "first_reference_date": "YYYY-MM-DD or not specified",
          "reporting_deadline": "YYYY-MM-DD or not specified"
        },
        "action_needed": "One-line suggested next step"
      }
    }
  ]
}

When VENDOR_RELEVANT is false, still include the "vendor_impact" object but with only "vendor_relevant": false — omit the other sub-fields in that case.

If no last-mile items are found in this scan, return an empty "items" array rather than padding with lower-relevance content.

**Grounding Rules**: Every item must be grounded in a primary source (EBA/DNB/SRB/ECB official site). Never infer implementation dates — if a date is not explicitly stated in the source, write "not specified" rather than estimating. For `source_url`: use ONLY a URL that was actually returned by your search tool in this session — never construct, guess, or infer a URL path from a title or pattern you've seen before, even if it looks plausible. If you are not fully certain of the exact URL for a specific document, use the toezichthouder's general reporting-frameworks landing page instead of a fabricated deep link.
"""


BASELINE_PROMPT_TEMPLATE = """Act as a Principal Regulatory Reporting Specialist. Establish a concise **current-state baseline** for the following DNB/EBA reporting category, for a large Dutch bank:

**Category: {category}**

Research via web search and summarize, as of today, the current reporting requirements, active taxonomy/DPM version, current thresholds or scope rules, and current submission cadence/format that apply to this category. This baseline will serve as the reference point against which future scans detect *changes only* — so be factual and specific (version numbers, dates, thresholds), not general background.

Respond with **ONLY valid JSON**, no markdown fencing, matching this schema:

{{
  "category": "{category}",
  "baseline_summary": "3-6 sentences describing the current state, with specific version numbers/dates/thresholds where known",
  "as_of_date": "YYYY-MM-DD"
}}

If you cannot find sufficiently specific, primary-sourced information for this category, still return the JSON with baseline_summary explaining what is and isn't known, rather than omitting the response.
"""


def verify_source_url(url, timeout=8):
    """Returns True unless the URL is clearly broken (404, DNS failure, timeout,
    connection refused). Bot-blocking (403 etc.) is treated as 'probably fine'
    since regulator sites often block scripted requests even for valid pages —
    only a hard 404 or network failure counts as broken/hallucinated."""
    if not url:
        return False
    headers = {"User-Agent": "Mozilla/5.0 (compatible; RegulatoryDigestBot/1.0)"}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True, stream=True)
        resp.close()
        return resp.status_code != 404
    except requests.RequestException:
        return False


def verify_and_fix_urls(items):
    """Mutates items in place: replaces any dead/hallucinated source_url with
    a safe fallback (the toezichthouder's known reporting-frameworks page)."""
    for item in items:
        url = item.get("source_url", "")
        body = item.get("body", "")
        if verify_source_url(url):
            item["_source_url_verified"] = True
        else:
            item["source_url"] = FALLBACK_URLS.get(body, url or "https://www.eba.europa.eu")
            item["_source_url_verified"] = False
    return items


def category_slug(category):
    return hashlib.sha256(category.encode("utf-8")).hexdigest()[:16]


def establish_baseline(category):
    """One-time (per category) grounded search to capture the current-state
    reference point. Only called for categories that don't have a baseline yet."""
    client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)
    response = client.models.generate_content(
        model=MODEL,
        contents=BASELINE_PROMPT_TEMPLATE.format(category=category),
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            response_mime_type="application/json",
        ),
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


def ensure_all_baselines():
    """First run: establishes a baseline per DNB + EBA category (one grounded
    search each). Subsequent runs: no-op for categories that already have a
    baseline — this is what makes it 'first search = current situation, from
    then on only changes'."""
    db = firestore.Client(project=PROJECT_ID)
    now = datetime.now(timezone.utc).isoformat()

    for category in ALL_CATEGORIES:
        doc_ref = db.collection(BASELINE_COLLECTION).document(category_slug(category))
        if doc_ref.get().exists:
            continue
        baseline = establish_baseline(category)
        doc_ref.set({
            "category": category,
            "baseline_summary": baseline.get("baseline_summary", ""),
            "as_of_date": baseline.get("as_of_date", now[:10]),
            "established_at": now,
            "last_updated": now,
        })


def get_all_baselines():
    db = firestore.Client(project=PROJECT_ID)
    docs = db.collection(BASELINE_COLLECTION).stream()
    return {doc.id: doc.to_dict() for doc in docs}


def format_baselines_for_prompt(baselines_by_slug):
    if not baselines_by_slug:
        return ""
    lines = []
    for category in ALL_CATEGORIES:
        data = baselines_by_slug.get(category_slug(category))
        if not data:
            continue
        lines.append(f"- **{category}** (baseline vastgesteld {data.get('as_of_date','')}): {data.get('baseline_summary','')}")
    return f"""

---

**Known Baselines — Report Changes Relative To These Only**

The following is the last established current-state baseline per category.
Do NOT re-describe or re-report this baseline as if it were new. Only report
items that represent a **change relative to this baseline** (a new version,
a new deadline, a finalized rule that supersedes what's listed here, etc.).
If nothing has changed for a category since its baseline, do not include an
item for it.

{chr(10).join(lines)}
"""


def get_recent_feedback(report_type, limit=10):
    """Fetch unresolved feedback for this report type, used to self-adjust the prompt."""
    db = firestore.Client(project=PROJECT_ID)
    docs = (
        db.collection(FEEDBACK_COLLECTION)
        .where("report_type", "==", report_type)
        .where("addressed", "==", False)
        .order_by("submitted_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    results = []
    for doc in docs:
        data = doc.to_dict()
        data["_id"] = doc.id
        results.append(data)
    return results


def mark_feedback_injected(feedback_items, max_injections=3):
    """Bump injection count; auto-resolve after it's been fed back N times."""
    db = firestore.Client(project=PROJECT_ID)
    for item in feedback_items:
        ref = db.collection(FEEDBACK_COLLECTION).document(item["_id"])
        times_injected = item.get("times_injected", 0) + 1
        ref.update({
            "times_injected": times_injected,
            "addressed": times_injected >= max_injections,
        })


def format_feedback_for_prompt(feedback_items):
    if not feedback_items:
        return ""
    lines = "\n".join(
        f"- [{f.get('category', 'overig')}] {f.get('comment', '')}"
        for f in feedback_items
    )
    return f"""

---

**Learned From Reader Feedback (address explicitly in this run)**

Readers of previous reports flagged the following gaps or issues. Actively check
whether these are now covered by your search and, if relevant material exists,
include it. If, after searching, nothing new is found for a flagged item, do not
fabricate content — simply note that no new material was found.

{lines}
"""


def build_feedback_footer(report_type):
    return f"""
    <hr style="margin-top:32px;border:none;border-top:1px solid #ddd;">
    <p style="font-size:12px;color:#888;">
      Content gemist die wel online te vinden was, of iets anders onduidelijk?
      <a href="{FEEDBACK_FORM_URL}?report={report_type}">Geef hier feedback</a> —
      dat wordt automatisch meegenomen in de volgende scan.
    </p>
    """


def get_secret(secret_id):
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")


def get_summary():
    client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)

    feedback_items = get_recent_feedback("weekly")
    full_prompt = PROMPT + format_feedback_for_prompt(feedback_items)

    response = client.models.generate_content(
        model=MODEL,
        contents=full_prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
        ),
    )

    if feedback_items:
        mark_feedback_injected(feedback_items)

    return response.text


def markdown_to_html(md_text):
    body_html = markdown.markdown(
        md_text, extensions=["tables", "fenced_code", "nl2br"]
    )
    footer_html = build_feedback_footer("weekly")
    return f"""\
<html>
<head>
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; line-height: 1.5; color: #1a1a1a; max-width: 800px; margin: 0 auto; padding: 20px; }}
  h1, h2, h3 {{ color: #0b3d91; margin-top: 24px; }}
  h2 {{ border-bottom: 2px solid #eee; padding-bottom: 6px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
  th, td {{ border: 1px solid #ddd; padding: 8px 10px; text-align: left; font-size: 14px; }}
  th {{ background-color: #f4f6f8; }}
  code {{ background-color: #f4f4f4; padding: 2px 5px; border-radius: 3px; font-size: 13px; }}
  a {{ color: #0b5fff; }}
</style>
</head>
<body>
{body_html}
{footer_html}
</body>
</html>
"""


def send_email(summary_text):
    creds = Credentials(
        token=None,
        refresh_token=get_secret("gmail-refresh-token"),
        client_id=get_secret("gmail-client-id"),
        client_secret=get_secret("gmail-client-secret"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    service = build("gmail", "v1", credentials=creds)

    html_body = markdown_to_html(summary_text)
    message = MIMEText(html_body, "html")
    message["to"] = TO_EMAIL
    message["subject"] = "Wekelijkse Regulatory Reporting Summary"
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def weekly_digest(request):
    summary = get_summary()
    send_email(summary)
    return "OK", 200


# ---------------------------------------------------------------------------
# Last-mile horizon scanner (sub-agent) — proactive, Firestore-backed change trail
# ---------------------------------------------------------------------------

def get_last_mile_findings():
    ensure_all_baselines()
    baselines = get_all_baselines()

    client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)

    feedback_items = get_recent_feedback("last_mile")
    full_prompt = (
        LAST_MILE_PROMPT
        + format_baselines_for_prompt(baselines)
        + format_feedback_for_prompt(feedback_items)
    )

    response = client.models.generate_content(
        model=MODEL,
        contents=full_prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            response_mime_type="application/json",
        ),
    )

    if feedback_items:
        mark_feedback_injected(feedback_items)

    text = response.text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    result = json.loads(text)
    result["items"] = verify_and_fix_urls(result.get("items", []))
    return result


def make_item_id(item):
    key = f"{item['body']}::{item['topic']}".lower().strip()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def update_tracked_items(items):
    """Dedup against Firestore, update history, return enriched items."""
    db = firestore.Client(project=PROJECT_ID)
    now = datetime.now(timezone.utc).isoformat()
    enriched = []

    for item in items:
        doc_id = make_item_id(item)
        doc_ref = db.collection(FIRESTORE_COLLECTION).document(doc_id)
        doc = doc_ref.get()

        snapshot = {
            "timestamp": now,
            "prior_state": item["prior_state"],
            "what_changed": item["what_changed"],
            "implementation_date": item["implementation_date"],
            "urgency": item["urgency"],
            "vendor_impact": item.get("vendor_impact", {"vendor_relevant": False}),
        }

        if doc.exists:
            data = doc.to_dict()
            history = data.get("history", [])
            history.append(snapshot)
            doc_ref.update({
                "last_seen": now,
                "history": history,
                "urgency": item["urgency"],
                "implementation_date": item["implementation_date"],
                "times_seen": data.get("times_seen", 1) + 1,
                "vendor_impact": item.get("vendor_impact", {"vendor_relevant": False}),
                "reporting_category": item.get("reporting_category", "Overig"),
                "source_url": item.get("source_url", data.get("source_url", "")),
            })
            item["_status"] = "confirmed_again"
            item["_first_detected"] = data.get("first_detected")
            item["_times_seen"] = data.get("times_seen", 1) + 1
        else:
            doc_ref.set({
                "body": item["body"],
                "topic": item["topic"],
                "first_detected": now,
                "last_seen": now,
                "urgency": item["urgency"],
                "implementation_date": item["implementation_date"],
                "source_url": item["source_url"],
                "times_seen": 1,
                "history": [snapshot],
                "vendor_impact": item.get("vendor_impact", {"vendor_relevant": False}),
                "reporting_category": item.get("reporting_category", "Overig"),
            })
            item["_status"] = "new"
            item["_first_detected"] = now
            item["_times_seen"] = 1

        enriched.append(item)

    return enriched


def get_full_changelog():
    """Pull all tracked items for the trail table in the email."""
    db = firestore.Client(project=PROJECT_ID)
    docs = db.collection(FIRESTORE_COLLECTION).order_by(
        "implementation_date"
    ).stream()
    return [doc.to_dict() for doc in docs]


def _none_if_unspecified(value):
    if not value or str(value).strip().lower() in ("not specified", "—", "n/a", ""):
        return None
    return value


def make_vendor_item_id(item):
    key = f"{item['body']}::{item['topic']}".lower().strip()
    return "vi_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def upsert_vendor_impact_items(scan_date, items):
    """Write/update vendor-relevant items into a dedicated Firestore collection
    matching the vendor coordination schema. Factual fields (title, dates,
    impact_summary, action_needed) are refreshed on every scan. Human-managed
    lifecycle fields (status, vendor_sla_date, test_window_start, go_live_date)
    are only set once on creation and never overwritten by the LLM afterwards.
    """
    db = firestore.Client(project=PROJECT_ID)
    now = datetime.now(timezone.utc).isoformat()

    for item in items:
        vi = item.get("vendor_impact", {}) or {}
        if not vi.get("vendor_relevant"):
            continue

        kd = vi.get("key_dates", {}) or {}
        doc_id = make_vendor_item_id(item)
        doc_ref = db.collection(VENDOR_COLLECTION).document(doc_id)
        doc = doc_ref.get()

        factual_fields = {
            "title": item["topic"],
            "source": item["body"],
            "source_url": item.get("source_url", ""),
            "reporting_category": item.get("reporting_category", "Overig"),
            "change_type": vi.get("change_type", "other"),
            "lead_time_class": (vi.get("lead_time_class") or "").lower(),
            "published_date": kd.get("publication_date") or item.get("publication_date"),
            "effective_date": _none_if_unspecified(kd.get("effective_date")),
            "reference_date": _none_if_unspecified(kd.get("first_reference_date")),
            "impact_summary": item.get("what_changed", ""),
            "action_needed": vi.get("action_needed", ""),
            "last_updated": now,
            "detected_in_digest": scan_date,
        }

        if doc.exists:
            # Never touch status / vendor_sla_date / test_window_start / go_live_date here
            doc_ref.update(factual_fields)
        else:
            doc_ref.set({
                "id": doc_id,
                **factual_fields,
                "status": "new",
                "vendor_sla_date": None,
                "test_window_start": None,
                "go_live_date": None,
            })


def get_vendor_impact_trail():
    """Full persistent vendor tracking table, including lifecycle status."""
    db = firestore.Client(project=PROJECT_ID)
    docs = db.collection(VENDOR_COLLECTION).order_by(
        "last_updated", direction=firestore.Query.DESCENDING
    ).stream()
    return [doc.to_dict() for doc in docs]


def render_last_mile_html(scan_result, enriched_items, changelog, vendor_trail, baselines):
    urgency_color = {"Immediate": "#d93025", "Near-term": "#f9a825", "Monitor": "#188038"}
    lead_time_color = {"short": "#188038", "medium": "#f9a825", "long": "#d93025"}
    status_color = {
        "new": "#0b5fff",
        "vendor_notified": "#f9a825",
        "vendor_confirmed": "#8e24aa",
        "testing": "#00838f",
        "parallel_run": "#6d4c41",
        "closed": "#757575",
    }

    def items_for_category(items, category):
        return [i for i in items if i.get("reporting_category") == category]

    def vendor_for_category(category):
        return [v for v in vendor_trail if v.get("reporting_category") == category]

    def build_blocks(categories):
        blocks = ""
        for category in categories:
            cat_findings = items_for_category(enriched_items, category)
            cat_vendor = vendor_for_category(category)
            baseline = baselines.get(category_slug(category), {})

            if not cat_findings and not cat_vendor:
                continue  # skip categories with nothing new and nothing tracked

            finding_rows = ""
            for item in cat_findings:
                badge = "NEW" if item["_status"] == "new" else f"seen {item['_times_seen']}x"
                color = urgency_color.get(item["urgency"], "#333")
                finding_rows += f"""
                <tr>
                  <td>{item['body']}</td>
                  <td><b>{item['topic']}</b><br><span style="color:#666;font-size:13px">{item['what_changed']}</span></td>
                  <td style="color:{color};font-weight:bold">{item['urgency']}</td>
                  <td>{item['implementation_date']}</td>
                  <td>{badge}</td>
                  <td><a href="{item['source_url']}">bron</a></td>
                </tr>"""

            vendor_rows = ""
            for v in cat_vendor:
                lt_color = lead_time_color.get(v.get("lead_time_class", ""), "#333")
                st_color = status_color.get(v.get("status", ""), "#333")
                status_link = f'{VENDOR_STATUS_URL}?id={v.get("id","")}'
                vendor_rows += f"""
                <tr>
                  <td><b>{v.get('title','')}</b><br><span style="color:#666;font-size:12px">{v.get('source','')}</span></td>
                  <td>{v.get('change_type','')}</td>
                  <td style="color:{lt_color};font-weight:bold">{v.get('lead_time_class','')}</td>
                  <td style="font-size:12px">
                    pub: {v.get('published_date') or '—'}<br>
                    eff: {v.get('effective_date') or '—'}<br>
                    ref: {v.get('reference_date') or '—'}
                  </td>
                  <td style="color:{st_color};font-weight:bold">{v.get('status','new')}</td>
                  <td>{v.get('action_needed','')}</td>
                  <td><a href="{status_link}">status bijwerken</a></td>
                </tr>"""

            baseline_note = ""
            if baseline:
                baseline_note = f"""
                <p style="font-size:12px;color:#888;margin:4px 0 12px 0;">
                  Baseline vastgesteld op {baseline.get('as_of_date','')}: {baseline.get('baseline_summary','')}
                </p>"""

            blocks += f"""
            <h3 style="color:#0b3d91;border-bottom:1px solid #ddd;padding-bottom:4px;margin-top:28px;">{category}</h3>
            {baseline_note}
            {'<table><tr><th>Body</th><th>Onderwerp</th><th>Urgentie</th><th>Implementatiedatum</th><th>Status</th><th>Bron</th></tr>' + finding_rows + '</table>' if finding_rows else '<p style="font-size:13px;color:#888;">Geen nieuwe wijzigingen in deze categorie.</p>'}
            {'<table><tr><th>Vendor item</th><th>Change type</th><th>Lead time</th><th>Key dates</th><th>Status</th><th>Actie nodig</th><th>Bijwerken</th></tr>' + vendor_rows + '</table>' if vendor_rows else ''}
            """
        return blocks

    dnb_blocks = build_blocks(DNB_CATEGORIES)
    eba_blocks = build_blocks(EBA_CATEGORIES)

    trail_rows = ""
    for entry in changelog:
        color = urgency_color.get(entry.get("urgency", ""), "#333")
        src_url = entry.get("source_url", "")
        src_link = f'<a href="{src_url}">bron</a>' if src_url else "—"
        trail_rows += f"""
        <tr>
          <td>{entry.get('body','')}</td>
          <td>{entry.get('reporting_category','')}</td>
          <td>{entry.get('topic','')}</td>
          <td style="color:{color}">{entry.get('urgency','')}</td>
          <td>{entry.get('implementation_date','')}</td>
          <td>{entry.get('first_detected','')[:10]}</td>
          <td>{entry.get('times_seen','')}</td>
          <td>{src_link}</td>
        </tr>"""

    return f"""
    <html><head><style>
      body {{ font-family: Arial, sans-serif; color:#1a1a1a; max-width:900px; margin:0 auto; padding:20px; }}
      h2 {{ color:#0b3d91; border-bottom:2px solid #eee; padding-bottom:6px; }}
      table {{ border-collapse:collapse; width:100%; margin:8px 0 16px 0; }}
      th,td {{ border:1px solid #ddd; padding:8px 10px; font-size:13px; text-align:left; vertical-align:top; }}
      th {{ background:#f4f6f8; }}
    </style></head><body>
      <p><b>Last-Mile Regulatory Horizon Scan</b> — {scan_result['scan_date']}</p>
      <p>{scan_result['period_verdict']}</p>

      <h2>Bevindingen per DNB-rapportagecategorie</h2>
      <p style="font-size:13px;color:#666;">Indeling volgt DNB's eigen Statistical Reporting structuur (dnb.nl/login/dlr/statistical-reporting/banks). Alleen categorieën met nieuwe bevindingen of getrackte vendor-items worden getoond.</p>
      {dnb_blocks or '<p>Geen bevindingen in deze scan.</p>'}

      <h2>Bevindingen per EBA-rapportagecategorie (Reporting Framework 4.2)</h2>
      <p style="font-size:13px;color:#666;">Indeling volgt de areas of reporting / modules van EBA Reporting Framework 4.2 (eba.europa.eu/risk-and-data-analysis/reporting-frameworks/reporting-framework-42). Alleen categorieën met nieuwe bevindingen of getrackte vendor-items worden getoond.</p>
      {eba_blocks or '<p>Geen bevindingen in deze scan.</p>'}

      <h2>Volledige trail — alle gevolgde items</h2>
      <table>
        <tr><th>Body</th><th>Categorie</th><th>Onderwerp</th><th>Urgentie</th><th>Implementatiedatum</th><th>Eerst gezien</th><th>Keer bevestigd</th><th>Bron</th></tr>
        {trail_rows or '<tr><td colspan="8">Nog geen items in de trail.</td></tr>'}
      </table>
      {build_feedback_footer("last_mile")}
    </body></html>
    """


def send_html_email(html_body, subject):
    creds = Credentials(
        token=None,
        refresh_token=get_secret("gmail-refresh-token"),
        client_id=get_secret("gmail-client-id"),
        client_secret=get_secret("gmail-client-secret"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    service = build("gmail", "v1", credentials=creds)
    message = MIMEText(html_body, "html")
    message["to"] = TO_EMAIL
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def last_mile_digest(request):
    scan_result = get_last_mile_findings()
    enriched_items = update_tracked_items(scan_result["items"])
    upsert_vendor_impact_items(scan_result["scan_date"], enriched_items)
    changelog = get_full_changelog()
    vendor_trail = get_vendor_impact_trail()
    baselines = get_all_baselines()
    html = render_last_mile_html(scan_result, enriched_items, changelog, vendor_trail, baselines)
    send_html_email(html, "Last-Mile Regulatory Horizon Scan")
    return "OK", 200


# ---------------------------------------------------------------------------
# Feedback capture — public HTTP form linked from email footers
# ---------------------------------------------------------------------------

def render_feedback_form(report_type, submitted=False):
    if submitted:
        return """
        <html><body style="font-family:Arial,sans-serif;max-width:500px;margin:60px auto;text-align:center;">
          <h2>Bedankt voor je feedback</h2>
          <p>Dit wordt meegenomen in de volgende scan.</p>
        </body></html>
        """
    return f"""
    <html><body style="font-family:Arial,sans-serif;max-width:500px;margin:60px auto;">
      <h2>Feedback op regulatory rapport</h2>
      <form method="POST" action="{FEEDBACK_FORM_URL}">
        <input type="hidden" name="report_type" value="{report_type}">
        <label>Type feedback</label><br>
        <select name="category" style="width:100%;padding:8px;margin:8px 0;">
          <option value="missing_content">Content ontbreekt (wel online te vinden)</option>
          <option value="incorrect">Informatie is onjuist</option>
          <option value="unclear">Onduidelijke opmaak/uitleg</option>
          <option value="other">Anders</option>
        </select><br>
        <label>Toelichting</label><br>
        <textarea name="comment" rows="5" style="width:100%;padding:8px;margin:8px 0;"
          placeholder="Bijv. 'EBA Q&amp;A 2026_xxxx over NSFR ontbrak, is 3 dagen geleden gepubliceerd'"></textarea><br>
        <button type="submit" style="padding:10px 20px;background:#0b5fff;color:white;border:none;border-radius:4px;">
          Versturen
        </button>
      </form>
    </body></html>
    """


def report_feedback(request):
    if request.method == "GET":
        report_type = request.args.get("report", "weekly")
        return render_feedback_form(report_type), 200, {"Content-Type": "text/html"}

    if request.method == "POST":
        report_type = request.form.get("report_type", "weekly")
        category = request.form.get("category", "other")
        comment = request.form.get("comment", "").strip()

        db = firestore.Client(project=PROJECT_ID)
        db.collection(FEEDBACK_COLLECTION).add({
            "report_type": report_type,
            "category": category,
            "comment": comment,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "times_injected": 0,
            "addressed": False,
        })
        return render_feedback_form(report_type, submitted=True), 200, {"Content-Type": "text/html"}

    return "Method not allowed", 405


# ---------------------------------------------------------------------------
# Vendor status update — public HTTP form linked from vendor-impact table rows
# ---------------------------------------------------------------------------

VENDOR_STATUSES = ["new", "vendor_notified", "vendor_confirmed", "testing", "parallel_run", "closed"]


def render_vendor_status_form(item, submitted=False):
    if submitted:
        return f"""
        <html><body style="font-family:Arial,sans-serif;max-width:500px;margin:60px auto;text-align:center;">
          <h2>Status bijgewerkt</h2>
          <p>{item.get('title','') if item else ''} staat nu op status
             <b>{item.get('status','') if item else ''}</b>.</p>
        </body></html>
        """
    if not item:
        return """
        <html><body style="font-family:Arial,sans-serif;max-width:500px;margin:60px auto;text-align:center;">
          <h2>Item niet gevonden</h2>
          <p>Deze link verwijst naar een item dat niet (meer) bestaat in Firestore.</p>
        </body></html>
        """
    options = "".join(
        f'<option value="{s}"{" selected" if s == item.get("status") else ""}>{s}</option>'
        for s in VENDOR_STATUSES
    )
    return f"""
    <html><body style="font-family:Arial,sans-serif;max-width:520px;margin:60px auto;">
      <h2>Status bijwerken</h2>
      <p><b>{item.get('title','')}</b><br><span style="color:#666;font-size:13px">{item.get('source','')} — {item.get('change_type','')}</span></p>
      <form method="POST" action="{VENDOR_STATUS_URL}">
        <input type="hidden" name="id" value="{item.get('id','')}">
        <label>Status</label><br>
        <select name="status" style="width:100%;padding:8px;margin:8px 0;">{options}</select><br>
        <label>Vendor SLA-datum (optioneel, laat leeg om ongewijzigd te laten)</label><br>
        <input type="date" name="vendor_sla_date" value="{item.get('vendor_sla_date') or ''}" style="padding:8px;margin:8px 0;width:100%;"><br>
        <label>Test window start (optioneel)</label><br>
        <input type="date" name="test_window_start" value="{item.get('test_window_start') or ''}" style="padding:8px;margin:8px 0;width:100%;"><br>
        <label>Go-live datum (optioneel)</label><br>
        <input type="date" name="go_live_date" value="{item.get('go_live_date') or ''}" style="padding:8px;margin:8px 0;width:100%;"><br>
        <button type="submit" style="padding:10px 20px;background:#0b5fff;color:white;border:none;border-radius:4px;margin-top:8px;">
          Opslaan
        </button>
      </form>
    </body></html>
    """


def vendor_status_update(request):
    db = firestore.Client(project=PROJECT_ID)

    if request.method == "GET":
        item_id = request.args.get("id", "")
        doc = db.collection(VENDOR_COLLECTION).document(item_id).get()
        item = doc.to_dict() if doc.exists else None
        return render_vendor_status_form(item), 200, {"Content-Type": "text/html"}

    if request.method == "POST":
        item_id = request.form.get("id", "")
        doc_ref = db.collection(VENDOR_COLLECTION).document(item_id)
        if not doc_ref.get().exists:
            return render_vendor_status_form(None), 404, {"Content-Type": "text/html"}

        update_fields = {
            "status": request.form.get("status", "new"),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        for date_field in ("vendor_sla_date", "test_window_start", "go_live_date"):
            value = request.form.get(date_field, "").strip()
            if value:
                update_fields[date_field] = value

        doc_ref.update(update_fields)
        updated_item = doc_ref.get().to_dict()
        return render_vendor_status_form(updated_item, submitted=True), 200, {"Content-Type": "text/html"}

    return "Method not allowed", 405

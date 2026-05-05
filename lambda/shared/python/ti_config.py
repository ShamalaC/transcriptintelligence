"""
Runtime configuration for all Lambda functions -- deployed via the shared layer.
Edit here; every Lambda picks up the change on the next deploy.
"""

# --- Vendor identity ---
# Emails/names from these domains are intentionally preserved (not redacted).
VENDOR_DOMAINS: set[str] = {"aegiscloud.com", "aegis.com"}
VENDOR_NAMES:   set[str] = {"aegis", "aegis cloud", "aegis cloud security"}

# --- S3 ---
RAW_BUCKET_NAME = "call-transcript-root"

# --- PII detection patterns ---
# Each entry is (regex_pattern, entity_type_label).
# Vendor domains are excluded at match time via negative lookahead in the EMAIL pattern.
# NOTE: For production NER coverage (names, addresses, company names) integrate
#       Microsoft Presidio -- regex alone cannot reliably detect those entities.
PII_PATTERNS: list[tuple[str, str]] = [
    # Structured identifiers
    (
        r"\b[A-Za-z0-9._%+-]+@(?!aegiscloud\.com)(?!aegis\.com)"
        r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        "EMAIL",
    ),
    (r"\b(\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", "PHONE"),
    (r"\b\d{3}-\d{2}-\d{4}\b", "SSN"),
    # Visa / Mastercard / Amex / Discover
    (
        r"\b(?:4[0-9]{12}(?:[0-9]{3})?"
        r"|5[1-5][0-9]{14}"
        r"|3[47][0-9]{13}"
        r"|6(?:011|5[0-9]{2})[0-9]{12})\b",
        "CREDIT_CARD",
    ),
    # Network / infrastructure
    (
        r"\b(?:25[0-5]|2[0-4]\d|[01]?\d\d?)"
        r"\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)"
        r"\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)"
        r"\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
        "IP_ADDRESS",
    ),
    # Credentials / secrets
    (r"\bAKIA[0-9A-Z]{16}\b",                            "AWS_ACCESS_KEY"),
    (r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b",                  "API_KEY"),
    (r"\bghp_[A-Za-z0-9]{36}\b",                         "API_KEY"),
    (r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]{20,}=*\b",     "AUTH_TOKEN"),
    # Government IDs
    (r"\b[A-Z]{1,2}[0-9]{7,9}\b",                        "PASSPORT_NO"),
]

# --- Call-type classifier patterns ---
CALL_TYPE_SUPPORT_PREFIX  = r"^Support Case"
CALL_TYPE_EXTERNAL_PREFIX = r"^Aegis /"

# --- Topic taxonomy (Business Moment / Call Purpose — 9 categories) ---
# L1: title regex — checked first; highest precision.
# L2/L3: TAXONOMY keyword lists — fallback when no L1 pattern matches.
CATEGORY_TITLE_PATTERNS: list[tuple[str, str]] = [
    (r"INCIDENT:|URGENT:|War Room|Pipeline Failure|Escalation Bridge|"
     r"Detect Outage\s*-|Customer Impact Assessment|Remediation Plan|"
     r"Complete Loss of|Cobalt Software - Aegis Detect",           "Incident Response"),
    (r"Win/Loss|Competitive (?:Threat Assessment|Landscape|Response|Evaluation)",
                                                                   "Competitive Intelligence"),
    (r"SOC 2|ISO 27001|HIPAA|PCI|Audit Prep|Audit - Internal|SOC 2 Type",
                                                                   "Compliance & Audit"),
    (r"^Support Case",                                             "Technical Support"),
    (r"Onboarding Kickoff|Deployment Kickoff|Identity Module Setup|Comply v2 Deployment",
                                                                   "Onboarding & Deployment"),
    (r"Multi-Year Renewal|Contract (?:Discussion|Review)|"
     r"Renewal (?:Confirmation|Concerns|Discussion)|Q\d+ Planning","Commercial & Renewal"),
    (r"Business Review|Account Review|Account Recovery|"
     r"Service Reliability Discussion|Platform Concerns|"
     r"Post-Incident Review|Detect (?:Recovery|Reliability)|Account Health",
                                                                   "Account Health & QBR"),
    (r"Product Feedback|Roadmap Review|Early Access",              "Product Feedback & Roadmap"),
    (r"Standup|All Hands|Sprint Planning|Launch Readiness|"
     r"Root Cause Analysis|30-Day Review|Architecture Review|"
     r"Post-Mortem|Win/Loss Analysis|Reliability.*Review",         "Internal Planning & Engineering"),
]
TAXONOMY: dict[str, list[str]] = {
    "Incident Response": [
        "outage", "incident", "p0", "war room", "customer impact",
        "remediation", "root cause", "pipeline failure", "blast radius",
        "critical failure", "complete loss", "service disruption",
    ],
    "Technical Support": [
        "support case", "bug", "latency", "timeout", "crash",
        "performance", "degradation", "error", "api", "restore",
        "backup", "recovery", "slow", "intermittent",
    ],
    "Compliance & Audit": [
        "compliance", "soc 2", "soc2", "iso 27001", "hipaa", "pci",
        "audit", "regulatory", "gdpr", "certification", "comply",
    ],
    "Commercial & Renewal": [
        "renewal", "contract", "pricing", "billing", "license",
        "overage", "invoice", "discount", "seat", "multi-year", "upsell", "payment",
    ],
    "Account Health & QBR": [
        "business review", "qbr", "account health", "account review",
        "post-incident review", "service reliability", "platform concerns",
        "churn", "retention", "at-risk", "escalation", "competitive",
    ],
    "Competitive Intelligence": [
        "win/loss", "competitive", "competitor", "vendor comparison",
        "evaluation", "market", "landscape",
    ],
    "Product Feedback & Roadmap": [
        "roadmap", "feature", "product feedback", "early access",
        "enhancement", "request", "wishlist", "gap",
    ],
    "Onboarding & Deployment": [
        "onboarding", "deployment", "kickoff", "setup", "implementation",
        "migration", "integration", "configuration", "identity module",
        "saml", "sso", "provisioning", "scim",
    ],
    "Internal Planning & Engineering": [
        "sprint", "standup", "all hands", "planning", "retro",
        "architecture", "launch", "backlog", "post-mortem",
        "root cause analysis", "engineering",
    ],
}

# --- Sentiment baselines (per call type) ---
CALL_TYPE_BASELINES: dict[str, float] = {
    "support":  2.94,
    "external": 4.21,
    "internal": 3.42,
}
SENTIMENT_STD_DEV             = 0.8
SENTIMENT_ANOMALY_Z_THRESHOLD = 1.5

# --- Escalation routing rules ---
# Key: (category, severity) -> owning team
ESCALATION_RULES: dict[tuple[str, str], str] = {
    ("Incident Response",    "p0"): "engineering",
    ("Incident Response",    "p1"): "engineering",
    ("Technical Support",    "p0"): "engineering",
    ("Compliance & Audit",   "p0"): "security",
    ("Account Health & QBR", "p1"): "account",
    ("Commercial & Renewal", "p1"): "cs",
}

# Severity levels that trigger PagerDuty
PAGERDUTY_SEVERITIES: set[str] = {"p0", "p1"}

# --- Issue severity patterns ---
SEVERITY_PATTERNS: dict[str, list[str]] = {
    "p0": ["outage", "down", "critical", "breach", "data loss", "zero visibility"],
    "p1": ["degraded", "failing", "blocked", "urgent", "escalat", "sev1"],
    "p2": ["slow", "intermittent", "error", "bug", "discrepancy", "overage"],
    "p3": ["question", "inquiry", "request", "minor", "clarif"],
}

# --- Agent / orchestrator loop ---
AGENTIC_LOOP_MAX_ITERATIONS = 10
ORCHESTRATOR_MAX_TOKENS     = 2048

# --- Ingestion truncation limits ---
INGESTION_SUMMARY_MAX_CHARS = 500
INGESTION_KEY_MOMENTS_MAX   = 5

# --- Chatbot retrieval ---
CHATBOT_TOP_K      = 5
CHATBOT_MAX_TOKENS = 400

# --- DynamoDB index names ---
TABLE_RECORDS_GSI = "call-type-index"

# --- Bedrock inference pricing (USD per 1M tokens, on-demand, us-east-1) ---
# Source: https://aws.amazon.com/bedrock/pricing/ — update when pricing changes.
BEDROCK_PRICING: dict[str, dict[str, float]] = {
    "us.anthropic.claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00},
    "us.anthropic.claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
    "us.anthropic.claude-opus-4-7":           {"input": 15.00, "output": 75.00},
}
BEDROCK_PRICING_DEFAULT = {"input": 3.00, "output": 15.00}

# --- Eval thresholds ---
EVAL_PASS_THRESHOLD = 0.90  # minimum accuracy fraction to pass CI regression gate

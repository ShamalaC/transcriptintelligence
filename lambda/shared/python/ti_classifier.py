"""
Shared call-type and category classifier -- single source of truth.
Patterns are defined in ti_config so they can be changed without touching logic.
"""
import re
from ti_config import (
    CALL_TYPE_SUPPORT_PREFIX, CALL_TYPE_EXTERNAL_PREFIX,
    CATEGORY_TITLE_PATTERNS, TAXONOMY,
)

CALL_TYPES = ("support", "external", "internal")

_COMPILED_TITLE_PATTERNS = [
    (re.compile(pat, re.IGNORECASE), cat)
    for pat, cat in CATEGORY_TITLE_PATTERNS
]


def classify_call_type(title: str) -> str:
    """Deterministic, zero-LLM call-type classifier based on title pattern."""
    if re.match(CALL_TYPE_SUPPORT_PREFIX, title or ""):
        return "support"
    if re.match(CALL_TYPE_EXTERNAL_PREFIX, title or ""):
        return "external"
    return "internal"


def classify_category(title: str, topics: list) -> str:
    """3-level category classifier matching the notebook's approach.

    L1: title regex (highest precision — checked first)
    L2: primary topic text match against TAXONOMY keywords
    L3: full topic string keyword scan (broadest fallback)
    """
    # L1 — title regex
    for pattern, cat in _COMPILED_TITLE_PATTERNS:
        if pattern.search(title or ""):
            return cat

    # L2 — primary topic (first topic only, exact keyword match)
    primary = (topics[0] if topics else "").lower()
    for cat, keywords in TAXONOMY.items():
        if any(kw in primary for kw in keywords):
            return cat

    # L3 — all topics combined, keyword scan
    topic_str = " ".join(topics).lower()
    for cat, keywords in TAXONOMY.items():
        if any(kw in topic_str for kw in keywords):
            return cat

    return "Other"

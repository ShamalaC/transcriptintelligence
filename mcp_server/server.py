#!/usr/bin/env python3
"""
Transcript Intelligence MCP Server.

Exposes transcript analysis data via the Model Context Protocol so any
MCP-compatible client (Claude Desktop, Cursor, Zed, etc.) can query the TI
platform using natural language.

Tools exposed:
  - search_transcripts     BM25 keyword search over all meeting records
  - get_meeting_insights   Full analysis record for a specific meeting
  - get_sentiment_trends   Aggregate sentiment by call type or date bucket
  - get_anomalies          List meetings flagged as sentiment anomalies

Setup:
    pip install -r mcp_server/requirements.txt
    AWS_PROFILE=<profile> RECORDS_TABLE=ti-records python mcp_server/server.py

Add to Claude Desktop config (~/.config/claude/claude_desktop_config.json):
    {
      "mcpServers": {
        "transcript-intelligence": {
          "command": "python",
          "args": ["/path/to/mcp_server/server.py"],
          "env": {"RECORDS_TABLE": "ti-records", "AWS_REGION": "us-east-1"}
        }
      }
    }
"""
import os, json, re, math
from collections import defaultdict
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
RECORDS_TABLE = os.environ.get("RECORDS_TABLE", "ti-records")
AWS_REGION    = os.environ.get("AWS_REGION", "us-east-1")
TOP_K         = int(os.environ.get("MCP_TOP_K", "8"))

ddb   = boto3.resource("dynamodb", region_name=AWS_REGION)
table = ddb.Table(RECORDS_TABLE)
mcp   = FastMCP("transcript-intelligence")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _float(v) -> float:
    return float(v) if isinstance(v, Decimal) else v


def _scan_all() -> list[dict]:
    items, last = [], None
    while True:
        kwargs = {}
        if last:
            kwargs["ExclusiveStartKey"] = last
        resp  = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
    return items


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def _bm25(query: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    tokenized = [_tokenize(d) for d in docs]
    if not tokenized:
        return []
    avgdl = sum(len(d) for d in tokenized) / len(tokenized)
    df: dict[str, int] = defaultdict(int)
    for doc in tokenized:
        for term in set(doc):
            df[term] += 1
    N       = len(docs)
    q_terms = _tokenize(query)
    scores  = []
    for doc in tokenized:
        score  = 0.0
        tf_map = defaultdict(int)
        for term in doc:
            tf_map[term] += 1
        for term in q_terms:
            tf  = tf_map.get(term, 0)
            idf = math.log((N - df[term] + 0.5) / (df[term] + 0.5) + 1)
            score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * len(doc) / avgdl))
        scores.append(score)
    return scores


def _record_to_doc(r: dict) -> str:
    issues_text = " ".join(
        i.get("title", "") for i in (r.get("issues") or [])[:3]
    )
    return " ".join(filter(None, [
        r.get("title", ""),
        r.get("call_type", ""),
        r.get("category", ""),
        r.get("sentiment_label", ""),
        issues_text,
    ]))


def _safe_record(r: dict) -> dict:
    """Convert Decimal values for JSON serialization."""
    def _conv(v):
        if isinstance(v, Decimal): return float(v)
        if isinstance(v, dict):    return {k: _conv(vv) for k, vv in v.items()}
        if isinstance(v, list):    return [_conv(i) for i in v]
        return v
    return _conv(r)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def search_transcripts(query: str, limit: int = 5) -> str:
    """
    Search meeting transcripts using BM25 keyword ranking.

    Args:
        query: Natural language search query (e.g. "login failures support tickets")
        limit: Maximum number of results to return (default 5, max 20)

    Returns:
        JSON list of matching meeting records ranked by relevance, including
        meeting_id, title, call_type, category, sentiment, and top issues.
    """
    limit   = min(max(1, limit), 20)
    records = _scan_all()
    if not records:
        return json.dumps({"results": [], "total_searched": 0})

    docs    = [_record_to_doc(r) for r in records]
    scores  = _bm25(query, docs)
    ranked  = sorted(zip(scores, records), key=lambda x: -x[0])
    hits    = [
        {
            "relevance_score": round(float(s), 3),
            "meeting_id":      r.get("meeting_id"),
            "title":           r.get("title"),
            "call_type":       r.get("call_type"),
            "category":        r.get("category"),
            "sentiment_label": r.get("sentiment_label"),
            "sentiment_score": _float(r.get("sentiment_score", 0)),
            "is_anomaly":      r.get("is_anomaly", False),
            "n_issues":        r.get("n_issues", 0),
            "top_issue":       (r.get("issues") or [{}])[0].get("title", ""),
            "processed_at":    r.get("processed_at"),
        }
        for s, r in ranked[:limit] if s > 0
    ]
    return json.dumps({"results": hits, "total_searched": len(records)}, default=str)


@mcp.tool()
def get_meeting_insights(meeting_id: str) -> str:
    """
    Retrieve the full analysis record for a specific meeting.

    Args:
        meeting_id: The unique meeting identifier (e.g. "01KQ7F58A2E07B857C24624D")

    Returns:
        Complete analysis including call type, category, sentiment analysis,
        extracted issues with severity, escalation routing, and PII-safe title.
    """
    resp = table.get_item(Key={"meeting_id": meeting_id})
    item = resp.get("Item")
    if not item:
        return json.dumps({"error": f"Meeting {meeting_id!r} not found"})
    return json.dumps(_safe_record(item), default=str, indent=2)


@mcp.tool()
def get_sentiment_trends(call_type: str = "", days: int = 30) -> str:
    """
    Aggregate sentiment statistics across meetings, optionally filtered by call type.

    Args:
        call_type: Filter by call type — "support", "external", "internal", or "" for all
        days:      Number of recent days to include (default 30, 0 = all time)

    Returns:
        JSON with average sentiment score, anomaly rate, score distribution,
        and count per sentiment label. Useful for spotting call type degradation trends.
    """
    from datetime import datetime, timezone, timedelta

    records = _scan_all()

    if call_type:
        records = [r for r in records if r.get("call_type") == call_type]

    if days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        records = [r for r in records if r.get("processed_at", "") >= cutoff]

    if not records:
        return json.dumps({"error": "No records found for the given filters",
                           "call_type": call_type, "days": days})

    scores    = [_float(r.get("sentiment_score", 0)) for r in records]
    anomalies = sum(1 for r in records if r.get("is_anomaly"))
    label_dist: dict[str, int] = defaultdict(int)
    for r in records:
        label_dist[r.get("sentiment_label", "unknown")] += 1

    return json.dumps({
        "call_type":      call_type or "all",
        "period_days":    days or "all_time",
        "n_meetings":     len(records),
        "avg_sentiment":  round(sum(scores) / len(scores), 3),
        "min_sentiment":  round(min(scores), 3),
        "max_sentiment":  round(max(scores), 3),
        "anomaly_count":  anomalies,
        "anomaly_rate":   round(anomalies / len(records), 3),
        "label_distribution": dict(label_dist),
    })


@mcp.tool()
def get_anomalies(min_z_score: float = 1.5, call_type: str = "") -> str:
    """
    List meetings flagged as sentiment anomalies, sorted by z-score magnitude.

    Args:
        min_z_score: Minimum absolute z-score to include (default 1.5)
        call_type:   Filter by call type — "support", "external", "internal", or "" for all

    Returns:
        JSON list of anomalous meetings with title, call type, sentiment score,
        z-score, and escalation routing. High-magnitude anomalies often signal
        at-risk accounts or unreported incidents.
    """
    records = _scan_all()
    anomalies = [
        r for r in records
        if r.get("is_anomaly") and abs(_float(r.get("z_score", 0))) >= min_z_score
        and (not call_type or r.get("call_type") == call_type)
    ]
    anomalies.sort(key=lambda r: abs(_float(r.get("z_score", 0))), reverse=True)

    results = [
        {
            "meeting_id":      r.get("meeting_id"),
            "title":           r.get("title"),
            "call_type":       r.get("call_type"),
            "category":        r.get("category"),
            "sentiment_score": _float(r.get("sentiment_score", 0)),
            "z_score":         _float(r.get("z_score", 0)),
            "sentiment_label": r.get("sentiment_label"),
            "escalation_team": r.get("escalation_team"),
            "processed_at":    r.get("processed_at"),
        }
        for r in anomalies
    ]
    return json.dumps({"anomalies": results, "count": len(results)}, default=str)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()

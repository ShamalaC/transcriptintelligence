"""
Dashboard Intelligence Agent — dashboard knowledge + BM25 record retrieval + Claude.

Loads pre-computed aggregates (KPIs, distribution, anomalies, escalations) from
DynamoDB on every request and passes them to Claude alongside BM25-retrieved records.
"""
import json, os, re, math, urllib.request, urllib.error, boto3
from collections import defaultdict
from decimal import Decimal

from ti_config import CHATBOT_TOP_K, TABLE_RECORDS_GSI
from ti_classifier import CALL_TYPES

ddb = boto3.resource("dynamodb")

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL    = "claude-haiku-4-5-20251001"
RECORDS_TABLE      = os.environ["RECORDS_TABLE"]
CHATBOT_MAX_TOKENS = 600

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "resources", "answer_prompt.txt")) as _f:
    ANSWER_PROMPT = _f.read()

_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
}


# ── Dashboard knowledge loading ───────────────────────────────────────────────

def _scan_all(table, projection: str, filter_expr=None, expr_names=None, expr_values=None) -> list:
    items, last = [], None
    while True:
        kwargs: dict = {"ProjectionExpression": projection}
        if filter_expr:  kwargs["FilterExpression"]         = filter_expr
        if expr_names:   kwargs["ExpressionAttributeNames"] = expr_names
        if expr_values:  kwargs["ExpressionAttributeValues"] = expr_values
        if last:         kwargs["ExclusiveStartKey"]         = last
        resp  = table.scan(**kwargs)
        items.extend(resp["Items"])
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
    return items


def _kpis(table) -> dict:
    items = _scan_all(
        table,
        "sentiment_score, is_anomaly, escalation_priority, feature_gap",
    )
    total        = len(items)
    anomalies    = sum(1 for i in items if i.get("is_anomaly"))
    escalated    = sum(1 for i in items if i.get("escalation_priority") in ("p0", "p1"))
    feature_gaps = sum(1 for i in items if i.get("feature_gap"))
    scores       = [float(i["sentiment_score"]) for i in items if i.get("sentiment_score")]
    avg_sent     = round(sum(scores) / len(scores), 2) if scores else 0.0
    return {
        "total": total, "anomalies": anomalies, "avg_sentiment": avg_sent,
        "escalated": escalated, "feature_gaps": feature_gaps,
    }


def _distribution(table) -> dict:
    dist = {}
    for ct in CALL_TYPES:
        resp = table.query(
            IndexName=TABLE_RECORDS_GSI,
            KeyConditionExpression=boto3.dynamodb.conditions.Key("call_type").eq(ct),
            Select="COUNT",
        )
        dist[ct] = resp["Count"]
    return dist


def _anomalies_brief(table) -> list:
    from boto3.dynamodb.conditions import Attr
    items = _scan_all(
        table,
        "meeting_id, title, account_name, call_type, category, "
        "sentiment_score, sentiment_label, z_score, escalation_priority, escalation_team, issues, #mo",
        filter_expr="is_anomaly = :t",
        expr_names={"#mo": "month"},
        expr_values={":t": True},
    )
    result = []
    for i in sorted(items, key=lambda x: float(x.get("z_score", 0))):
        score  = float(i.get("sentiment_score", 0))
        z      = float(i.get("z_score", 0))
        issues = i.get("issues", [])
        top    = issues[0] if issues else {}
        account = i.get("account_name", "") or _acct(i.get("title", ""))
        evidence = f"Sentiment {score:.1f}/5 ({i.get('sentiment_label','')}) — z={z:+.2f}σ from {i.get('call_type','')} baseline"
        if top:
            evidence += f". {top.get('severity','').upper()}: {top.get('title','')}"
        result.append({
            "account":   account or i.get("title", "")[:40],
            "call_type": i.get("call_type", ""),
            "category":  i.get("category", ""),
            "priority":  i.get("escalation_priority", "—"),
            "team":      i.get("escalation_team", ""),
            "month":     i.get("month", ""),
            "evidence":  evidence,
        })
    return result


def _escalations_brief(table) -> list:
    items = _scan_all(
        table,
        "meeting_id, title, account_name, call_type, category, "
        "sentiment_score, z_score, escalation_priority, escalation_team, has_pagerduty, issues, #mo",
        filter_expr="escalation_priority IN (:p0, :p1)",
        expr_names={"#mo": "month"},
        expr_values={":p0": "p0", ":p1": "p1"},
    )
    priority_order = {"p0": 0, "p1": 1}
    result = []
    for i in sorted(items, key=lambda x: (priority_order.get(x.get("escalation_priority", "p3"), 9),
                                           -float(x.get("z_score", 0)))):
        score  = float(i.get("sentiment_score", 0))
        z      = float(i.get("z_score", 0))
        issues = i.get("issues", [])
        pri    = i.get("escalation_priority", "")
        team   = i.get("escalation_team", "")
        pd     = i.get("has_pagerduty", False)
        top    = issues[0] if issues else {}
        account = i.get("account_name", "") or _acct(i.get("title", ""))
        reason_parts = []
        if top:
            reason_parts.append(f"{top.get('severity','').upper()}: \"{top.get('title','')}\"")
        if abs(z) > 1.5:
            direction = "below" if z < 0 else "above"
            reason_parts.append(f"sentiment {score:.1f}/5 ({z:+.2f}σ {direction} baseline)")
        if not reason_parts:
            reason_parts.append(f"category {i.get('category','')} matched escalation rule")
        reasoning = ". ".join(reason_parts) + f" → {team} team"
        if pd:
            reasoning += " (PagerDuty fired)"
        result.append({
            "account":   account or i.get("title", "")[:40],
            "call_type": i.get("call_type", ""),
            "category":  i.get("category", ""),
            "priority":  pri,
            "team":      team,
            "pagerduty": pd,
            "month":     i.get("month", ""),
            "reasoning": reasoning,
        })
    return result


def _acct(title: str) -> str:
    m = re.match(r"^Aegis\s*/\s*(.+?)(?:\s+-\s+.+)?$", title or "", re.IGNORECASE)
    return m.group(1).strip() if m else ""


def load_dashboard_knowledge(table) -> str:
    """Build a structured knowledge document from all dashboard analytics."""
    kpis   = _kpis(table)
    dist   = _distribution(table)
    total  = sum(dist.values())
    anoms  = _anomalies_brief(table)
    escs   = _escalations_brief(table)

    lines = [
        "=== AEGIS CLOUD SECURITY — CALL INTELLIGENCE DASHBOARD ===",
        "",
        "── KPI SUMMARY ──",
        f"Total Meetings Processed:  {kpis['total']}",
        f"Anomalies Flagged:         {kpis['anomalies']} ({round(kpis['anomalies']/kpis['total']*100)}% of all meetings)",
        f"Average Sentiment Score:   {kpis['avg_sentiment']}/5.0",
        f"Meetings Escalated P0/P1:  {kpis['escalated']} ({round(kpis['escalated']/kpis['total']*100)}% of all meetings)",
        f"Feature Gaps Identified:   {kpis['feature_gaps']}",
        "",
        "── CALL TYPE DISTRIBUTION ──",
    ]
    for ct, count in dist.items():
        pct = round(count / total * 100, 1) if total else 0
        label = {"support": "Support Cases", "external": "External (Customer)", "internal": "Internal"}.get(ct, ct)
        lines.append(f"  {label}: {count} meetings ({pct}%)")

    lines += ["", f"── ANOMALY ACCOUNTS ({len(anoms)} flagged — sentiment z-score > 1.5σ from call-type baseline) ──"]
    for a in anoms[:20]:
        lines.append(f"  [{a['priority'].upper()}] {a['account']} | {a['call_type']} | {a['category']} | {a['month']} | {a['evidence']}")

    p0_escs = [e for e in escs if e["priority"] == "p0"]
    p1_escs = [e for e in escs if e["priority"] == "p1"]
    lines += ["", f"── ESCALATED MEETINGS — P0 CRITICAL ({len(p0_escs)}) ──"]
    for e in p0_escs[:15]:
        pd = " [PagerDuty fired]" if e["pagerduty"] else ""
        lines.append(f"  {e['account']} | {e['category']} | {e['month']} | {e['reasoning']}{pd}")

    lines += ["", f"── ESCALATED MEETINGS — P1 HIGH ({len(p1_escs)}) ──"]
    for e in p1_escs[:15]:
        pd = " [PagerDuty fired]" if e["pagerduty"] else ""
        lines.append(f"  {e['account']} | {e['category']} | {e['month']} | {e['reasoning']}{pd}")

    return "\n".join(lines)


# ── BM25 retrieval ────────────────────────────────────────────────────────────

def scan_records(table) -> list[dict]:
    items, last = [], None
    while True:
        kwargs: dict = {"Limit": 200}
        if last:
            kwargs["ExclusiveStartKey"] = last
        resp  = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
    return items


def tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def bm25_scores(query: str, docs: list[str], k1=1.5, b=0.75) -> list[float]:
    tokenized = [tokenize(d) for d in docs]
    avgdl     = sum(len(d) for d in tokenized) / max(len(tokenized), 1)
    df: dict[str, int] = defaultdict(int)
    for doc in tokenized:
        for term in set(doc):
            df[term] += 1
    N       = len(docs)
    q_terms = tokenize(query)
    scores  = []
    for doc in tokenized:
        score = 0.0
        tf_map: dict[str, int] = defaultdict(int)
        for term in doc:
            tf_map[term] += 1
        for term in q_terms:
            tf  = tf_map.get(term, 0)
            idf = math.log((N - df[term] + 0.5) / (df[term] + 0.5) + 1)
            score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * len(doc) / avgdl))
        scores.append(score)
    return scores


def build_doc(record: dict) -> str:
    issues = " ".join(
        i.get("title", i.get("description", "")) for i in record.get("issues", [])[:3]
    )
    parts = [
        record.get("title", ""),
        record.get("account_name", ""),
        record.get("call_type", ""),
        record.get("category", ""),
        record.get("sentiment_label", ""),
        record.get("escalation_team", ""),
        record.get("escalation_priority", ""),
        issues,
    ]
    return " ".join(str(p) for p in parts if p)


def retrieve(query: str, records: list[dict]) -> list[dict]:
    docs   = [build_doc(r) for r in records]
    scores = bm25_scores(query, docs)
    ranked = sorted(zip(scores, records), key=lambda x: -x[0])
    return [{"score": float(s), **r} for s, r in ranked[:CHATBOT_TOP_K] if s > 0]


def build_context(records: list[dict]) -> str:
    lines = []
    for r in records:
        issues_str = "; ".join(
            f"{i.get('severity','?').upper()}: {i.get('title','')}"
            for i in r.get("issues", [])[:3]
        )
        lines.append(
            f"meeting_id:  {r['meeting_id']}\n"
            f"title:       {r.get('title', '')}\n"
            f"account:     {r.get('account_name', '-')}\n"
            f"call_type:   {r.get('call_type', '')}\n"
            f"category:    {r.get('category', '')}\n"
            f"sentiment:   {r.get('sentiment_label', '')}  "
            f"(score={r.get('sentiment_score', '')}  z={r.get('z_score', '')}  "
            f"anomaly={r.get('is_anomaly', False)})\n"
            f"escalation:  team={r.get('escalation_team', '')}  "
            f"priority={r.get('escalation_priority', '')}  "
            f"pagerduty={r.get('has_pagerduty', False)}\n"
            f"feature_gap: {r.get('feature_gap', False)}\n"
            f"issues:      {issues_str or 'none'}\n"
            f"month:       {r.get('month', '')}"
        )
    return "\n\n---\n\n".join(lines)


# ── Claude call ───────────────────────────────────────────────────────────────

def answer(question: str, dashboard_knowledge: str, context_records: list[dict]) -> str:
    if not ANTHROPIC_API_KEY:
        return "ANTHROPIC_API_KEY is not configured on this Lambda."

    context = build_context(context_records)
    prompt  = ANSWER_PROMPT.format(
        dashboard_knowledge=dashboard_knowledge,
        context=context,
        question=question,
    )

    payload = json.dumps({
        "model":      ANTHROPIC_MODEL,
        "max_tokens": CHATBOT_MAX_TOKENS,
        "messages":   [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["content"][0]["text"].strip()


# ── Lambda handler ────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    body     = json.loads(event.get("body") or "{}")
    question = body.get("question", "").strip()
    if not question:
        return {"statusCode": 400, "headers": _HEADERS,
                "body": json.dumps({"error": "question is required"})}

    table = ddb.Table(RECORDS_TABLE)

    # Load dashboard knowledge (aggregates) + BM25 records in parallel
    dashboard_knowledge = load_dashboard_knowledge(table)
    records = scan_records(table)
    hits    = retrieve(question, records)

    try:
        ans = answer(question, dashboard_knowledge, hits)
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        return {"statusCode": 502, "headers": _HEADERS,
                "body": json.dumps({"error": f"Anthropic API error: {err[:200]}"})}
    except Exception as e:
        return {"statusCode": 500, "headers": _HEADERS,
                "body": json.dumps({"error": str(e)})}

    return {
        "statusCode": 200,
        "headers": _HEADERS,
        "body": json.dumps({
            "question": question,
            "answer":   ans,
            "sources": [
                {
                    "meeting_id": h["meeting_id"],
                    "title":      h.get("title", ""),
                    "account":    h.get("account_name", ""),
                    "call_type":  h.get("call_type"),
                    "category":   h.get("category"),
                    "score":      round(h["score"], 3),
                }
                for h in hits
            ],
        }),
    }

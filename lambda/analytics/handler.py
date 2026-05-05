"""
Analytics Lambda.
  GET /analytics/call-type-distribution  -- call-type breakdown
  GET /analytics/kpis                    -- summary KPI cards
  GET /analytics/anomalies               -- anomaly records with evidence
  GET /analytics/escalations             -- escalated meetings with reasoning
"""
import json, os, boto3
from boto3.dynamodb.conditions import Key
from decimal import Decimal

from ti_classifier import CALL_TYPES
from ti_config import TABLE_RECORDS_GSI

ddb = boto3.resource("dynamodb")

RECORDS_TABLE   = os.environ["RECORDS_TABLE"]
CALL_TYPE_INDEX = TABLE_RECORDS_GSI


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def query_distribution(table) -> tuple[dict, dict]:
    """Returns (distribution_counts, sample_titles) using the GSI -- no full-table scan."""
    distribution: dict[str, int] = {}
    samples: dict[str, list[str]] = {}

    for ct in CALL_TYPES:
        # COUNT query -- reads only index metadata, not item data
        count_resp = table.query(
            IndexName=CALL_TYPE_INDEX,
            KeyConditionExpression=Key("call_type").eq(ct),
            Select="COUNT",
        )
        distribution[ct] = count_resp["Count"]

        # Fetch 3 sample titles for context
        sample_resp = table.query(
            IndexName=CALL_TYPE_INDEX,
            KeyConditionExpression=Key("call_type").eq(ct),
            ProjectionExpression="title, meeting_id, category",
            Limit=3,
        )
        samples[ct] = [
            item.get("title", "(untitled)") for item in sample_resp.get("Items", [])
        ]

    return distribution, samples


def compute_kpis(table) -> dict:
    """Full table scan to compute summary KPI values."""
    items, last = [], None
    while True:
        kwargs = {
            "ProjectionExpression":
                "sentiment_score, is_anomaly, has_pagerduty, escalation_priority, feature_gap",
        }
        if last:
            kwargs["ExclusiveStartKey"] = last
        resp = table.scan(**kwargs)
        items.extend(resp["Items"])
        last = resp.get("LastEvaluatedKey")
        if not last:
            break

    total       = len(items)
    anomalies   = sum(1 for i in items if i.get("is_anomaly"))
    escalated   = sum(1 for i in items if i.get("escalation_priority") in ("p0", "p1"))
    feature_gaps = sum(1 for i in items if i.get("feature_gap"))
    scores      = [float(i["sentiment_score"]) for i in items if i.get("sentiment_score")]
    avg_sent    = round(sum(scores) / len(scores), 2) if scores else 0.0

    return {
        "total_meetings":   total,
        "anomalies_flagged": anomalies,
        "avg_sentiment":    avg_sent,
        "meetings_escalated": escalated,
        "feature_gaps":     feature_gaps,
    }


def full_scan(table, projection: str) -> list[dict]:
    items, last = [], None
    while True:
        kwargs = {"ProjectionExpression": projection}
        if last:
            kwargs["ExclusiveStartKey"] = last
        resp = table.scan(**kwargs)
        items.extend(resp["Items"])
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
    return items


def compute_anomalies(table) -> list[dict]:
    projection = (
        "meeting_id, title, account_name, call_type, category, "
        "sentiment_score, sentiment_label, z_score, is_anomaly, "
        "escalation_team, escalation_priority, issues, #mo"
    )
    items = table.scan(
        ProjectionExpression=projection,
        ExpressionAttributeNames={"#mo": "month"},
        FilterExpression="is_anomaly = :t",
        ExpressionAttributeValues={":t": True},
    ).get("Items", [])
    # keep scanning pages
    last = items  # simple single-page for now — anomalies are ~25 records
    result = []
    for i in sorted(items, key=lambda x: float(x.get("z_score", 0))):
        score    = float(i.get("sentiment_score", 0))
        z        = float(i.get("z_score", 0))
        issues   = i.get("issues", [])
        top_issue = issues[0] if issues else {}
        evidence = (
            f"Sentiment {score:.1f}/5 ({i.get('sentiment_label','')}) — "
            f"z-score {z:+.2f}σ from {i.get('call_type','')} baseline"
        )
        if top_issue:
            evidence += f". Top issue ({top_issue.get('severity','').upper()}): {top_issue.get('title','')}"
        result.append({
            "meeting_id":    i["meeting_id"],
            "title":         i.get("title", ""),
            "account":       i.get("account_name", "") or _account_from_title(i.get("title", "")),
            "call_type":     i.get("call_type", ""),
            "category":      i.get("category", ""),
            "sentiment":     score,
            "sentiment_label": i.get("sentiment_label", ""),
            "z_score":       z,
            "month":         i.get("month", ""),
            "escalation":    i.get("escalation_team", ""),
            "priority":      i.get("escalation_priority", ""),
            "evidence":      evidence,
            "issues":        [{"severity": x.get("severity"), "title": x.get("title", "")}
                              for x in issues[:3]],
        })
    return result


def compute_escalations(table) -> list[dict]:
    items = []
    last = None
    while True:
        kwargs = {
            "ProjectionExpression": (
                "meeting_id, title, account_name, call_type, category, "
                "sentiment_score, sentiment_label, z_score, is_anomaly, "
                "escalation_team, escalation_priority, has_pagerduty, issues, #mo"
            ),
            "ExpressionAttributeNames": {"#mo": "month"},
            "FilterExpression": "escalation_priority IN (:p0, :p1)",
            "ExpressionAttributeValues": {":p0": "p0", ":p1": "p1"},
        }
        if last:
            kwargs["ExclusiveStartKey"] = last
        resp = table.scan(**kwargs)
        items.extend(resp["Items"])
        last = resp.get("LastEvaluatedKey")
        if not last:
            break

    priority_order = {"p0": 0, "p1": 1}
    result = []
    for i in sorted(items, key=lambda x: (priority_order.get(x.get("escalation_priority", "p3"), 9),
                                           -float(x.get("z_score", 0)))):
        score   = float(i.get("sentiment_score", 0))
        z       = float(i.get("z_score", 0))
        issues  = i.get("issues", [])
        pri     = i.get("escalation_priority", "")
        team    = i.get("escalation_team", "")
        pd      = i.get("has_pagerduty", False)

        # Build reasoning sentence
        top_issue = issues[0] if issues else {}
        sev_label = top_issue.get("severity", pri).upper() if top_issue else pri.upper()
        reason_parts = []
        if top_issue:
            reason_parts.append(f"{sev_label} signal: \"{top_issue.get('title', '')}\"")
        if abs(z) > 1.5:
            direction = "below" if z < 0 else "above"
            reason_parts.append(f"sentiment {score:.1f}/5 ({z:+.2f}σ {direction} baseline)")
        if not reason_parts:
            reason_parts.append(f"category {i.get('category','')} matched escalation rule")
        reasoning = ". ".join(reason_parts) + f" → routed to {team} team"
        if pd:
            reasoning += " (PagerDuty fired)"

        result.append({
            "meeting_id":   i["meeting_id"],
            "title":        i.get("title", ""),
            "account":      i.get("account_name", "") or _account_from_title(i.get("title", "")),
            "call_type":    i.get("call_type", ""),
            "category":     i.get("category", ""),
            "sentiment":    score,
            "z_score":      z,
            "priority":     pri,
            "team":         team,
            "pagerduty":    pd,
            "is_anomaly":   bool(i.get("is_anomaly", False)),
            "month":        i.get("month", ""),
            "reasoning":    reasoning,
            "issues":       [{"severity": x.get("severity"), "title": x.get("title", "")}
                             for x in issues[:3]],
        })
    return result


def _account_from_title(title: str) -> str:
    """Extract 'Acme Corp' from 'Aegis / Acme Corp - ...' for external meetings."""
    import re
    m = re.match(r"^Aegis\s*/\s*(.+?)(?:\s+-\s+.+)?$", title or "", re.IGNORECASE)
    return m.group(1).strip() if m else ""


_HEADERS = {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"}


def lambda_handler(event, context):
    table = ddb.Table(RECORDS_TABLE)
    path  = event.get("path", "") or event.get("rawPath", "")

    if "anomalies" in path:
        try:
            body = {"anomalies": compute_anomalies(table)}
        except Exception as exc:
            return {"statusCode": 500, "headers": _HEADERS, "body": json.dumps({"error": str(exc)})}
        return {"statusCode": 200, "headers": _HEADERS, "body": json.dumps(body, cls=DecimalEncoder)}

    if "escalations" in path:
        try:
            body = {"escalations": compute_escalations(table)}
        except Exception as exc:
            return {"statusCode": 500, "headers": _HEADERS, "body": json.dumps({"error": str(exc)})}
        return {"statusCode": 200, "headers": _HEADERS, "body": json.dumps(body, cls=DecimalEncoder)}

    if "kpis" in path:
        try:
            body = compute_kpis(table)
        except Exception as exc:
            return {"statusCode": 500, "headers": _HEADERS, "body": json.dumps({"error": str(exc)})}
        return {"statusCode": 200, "headers": _HEADERS, "body": json.dumps(body, cls=DecimalEncoder)}

    # default: call-type distribution
    try:
        distribution, samples = query_distribution(table)
    except Exception as exc:
        return {"statusCode": 500, "headers": _HEADERS, "body": json.dumps({"error": str(exc)})}

    total = sum(distribution.values())
    body  = {
        "total_meetings": total,
        "distribution":   distribution,
        "percentages": {
            ct: round(n / total * 100, 1) if total else 0
            for ct, n in distribution.items()
        },
        "samples": samples,
    }
    return {"statusCode": 200, "headers": _HEADERS, "body": json.dumps(body, cls=DecimalEncoder)}

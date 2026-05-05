"""
Feedback Lambda -- human-in-the-loop correction endpoint.
POST /feedback

Accepts analyst corrections to pipeline outputs (category, call type, sentiment
label) and writes them to the ti-feedback table. Corrections are consumed by
the eval runner to expand ground-truth cases over time, closing the loop between
human review and automated regression testing.

Request body:
    {
      "meeting_id":               "01KQ7F58A2E07B857C24624D",
      "corrected_category":       "Product Reliability",  // optional
      "corrected_call_type":      "support",              // optional
      "corrected_sentiment_label":"negative",             // optional
      "notes":                    "Misclassified as internal, was a support call."
    }
"""
import json, os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from ti_shared import StructuredLogger

log = StructuredLogger(__name__)

ddb = boto3.resource("dynamodb")

FEEDBACK_TABLE = os.environ["FEEDBACK_TABLE"]
RECORDS_TABLE  = os.environ["RECORDS_TABLE"]

VALID_CALL_TYPES      = {"support", "external", "internal"}
VALID_SENTIMENT_LABELS = {"positive", "mixed-positive", "mixed", "mixed-negative", "negative"}

_CORRECTIONS = {"corrected_category", "corrected_call_type", "corrected_sentiment_label"}


def _validate(body: dict) -> str | None:
    if not body.get("meeting_id"):
        return "meeting_id is required"
    ct = body.get("corrected_call_type")
    if ct and ct not in VALID_CALL_TYPES:
        return f"corrected_call_type must be one of {sorted(VALID_CALL_TYPES)}"
    sl = body.get("corrected_sentiment_label")
    if sl and sl not in VALID_SENTIMENT_LABELS:
        return f"corrected_sentiment_label must be one of {sorted(VALID_SENTIMENT_LABELS)}"
    if not any(body.get(k) for k in _CORRECTIONS):
        return "at least one correction field is required"
    return None


def lambda_handler(event, context):
    body = json.loads(event.get("body") or "{}")

    err = _validate(body)
    if err:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": err}),
        }

    meeting_id   = body["meeting_id"]
    submitted_at = datetime.now(timezone.utc).isoformat()

    # Verify the meeting exists before accepting a correction
    try:
        resp = ddb.Table(RECORDS_TABLE).get_item(
            Key={"meeting_id": meeting_id},
            ProjectionExpression="meeting_id, title, call_type, category",
        )
    except ClientError as e:
        log.error("records_table_read_failed", meeting_id=meeting_id, error=str(e))
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": "could not verify meeting_id"}),
        }

    original = resp.get("Item")
    if not original:
        return {
            "statusCode": 404,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": f"meeting_id {meeting_id!r} not found"}),
        }

    feedback_item = {
        "meeting_id":               meeting_id,
        "submitted_at":             submitted_at,
        "original_category":        original.get("category", ""),
        "original_call_type":       original.get("call_type", ""),
        "corrected_category":       body.get("corrected_category", ""),
        "corrected_call_type":      body.get("corrected_call_type", ""),
        "corrected_sentiment_label":body.get("corrected_sentiment_label", ""),
        "notes":                    body.get("notes", ""),
        "title":                    original.get("title", ""),
    }

    ddb.Table(FEEDBACK_TABLE).put_item(Item=feedback_item)

    log.info("feedback_recorded", meeting_id=meeting_id,
             corrected_fields=[k for k in _CORRECTIONS if body.get(k)])

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({
            "status":       "recorded",
            "meeting_id":   meeting_id,
            "submitted_at": submitted_at,
            "corrections":  {k: body[k] for k in _CORRECTIONS if body.get(k)},
        }),
    }

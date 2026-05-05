"""
Nightly DynamoDB -> S3 export for QuickSight analytics.
Triggered by EventBridge at 02:00 UTC daily.
Writes date-partitioned NDJSON: exports/records/date=YYYY-MM-DD/records.ndjson
"""
import json
import os
import boto3
from datetime import datetime, timezone
from decimal import Decimal

from ti_shared import StructuredLogger

log = StructuredLogger(__name__)

RECORDS_TABLE     = os.environ["RECORDS_TABLE"]
PROCESSED_BUCKET  = os.environ["PROCESSED_BUCKET"]
EXPORT_S3_PREFIX  = os.environ.get("EXPORT_S3_PREFIX", "exports/records/")

_dynamo  = boto3.resource("dynamodb")
_s3      = boto3.client("s3")
_records = _dynamo.Table(RECORDS_TABLE)


def _to_json_safe(obj):
    """Recursively convert Decimal -> float so json.dumps works."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_json_safe(i) for i in obj]
    return obj


def scan_all_records():
    items = []
    kwargs = {}
    while True:
        resp = _records.scan(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def lambda_handler(event, context):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    s3_key = f"{EXPORT_S3_PREFIX}date={today}/records.ndjson"

    log.info("export_start", date=today, table=RECORDS_TABLE)

    items = scan_all_records()
    log.info("export_scanned", n_records=len(items))

    lines = [json.dumps(_to_json_safe(item)) for item in items]
    body  = "\n".join(lines)

    _s3.put_object(
        Bucket=PROCESSED_BUCKET,
        Key=s3_key,
        Body=body.encode("utf-8"),
        ContentType="application/x-ndjson",
    )

    log.info("export_complete", s3_key=s3_key, n_records=len(items))
    return {"status": "ok", "s3_key": s3_key, "n_records": len(items)}

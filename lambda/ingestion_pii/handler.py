"""
Ingestion + PII Firewall Lambda.

Reads the 6 meeting files from S3 raw/, redacts PII, writes sanitized JSON
to S3 processed/, stores placeholder->original map in DynamoDB, classifies
call type, emits CloudWatch metrics, then invokes either:
  (a) the Bedrock Agent via bedrock-agent-runtime:InvokeAgent  [primary]
  (b) the ti-orchestrator Lambda asynchronously                [fallback]
"""
import json, os, re, hashlib, time, boto3
from datetime import datetime, timezone
from botocore.exceptions import ClientError

from ti_classifier import classify_call_type
from ti_config import (
    PII_PATTERNS, RAW_BUCKET_NAME,
    INGESTION_SUMMARY_MAX_CHARS, INGESTION_KEY_MOMENTS_MAX,
)
from ti_shared import StructuredLogger

log = StructuredLogger(__name__)

s3             = boto3.client("s3")
ddb            = boto3.resource("dynamodb")
lambda_client  = boto3.client("lambda")
cw             = boto3.client("cloudwatch")

BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
bedrock_agent  = boto3.client("bedrock-agent-runtime", region_name=BEDROCK_REGION)
ssm_client     = boto3.client("ssm", region_name=BEDROCK_REGION)

RAW_BUCKET                 = RAW_BUCKET_NAME
PROCESSED_BUCKET           = os.environ["PROCESSED_BUCKET"]
PII_VAULT_TABLE            = os.environ["PII_VAULT_TABLE"]
LINEAGE_TABLE              = os.environ["LINEAGE_TABLE"]
ORCHESTRATOR_FUNCTION_NAME = os.environ["ORCHESTRATOR_FUNCTION_NAME"]
SSM_AGENT_ID_PATH          = os.environ.get("SSM_AGENT_ID_PATH",    "/ti/bedrock-agent-id")
SSM_AGENT_ALIAS_PATH       = os.environ.get("SSM_AGENT_ALIAS_PATH", "/ti/bedrock-agent-alias-id")

# SSM cold-start cache
_agent_ids: dict[str, str] = {}


def load_agent_ids() -> tuple[str, str]:
    """Load Bedrock Agent IDs from SSM Parameter Store (cached per cold start)."""
    if not _agent_ids:
        try:
            resp = ssm_client.get_parameters(
                Names=[SSM_AGENT_ID_PATH, SSM_AGENT_ALIAS_PATH],
                WithDecryption=False,
            )
            for p in resp.get("Parameters", []):
                _agent_ids[p["Name"]] = p["Value"]
            log.info("ssm_agent_ids_loaded",
                     agent_id=_agent_ids.get(SSM_AGENT_ID_PATH, ""),
                     alias_id=_agent_ids.get(SSM_AGENT_ALIAS_PATH, ""))
        except Exception as e:
            log.warning("ssm_load_failed", error=str(e))
    return (
        _agent_ids.get(SSM_AGENT_ID_PATH, ""),
        _agent_ids.get(SSM_AGENT_ALIAS_PATH, ""),
    )


# --- PII helpers ---

def placeholder(entity_type: str, value: str) -> str:
    h = hashlib.md5(value.lower().encode()).hexdigest()[:6]
    return f"[{entity_type}_{h}]"


def redact(text: str, vault_table, meeting_id: str) -> tuple[str, int]:
    """Return (redacted_text, n_replacements)."""
    if not isinstance(text, str):
        return text, 0
    n = 0
    for pattern, etype in PII_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            original = match.group()
            ph = placeholder(etype, original)
            vault_table.put_item(Item={
                "placeholder": ph,
                "original":    original,
                "entity_type": etype,
                "meeting_id":  meeting_id,
                "created_at":  datetime.now(timezone.utc).isoformat(),
            })
            text = text.replace(original, ph)
            n += 1
    return text, n


def redact_obj(obj, vault_table, meeting_id: str) -> tuple[object, int]:
    """Recursively redact all string values; return (sanitized, total_replacements)."""
    if isinstance(obj, str):
        return redact(obj, vault_table, meeting_id)
    if isinstance(obj, dict):
        total, result = 0, {}
        for k, v in obj.items():
            result[k], n = redact_obj(v, vault_table, meeting_id)
            total += n
        return result, total
    if isinstance(obj, list):
        total, result = 0, []
        for i in obj:
            item, n = redact_obj(i, vault_table, meeting_id)
            result.append(item)
            total += n
        return result, total
    return obj, 0


def load_file(bucket: str, key: str) -> dict:
    resp = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(resp["Body"].read())


def write_processed(meeting_id: str, data: dict) -> str:
    key = f"processed/{meeting_id}/sanitized.json"
    s3.put_object(
        Bucket=PROCESSED_BUCKET,
        Key=key,
        Body=json.dumps(data),
        ContentType="application/json",
    )
    return key


def log_lineage(lineage_table, meeting_id: str, stage: str, meta: dict):
    lineage_table.put_item(Item={
        "meeting_id": meeting_id,
        "stage":      stage,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        **meta,
    })


def emit_metric(metric_name: str, value: float = 1, dimensions: list | None = None):
    """Best-effort CloudWatch metric emission -- never blocks the pipeline."""
    try:
        cw.put_metric_data(
            Namespace="TranscriptIntelligence",
            MetricData=[{
                "MetricName": metric_name,
                "Dimensions": dimensions or [],
                "Value":      value,
                "Unit":       "Count",
            }],
        )
    except Exception:
        pass


# --- Lambda handler ---

def lambda_handler(event, context):
    vault_table   = ddb.Table(PII_VAULT_TABLE)
    lineage_table = ddb.Table(LINEAGE_TABLE)

    for sqs_record in event.get("Records", []):
        body       = json.loads(sqs_record["body"])
        meeting_id = body["meeting_id"]
        bucket     = body.get("bucket", RAW_BUCKET)

        log.info("processing_start", meeting_id=meeting_id, bucket=bucket)

        try:
            # Load all 6 files
            files: dict[str, dict] = {}
            for fname in ["meeting-info", "transcript", "summary",
                          "speaker-meta", "speakers", "events"]:
                key = f"raw/{meeting_id}/{fname}.json"
                try:
                    files[fname] = load_file(bucket, key)
                except Exception as e:
                    log.warning("file_load_failed", meeting_id=meeting_id,
                                file=fname, error=str(e))
                    files[fname] = {}

            # Classify call type (deterministic, no LLM)
            title     = files.get("meeting-info", {}).get("title", "")
            call_type = classify_call_type(title)

            # Redact PII
            sanitized, n_replacements = redact_obj(files, vault_table, meeting_id)
            sanitized["meeting_id"] = meeting_id
            sanitized["call_type"]  = call_type

            log.info("pii_redacted", meeting_id=meeting_id,
                     call_type=call_type, n_replacements=n_replacements)

            # Write sanitized payload to processed bucket
            out_key = write_processed(meeting_id, sanitized)

            # Emit metrics
            emit_metric("ProcessedMeetings",
                        dimensions=[{"Name": "CallType", "Value": call_type}])

            # Lineage
            log_lineage(lineage_table, meeting_id, "ingestion_pii", {
                "processed_key":  out_key,
                "call_type":      call_type,
                "title":          title,
                "files_loaded":   list(files.keys()),
                "n_pii_redacted": n_replacements,
            })

            # Invoke Bedrock Agent (primary) or orchestrator (fallback)
            bedrock_agent_id, bedrock_agent_alias_id = load_agent_ids()

            if bedrock_agent_id and bedrock_agent_alias_id not in ("", "PLACEHOLDER"):
                summary_data = files.get("summary", {})
                agent_input = (
                    f"Analyse this meeting:\n"
                    f"meeting_id: {meeting_id}\n"
                    f"title: {title}\n"
                    f"topics: {','.join(summary_data.get('topics', []))}\n"
                    f"sentiment_score: {summary_data.get('sentimentScore', 3.0)}\n"
                    f"sentiment_label: {summary_data.get('overallSentiment', 'mixed-positive')}\n"
                    f"summary: {summary_data.get('summary', '')[:INGESTION_SUMMARY_MAX_CHARS]}\n"
                    f"key_moments: {json.dumps(summary_data.get('keyMoments', [])[:INGESTION_KEY_MOMENTS_MAX])}\n"
                    f"processed_bucket: {PROCESSED_BUCKET}\n"
                    f"processed_at: {datetime.now(timezone.utc).isoformat()}"
                )

                # Invoke Bedrock Agent with exponential-backoff retry on throttle
                response = None
                for attempt in range(4):
                    try:
                        response = bedrock_agent.invoke_agent(
                            agentId=bedrock_agent_id,
                            agentAliasId=bedrock_agent_alias_id,
                            sessionId=meeting_id,
                            inputText=agent_input,
                            enableTrace=True,
                        )
                        break
                    except ClientError as e:
                        if e.response["Error"]["Code"] == "throttlingException" and attempt < 3:
                            wait = 5 * (2 ** attempt)
                            log.warning("bedrock_agent_throttled",
                                        meeting_id=meeting_id, attempt=attempt + 1, retry_in=wait)
                            time.sleep(wait)
                        else:
                            raise

                completion, tool_calls = "", 0
                for ev in response.get("completion", []):
                    if "chunk" in ev:
                        completion += ev["chunk"]["bytes"].decode("utf-8")
                    elif "trace" in ev:
                        ot = ev["trace"].get("trace", {}).get("orchestrationTrace", {})
                        if "invocationInput" in ot:
                            tool_calls += 1

                log.info("bedrock_agent_complete", meeting_id=meeting_id,
                         agent_id=bedrock_agent_id,
                         completion_chars=len(completion),
                         tool_calls=tool_calls)

                log_lineage(lineage_table, meeting_id, "ingestion_pii_bedrock_agent", {
                    "processed_key":    out_key,
                    "call_type":        call_type,
                    "agent_id":         bedrock_agent_id,
                    "completion_chars": len(completion),
                    "tool_calls":       tool_calls,
                })

            else:
                # Fallback: async invoke of ti-orchestrator
                lambda_client.invoke(
                    FunctionName=ORCHESTRATOR_FUNCTION_NAME,
                    InvocationType="Event",
                    Payload=json.dumps({
                        "meeting_id":       meeting_id,
                        "processed_bucket": PROCESSED_BUCKET,
                        "processed_key":    out_key,
                        "title":            title,
                        "call_type":        call_type,
                    }).encode(),
                )
                log.info("orchestrator_fallback_invoked", meeting_id=meeting_id)

        except Exception as exc:
            log.error("processing_failed", meeting_id=meeting_id, error=str(exc))
            emit_metric("ProcessingErrors",
                        dimensions=[{"Name": "Stage", "Value": "ingestion_pii"}])
            raise  # re-raise so SQS retries and eventually routes to DLQ

    return {"status": "ok"}

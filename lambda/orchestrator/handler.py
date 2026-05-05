"""
Orchestrator Lambda -- Claude Agent SDK with tool use (fallback path).

Used when the Bedrock Agent is not yet configured (BEDROCK_AGENT_ID not set
in SSM). Runs the same 6-tool analysis chain via a custom agentic loop using
anthropic.AnthropicBedrock.

Tool schemas are loaded from resources/tool_schemas.json.
System prompt is loaded from resources/system_prompt.txt.
All tunable constants come from ti_config -- no magic numbers in this file.
"""
import json, os, re, time, boto3
from datetime import datetime, timezone

from botocore.exceptions import ClientError
import anthropic
import ti_classifier

from ti_config import (
    TAXONOMY, CALL_TYPE_BASELINES, ESCALATION_RULES,
    SENTIMENT_STD_DEV, SENTIMENT_ANOMALY_Z_THRESHOLD,
    SEVERITY_PATTERNS, PAGERDUTY_SEVERITIES,
    AGENTIC_LOOP_MAX_ITERATIONS, ORCHESTRATOR_MAX_TOKENS,
    INGESTION_SUMMARY_MAX_CHARS, INGESTION_KEY_MOMENTS_MAX,
)
from ti_shared import dec, StructuredLogger, TokenTracker

log = StructuredLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))

_EXTERNAL_TITLE_RE = re.compile(r'^Aegis\s*/\s*(.+?)(?:\s+-\s+.+)?$', re.IGNORECASE)
_FEATURE_GAP_KWS   = frozenset([
    'roadmap', 'feature request', 'feature gap', 'gap', 'wishlist',
    'enhancement', 'missing capability', 'product feedback',
    'early access', 'backlog', 'not supported', 'workaround needed',
])

def _extract_account_name(title: str, call_type: str) -> str:
    if call_type != 'external':
        return ''
    m = _EXTERNAL_TITLE_RE.match(title.strip())
    return m.group(1).strip() if m else ''

def _has_feature_gap(category: str, title: str, issues: list) -> bool:
    if category == 'Product Feedback & Roadmap':
        return True
    combined = (title + ' ' + ' '.join(i.get('title', '') for i in issues)).lower()
    return any(kw in combined for kw in _FEATURE_GAP_KWS)

_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
bedrock_client = (
    anthropic.Anthropic(api_key=_api_key)
    if _api_key
    else anthropic.AnthropicBedrock(aws_region=os.environ.get("BEDROCK_REGION", "us-east-1"))
)
s3   = boto3.client("s3")
ddb  = boto3.resource("dynamodb")
sns  = boto3.client("sns")
cw   = boto3.client("cloudwatch")

MODEL            = os.environ["MODEL_SONNET"]
RECORDS_TABLE    = os.environ["RECORDS_TABLE"]
LINEAGE_TABLE    = os.environ["LINEAGE_TABLE"]
PROCESSED_BUCKET = os.environ["PROCESSED_BUCKET"]
ESCALATION_TOPIC = os.environ["ESCALATION_TOPIC"]

with open(os.path.join(_HERE, "resources", "system_prompt.txt")) as _f:
    SYSTEM_PROMPT = _f.read().strip()

with open(os.path.join(_HERE, "resources", "tool_schemas.json")) as _f:
    TOOLS = json.load(_f)


# --- Tool implementations ---

def tool_classify_call_type(title: str) -> dict:
    return {'call_type': ti_classifier.classify_call_type(title)}


def tool_analyze_sentiment(call_type: str, upstream_score: float,
                           upstream_label: str) -> dict:
    baseline = CALL_TYPE_BASELINES.get(call_type, 3.0)
    z = round((upstream_score - baseline) / SENTIMENT_STD_DEV, 2)
    return {
        'score':      upstream_score,
        'label':      upstream_label,
        'baseline':   baseline,
        'z_score':    z,
        'is_anomaly': abs(z) > SENTIMENT_ANOMALY_Z_THRESHOLD,
    }


def tool_classify_category(topics: list) -> dict:
    topic_str = ' '.join(topics).lower()
    matched = [cat for cat, kws in TAXONOMY.items()
               if any(kw in topic_str for kw in kws)]
    primary = matched[0] if matched else 'Other'
    return {'primary_category': primary, 'all_categories': matched or ['Other']}


def tool_extract_issues(summary_text: str, key_moments: list = None) -> dict:
    if key_moments is None:
        key_moments = []
    combined = (summary_text + ' ' +
                ' '.join(km.get('text', '') for km in key_moments)).lower()
    issues = []
    for sev, patterns in SEVERITY_PATTERNS.items():
        for p in patterns:
            if p in combined:
                for km in key_moments:
                    if p in km.get('text', '').lower():
                        issues.append({'title': km['text'][:80], 'severity': sev,
                                       'type': km.get('type', '')})
                        break
                else:
                    issues.append({'title': f'{sev.upper()} signal: {p}',
                                   'severity': sev, 'type': 'pattern_match'})
                break
        if issues and issues[-1]['severity'] in PAGERDUTY_SEVERITIES:
            break
    seen, deduped = set(), []
    for i in issues:
        if i['severity'] not in seen:
            seen.add(i['severity'])
            deduped.append(i)
    return {'issues': deduped, 'n_issues': len(deduped)}


def tool_route_escalation(category: str, severity: str, sentiment_score: float,
                          is_anomaly: bool, call_type: str) -> dict:
    team = ESCALATION_RULES.get((category, severity))
    if not team:
        if is_anomaly and sentiment_score < 2.5:
            team = 'cs'
        elif severity in PAGERDUTY_SEVERITIES:
            team = 'engineering'
        else:
            team = 'support'
    return {
        'team':                team,
        'priority':            severity or 'p3',
        'pagerduty':           severity in PAGERDUTY_SEVERITIES,
        'requires_escalation': severity in PAGERDUTY_SEVERITIES or is_anomaly,
    }


def tool_save_record(meeting_id: str, title: str, call_type: str, category: str,
                     sentiment_score: float, sentiment_label: str, is_anomaly: bool,
                     z_score: float, issues: list, escalation: dict,
                     processed_bucket: str, processed_at: str) -> dict:
    """Write final record to DynamoDB. Idempotent: duplicate meeting_id is silently skipped."""
    record = {
        'meeting_id':          meeting_id,
        'title':               title,
        'call_type':           call_type,
        'category':            category,
        'sentiment_score':     sentiment_score,
        'sentiment_label':     sentiment_label,
        'is_anomaly':          is_anomaly,
        'z_score':             z_score,
        'n_issues':            len(issues),
        'issues':              issues,
        'escalation_team':     escalation.get('team', ''),
        'escalation_priority': escalation.get('priority', 'p3'),
        'has_pagerduty':       escalation.get('pagerduty', False),
        'processed_at':        processed_at,
        'processed_bucket':    processed_bucket,
        'account_name':        _extract_account_name(title, call_type),
        'feature_gap':         _has_feature_gap(category, title, issues),
        'month':               processed_at[:7] if len(processed_at) >= 7 else '',
    }

    try:
        ddb.Table(RECORDS_TABLE).put_item(
            Item=dec(record),
            ConditionExpression="attribute_not_exists(meeting_id)",
        )
        log.info("record_saved", meeting_id=meeting_id,
                 call_type=call_type, category=category)
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            log.info("record_duplicate_skipped", meeting_id=meeting_id)
            return {'status': 'duplicate', 'meeting_id': meeting_id}
        raise

    ddb.Table(LINEAGE_TABLE).put_item(Item={
        'meeting_id': meeting_id,
        'stage':      'orchestrator',
        'timestamp':  processed_at,
        'status':     'complete',
        'model':      MODEL,
    })

    if ESCALATION_TOPIC and escalation.get('pagerduty'):
        try:
            sns.publish(
                TopicArn=ESCALATION_TOPIC,
                Subject=f"[TI] Escalation: {meeting_id}",
                Message=json.dumps({
                    'meeting_id': meeting_id,
                    'title':      title,
                    'category':   category,
                    'team':       escalation.get('team'),
                    'priority':   escalation.get('priority'),
                }, default=str),
            )
            log.info("escalation_published", meeting_id=meeting_id,
                     team=escalation.get('team'))
        except Exception as e:
            log.warning("escalation_publish_failed", meeting_id=meeting_id, error=str(e))

    return {'status': 'saved', 'meeting_id': meeting_id}


# --- Token metric emission ---

def _emit_token_metrics(tracker: TokenTracker, meeting_id: str) -> None:
    """Publishes per-run token usage to CloudWatch for cost visibility."""
    try:
        dims = [{"Name": "Model", "Value": tracker.model}]
        cw.put_metric_data(
            Namespace="TI/BedrockUsage",
            MetricData=[
                {"MetricName": "InputTokens",      "Value": tracker.input_tokens,       "Unit": "Count",         "Dimensions": dims},
                {"MetricName": "OutputTokens",     "Value": tracker.output_tokens,      "Unit": "Count",         "Dimensions": dims},
                {"MetricName": "TotalTokens",      "Value": tracker.total_tokens,       "Unit": "Count",         "Dimensions": dims},
                {"MetricName": "EstimatedCostUSD", "Value": tracker.estimated_cost_usd, "Unit": "None",          "Dimensions": dims},
            ],
        )
    except Exception as exc:
        log.warning("token_metric_emit_failed", meeting_id=meeting_id, error=str(exc))


# --- Tool registry ---

TOOL_MAP = {
    'classify_call_type': tool_classify_call_type,
    'classify_category':  tool_classify_category,
    'analyze_sentiment':  tool_analyze_sentiment,
    'extract_issues':     tool_extract_issues,
    'route_escalation':   tool_route_escalation,
    'save_record':        tool_save_record,
}


def run_tool(name: str, inputs: dict) -> str:
    fn = TOOL_MAP.get(name)
    if not fn:
        return json.dumps({'error': f'Unknown tool: {name}'})
    try:
        result = fn(**inputs)
        return json.dumps(result, default=str)
    except Exception as e:
        log.error("tool_execution_failed", tool=name, error=str(e))
        return json.dumps({'error': str(e)})


# --- Agentic loop ---

def run_agent(sanitized_data: dict, processed_bucket: str) -> dict:
    info    = sanitized_data.get('meeting-info', {})
    summary = sanitized_data.get('summary', {})
    title   = info.get('title', '')
    meeting_id   = sanitized_data.get('meeting_id', '')
    # Use the meeting's own start time so `month` reflects when it happened
    processed_at = info.get('startTime', datetime.now(timezone.utc).isoformat())

    user_message = (
        f"Analyse this meeting:\n\n"
        f"meeting_id: {meeting_id}\n"
        f"title: {title}\n"
        f"topics: {summary.get('topics', [])}\n"
        f"sentiment_score: {summary.get('sentimentScore', 3.0)}\n"
        f"sentiment_label: {summary.get('overallSentiment', 'mixed-positive')}\n"
        f"summary: {summary.get('summary', '')[:INGESTION_SUMMARY_MAX_CHARS]}\n"
        f"key_moments: {json.dumps(summary.get('keyMoments', [])[:INGESTION_KEY_MOMENTS_MAX])}\n"
        f"processed_bucket: {processed_bucket}\n"
        f"processed_at: {processed_at}"
    )

    messages = [{"role": "user", "content": user_message}]
    tool_calls_made = []
    tracker = TokenTracker(MODEL)

    log.info("agent_loop_start", meeting_id=meeting_id)

    for iteration in range(AGENTIC_LOOP_MAX_ITERATIONS):
        for attempt in range(6):
            try:
                response = bedrock_client.messages.create(
                    model=MODEL,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=messages,
                    max_tokens=ORCHESTRATOR_MAX_TOKENS,
                )
                break
            except anthropic.RateLimitError:
                wait = (2 ** attempt) * 5
                log.warning("rate_limit_retry", meeting_id=meeting_id,
                            attempt=attempt + 1, wait_seconds=wait)
                time.sleep(wait)
        else:
            raise RuntimeError(f"Rate limit not resolved after 6 retries for {meeting_id}")
        tracker.add(response.usage)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            log.info("agent_loop_complete", meeting_id=meeting_id,
                     iterations=iteration + 1, n_tools=len(tool_calls_made),
                     **tracker.as_dict())
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result_str = run_tool(block.name, block.input)
                    tool_calls_made.append({
                        'tool':   block.name,
                        'input':  block.input,
                        'result': result_str,
                    })
                    log.info("tool_called", meeting_id=meeting_id, tool=block.name)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result_str,
                    })
            messages.append({"role": "user", "content": tool_results})

    _emit_token_metrics(tracker, meeting_id)
    return {'meeting_id': meeting_id, 'tool_calls': tool_calls_made,
            'status': 'complete', 'token_usage': tracker.as_dict()}


# --- Lambda handler ---

def lambda_handler(event, context):
    meeting_id       = event['meeting_id']
    processed_bucket = event['processed_bucket']
    processed_key    = event['processed_key']

    log.info("orchestrator_start", meeting_id=meeting_id, processed_key=processed_key)

    obj = s3.get_object(Bucket=processed_bucket, Key=processed_key)
    sanitized_data = json.loads(obj['Body'].read())

    result = run_agent(sanitized_data, processed_bucket)

    log.info("orchestrator_complete", meeting_id=meeting_id,
             tool_calls_made=len(result['tool_calls']))

    return {
        'status':          'ok',
        'meeting_id':      meeting_id,
        'tool_calls_made': len(result['tool_calls']),
    }

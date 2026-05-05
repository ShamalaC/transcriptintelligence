"""
Agent Actions Lambda -- handles ALL action group calls from the Bedrock Agent.

Bedrock Agents dispatches here for every tool call made by the managed
orchestration agent (TranscriptAnalysisActions action group). The Lambda
receives a structured event with `function` and `parameters`, executes the
appropriate tool, and returns the result in the Bedrock Agent response format.
"""
import json, os, re, boto3
from datetime import datetime, timezone

from botocore.exceptions import ClientError
import ti_classifier
from ti_config import (
    TAXONOMY, CALL_TYPE_BASELINES, ESCALATION_RULES,
    SENTIMENT_STD_DEV, SENTIMENT_ANOMALY_Z_THRESHOLD,
    SEVERITY_PATTERNS, PAGERDUTY_SEVERITIES,
)
from ti_shared import dec, StructuredLogger

log = StructuredLogger(__name__)

ddb = boto3.resource("dynamodb")
sns = boto3.client("sns")

RECORDS_TABLE    = os.environ.get("RECORDS_TABLE",    "ti-records")
LINEAGE_TABLE    = os.environ.get("LINEAGE_TABLE",    "ti-lineage")
ESCALATION_TOPIC = os.environ.get("ESCALATION_TOPIC", "")
MODEL_SONNET     = os.environ.get("MODEL_SONNET",     "us.anthropic.claude-sonnet-4-6")

# Keywords that signal a product/feature gap discussion in any meeting
_FEATURE_GAP_KEYWORDS = frozenset([
    'roadmap', 'feature request', 'feature gap', 'gap', 'wishlist',
    'enhancement', 'missing capability', 'product feedback',
    'early access', 'backlog', 'not supported', 'workaround needed',
])

# Pattern: "Aegis / Acme Corp - Annual Renewal" → "Acme Corp"
_EXTERNAL_TITLE_RE = re.compile(
    r'^Aegis\s*/\s*(.+?)(?:\s+-\s+.+)?$', re.IGNORECASE
)


def _extract_account_name(title: str, call_type: str) -> str:
    """Return the customer account name for external calls; empty string otherwise."""
    if call_type != 'external':
        return ''
    m = _EXTERNAL_TITLE_RE.match(title.strip())
    return m.group(1).strip() if m else ''


def _has_feature_gap(ctx: dict, issues: list) -> bool:
    """True when this meeting contains product/feature gap signals."""
    if ctx.get('category') == 'Product Feedback & Roadmap':
        return True
    combined = (
        ctx.get('title', '') + ' ' +
        ' '.join(i.get('title', '') for i in issues if isinstance(i, dict))
    ).lower()
    return any(kw in combined for kw in _FEATURE_GAP_KEYWORDS)


# --- Tool implementations ---

def classify_call_type(title: str) -> dict:
    return {'call_type': ti_classifier.classify_call_type(title)}


def classify_category(topics: str) -> dict:
    topic_list = (topics if isinstance(topics, list)
                  else [t.strip() for t in str(topics).split(',') if t.strip()])
    topic_str = ' '.join(topic_list).lower()
    matched = [cat for cat, kws in TAXONOMY.items() if any(kw in topic_str for kw in kws)]
    primary = matched[0] if matched else 'Other'
    return {'primary_category': primary, 'all_categories': matched or ['Other']}


def analyze_sentiment(call_type: str, upstream_score, upstream_label: str) -> dict:
    score    = float(upstream_score)
    baseline = CALL_TYPE_BASELINES.get(call_type, 3.0)
    z        = round((score - baseline) / SENTIMENT_STD_DEV, 2)
    return {
        'score':      score,
        'label':      upstream_label,
        'baseline':   baseline,
        'z_score':    z,
        'is_anomaly': abs(z) > SENTIMENT_ANOMALY_Z_THRESHOLD,
    }


def extract_issues(summary_text: str, key_moments_json: str = None) -> dict:
    key_moments = []
    if key_moments_json:
        try:
            parsed = json.loads(key_moments_json)
            if isinstance(parsed, list):
                key_moments = parsed
        except (json.JSONDecodeError, TypeError):
            pass

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


def route_escalation(category: str, severity: str, sentiment_score,
                     is_anomaly, call_type: str) -> dict:
    score   = float(sentiment_score)
    anomaly = (str(is_anomaly).lower() in ('true', '1', 'yes')
               if isinstance(is_anomaly, str) else bool(is_anomaly))
    team    = ESCALATION_RULES.get((category, severity))
    if not team:
        if anomaly and score < 2.5:
            team = 'cs'
        elif severity in PAGERDUTY_SEVERITIES:
            team = 'engineering'
        else:
            team = 'support'
    return {
        'team':                team,
        'priority':            severity or 'p3',
        'pagerduty':           severity in PAGERDUTY_SEVERITIES,
        'requires_escalation': severity in PAGERDUTY_SEVERITIES or anomaly,
    }


def save_record(meeting_id: str, meeting_context_json: str,
                sentiment_json: str, issues_json: str,
                escalation_json: str) -> dict:
    """
    Persist the final analysed record to DynamoDB.

    All complex fields arrive as JSON strings (Bedrock Agent 5-param limit).
    Idempotent: a duplicate meeting_id is silently skipped -- safe if the agent
    retries or the Lambda is invoked more than once for the same session.
    """
    def parse(s, default):
        try:
            return json.loads(s) if isinstance(s, str) else (s or default)
        except (json.JSONDecodeError, TypeError):
            return default

    ctx        = parse(meeting_context_json, {})
    sentiment  = parse(sentiment_json, {})
    issues     = parse(issues_json, [])
    escalation = parse(escalation_json, {})

    processed_at = ctx.get('processed_at', datetime.now(timezone.utc).isoformat())
    score        = float(sentiment.get('sentiment_score', 3.0))
    anomaly_raw  = sentiment.get('is_anomaly', False)
    anomaly      = (str(anomaly_raw).lower() in ('true', '1', 'yes')
                    if isinstance(anomaly_raw, str) else bool(anomaly_raw))

    _title     = ctx.get('title', '')
    _call_type = ctx.get('call_type', '')
    _issues    = issues if isinstance(issues, list) else []

    record = {
        'meeting_id':          meeting_id,
        'title':               _title,
        'call_type':           _call_type,
        'category':            ctx.get('category', ''),
        'sentiment_score':     score,
        'sentiment_label':     sentiment.get('sentiment_label', 'unknown'),
        'is_anomaly':          anomaly,
        'z_score':             float(sentiment.get('z_score', 0.0)),
        'n_issues':            len(_issues),
        'issues':              _issues,
        'escalation_team':     escalation.get('team', '') if isinstance(escalation, dict) else '',
        'escalation_priority': escalation.get('priority', 'p3') if isinstance(escalation, dict) else 'p3',
        'has_pagerduty':       escalation.get('pagerduty', False) if isinstance(escalation, dict) else False,
        'processed_at':        processed_at,
        'processed_bucket':    ctx.get('processed_bucket', ''),
        # QuickSight enrichment fields
        'account_name':        _extract_account_name(_title, _call_type),
        'feature_gap':         _has_feature_gap(ctx, _issues),
        'month':               processed_at[:7] if len(processed_at) >= 7 else '',
    }

    try:
        ddb.Table(RECORDS_TABLE).put_item(
            Item=dec(record),
            ConditionExpression="attribute_not_exists(meeting_id)",
        )
        log.info("record_saved", meeting_id=meeting_id,
                 call_type=record['call_type'], category=record['category'])
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            log.info("record_duplicate_skipped", meeting_id=meeting_id)
            return {'status': 'duplicate', 'meeting_id': meeting_id}
        raise

    ddb.Table(LINEAGE_TABLE).put_item(Item={
        'meeting_id': meeting_id,
        'stage':      'bedrock_agent',
        'timestamp':  processed_at,
        'status':     'complete',
        'model':      MODEL_SONNET,
    })

    if ESCALATION_TOPIC and isinstance(escalation, dict) and escalation.get('pagerduty'):
        try:
            sns.publish(
                TopicArn=ESCALATION_TOPIC,
                Subject=f"[TI] Escalation: {meeting_id}",
                Message=json.dumps({
                    'meeting_id': meeting_id,
                    'title':      ctx.get('title', ''),
                    'category':   ctx.get('category', ''),
                    'team':       escalation.get('team'),
                    'priority':   escalation.get('priority'),
                }, default=str),
            )
            log.info("escalation_published", meeting_id=meeting_id,
                     team=escalation.get('team'), priority=escalation.get('priority'))
        except Exception as e:
            log.warning("escalation_publish_failed", meeting_id=meeting_id, error=str(e))

    return {'status': 'saved', 'meeting_id': meeting_id}


# --- Dispatcher ---

TOOL_MAP = {
    'classify_call_type': classify_call_type,
    'classify_category':  classify_category,
    'analyze_sentiment':  analyze_sentiment,
    'extract_issues':     extract_issues,
    'route_escalation':   route_escalation,
    'save_record':        save_record,
}


def dispatch(function_name: str, params: dict):
    fn = TOOL_MAP.get(function_name)
    if not fn:
        raise ValueError(f"Unknown function: {function_name!r}")
    return fn(**params)


# --- Lambda handler ---

def lambda_handler(event, context):
    """
    Entry point called by Amazon Bedrock Agents for every tool invocation.

    Event shape (Bedrock Agents function-details format):
    {
      "messageVersion": "1.0",
      "actionGroup": "TranscriptAnalysisActions",
      "function": "analyze_sentiment",
      "parameters": [
        {"name": "call_type", "type": "string", "value": "support"},
        ...
      ]
    }
    """
    action_group = event.get('actionGroup', 'TranscriptAnalysisActions')
    function     = event.get('function', '')
    params       = {p['name']: p['value'] for p in event.get('parameters', [])}

    log.info("tool_invoked", function=function, action_group=action_group)

    try:
        result = dispatch(function, params)
        body   = json.dumps(result, default=str)
    except Exception as e:
        log.error("tool_failed", function=function, error=str(e))
        body = json.dumps({'error': str(e)})

    return {
        'messageVersion': '1.0',
        'response': {
            'actionGroup': action_group,
            'function':    function,
            'functionResponse': {
                'responseBody': {'TEXT': {'body': body}}
            },
        },
    }

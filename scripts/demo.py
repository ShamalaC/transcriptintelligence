"""
Transcript Intelligence — REAL end-to-end pipeline demo.

Uploads a meeting to S3, lets the cloud pipeline run (S3 trigger → SQS →
ingestion_pii Lambda → orchestrator Lambda → Claude tool calls → DynamoDB),
and streams CloudWatch logs live so you can watch every step happen on AWS.

Usage:
  python3 scripts/demo.py                   # random meeting, real pipeline
  python3 scripts/demo.py <meeting_id>      # specific meeting
"""
import sys, os, json, time, random, argparse, subprocess, boto3
from pathlib import Path
from datetime import datetime, timezone, timedelta

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "lambda" / "shared" / "python"))

# ── AWS clients ───────────────────────────────────────────────────────────────
REGION         = "us-east-1"
RAW_BUCKET     = "call-transcript-root"
RECORDS_TABLE  = "ti-records"
LINEAGE_TABLE  = "ti-lineage"
DATASET_DIR    = REPO_ROOT / "data" / "dataset"
DASHBOARD_URL  = (
    "https://us-east-1.quicksight.aws.amazon.com/sn/account/shamala-aws"
    "/dashboards/ti-analytics-dashboard/sheets/overview"
)
LOG_GROUPS = [
    "TranscriptIntelligenceStack-LGS3TriggerE52AA3E1-oAEAKvxNjs3O",
    "TranscriptIntelligenceStack-LGIngestionPII514EFDFA-sbL1Fcxk4g3s",
    "TranscriptIntelligenceStack-LGOrchestrator8848F3B7-uGypV0cuIvjZ",
]

s3   = boto3.client("s3",        region_name=REGION)
ddb  = boto3.resource("dynamodb", region_name=REGION)
logs = boto3.client("logs",      region_name=REGION)

# ── colours ───────────────────────────────────────────────────────────────────
G   = "\033[32m"
Y   = "\033[33m"
C   = "\033[36m"
B   = "\033[1m"
R   = "\033[31m"
DIM = "\033[2m"
NC  = "\033[0m"


def banner():
    print(f"""
{B}{C}╔══════════════════════════════════════════════════════════════╗
║        TRANSCRIPT INTELLIGENCE  —  Live Pipeline Demo        ║
║        Aegis Cloud Security · B2B SaaS · Claude on AWS       ║
╚══════════════════════════════════════════════════════════════╝{NC}
""")


def step(n, label):
    print(f"\n{B}{Y}[ Step {n} ]{NC}  {B}{label}{NC}")


def ok(msg):   print(f"  {G}✓{NC}  {msg}")
def info(msg): print(f"  {DIM}→{NC}  {msg}")
def log_line(source, msg):
    src_color = {
        "s3-trigger":    C,
        "ingestion-pii": Y,
        "orchestrator":  G,
    }.get(source, DIM)
    print(f"  {src_color}[{source}]{NC}  {msg}")


# ── delete existing DynamoDB record so pipeline can write fresh ───────────────
def delete_existing(meeting_id: str):
    table  = ddb.Table(RECORDS_TABLE)
    ltable = ddb.Table(LINEAGE_TABLE)
    table.delete_item(Key={"meeting_id": meeting_id})
    # delete all lineage stages
    result = ltable.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("meeting_id").eq(meeting_id)
    )
    for item in result.get("Items", []):
        ltable.delete_item(Key={"meeting_id": item["meeting_id"], "stage": item["stage"]})


# All 6 files the ingestion_pii Lambda reads from S3.
# meeting-info.json is uploaded last — it's the suffix that triggers the S3
# notification → SQS → pipeline.  The other 5 files are uploaded first so
# ingestion_pii can read them all when it fires.
_ALL_FILES   = ["transcript", "summary", "speaker-meta", "speakers", "events"]
_TRIGGER_FILE = "meeting-info"


# ── upload meeting files to S3 raw bucket ─────────────────────────────────────
def upload_meeting(meeting_dir: Path, meeting_id: str):
    """Upload all 6 meeting files to S3.

    The 5 non-trigger files are uploaded first (no S3 notification fires for
    them — the bucket filter watches only *meeting-info.json).  meeting-info.json
    is uploaded last via trigger_upload() after delete_existing() clears the
    DynamoDB record, so exactly one fresh pipeline run starts.
    """
    uploaded = 0
    for fname in _ALL_FILES:
        fpath = meeting_dir / f"{fname}.json"
        if fpath.exists():
            key = f"raw/{meeting_id}/{fname}.json"
            s3.upload_file(str(fpath), RAW_BUCKET, key)
            ok(f"Staged  s3://{RAW_BUCKET}/{key}")
            uploaded += 1
    info(f"{uploaded} support files staged (no pipeline trigger yet)")


def trigger_upload(meeting_dir: Path, meeting_id: str):
    """Upload meeting-info.json — this is the suffix that fires the S3 notification."""
    fpath = meeting_dir / f"{_TRIGGER_FILE}.json"
    if fpath.exists():
        key = f"raw/{meeting_id}/{_TRIGGER_FILE}.json"
        s3.upload_file(str(fpath), RAW_BUCKET, key)
        ok(f"Uploaded  s3://{RAW_BUCKET}/{key}  → pipeline triggered")


# ── tail CloudWatch logs across all three Lambda groups ──────────────────────
def poll_log_group(group_name: str, friendly: str, start_ms: int,
                   meeting_id: str, seen: set):
    """Fetch and print new log events using filter_log_events (time-based, no stream lookup)."""
    try:
        paginator = logs.get_paginator("filter_log_events")
        for page in paginator.paginate(
            logGroupName=group_name,
            startTime=start_ms,
            PaginationConfig={"MaxItems": 200},
        ):
            for ev in page.get("events", []):
                key = f"{ev['logStreamName']}:{ev['timestamp']}:{ev['message'][:40]}"
                if key in seen:
                    continue
                seen.add(key)

                msg = ev["message"].strip()
                if not msg:
                    continue
                if msg.startswith(("START ", "END ", "REPORT ", "INIT_START", "[INFO]", "[ERROR]", "[WARNING]")):
                    # Lambda prefixed lines — parse the JSON suffix instead
                    try:
                        json_start = msg.index("{")
                        msg = msg[json_start:]
                    except ValueError:
                        continue

                try:
                    d = json.loads(msg)
                    mid = d.get("meeting_id", "")
                    if mid and mid != meeting_id:
                        continue
                    event_name = d.get("event", d.get("message", ""))
                    if not event_name:
                        continue
                    extras = {k: v for k, v in d.items()
                              if k not in ("event", "message", "level", "logger",
                                           "timestamp", "meeting_id", "name")}
                    extra_str = "  ".join(f"{k}={v}" for k, v in list(extras.items())[:5])
                    display = f"{event_name}  {extra_str}" if extra_str else event_name
                except Exception:
                    display = msg[:100]

                if display:
                    log_line(friendly, display)
    except Exception:
        pass


def tail_logs(start_time_ms: int, meeting_id: str, timeout_sec: int = 120):
    """Stream CloudWatch logs from all three Lambda groups until record is in DynamoDB."""
    seen_events = set()
    table = ddb.Table(RECORDS_TABLE)
    deadline = time.time() + timeout_sec
    found_record = False
    drain_until = None

    groups = {
        "TranscriptIntelligenceStack-LGS3TriggerE52AA3E1-oAEAKvxNjs3O":   "s3-trigger",
        "TranscriptIntelligenceStack-LGIngestionPII514EFDFA-sbL1Fcxk4g3s": "ingestion-pii",
        "TranscriptIntelligenceStack-LGOrchestrator8848F3B7-uGypV0cuIvjZ": "orchestrator",
    }

    print(f"\n  {DIM}Streaming CloudWatch logs — watching all three Lambda functions...{NC}\n")

    # initial wait: s3-trigger + ingestion_pii fire within ~5s of upload
    time.sleep(8)

    while time.time() < deadline:
        # Poll all log groups first
        for group_name, friendly in groups.items():
            poll_log_group(group_name, friendly, start_time_ms, meeting_id, seen_events)

        # Check DynamoDB
        if not found_record:
            resp = table.get_item(Key={"meeting_id": meeting_id})
            if "Item" in resp:
                found_record = True
                drain_until = time.time() + 18  # keep draining logs for 18s after record appears

        if found_record and time.time() >= drain_until:
            break

        time.sleep(4)

    return found_record


# ── main demo flow ────────────────────────────────────────────────────────────
def run_demo(meeting_dir: Path):
    info_j   = json.loads((meeting_dir / "meeting-info.json").read_text())
    summary  = json.loads((meeting_dir / "summary.json").read_text())
    meeting_id = info_j["meetingId"]
    title      = info_j.get("title", "")
    attendees  = info_j.get("allEmails", [])
    topics     = summary.get("topics", [])

    # ── Step 1: show raw input ────────────────────────────────────────────────
    step(1, "Raw meeting input")
    ok(f"Meeting:   {B}{title}{NC}")
    info(f"ID:        {meeting_id}")
    info(f"Date:      {info_j.get('startTime','')[:10]}  |  "
         f"Duration: {info_j.get('duration',0):.0f} min  |  "
         f"Attendees: {len(attendees)}")
    info(f"Topics:    {', '.join(topics[:5])}{'...' if len(topics) > 5 else ''}")
    info(f"Sentiment: {summary.get('sentimentScore')}  ({summary.get('overallSentiment')})")

    # ── Step 2: stage all support files, then clear the DynamoDB record ─────
    step(2, "Staging all 6 meeting files to S3  (transcript, summary, speakers, …)")
    upload_meeting(meeting_dir, meeting_id)
    delete_existing(meeting_id)
    ok(f"DynamoDB record cleared  {meeting_id}")

    # ── Step 3: upload meeting-info.json (the S3-notification trigger) ────────
    step(3, "Uploading meeting-info.json  →  fires the pipeline")
    start_ms = int(time.time() * 1000)
    trigger_upload(meeting_dir, meeting_id)

    print(f"""
  {B}Single pipeline run triggered on AWS:{NC}
  {DIM}S3 upload  →  s3_trigger Lambda  →  SQS  →  ingestion_pii (PII scrub)
           →  orchestrator Lambda  →  Claude (6 tool calls)  →  DynamoDB{NC}
""")

    # ── Step 4: stream CloudWatch logs live ───────────────────────────────────
    step(4, "Live CloudWatch logs  (all three Lambda functions)")
    found = tail_logs(start_ms, meeting_id, timeout_sec=120)

    if not found:
        print(f"\n  {R}Timed out waiting for DynamoDB record — check CloudWatch manually{NC}")
        return

    # ── Step 5: show final DynamoDB record ────────────────────────────────────
    step(5, "Reading completed record from DynamoDB")
    item = ddb.Table(RECORDS_TABLE).get_item(Key={"meeting_id": meeting_id})["Item"]

    def fv(v):
        if isinstance(v, bool):  return str(v)
        if isinstance(v, float): return f"{v:.2f}"
        return str(v)

    print(f"""
{B}{G}══════════════════════════════════════════════════════════════{NC}
{B}  Pipeline complete  —  record written to DynamoDB by Claude{NC}

    meeting_id       {item['meeting_id']}
    title            {str(item.get('title',''))[:55]}
    call_type        {item.get('call_type','')}
    category         {item.get('category','')}
    sentiment        {fv(item.get('sentiment_score',0))}  (z={fv(item.get('z_score',0))})  anomaly={item.get('is_anomaly',False)}
    escalation       team={item.get('escalation_team','')}  priority={item.get('escalation_priority','')}
    feature_gap      {item.get('feature_gap',False)}
    month            {item.get('month','')}
{B}{G}══════════════════════════════════════════════════════════════{NC}
""")

    # ── Step 6: rebuild QuickSight dashboard ──────────────────────────────────
    step(6, "Rebuilding QuickSight dashboard with latest data")
    rebuild = REPO_ROOT / "scripts" / "rebuild_dashboard.py"
    result  = subprocess.run([sys.executable, str(rebuild)], capture_output=True, text=True)
    for line in result.stdout.strip().splitlines():
        ok(line) if "Done" in line or "created" in line or "updated" in line else info(line)
    if result.returncode != 0:
        print(f"  {R}rebuild_dashboard error:{NC}\n{result.stderr[:300]}")
        return

    print(f"""
  {B}Open dashboard →  {C}{DASHBOARD_URL}{NC}
""")


# ── entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Transcript Intelligence — live AWS pipeline demo")
    parser.add_argument("meeting_id", nargs="?", help="Specific meeting ID to process")
    args = parser.parse_args()

    if args.meeting_id:
        meeting_dir = DATASET_DIR / args.meeting_id
        if not meeting_dir.is_dir():
            print(f"Meeting not found: {args.meeting_id}")
            sys.exit(1)
    else:
        dirs = [d for d in DATASET_DIR.iterdir() if d.is_dir()]
        meeting_dir = random.choice(dirs)

    banner()
    run_demo(meeting_dir)


if __name__ == "__main__":
    main()

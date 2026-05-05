"""
Infrastructure-level configuration -- consumed by CDK stack and app.py only.
Override AWS_ACCOUNT_ID / AWS_REGION via CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION
environment variables in CI so this file never needs editing per-environment.
"""
import os

# --- AWS deployment ---
AWS_ACCOUNT_ID = os.environ["CDK_DEFAULT_ACCOUNT"]  # set via env, never hardcode
AWS_REGION     = os.environ.get("CDK_DEFAULT_REGION",  "us-east-1")

# --- Bedrock ---
BEDROCK_REGION = "us-east-1"
MODEL_FAST     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
MODEL_SONNET   = "us.anthropic.claude-sonnet-4-6"

# --- SSM parameter paths (Bedrock Agent IDs -- set post-deploy, not by CDK) ---
SSM_AGENT_ID_PATH    = "/ti/bedrock-agent-id"
SSM_AGENT_ALIAS_PATH = "/ti/bedrock-agent-alias-id"

# --- S3 ---
RAW_BUCKET_NAME         = "call-transcript-root"
PROCESSED_BUCKET_PREFIX = "call-transcript-processed"

# --- DynamoDB table names ---
TABLE_PII_VAULT  = "ti-pii-vault"
TABLE_LINEAGE    = "ti-lineage"
TABLE_RECORDS    = "ti-records"
TABLE_RECORDS_GSI = "call-type-index"

# --- SQS ---
QUEUE_PIPELINE             = "ti-pipeline-queue"
QUEUE_DLQ                  = "ti-pipeline-dlq"
SQS_VISIBILITY_TIMEOUT_SEC = 900
SQS_DLQ_RETENTION_DAYS    = 14
SQS_MAX_RECEIVE_COUNT      = 3

# --- SNS topics ---
TOPIC_ESCALATIONS = "ti-escalations"
TOPIC_OPS_ALERTS  = "ti-ops-alerts"

# --- CloudWatch alarm ---
DLQ_ALARM_NAME           = "ti-pipeline-dlq-depth"
DLQ_ALARM_THRESHOLD      = 1
DLQ_ALARM_PERIOD_MINUTES = 1

# --- Lambda runtime ---
LAMBDA_RUNTIME = "python3.12"

# timeouts (seconds)
TIMEOUT_INGESTION     = 300
TIMEOUT_ORCHESTRATOR  = 600
TIMEOUT_AGENT_ACTIONS = 60
TIMEOUT_CHATBOT       = 30
TIMEOUT_S3_TRIGGER    = 30

# memory (MB)
MEMORY_INGESTION     = 1024
MEMORY_ORCHESTRATOR  = 1024
MEMORY_AGENT_ACTIONS = 512
MEMORY_CHATBOT       = 512

# --- CloudWatch logs ---
LOG_RETENTION_DAYS = 30  # maps to RetentionDays.ONE_MONTH

# --- S3 trigger ---
RAW_PREFIX         = "raw/"
RAW_TRIGGER_SUFFIX = "meeting-info.json"

# --- SQS event source ---
SQS_BATCH_SIZE = 1

# --- API Gateway ---
API_NAME        = "ti-chatbot"
API_DESCRIPTION = "Transcript Intelligence RAG Chatbot"

# --- QuickSight / Athena analytics ---
GLUE_DATABASE_NAME    = "ti_analytics"
GLUE_TABLE_NAME       = "ti_records_export"
ATHENA_WORKGROUP_NAME = "ti-analytics"
EXPORT_S3_PREFIX      = "exports/records/"
EXPORT_SCHEDULE_CRON  = "cron(0 2 * * ? *)"   # 02:00 UTC daily
TIMEOUT_EXPORT        = 300
MEMORY_EXPORT         = 512

# --- QuickSight embedding ---
QS_EMBED_DASHBOARD_ID = "ti-analytics-dashboard"
QS_EMBED_NAMESPACE    = "default"
QS_SESSION_MINUTES    = 60
TIMEOUT_QS_EMBED      = 15
MEMORY_QS_EMBED       = 256

# --- Analytics & Feedback ---
TABLE_FEEDBACK       = "ti-feedback"
TIMEOUT_ANALYTICS    = 15
MEMORY_ANALYTICS     = 256
TIMEOUT_FEEDBACK     = 15
MEMORY_FEEDBACK      = 256

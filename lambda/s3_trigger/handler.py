import json, os, boto3

sqs = boto3.client("sqs")
QUEUE_URL = os.environ["PIPELINE_QUEUE_URL"]


def lambda_handler(event, context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key    = record["s3"]["object"]["key"]
        # key looks like: raw/MEETING_ID/meeting-info.json
        parts      = key.split("/")
        meeting_id = parts[1] if len(parts) >= 2 else key
        sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps({"bucket": bucket, "meeting_id": meeting_id}),
        )
    return {"status": "queued"}

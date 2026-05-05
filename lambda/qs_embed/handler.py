"""
QuickSight embed URL Lambda -- GET /dashboard

Returns a time-limited anonymous embed URL for the ti-analytics-dashboard.
The client drops this URL into an <iframe> — no AWS credentials required
for the viewer.

Requires QuickSight Enterprise edition with anonymous embedding enabled on
the "default" namespace (one-time console step per account).
"""
import json
import os

import boto3

ACCOUNT_ID    = os.environ["QS_ACCOUNT_ID"]
REGION        = os.environ.get("QS_REGION", "us-east-1")
DASHBOARD_ID  = os.environ["QS_DASHBOARD_ID"]
NAMESPACE     = os.environ.get("QS_NAMESPACE", "default")
SESSION_MINS  = int(os.environ.get("QS_SESSION_MINUTES", "60"))

qs = boto3.client("quicksight", region_name=REGION)

_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
}


def lambda_handler(event, context):
    try:
        resp = qs.generate_embed_url_for_anonymous_user(
            AwsAccountId=ACCOUNT_ID,
            Namespace=NAMESPACE,
            SessionLifetimeInMinutes=SESSION_MINS,
            AuthorizedResourceArns=[
                f"arn:aws:quicksight:{REGION}:{ACCOUNT_ID}:dashboard/{DASHBOARD_ID}"
            ],
            ExperienceConfiguration={
                "Dashboard": {"InitialDashboardId": DASHBOARD_ID}
            },
        )
        return {
            "statusCode": 200,
            "headers": _HEADERS,
            "body": json.dumps({
                "embed_url":  resp["EmbedUrl"],
                "expires_in": SESSION_MINS * 60,
            }),
        }
    except Exception as exc:
        return {
            "statusCode": 500,
            "headers": _HEADERS,
            "body": json.dumps({"error": str(exc)}),
        }

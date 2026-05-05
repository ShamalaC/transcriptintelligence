#!/usr/bin/env python3
import aws_cdk as cdk
from transcript_intelligence.transcript_intelligence_stack import TranscriptIntelligenceStack
from config.app_config import AWS_ACCOUNT_ID, AWS_REGION

app = cdk.App()
TranscriptIntelligenceStack(
    app, "TranscriptIntelligenceStack",
    env=cdk.Environment(account=AWS_ACCOUNT_ID, region=AWS_REGION),
)
app.synth()

import aws_cdk as core
import aws_cdk.assertions as assertions

from transcript_intelligence.transcript_intelligence_stack import TranscriptIntelligenceStack

# example tests. To run these tests, uncomment this file along with the example
# resource in transcript_intelligence/transcript_intelligence_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = TranscriptIntelligenceStack(app, "transcript-intelligence")
    template = assertions.Template.from_stack(stack)

#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })

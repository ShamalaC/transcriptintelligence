"""
CDK stack assertions - verifies that CloudFormation resources are synthesised
with the correct configuration.  No AWS calls required.
"""
import aws_cdk as cdk
import aws_cdk.assertions as assertions
import pytest

from transcript_intelligence.transcript_intelligence_stack import TranscriptIntelligenceStack


@pytest.fixture(scope="module")
def template():
    app   = cdk.App()
    stack = TranscriptIntelligenceStack(app, "TITest")
    return assertions.Template.from_stack(stack)


@pytest.fixture(scope="module")
def template_qs():
    """Stack synthesised with qs_user context -- enables QuickSight custom resource."""
    app   = cdk.App(context={"qs_user": "default/test-admin"})
    stack = TranscriptIntelligenceStack(app, "TITestQS")
    return assertions.Template.from_stack(stack)


# --- SQS ---

class TestSQS:
    def test_pipeline_queue_exists(self, template):
        template.has_resource_properties("AWS::SQS::Queue", {
            "QueueName": "ti-pipeline-queue",
            "VisibilityTimeout": 900,
        })

    def test_dlq_exists(self, template):
        template.has_resource_properties("AWS::SQS::Queue", {
            "QueueName": "ti-pipeline-dlq",
            "MessageRetentionPeriod": 1209600,  # 14 days
        })

    def test_pipeline_queue_has_redrive(self, template):
        template.has_resource_properties("AWS::SQS::Queue", {
            "QueueName": "ti-pipeline-queue",
            "RedrivePolicy": assertions.Match.object_like({
                "maxReceiveCount": 3,
            }),
        })


# --- DynamoDB ---

class TestDynamoDB:
    def test_records_table(self, template):
        template.has_resource_properties("AWS::DynamoDB::Table", {
            "TableName": "ti-records",
            "BillingMode": "PAY_PER_REQUEST",
        })

    def test_pii_vault_pitr_enabled(self, template):
        template.has_resource_properties("AWS::DynamoDB::Table", {
            "TableName": "ti-pii-vault",
            "PointInTimeRecoverySpecification": {
                "PointInTimeRecoveryEnabled": True,
            },
        })

    def test_records_table_has_gsi(self, template):
        template.has_resource_properties("AWS::DynamoDB::Table", {
            "TableName": "ti-records",
            "GlobalSecondaryIndexes": assertions.Match.array_with([
                assertions.Match.object_like({"IndexName": "call-type-index"})
            ]),
        })

    def test_lineage_table(self, template):
        template.has_resource_properties("AWS::DynamoDB::Table", {
            "TableName": "ti-lineage",
        })


# --- Lambda ---

class TestLambda:
    def _fn(self, template, name):
        resources = template.find_resources("AWS::Lambda::Function", {
            "Properties": {"FunctionName": name}
        })
        assert resources, f"Lambda {name!r} not found in template"
        return list(resources.values())[0]

    def test_all_expected_functions_exist(self, template):
        for name in [
            "ti-ingestion_pii", "ti-orchestrator", "ti-agent-actions",
            "ti-chatbot", "ti-s3-trigger",
            "ti-export", "ti-qs-embed",
        ]:
            self._fn(template, name)

    def test_ingestion_pii_runtime(self, template):
        fn = self._fn(template, "ti-ingestion_pii")
        assert fn["Properties"]["Runtime"] == "python3.12"

    def test_ingestion_pii_timeout(self, template):
        fn = self._fn(template, "ti-ingestion_pii")
        assert fn["Properties"]["Timeout"] == 300

    def test_orchestrator_timeout(self, template):
        fn = self._fn(template, "ti-orchestrator")
        assert fn["Properties"]["Timeout"] == 600

    def test_xray_tracing_active(self, template):
        # All business Lambdas should have X-Ray active (5 core + ti-export + ti-qs-embed = 7)
        resources = template.find_resources("AWS::Lambda::Function", {
            "Properties": {"TracingConfig": {"Mode": "Active"}}
        })
        assert len(resources) >= 7, "Expected all 7 Lambdas to have X-Ray tracing"

    def test_ingestion_pii_has_ssm_env_vars(self, template):
        fn = self._fn(template, "ti-ingestion_pii")
        env_vars = fn["Properties"]["Environment"]["Variables"]
        assert "SSM_AGENT_ID_PATH"    in env_vars
        assert "SSM_AGENT_ALIAS_PATH" in env_vars

    def test_no_plaintext_agent_ids(self, template):
        fn = self._fn(template, "ti-ingestion_pii")
        env_vars = fn["Properties"]["Environment"]["Variables"]
        assert "BEDROCK_AGENT_ID"       not in env_vars
        assert "BEDROCK_AGENT_ALIAS_ID" not in env_vars

    def test_dead_lambdas_removed(self, template):
        for name in [
            "ti-categorization", "ti-escalation", "ti-feature_extraction",
            "ti-issue_extraction", "ti-sentiment", "ti-store_results",
        ]:
            resources = template.find_resources("AWS::Lambda::Function", {
                "Properties": {"FunctionName": name}
            })
            assert not resources, f"Dead Lambda {name!r} should not exist in template"


# --- IAM ---

class TestIAM:
    def test_no_marketplace_policy(self, template):
        policies = template.find_resources("AWS::IAM::Policy")
        for _, policy in policies.items():
            stmts = policy.get("Properties", {}).get("PolicyDocument", {}).get("Statement", [])
            for stmt in stmts:
                actions = stmt.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                for action in actions:
                    assert not action.startswith("aws-marketplace:"), \
                        f"Marketplace policy found - should have been removed: {action}"

    def test_bedrock_policy_not_wildcard_resource(self, template):
        policies = template.find_resources("AWS::IAM::Policy")
        for _, policy in policies.items():
            stmts = policy.get("Properties", {}).get("PolicyDocument", {}).get("Statement", [])
            for stmt in stmts:
                actions = stmt.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                bedrock_actions = [a for a in actions if "bedrock:InvokeModel" in a]
                if bedrock_actions:
                    resources = stmt.get("Resource", [])
                    if isinstance(resources, str):
                        resources = [resources]
                    assert "*" not in resources, \
                        "bedrock:InvokeModel must not have wildcard resource"


# --- Observability ---

class TestObservability:
    def test_dlq_alarm_exists(self, template):
        template.has_resource_properties("AWS::CloudWatch::Alarm", {
            "AlarmName": "ti-pipeline-dlq-depth",
            "Threshold": 1,
            "EvaluationPeriods": 1,
        })

    def test_ops_sns_topic_exists(self, template):
        template.has_resource_properties("AWS::SNS::Topic", {
            "TopicName": "ti-ops-alerts",
        })

    def test_escalation_sns_topic_exists(self, template):
        template.has_resource_properties("AWS::SNS::Topic", {
            "TopicName": "ti-escalations",
        })


# --- API Gateway ---

class TestAPIGateway:
    def test_rest_api_exists(self, template):
        template.has_resource_properties("AWS::ApiGateway::RestApi", {
            "Name": "ti-chatbot",
        })


# --- Analytics Pipeline (Glue / Athena / EventBridge / export Lambda) ---

class TestAnalyticsPipeline:
    def test_glue_database_exists(self, template):
        template.has_resource_properties("AWS::Glue::Database", {
            "DatabaseInput": {"Name": "ti_analytics"},
        })

    def test_glue_table_exists(self, template):
        template.has_resource_properties("AWS::Glue::Table", {
            "DatabaseName": "ti_analytics",
            "TableInput": assertions.Match.object_like({
                "Name":      "ti_records_export",
                "TableType": "EXTERNAL_TABLE",
            }),
        })

    def test_glue_table_partition_projection_enabled(self, template):
        tables = template.find_resources("AWS::Glue::Table", {
            "Properties": {"DatabaseName": "ti_analytics"}
        })
        assert tables, "Glue table ti_records_export not found"
        params = list(tables.values())[0]["Properties"]["TableInput"]["Parameters"]
        assert params["projection.enabled"]   == "true"
        assert params["projection.date.type"] == "date"
        assert "storage.location.template" in params

    def test_athena_workgroup_exists(self, template):
        template.has_resource_properties("AWS::Athena::WorkGroup", {
            "Name": "ti-analytics",
        })

    def test_athena_workgroup_output_to_processed_bucket(self, template):
        groups = template.find_resources("AWS::Athena::WorkGroup", {
            "Properties": {"Name": "ti-analytics"}
        })
        assert groups
        cfg = list(groups.values())[0]["Properties"]["WorkGroupConfiguration"]
        location = cfg["ResultConfiguration"]["OutputLocation"]
        # CDK synthesises this as Fn::Join -- stringify and check the suffix is present
        assert "athena-results" in str(location)

    def test_export_lambda_exists(self, template):
        template.has_resource_properties("AWS::Lambda::Function", {
            "FunctionName": "ti-export",
            "Runtime":      "python3.12",
            "Timeout":      300,
        })

    def test_export_lambda_xray_active(self, template):
        resources = template.find_resources("AWS::Lambda::Function", {
            "Properties": {"FunctionName": "ti-export"}
        })
        fn = list(resources.values())[0]
        assert fn["Properties"]["TracingConfig"]["Mode"] == "Active"

    def test_nightly_eventbridge_rule_exists(self, template):
        template.has_resource_properties("AWS::Events::Rule", {
            "Name":               "ti-nightly-export",
            "ScheduleExpression": "cron(0 2 * * ? *)",
        })

    def test_quicksight_datasource_exists(self, template):
        template.has_resource_properties("AWS::QuickSight::DataSource", {
            "Type":         "ATHENA",
            "DataSourceId": "ti-athena-datasource",
        })

    def test_s3_bucket_policy_grants_quicksight_read(self, template):
        policies = template.find_resources("AWS::S3::BucketPolicy")
        found = False
        for policy in policies.values():
            stmts = policy["Properties"]["PolicyDocument"]["Statement"]
            for stmt in stmts:
                principals = stmt.get("Principal", {})
                if isinstance(principals, dict):
                    svc = principals.get("Service", "")
                    if "quicksight.amazonaws.com" in svc:
                        found = True
        assert found, "S3 bucket policy must grant quicksight.amazonaws.com read access"


# --- QuickSight Auto Setup Custom Resource ---

class TestQuickSightAutoSetup:
    def test_qs_setup_lambda_exists_when_qs_user_set(self, template_qs):
        resources = template_qs.find_resources("AWS::Lambda::Function", {
            "Properties": {"FunctionName": "ti-qs-setup"}
        })
        assert resources, "ti-qs-setup Lambda should be created when qs_user context is set"

    def test_qs_setup_lambda_absent_without_qs_user(self, template):
        resources = template.find_resources("AWS::Lambda::Function", {
            "Properties": {"FunctionName": "ti-qs-setup"}
        })
        assert not resources, "ti-qs-setup Lambda must not exist when qs_user context is absent"

    def test_qs_user_arn_injected_into_env(self, template_qs):
        resources = template_qs.find_resources("AWS::Lambda::Function", {
            "Properties": {"FunctionName": "ti-qs-setup"}
        })
        env_vars = list(resources.values())[0]["Properties"]["Environment"]["Variables"]
        assert "QS_USER_ARN" in env_vars
        # CDK synthesises this as Fn::Join -- stringify to check key segments are present
        arn_repr = str(env_vars["QS_USER_ARN"])
        assert "test-admin" in arn_repr
        assert "quicksight" in arn_repr

    def test_glue_and_athena_env_vars_present(self, template_qs):
        resources = template_qs.find_resources("AWS::Lambda::Function", {
            "Properties": {"FunctionName": "ti-qs-setup"}
        })
        env_vars = list(resources.values())[0]["Properties"]["Environment"]["Variables"]
        assert env_vars["GLUE_DATABASE_NAME"]    == "ti_analytics"
        assert env_vars["GLUE_TABLE_NAME"]       == "ti_records_export"
        assert env_vars["ATHENA_WORKGROUP_NAME"]  == "ti-analytics"

    def test_qs_setup_has_xray_tracing(self, template_qs):
        resources = template_qs.find_resources("AWS::Lambda::Function", {
            "Properties": {"FunctionName": "ti-qs-setup"}
        })
        fn = list(resources.values())[0]
        assert fn["Properties"]["TracingConfig"]["Mode"] == "Active"

    def test_custom_resource_exists_with_qs_user(self, template_qs):
        # cr.Provider creates a Custom Resource that CloudFormation manages
        resources = template_qs.find_resources("AWS::CloudFormation::CustomResource")
        assert resources, "QsSetup CustomResource should exist when qs_user context is set"

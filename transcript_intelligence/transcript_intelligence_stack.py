from aws_cdk import (
    Stack, Duration, RemovalPolicy, CustomResource,
    aws_s3 as s3,
    aws_sqs as sqs,
    aws_dynamodb as dynamodb,
    aws_lambda as lambda_,
    aws_apigateway as apigw,
    aws_sns as sns,
    aws_s3_notifications as s3n,
    aws_lambda_event_sources as event_sources,
    aws_iam as iam,
    aws_logs as logs,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_glue as glue,
    aws_athena as athena,
    aws_events as events,
    aws_events_targets as targets,
    aws_quicksight as quicksight,
    custom_resources as cr,
    triggers,
    CfnOutput,
)
from constructs import Construct
from config.app_config import (
    BEDROCK_REGION, MODEL_FAST, MODEL_SONNET,
    SSM_AGENT_ID_PATH, SSM_AGENT_ALIAS_PATH,
    RAW_BUCKET_NAME, PROCESSED_BUCKET_PREFIX,
    TABLE_PII_VAULT, TABLE_LINEAGE, TABLE_RECORDS, TABLE_RECORDS_GSI,
    TABLE_FEEDBACK,
    QUEUE_PIPELINE, QUEUE_DLQ,
    SQS_VISIBILITY_TIMEOUT_SEC, SQS_DLQ_RETENTION_DAYS, SQS_MAX_RECEIVE_COUNT,
    TOPIC_ESCALATIONS, TOPIC_OPS_ALERTS,
    DLQ_ALARM_NAME, DLQ_ALARM_THRESHOLD, DLQ_ALARM_PERIOD_MINUTES,
    TIMEOUT_INGESTION, TIMEOUT_ORCHESTRATOR, TIMEOUT_AGENT_ACTIONS,
    TIMEOUT_CHATBOT, TIMEOUT_S3_TRIGGER,
    TIMEOUT_EXPORT, TIMEOUT_QS_EMBED,
    TIMEOUT_ANALYTICS, TIMEOUT_FEEDBACK,
    MEMORY_INGESTION, MEMORY_ORCHESTRATOR, MEMORY_AGENT_ACTIONS,
    MEMORY_CHATBOT, MEMORY_EXPORT, MEMORY_QS_EMBED,
    MEMORY_ANALYTICS, MEMORY_FEEDBACK,
    QS_EMBED_DASHBOARD_ID, QS_EMBED_NAMESPACE, QS_SESSION_MINUTES,
    RAW_PREFIX, RAW_TRIGGER_SUFFIX, SQS_BATCH_SIZE,
    API_NAME, API_DESCRIPTION,
    GLUE_DATABASE_NAME, GLUE_TABLE_NAME, ATHENA_WORKGROUP_NAME,
    EXPORT_S3_PREFIX, EXPORT_SCHEDULE_CRON,
)


class TranscriptIntelligenceStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- S3 Buckets ---
        raw_bucket = s3.Bucket.from_bucket_name(self, "RawBucket", RAW_BUCKET_NAME)

        processed_bucket = s3.Bucket(
            self, "ProcessedBucket",
            bucket_name=f"{PROCESSED_BUCKET_PREFIX}-{self.account}",
            removal_policy=RemovalPolicy.RETAIN,
            versioned=True,
        )

        # --- DynamoDB Tables ---
        pii_vault_table = dynamodb.Table(
            self, "PIIVaultTable",
            table_name=TABLE_PII_VAULT,
            partition_key=dynamodb.Attribute(name="placeholder", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
        )

        lineage_table = dynamodb.Table(
            self, "LineageTable",
            table_name=TABLE_LINEAGE,
            partition_key=dynamodb.Attribute(name="meeting_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="stage", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        records_table = dynamodb.Table(
            self, "RecordsTable",
            table_name=TABLE_RECORDS,
            partition_key=dynamodb.Attribute(name="meeting_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            stream=dynamodb.StreamViewType.NEW_IMAGE,
        )
        feedback_table = dynamodb.Table(
            self, "FeedbackTable",
            table_name=TABLE_FEEDBACK,
            partition_key=dynamodb.Attribute(name="meeting_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="submitted_at",   type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # GSI: call_type -> processed_at - O(1) count per type without a full table scan
        records_table.add_global_secondary_index(
            index_name=TABLE_RECORDS_GSI,
            partition_key=dynamodb.Attribute(name="call_type",   type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(    name="processed_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.INCLUDE,
            non_key_attributes=["title", "category", "sentiment_score"],
        )

        # --- SQS Queue ---
        dlq = sqs.Queue(
            self, "DLQ",
            queue_name=QUEUE_DLQ,
            retention_period=Duration.days(SQS_DLQ_RETENTION_DAYS),
        )

        pipeline_queue = sqs.Queue(
            self, "PipelineQueue",
            queue_name=QUEUE_PIPELINE,
            visibility_timeout=Duration.seconds(SQS_VISIBILITY_TIMEOUT_SEC),
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=SQS_MAX_RECEIVE_COUNT, queue=dlq
            ),
        )

        # --- DLQ Alarm - fires the moment a message lands in the DLQ ---
        ops_topic = sns.Topic(self, "OpsTopic", topic_name=TOPIC_OPS_ALERTS)
        dlq_alarm = cloudwatch.Alarm(
            self, "DLQAlarm",
            alarm_name=DLQ_ALARM_NAME,
            alarm_description="A meeting failed processing 3x and landed in the DLQ",
            metric=dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(DLQ_ALARM_PERIOD_MINUTES),
                statistic="Sum",
            ),
            threshold=DLQ_ALARM_THRESHOLD,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        dlq_alarm.add_alarm_action(cw_actions.SnsAction(ops_topic))

        # --- SNS Topics ---
        escalation_topic = sns.Topic(self, "EscalationTopic", topic_name=TOPIC_ESCALATIONS)

        # --- IAM policies (least-privilege) ---
        # Scoped to Anthropic Claude models - not wildcard *
        bedrock_model_policy = iam.PolicyStatement(
            sid="BedrockClaudeModels",
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                f"arn:aws:bedrock:{BEDROCK_REGION}::foundation-model/anthropic.claude-*",
                # Cross-region inference profiles resolve across partitions
                f"arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
                f"arn:aws:bedrock:{BEDROCK_REGION}:{self.account}:inference-profile/*",
            ],
        )

        # Scoped to agents in this account - ingestion Lambda only
        bedrock_agent_invoke_policy = iam.PolicyStatement(
            sid="BedrockAgentInvoke",
            actions=["bedrock-agent-runtime:InvokeAgent"],
            resources=[
                f"arn:aws:bedrock:{BEDROCK_REGION}:{self.account}:agent/*",
                f"arn:aws:bedrock:{BEDROCK_REGION}:{self.account}:agent-alias/*/*",
            ],
        )

        # SSM read - agent IDs stored outside CDK so re-deploys don't reset them
        ssm_agent_ids_policy = iam.PolicyStatement(
            sid="SSMAgentIds",
            actions=["ssm:GetParameter", "ssm:GetParameters"],
            resources=[
                f"arn:aws:ssm:{BEDROCK_REGION}:{self.account}:parameter{SSM_AGENT_ID_PATH}",
                f"arn:aws:ssm:{BEDROCK_REGION}:{self.account}:parameter{SSM_AGENT_ALIAS_PATH}",
            ],
        )

        cloudwatch_policy = iam.PolicyStatement(
            sid="CWMetrics",
            actions=["cloudwatch:PutMetricData"],
            resources=["*"],  # PutMetricData has no resource-level restriction
        )

        common_env = {
            "PROCESSED_BUCKET":  processed_bucket.bucket_name,
            "PII_VAULT_TABLE":   pii_vault_table.table_name,
            "LINEAGE_TABLE":     lineage_table.table_name,
            "RECORDS_TABLE":     records_table.table_name,
            "BEDROCK_REGION":    BEDROCK_REGION,
            "MODEL_FAST":        MODEL_FAST,
            "MODEL_SONNET":      MODEL_SONNET,
            "ESCALATION_TOPIC":  escalation_topic.topic_arn,
        }

        # --- Lambda Layers ---
        shared_layer = lambda_.LayerVersion(
            self, "SharedUtilsLayer",
            layer_version_name="ti-shared-utils",
            description="Shared classifier, constants (TAXONOMY/ESCALATION_RULES), StructuredLogger",
            code=lambda_.Code.from_asset("lambda/shared"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            removal_policy=RemovalPolicy.RETAIN,
        )

        anthropic_layer = lambda_.LayerVersion(
            self, "AnthropicLayer",
            layer_version_name="ti-anthropic-sdk",
            description="Anthropic Python SDK for Claude Agent tool use",
            code=lambda_.Code.from_asset("lambda/anthropic_layer"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            removal_policy=RemovalPolicy.RETAIN,
        )

        # --- Lambda factory - shared grants, X-Ray tracing enabled by default ---
        def make_lambda(lid, folder, timeout=300, memory=512, extra_env=None,
                        extra_layers=None, reserved_concurrency=None):
            env = {**common_env, **(extra_env or {})}
            kwargs = {}
            if reserved_concurrency is not None:
                kwargs["reserved_concurrent_executions"] = reserved_concurrency
            fn = lambda_.Function(
                self, lid,
                function_name=f"ti-{folder}",
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler="handler.lambda_handler",
                code=lambda_.Code.from_asset(f"lambda/{folder}"),
                timeout=Duration.seconds(timeout),
                memory_size=memory,
                environment=env,
                layers=[shared_layer] + (extra_layers or []),
                tracing=lambda_.Tracing.ACTIVE,
                log_group=logs.LogGroup(
                    self, f"LG{lid}",
                    removal_policy=RemovalPolicy.DESTROY,
                    retention=logs.RetentionDays.ONE_MONTH,
                ),
                **kwargs,
            )
            fn.add_to_role_policy(cloudwatch_policy)
            processed_bucket.grant_read_write(fn)
            pii_vault_table.grant_read_write_data(fn)
            lineage_table.grant_read_write_data(fn)
            records_table.grant_read_write_data(fn)
            return fn

        # --- Lambda Functions ---
        # NOTE: reserved_concurrency omitted - this account's total concurrency is 10.
        # In a production account (default 1000), add reserved_concurrency=10 here
        # to prevent bulk S3 uploads from exhausting Bedrock throughput.
        ingestion_fn = make_lambda(
            "IngestionPII", "ingestion_pii",
            timeout=TIMEOUT_INGESTION, memory=MEMORY_INGESTION,
        )
        # ingestion_pii invokes the Bedrock Agent (not the model directly)
        ingestion_fn.add_to_role_policy(bedrock_agent_invoke_policy)
        # Reads agent IDs from SSM at cold start
        ingestion_fn.add_to_role_policy(ssm_agent_ids_policy)
        ingestion_fn.add_environment("SSM_AGENT_ID_PATH",    SSM_AGENT_ID_PATH)
        ingestion_fn.add_environment("SSM_AGENT_ALIAS_PATH", SSM_AGENT_ALIAS_PATH)

        chatbot_fn = make_lambda(
            "Chatbot", "chatbot",
            timeout=TIMEOUT_CHATBOT, memory=MEMORY_CHATBOT,
        )
        chatbot_fn.add_to_role_policy(bedrock_model_policy)

        raw_bucket.grant_read(ingestion_fn)

        analytics_fn = make_lambda(
            "Analytics", "analytics",
            timeout=TIMEOUT_ANALYTICS, memory=MEMORY_ANALYTICS,
        )

        feedback_fn = make_lambda(
            "Feedback", "feedback",
            timeout=TIMEOUT_FEEDBACK, memory=MEMORY_FEEDBACK,
            extra_env={"FEEDBACK_TABLE": feedback_table.table_name},
        )
        feedback_table.grant_read_write_data(feedback_fn)

        # --- Orchestrator Lambda (Claude Agent SDK - fallback path) ---
        orchestrator_fn = lambda_.Function(
            self, "Orchestrator",
            function_name="ti-orchestrator",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda/orchestrator"),
            timeout=Duration.seconds(TIMEOUT_ORCHESTRATOR),
            memory_size=MEMORY_ORCHESTRATOR,
            environment={**common_env},
            layers=[shared_layer, anthropic_layer],
            tracing=lambda_.Tracing.ACTIVE,
            log_group=logs.LogGroup(
                self, "LGOrchestrator",
                removal_policy=RemovalPolicy.DESTROY,
                retention=logs.RetentionDays.ONE_MONTH,
            ),
        )
        orchestrator_fn.add_to_role_policy(bedrock_model_policy)
        orchestrator_fn.add_to_role_policy(cloudwatch_policy)
        processed_bucket.grant_read_write(orchestrator_fn)
        records_table.grant_read_write_data(orchestrator_fn)
        lineage_table.grant_read_write_data(orchestrator_fn)
        escalation_topic.grant_publish(orchestrator_fn)

        orchestrator_fn.grant_invoke(ingestion_fn)
        ingestion_fn.add_environment("ORCHESTRATOR_FUNCTION_NAME", orchestrator_fn.function_name)

        # --- Agent Actions Lambda (Bedrock Agent action group handler) ---
        # Pure logic - no Bedrock model calls; does not receive bedrock_model_policy.
        agent_actions_fn = lambda_.Function(
            self, "AgentActions",
            function_name="ti-agent-actions",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda/agent_actions"),
            timeout=Duration.seconds(TIMEOUT_AGENT_ACTIONS),
            memory_size=MEMORY_AGENT_ACTIONS,
            environment={**common_env},
            layers=[shared_layer],
            tracing=lambda_.Tracing.ACTIVE,
            log_group=logs.LogGroup(
                self, "LGAgentActions",
                removal_policy=RemovalPolicy.DESTROY,
                retention=logs.RetentionDays.ONE_MONTH,
            ),
        )
        agent_actions_fn.add_to_role_policy(cloudwatch_policy)
        processed_bucket.grant_read_write(agent_actions_fn)
        records_table.grant_read_write_data(agent_actions_fn)
        lineage_table.grant_read_write_data(agent_actions_fn)
        escalation_topic.grant_publish(agent_actions_fn)

        # --- S3 Trigger Lambda (S3 event -> SQS) ---
        s3_trigger_fn = lambda_.Function(
            self, "S3Trigger",
            function_name="ti-s3-trigger",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda/s3_trigger"),
            timeout=Duration.seconds(TIMEOUT_S3_TRIGGER),
            environment={"PIPELINE_QUEUE_URL": pipeline_queue.queue_url},
            tracing=lambda_.Tracing.ACTIVE,
            log_group=logs.LogGroup(
                self, "LGS3Trigger",
                removal_policy=RemovalPolicy.DESTROY,
                retention=logs.RetentionDays.ONE_MONTH,
            ),
        )
        pipeline_queue.grant_send_messages(s3_trigger_fn)

        raw_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(s3_trigger_fn),
            s3.NotificationKeyFilter(prefix=RAW_PREFIX, suffix=RAW_TRIGGER_SUFFIX),
        )

        ingestion_fn.add_event_source(
            event_sources.SqsEventSource(pipeline_queue, batch_size=SQS_BATCH_SIZE)
        )

        # --- API Gateway ---
        api = apigw.RestApi(
            self, "ChatbotAPI",
            rest_api_name=API_NAME,
            description=API_DESCRIPTION,
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["GET", "POST", "OPTIONS"],
            ),
        )
        api.root.add_resource("chat").add_method(
            "POST", apigw.LambdaIntegration(chatbot_fn)
        )

        analytics_resource = api.root.add_resource("analytics")
        analytics_resource.add_resource("call-type-distribution").add_method(
            "GET", apigw.LambdaIntegration(analytics_fn)
        )

        api.root.add_resource("feedback").add_method(
            "POST", apigw.LambdaIntegration(feedback_fn)
        )

        # --- QuickSight embed endpoint ---
        qs_embed_fn = lambda_.Function(
            self, "QsEmbed",
            function_name="ti-qs-embed",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda/qs_embed"),
            timeout=Duration.seconds(TIMEOUT_QS_EMBED),
            memory_size=MEMORY_QS_EMBED,
            environment={
                "QS_ACCOUNT_ID":      self.account,
                "QS_REGION":          self.region,
                "QS_DASHBOARD_ID":    QS_EMBED_DASHBOARD_ID,
                "QS_NAMESPACE":       QS_EMBED_NAMESPACE,
                "QS_SESSION_MINUTES": str(QS_SESSION_MINUTES),
            },
            tracing=lambda_.Tracing.ACTIVE,
            log_group=logs.LogGroup(
                self, "LGQsEmbed",
                removal_policy=RemovalPolicy.DESTROY,
                retention=logs.RetentionDays.ONE_MONTH,
            ),
        )
        qs_embed_fn.add_to_role_policy(iam.PolicyStatement(
            sid="QuickSightEmbed",
            actions=["quicksight:GenerateEmbedUrlForAnonymousUser"],
            resources=[
                f"arn:aws:quicksight:{self.region}:{self.account}:dashboard/{QS_EMBED_DASHBOARD_ID}",
                f"arn:aws:quicksight:{self.region}:{self.account}:namespace/{QS_EMBED_NAMESPACE}",
            ],
        ))
        api.root.add_resource("dashboard").add_method(
            "GET", apigw.LambdaIntegration(qs_embed_fn)
        )
        # --- QuickSight Analytics Pipeline ---

        # Glue database (logical container for the Athena table)
        glue_db = glue.CfnDatabase(
            self, "GlueAnalyticsDB",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=GLUE_DATABASE_NAME,
                description="Transcript Intelligence daily record exports",
            ),
        )

        # Glue table - schema matches flattened ti-records export
        glue_table = glue.CfnTable(
            self, "GlueRecordsTable",
            catalog_id=self.account,
            database_name=GLUE_DATABASE_NAME,
            table_input=glue.CfnTable.TableInputProperty(
                name=GLUE_TABLE_NAME,
                description="Daily NDJSON export of ti-records for QuickSight",
                table_type="EXTERNAL_TABLE",
                parameters={
                    "classification":                "json",
                    "typeOfData":                    "file",
                    # Partition projection: Athena generates date partition values
                    # dynamically -- no MSCK REPAIR TABLE or manual AddPartition needed.
                    "projection.enabled":            "true",
                    "projection.date.type":          "date",
                    "projection.date.range":         "2024-01-01,NOW",
                    "projection.date.format":        "yyyy-MM-dd",
                    "projection.date.interval":      "1",
                    "projection.date.interval.unit": "DAYS",
                    "storage.location.template":
                        f"s3://{processed_bucket.bucket_name}/{EXPORT_S3_PREFIX}date=${{date}}/",
                },
                partition_keys=[
                    glue.CfnTable.ColumnProperty(name="date", type="string"),
                ],
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=f"s3://{processed_bucket.bucket_name}/{EXPORT_S3_PREFIX}",
                    input_format="org.apache.hadoop.mapred.TextInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.openx.data.jsonserde.JsonSerDe",
                    ),
                    columns=[
                        glue.CfnTable.ColumnProperty(name="meeting_id",         type="string"),
                        glue.CfnTable.ColumnProperty(name="title",              type="string"),
                        glue.CfnTable.ColumnProperty(name="call_type",          type="string"),
                        glue.CfnTable.ColumnProperty(name="primary_category",   type="string"),
                        glue.CfnTable.ColumnProperty(name="sentiment_score",    type="double"),
                        glue.CfnTable.ColumnProperty(name="sentiment_label",    type="string"),
                        glue.CfnTable.ColumnProperty(name="is_anomaly",         type="boolean"),
                        glue.CfnTable.ColumnProperty(name="z_score",            type="double"),
                        glue.CfnTable.ColumnProperty(name="severity",           type="string"),
                        glue.CfnTable.ColumnProperty(name="escalation_team",    type="string"),
                        glue.CfnTable.ColumnProperty(name="requires_escalation",type="boolean"),
                        glue.CfnTable.ColumnProperty(name="processed_at",       type="string"),
                        glue.CfnTable.ColumnProperty(name="model",              type="string"),
                    ],
                ),
            ),
        )
        glue_table.add_dependency(glue_db)

        # Athena workgroup - query output written back to processed bucket
        athena_workgroup = athena.CfnWorkGroup(
            self, "AthenaWorkgroup",
            name=ATHENA_WORKGROUP_NAME,
            description="Transcript Intelligence analytics queries",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{processed_bucket.bucket_name}/athena-results/",
                ),
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=True,
            ),
            recursive_delete_option=True,
        )

        # Export Lambda - full DynamoDB scan -> NDJSON -> S3
        export_fn = lambda_.Function(
            self, "ExportFn",
            function_name="ti-export",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambda/export"),
            timeout=Duration.seconds(TIMEOUT_EXPORT),
            memory_size=MEMORY_EXPORT,
            environment={
                **common_env,
                "EXPORT_S3_PREFIX": EXPORT_S3_PREFIX,
            },
            layers=[shared_layer],
            tracing=lambda_.Tracing.ACTIVE,
            log_group=logs.LogGroup(
                self, "LGExportFn",
                removal_policy=RemovalPolicy.DESTROY,
                retention=logs.RetentionDays.ONE_MONTH,
            ),
        )
        export_fn.add_to_role_policy(cloudwatch_policy)
        processed_bucket.grant_read_write(export_fn)
        records_table.grant_read_data(export_fn)

        # EventBridge rule - nightly at 02:00 UTC
        events.Rule(
            self, "NightlyExportRule",
            rule_name="ti-nightly-export",
            description="Trigger DynamoDB -> S3 export for QuickSight at 02:00 UTC",
            schedule=events.Schedule.expression(EXPORT_SCHEDULE_CRON),
            targets=[targets.LambdaFunction(export_fn)],
        )

        # CDK Trigger: run the export Lambda once immediately after deploy so
        # QuickSight has data from day one without waiting for 02:00 UTC.
        initial_export = triggers.Trigger(
            self, "InitialExportTrigger",
            handler=export_fn,
            execute_after=[export_fn, records_table, processed_bucket],
        )

        # QuickSight data source - Athena connector.
        # Grant the QuickSight managed service role (aws-quicksight-service-role-v0) read on
        # exports and read+write on athena-results (Athena writes query output there on behalf
        # of QuickSight). Using the role ARN directly because it is the actual caller; the
        # quicksight.amazonaws.com service principal alone does not satisfy S3 bucket policies
        # when Athena is the execution engine.
        qs_service_role_arn = f"arn:aws:iam::{self.account}:role/service-role/aws-quicksight-service-role-v0"
        qs_role_principal = iam.ArnPrincipal(qs_service_role_arn)

        processed_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="QuickSightS3Read",
                actions=["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"],
                principals=[qs_role_principal],
                resources=[
                    processed_bucket.bucket_arn,
                    f"{processed_bucket.bucket_arn}/{EXPORT_S3_PREFIX}*",
                    f"{processed_bucket.bucket_arn}/athena-results/*",
                ],
            )
        )
        processed_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="QuickSightAthenaResultsWrite",
                actions=["s3:PutObject", "s3:AbortMultipartUpload"],
                principals=[qs_role_principal],
                resources=[
                    f"{processed_bucket.bucket_arn}/athena-results/*",
                ],
            )
        )

        qs_datasource = quicksight.CfnDataSource(
            self, "QuickSightDataSource",
            aws_account_id=self.account,
            data_source_id="ti-athena-datasource",
            name="Transcript Intelligence - Athena",
            type="ATHENA",
            data_source_parameters=quicksight.CfnDataSource.DataSourceParametersProperty(
                athena_parameters=quicksight.CfnDataSource.AthenaParametersProperty(
                    work_group=ATHENA_WORKGROUP_NAME,
                ),
            ),
            ssl_properties=quicksight.CfnDataSource.SslPropertiesProperty(
                disable_ssl=False,
            ),
        )
        qs_datasource.add_dependency(athena_workgroup)
        # Ensure bucket policy is fully applied before QuickSight runs its connection test.
        qs_datasource.node.add_dependency(processed_bucket)

        # QuickSight dataset + dashboard -- automated via CDK Custom Resource.
        # Set qs_user in cdk.json context (format: "namespace/username", e.g. "default/admin")
        # or pass at deploy time: cdk deploy -c qs_user=default/admin
        qs_user = self.node.try_get_context("qs_user") or ""
        if qs_user:
            parts = qs_user.split("/", 1)
            qs_namespace = parts[0] if len(parts) == 2 else "default"
            qs_username  = parts[1] if len(parts) == 2 else parts[0]
            qs_user_arn  = (
                f"arn:aws:quicksight:{self.region}:{self.account}"
                f":user/{qs_namespace}/{qs_username}"
            )

            qs_setup_fn = lambda_.Function(
                self, "QsSetupFn",
                function_name="ti-qs-setup",
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler="handler.on_event",
                code=lambda_.Code.from_asset("lambda/qs_setup"),
                timeout=Duration.minutes(5),
                memory_size=256,
                tracing=lambda_.Tracing.ACTIVE,
                environment={
                    "QS_ACCOUNT_ID":         self.account,
                    "QS_REGION":             self.region,
                    "QS_USER_ARN":           qs_user_arn,
                    "QS_DATASOURCE_ID":      "ti-athena-datasource",
                    "ATHENA_WORKGROUP_NAME": ATHENA_WORKGROUP_NAME,
                    "GLUE_DATABASE_NAME":    GLUE_DATABASE_NAME,
                    "GLUE_TABLE_NAME":       GLUE_TABLE_NAME,
                },
                log_group=logs.LogGroup(
                    self, "LGQsSetup",
                    removal_policy=RemovalPolicy.DESTROY,
                    retention=logs.RetentionDays.ONE_MONTH,
                ),
            )
            qs_setup_fn.add_to_role_policy(iam.PolicyStatement(
                sid="QuickSightSetup",
                actions=["quicksight:*"],
                resources=["*"],
            ))

            qs_provider = cr.Provider(
                self, "QsSetupProvider",
                on_event_handler=qs_setup_fn,
            )

            qs_setup_cr = CustomResource(
                self, "QsSetup",
                service_token=qs_provider.service_token,
                removal_policy=RemovalPolicy.RETAIN,
            )
            qs_setup_cr.node.add_dependency(qs_datasource)
            qs_setup_cr.node.add_dependency(initial_export)

        # --- Outputs ---
        CfnOutput(self, "ProcessedBucketName",        value=processed_bucket.bucket_name)
        CfnOutput(self, "PipelineQueueURL",           value=pipeline_queue.queue_url)
        CfnOutput(self, "ChatbotAPIURL",              value=api.url)
        CfnOutput(self, "DashboardEmbedURL",          value=f"{api.url}dashboard",
                  description="GET this URL to receive a time-limited QuickSight embed URL")
        CfnOutput(self, "EscalationTopicARN",         value=escalation_topic.topic_arn)
        CfnOutput(self, "OpsTopicARN",                value=ops_topic.topic_arn,
                  description="Subscribe your ops email here for DLQ failure alerts")
        CfnOutput(self, "DLQAlarmName",               value=dlq_alarm.alarm_name)
        CfnOutput(self, "OrchestratorFnName",         value=orchestrator_fn.function_name)
        CfnOutput(self, "AgentActionsFnArn",          value=agent_actions_fn.function_arn)
        CfnOutput(self, "BedrockAgentIdSSMPath",      value=SSM_AGENT_ID_PATH,
                  description="aws ssm put-parameter --name /ti/bedrock-agent-id --value <ID> --overwrite")
        CfnOutput(self, "BedrockAgentAliasIdSSMPath", value=SSM_AGENT_ALIAS_PATH,
                  description="aws ssm put-parameter --name /ti/bedrock-agent-alias-id --value <ALIAS> --overwrite")
        CfnOutput(self, "GlueDatabaseName",           value=GLUE_DATABASE_NAME)
        CfnOutput(self, "GlueTableName",              value=GLUE_TABLE_NAME)
        CfnOutput(self, "AthenaWorkgroupName",        value=ATHENA_WORKGROUP_NAME)
        CfnOutput(self, "ExportFnName",               value=export_fn.function_name)
        CfnOutput(self, "QuickSightDataSourceId",     value=qs_datasource.ref,
                  description="Set qs_user in cdk.json context then redeploy to auto-create dataset and dashboard")
        CfnOutput(self, "AnalyticsURL",               value=f"{api.url}analytics/call-type-distribution",
                  description="GET for call-type distribution KPIs")
        CfnOutput(self, "FeedbackURL",                value=f"{api.url}feedback",
                  description="POST analyst corrections here to grow eval ground truth")
        CfnOutput(self, "FeedbackTableName",          value=feedback_table.table_name)

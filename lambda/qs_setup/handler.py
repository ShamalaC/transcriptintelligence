"""
CDK Custom Resource handler: idempotent QuickSight dataset + dashboard.
Invoked automatically by `cdk deploy` via cr.Provider - no manual script needed.

RequestType=Create/Update: upsert dataset then dashboard.
RequestType=Delete:        best-effort delete (stack destroy).
"""
import os
import boto3
import botocore.exceptions

ACCOUNT_ID    = os.environ["QS_ACCOUNT_ID"]
REGION        = os.environ.get("QS_REGION", "us-east-1")
QS_USER_ARN   = os.environ.get("QS_USER_ARN", "")
DATASOURCE_ID = os.environ["QS_DATASOURCE_ID"]
WORKGROUP     = os.environ["ATHENA_WORKGROUP_NAME"]
DATABASE      = os.environ["GLUE_DATABASE_NAME"]
TABLE         = os.environ["GLUE_TABLE_NAME"]

DATASET_ID   = "ti-records-dataset"
DASHBOARD_ID = "ti-analytics-dashboard"

qs = boto3.client("quicksight", region_name=REGION)

DATASOURCE_ARN = f"arn:aws:quicksight:{REGION}:{ACCOUNT_ID}:datasource/{DATASOURCE_ID}"


def on_event(event, context):
    req = event["RequestType"]
    if req in ("Create", "Update"):
        upsert_dataset()
        upsert_dashboard()
    elif req == "Delete":
        delete_all()
    return {"PhysicalResourceId": "qs-setup-singleton"}


def upsert_dataset():
    physical_id = "ti-records-physical"
    payload = {
        "AwsAccountId": ACCOUNT_ID,
        "DataSetId":    DATASET_ID,
        "Name":         "Transcript Intelligence Records",
        "ImportMode":   "DIRECT_QUERY",
        "PhysicalTableMap": {
            physical_id: {
                "RelationalTable": {
                    "DataSourceArn": DATASOURCE_ARN,
                    "Catalog":       "AWSDataCatalog",
                    "Schema":        DATABASE,
                    "Name":          TABLE,
                    "InputColumns": [
                        {"Name": "meeting_id",          "Type": "STRING"},
                        {"Name": "title",               "Type": "STRING"},
                        {"Name": "call_type",           "Type": "STRING"},
                        {"Name": "primary_category",    "Type": "STRING"},
                        {"Name": "sentiment_score",     "Type": "DECIMAL"},
                        {"Name": "sentiment_label",     "Type": "STRING"},
                        {"Name": "is_anomaly",          "Type": "BIT"},
                        {"Name": "z_score",             "Type": "DECIMAL"},
                        {"Name": "severity",            "Type": "STRING"},
                        {"Name": "escalation_team",     "Type": "STRING"},
                        {"Name": "requires_escalation", "Type": "BIT"},
                        {"Name": "processed_at",        "Type": "STRING"},
                        {"Name": "model",               "Type": "STRING"},
                        {"Name": "date",                "Type": "STRING"},
                        # QuickSight enrichment fields (added in v2)
                        {"Name": "account_name",        "Type": "STRING"},
                        {"Name": "feature_gap",         "Type": "BIT"},
                        {"Name": "month",               "Type": "STRING"},
                    ],
                }
            }
        },
        "LogicalTableMap": {
            "records-logical": {
                "Alias": "Records",
                "Source": {"PhysicalTableId": physical_id},
            }
        },
    }
    if QS_USER_ARN:
        payload["Permissions"] = [
            {
                "Principal": QS_USER_ARN,
                "Actions": [
                    "quicksight:DescribeDataSet",
                    "quicksight:DescribeDataSetPermissions",
                    "quicksight:PassDataSet",
                    "quicksight:DescribeIngestion",
                    "quicksight:ListIngestions",
                    "quicksight:UpdateDataSet",
                    "quicksight:DeleteDataSet",
                    "quicksight:CreateIngestion",
                    "quicksight:CancelIngestion",
                    "quicksight:UpdateDataSetPermissions",
                ],
            }
        ]
    try:
        qs.create_data_set(**payload)
    except qs.exceptions.ResourceExistsException:
        payload.pop("Permissions", None)
        qs.update_data_set(**payload)


def build_dashboard_def():
    dataset_arn = f"arn:aws:quicksight:{REGION}:{ACCOUNT_ID}:dataset/{DATASET_ID}"
    ds = "records"

    def col(name):
        return {"DataSetIdentifier": ds, "ColumnName": name}

    def cat_dim(fid, c):
        return {"CategoricalDimensionField": {"FieldId": fid, "Column": col(c)}}

    def num_agg(fid, c, agg="COUNT"):
        return {"NumericalMeasureField": {"FieldId": fid, "Column": col(c),
                "AggregationFunction": {"SimpleNumericalAggregation": agg}}}

    def num_dim(fid, c):
        return {"NumericalDimensionField": {"FieldId": fid, "Column": col(c)}}

    def date_dim(fid, c):
        return {"DateDimensionField": {"FieldId": fid, "Column": col(c)}}

    def title(text):
        return {"Visibility": "VISIBLE", "FormatText": {"PlainText": text}}

    def cat_filter(filter_id, col_name, values):
        return {
            "CategoryFilter": {
                "FilterId": filter_id,
                "Column":   col(col_name),
                "Configuration": {
                    "FilterListConfiguration": {
                        "MatchOperator": "CONTAINS",
                        "CategoryValues": values,
                        "NullOption": "NON_NULLS_ONLY",
                    }
                },
            }
        }

    def num_eq_filter(filter_id, col_name, value):
        return {
            "NumericEqualityFilter": {
                "FilterId":     filter_id,
                "Column":       col(col_name),
                "Value":        value,
                "MatchOperator": "EQUALS",
                "NullOption":   "NON_NULLS_ONLY",
            }
        }

    def scope_to(sheet_id, visual_ids):
        return {
            "SelectedSheets": {
                "SheetVisualScopingConfigurations": [{
                    "SheetId": sheet_id,
                    "Scope":   "SELECTED_VISUALS",
                    "VisualIds": visual_ids,
                }]
            }
        }

    return {
        "DataSetIdentifierDeclarations": [{"Identifier": ds, "DataSetArn": dataset_arn}],
        "ParameterDeclarations": [],

        # Dashboard-level calculated field: churn risk label used in the churn table
        "CalculatedFields": [
            {
                "DataSetIdentifier": ds,
                "Name":       "churn_risk_flag",
                "Expression": (
                    "ifelse({call_type} = 'external' AND {is_anomaly} = 1"
                    " AND {sentiment_score} < 3.5, 'At Risk', 'Healthy')"
                ),
            }
        ],

        # Per-visual filters — scoped so they only affect their target visual
        "FilterGroups": [
            # Blast radius: only Incident Response meetings
            {
                "FilterGroupId": "fg-blast-radius",
                "Filters": [cat_filter("f-incident-cat", "primary_category",
                                       ["Incident Response"])],
                "ScopeConfiguration": scope_to("insights", ["blast-radius-bar"]),
                "CrossDataset": "SINGLE_DATASET",
                "Status": "ENABLED",
            },
            # Churn risk: only external calls
            {
                "FilterGroupId": "fg-churn-risk",
                "Filters": [cat_filter("f-external-ct", "call_type", ["external"])],
                "ScopeConfiguration": scope_to("insights", ["churn-risk-table"]),
                "CrossDataset": "SINGLE_DATASET",
                "Status": "ENABLED",
            },
            # Feature gap: only meetings flagged as feature_gap = 1
            {
                "FilterGroupId": "fg-feature-gap",
                "Filters": [num_eq_filter("f-fg-true", "feature_gap", 1)],
                "ScopeConfiguration": scope_to("insights", ["feature-gap-bar"]),
                "CrossDataset": "SINGLE_DATASET",
                "Status": "ENABLED",
            },
        ],

        "Sheets": [
            # ── Sheet 1: Overview (existing 6 visuals, unchanged) ─────────────
            {
                "SheetId": "overview",
                "Name":    "Overview",
                "Visuals": [
                    # 1 - Call Type Distribution (donut)
                    {"PieChartVisual": {
                        "VisualId": "call-type-pie",
                        "Title": title("Call Type Distribution"),
                        "ChartConfiguration": {
                            "FieldWells": {"PieChartAggregatedFieldWells": {
                                "Category": [cat_dim("ct_cat", "call_type")],
                                "Values":   [num_agg("ct_val", "meeting_id")],
                            }},
                            "DonutOptions": {"ArcOptions": {"ArcThickness": "MEDIUM"}},
                        },
                    }},
                    # 2 - Sentiment Trend (line by date)
                    {"LineChartVisual": {
                        "VisualId": "sentiment-trend",
                        "Title": title("Avg Sentiment Score by Date"),
                        "ChartConfiguration": {
                            "FieldWells": {"LineChartAggregatedFieldWells": {
                                "Category": [date_dim("sent_date", "date")],
                                "Values":   [num_agg("sent_val", "sentiment_score", "AVERAGE")],
                            }},
                            "Type": "LINE",
                        },
                    }},
                    # 3 - Category Breakdown (horizontal bar)
                    {"BarChartVisual": {
                        "VisualId": "category-bar",
                        "Title": title("Volume by Category"),
                        "ChartConfiguration": {
                            "FieldWells": {"BarChartAggregatedFieldWells": {
                                "Category": [cat_dim("cat_dim", "primary_category")],
                                "Values":   [num_agg("cat_val", "meeting_id")],
                            }},
                            "Orientation": "HORIZONTAL",
                            "BarsArrangement": "CLUSTERED",
                        },
                    }},
                    # 4 - Escalation Routing (stacked bar)
                    {"BarChartVisual": {
                        "VisualId": "escalation-routing",
                        "Title": title("Escalation Routing by Call Type"),
                        "ChartConfiguration": {
                            "FieldWells": {"BarChartAggregatedFieldWells": {
                                "Category": [cat_dim("esc_ct",   "call_type")],
                                "Colors":   [cat_dim("esc_team", "escalation_team")],
                                "Values":   [num_agg("esc_val",  "meeting_id")],
                            }},
                            "Orientation": "VERTICAL",
                            "BarsArrangement": "STACKED",
                        },
                    }},
                    # 5 - Severity Heatmap (pivot table)
                    {"PivotTableVisual": {
                        "VisualId": "severity-heatmap",
                        "Title": title("Severity by Category"),
                        "ChartConfiguration": {
                            "FieldWells": {"PivotTableAggregatedFieldWells": {
                                "Rows":    [cat_dim("sev_row", "primary_category")],
                                "Columns": [cat_dim("sev_col", "severity")],
                                "Values":  [num_agg("sev_val", "meeting_id")],
                            }},
                        },
                    }},
                    # 6 - Anomaly Count KPI
                    {"KPIVisual": {
                        "VisualId": "anomaly-kpi",
                        "Title": title("Anomaly Count"),
                        "ChartConfiguration": {
                            "FieldWells": {"Values": [num_agg("anom_val", "is_anomaly", "SUM")]},
                        },
                    }},
                ],
            },

            # ── Sheet 2: Insights (5 new visuals — notebook parity) ──────────
            {
                "SheetId": "insights",
                "Name":    "Insights",
                "Visuals": [
                    # 7 - Monthly Sentiment by Call Type — The March Story
                    # Three lines (support / external / internal) showing the March drop
                    {"LineChartVisual": {
                        "VisualId": "monthly-sentiment",
                        "Title": title("Monthly Avg Sentiment by Call Type"),
                        "ChartConfiguration": {
                            "FieldWells": {"LineChartAggregatedFieldWells": {
                                "Category": [cat_dim("ms_month", "month")],
                                "Colors":   [cat_dim("ms_type",  "call_type")],
                                "Values":   [num_agg("ms_sent",  "sentiment_score", "AVERAGE")],
                            }},
                            "Type": "LINE",
                            "SortConfiguration": {
                                "CategorySort": [{
                                    "FieldSort": {"FieldId": "ms_month", "Direction": "ASC"}
                                }]
                            },
                        },
                    }},

                    # 8 - Anomaly Z-Score Scatter
                    # Each dot is one meeting: x=sentiment, y=z-score, colour=call_type
                    {"ScatterPlotVisual": {
                        "VisualId": "anomaly-scatter",
                        "Title": title("Anomaly Scatter: Sentiment vs Z-Score by Call Type"),
                        "ChartConfiguration": {
                            "FieldWells": {
                                "ScatterPlotUnaggregatedFieldWells": {
                                    "XAxis":    [num_dim("scat_x",   "sentiment_score")],
                                    "YAxis":    [num_dim("scat_y",   "z_score")],
                                    "Category": [cat_dim("scat_cat", "call_type")],
                                }
                            },
                        },
                    }},

                    # 9 - Churn Risk Table (filtered to external calls only via fg-churn-risk)
                    # Sorted lowest-sentiment-first = highest churn risk at the top
                    {"TableVisual": {
                        "VisualId": "churn-risk-table",
                        "Title": title("Churn Risk — External Accounts by Sentiment"),
                        "ChartConfiguration": {
                            "FieldWells": {
                                "TableAggregatedFieldWells": {
                                    "GroupBy": [
                                        cat_dim("tbl_acc",  "account_name"),
                                        cat_dim("tbl_flag", "churn_risk_flag"),
                                    ],
                                    "Values": [
                                        num_agg("tbl_sent", "sentiment_score", "AVERAGE"),
                                        num_agg("tbl_z",    "z_score",         "AVERAGE"),
                                        num_agg("tbl_anom", "is_anomaly",      "SUM"),
                                    ],
                                }
                            },
                            "SortConfiguration": {
                                "RowSort": [{
                                    "FieldSort": {"FieldId": "tbl_sent", "Direction": "ASC"}
                                }]
                            },
                        },
                    }},

                    # 10 - Feature Gap Rate by Category
                    # Filtered to feature_gap=1 meetings only via fg-feature-gap
                    {"BarChartVisual": {
                        "VisualId": "feature-gap-bar",
                        "Title": title("Feature Gap Signals by Category"),
                        "ChartConfiguration": {
                            "FieldWells": {"BarChartAggregatedFieldWells": {
                                "Category": [cat_dim("fg_cat", "primary_category")],
                                "Values":   [num_agg("fg_val", "meeting_id")],
                            }},
                            "Orientation": "HORIZONTAL",
                            "BarsArrangement": "CLUSTERED",
                            "SortConfiguration": {
                                "CategorySort": [{
                                    "FieldSort": {"FieldId": "fg_val", "Direction": "DESC"}
                                }]
                            },
                        },
                    }},

                    # 11 - Outage Blast Radius by Month
                    # Filtered to Incident Response only via fg-blast-radius
                    # Shows how many meetings of each call_type were triggered per month
                    {"BarChartVisual": {
                        "VisualId": "blast-radius-bar",
                        "Title": title("Outage Blast Radius — Incident Response Meetings by Month"),
                        "ChartConfiguration": {
                            "FieldWells": {"BarChartAggregatedFieldWells": {
                                "Category": [cat_dim("br_month", "month")],
                                "Colors":   [cat_dim("br_type",  "call_type")],
                                "Values":   [num_agg("br_val",   "meeting_id")],
                            }},
                            "Orientation": "VERTICAL",
                            "BarsArrangement": "STACKED",
                            "SortConfiguration": {
                                "CategorySort": [{
                                    "FieldSort": {"FieldId": "br_month", "Direction": "ASC"}
                                }]
                            },
                        },
                    }},
                ],
            },
        ],
    }


def upsert_dashboard():
    payload = {
        "AwsAccountId": ACCOUNT_ID,
        "DashboardId":  DASHBOARD_ID,
        "Name":         "Transcript Intelligence Analytics",
        "Definition":   build_dashboard_def(),
        "DashboardPublishOptions": {
            "AdHocFilteringOption": {"AvailabilityStatus": "ENABLED"},
            "ExportToCSVOption":    {"AvailabilityStatus": "ENABLED"},
        },
    }
    if QS_USER_ARN:
        payload["Permissions"] = [
            {
                "Principal": QS_USER_ARN,
                "Actions": [
                    "quicksight:DescribeDashboard",
                    "quicksight:ListDashboardVersions",
                    "quicksight:UpdateDashboardPermissions",
                    "quicksight:QueryDashboard",
                    "quicksight:UpdateDashboard",
                    "quicksight:DeleteDashboard",
                    "quicksight:DescribeDashboardPermissions",
                    "quicksight:UpdateDashboardPublishedVersion",
                ],
            }
        ]
    try:
        qs.create_dashboard(**payload)
    except qs.exceptions.ResourceExistsException:
        payload.pop("Permissions", None)
        qs.update_dashboard(**payload)


def delete_all():
    for fn, kwargs in [
        (qs.delete_dashboard, {"AwsAccountId": ACCOUNT_ID, "DashboardId": DASHBOARD_ID}),
        (qs.delete_data_set,  {"AwsAccountId": ACCOUNT_ID, "DataSetId":   DATASET_ID}),
    ]:
        try:
            fn(**kwargs)
        except (botocore.exceptions.ClientError,):
            pass

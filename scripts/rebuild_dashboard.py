"""
Rebuild the QuickSight dashboard to match all notebook visualizations.
Fixes column name mismatches and adds all 15 charts across 4 sheets.

Run: python3 scripts/rebuild_dashboard.py
"""
import boto3, json

ACCOUNT_ID    = "747747309973"
REGION        = "us-east-1"
DATASOURCE_ID = "ti-athena-datasource"
DATABASE      = "ti_analytics"
TABLE         = "ti_records_export"
DATASET_ID    = "ti-records-dataset"
DASHBOARD_ID  = "ti-analytics-dashboard"
USER_ARN      = f"arn:aws:quicksight:{REGION}:{ACCOUNT_ID}:user/default/{ACCOUNT_ID}"
DATASOURCE_ARN = f"arn:aws:quicksight:{REGION}:{ACCOUNT_ID}:datasource/{DATASOURCE_ID}"
DATASET_ARN    = f"arn:aws:quicksight:{REGION}:{ACCOUNT_ID}:dataset/{DATASET_ID}"

qs = boto3.client("quicksight", region_name=REGION)

PERMISSIONS = [{"Principal": USER_ARN, "Actions": [
    "quicksight:DescribeDashboard", "quicksight:ListDashboardVersions",
    "quicksight:UpdateDashboardPermissions", "quicksight:QueryDashboard",
    "quicksight:UpdateDashboard", "quicksight:DeleteDashboard",
    "quicksight:DescribeDashboardPermissions", "quicksight:UpdateDashboardPublishedVersion",
]}]

DS_PERMISSIONS = [{"Principal": USER_ARN, "Actions": [
    "quicksight:DescribeDataSet", "quicksight:DescribeDataSetPermissions",
    "quicksight:PassDataSet", "quicksight:DescribeIngestion", "quicksight:ListIngestions",
    "quicksight:UpdateDataSet", "quicksight:DeleteDataSet", "quicksight:CreateIngestion",
    "quicksight:CancelIngestion", "quicksight:UpdateDataSetPermissions",
]}]


# ── 1. Upsert dataset with correct column names ──────────────────────────────

def upsert_dataset():
    physical_id = "ti-records-physical"
    payload = {
        "AwsAccountId": ACCOUNT_ID,
        "DataSetId":    DATASET_ID,
        "Name":         "Transcript Intelligence Records",
        "ImportMode":   "SPICE",
        "PhysicalTableMap": {
            physical_id: {
                "RelationalTable": {
                    "DataSourceArn": DATASOURCE_ARN,
                    "Catalog": "AWSDataCatalog",
                    "Schema":  DATABASE,
                    "Name":    TABLE,
                    "InputColumns": [
                        {"Name": "meeting_id",          "Type": "STRING"},
                        {"Name": "title",               "Type": "STRING"},
                        {"Name": "call_type",           "Type": "STRING"},
                        {"Name": "category",            "Type": "STRING"},
                        {"Name": "sentiment_score",     "Type": "DECIMAL"},
                        {"Name": "sentiment_label",     "Type": "STRING"},
                        {"Name": "is_anomaly",          "Type": "BIT"},
                        {"Name": "z_score",             "Type": "DECIMAL"},
                        {"Name": "n_issues",            "Type": "INTEGER"},
                        {"Name": "escalation_team",     "Type": "STRING"},
                        {"Name": "escalation_priority", "Type": "STRING"},
                        {"Name": "has_pagerduty",       "Type": "BIT"},
                        {"Name": "processed_at",        "Type": "STRING"},
                        {"Name": "account_name",        "Type": "STRING"},
                        {"Name": "feature_gap",         "Type": "BIT"},
                        {"Name": "month",               "Type": "STRING"},
                        {"Name": "date",                "Type": "STRING"},
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
        "Permissions": DS_PERMISSIONS,
    }
    try:
        qs.create_data_set(**payload)
        print("Dataset created")
    except qs.exceptions.ResourceExistsException:
        payload.pop("Permissions")
        qs.update_data_set(**payload)
        print("Dataset updated")


# ── 2. Build complete dashboard definition ───────────────────────────────────

def build_def():
    ds = "records"

    def col(name):
        return {"DataSetIdentifier": ds, "ColumnName": name}

    def cat_dim(fid, c):
        return {"CategoricalDimensionField": {"FieldId": fid, "Column": col(c)}}

    def num_agg(fid, c, agg="COUNT"):
        return {"NumericalMeasureField": {
            "FieldId": fid, "Column": col(c),
            "AggregationFunction": {"SimpleNumericalAggregation": agg}}}

    def cat_agg(fid, c, agg="COUNT"):
        return {"CategoricalMeasureField": {
            "FieldId": fid, "Column": col(c),
            "AggregationFunction": agg}}

    def num_dim(fid, c):
        return {"NumericalDimensionField": {"FieldId": fid, "Column": col(c)}}

    def T(text):
        return {"Visibility": "VISIBLE", "FormatText": {"PlainText": text}}

    def cat_filter(fid, col_name, values):
        return {"CategoryFilter": {
            "FilterId": fid, "Column": col(col_name),
            "Configuration": {"FilterListConfiguration": {
                "MatchOperator": "CONTAINS",
                "CategoryValues": values,
                "NullOption": "NON_NULLS_ONLY",
            }},
        }}

    def num_eq_filter(fid, col_name, value):
        return {"NumericEqualityFilter": {
            "FilterId": fid, "Column": col(col_name),
            "Value": value, "MatchOperator": "EQUALS",
            "NullOption": "NON_NULLS_ONLY",
        }}

    def scope(sheet_id, visual_ids):
        return {"SelectedSheets": {"SheetVisualScopingConfigurations": [{
            "SheetId": sheet_id, "Scope": "SELECTED_VISUALS", "VisualIds": visual_ids,
        }]}}

    # ── KPI helper ────────────────────────────────────────────────────────────
    def kpi(vid, text, field, subtitle="", fmt="NUMBER"):
        cfg = {
            "FieldWells": {"Values": [field]},
            "KPIOptions": {
                "PrimaryValueFontConfiguration": {
                    "FontSize": {"Relative": "EXTRA_LARGE"},
                },
            },
        }
        vis = {
            "VisualId":         vid,
            "Title":            T(text),
            "ChartConfiguration": cfg,
        }
        if subtitle:
            vis["Subtitle"] = {"Visibility": "VISIBLE",
                               "FormatText": {"PlainText": subtitle}}
        return {"KPIVisual": vis}

    # ── Bar chart helper ─────────────────────────────────────────────────────
    def hbar(vid, text, cat_field, val_field, color_field=None, sort_asc=False):
        wells = {"Category": [cat_field], "Values": [val_field]}
        if color_field:
            wells["Colors"] = [color_field]
        sort_dir = "ASC" if sort_asc else "DESC"
        val_fid = val_field.get("NumericalMeasureField", val_field.get(
            "CategoricalMeasureField", {})).get("FieldId", "")
        return {"BarChartVisual": {
            "VisualId": vid, "Title": T(text),
            "ChartConfiguration": {
                "FieldWells": {"BarChartAggregatedFieldWells": wells},
                "Orientation": "HORIZONTAL",
                "BarsArrangement": "CLUSTERED",
                "SortConfiguration": {"CategorySort": [
                    {"FieldSort": {"FieldId": val_fid, "Direction": sort_dir}}
                ]},
            },
        }}

    def vbar(vid, text, cat_field, val_field, color_field=None, stacked=False, sort_asc=False):
        wells = {"Category": [cat_field], "Values": [val_field]}
        if color_field:
            wells["Colors"] = [color_field]
        return {"BarChartVisual": {
            "VisualId": vid, "Title": T(text),
            "ChartConfiguration": {
                "FieldWells": {"BarChartAggregatedFieldWells": wells},
                "Orientation": "VERTICAL",
                "BarsArrangement": "STACKED" if stacked else "CLUSTERED",
            },
        }}

    return {
        "DataSetIdentifierDeclarations": [{"Identifier": ds, "DataSetArn": DATASET_ARN}],
        "ParameterDeclarations": [],
        "CalculatedFields": [
            {
                "DataSetIdentifier": ds,
                "Name": "churn_risk_flag",
                "Expression": (
                    "ifelse({call_type} = 'external' AND {sentiment_score} < 3.0,"
                    " 'At Risk', 'Healthy')"
                ),
            }
        ],
        "FilterGroups": [
            # External calls only → all three account charts
            {
                "FilterGroupId": "fg-external",
                "Filters": [cat_filter("f-external-ct", "call_type", ["external"])],
                "ScopeConfiguration": scope("accounts", [
                    "churn-risk-bar", "march-accounts-bar", "account-volume-bar",
                ]),
                "CrossDataset": "SINGLE_DATASET", "Status": "ENABLED",
            },
            # March external meetings → march accounts chart
            {
                "FilterGroupId": "fg-march",
                "Filters": [cat_filter("f-march-month", "month", ["2026-03"])],
                "ScopeConfiguration": scope("accounts", ["march-accounts-bar"]),
                "CrossDataset": "SINGLE_DATASET", "Status": "ENABLED",
            },
            # Incident Response only → blast radius chart
            {
                "FilterGroupId": "fg-incident",
                "Filters": [cat_filter("f-incident-cat", "category", ["Incident Response"])],
                "ScopeConfiguration": scope("escalations", ["blast-radius-bar"]),
                "CrossDataset": "SINGLE_DATASET", "Status": "ENABLED",
            },
            # Feature gap = true → feature gap chart
            {
                "FilterGroupId": "fg-feature-gap",
                "Filters": [num_eq_filter("f-fg-true", "feature_gap", 1)],
                "ScopeConfiguration": scope("escalations", ["feature-gap-bar"]),
                "CrossDataset": "SINGLE_DATASET", "Status": "ENABLED",
            },
        ],

        "Sheets": [

            # ═══════════════════════════════════════════════════════════════
            # Sheet 1 — Overview  
            # ═══════════════════════════════════════════════════════════════
            {
                "SheetId": "overview",
                "Name": "Overview",
                "Visuals": [

                    # KPI: total meetings
                    kpi("kpi-total",     "Total Meetings",
                        cat_agg("kpi_total", "meeting_id"),
                        subtitle="All processed meetings"),
                    # KPI: anomalies
                    kpi("kpi-anomaly",   "Anomalies Flagged",
                        num_agg("kpi_anom", "is_anomaly", "SUM"),
                        subtitle="Sentiment z-score > 1.5σ"),
                    # KPI: avg sentiment
                    kpi("kpi-sentiment", "Avg Sentiment Score",
                        num_agg("kpi_sent", "sentiment_score", "AVERAGE"),
                        subtitle="Scale 1 (negative) → 5 (positive)",
                        fmt="DECIMAL"),
                    # KPI: meetings with escalation
                    kpi("kpi-escalation","Meetings Escalated",
                        num_agg("kpi_esc", "has_pagerduty", "SUM"),
                        subtitle="P0 / P1 priority triggers"),

                    # Fig 01a — Call Type Distribution (donut)
                    {"PieChartVisual": {
                        "VisualId": "call-type-donut",
                        "Title": T("Call Type Distribution"),
                        "ChartConfiguration": {
                            "FieldWells": {"PieChartAggregatedFieldWells": {
                                "Category": [cat_dim("ctd_cat", "call_type")],
                                "Values":   [cat_agg("ctd_val", "meeting_id")],
                            }},
                            "DonutOptions": {"ArcOptions": {"ArcThickness": "MEDIUM"}},
                        },
                    }},

                    # Fig 01b — Call Type by Month (stacked bar)
                    vbar("call-type-by-month",
                         "Meetings by Month & Call Type",
                         cat_dim("ctm_month", "month"),
                         cat_agg("ctm_val",   "meeting_id"),
                         color_field=cat_dim("ctm_color", "call_type"),
                         stacked=True),

                    # Fig 01c — Sentiment Label Distribution (horizontal bar)
                    hbar("sentiment-label-bar",
                         "Sentiment Label Distribution",
                         cat_dim("sl_cat", "sentiment_label"),
                         cat_agg("sl_val", "meeting_id"),
                         sort_asc=False),

                    # Fig 02a — Category Distribution (horizontal bar, sorted desc)
                    hbar("category-bar",
                         "Volume by Category",
                         cat_dim("cat_dim", "category"),
                         cat_agg("cat_val", "meeting_id"),
                         sort_asc=False),

                    # Fig 02b — Category by Call Type (stacked bar)
                    vbar("category-by-calltype",
                         "Category Mix by Call Type",
                         cat_dim("cct_cat",   "call_type"),
                         cat_agg("cct_val",   "meeting_id"),
                         color_field=cat_dim("cct_color", "category"),
                         stacked=True),

                    # Avg sentiment by category (supports Fig 03 context)
                    hbar("sentiment-by-category",
                         "Avg Sentiment Score by Category",
                         cat_dim("sbc_cat", "category"),
                         num_agg("sbc_val", "sentiment_score", "AVERAGE"),
                         sort_asc=True),
                ],
            },

            # ═══════════════════════════════════════════════════════════════
            # Sheet 2 — Sentiment & Anomalies  (Fig 04 + Fig 05)
            # ═══════════════════════════════════════════════════════════════
            {
                "SheetId": "sentiment",
                "Name": "Sentiment & Anomalies",
                "Visuals": [

                    # Monthly Sentiment Trend by Call Type (line chart)
                    {"LineChartVisual": {
                        "VisualId": "monthly-sentiment-line",
                        "Title": T("Monthly Avg Sentiment by Call Type"),
                        "ChartConfiguration": {
                            "FieldWells": {"LineChartAggregatedFieldWells": {
                                "Category": [cat_dim("ms_month", "month")],
                                "Colors":   [cat_dim("ms_color", "call_type")],
                                "Values":   [num_agg("ms_val",   "sentiment_score", "AVERAGE")],
                            }},
                            "Type": "LINE",
                            "SortConfiguration": {"CategorySort": [
                                {"FieldSort": {"FieldId": "ms_month", "Direction": "ASC"}}
                            ]},
                        },
                    }},

                    # Fig 05 — Anomaly Scatter: sentiment_score vs z_score, colour=call_type
                    {"ScatterPlotVisual": {
                        "VisualId": "anomaly-scatter",
                        "Title": T("Anomaly Scatter: Sentiment vs Z-Score"),
                        "ChartConfiguration": {
                            "FieldWells": {"ScatterPlotUnaggregatedFieldWells": {
                                "XAxis":    [num_dim("sc_x",   "sentiment_score")],
                                "YAxis":    [num_dim("sc_y",   "z_score")],
                                "Category": [cat_dim("sc_cat", "call_type")],
                                "Size":     [num_agg("sc_sz",  "is_anomaly", "SUM")],
                            }},
                        },
                    }},

                    # Avg z-score by call type (supports anomaly analysis)
                    hbar("zscore-by-calltype",
                         "Avg Z-Score by Call Type",
                         cat_dim("zs_cat", "call_type"),
                         num_agg("zs_val", "z_score", "AVERAGE"),
                         sort_asc=True),

                    # Sentiment distribution by category (avg sentiment bar)
                    hbar("sentiment-by-cat-bar",
                         "Avg Sentiment by Category vs Baseline",
                         cat_dim("s3_cat", "category"),
                         num_agg("s3_val", "sentiment_score", "AVERAGE"),
                         sort_asc=True),

                    # Anomaly count by month
                    vbar("anomaly-by-month",
                         "Anomaly Count by Month",
                         cat_dim("am_month", "month"),
                         num_agg("am_val",   "is_anomaly", "SUM"),
                         color_field=cat_dim("am_color", "call_type"),
                         stacked=True),

                    # Pivot: category × month average sentiment (heatmap-style)
                    {"PivotTableVisual": {
                        "VisualId": "sentiment-pivot",
                        "Title": T("Sentiment Heatmap: Category × Month"),
                        "ChartConfiguration": {
                            "FieldWells": {"PivotTableAggregatedFieldWells": {
                                "Rows":    [cat_dim("pv_row", "category")],
                                "Columns": [cat_dim("pv_col", "month")],
                                "Values":  [num_agg("pv_val", "sentiment_score", "AVERAGE")],
                            }},
                        },
                    }},
                ],
            },

            # ═══════════════════════════════════════════════════════════════
            # Sheet 3 — Accounts & Risk  (Fig 05b + Fig 06)
            # ═══════════════════════════════════════════════════════════════
            {
                "SheetId": "accounts",
                "Name": "Accounts & Churn Risk",
                "Visuals": [

                    # Fig 06 — Churn Risk: external accounts sorted by avg sentiment ASC
                    # (filtered to external via fg-external)
                    hbar("churn-risk-bar",
                         "Account Churn Risk (External, Sentiment Low→High)",
                         cat_dim("cr_acc", "account_name"),
                         num_agg("cr_val", "sentiment_score", "AVERAGE"),
                         sort_asc=True),

                    # Fig 05b — March accounts: external, March 2026 only
                    # (filtered to external + 2026-03 via fg-external + fg-march)
                    hbar("march-accounts-bar",
                         " March 2026 Accounts by Sentiment (Outage Impact)",
                         cat_dim("ma_acc", "account_name"),
                         num_agg("ma_val", "sentiment_score", "AVERAGE"),
                         sort_asc=True),

                    # External account meeting count
                    hbar("account-volume-bar",
                         "External Account Meeting Volume",
                         cat_dim("av_acc", "account_name"),
                         cat_agg("av_val", "meeting_id"),
                         sort_asc=False),

                    # Churn risk flag summary (At Risk vs Healthy)
                    {"PieChartVisual": {
                        "VisualId": "churn-risk-pie",
                        "Title": T("At-Risk vs Healthy Accounts"),
                        "ChartConfiguration": {
                            "FieldWells": {"PieChartAggregatedFieldWells": {
                                "Category": [cat_dim("crp_cat", "churn_risk_flag")],
                                "Values":   [cat_agg("crp_val", "meeting_id")],
                            }},
                            "DonutOptions": {"ArcOptions": {"ArcThickness": "MEDIUM"}},
                        },
                    }},

                    # Category breakdown for external accounts
                    vbar("external-category-bar",
                         "Category Mix for External Accounts",
                         cat_dim("ec_cat",   "category"),
                         cat_agg("ec_val",   "meeting_id"),
                         color_field=cat_dim("ec_color", "month"),
                         stacked=True),
                ],
            },

            # ═══════════════════════════════════════════════════════════════
            # Sheet 4 — Escalations & Feature Gaps  (Fig 07 + Fig 08 + Fig 09)
            # ═══════════════════════════════════════════════════════════════
            {
                "SheetId": "escalations",
                "Name": "Escalations & Feature Gaps",
                "Visuals": [

                    # Fig 09a — Escalation Routing by Team (vertical bar)
                    vbar("escalation-team-bar",
                         "Escalation Routing by Team",
                         cat_dim("et_team", "escalation_team"),
                         cat_agg("et_val",  "meeting_id"),
                         sort_asc=False),

                    # Fig 09b — Severity Distribution
                    vbar("severity-bar",
                         " Issue Severity Distribution (p0–p3)",
                         cat_dim("sv_pri", "escalation_priority"),
                         cat_agg("sv_val", "meeting_id"),
                         sort_asc=False),

                    # Escalation routing stacked by call type
                    vbar("escalation-calltype-bar",
                         "Escalation Team by Call Type",
                         cat_dim("ect_team",  "escalation_team"),
                         cat_agg("ect_val",   "meeting_id"),
                         color_field=cat_dim("ect_color", "call_type"),
                         stacked=True),

                    # Fig 07 — Outage Blast Radius: Incident Response by month + call type
                    # (filtered to Incident Response via fg-incident)
                    vbar("blast-radius-bar",
                         "Outage Blast Radius: Incident Response by Month",
                         cat_dim("br_month", "month"),
                         cat_agg("br_val",   "meeting_id"),
                         color_field=cat_dim("br_color", "call_type"),
                         stacked=True),

                    # Fig 08 — Feature Gap signals by category
                    # (filtered to feature_gap=1 via fg-feature-gap)
                    hbar("feature-gap-bar",
                         "Feature Gap Signals by Category",
                         cat_dim("fg_cat", "category"),
                         cat_agg("fg_val", "meeting_id"),
                         sort_asc=False),

                    # n_issues distribution
                    vbar("issues-by-calltype",
                         "Total Issues by Call Type",
                         cat_dim("ni_ct",  "call_type"),
                         num_agg("ni_val", "n_issues", "SUM"),
                         color_field=cat_dim("ni_color", "escalation_priority"),
                         stacked=True),
                ],
            },
        ],
    }


# ── 3. Delete failing dashboard and recreate ─────────────────────────────────

def rebuild_dashboard():
    try:
        qs.delete_dashboard(AwsAccountId=ACCOUNT_ID, DashboardId=DASHBOARD_ID)
        print(f"Deleted existing {DASHBOARD_ID}")
    except Exception:
        pass

    resp = qs.create_dashboard(
        AwsAccountId=ACCOUNT_ID,
        DashboardId=DASHBOARD_ID,
        Name="Transcript Intelligence Analytics",
        Definition=build_def(),
        Permissions=PERMISSIONS,
        DashboardPublishOptions={
            "AdHocFilteringOption": {"AvailabilityStatus": "ENABLED"},
            "ExportToCSVOption":    {"AvailabilityStatus": "ENABLED"},
        },
    )
    print(f"Dashboard created: status={resp['Status']}")


def ingest_spice():
    """Trigger SPICE ingestion and wait for it to complete."""
    import time, uuid
    ingestion_id = f"ingest-{uuid.uuid4().hex[:8]}"
    qs.create_ingestion(
        DataSetId=DATASET_ID,
        IngestionId=ingestion_id,
        AwsAccountId=ACCOUNT_ID,
    )
    print(f"   SPICE ingestion started: {ingestion_id}")
    for _ in range(30):
        time.sleep(6)
        resp  = qs.describe_ingestion(
            DataSetId=DATASET_ID,
            IngestionId=ingestion_id,
            AwsAccountId=ACCOUNT_ID,
        )
        state = resp["Ingestion"]["IngestionStatus"]
        rows  = resp["Ingestion"].get("RowInfo", {}).get("RowsIngested", "…")
        print(f"   {state}  rows={rows}")
        if state in ("COMPLETED", "FAILED", "CANCELLED"):
            if state != "COMPLETED":
                raise RuntimeError(f"SPICE ingestion {state}")
            return
    raise TimeoutError("SPICE ingestion did not complete in 3 minutes")


if __name__ == "__main__":
    print("1. Updating dataset columns (SPICE mode)...")
    upsert_dataset()

    print("2. Ingesting data into SPICE...")
    ingest_spice()

    print("3. Rebuilding dashboard with all notebook charts...")
    rebuild_dashboard()

    print("Done. Open: https://us-east-1.quicksight.aws.amazon.com/sn/dashboards/ti-analytics-dashboard")

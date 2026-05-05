"""
Unit tests for Lambda tool logic - no AWS calls required.

Covers every tool function in both agent_actions and orchestrator so a bug
in shared constants (TAXONOMY, ESCALATION_RULES, etc.) is caught before deploy.
"""
import json
import sys
import os
import pytest
from decimal import Decimal

# Make shared layer importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../lambda/shared/python"))

# --- Shared layer ---

class TestSharedLayer:
    def test_classify_call_type_support(self):
        from ti_classifier import classify_call_type
        assert classify_call_type("Support Case #42") == "support"
        assert classify_call_type("Support Case - Login Issue") == "support"

    def test_classify_call_type_external(self):
        from ti_classifier import classify_call_type
        assert classify_call_type("Aegis / Acme Corp Onboarding") == "external"
        assert classify_call_type("Aegis / Quarterly Review") == "external"

    def test_classify_call_type_internal(self):
        from ti_classifier import classify_call_type
        assert classify_call_type("Sprint Planning Q2") == "internal"
        assert classify_call_type("") == "internal"
        assert classify_call_type(None) == "internal"

    def test_taxonomy_keys(self):
        from ti_config import TAXONOMY
        expected = {
            "Product Reliability", "Compliance & Audit", "Customer Risk",
            "Revenue & Contracts", "Product Development", "Identity & Security",
            "Onboarding & Adoption",
        }
        assert set(TAXONOMY.keys()) == expected

    def test_call_type_baselines(self):
        from ti_config import CALL_TYPE_BASELINES
        assert CALL_TYPE_BASELINES["support"]  == pytest.approx(2.94)
        assert CALL_TYPE_BASELINES["external"] == pytest.approx(4.21)
        assert CALL_TYPE_BASELINES["internal"] == pytest.approx(3.42)

    def test_escalation_rules_p0(self):
        from ti_config import ESCALATION_RULES
        assert ESCALATION_RULES[("Product Reliability", "p0")] == "engineering"
        assert ESCALATION_RULES[("Compliance & Audit",  "p0")] == "security"

    def test_dec_float(self):
        from ti_shared import dec
        assert dec(1.5) == Decimal("1.5")

    def test_dec_nested(self):
        from ti_shared import dec
        result = dec({"score": 2.94, "items": [1.0, 2.0]})
        assert result["score"] == Decimal("2.94")
        assert result["items"] == [Decimal("1.0"), Decimal("2.0")]

    def test_dec_passthrough(self):
        from ti_shared import dec
        assert dec("hello") == "hello"
        assert dec(42)      == 42
        assert dec(True)    == True

    def test_structured_logger_emits_json(self, capsys):
        from ti_shared import StructuredLogger
        log = StructuredLogger("test.info")
        log.info("test_event", meeting_id="abc-123", count=3)
        captured = capsys.readouterr()
        # Python logging.StreamHandler writes to stderr by default
        payload = json.loads(captured.err.strip())
        assert payload["level"] == "INFO"
        assert payload["event"] == "test_event"
        assert payload["meeting_id"] == "abc-123"
        assert payload["count"] == 3

    def test_structured_logger_error(self, capsys):
        from ti_shared import StructuredLogger
        log = StructuredLogger("test.error")
        log.error("something_failed", error="TimeoutError")
        captured = capsys.readouterr()
        payload = json.loads(captured.err.strip())
        assert payload["level"] == "ERROR"


# --- agent_actions tools ---

# Set required env vars before importing the handler
os.environ.setdefault("RECORDS_TABLE",    "ti-records")
os.environ.setdefault("LINEAGE_TABLE",    "ti-lineage")
os.environ.setdefault("ESCALATION_TOPIC", "")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../lambda/agent_actions"))

from handler import (  # noqa: E402
    classify_call_type,
    classify_category,
    analyze_sentiment,
    extract_issues,
    route_escalation,
)


class TestAgentActionsClassifyCallType:
    def test_support(self):
        assert classify_call_type("Support Case #1")["call_type"] == "support"

    def test_external(self):
        assert classify_call_type("Aegis / Renewal")["call_type"] == "external"

    def test_internal(self):
        assert classify_call_type("Retro Meeting")["call_type"] == "internal"


class TestAgentActionsClassifyCategory:
    def test_product_reliability_from_topics(self):
        r = classify_category("outage,performance,latency")
        assert r["primary_category"] == "Product Reliability"

    def test_compliance_topic(self):
        r = classify_category("soc 2,compliance,audit")
        assert r["primary_category"] == "Compliance & Audit"

    def test_fallback_other(self):
        r = classify_category("quarterly review,general discussion")
        assert r["primary_category"] == "Other"

    def test_multiple_categories_returned(self):
        r = classify_category("outage,billing,contract")
        assert len(r["all_categories"]) >= 2

    def test_accepts_list_input(self):
        r = classify_category(["onboarding", "deployment"])
        assert r["primary_category"] == "Onboarding & Adoption"


class TestAgentActionsSentiment:
    def test_anomaly_below_floor(self):
        # support baseline 2.94, std ~0.8 - score 1.5 -> z = -1.8 -> anomaly
        r = analyze_sentiment("support", 1.5, "very-negative")
        assert r["is_anomaly"] is True
        assert r["z_score"] < -1.5

    def test_normal_within_range(self):
        # score right at baseline -> z ~= 0
        r = analyze_sentiment("support", 2.94, "neutral")
        assert r["is_anomaly"] is False
        assert abs(r["z_score"]) < 0.1

    def test_external_baseline_applied(self):
        # external baseline 4.21 - score of 2.94 is anomalously low
        r = analyze_sentiment("external", 2.94, "negative")
        assert r["baseline"] == pytest.approx(4.21)
        assert r["is_anomaly"] is True

    def test_unknown_call_type_defaults(self):
        r = analyze_sentiment("unknown_type", 3.0, "neutral")
        assert r["baseline"] == 3.0   # fallback baseline


class TestAgentActionsExtractIssues:
    def test_p0_outage_detected(self):
        r = extract_issues("Production outage affecting all tenants")
        assert any(i["severity"] == "p0" for i in r["issues"])

    def test_p1_degraded_detected(self):
        r = extract_issues("Service is degraded for some customers")
        assert any(i["severity"] == "p1" for i in r["issues"])

    def test_p2_bug_detected(self):
        r = extract_issues("Intermittent login bug reported by users")
        assert any(i["severity"] in ("p2", "p3") for i in r["issues"])

    def test_no_issues_clean_transcript(self):
        r = extract_issues("Great call, everything is working perfectly")
        assert r["n_issues"] == 0

    def test_key_moments_enrichment(self):
        moments = [{"text": "System outage at 2 AM caused data loss", "type": "incident"}]
        r = extract_issues("outage reported", json.dumps(moments))
        assert any(i["severity"] == "p0" for i in r["issues"])

    def test_p0_short_circuits_lower_severities(self):
        # p0 should stop searching - only one issue returned
        r = extract_issues("Critical outage AND intermittent bug AND billing question")
        sevs = [i["severity"] for i in r["issues"]]
        assert "p0" in sevs
        assert len(sevs) == 1  # stops at p0

    def test_deduplication(self):
        r = extract_issues("outage outage outage critical down")
        p0_count = sum(1 for i in r["issues"] if i["severity"] == "p0")
        assert p0_count == 1


class TestAgentActionsRouteEscalation:
    def test_p0_product_reliability_routes_engineering(self):
        r = route_escalation("Product Reliability", "p0", 2.0, True, "support")
        assert r["team"] == "engineering"
        assert r["pagerduty"] is True

    def test_compliance_p0_routes_security(self):
        r = route_escalation("Compliance & Audit", "p0", 3.0, False, "external")
        assert r["team"] == "security"

    def test_customer_risk_p1_routes_account(self):
        r = route_escalation("Customer Risk", "p1", 2.5, False, "external")
        assert r["team"] == "account"

    def test_anomaly_low_sentiment_routes_cs(self):
        r = route_escalation("Other", "p3", 2.0, True, "support")
        assert r["team"] == "cs"

    def test_p3_no_anomaly_routes_support(self):
        r = route_escalation("Other", "p3", 3.5, False, "support")
        assert r["team"] == "support"
        assert r["pagerduty"] is False

    def test_requires_escalation_flag(self):
        r = route_escalation("Product Reliability", "p1", 2.0, False, "support")
        assert r["requires_escalation"] is True

    def test_is_anomaly_string_coercion(self):
        # Bedrock Agent passes boolean-ish strings
        r = route_escalation("Other", "p3", 2.0, "true", "support")
        assert r["team"] == "cs"


# --- PII redaction ---
# Load ingestion_pii handler under an explicit module name to avoid the
# "handler" name collision with the already-imported agent_actions handler.

import importlib.util as _ilu

os.environ.setdefault("PROCESSED_BUCKET",           "test-bucket")
os.environ.setdefault("PII_VAULT_TABLE",             "ti-pii-vault")
os.environ.setdefault("ORCHESTRATOR_FUNCTION_NAME",  "ti-orchestrator")
os.environ.setdefault("SSM_AGENT_ID_PATH",           "/ti/bedrock-agent-id")
os.environ.setdefault("SSM_AGENT_ALIAS_PATH",        "/ti/bedrock-agent-alias-id")

_ingestion_path = os.path.join(
    os.path.dirname(__file__), "../../lambda/ingestion_pii/handler.py"
)
_spec = _ilu.spec_from_file_location("ingestion_pii_handler", _ingestion_path)
_ingestion_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_ingestion_mod)
_PII_PATTERNS = _ingestion_mod.PII_PATTERNS


class TestPIIPatterns:
    """Verify each pattern matches its target and does NOT match vendor emails."""

    def _redact_text(self, text: str) -> str:
        """Run just the regex layer without touching AWS."""
        import re, hashlib
        for pattern, etype in _PII_PATTERNS:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                original = match.group()
                h = hashlib.md5(original.lower().encode()).hexdigest()[:6]
                ph = f"[{etype}_{h}]"
                text = text.replace(original, ph)
        return text

    def test_email_redacted(self):
        out = self._redact_text("Contact john.doe@acme.com for details")
        assert "john.doe@acme.com" not in out
        assert "EMAIL" in out

    def test_vendor_email_preserved(self):
        out = self._redact_text("Reach out to alice@aegiscloud.com")
        assert "alice@aegiscloud.com" in out

    def test_phone_redacted(self):
        out = self._redact_text("Call me at 415-555-1234")
        assert "415-555-1234" not in out
        assert "PHONE" in out

    def test_ssn_redacted(self):
        out = self._redact_text("SSN is 123-45-6789")
        assert "123-45-6789" not in out
        assert "SSN" in out

    def test_credit_card_redacted(self):
        out = self._redact_text("Card number 4111111111111111")
        assert "4111111111111111" not in out
        assert "CREDIT_CARD" in out

    def test_ip_address_redacted(self):
        out = self._redact_text("Server at 192.168.1.100 is down")
        assert "192.168.1.100" not in out
        assert "IP_ADDRESS" in out

    def test_aws_access_key_redacted(self):
        out = self._redact_text("Key: AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "AWS_ACCESS_KEY" in out

    def test_anthropic_api_key_redacted(self):
        out = self._redact_text("Token: sk-ant-api03-abcdefghijklmnopqrstuvwxyz01234567")
        assert "sk-ant" not in out
        assert "API_KEY" in out

    def test_clean_text_unchanged(self):
        text = "The Aegis platform had a great quarter with strong adoption."
        out = self._redact_text(text)
        assert out == text


# --- Export Lambda ---

os.environ.setdefault("EXPORT_S3_PREFIX", "exports/records/")

_export_path = os.path.join(os.path.dirname(__file__), "../../lambda/export/handler.py")
_export_spec = _ilu.spec_from_file_location("export_handler", _export_path)
_export_mod  = _ilu.module_from_spec(_export_spec)
_export_spec.loader.exec_module(_export_mod)


class TestExportLambda:
    def test_to_json_safe_decimal(self):
        assert _export_mod._to_json_safe(Decimal("3.14")) == pytest.approx(3.14)
        assert isinstance(_export_mod._to_json_safe(Decimal("1")), float)

    def test_to_json_safe_nested_dict_and_list(self):
        result = _export_mod._to_json_safe({"score": Decimal("2.5"), "items": [Decimal("1.0")]})
        assert result["score"] == pytest.approx(2.5)
        assert result["items"][0] == pytest.approx(1.0)

    def test_to_json_safe_passthrough(self):
        assert _export_mod._to_json_safe("hello") == "hello"
        assert _export_mod._to_json_safe(42)      == 42
        assert _export_mod._to_json_safe(True)    is True

    def test_lambda_handler_s3_key_contains_today(self):
        from unittest.mock import patch, MagicMock
        from datetime import datetime, timezone
        mock_s3      = MagicMock()
        mock_records = MagicMock()
        mock_records.scan.return_value = {"Items": [{"meeting_id": "abc"}]}
        with patch.object(_export_mod, "_s3", mock_s3), \
             patch.object(_export_mod, "_records", mock_records):
            resp = _export_mod.lambda_handler({}, None)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert today in resp["s3_key"]
        assert "exports/records/" in resp["s3_key"]

    def test_lambda_handler_returns_correct_count(self):
        from unittest.mock import patch, MagicMock
        items = [{"meeting_id": f"m{i}"} for i in range(5)]
        mock_s3      = MagicMock()
        mock_records = MagicMock()
        mock_records.scan.return_value = {"Items": items}
        with patch.object(_export_mod, "_s3", mock_s3), \
             patch.object(_export_mod, "_records", mock_records):
            resp = _export_mod.lambda_handler({}, None)
        assert resp["n_records"] == 5
        assert resp["status"] == "ok"

    def test_lambda_handler_calls_s3_put_object(self):
        from unittest.mock import patch, MagicMock
        mock_s3      = MagicMock()
        mock_records = MagicMock()
        mock_records.scan.return_value = {"Items": [{"meeting_id": "x"}]}
        with patch.object(_export_mod, "_s3", mock_s3), \
             patch.object(_export_mod, "_records", mock_records):
            _export_mod.lambda_handler({}, None)
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["ContentType"] == "application/x-ndjson"

    def test_scan_paginates_until_no_last_key(self):
        from unittest.mock import patch, MagicMock
        page1 = {"Items": [{"id": "a"}], "LastEvaluatedKey": {"id": "a"}}
        page2 = {"Items": [{"id": "b"}]}
        mock_records = MagicMock()
        mock_records.scan.side_effect = [page1, page2]
        with patch.object(_export_mod, "_records", mock_records):
            items = _export_mod.scan_all_records()
        assert len(items) == 2
        assert mock_records.scan.call_count == 2


# --- QuickSight Setup Lambda (CDK Custom Resource handler) ---

os.environ.setdefault("QS_ACCOUNT_ID",         "123456789012")
os.environ.setdefault("QS_REGION",             "us-east-1")
os.environ.setdefault("QS_USER_ARN",
    "arn:aws:quicksight:us-east-1:123456789012:user/default/test-admin")
os.environ.setdefault("QS_DATASOURCE_ID",      "ti-athena-datasource")
os.environ.setdefault("ATHENA_WORKGROUP_NAME",  "ti-analytics")
os.environ.setdefault("GLUE_DATABASE_NAME",    "ti_analytics")
os.environ.setdefault("GLUE_TABLE_NAME",       "ti_records_export")

_qs_path = os.path.join(os.path.dirname(__file__), "../../lambda/qs_setup/handler.py")
_qs_spec = _ilu.spec_from_file_location("qs_setup_handler", _qs_path)
_qs_mod  = _ilu.module_from_spec(_qs_spec)
_qs_spec.loader.exec_module(_qs_mod)


class TestQsSetupLambda:
    def test_dashboard_def_has_six_visuals(self):
        defn = _qs_mod.build_dashboard_def()
        assert len(defn["Sheets"]) == 1
        assert len(defn["Sheets"][0]["Visuals"]) == 6

    def test_dashboard_def_references_correct_dataset_arn(self):
        defn = _qs_mod.build_dashboard_def()
        decls = defn["DataSetIdentifierDeclarations"]
        assert len(decls) == 1
        assert decls[0]["Identifier"] == "records"
        assert "ti-records-dataset" in decls[0]["DataSetArn"]
        assert "123456789012" in decls[0]["DataSetArn"]

    def test_on_event_create_calls_both_upserts(self):
        from unittest.mock import patch
        with patch.object(_qs_mod, "upsert_dataset") as mock_ds, \
             patch.object(_qs_mod, "upsert_dashboard") as mock_db:
            result = _qs_mod.on_event({"RequestType": "Create"}, None)
        mock_ds.assert_called_once()
        mock_db.assert_called_once()
        assert result["PhysicalResourceId"] == "qs-setup-singleton"

    def test_on_event_update_calls_both_upserts(self):
        from unittest.mock import patch
        with patch.object(_qs_mod, "upsert_dataset") as mock_ds, \
             patch.object(_qs_mod, "upsert_dashboard") as mock_db:
            _qs_mod.on_event({"RequestType": "Update"}, None)
        mock_ds.assert_called_once()
        mock_db.assert_called_once()

    def test_on_event_delete_calls_delete_functions(self):
        from unittest.mock import patch, MagicMock
        mock_qs = MagicMock()
        with patch.object(_qs_mod, "qs", mock_qs):
            _qs_mod.on_event({"RequestType": "Delete"}, None)
        mock_qs.delete_dashboard.assert_called_once()
        mock_qs.delete_data_set.assert_called_once()

    def test_upsert_dataset_calls_create_with_correct_id(self):
        from unittest.mock import patch, MagicMock
        mock_qs = MagicMock()
        with patch.object(_qs_mod, "qs", mock_qs):
            _qs_mod.upsert_dataset()
        call_kwargs = mock_qs.create_data_set.call_args[1]
        assert call_kwargs["DataSetId"] == "ti-records-dataset"
        assert call_kwargs["ImportMode"] == "DIRECT_QUERY"

    def test_upsert_dataset_falls_back_to_update_on_conflict(self):
        from unittest.mock import patch, MagicMock
        mock_qs = MagicMock()
        mock_qs.exceptions.ResourceExistsException = Exception
        mock_qs.create_data_set.side_effect = Exception("already exists")
        with patch.object(_qs_mod, "qs", mock_qs):
            _qs_mod.upsert_dataset()
        mock_qs.update_data_set.assert_called_once()

    def test_upsert_dashboard_calls_create_with_correct_id(self):
        from unittest.mock import patch, MagicMock
        mock_qs = MagicMock()
        with patch.object(_qs_mod, "qs", mock_qs):
            _qs_mod.upsert_dashboard()
        call_kwargs = mock_qs.create_dashboard.call_args[1]
        assert call_kwargs["DashboardId"] == "ti-analytics-dashboard"
        assert "Definition" in call_kwargs

    def test_upsert_dashboard_falls_back_to_update_on_conflict(self):
        from unittest.mock import patch, MagicMock
        mock_qs = MagicMock()
        mock_qs.exceptions.ResourceExistsException = Exception
        mock_qs.create_dashboard.side_effect = Exception("already exists")
        with patch.object(_qs_mod, "qs", mock_qs):
            _qs_mod.upsert_dashboard()
        mock_qs.update_dashboard.assert_called_once()

    def test_delete_swallows_not_found_errors(self):
        from unittest.mock import patch, MagicMock
        import botocore.exceptions
        mock_qs = MagicMock()
        error_response = {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}}
        mock_qs.delete_dashboard.side_effect = botocore.exceptions.ClientError(
            error_response, "DeleteDashboard"
        )
        mock_qs.delete_data_set.side_effect = botocore.exceptions.ClientError(
            error_response, "DeleteDataSet"
        )
        with patch.object(_qs_mod, "qs", mock_qs):
            # Should not raise
            _qs_mod.delete_all()

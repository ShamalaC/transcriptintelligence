"""
Shared helpers -- single source of truth for utilities across all pipeline Lambdas.
Deployed as part of the ti-shared-utils Lambda Layer.
All business constants (TAXONOMY, ESCALATION_RULES, etc.) live in ti_config.
"""
import json
import logging
from decimal import Decimal
from typing import Any

from ti_config import BEDROCK_PRICING, BEDROCK_PRICING_DEFAULT


def dec(v: Any) -> Any:
    """Recursively convert float -> Decimal for DynamoDB write compatibility."""
    if isinstance(v, float):   return Decimal(str(round(v, 6)))
    if isinstance(v, Decimal): return v
    if isinstance(v, dict):    return {k: dec(vv) for k, vv in v.items()}
    if isinstance(v, list):    return [dec(i) for i in v]
    return v


class TokenTracker:
    """
    Accumulates Bedrock token usage across all iterations of an agentic loop.

    Attach to a run_agent call, call .add(response.usage) each iteration,
    then emit .as_dict() to the lineage record and CloudWatch.
    """

    def __init__(self, model: str) -> None:
        self.model         = model
        self.input_tokens  = 0
        self.output_tokens = 0

    def add(self, usage: Any) -> None:
        self.input_tokens  += getattr(usage, "input_tokens",  0)
        self.output_tokens += getattr(usage, "output_tokens", 0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def estimated_cost_usd(self) -> float:
        prices = BEDROCK_PRICING.get(self.model, BEDROCK_PRICING_DEFAULT)
        return round(
            self.input_tokens  / 1_000_000 * prices["input"] +
            self.output_tokens / 1_000_000 * prices["output"],
            6,
        )

    def as_dict(self) -> dict:
        return {
            "input_tokens":       self.input_tokens,
            "output_tokens":      self.output_tokens,
            "total_tokens":       self.total_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "model":              self.model,
        }


class StructuredLogger:
    """
    Emits JSON log lines -- CloudWatch Logs Insights can query every field.

    Usage:
        log = StructuredLogger(__name__)
        log.info("pii_redacted", meeting_id="abc", n_replacements=3)
        log.error("bedrock_call_failed", meeting_id="abc", error="ThrottlingException")
    """

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)
        if not self._log.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._log.addHandler(handler)
        self._log.setLevel(logging.INFO)

    def info(self, event: str, **kw: Any) -> None:
        self._log.info(json.dumps({"level": "INFO",  "event": event, **kw}, default=str))

    def warning(self, event: str, **kw: Any) -> None:
        self._log.warning(json.dumps({"level": "WARN",  "event": event, **kw}, default=str))

    def error(self, event: str, **kw: Any) -> None:
        self._log.error(json.dumps({"level": "ERROR", "event": event, **kw}, default=str))

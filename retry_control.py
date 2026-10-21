"""Retry timing and a run-wide sleep budget shared by metadata and binary requests."""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def server_retry_after(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return max(0.0, (when - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def retry_delay(value: str | None, attempt: int) -> float:
    requested = server_retry_after(value)
    # Never shorten a valid server cooldown and then retry early. Budget policy stops instead.
    return requested if requested is not None else float(min(8, 2**attempt))


@dataclass
class RetryBudget:
    max_wait: float
    max_total_wait: float
    spent: float = 0.0
    retries: int = 0

    @property
    def remaining(self) -> float:
        return max(0.0, self.max_total_wait - self.spent)

    def rejection(self, delay: float) -> str | None:
        if delay > self.max_wait:
            return "single_wait_limit"
        if delay > self.remaining:
            return "run_wait_budget"
        return None

    def reserve(self, delay: float) -> None:
        self.spent += delay
        self.retries += 1

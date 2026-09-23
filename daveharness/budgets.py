"""Immutable limits for the new lifecycle; legacy adapters opt out of new ceilings."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunBudget:
    model_steps: int = 8
    errors: int = 2
    tool_calls: int | None = 32
    total_wall_seconds: float | None = 300.0
    tool_output_bytes: int | None = 65_536
    transcript_bytes: int | None = 2_097_152
    event_count: int | None = 1_000

    def __post_init__(self) -> None:
        for name in ("model_steps", "errors"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("tool_calls", "tool_output_bytes", "transcript_bytes", "event_count"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise ValueError(f"{name} must be a positive integer")
        wall = self.total_wall_seconds
        if wall is not None and (isinstance(wall, bool) or not isinstance(wall, (int, float)) or not math.isfinite(wall) or wall <= 0):
            raise ValueError("total_wall_seconds must be finite and positive")

    @classmethod
    def legacy(cls, *, model_steps: int = 8, errors: int = 2) -> RunBudget:
        """Keep shipped routes at their original step and error limits."""
        return cls(model_steps, errors, None, None, None, None, None)

    def exceeded(self, name: str, count: int) -> bool:
        if name not in {"model_steps", "errors", "tool_calls", "tool_output_bytes", "transcript_bytes", "event_count"}:
            raise ValueError("unknown budget metric")
        limit = getattr(self, name)
        return limit is not None and count > limit

    def wall_expired(self, started: float, now: float) -> bool:
        return self.total_wall_seconds is not None and now - started >= self.total_wall_seconds

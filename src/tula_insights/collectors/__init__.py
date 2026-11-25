"""Collectors: one module per source of raw items."""

from .base import Collector, CollectorError, RunContext
from .registry import build_collector, known_collectors, plan_sources

__all__ = [
    "Collector",
    "CollectorError",
    "RunContext",
    "build_collector",
    "known_collectors",
    "plan_sources",
]

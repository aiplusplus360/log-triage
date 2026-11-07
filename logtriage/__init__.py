"""Cluster logs into templates and surface only what is new."""

__version__ = "0.1.0"

from .core import (
    SEVERITIES,
    Cluster,
    baseline_document,
    bursts,
    cluster,
    compare_with_baseline,
    read_baseline,
    read_lines,
    severity_of,
    template_of,
)

__all__ = [
    "SEVERITIES",
    "Cluster",
    "baseline_document",
    "bursts",
    "cluster",
    "compare_with_baseline",
    "read_baseline",
    "read_lines",
    "severity_of",
    "template_of",
]

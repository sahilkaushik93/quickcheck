"""Configuration-driven, bounded aggregation contracts for Execution.

Public request and output models remain canonical in :mod:`app.execution.models`.
This package adds runtime policy and resolution context used by the forthcoming
resolver and mergeable accumulators.
"""

from app.execution.aggregation.models import (
    AggregationColumnDescriptor,
    AggregationPolicy,
    CardinalityEstimate,
    DimensionPolicy,
    FilterPolicy,
    TimePolicy,
)

__all__ = [
    "AggregationColumnDescriptor",
    "AggregationPolicy",
    "CardinalityEstimate",
    "DimensionPolicy",
    "FilterPolicy",
    "TimePolicy",
]

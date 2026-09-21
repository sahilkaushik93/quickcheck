"""Pure deterministic routing decisions for the trusted-insight graph."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from app.business_impact.graph.state import BusinessImpactGraphState, GraphNodeStatus
from app.business_impact.models import BusinessImpactStatus


class GraphRoutingError(ValueError):
    """Safe error raised for invalid graph-routing policy."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class GraphRoute(str, Enum):
    """Named destinations understood by the future graph builder."""

    RETRY = "retry"
    NEXT = "next"
    PARTIAL = "partial"
    FAILED = "failed"
    INSUFFICIENT_CONTEXT = "insufficient_context"
    INTERRUPT_FOR_REVIEW = "interrupt_for_review"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class GraphNodePolicy:
    node_id: str
    timeout_seconds: int
    max_attempts: int
    retryable_errors: frozenset[str]
    interrupt: bool = False


@dataclass(frozen=True, slots=True)
class GraphRoutingConfig:
    policy_version: str
    maximum_graph_steps: int
    nodes: Mapping[str, GraphNodePolicy]

    @classmethod
    def from_file(cls, path: str | Path) -> "GraphRoutingConfig":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            nodes = {
                str(node_id): GraphNodePolicy(
                    node_id=str(node_id),
                    timeout_seconds=int(raw["timeout_seconds"]),
                    max_attempts=int(raw["max_attempts"]),
                    retryable_errors=frozenset(str(value) for value in raw.get("retryable_errors", [])),
                    interrupt=bool(raw.get("interrupt", False)),
                )
                for node_id, raw in data["node_policies"].items()
            }
            config = cls(
                policy_version=str(data["policy_version"]),
                maximum_graph_steps=int(data["execution"]["maximum_graph_steps"]),
                nodes=nodes,
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise GraphRoutingError("GRAPH_POLICY_LOAD_FAILED", "Graph routing policy could not be loaded.") from exc
        if config.maximum_graph_steps < 1 or not config.nodes:
            raise GraphRoutingError("GRAPH_POLICY_INVALID", "Graph routing policy contains invalid limits.")
        if any(item.timeout_seconds < 1 or item.max_attempts < 1 for item in config.nodes.values()):
            raise GraphRoutingError("GRAPH_POLICY_INVALID", "Graph node policies contain invalid limits.")
        return config


@dataclass(frozen=True, slots=True)
class GraphRoutingDecision:
    """Pure routing outcome; the builder owns any state mutation."""

    route: GraphRoute
    status: BusinessImpactStatus | None
    reason_code: str
    retry_node_id: str | None = None


class GraphRouter:
    """Determines retry, partial, failure and human-review branches without I/O."""

    def __init__(self, config: GraphRoutingConfig) -> None:
        self._config = config

    def after_node(
        self,
        state: BusinessImpactGraphState,
        *,
        node_id: str,
        status: GraphNodeStatus,
        error_code: str | None = None,
    ) -> GraphRoutingDecision:
        """Route one completed node using configured retry and safety semantics."""

        policy = self._policy_for(node_id)
        if len(state.node_traces) >= self._config.maximum_graph_steps:
            return GraphRoutingDecision(GraphRoute.FAILED, BusinessImpactStatus.FAILED, "GRAPH_STEP_LIMIT_EXCEEDED")
        if state.requires_human_review or policy.interrupt and status == GraphNodeStatus.SUCCEEDED:
            return GraphRoutingDecision(GraphRoute.INTERRUPT_FOR_REVIEW, None, "HUMAN_REVIEW_REQUIRED")
        if status == GraphNodeStatus.SUCCEEDED:
            return GraphRoutingDecision(GraphRoute.NEXT, None, "NODE_SUCCEEDED")
        if status == GraphNodeStatus.INTERRUPTED:
            return GraphRoutingDecision(GraphRoute.INTERRUPT_FOR_REVIEW, None, "NODE_INTERRUPTED")
        if status == GraphNodeStatus.SKIPPED:
            return GraphRoutingDecision(GraphRoute.NEXT, None, "NODE_SKIPPED")
        if status != GraphNodeStatus.FAILED:
            raise GraphRoutingError("GRAPH_NODE_STATUS_INVALID", "Only terminal graph node statuses may be routed.")
        return self._after_failure(state, policy, error_code)

    def terminal_for_state(self, state: BusinessImpactGraphState) -> GraphRoutingDecision:
        """Choose a safe terminal state after validation and citation checks."""

        if state.requires_human_review:
            return GraphRoutingDecision(GraphRoute.INTERRUPT_FOR_REVIEW, None, "HUMAN_REVIEW_REQUIRED")
        if any(error.component == "validate_handoff" for error in state.errors):
            return GraphRoutingDecision(GraphRoute.FAILED, BusinessImpactStatus.FAILED, "INVALID_HANDOFF")
        if state.fact_pack is None or not state.fact_pack.facts:
            return GraphRoutingDecision(GraphRoute.INSUFFICIENT_CONTEXT, BusinessImpactStatus.INSUFFICIENT_CONTEXT, "INSUFFICIENT_INTERNAL_FACTS")
        if not state.external_context or not state.citations:
            return GraphRoutingDecision(GraphRoute.PARTIAL, BusinessImpactStatus.PARTIAL, "INSUFFICIENT_EXTERNAL_CONTEXT")
        if not state.final_claims:
            return GraphRoutingDecision(GraphRoute.PARTIAL, BusinessImpactStatus.PARTIAL, "TRUSTED_INSIGHT_UNAVAILABLE")
        if state.errors:
            return GraphRoutingDecision(GraphRoute.PARTIAL, BusinessImpactStatus.PARTIAL, "NON_FATAL_GRAPH_ERRORS")
        return GraphRoutingDecision(GraphRoute.COMPLETED, BusinessImpactStatus.COMPLETED, "ALL_VALIDATIONS_PASSED")

    def _after_failure(
        self,
        state: BusinessImpactGraphState,
        policy: GraphNodePolicy,
        error_code: str | None,
    ) -> GraphRoutingDecision:
        attempts = sum(1 for trace in state.node_traces if trace.node_id == policy.node_id)
        if error_code in policy.retryable_errors and attempts < policy.max_attempts:
            return GraphRoutingDecision(GraphRoute.RETRY, None, "RETRYABLE_NODE_FAILURE", policy.node_id)
        if policy.node_id == "validate_handoff":
            return GraphRoutingDecision(GraphRoute.FAILED, BusinessImpactStatus.FAILED, "INVALID_HANDOFF")
        if policy.node_id == "build_fact_pack":
            return GraphRoutingDecision(GraphRoute.INSUFFICIENT_CONTEXT, BusinessImpactStatus.INSUFFICIENT_CONTEXT, "INSUFFICIENT_INTERNAL_FACTS")
        if policy.node_id in {"retrieve_context", "verify_sources"}:
            return GraphRoutingDecision(GraphRoute.PARTIAL, BusinessImpactStatus.PARTIAL, "INSUFFICIENT_EXTERNAL_CONTEXT")
        if policy.node_id in {"generate_insight", "validate_citations", "policy_guard"}:
            return GraphRoutingDecision(GraphRoute.PARTIAL, BusinessImpactStatus.PARTIAL, "TRUSTED_INSIGHT_VALIDATION_FAILED")
        return GraphRoutingDecision(GraphRoute.PARTIAL, BusinessImpactStatus.PARTIAL, "NON_FATAL_NODE_FAILURE")

    def _policy_for(self, node_id: str) -> GraphNodePolicy:
        try:
            return self._config.nodes[node_id]
        except KeyError as exc:
            raise GraphRoutingError("GRAPH_NODE_UNCONFIGURED", "Graph node is not configured by graph policy.") from exc


__all__ = [
    "GraphNodePolicy",
    "GraphRoute",
    "GraphRouter",
    "GraphRoutingConfig",
    "GraphRoutingDecision",
    "GraphRoutingError",
]

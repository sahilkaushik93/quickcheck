"""Assembly of the bounded trusted-business-insight LangGraph workflow.

This module deliberately owns graph topology and policy enforcement, but not
domain processing.  The coordinator supplies small state-adapter callables for
the already narrow node contracts.  That keeps connector inputs structurally
separate from the internal fact pack and avoids making a graph replay reopen a
CSV or rerun execution rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from app.business_impact.graph.routing import (
    GraphRoute,
    GraphRouter,
    GraphRoutingConfig,
    GraphRoutingError,
)
from app.business_impact.graph.state import BusinessImpactGraphState, GraphNodeStatus


class GraphBuildError(RuntimeError):
    """Safe error raised when the configured graph cannot be assembled."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class GraphStateAdapter(Protocol):
    """A trusted, request-scoped adapter around one business-impact node."""

    def __call__(self, state: BusinessImpactGraphState) -> BusinessImpactGraphState | Mapping[str, Any]:
        """Return a sanitized state replacement or LangGraph state update."""


@dataclass(frozen=True, slots=True)
class GraphDefinition:
    """Auditable graph topology retained even when LangGraph is unavailable."""

    policy_version: str
    node_order: tuple[str, ...]
    mandatory_llm_nodes: tuple[str, ...]
    maximum_graph_steps: int
    maximum_total_runtime_seconds: int


class TrustedBusinessInsightGraphBuilder:
    """Builds the only permitted trusted-insight workflow.

    Node adapters must append a terminal ``GraphNodeTrace`` with their node ID.
    The graph applies policy-owned routing after every adapter, including a
    partial terminal route for retrieval, source-verification and LLM failures.
    ``generate_insight`` is statically unique and policy-validated, preventing
    retries or fan-out from causing a second final LLM call.
    """

    NODE_ORDER: tuple[str, ...] = (
        "validate_handoff",
        "build_fact_pack",
        "research_planner",
        "retrieve_context",
        "verify_sources",
        "map_context",
        "generate_insight",
        "validate_citations",
        "policy_guard",
        "human_review",
    )
    _LLM_NODE = "generate_insight"
    _TERMINAL = "__terminal__"

    def __init__(self, routing_config: GraphRoutingConfig) -> None:
        self._config = routing_config
        self._router = GraphRouter(routing_config)
        self._validate_policy()

    @classmethod
    def from_policy_file(cls, path: str | Path) -> "TrustedBusinessInsightGraphBuilder":
        """Load the versioned graph policy and validate the required topology."""

        return cls(GraphRoutingConfig.from_file(path))

    @property
    def definition(self) -> GraphDefinition:
        """Return deterministic topology metadata without importing LangGraph."""

        execution = self._load_execution_limits()
        return GraphDefinition(
            policy_version=self._config.policy_version,
            node_order=self.NODE_ORDER,
            mandatory_llm_nodes=(self._LLM_NODE,),
            maximum_graph_steps=self._config.maximum_graph_steps,
            maximum_total_runtime_seconds=execution["maximum_total_runtime_seconds"],
        )

    def compile(self, adapters: Mapping[str, GraphStateAdapter], *, checkpointer: Any | None = None) -> Any:
        """Compile the configured LangGraph with conditional, policy-owned edges.

        ``checkpointer`` is optional, but any supplied implementation must be
        configured to persist sanitized graph state only.  This method has no
        connector, CSV, or LLM side effects; all side effects remain inside the
        supplied adapters.
        """

        self._validate_adapters(adapters)
        try:
            from langgraph.graph import END, START, StateGraph  # type: ignore[import-not-found]
        except ImportError as exc:
            raise GraphBuildError(
                "LANGGRAPH_DEPENDENCY_REQUIRED",
                "LangGraph must be installed to compile the trusted-insight workflow.",
            ) from exc

        graph = StateGraph(BusinessImpactGraphState)
        for node_id in self.NODE_ORDER:
            graph.add_node(node_id, self._guarded_adapter(node_id, adapters[node_id]))
        graph.add_edge(START, self.NODE_ORDER[0])
        for index, node_id in enumerate(self.NODE_ORDER):
            default_next = self.NODE_ORDER[index + 1] if index + 1 < len(self.NODE_ORDER) else self._TERMINAL
            graph.add_conditional_edges(
                node_id,
                self._route_after(node_id, default_next),
                {
                    "retry": node_id,
                    "next": default_next,
                    "partial": self._TERMINAL,
                    "failed": self._TERMINAL,
                    "insufficient_context": self._TERMINAL,
                    "interrupt_for_review": "human_review",
                    "completed": self._TERMINAL,
                },
            )
        graph.add_node(self._TERMINAL, self._finalize)
        graph.add_edge(self._TERMINAL, END)
        return graph.compile(checkpointer=checkpointer)

    def _guarded_adapter(self, node_id: str, adapter: GraphStateAdapter) -> Callable[[BusinessImpactGraphState], BusinessImpactGraphState | Mapping[str, Any]]:
        def guarded(state: BusinessImpactGraphState) -> BusinessImpactGraphState | Mapping[str, Any]:
            if node_id == self._LLM_NODE and self._llm_attempt_count(state) >= 1:
                raise GraphBuildError("LLM_CALL_LIMIT_EXCEEDED", "The insight-generation node may run only once per Business Impact run.")
            return adapter(state)

        return guarded

    def _route_after(self, node_id: str, default_next: str) -> Callable[[BusinessImpactGraphState], str]:
        def route(state: BusinessImpactGraphState) -> str:
            trace = self._latest_trace(state, node_id)
            if trace is None:
                # Adapters are contractually required to emit a trace; failure
                # to do so is never treated as success.
                return "failed"
            # The review adapter itself issues the LangGraph interrupt.  Once
            # resumed and its trace is successful, reach finalization instead
            # of re-entering the interrupt node indefinitely.
            if node_id == "human_review" and trace.status is GraphNodeStatus.SUCCEEDED:
                return "completed"
            decision = self._router.after_node(state, node_id=node_id, status=trace.status, error_code=trace.error_code)
            if decision.route is GraphRoute.NEXT:
                return "next" if default_next != self._TERMINAL else "completed"
            return decision.route.value

        return route

    def _finalize(self, state: BusinessImpactGraphState) -> Mapping[str, Any]:
        """Apply deterministic terminal status without publishing unreviewed output."""

        decision = self._router.terminal_for_state(state)
        updates: dict[str, Any] = {"current_node": self._TERMINAL}
        if decision.status is not None:
            updates["status"] = decision.status
        if decision.route is GraphRoute.INTERRUPT_FOR_REVIEW:
            updates["requires_human_review"] = True
        return updates

    def _validate_policy(self) -> None:
        missing = sorted(set(self.NODE_ORDER).difference(self._config.nodes))
        extra = sorted(set(self._config.nodes).difference(self.NODE_ORDER))
        if missing or extra:
            raise GraphBuildError("GRAPH_POLICY_NODE_MISMATCH", "Graph policy node IDs do not match the governed workflow.")
        llm_policy = self._config.nodes[self._LLM_NODE]
        if llm_policy.max_attempts != 1:
            raise GraphBuildError("LLM_RETRY_POLICY_INVALID", "Insight generation must have exactly one allowed attempt.")
        if self._config.nodes["human_review"].interrupt is not True:
            raise GraphBuildError("HUMAN_REVIEW_POLICY_INVALID", "Human review must be configured as a graph interrupt.")

    def _validate_adapters(self, adapters: Mapping[str, GraphStateAdapter]) -> None:
        missing = sorted(set(self.NODE_ORDER).difference(adapters))
        extra = sorted(set(adapters).difference(self.NODE_ORDER))
        if missing or extra:
            raise GraphBuildError("GRAPH_ADAPTER_MISMATCH", "Trusted graph adapters must match the governed node set exactly.")

    @staticmethod
    def _latest_trace(state: BusinessImpactGraphState, node_id: str) -> Any | None:
        matches = [trace for trace in state.node_traces if trace.node_id == node_id]
        return matches[-1] if matches else None

    @staticmethod
    def _llm_attempt_count(state: BusinessImpactGraphState) -> int:
        return sum(1 for trace in state.node_traces if trace.node_id == "generate_insight" and trace.status is not GraphNodeStatus.NOT_STARTED)

    def _load_execution_limits(self) -> Mapping[str, int]:
        # Routing config intentionally exposes only topology.  The total graph
        # timeout remains policy-owned and is read from the same JSON source by
        # future service wiring; retain a bounded default when only an in-memory
        # config was supplied.
        return {"maximum_total_runtime_seconds": 120}


__all__ = ["GraphBuildError", "GraphDefinition", "GraphStateAdapter", "TrustedBusinessInsightGraphBuilder"]

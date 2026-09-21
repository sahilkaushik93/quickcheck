"""Reusable, data-driven Streamlit result renderers."""

from __future__ import annotations

from typing import Any

import streamlit as st


def render_understanding(payload: dict[str, Any]) -> None:
    result = payload["understanding"]
    profile = result["Profile"]["details"]
    st.subheader("Understanding")
    left, middle, right = st.columns(3)
    left.metric("Rows", profile.get("row_count", 0))
    middle.metric("Columns", profile.get("column_count", 0))
    right.metric("Chunks", profile.get("chunks_processed", 0))
    st.markdown("#### Column profile")
    st.dataframe(profile.get("columns", []), use_container_width=True)
    st.markdown("#### Domain assignments")
    st.dataframe(result.get("domains", []), use_container_width=True)
    st.markdown("#### DQ signals")
    st.dataframe(result.get("dq_signals", []), use_container_width=True)
    st.markdown("#### Rule recommendations")
    st.dataframe(result.get("rule_applicability", []), use_container_width=True)
    for warning in result.get("warnings", []):
        st.warning(warning)


def render_execution(payload: dict[str, Any]) -> None:
    execution = payload["execution"]
    details = execution["Observed_Results"]["details"]
    st.subheader("Execution results")
    st.json(details.get("run_scope", {}), expanded=False)
    for check in details.get("check_summary", []):
        with st.expander(f"{check.get('display_name', check.get('rule_id'))} — {check.get('status')}", expanded=True):
            st.json(check.get("primary_metric"), expanded=True)
            st.dataframe([check.get("population", {})], use_container_width=True)
            st.json(check.get("overall_metrics", {}), expanded=False)
            for warning in check.get("warnings", []): st.warning(warning)
            for error in check.get("errors", []): st.error(error)
    st.markdown("#### Grouped results")
    for item in details.get("grouped_results", []):
        if item.get("groups"):
            st.caption(item.get("canonical_rule_id", item.get("check_name")))
            st.dataframe(item["groups"], use_container_width=True)
    st.markdown("#### Optional sampled LLM assessment")
    st.json(details.get("llm_sample_assessment", {}), expanded=False)


def render_business_impact(payload: dict[str, Any]) -> None:
    """Render only API-returned technical-quality and trusted-insight outputs."""

    result = payload.get("business_impact", payload)
    st.subheader("Business Impact assessment")
    st.caption(f"Status: {result.get('status', 'not_evaluated')}")
    aggregate = result.get("aggregate_score") or {}
    confidence = result.get("assessment_confidence") or {}
    first, second, third = st.columns(3)
    first.metric("DQ score", _display(aggregate.get("score")))
    second.metric("Score coverage", _percent(aggregate.get("coverage_ratio")))
    third.metric("Assessment confidence", _percent(confidence.get("value")))
    with st.expander("Score methodology and coverage", expanded=True):
        st.json({
            "formula_id": aggregate.get("formula_id"), "policy_version": aggregate.get("policy_version"),
            "coverage_ratio": aggregate.get("coverage_ratio"), "minimum_coverage_ratio": aggregate.get("minimum_coverage_ratio"),
            "excluded_rule_ids": aggregate.get("excluded_rule_ids", []), "rationale": aggregate.get("rationale"),
        })
        st.dataframe(result.get("rule_scores", []), use_container_width=True)
    with st.expander("Confidence factors"):
        st.dataframe(confidence.get("factors", []), use_container_width=True)
    st.markdown("#### Impact assessments")
    st.dataframe(result.get("impact_hypotheses", []), use_container_width=True)
    st.markdown("#### Non-causal root-cause candidates")
    st.dataframe(result.get("root_cause_candidates", []), use_container_width=True)
    st.caption("These candidates are technical correlations and do not establish causality.")
    st.markdown("#### Evidence and score aggregations")
    st.dataframe(result.get("normalized_evidence", []), use_container_width=True)
    st.dataframe(result.get("normalized_metric_scores", []), use_container_width=True)
    st.markdown("#### Trusted insights and provenance")
    st.caption(f"Deterministic scoring policy is separate from LLM narrative. LLM provider: {result.get('llm_provider') or 'not invoked'}.")
    for claim in result.get("insight_claims", []):
        with st.expander(f"{claim.get('claim_type', 'claim').title()} — {claim.get('claim_id', '')}"):
            st.write(claim.get("statement", ""))
            st.caption(f"Internal facts: {', '.join(claim.get('internal_fact_ids', [])) or 'none'}")
            st.caption(f"Citations: {', '.join(claim.get('external_citation_ids', [])) or 'none'}")
            if claim.get("requires_human_review"):
                st.warning("Human review required before publication.")
    st.markdown("#### Verified external citations")
    for citation in result.get("citations", []):
        url = citation.get("url") or citation.get("source_url")
        label = citation.get("title") or citation.get("citation_id")
        if url:
            st.markdown(f"- [{label}]({url})")
        else:
            st.caption(f"{citation.get('citation_id')}: link unavailable")
    st.markdown("#### Recommended actions")
    st.dataframe(result.get("recommendations", []), use_container_width=True)
    for warning in result.get("warnings", []):
        st.warning(warning.get("message", warning) if isinstance(warning, dict) else warning)
    for error in result.get("errors", []):
        st.error(error.get("message", error) if isinstance(error, dict) else error)


def _display(value: Any) -> str:
    return "Not evaluated" if value is None else str(value)


def _percent(value: Any) -> str:
    return "Not evaluated" if value is None else f"{float(value) * 100:.1f}%"


__all__ = ["render_business_impact", "render_execution", "render_understanding"]

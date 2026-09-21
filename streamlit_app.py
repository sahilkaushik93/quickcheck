"""Streamlit UI for the FastAPI-backed LUMI Transcript DQ POC."""

from __future__ import annotations

import io
import json

import pandas as pd
import streamlit as st

from ui.api_client import APIClient, APIClientError
from ui.renderers import render_business_impact, render_execution, render_understanding


def _csv_values(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


st.set_page_config(page_title="UnDQ Transcript DQ", layout="wide")
st.title("UnDQ Transcript Data Quality")
st.info("Current POC scope: Understanding, Execution and governed Business Impact. Business-impact results do not represent financial loss, penalties, or causal findings.")
client = APIClient()
try:
    readiness = client.ready()
    st.sidebar.success(f"Backend: {readiness.get('status')}")
except APIClientError as exc:
    st.sidebar.error(str(exc))

csv_file = st.file_uploader("Structured transcript CSV", type=["csv"])
metadata_file = st.file_uploader("LUMI metadata dictionary", type=["xlsx"])
rules_file = st.file_uploader("Versioned rule registry ZIP", type=["zip"])

with st.sidebar:
    st.header("Semantic enrichment")
    llm_enabled = st.checkbox("Enable one sampled LLM request", value=False)
    provider = st.selectbox("Provider", ["ollama", "launchpad", "doc_intelligence"])
    model = st.text_input("Model (optional)")
    time_grain = st.selectbox("Time grain", ["none", "day", "week", "month", "quarter", "year"], index=3)
    time_column = st.text_input("Time column", value="intrct_ts")
    direction_column = st.text_input("Direction column", value="intrct_drct")

if csv_file:
    csv_bytes = csv_file.getvalue()
    try:
        preview = pd.read_csv(io.BytesIO(csv_bytes), nrows=100)
        st.caption(f"Preview: {len(preview)} rows × {len(preview.columns)} columns (bounded to 100 rows)")
        st.dataframe(preview, use_container_width=True)
    except Exception as exc:
        st.error(f"CSV preview failed: {exc}")

llm = {"enabled": llm_enabled, "provider": provider, "model": model or None, "request_id": "streamlit"}
if st.button("Run Understanding", disabled=not all((csv_file, metadata_file, rules_file))):
    try:
        with st.spinner("Profiling structured input..."):
            st.session_state["understanding_response"] = client.understanding(csv_name=csv_file.name, csv_bytes=csv_file.getvalue(), metadata_name=metadata_file.name, metadata_bytes=metadata_file.getvalue(), rules_name=rules_file.name, rules_bytes=rules_file.getvalue(), llm=llm)
    except APIClientError as exc:
        st.error(str(exc))

understanding_response = st.session_state.get("understanding_response")
if understanding_response:
    render_understanding(understanding_response)
    understanding = understanding_response["understanding"]
    steps = understanding.get("execution_plan", {}).get("steps", [])
    available = [step["rule_id"] for step in steps]
    selected = st.multiselect("Rules to execute", available, default=available)
    if st.button("Run selected rules", disabled=not selected):
        filtered = json.loads(json.dumps(understanding))
        aggregation = {"time": {"grain": time_grain, **({"column": time_column} if time_grain != "none" else {})}, "dimensions": ([{"column": direction_column, "alias": "interaction_direction"}] if direction_column else []), "include_overall": True}
        try:
            with st.spinner("Executing selected rules..."):
                st.session_state["execution_response"] = client.execution(
                    csv_name=csv_file.name,
                    csv_bytes=csv_file.getvalue(),
                    understanding=filtered,
                    aggregation=aggregation,
                    llm=llm,
                    selected_rule_ids=selected,
                )
        except APIClientError as exc:
            st.error(str(exc))

if st.session_state.get("execution_response"):
    render_execution(st.session_state["execution_response"])
    st.divider()
    st.header("Generate Business Impact")
    st.caption("The service receives only completed canonical Understanding and Execution outputs. Public research uses configured allowlisted sources; this UI cannot override that policy.")
    with st.expander("Business context", expanded=True):
        context_id = st.text_input("Context ID", value="context-streamlit")
        capabilities = st.text_input("Business capabilities (comma separated)", value="customer service")
        channels = st.text_input("Service channels (comma separated)", value="voice")
        regulated_domains = st.text_input("Regulated domains (comma separated)", value="consumer financial services")
        impact_categories = st.text_input("Approved impact categories (comma separated)", value="data quality, operational reliability")
        persist_impact_artifacts = st.checkbox("Persist compact Business Impact artifacts", value=False)
        external_context_required = st.checkbox("Require verified public context", value=True)
    left, right = st.columns(2)
    impact_provider = left.selectbox("Required Business Impact LLM provider", ["ollama", "launchpad", "doc_intelligence"], key="impact_provider")
    impact_model = right.text_input("Business Impact LLM model", value="", key="impact_model")
    st.caption("Approved public sources: SEC, American Express Investor Relations, CFPB, Federal Reserve, OCC, FTC and Federal Register.")
    if st.button("Generate Business Impact"):
        understanding_payload = st.session_state.get("understanding_response", {}).get("understanding", {})
        canonical_understanding = understanding_payload.get("output")
        canonical_execution = st.session_state["execution_response"].get("execution", {}).get("output")
        if not canonical_understanding or not canonical_execution:
            st.error("Canonical UnderstandingOutput and ExecutionOutput are required. Run the compatible backend integration first.")
        else:
            context = {
                "context_id": context_id,
                "context_schema_version": "1.0",
                "business_capabilities": _csv_values(capabilities),
                "service_channels": _csv_values(channels),
                "regulated_domains": _csv_values(regulated_domains),
                "approved_impact_categories": _csv_values(impact_categories),
                "supplied_by": "streamlit",
            }
            try:
                with st.spinner("Calculating deterministic scores and generating one governed insight..."):
                    response = client.business_impact(
                        understanding=canonical_understanding,
                        execution=canonical_execution,
                        business_context=context,
                        options={"persist_artifacts": persist_impact_artifacts, "external_context_required": external_context_required},
                        llm={"provider": impact_provider, "model": impact_model or None, "request_id": "streamlit-impact"},
                    )
                    st.session_state["business_impact_response"] = response
            except APIClientError as exc:
                st.error(str(exc))

business_impact_response = st.session_state.get("business_impact_response")
if business_impact_response:
    render_business_impact(business_impact_response)
    impact = business_impact_response.get("business_impact", {})
    references = impact.get("artifact_references", {})
    if references:
        st.markdown("#### Business Impact artifacts")
        run_id = impact.get("run_id")
        if run_id:
            try:
                available = client.list_business_impact_artifacts(run_id).get("artifacts", [])
                for filename in available:
                    body = client.download_business_impact_artifact(run_id, filename)
                    st.download_button(f"Download {filename}", data=body, file_name=filename, key=f"impact-{run_id}-{filename}")
            except APIClientError as exc:
                st.warning(str(exc))

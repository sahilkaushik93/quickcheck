"""Optional final fallback; disabled unless a dedicated approved source is configured."""
from app.business_impact.external_context.base import ApprovedSource, ConnectorLimits, ConnectorResponse, ExternalContextConnector, ExternalContextPolicyError
from app.business_impact.external_context.models import PublicResearchTopic
from app.business_impact.external_context.query_guard import PublicQueryGuard

class ApprovedWebSearchConnector(ExternalContextConnector):
    """Policy gate for a future enterprise search connector, never open web browsing."""
    def __init__(self, query_guard: PublicQueryGuard) -> None: self._query_guard = query_guard
    @property
    def connector_id(self) -> str: return "approved_web_search"
    def retrieve(self, topic: PublicResearchTopic, *, source: ApprovedSource, limits: ConnectorLimits) -> ConnectorResponse:
        self._query_guard.validate_topic(topic)
        del source, limits
        raise ExternalContextPolicyError("APPROVED_SEARCH_NOT_CONFIGURED", "Approved web search is unavailable until a dedicated allowlisted search source is configured.")

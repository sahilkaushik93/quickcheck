"""OCC primary-authority public source connector."""
from app.business_impact.external_context.base import ApprovedSource
from app.business_impact.external_context.connectors import HttpPublicConnector

class OccConnector(HttpPublicConnector):
    @property
    def connector_id(self) -> str: return "occ"
    def endpoint(self, source: ApprovedSource) -> str: return f"https://{sorted(source.allowed_hosts)[0]}/topics/"

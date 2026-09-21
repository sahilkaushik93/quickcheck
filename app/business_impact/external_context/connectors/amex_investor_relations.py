"""American Express Investor Relations primary-corporate connector."""
from app.business_impact.external_context.base import ApprovedSource
from app.business_impact.external_context.connectors import HttpPublicConnector

class AmexInvestorRelationsConnector(HttpPublicConnector):
    @property
    def connector_id(self) -> str: return "amex_investor_relations"
    def endpoint(self, source: ApprovedSource) -> str:
        host = next((item for item in source.allowed_hosts if item.startswith("ir.")), sorted(source.allowed_hosts)[0])
        return f"https://{host}/"
    def document_type(self, url: str, source: ApprovedSource) -> str:
        return "news_release" if "news" in url else super().document_type(url, source)

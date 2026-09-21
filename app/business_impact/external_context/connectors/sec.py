"""SEC primary-authority public source connector."""
from app.business_impact.external_context.base import ApprovedSource
from app.business_impact.external_context.connectors import HttpPublicConnector

class SecConnector(HttpPublicConnector):
    @property
    def connector_id(self) -> str: return "sec"
    def endpoint(self, source: ApprovedSource) -> str:
        host = sorted(source.allowed_hosts)[0]
        return f"https://{host}/news/pressreleases"
    def document_type(self, url: str, source: ApprovedSource) -> str:
        return "press_release" if "news" in url else super().document_type(url, source)

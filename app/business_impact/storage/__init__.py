"""Compact, privacy-safe Business Impact artifact persistence."""

from app.business_impact.storage.artifact_writer import BusinessImpactArtifactWriter, ArtifactWriteError

__all__ = ["ArtifactWriteError", "BusinessImpactArtifactWriter"]

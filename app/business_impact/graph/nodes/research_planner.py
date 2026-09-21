"""Create generic, policy-owned public research topics from approved conditions."""

from __future__ import annotations

from dataclasses import dataclass

from app.business_impact.external_context.models import PublicResearchTopic
from app.business_impact.external_context.query_guard import PublicQueryGuard, QueryPolicyError


@dataclass(frozen=True, slots=True)
class ResearchCondition:
    """Approved condition identifier; contains no values, metrics or source data."""

    canonical_rule_id: str
    condition_id: str


@dataclass(frozen=True, slots=True)
class ResearchPlan:
    """Deterministic public-only plan for the retrieval node."""

    topics: tuple[PublicResearchTopic, ...]
    skipped_conditions: tuple[ResearchCondition, ...]
    warnings: tuple[str, ...]


class ResearchPlanner:
    """Uses only mapping-owned templates; no internal facts are an input."""

    def __init__(self, query_guard: PublicQueryGuard, *, maximum_topics: int) -> None:
        if maximum_topics < 1:
            raise ValueError("maximum_topics must be positive")
        self._guard = query_guard
        self._maximum_topics = maximum_topics

    def plan(self, conditions: tuple[ResearchCondition, ...]) -> ResearchPlan:
        """Resolve configured generic topics, safely skipping unmapped conditions."""

        topics: list[PublicResearchTopic] = []
        skipped: list[ResearchCondition] = []
        for condition in sorted(set(conditions), key=lambda item: (item.canonical_rule_id, item.condition_id)):
            try:
                candidates = self._guard.build_topics(canonical_rule_id=condition.canonical_rule_id, condition_id=condition.condition_id)
            except QueryPolicyError:
                skipped.append(condition)
                continue
            for topic in candidates:
                if len(topics) >= self._maximum_topics:
                    break
                self._guard.validate_topic(topic)
                topics.append(topic)
        warnings = () if not skipped else ("Some approved conditions have no configured public-research mapping.",)
        return ResearchPlan(tuple(topics), tuple(skipped), warnings)


__all__ = ["ResearchCondition", "ResearchPlan", "ResearchPlanner"]

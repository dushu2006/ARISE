"""Deterministic target resolution; visual/model output is only a candidate source."""

from __future__ import annotations

import re
from collections.abc import Iterable

from arise.core.computer import (
    CoordinateMapper,
    PerceptionSource,
    Rect,
    ResolutionStatus,
    TargetCandidate,
    TargetQuery,
    TargetResolution,
)

_WORDS = re.compile(r"[^\w]+", flags=re.UNICODE)


def _normalized_words(value: str) -> tuple[str, ...]:
    return tuple(part for part in _WORDS.split(value.casefold()) if part)


def _semantic_match(query: str, candidate: TargetCandidate) -> float:
    if not query.strip():
        return 0.82
    identity = candidate.descriptor.identity
    label = " ".join(
        part
        for part in (
            identity.semantic_name,
            identity.role,
            identity.object_id,
            candidate.descriptor.automation_id,
            *candidate.descriptor.hierarchy,
        )
        if part
    )
    wanted = _normalized_words(query)
    actual = _normalized_words(label)
    if not wanted or not actual:
        return 0.0
    if wanted == actual:
        return 1.0
    if len(wanted) <= len(actual) and any(
        actual[index : index + len(wanted)] == wanted
        for index in range(len(actual) - len(wanted) + 1)
    ):
        return 0.92
    wanted_set = set(wanted)
    actual_set = set(actual)
    overlap = len(wanted_set & actual_set)
    if not overlap:
        return 0.0
    precision = overlap / len(actual_set)
    recall = overlap / len(wanted_set)
    return 2 * precision * recall / (precision + recall)


def _candidate_score(query: TargetQuery, candidate: TargetCandidate) -> float:
    identity = candidate.descriptor.identity
    if not candidate.descriptor.visible or not candidate.descriptor.enabled:
        return 0.0
    if query.role is not None and (identity.role or "").casefold() != query.role.casefold():
        return 0.0
    for expected, actual in (
        (query.application, identity.application),
        (query.process_id, identity.process_id),
        (query.window_id, identity.window_id),
        (query.page_id, identity.page_id),
    ):
        if expected is not None and expected != actual:
            return 0.0
    semantic = _semantic_match(query.semantic_name, candidate)
    selector = (
        candidate.descriptor.selector_quality.score
        if candidate.descriptor.selector_quality is not None
        else 0.70
    )
    # Adapter confidence cannot substitute for semantic match or selector quality.
    return (semantic * 0.55) + (selector * 0.25) + (candidate.confidence * 0.20)


class TargetResolver:
    """Combine candidates by source priority and refuse ambiguous matches.

    The source order is an architectural policy: application API, DOM, UIA,
    accessibility, OCR/layout, vision, then explicit coordinate fallback.
    A lower-priority source never displaces a usable higher-priority result.
    """

    def resolve(
        self,
        query: TargetQuery,
        candidates: Iterable[TargetCandidate],
    ) -> TargetResolution:
        scored: list[tuple[TargetCandidate, float]] = []
        for candidate in candidates:
            source = candidate.descriptor.source
            if source not in query.allowed_sources:
                continue
            if source is PerceptionSource.COORDINATE and not query.allow_coordinate_fallback:
                continue
            if candidate.confidence < query.minimum_confidence:
                continue
            score = _candidate_score(query, candidate)
            if score > 0:
                scored.append((candidate, score))

        # Do not deduplicate by stable fingerprint: two simultaneously visible
        # controls can share the same semantic identity and must remain ambiguous.
        ranked = sorted(
            scored,
            key=lambda item: (item[0].descriptor.source.priority, -item[1]),
        )[: query.max_candidates]
        eligible = [item for item in ranked if item[1] >= query.minimum_confidence]
        if not eligible:
            return TargetResolution(
                ResolutionStatus.NOT_FOUND,
                (),
                reason="No sufficiently grounded target matched the request.",
            )

        best_priority = eligible[0][0].descriptor.source.priority
        preferred = [
            item for item in eligible if item[0].descriptor.source.priority == best_priority
        ]
        top_score = preferred[0][1]
        plausible = [item for item in preferred if top_score - item[1] <= query.ambiguity_margin]
        if len(plausible) > 1:
            return TargetResolution(
                ResolutionStatus.AMBIGUOUS,
                tuple(item[0] for item in plausible),
                reason=(
                    "Multiple targets at the preferred perception layer are similarly plausible."
                ),
            )
        return TargetResolution(
            ResolutionStatus.RESOLVED,
            (preferred[0][0],),
            selected=preferred[0][0],
            reason="One target matched at the highest available perception priority.",
        )


__all__ = ["CoordinateMapper", "Rect", "TargetResolver"]

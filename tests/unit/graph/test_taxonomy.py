"""Gap-Closure WP-3 (G10): relationship node-kind combination validator."""

from __future__ import annotations

import pytest

from app.modules.graph.models import GraphNodeKind, GraphRelationshipKind
from app.modules.graph.taxonomy import (
    ALLOWED_RELATIONSHIP_NODE_KINDS,
    is_recommended_entity_type,
    is_recommended_event_type,
    validate_relationship_combination,
)


@pytest.mark.parametrize(
    ("relationship_kind", "from_kind", "to_kind"),
    [(kind, pair[0], pair[1]) for kind, pair in ALLOWED_RELATIONSHIP_NODE_KINDS.items()],
)
def test_every_documented_combination_is_valid(
    relationship_kind: GraphRelationshipKind, from_kind: GraphNodeKind, to_kind: GraphNodeKind
) -> None:
    assert validate_relationship_combination(from_kind, relationship_kind, to_kind)


def test_wp3_additions_are_all_documented() -> None:
    for kind in (
        GraphRelationshipKind.POSSIBLY_SAME_AS,
        GraphRelationshipKind.CANDIDATE_ASSOCIATION,
        GraphRelationshipKind.CONTRADICTED_BY,
    ):
        assert kind in ALLOWED_RELATIONSHIP_NODE_KINDS


def test_swapped_node_kinds_are_rejected() -> None:
    """`HAS_EVIDENCE` is CASE->EVIDENCE, never the reverse."""
    assert not validate_relationship_combination(
        GraphNodeKind.EVIDENCE, GraphRelationshipKind.HAS_EVIDENCE, GraphNodeKind.CASE
    )


def test_a_relationship_kind_used_between_the_wrong_node_kinds_is_rejected() -> None:
    """`POSSIBLY_SAME_AS` is ENTITY->ENTITY, never OBSERVATION->OBSERVATION
    (that pairing belongs to `CANDIDATE_ASSOCIATION`)."""
    assert not validate_relationship_combination(
        GraphNodeKind.OBSERVATION,
        GraphRelationshipKind.POSSIBLY_SAME_AS,
        GraphNodeKind.OBSERVATION,
    )


def test_mentions_is_observation_to_entity_mention_only() -> None:
    assert validate_relationship_combination(
        GraphNodeKind.OBSERVATION, GraphRelationshipKind.MENTIONS, GraphNodeKind.ENTITY_MENTION
    )
    assert not validate_relationship_combination(
        GraphNodeKind.OBSERVATION, GraphRelationshipKind.MENTIONS, GraphNodeKind.ENTITY
    )


def test_recommended_entity_types_are_advisory_not_enforced() -> None:
    assert is_recommended_entity_type("phone")
    assert is_recommended_entity_type("unknown")
    # A caller-invented type not in the recommended set is still a valid
    # question to ask (`False`), but `EntityV1.entity_type` itself has no
    # enum constraint at all -- this function never blocks construction.
    assert not is_recommended_entity_type("a-brand-new-type-nobody-catalogued-yet")


def test_recommended_event_types_are_empty_and_documented_as_provisional() -> None:
    """No `EventV1` is projected anywhere yet -- see the module docstring."""
    assert not is_recommended_event_type("meeting")
    assert not is_recommended_event_type("anything")

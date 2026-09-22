"""Gap-Closure WP-6/re-close (G8): `app.core.event_catalog` coverage.

The core guarantee under test: every `IntegrityEventKind` and every
`AuditEventType` member resolves to a real `EventCatalogName` -- a new
member added to either enum without updating this catalog's mapping
fails these tests immediately, the same drift-protection contract
`access_control.audit_catalog`'s own test already established for
`AuditEventType` alone, now extended to cover the catalog itself.
"""

from __future__ import annotations

from app.core.event_catalog import (
    DOCUMENTED_LOG_EVENT_MAPPING,
    INTEGRITY_EVENT_KIND_TO_CATALOG,
    RESERVED_NOT_YET_EMITTED,
    SHARED_OR_STATE_BASED_EMISSIONS,
    EventCatalogName,
    catalog_name_for_audit_event,
    catalog_name_for_hypothesis_action,
)
from app.modules.access_control.audit_catalog import AuditEventType
from app.modules.graph.hypothesis_models import HypothesisActionKind
from app.modules.integrity.models import IntegrityEventKind


def test_catalog_has_exactly_21_names() -> None:
    assert len(list(EventCatalogName)) == 21


def test_catalog_has_no_duplicate_values() -> None:
    values = [member.value for member in EventCatalogName]
    assert len(values) == len(set(values))


def test_catalog_names_are_lowercase_dot_separated() -> None:
    for member in EventCatalogName:
        assert member.value == member.value.lower()
        assert "." in member.value
        assert " " not in member.value


def test_every_integrity_event_kind_except_hypothesis_action_is_mapped() -> None:
    """`HYPOTHESIS_ACTION` is resolved separately (it maps to one of two
    names) -- see `test_hypothesis_action_kinds_are_both_mapped` below."""
    mapped = set(INTEGRITY_EVENT_KIND_TO_CATALOG)
    all_kinds = set(IntegrityEventKind) - {IntegrityEventKind.HYPOTHESIS_ACTION}
    assert mapped == all_kinds, f"unmapped IntegrityEventKind member(s): {all_kinds - mapped}"


def test_every_integrity_event_kind_to_catalog_value_is_a_real_catalog_name() -> None:
    for catalog_name in INTEGRITY_EVENT_KIND_TO_CATALOG.values():
        assert catalog_name in EventCatalogName


def test_hypothesis_action_kinds_are_both_mapped() -> None:
    assert (
        catalog_name_for_hypothesis_action(HypothesisActionKind.CREATED)
        == EventCatalogName.HYPOTHESIS_PROPOSED
    )
    for reviewed_kind in (
        HypothesisActionKind.ACCEPTED_BY_REVIEWER,
        HypothesisActionKind.REJECTED_BY_REVIEWER,
    ):
        assert (
            catalog_name_for_hypothesis_action(reviewed_kind)
            == EventCatalogName.HYPOTHESIS_REVIEWED
        )


def test_every_audit_event_type_resolves_to_a_real_catalog_name() -> None:
    for event_type in AuditEventType:
        catalog_name = catalog_name_for_audit_event(event_type)
        assert catalog_name in EventCatalogName


def test_worker_credential_audit_events_get_their_own_specific_catalog_name() -> None:
    assert (
        catalog_name_for_audit_event(AuditEventType.WORKER_CREDENTIAL_ROTATED)
        == EventCatalogName.WORKER_CREDENTIAL_ROTATED
    )
    assert (
        catalog_name_for_audit_event(AuditEventType.WORKER_CREDENTIAL_REVOKED)
        == EventCatalogName.WORKER_CREDENTIAL_REVOKED
    )


def test_most_audit_event_types_fall_back_to_the_generic_audit_appended_name() -> None:
    assert (
        catalog_name_for_audit_event(AuditEventType.AUTH_LOGIN_SUCCESS)
        == EventCatalogName.AUDIT_APPENDED
    )
    assert (
        catalog_name_for_audit_event(AuditEventType.CASE_CREATE) == EventCatalogName.AUDIT_APPENDED
    )


def test_reserved_not_yet_emitted_entries_are_real_catalog_names_with_a_reason() -> None:
    for catalog_name, reason in RESERVED_NOT_YET_EMITTED.items():
        assert catalog_name in EventCatalogName
        assert isinstance(reason, str)
        assert len(reason) > 20  # a real explanation, not a placeholder


def test_documented_log_event_mapping_values_are_real_catalog_names() -> None:
    for catalog_name in DOCUMENTED_LOG_EVENT_MAPPING.values():
        assert catalog_name in EventCatalogName


def test_shared_or_state_based_emissions_are_real_catalog_names_with_a_reason() -> None:
    for catalog_name, reason in SHARED_OR_STATE_BASED_EMISSIONS.items():
        assert catalog_name in EventCatalogName
        assert isinstance(reason, str)
        assert len(reason) > 20


def test_every_catalog_name_is_either_mapped_from_somewhere_or_explicitly_reserved() -> None:
    """No catalog name is silently orphaned: it must appear as a value in
    at least one of the enum-backed mappings, the documented log mapping,
    or the explicit not-yet-emitted reservation."""
    accounted_for = (
        set(INTEGRITY_EVENT_KIND_TO_CATALOG.values())
        | {EventCatalogName.HYPOTHESIS_PROPOSED, EventCatalogName.HYPOTHESIS_REVIEWED}
        | {catalog_name_for_audit_event(t) for t in AuditEventType}
        | set(DOCUMENTED_LOG_EVENT_MAPPING.values())
        | set(RESERVED_NOT_YET_EMITTED)
        | set(SHARED_OR_STATE_BASED_EMISSIONS)
    )
    all_names = set(EventCatalogName)
    assert accounted_for == all_names, f"orphaned catalog name(s): {all_names - accounted_for}"

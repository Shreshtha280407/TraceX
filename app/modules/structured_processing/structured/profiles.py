"""Explicit, versioned parser profiles.

This file is the single source of truth for every profile's metadata:
accepted content types, header aliases, required fields, and its
confidence rule. See `docs/architecture/parser-profiles-v1.md` for the
human-readable version of the same information — the two must stay in
sync (there is a contract test asserting they list the same profiles).

Extraction *logic* lives elsewhere (`document/fir_report.py`, `structured/
{cdr,finance}.py`, `structured/{csv_parser,xlsx_parser,json_parser}.py`
for the generic profiles) and imports the relevant profile from here.
"""

from __future__ import annotations

from app.modules.structured_processing.errors import ErrorCode, ProcessingError
from app.modules.structured_processing.models import ParserProfile

FIR_REPORT_TEXT_V1 = ParserProfile(
    name="fir_report_text_v1",
    version="1.0.0",
    description=(
        "Deterministic regex extraction of explicitly-labelled fields from "
        "FIR/police-report text sourced from PDF, DOCX, or TXT."
    ),
    accepted_content_types=frozenset(
        {
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/plain",
        }
    ),
    observation_types=(
        "fir_reference",
        "police_station_mention",
        "phone_number_mention",
        "email_address_mention",
        "vehicle_identifier_mention",
        "financial_identifier_mention",
        "date_time_mention",
        "amount_mention",
        "legal_section_mention",
        # Phase 3 additions -- see document/{ocr,ner,ner_fallback,ner_spacy,
        # relations}.py.
        "document_page_ocr_text",
        "ner_entity_mention",
        "person_contact_association",
        "dated_communication_reference",
        "transaction_claim",
        "incident_event_mention",
    ),
    confidence_rule=(
        "0.95 for every regex match: all fir_report_text_v1 patterns require "
        "an exact, explicit deterministic regex match (a label like 'FIR "
        "No.', 'Police Station', 'Section', a currency symbol, or a "
        "structurally-distinctive format like an email/vehicle-plate/UPI "
        "pattern) in embedded text extracted from a trusted (non-scanned, "
        "non-corrupted) page. NER mentions use a separate, lower "
        "confidence tier (0.50 deterministic-fallback / 0.75 real local "
        "model — see document/ner.py) since they are a statistical/"
        "heuristic judgment, not an exact structural match. Relation/event "
        "mentions use 0.60 (an inference from proximity, not itself a "
        "directly-matched fact — see document/relations.py)."
    ),
)

CDR_GENERIC_V1 = ParserProfile(
    name="cdr_generic_v1",
    version="1.0.0",
    description="Normalises Call Detail Records from CSV/XLSX/JSON via explicit header aliases.",
    accepted_content_types=frozenset(
        {
            "text/csv",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/json",
        }
    ),
    observation_types=(
        "cdr_call_record",
        "cdr_device_identifier_mention",
        "cdr_subscriber_identifier_mention",
        "cdr_tower_mention",
    ),
    confidence_rule=(
        "1.00 for cdr_call_record — a complete record read directly from a "
        "validated row/object. 0.90 for identifier mentions (device/"
        "subscriber/tower) where the value is stored exactly as read, since "
        "no normalization is applied to these free-form identifiers."
    ),
    field_aliases={
        "caller_number": ("caller_number", "caller", "a_number", "calling_number", "from_number"),
        "callee_number": ("callee_number", "callee", "b_number", "called_number", "to_number"),
        "timestamp": ("timestamp", "call_time", "date_time", "start_time"),
        # An explicit per-record source timezone, when the export carries one
        # (e.g. an IANA name like "Asia/Kolkata" or a fixed offset like
        # "+05:30") -- see structured/cdr.py's timestamp normalization.
        "source_timezone": ("source_timezone", "timezone", "tz", "utc_offset"),
        "duration_seconds": ("duration_seconds", "duration", "call_duration", "duration_secs"),
        "call_type": ("call_type", "type", "direction"),
        "cell_tower_id": ("cell_tower_id", "tower_id", "cell_id", "site_id"),
        "imei": ("imei",),
        "imsi": ("imsi",),
    },
    required_fields=("caller_number", "timestamp"),
)

FINANCIAL_TRANSACTION_GENERIC_V1 = ParserProfile(
    name="financial_transaction_generic_v1",
    version="1.0.0",
    description=(
        "Normalises financial transaction records from CSV/XLSX/JSON via explicit header aliases."
    ),
    accepted_content_types=frozenset(
        {
            "text/csv",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/json",
        }
    ),
    observation_types=(
        "financial_transaction_record",
        "financial_account_mention",
        "transaction_reference_mention",
        "amount_mention",
    ),
    confidence_rule=(
        "1.00 for financial_transaction_record — a complete record read "
        "directly from a validated row/object, with the amount preserved "
        "exactly as its original source string (never float-converted). "
        "0.90 for account/reference mentions, stored exactly as read."
    ),
    field_aliases={
        "transaction_id": ("transaction_id", "txn_id", "reference_no", "txn_ref"),
        "timestamp": ("timestamp", "date", "transaction_date", "txn_date", "value_date"),
        "source_timezone": ("source_timezone", "timezone", "tz", "utc_offset"),
        "sender_account": ("sender_account", "from_account", "debit_account", "payer_account"),
        "receiver_account": ("receiver_account", "to_account", "credit_account", "payee_account"),
        "amount": ("amount", "txn_amount", "value"),
        "currency": ("currency", "ccy"),
        "direction": ("direction", "debit_credit", "dr_cr", "entry_type"),
        "reference": ("reference", "remarks", "narration", "description"),
        "channel": ("channel", "mode", "payment_mode"),
        "status": ("status",),
        "balance": ("balance", "closing_balance", "available_balance"),
        "counterparty": ("counterparty", "counterparty_name", "payee_name", "payer_name"),
    },
    required_fields=("amount", "currency"),
)

GENERIC_TABULAR_V1 = ParserProfile(
    name="generic_tabular_v1",
    version="1.0.0",
    description=(
        "Fallback profile for CSV/XLSX data that doesn't match a CDR or "
        "financial shape: one tabular_record observation per row, holding "
        "that row's header:value pairs."
    ),
    accepted_content_types=frozenset(
        {"text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    ),
    observation_types=("tabular_record",),
    confidence_rule="1.00 — a complete row read directly from a validated tabular source.",
)

GENERIC_JSON_V1 = ParserProfile(
    name="generic_json_v1",
    version="1.0.0",
    description=(
        "Fallback profile for JSON that doesn't match a CDR or financial "
        "record-array shape: one json_scalar_value observation per scalar "
        "leaf value, with its exact JSON path."
    ),
    accepted_content_types=frozenset({"application/json"}),
    observation_types=("json_scalar_value",),
    confidence_rule=(
        "1.00 — a scalar value read directly from a validated JSON document at an exact path."
    ),
)

_PROFILES: dict[str, ParserProfile] = {
    profile.name: profile
    for profile in (
        FIR_REPORT_TEXT_V1,
        CDR_GENERIC_V1,
        FINANCIAL_TRANSACTION_GENERIC_V1,
        GENERIC_TABULAR_V1,
        GENERIC_JSON_V1,
    )
}


def get_profile(name: str) -> ParserProfile:
    """Look up a profile by name, or raise `unsupported_parser_profile`."""
    profile = _PROFILES.get(name)
    if profile is None:
        raise ProcessingError(
            ErrorCode.UNSUPPORTED_PARSER_PROFILE, f"'{name}' is not a recognized parser profile"
        )
    return profile

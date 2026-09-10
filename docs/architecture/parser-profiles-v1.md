# Parser Profiles v1

Field-level reference for every profile defined in `app/modules/structured_processing/structured/profiles.py` — that file is the source of truth; this document must stay in sync with it (`tests/unit/structured_processing` implicitly enforces this by testing against the live profile objects, not a copy).

`job.processor_name` selects a profile explicitly. `worker.process_job` classifies `evidence.content_type` against the Phase 1 supported format set first, then cross-checks it against the chosen profile's `accepted_content_types` — a mismatch (e.g. a `cdr_generic_v1` job pointed at a PDF) fails safely as `unsupported_parser_profile` rather than being silently reinterpreted.

## `fir_report_text_v1`

- **Accepted input types**: `application/pdf`, DOCX (`application/vnd.openxmlformats-officedocument.wordprocessingml.document`), `text/plain`.
- **Source fields / aliases**: none — this profile scans free text via fixed regex patterns (see `document/fir_report.py`), not header-mapped fields.
- **Required vs optional**: every mention type is independently optional; a document with none of them still succeeds with zero observations.
- **Normalisation**: none beyond `.strip()` on the matched value. Dates/times are captured as raw matched text only (no DD/MM vs MM/DD resolution attempted — see known limitations).
- **Observation types emitted**: `fir_reference`, `police_station_mention`, `phone_number_mention`, `email_address_mention`, `vehicle_identifier_mention`, `financial_identifier_mention`, `date_time_mention`, `amount_mention`, `legal_section_mention`.
- **Confidence rule**: `0.95` for every match — every pattern requires an explicit label or a structurally-distinctive format in text extracted from a non-scanned page.
- **Safe failure/defer behaviour**: an encrypted PDF fails as `encrypted_pdf_unsupported`; a fully or partially scanned PDF defers as `document_requires_ocr` (see `document/ocr_routing.py`); a corrupt PDF/DOCX fails as `invalid_pdf`/`invalid_docx`.

## `cdr_generic_v1`

- **Accepted input types**: `text/csv`, XLSX, `application/json` (a top-level array of objects, or one `{"key": [...]}` wrapper).
- **Source fields / header aliases** (case/space/hyphen-insensitive — see `models.normalize_header`):

  | Canonical field | Accepted aliases |
  |---|---|
  | `caller_number` | `caller_number`, `caller`, `a_number`, `calling_number`, `from_number` |
  | `callee_number` | `callee_number`, `callee`, `b_number`, `called_number`, `to_number` |
  | `timestamp` | `timestamp`, `call_time`, `date_time`, `start_time` |
  | `duration_seconds` | `duration_seconds`, `duration`, `call_duration`, `duration_secs` |
  | `call_type` | `call_type`, `type`, `direction` |
  | `cell_tower_id` | `cell_tower_id`, `tower_id`, `cell_id`, `site_id` |
  | `imei` | `imei` |
  | `imsi` | `imsi` |

- **Required vs optional**: `caller_number` and `timestamp` are required; everything else is optional.
- **Normalisation**:
  - Phone numbers are stripped of separators and a `+91`/leading-`0` prefix *only if* the result is an unambiguous 10-digit Indian mobile number (`[6-9]\d{9}`); otherwise the original value is kept unchanged.
  - Timestamps are parsed only against: `%Y-%m-%d %H:%M:%S`, `%Y-%m-%dT%H:%M:%S`, `%d/%m/%Y %H:%M:%S`, `%d-%m-%Y %H:%M:%S`, `%Y-%m-%d`, `%d/%m/%Y`. All are interpreted as UTC (no timezone offset support in this phase). A value matching none of these fails as `required_field_missing`.
  - `duration_seconds` is stored as a raw string (`duration_seconds_raw`) always, plus a numeric `duration_seconds` only when it parses as a non-negative number.
- **Observation types emitted**: `cdr_call_record` (whole-row, confidence `1.00`), `cdr_device_identifier_mention`, `cdr_subscriber_identifier_mention`, `cdr_tower_mention` (field-level, confidence `0.90`).
- **Safe failure/defer behaviour**: missing `caller_number`/`timestamp`, or an unparseable timestamp, fails the whole job as `required_field_missing`.

## `financial_transaction_generic_v1`

- **Accepted input types**: `text/csv`, XLSX, `application/json`.
- **Source fields / header aliases**:

  | Canonical field | Accepted aliases |
  |---|---|
  | `transaction_id` | `transaction_id`, `txn_id`, `reference_no`, `txn_ref` |
  | `timestamp` | `timestamp`, `date`, `transaction_date`, `txn_date` |
  | `sender_account` | `sender_account`, `from_account`, `debit_account`, `payer_account` |
  | `receiver_account` | `receiver_account`, `to_account`, `credit_account`, `payee_account` |
  | `amount` | `amount`, `txn_amount`, `value` |
  | `currency` | `currency`, `ccy` |
  | `reference` | `reference`, `remarks`, `narration` |
  | `channel` | `channel`, `mode`, `payment_mode` |
  | `status` | `status` |

- **Required vs optional**: `amount` and `currency` are required; everything else is optional.
- **Normalisation**: `amount` is parsed with `decimal.Decimal` (never `float`) after defensively stripping a leading currency symbol/code and thousands separators; the original string is always kept as `amount_raw` alongside the normalized `amount`. `currency` is uppercased (`currency`) with the original preserved as `currency_raw`; an empty or missing currency fails the record rather than being invented.
- **Observation types emitted**: `financial_transaction_record` (whole-row, `1.00`), `amount_mention` (`1.00`), `financial_account_mention`, `transaction_reference_mention` (field-level, `0.90`).
- **Safe failure/defer behaviour**: missing `amount`/`currency`, an unparseable amount, or an empty currency fails the whole job as `required_field_missing`.

## `generic_tabular_v1`

- **Accepted input types**: `text/csv`, XLSX.
- **Source fields / aliases**: none — every header is passed through as-is.
- **Required vs optional**: nothing is required; any row shape is accepted.
- **Normalisation**: none. Values are stored exactly as read (subject to `limits.MAX_CELL_TEXT_LENGTH`).
- **Observation types emitted**: `tabular_record` — one per row, `attributes` holding that row's `{header: value}` pairs.
- **Confidence rule**: `1.00` — a complete row read directly from a validated tabular source.

## `generic_json_v1`

- **Accepted input types**: `application/json`.
- **Source fields / aliases**: none — recursive scalar traversal (`structured/json_parser.traverse_json_scalars`), bounded by `limits.MAX_JSON_DEPTH`/`MAX_JSON_NODES`/`MAX_JSON_STRING_LENGTH`.
- **Required vs optional**: not applicable; every scalar leaf is independently emitted.
- **Normalisation**: none. `null` values are skipped (no meaningful scalar to observe); containers (objects/arrays) are never themselves captured as a value.
- **Observation types emitted**: `json_scalar_value` — one per scalar leaf, with its exact `json_path` (e.g. `$.items[2].amount`).
- **Confidence rule**: `1.00` — a scalar value read directly from a validated JSON document at an exact path.

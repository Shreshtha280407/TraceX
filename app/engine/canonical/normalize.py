"""Strict source-to-v1 normalization. Unknown means null; it is never inferred."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.engine.adapters.source import ParsedRow

TXID = re.compile(r"^[a-f0-9]{64}$")
MAX_SATS = 2_100_000_000_000_000


class NormalizationError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedRow:
    facts: dict[str, list[dict[str, Any]]]


def _source_refs(*, source_id: str, source_sha256: str, row: ParsedRow) -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": source_id,
            "source_sha256": source_sha256,
            "locator_type": row.locator_type,
            "locator": row.locator,
            "byte_start": None,
            "byte_end": None,
        }
    ]


def _list(value: Any, *, field: str) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise NormalizationError(f"{field} must be a JSON array") from exc
        if isinstance(parsed, list):
            return parsed
    raise NormalizationError(f"{field} must be an array")


def _optional_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, (str, int, float)):
        raise NormalizationError("expected scalar text value")
    return str(value)


def _sats(value: Any, *, field: str, is_sats: bool = False) -> int | None:
    if value is None or value == "":
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise NormalizationError(f"{field} is not a valid number") from exc
    if not decimal.is_finite() or decimal < 0:
        raise NormalizationError(f"{field} must be finite and non-negative")
    sats = decimal if is_sats else decimal * Decimal(100_000_000)
    if sats != sats.to_integral_value():
        raise NormalizationError(f"{field} has excess Bitcoin precision")
    if sats > MAX_SATS:
        raise NormalizationError(f"{field} exceeds maximum Bitcoin supply in satoshis")
    return int(sats)


def _timestamp(value: Any) -> tuple[str | None, str | None]:
    original = _optional_text(value)
    if original is None:
        return None, None
    candidate = original.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise NormalizationError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise NormalizationError("timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z"), original


def _endpoint_port(value: Any, *, field: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise NormalizationError(f"{field} must be an integer port") from exc
    if not 0 <= port <= 65535:
        raise NormalizationError(f"{field} must be in 0..65535")
    return port


def normalize_row(*, case_id: str, source_id: str, source_sha256: str, row: ParsedRow) -> NormalizedRow:
    raw = row.value
    raw_txid = _optional_text(raw.get("txid"))
    txid = raw_txid.lower() if raw_txid else None
    if not txid or not TXID.fullmatch(txid):
        raise NormalizationError("txid must be a lowercase/uppercase 64-character hexadecimal identifier")
    network = _optional_text(raw.get("network")) or "unknown"
    if network not in {"bitcoin-mainnet", "bitcoin-testnet", "bitcoin-regtest", "unknown"}:
        raise NormalizationError("unsupported network")
    refs = _source_refs(source_id=source_id, source_sha256=source_sha256, row=row)
    observed_at, timestamp_original = _timestamp(raw.get("timestamp") or raw.get("observed_at"))
    fee = (
        _sats(raw.get("fee_sats"), field="fee_sats", is_sats=True)
        if "fee_sats" in raw
        else _sats(raw.get("fee"), field="fee")
    )
    transaction = {
        "schema_version": "1.0.0",
        "case_id": case_id,
        "network": network,
        "txid": txid,
        "record_variant": "canonical",
        "block_hash": _optional_text(raw.get("block_hash")),
        "block_height": None,
        "block_time": None,
        "block_time_original": None,
        "fee_sats": fee,
        "confirmation_status": "unknown",
        "source_refs": refs,
        "source_timestamp": observed_at,
        "source_timestamp_original": timestamp_original,
        "source_timestamp_meaning": "unknown",
    }
    if raw.get("block_height") not in (None, ""):
        try:
            transaction["block_height"] = int(raw["block_height"])
        except (TypeError, ValueError) as exc:
            raise NormalizationError("block_height must be an integer") from exc
    if raw.get("block_time") not in (None, ""):
        transaction["block_time"], transaction["block_time_original"] = _timestamp(raw["block_time"])
        transaction["source_timestamp_meaning"] = "block_time"
    source_inputs = _list(raw.get("inputs"), field="inputs") if raw.get("inputs") not in (None, "") else []
    input_addresses = _list(raw.get("input_addresses"), field="input_addresses")
    input_amounts = _list(raw.get("input_amounts"), field="input_amounts")
    if input_amounts and len(input_addresses) != len(input_amounts):
        raise NormalizationError("input_addresses and input_amounts lengths differ")
    source_outputs = _list(raw.get("outputs"), field="outputs") if raw.get("outputs") not in (None, "") else []
    output_addresses = _list(raw.get("output_addresses"), field="output_addresses")
    output_amounts = _list(raw.get("output_amounts"), field="output_amounts")
    if not source_outputs and (len(output_addresses) != len(output_amounts) or not output_amounts):
        raise NormalizationError("output_addresses/output_amounts must be non-empty arrays of equal length")
    inputs = []
    for vin, source_input in enumerate(source_inputs):
        if not isinstance(source_input, dict):
            raise NormalizationError(f"inputs[{vin}] must be an object")
        previous_txid = _optional_text(source_input.get("prev_txid"))
        if previous_txid is not None:
            previous_txid = previous_txid.lower()
            if not TXID.fullmatch(previous_txid):
                raise NormalizationError(f"inputs[{vin}].prev_txid must be a 64-character hexadecimal identifier")
        previous_vout = source_input.get("prev_vout")
        if previous_vout not in (None, ""):
            try:
                previous_vout = int(previous_vout)
            except (TypeError, ValueError) as exc:
                raise NormalizationError(f"inputs[{vin}].prev_vout must be an integer") from exc
            if previous_vout < 0:
                raise NormalizationError(f"inputs[{vin}].prev_vout must be non-negative")
        input_value = (
            _sats(source_input.get("amount_sats"), field=f"inputs[{vin}].amount_sats", is_sats=True)
            if "amount_sats" in source_input
            else _sats(source_input.get("amount"), field=f"inputs[{vin}].amount")
        )
        inputs.append(
            {
                "schema_version": "1.0.0",
                "case_id": case_id,
                "network": network,
                "txid": txid,
                "vin": vin,
                "prev_txid": previous_txid,
                "prev_vout": previous_vout,
                "address": _optional_text(source_input.get("address")),
                "amount_sats": input_value,
                "sequence": source_input.get("sequence"),
                "source_refs": refs,
            }
        )
    for vin, address in enumerate(input_addresses) if not source_inputs else []:
        inputs.append(
            {
                "schema_version": "1.0.0",
                "case_id": case_id,
                "network": network,
                "txid": txid,
                "vin": vin,
                "prev_txid": None,
                "prev_vout": None,
                "address": _optional_text(address),
                "amount_sats": _sats(input_amounts[vin], field=f"input_amounts[{vin}]") if input_amounts else None,
                "sequence": None,
                "source_refs": refs,
            }
        )
    script_type = _optional_text(raw.get("script_type"))
    outputs = []
    for vout, source_output in enumerate(source_outputs):
        if not isinstance(source_output, dict):
            raise NormalizationError(f"outputs[{vout}] must be an object")
        output_value = (
            _sats(source_output.get("amount_sats"), field=f"outputs[{vout}].amount_sats", is_sats=True)
            if "amount_sats" in source_output
            else _sats(source_output.get("amount", source_output.get("value")), field=f"outputs[{vout}].amount")
        )
        if output_value is None:
            raise NormalizationError(f"outputs[{vout}].amount is required")
        outputs.append(
            {
                "schema_version": "1.0.0",
                "case_id": case_id,
                "network": network,
                "txid": txid,
                "vout": vout,
                "amount_sats": output_value,
                "script_id": _optional_text(source_output.get("script_id")),
                "script_type": _optional_text(source_output.get("script_type")) or script_type,
                "address": _optional_text(source_output.get("address")),
                "source_refs": refs,
            }
        )
    for vout, address in enumerate(output_addresses) if not source_outputs else []:
        outputs.append(
            {
                "schema_version": "1.0.0",
                "case_id": case_id,
                "network": network,
                "txid": txid,
                "vout": vout,
                "amount_sats": _sats(output_amounts[vout], field=f"output_amounts[{vout}]"),
                "script_id": None,
                "script_type": script_type,
                "address": _optional_text(address),
                "source_refs": refs,
            }
        )
    network_fields = ("src_ip", "dst_ip", "src_port", "dst_port", "geo_country", "asn")
    observations: list[dict[str, Any]] = []
    if any(raw.get(field) not in (None, "") for field in network_fields):
        observations.append(
            {
                "schema_version": "1.0.0",
                "case_id": case_id,
                "network": network,
                "observation_id": f"{source_id}:{row.logical_record}",
                "txid": txid,
                "observer_id": _optional_text(raw.get("observer_id")) or "source-unspecified",
                "src_ip": _optional_text(raw.get("src_ip")),
                "dst_ip": _optional_text(raw.get("dst_ip")),
                "src_port": _endpoint_port(raw.get("src_port"), field="src_port"),
                "dst_port": _endpoint_port(raw.get("dst_port"), field="dst_port"),
                "endpoint_ip": _optional_text(raw.get("src_ip")),
                "endpoint_port": _endpoint_port(raw.get("src_port"), field="src_port"),
                "direction": "unknown",
                "geo_country": _optional_text(raw.get("geo_country")),
                "asn": _optional_text(raw.get("asn")),
                "observer_received_at": observed_at,
                "observer_time_original": timestamp_original,
                "clock_quality": "unknown",
                "capture_scope": "source-supplied network metadata; no origin or ownership assertion",
                "source_refs": refs,
            }
        )
    return NormalizedRow(
        {"transactions": [transaction], "inputs": inputs, "outputs": outputs, "network_observations": observations}
    )

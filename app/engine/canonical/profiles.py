"""Source-schema profiles: map real-world column names onto the v1 field names.

The PS publishes a minimum field list (timestamp, src/dst IP and port, txid,
input/output addresses and amounts, fee, script type, geo country / ASN), but
no fixed header spelling, and the dataset ships with the event, not with the
problem statement. Exports in the wild say `Source IP`, `tx_hash`,
`from_addresses`, `inputs_value_sats`, `Country`, ... This module recognises
those spellings so the strict normaliser sees canonical names.

Rules:
* a canonical key already present always wins over an alias of it;
* only keys are renamed -- values pass through untouched, so the original
  record (kept verbatim as evidence) and the canonical record differ only in
  naming, which is recorded in the canonical transaction's `field_mapping`;
* the mapping is computed once per distinct header (a CSV has one), so a
  canonical-schema source pays one set lookup per row.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

CANONICAL_KEYS = frozenset({
    "txid", "network", "timestamp", "observed_at", "block_hash", "block_height", "block_time",
    "fee", "fee_sats", "inputs", "outputs", "input_addresses", "input_amounts", "input_amounts_sats",
    "output_addresses", "output_amounts", "output_amounts_sats", "script_type", "src_ip", "dst_ip",
    "src_port", "dst_port", "geo_country", "asn", "observer_id",
})

_ALIASES: dict[str, tuple[str, ...]] = {
    "txid": ("txid", "txhash", "transactionid", "transactionhash", "hash", "tx", "txidhex", "transaction"),
    "timestamp": ("timestamp", "time", "datetime", "ts", "eventtime", "capturetime", "capturedat", "date",
                  "timestamputc", "eventtimestamp", "seenat", "firstseen"),
    "observed_at": ("observedat", "observertime", "receivedat"),
    "src_ip": ("srcip", "sourceip", "ipsrc", "srcaddr", "srcipaddress", "sourceipaddress", "clientip", "peerip"),
    "dst_ip": ("dstip", "destip", "destinationip", "ipdst", "dstaddr", "dstipaddress", "destinationipaddress",
               "serverip"),
    "src_port": ("srcport", "sourceport", "sport", "portsrc"),
    "dst_port": ("dstport", "destport", "destinationport", "dport", "portdst"),
    "input_addresses": ("inputaddresses", "inputaddress", "inaddresses", "fromaddresses", "fromaddress",
                        "senderaddresses", "senders", "inputwallets", "inputwalletaddresses", "fromwallets", "from"),
    "output_addresses": ("outputaddresses", "outputaddress", "outaddresses", "toaddresses", "toaddress",
                         "receiveraddresses", "receivers", "outputwallets", "outputwalletaddresses", "towallets", "to"),
    "input_amounts": ("inputamounts", "inputamount", "inamounts", "inputvalues", "invalues", "inputamountsbtc",
                      "inputvaluesbtc", "fromamounts"),
    "output_amounts": ("outputamounts", "outputamount", "outamounts", "outputvalues", "outvalues",
                       "outputamountsbtc", "outputvaluesbtc", "toamounts"),
    "input_amounts_sats": ("inputamountssats", "inputvaluessats", "inputamountssatoshi", "inputvaluessatoshi",
                           "inputamountssatoshis", "inamountssats"),
    "output_amounts_sats": ("outputamountssats", "outputvaluessats", "outputamountssatoshi",
                            "outputvaluessatoshi", "outputamountssatoshis", "outamountssats"),
    "fee": ("fee", "fees", "txfee", "transactionfee", "feebtc", "feeamount"),
    "fee_sats": ("feesats", "feesatoshi", "feesatoshis", "feeinsats"),
    "script_type": ("scripttype", "script", "addresstype", "outputscripttype", "scriptpubkeytype"),
    "geo_country": ("geocountry", "country", "countrycode", "geo", "srccountry", "sourcecountry", "geoipcountry",
                    "ipcountry"),
    "asn": ("asn", "asnumber", "as", "srcasn", "sourceasn", "autonomoussystem", "autonomoussystemnumber"),
    "block_height": ("blockheight", "height", "block"),
    "block_time": ("blocktime", "blocktimestamp", "confirmedat"),
    "block_hash": ("blockhash",),
    "network": ("network", "chain", "net"),
    "observer_id": ("observerid", "sensor", "sensorid", "observer", "probe", "collector"),
}
_LOOKUP = {alias: canonical for canonical, aliases in _ALIASES.items() for alias in aliases}
_STRIP = re.compile(r"[\s_\-\.\[\]\(\)/:]+")


def _normal(key: str) -> str:
    return _STRIP.sub("", str(key)).lower()


@lru_cache(maxsize=256)
def mapping_for(keys: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """(source key, canonical key) renames for one header; empty for canonical input."""
    present = set(keys)
    renames: list[tuple[str, str]] = []
    claimed: set[str] = set(present & CANONICAL_KEYS)
    for key in keys:
        if key in CANONICAL_KEYS:
            continue
        canonical = _LOOKUP.get(_normal(key))
        if canonical is None or canonical in claimed:
            continue
        claimed.add(canonical)
        renames.append((key, canonical))
    return tuple(renames)


def canonicalize(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The record with recognised alias keys renamed, plus the renames applied."""
    keys = tuple(raw)
    if CANONICAL_KEYS.issuperset(keys):
        return raw, {}
    renames = mapping_for(keys)
    if not renames:
        return raw, {}
    value = dict(raw)
    for source, canonical in renames:
        value[canonical] = value.pop(source)
    return value, dict(renames)


def describe(keys: list[str]) -> dict[str, Any]:
    """Coverage of the PS minimum field list for a header -- used by `tracex-dataset`."""
    renames = dict(mapping_for(tuple(keys)))
    mapped = {canonical: source for source, canonical in renames.items()}
    for key in keys:
        if key in CANONICAL_KEYS:
            mapped.setdefault(key, key)
    required = {
        "timestamp": ("timestamp", "observed_at", "block_time"),
        "src_ip": ("src_ip",), "dst_ip": ("dst_ip",), "src_port": ("src_port",), "dst_port": ("dst_port",),
        "txid": ("txid",),
        "input_addresses[]": ("input_addresses", "inputs"),
        "output_addresses[]": ("output_addresses", "outputs"),
        "input_amounts[]": ("input_amounts", "input_amounts_sats", "inputs"),
        "output_amounts[]": ("output_amounts", "output_amounts_sats", "outputs"),
        "fee": ("fee", "fee_sats"), "script_type": ("script_type", "outputs"),
        "geo_country/asn": ("geo_country", "asn"),
    }
    coverage = {
        field: next((f"{mapped[name]} -> {name}" for name in names if name in mapped), None)
        for field, names in required.items()
    }
    unmapped = [key for key in keys if key not in CANONICAL_KEYS and key not in renames]
    return {"renames": renames, "ps_minimum_fields": coverage,
            "missing": [field for field, source in coverage.items() if source is None], "unmapped": unmapped}

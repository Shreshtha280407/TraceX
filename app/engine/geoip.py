"""Offline Geo-IP / ASN enrichment from downloadable open-source databases.

The PS asks for integration with a downloadable open-source Geo-IP database.
TraceX supports two openly licensed range databases, both distributed as plain
CSV and refreshed regularly:

* **DB-IP IP-to-Country Lite** (CC BY 4.0, attribution to DB-IP.com required)
  -- ``start,end,country`` ranges for IPv4 and IPv6;
* **IPtoASN** (Public Domain Dedication and License 1.0) -- ``start,end,asn,org``
  ranges for IPv4 and IPv6, or the combined ``ip2asn-combined.tsv`` file.

Both are fetched once on a connected machine (``tracex-geoip download``) or
copied onto the offline host by hand and compiled (``tracex-geoip import
FILE...``) into a compact sorted range table under ``TRACEX_GEOIP_DIR``
(default ``var/geoip``). At runtime nothing touches the network: lookups are a
binary search over that table, a few microseconds per address, with no numpy
or third-party dependency.

A lookup never asserts where a person is. It says which country/ASN the
*database* assigns to an IP range, and the IP's scope (public, private,
documentation, ...). Non-public addresses -- including the RFC 5737
documentation ranges the synthetic fixtures use -- are never looked up, so a
reported country on such an address is reported as unverifiable rather than
confirmed or contradicted.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import hashlib
import io
import ipaddress
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from array import array
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

FORMAT_VERSION = 1
COMPILED_DIR = "compiled"

#: npm mirrors of the two databases (https://github.com/sapics/ip-location-db),
#: used by `tracex-geoip download` because the registry is reachable from most
#: build networks. The upstream publishers' own files are accepted by `import`.
NPM_PACKAGES = {
    "dbip-country": {
        "package": "@ip-location-db/dbip-country",
        "kind": "country",
        "licence": "CC-BY-4.0",
        "attribution": "IP Geolocation by DB-IP (https://db-ip.com)",
        "upstream": "https://db-ip.com/db/download/ip-to-country-lite",
    },
    "iptoasn-asn": {
        "package": "@ip-location-db/iptoasn-asn",
        "kind": "asn",
        "licence": "PDDL-1.0",
        "attribution": "IPtoASN (https://iptoasn.com)",
        "upstream": "https://iptoasn.com/",
    },
}

_DOCUMENTATION_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32", "3fff::/20")
)


def default_geoip_dir() -> Path:
    return Path(os.environ.get("TRACEX_GEOIP_DIR", "var/geoip")).resolve()


def ip_scope(value: str | None) -> tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    """Classify an address without any database: public, private, documentation, ..."""
    if not value:
        return "missing", None
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return "invalid", None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if any(address in network for network in _DOCUMENTATION_NETWORKS if network.version == address.version):
        return "documentation", address
    if address.is_unspecified:
        return "unspecified", address
    if address.is_loopback:
        return "loopback", address
    if address.is_link_local:
        return "link_local", address
    if address.is_multicast:
        return "multicast", address
    if address.is_private:
        return "private", address
    if address.is_reserved or not address.is_global:
        return "reserved", address
    return "public", address


@dataclass(frozen=True)
class GeoIPResult:
    ip: str
    scope: str
    version: int | None
    country: str | None = None
    asn: int | None = None
    as_org: str | None = None

    @property
    def asn_label(self) -> str | None:
        return f"AS{self.asn}" if self.asn is not None else None

    def as_dict(self) -> dict:
        data = asdict(self)
        data["asn_label"] = self.asn_label
        return data


# --------------------------------------------------------------------------- #
# Compiled range tables
# --------------------------------------------------------------------------- #


class _Blob16:
    """Sorted 16-byte big-endian values packed in one bytes object, indexable
    like a list so `bisect` can search it without materializing 300K ints."""

    __slots__ = ("data",)

    def __init__(self, data: bytes) -> None:
        self.data = data

    def __len__(self) -> int:
        return len(self.data) // 16

    def __getitem__(self, index: int) -> bytes:
        start = index * 16
        return self.data[start:start + 16]


def _u32(path: Path) -> array:
    values = array("I")
    values.frombytes(path.read_bytes())
    if sys.byteorder == "big":
        values.byteswap()
    return values


def _u16(path: Path) -> array:
    values = array("H")
    values.frombytes(path.read_bytes())
    if sys.byteorder == "big":
        values.byteswap()
    return values


def _write_array(path: Path, values: array) -> None:
    if sys.byteorder == "big":
        values = array(values.typecode, values)
        values.byteswap()
    path.write_bytes(values.tobytes())


class _RangeTable:
    """starts/ends sorted by start; values parallel. Lookup = last start <= ip."""

    def __init__(self, starts, ends, values) -> None:
        self.starts, self.ends, self.values = starts, ends, values

    def find(self, key) -> int | None:
        index = bisect.bisect_right(self.starts, key) - 1
        if index >= 0 and key <= self.ends[index]:
            return self.values[index]
        return None

    def __len__(self) -> int:
        return len(self.starts)


@dataclass
class _Builder:
    """Accumulates parsed ranges for one (kind, version) table."""

    rows: list[tuple[int, int, object]]

    def table(self) -> list[tuple[int, int, object]]:
        self.rows.sort(key=lambda row: (row[0], row[1]))
        merged: list[tuple[int, int, object]] = []
        for start, end, value in self.rows:
            if merged and start <= merged[-1][1]:
                # Overlap: keep the earlier (first-listed after sort) range,
                # trim the later one so the table stays non-overlapping.
                start = merged[-1][1] + 1
                if start > end:
                    continue
            merged.append((start, end, value))
        return merged


class GeoIPDatabase:
    """A compiled database directory, opened read-only."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        self.countries: list[str] = self.manifest.get("countries", [])
        self.orgs: list[str] = json.loads((directory / "asn_orgs.json").read_text(encoding="utf-8")) if (
            directory / "asn_orgs.json"
        ).exists() else []
        self._tables: dict[str, _RangeTable | None] = {}

    # Tables are loaded on first use: an import that only sees IPv4 never
    # reads the IPv6 files.
    def _table(self, name: str) -> _RangeTable | None:
        if name in self._tables:
            return self._tables[name]
        directory = self.directory
        table: _RangeTable | None = None
        if (directory / f"{name}.start").exists():
            if name.endswith("_v4"):
                starts, ends = _u32(directory / f"{name}.start"), _u32(directory / f"{name}.end")
            else:
                starts = _Blob16((directory / f"{name}.start").read_bytes())
                ends = _Blob16((directory / f"{name}.end").read_bytes())
            if name.startswith("country"):
                values = _u16(directory / f"{name}.value")
            else:
                values = _u32(directory / f"{name}.value")
            table = _RangeTable(starts, ends, values)
            if name.startswith("asn"):
                table.orgs = _u32(directory / f"{name}.org")  # type: ignore[attr-defined]
        self._tables[name] = table
        return table

    @property
    def has_country(self) -> bool:
        return any(source.get("kind") == "country" for source in self.manifest.get("sources", []))

    @property
    def has_asn(self) -> bool:
        return any(source.get("kind") == "asn" for source in self.manifest.get("sources", []))

    def lookup(self, value: str | None) -> GeoIPResult:
        scope, address = ip_scope(value)
        text = str(value) if value is not None else ""
        if address is None:
            return GeoIPResult(text, scope, None)
        if scope != "public":
            return GeoIPResult(text, scope, address.version)
        if address.version == 4:
            key: object = int(address)
            suffix = "v4"
        else:
            key = int(address).to_bytes(16, "big")
            suffix = "v6"
        country = asn = org = None
        country_table = self._table(f"country_{suffix}")
        if country_table is not None:
            slot = country_table.find(key)
            if slot is not None:
                country = self.countries[slot]
        asn_table = self._table(f"asn_{suffix}")
        if asn_table is not None:
            index = bisect.bisect_right(asn_table.starts, key) - 1
            if index >= 0 and key <= asn_table.ends[index]:
                asn = int(asn_table.values[index])
                org_slot = asn_table.orgs[index]  # type: ignore[attr-defined]
                org = self.orgs[org_slot] if org_slot < len(self.orgs) else None
        return GeoIPResult(text, scope, address.version, country, asn, org)

    def lookup_many(self, values: Iterable[str | None]) -> dict[str, GeoIPResult]:
        return {str(value): self.lookup(value) for value in set(values) if value}

    def summary(self) -> dict:
        return {
            "installed": True,
            "directory": str(self.directory),
            "compiled_at": self.manifest.get("compiled_at"),
            "format_version": self.manifest.get("format_version"),
            "sources": self.manifest.get("sources", []),
            "has_country": self.has_country,
            "has_asn": self.has_asn,
        }


_OPEN: dict[str, tuple[float, GeoIPDatabase]] = {}


def open_database(directory: Path | None = None) -> GeoIPDatabase | None:
    """The compiled database under `directory` (or TRACEX_GEOIP_DIR), cached
    per process and reopened when it is recompiled; None when not installed."""
    root = (directory or default_geoip_dir()) / COMPILED_DIR
    manifest = root / "manifest.json"
    try:
        stamp = manifest.stat().st_mtime
    except OSError:
        return None
    cached = _OPEN.get(str(root))
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        database = GeoIPDatabase(root)
    except (OSError, ValueError):
        return None
    _OPEN[str(root)] = (stamp, database)
    return database


def status(directory: Path | None = None) -> dict:
    database = open_database(directory)
    if database is None:
        return {
            "installed": False,
            "directory": str((directory or default_geoip_dir()) / COMPILED_DIR),
            "hint": "run `tracex-geoip download` on a connected machine, or copy the DB-IP / IPtoASN "
            "CSV files here and run `tracex-geoip import FILE...`",
        }
    return database.summary()


# --------------------------------------------------------------------------- #
# Import / compile
# --------------------------------------------------------------------------- #


def _ip_key(text: str) -> tuple[int, int]:
    """(version, integer) for a CSV cell holding a dotted/colon IP or an integer."""
    text = text.strip()
    if text.isdigit():
        number = int(text)
        return (4 if number < 1 << 32 else 6), number
    address = ipaddress.ip_address(text)
    return address.version, int(address)


def _text_lines(name: str, data: bytes) -> Iterator[str]:
    if name.endswith(".gz"):
        data = gzip.decompress(data)
    yield from io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", newline="")


def _rows(lines: Iterable[str], delimiter: str) -> Iterator[list[str]]:
    import csv

    yield from csv.reader(lines, delimiter=delimiter)


def _detect(name: str, sample: list[str]) -> tuple[str, str]:
    """(kind, delimiter) for one range file, from its name and first rows."""
    lowered = name.lower()
    delimiter = "\t" if ("\t" in sample[0] if sample else False) else ","
    columns = len(sample[0].split(delimiter)) if sample else 0
    if "ip2asn-combined" in lowered or "ip2asn-v" in lowered or (delimiter == "\t" and columns == 5):
        return "iptoasn_tsv", "\t"
    if columns == 3:
        return "country", delimiter
    if columns == 4:
        return "asn", delimiter
    if columns >= 5 and delimiter == ",":
        return "asn", delimiter
    raise ValueError(f"{name}: unrecognised Geo-IP range layout ({columns} columns)")


def _licence_for(name: str, kind: str, members: dict[str, bytes]) -> tuple[str, str]:
    lowered = name.lower()
    if "dbip" in lowered or any("DBIP-LICENSE" in member for member in members):
        return "CC-BY-4.0", NPM_PACKAGES["dbip-country"]["attribution"]
    if "iptoasn" in lowered or "ip2asn" in lowered:
        return "PDDL-1.0", NPM_PACKAGES["iptoasn-asn"]["attribution"]
    return "unknown (record the licence before relying on this data)", "unknown"


def _expand(path: Path) -> Iterator[tuple[str, bytes, dict[str, bytes]]]:
    """(member name, bytes, sibling members) for each range file inside `path`.

    Accepts a plain CSV/TSV (optionally .gz) or an npm/tar package of them.
    For packages, the `-num.csv` duplicates of the text-IP files are skipped.
    """
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            members = {
                member.name: archive.extractfile(member).read()  # type: ignore[union-attr]
                for member in archive.getmembers()
                if member.isfile()
            }
        for name, data in sorted(members.items()):
            base = name.rsplit("/", 1)[-1]
            if base.endswith((".csv", ".tsv", ".csv.gz", ".tsv.gz")) and "-num." not in base:
                yield base, data, members
        return
    yield path.name, path.read_bytes(), {}


def compile_database(files: list[Path], directory: Path | None = None) -> dict:
    """Parse range files into the compiled table directory; returns the manifest."""
    target_root = directory or default_geoip_dir()
    builders: dict[str, _Builder] = {}
    countries: dict[str, int] = {}
    orgs: dict[str, int] = {}
    sources: list[dict] = []

    def add(kind: str, version: int, start: int, end: int, value: object) -> None:
        builders.setdefault(f"{kind}_v{version}", _Builder([])).rows.append((start, end, value))

    for path in files:
        path = Path(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        for name, data, members in _expand(path):
            lines = list(_text_lines(name, data))
            sample = [line for line in lines[:5] if line.strip()]
            kind, delimiter = _detect(name, sample)
            licence, attribution = _licence_for(f"{path.name}/{name}", kind, members)
            counts = {"v4": 0, "v6": 0, "skipped": 0}
            for row in _rows(lines, delimiter):
                if len(row) < 3 or not row[0].strip() or row[0].startswith("#"):
                    continue
                try:
                    version, start = _ip_key(row[0])
                    _, end = _ip_key(row[1])
                except ValueError:
                    counts["skipped"] += 1  # header or malformed line
                    continue
                if kind == "country":
                    code = row[2].strip().upper()
                    if not code or code in {"ZZ", "--"}:
                        continue
                    add("country", version, start, end, countries.setdefault(code, len(countries)))
                else:
                    number = row[2].strip().upper().removeprefix("AS")
                    if not number.isdigit() or int(number) == 0:
                        continue  # AS0 = not routed
                    org = (row[4] if kind == "iptoasn_tsv" and len(row) > 4 else row[3] if len(row) > 3 else "").strip()
                    add("asn", version, start, end, (int(number), orgs.setdefault(org, len(orgs))))
                counts[f"v{version}"] += 1
            sources.append({
                "file": f"{path.name}" + (f"!{name}" if name != path.name else ""),
                "sha256": digest,
                "bytes": path.stat().st_size,
                "kind": "asn" if kind == "iptoasn_tsv" else kind,
                "rows_v4": counts["v4"],
                "rows_v6": counts["v6"],
                "licence": licence,
                "attribution": attribution,
            })
    if not builders:
        raise ValueError("no Geo-IP ranges were found in the given files")

    staging = Path(tempfile.mkdtemp(prefix=".compiling-", dir=_ensure(target_root)))
    # mkdtemp creates 0700: the database is often compiled by root (an image
    # build) and read by an unprivileged service user, so make it world-readable.
    staging.chmod(0o755)
    try:
        for name, builder in builders.items():
            table = builder.table()
            is_v4 = name.endswith("_v4")
            if is_v4:
                _write_array(staging / f"{name}.start", array("I", (row[0] for row in table)))
                _write_array(staging / f"{name}.end", array("I", (row[1] for row in table)))
            else:
                (staging / f"{name}.start").write_bytes(b"".join(row[0].to_bytes(16, "big") for row in table))
                (staging / f"{name}.end").write_bytes(b"".join(row[1].to_bytes(16, "big") for row in table))
            if name.startswith("country"):
                _write_array(staging / f"{name}.value", array("H", (row[2] for row in table)))
            else:
                _write_array(staging / f"{name}.value", array("I", (row[2][0] for row in table)))
                _write_array(staging / f"{name}.org", array("I", (row[2][1] for row in table)))
        country_names = [code for code, _ in sorted(countries.items(), key=lambda item: item[1])]
        (staging / "asn_orgs.json").write_text(
            json.dumps([org for org, _ in sorted(orgs.items(), key=lambda item: item[1])]), encoding="utf-8"
        )
        manifest = {
            "format_version": FORMAT_VERSION,
            "compiled_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "countries": country_names,
            "tables": {name: len(builder.rows) for name, builder in sorted(builders.items())},
            "sources": sources,
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        for path in staging.iterdir():
            path.chmod(0o644)
        final = target_root / COMPILED_DIR
        previous = target_root / ".previous"
        shutil.rmtree(previous, ignore_errors=True)
        if final.exists():
            final.rename(previous)
        staging.rename(final)
        shutil.rmtree(previous, ignore_errors=True)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def download(directory: Path | None = None, packages: Iterable[str] = tuple(NPM_PACKAGES)) -> dict:
    """Fetch the latest open databases (needs network once) and compile them."""
    root = _ensure(directory or default_geoip_dir())
    downloads = _ensure(root / "downloads")
    fetched: list[Path] = []
    for key in packages:
        spec = NPM_PACKAGES[key]
        registry = "https://registry.npmjs.org/" + spec["package"].replace("/", "%2f")
        with urllib.request.urlopen(registry, timeout=60) as response:
            metadata = json.load(response)
        version = metadata["dist-tags"]["latest"]
        dist = metadata["versions"][version]["dist"]
        target = downloads / f"{key}-{version}.tgz"
        if not target.exists():
            with urllib.request.urlopen(dist["tarball"], timeout=600) as response:
                payload = response.read()
            integrity = dist.get("integrity", "")
            if integrity.startswith("sha512-"):
                import base64

                expected = base64.b64decode(integrity.removeprefix("sha512-"))
                if hashlib.sha512(payload).digest() != expected:
                    raise RuntimeError(f"{key}: downloaded tarball failed its sha512 integrity check")
            target.write_bytes(payload)
        fetched.append(target)
    return compile_database(fetched, root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tracex-geoip", description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", type=Path, default=None, help="Geo-IP directory (default TRACEX_GEOIP_DIR or var/geoip)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show the installed database")
    sub.add_parser("download", help="fetch DB-IP country lite + IPtoASN and compile (needs network once)")
    importer = sub.add_parser("import", help="compile local CSV/TSV(.gz) or .tgz package files (offline)")
    importer.add_argument("files", nargs="+", type=Path)
    lookup = sub.add_parser("lookup", help="look up one or more IPs")
    lookup.add_argument("ips", nargs="+")
    args = parser.parse_args(argv)
    if args.command == "status":
        print(json.dumps(status(args.dir), indent=2))
    elif args.command == "download":
        manifest = download(args.dir)
        print(json.dumps({"compiled": manifest["tables"], "sources": manifest["sources"]}, indent=2))
    elif args.command == "import":
        manifest = compile_database(args.files, args.dir)
        print(json.dumps({"compiled": manifest["tables"], "sources": manifest["sources"]}, indent=2))
    else:
        database = open_database(args.dir)
        for ip in args.ips:
            if database is None:
                scope, _ = ip_scope(ip)
                print(json.dumps({"ip": ip, "scope": scope, "database": "not installed"}))
            else:
                print(json.dumps(database.lookup(ip).as_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

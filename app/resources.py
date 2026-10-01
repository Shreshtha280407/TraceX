"""What the machine running this process can actually give the pipeline.

TraceX runs on anything from a laptop to a server, often inside a container
whose memory limit is far below the host's. Instead of one fixed set of sizes,
the heavy stages ask this module for a plan derived from the memory and CPUs
that are really available *now* (cgroup limits included), so a large machine
keeps the fast settings and a small one bounds its working set instead of
being killed by the OOM-killer.

Every figure can be pinned with `TRACEX_MEMORY_BUDGET_MB` for reproducible
benchmarks. Reading the figures costs a few file reads, so callers ask once
per stage rather than caching for the life of the process.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MB = 1 << 20
#: Share of currently-available memory one import may plan to use. The rest is
#: left for the database, the API process and the OS page cache.
BUDGET_FRACTION = 0.6
#: Used only when the platform exposes no memory figures at all.
FALLBACK_BUDGET_BYTES = 2048 * MB

# Measured on the 100K generator-v2 fixture: one in-flight feature row (its dict
# plus three serialized JSON documents) is ~4 KB, and one parse-batch record
# with its normalized facts and Parquet staging is ~6 KB.
_FEATURE_ROW_BYTES = 4 * 1024
_PARSE_RECORD_BYTES = 6 * 1024


def _read_int(path: str) -> int | None:
    try:
        raw = Path(path).read_text().strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    # cgroup v1 reports "no limit" as a huge page-aligned number.
    return value if 0 < value < 1 << 60 else None


def _meminfo_bytes(field: str) -> int | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith(f"{field}:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _cgroup_limit_and_usage() -> tuple[int | None, int | None]:
    limit = _read_int("/sys/fs/cgroup/memory.max")
    if limit is not None:
        return limit, _read_int("/sys/fs/cgroup/memory.current")
    limit = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    return limit, _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes") if limit else None


def total_memory_bytes() -> int | None:
    """Physical memory, capped by this process's cgroup/container limit."""
    candidates: list[int] = []
    try:
        candidates.append(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (ValueError, OSError, AttributeError):
        pass
    limit, _ = _cgroup_limit_and_usage()
    if limit:
        candidates.append(limit)
    return min(candidates) if candidates else None


def available_memory_bytes() -> int | None:
    """Memory that can be allocated now without swapping, cgroup-aware."""
    candidates: list[int] = []
    available = _meminfo_bytes("MemAvailable")
    if available is not None:
        candidates.append(available)
    else:
        try:
            candidates.append(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES"))
        except (ValueError, OSError, AttributeError):
            pass
    limit, usage = _cgroup_limit_and_usage()
    if limit:
        candidates.append(max(0, limit - (usage or 0)))
    if not candidates:
        total = total_memory_bytes()
        # macOS exposes no "available" figure through sysconf; half of
        # physical memory is a conservative stand-in.
        return total // 2 if total else None
    return min(candidates)


def cpu_count() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def _clamp(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, value)))


@dataclass(frozen=True)
class ResourcePlan:
    total_memory_bytes: int | None
    available_memory_bytes: int | None
    memory_budget_bytes: int
    cpu_count: int
    #: Rows per bulk INSERT for write-once analytical rows (feature/finding rows).
    insert_chunk_rows: int
    #: Upper bound on source records per parse batch; never raises a configured value.
    max_ingestion_batch_records: int
    #: DuckDB working memory for the graph build; beyond it DuckDB spills to disk.
    duckdb_memory_limit_mb: int
    duckdb_threads: int

    def ingestion_batch_records(self, configured: int) -> int:
        return max(1, min(configured, self.max_ingestion_batch_records))

    def as_dict(self) -> dict:
        return asdict(self)


def current_plan() -> ResourcePlan:
    total = total_memory_bytes()
    available = available_memory_bytes()
    override = os.environ.get("TRACEX_MEMORY_BUDGET_MB")
    budget: int
    if override:
        try:
            budget = max(64, int(float(override))) * MB
        except ValueError:
            logger.warning("TRACEX_MEMORY_BUDGET_MB=%r is not a number; ignoring it", override)
            override = None
    if not override:
        budget = int(available * BUDGET_FRACTION) if available else FALLBACK_BUDGET_BYTES
        budget = max(budget, 256 * MB)
    cpus = cpu_count()
    return ResourcePlan(
        total_memory_bytes=total,
        available_memory_bytes=available,
        memory_budget_bytes=budget,
        cpu_count=cpus,
        # ~2% of the budget in flight per INSERT: 20K rows at a 4 GB budget,
        # 2K rows on a 512 MB container. Total INSERT work is identical either
        # way; only how much of it is held in memory at once changes.
        insert_chunk_rows=_clamp(budget * 0.02 / _FEATURE_ROW_BYTES, 2_000, 25_000),
        # ~5% of the budget per parse batch; 32768 (the shipped default) on any
        # machine with a multi-GB budget, smaller on constrained ones.
        max_ingestion_batch_records=_clamp(budget * 0.05 / _PARSE_RECORD_BYTES, 2_048, 32_768),
        duckdb_memory_limit_mb=_clamp(budget * 0.5 / MB, 256, 1 << 20),
        duckdb_threads=cpus,
    )

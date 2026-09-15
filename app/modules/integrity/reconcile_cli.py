"""Run a bounded, case-scoped integrity reconciliation.

Usage: ``uv run python -m app.modules.integrity.reconcile_cli --case-id <uuid>``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import UUID

from app.core.config import get_settings
from app.modules.integrity.reconciliation import IntegrityReconciliationService
from app.modules.integrity.repository import IntegrityRepository, create_engine
from app.modules.integrity.service import IntegrityService


async def _run(case_id: UUID, limit: int) -> None:
    settings = get_settings()
    repository = IntegrityRepository(create_engine(settings))
    try:
        receipt = await IntegrityReconciliationService(
            IntegrityService(repository, settings), repository
        ).reconcile_case(case_id, limit=limit)
    finally:
        await repository.close()
    print(json.dumps(receipt.__dict__, default=str, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True, type=UUID)
    parser.add_argument("--limit", default=500, type=int)
    args = parser.parse_args()
    asyncio.run(_run(args.case_id, args.limit))


if __name__ == "__main__":
    main()

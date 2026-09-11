"""Evidence lifecycle: case-scoped upload, private storage, and durable worker-job foundation.

Phase 2 (Nipun). See `docs/architecture/evidence-lifecycle.md`. This module
owns evidence ingestion and the durable `WorkerJobV1` producer only -- it
never extracts, transcribes, analyzes, or resolves evidence content, and it
never projects anything into Neo4j.
"""

from __future__ import annotations

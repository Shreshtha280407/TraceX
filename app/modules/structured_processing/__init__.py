"""Document and structured-data processing (Jasraj Phase 1).

Transforms supported local source content (FIRs/police reports as PDF/
DOCX/TXT, CDR and financial CSV/XLSX/JSON, generic JSON) into canonical
`ObservationV1` objects and safe `WorkerResultV1` results. See
`docs/architecture/document-and-structured-processing-v1.md` for the full
design and `worker.process_job` for the entry point.

This module never accesses PostgreSQL, Neo4j, Redis, or MinIO directly —
see `models.SourceResolver`.
"""

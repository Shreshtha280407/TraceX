"""Add durable media chunk manifests, lineage, and checkpoints.

Revision ID: f4a1c9e0d2b3
Revises: e4f7a8b9c0d1
Create Date: 2026-09-14

The tables contain only canonical metadata and opaque object references.
Partial observation/outbox insertion remains owned by the existing evidence
lifecycle transaction; this migration supplies its additional Phase 4 rows.
"""

from __future__ import annotations

from alembic import op

revision = "f4a1c9e0d2b3"
down_revision = "e4f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE media_chunk_manifests ("
        "manifest_id UUID PRIMARY KEY, case_id UUID NOT NULL, evidence_id UUID NOT NULL, "
        "job_id UUID NOT NULL, version TEXT NOT NULL, manifest_hash TEXT NOT NULL, "
        "processor_name TEXT NOT NULL, processor_version TEXT NOT NULL, "
        "configuration_hash TEXT NOT NULL, canonical_payload JSONB NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ NULL, "
        "CONSTRAINT uq_media_manifest_scope_hash "
        "UNIQUE (case_id, evidence_id, job_id, manifest_hash)"
        ")"
    )
    op.execute(
        "CREATE TABLE media_chunks ("
        "chunk_id UUID PRIMARY KEY, manifest_id UUID NOT NULL "
        "REFERENCES media_chunk_manifests(manifest_id), "
        "case_id UUID NOT NULL, evidence_id UUID NOT NULL, job_id UUID NOT NULL, "
        "chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0), canonical_boundary JSONB NOT NULL, "
        "status TEXT NOT NULL CHECK (status IN ('pending', 'completed')), "
        "publication_hash TEXT NULL, observation_batch_id UUID NULL, "
        "completed_at TIMESTAMPTZ NULL, created_at TIMESTAMPTZ NOT NULL, "
        "CONSTRAINT uq_media_chunk_manifest_index UNIQUE (manifest_id, chunk_index)"
        ")"
    )
    op.execute(
        "CREATE TABLE media_derived_artifacts ("
        "artifact_id UUID PRIMARY KEY, case_id UUID NOT NULL, evidence_id UUID NOT NULL, "
        "job_id UUID NOT NULL, "
        "manifest_id UUID NOT NULL REFERENCES media_chunk_manifests(manifest_id), "
        "chunk_id UUID NOT NULL REFERENCES media_chunks(chunk_id), idempotency_key TEXT NOT NULL, "
        "canonical_payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL, "
        "CONSTRAINT uq_media_artifact_chunk_key UNIQUE (chunk_id, idempotency_key)"
        ")"
    )
    op.execute(
        "CREATE TABLE media_chunk_observations ("
        "chunk_id UUID NOT NULL REFERENCES media_chunks(chunk_id), "
        "observation_id UUID NOT NULL REFERENCES worker_observations(observation_id), "
        "PRIMARY KEY (chunk_id, observation_id)"
        ")"
    )
    op.execute(
        "CREATE TABLE media_checkpoints ("
        "checkpoint_id UUID PRIMARY KEY, manifest_id UUID NOT NULL "
        "REFERENCES media_chunk_manifests(manifest_id), "
        "case_id UUID NOT NULL, evidence_id UUID NOT NULL, job_id UUID NOT NULL, "
        "manifest_hash TEXT NOT NULL, "
        "processor_version TEXT NOT NULL, configuration_hash TEXT NOT NULL, "
        "completed_chunk_ids JSONB NOT NULL, observation_batch_ids JSONB NOT NULL, "
        "artifact_ids JSONB NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL"
        ")"
    )
    op.create_index("ix_media_chunks_scope_status", "media_chunks", ["case_id", "job_id", "status"])
    op.create_index(
        "ix_media_checkpoints_scope_manifest",
        "media_checkpoints",
        ["case_id", "job_id", "manifest_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_media_checkpoints_scope_manifest", table_name="media_checkpoints")
    op.drop_index("ix_media_chunks_scope_status", table_name="media_chunks")
    op.drop_table("media_checkpoints")
    op.drop_table("media_chunk_observations")
    op.drop_table("media_derived_artifacts")
    op.drop_table("media_chunks")
    op.drop_table("media_chunk_manifests")

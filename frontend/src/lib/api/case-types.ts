/**
 * Types mirror real backend shapes exactly -- copied from
 * app/modules/access_control/{models,case_service}.py,
 * app/modules/evidence_lifecycle/{schemas,models}.py,
 * app/contracts/evidence.py, and app/modules/graph/{review_models,hypothesis_models}.py.
 * Never invented. See docs/architecture/evidence-lifecycle.md and
 * docs/architecture/phase-6-review-and-hypothesis.md.
 */

import type { CaseRole, CaseView, ClearanceLevel } from './types'

export interface CaseCreateRequest {
  case_reference: string
  classification: ClearanceLevel
}

export interface CaseMemberAddRequest {
  user_id: string
  role: CaseRole
  clearance: ClearanceLevel
}

export interface CaseMemberView {
  user_id: string
  role: CaseRole
  clearance: ClearanceLevel
  is_active: boolean
}

/** `security_audit_events` row -- safe telemetry only, never a secret or raw evidence value. */
export interface SecurityAuditEventRecord {
  event_id: string
  occurred_at: string
  event_type: string
  outcome: 'success' | 'failure' | 'denied'
  request_id: string | null
  user_id_nullable: string | null
  case_id_nullable: string | null
  ip_hash_or_safe_network_marker: string | null
  metadata_safe_json: Record<string, unknown>
}

export interface CaseAuditEventListResponse {
  items: SecurityAuditEventRecord[]
}

/** Mirrors `SourceType` in app/contracts/evidence.py exactly. */
export type SourceType =
  | 'document'
  | 'cdr'
  | 'financial'
  | 'video'
  | 'image'
  | 'audio'
  | 'chat'
  | 'structured_tabular'
  | 'structured_json'
  | 'audio_transcript'
  | 'audio_diarization'
  | 'whatsapp_chat'
  | 'telegram_chat'
  | 'instagram_chat'
  | 'other'

/** Mirrors `EvidenceClassification` in app/contracts/evidence.py exactly. */
export type EvidenceClassification = 'unclassified' | 'restricted' | 'confidential' | 'secret'

/** Mirrors `EvidenceProcessingStatus` in app/contracts/evidence.py exactly. */
export type EvidenceProcessingStatus = 'uploaded' | 'queued' | 'processing' | 'processed' | 'failed'

export interface EvidenceView {
  evidence_id: string
  case_id: string
  source_type: SourceType
  original_filename: string
  content_type: string
  sha256: string
  classification: EvidenceClassification
  uploaded_by: string
  uploaded_at: string
  parser_profile: string | null
  processing_status: EvidenceProcessingStatus
  created_at: string
}

export interface EvidenceListResponse {
  items: EvidenceView[]
}

/** Mirrors `WorkerStatus` in app/contracts/worker.py exactly. */
export type WorkerJobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'deferred' | 'cancelled'

export interface JobView {
  job_id: string
  case_id: string
  evidence_id: string
  source_type: SourceType
  processor_name: string
  processor_version: string
  attempt: number
  status: WorkerJobStatus
  requested_at: string
  dispatched_at: string | null
  claimed_at: string | null
  completed_at: string | null
  observation_count: number
  last_error_code: string | null
  last_error_message: string | null
}

export interface EvidenceUploadResponse {
  evidence: EvidenceView
  job: JobView
}

/** A case the current investigator has an active membership on, hydrated from real endpoints. */
export interface AssignedCase extends CaseView {
  role: CaseRole
  clearance: ClearanceLevel
}

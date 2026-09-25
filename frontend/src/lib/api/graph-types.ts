/**
 * Types mirror real backend shapes exactly -- copied from
 * app/modules/graph/{schemas,models,integration_models,entity_models,
 * hypothesis_models,review_models}.py and app/contracts/entity.py. Never
 * invented. See docs/architecture/graph-taxonomy-v1.md and
 * docs/decisions/ADR-021-graph-taxonomy-alignment.md.
 */

import type { CaseNoteRecord } from './case-types'

// --- Case graph snapshot (Entity/Event nodes only -- Section 7) ------------

/** Mirrors `GraphRelationshipKind` in app/modules/graph/models.py exactly.
 * Only these three ever appear in a snapshot -- see schemas.py's own comment:
 * "scoped to Entity/Event nodes and the relationship kinds that connect them". */
export type GraphRelationshipKind = 'HAS_PARTICIPANT' | 'POSSIBLY_SAME_AS' | 'CONTRADICTED_BY'

export interface GraphSnapshotEntityView {
  entity_id: string
  entity_type: string
  canonical_label: string
  aliases: string[]
  review_status: string
}

export interface GraphSnapshotEventView {
  event_id: string
  event_type: string
  review_status: string
  confidence: number
  event_time: string | null
}

export interface GraphSnapshotRelationshipView {
  kind: GraphRelationshipKind
  from_id: string
  to_id: string
  /** The real entity_resolution_candidate_id backing a POSSIBLY_SAME_AS/
   * CONTRADICTED_BY edge -- null for HAS_PARTICIPANT, which carries no such
   * per-edge identifier. Two distinct real candidates can reference the same
   * (from_id, to_id) pair, so (kind, from_id, to_id) alone is not always
   * a unique key for this relationship. */
  relationship_id: string | null
}

export interface GraphSnapshotResponse {
  case_id: string
  entities: GraphSnapshotEntityView[]
  events: GraphSnapshotEventView[]
  relationships: GraphSnapshotRelationshipView[]
  node_limit: number
  relationship_limit: number
}

export interface GraphAnalyticsResponse {
  case_id: string
  node_counts: Record<string, number>
  relationship_counts: Record<string, number>
}

export interface GraphCoParticipationMotif {
  shared_entity_id: string
  event_a_id: string
  event_b_id: string
}

export interface GraphMotifsResponse {
  case_id: string
  co_participation: GraphCoParticipationMotif[]
  limit: number
}

// --- Projected observations (evidence-provenance detail) -------------------

export interface GraphEntityMentionView {
  mention_id: string
  observation_id: string
  ordinal: number
  mention_type: string | null
  display_label: string
}

export interface GraphObservationView {
  observation_id: string
  case_id: string
  evidence_id: string
  observation_type: string
  extraction_confidence: number
  event_time: string | null
  time_window_start: string | null
  time_window_end: string | null
  location_raw_text: string | null
  location_latitude: number | null
  location_longitude: number | null
  extractor_name: string
  extractor_version: string
  mentions: GraphEntityMentionView[]
}

export interface CaseGraphObservationsResponse {
  case_id: string
  items: GraphObservationView[]
  limit: number
  offset: number
  has_more: boolean
}

// --- Evidence Viewer drill-down (Section 5 page 13 / Section 9 row 13) -----
// Mirrors `SourceLocatorView`/`EvidenceObservationView`/`EvidenceSummaryView`
// in app/modules/graph/schemas.py exactly. Unlike `GraphObservationView`
// above, these carry `source_locator` -- the exact page/row/frame/timestamp
// pointer back into the source evidence.

export interface SourceLocatorView {
  page: number | null
  span_start: number | null
  span_end: number | null
  bbox_x_min: number | null
  bbox_y_min: number | null
  bbox_x_max: number | null
  bbox_y_max: number | null
  sheet: string | null
  row: number | null
  column: number | null
  json_path: string | null
  frame_number: number | null
  time_start_ms: number | null
  time_end_ms: number | null
  message_id: string | null
}

export interface EvidenceObservationView {
  observation_id: string
  case_id: string
  evidence_id: string
  observation_type: string
  extraction_confidence: number
  event_time: string | null
  source_locator: SourceLocatorView
  extractor_name: string
  extractor_version: string
}

export interface EvidenceSummaryView {
  evidence_id: string
  case_id: string
  source_type: string
  content_type: string
  classification: string
  processing_status: string
}

export interface EvidenceObservationsResponse {
  case_id: string
  evidence_id: string
  items: EvidenceObservationView[]
  total_observations: number
  truncated: boolean
}

export interface ObservationProvenanceResponse {
  observation: EvidenceObservationView
  evidence: EvidenceSummaryView | null
}

// --- Entities (Gap-Closure WP-2) --------------------------------------------

/** Mirrors `EntityV1` in app/contracts/entity.py exactly. */
export interface EntityV1 {
  schema_version: 'v1'
  entity_id: string
  case_id: string
  entity_type: string
  canonical_label: string
  aliases: string[]
  stable_identifiers: Record<string, unknown>
  attributes: Record<string, unknown>
  created_from_observation_ids: string[]
  review_status: 'unreviewed' | 'confirmed' | 'disputed'
  created_at: string
}

export interface EntityListResponse {
  items: EntityV1[]
  next_cursor: string | null
}

/** Mirrors `RetrievalReason` in app/modules/graph/intelligence/models.py exactly. */
export type RetrievalReason =
  | 'exact_identifier'
  | 'exact_alias'
  | 'normalized_alias'
  | 'transliteration_candidate'
  | 'platform_scoped_handle'
  | 'local_vector_candidate'
  | 'temporal_hot_window'

export interface EntityResolutionCandidateRecord {
  entity_resolution_candidate_id: string
  case_id: string
  left_entity_id: string
  right_entity_id: string
  reasons: RetrievalReason[]
  identifier_types: string[]
  vector_score: number | null
  contradiction_reasons: string[]
  supporting_observation_ids: string[]
  config_version: string
  created_at: string
}

/** Mirrors `EntityReviewOutcome` in app/modules/graph/entity_models.py exactly.
 * `VERIFIED_SAME` is the one authorized path that may treat two entities as
 * one identity -- never automatic. `SPLIT` reverses an earlier
 * `VERIFIED_SAME` for the same pair; it is not a first-pass option. There is
 * no "needs more evidence" value anywhere in the real backend. */
export type EntityReviewOutcome = 'verified_same' | 'rejected' | 'split'

export interface EntityReviewDecisionRecord {
  entity_review_decision_id: string
  case_id: string
  entity_resolution_candidate_id: string
  decision: EntityReviewOutcome
  reviewer_user_id: string
  rationale: string | null
  rationale_commitment_sha256: string | null
  created_at: string
}

export type EntityResolutionEffectiveStatus = 'needs_review' | EntityReviewOutcome

export interface EntityResolutionReviewView {
  candidate: EntityResolutionCandidateRecord
  effective_status: EntityResolutionEffectiveStatus
  latest_decision: EntityReviewDecisionRecord | null
  decision_history: EntityReviewDecisionRecord[]
}

export interface EntityResolutionCandidateListResponse {
  items: EntityResolutionReviewView[]
}

// --- Phase 5 correlation/candidate integration (read-only, Nipun's module) -

export type PropositionStatus = 'candidate' | 'needs_review' | 'rejected'

export interface EvidencePathSnapshot {
  evidence_id: string
  observation_id: string
  observation_created_at: string
  event_time: string | null
  time_window_start: string | null
  time_window_end: string | null
}

export interface CandidateLinkRecord {
  candidate_link_id: string
  correlation_id: string
  case_id: string
  idempotency_key: string
  left_observation_id: string
  right_observation_id: string
  status: PropositionStatus
  reason_reference: string | null
  evidence_paths: EvidencePathSnapshot[]
  created_at: string
}

export interface CandidateListResponse {
  items: CandidateLinkRecord[]
}

export type GraphUpdateEventStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'deferred'

export interface GraphUpdateEventRecord {
  event_id: string
  event_type: string
  case_id: string
  aggregate_type: string
  aggregate_id: string
  status: GraphUpdateEventStatus
  created_at: string
  updated_at: string
  completed_at: string | null
}

export interface CorrelationRecord {
  correlation_id: string
  case_id: string
  correlation_type: string
  status: PropositionStatus
  supporting_observation_ids: string[]
  contradictory_observation_ids: string[]
  hypothesis_reference: string | null
  created_at: string
  updated_at: string
}

export interface CorrelationIntegrationView {
  correlation: CorrelationRecord
  projection: GraphUpdateEventRecord
}

export interface CorrelationListResponse {
  items: CorrelationIntegrationView[]
}

// --- Phase 6 Part 5: candidate review decisions (Verify/Reject, this module) -

/** Mirrors `CandidateReviewOutcome` in app/modules/graph/review_models.py
 * exactly -- deliberately only two values (see that module's own docstring:
 * "never itself a verified identity... only records whether a human
 * accepted or rejected the candidate correlation"). */
export type CandidateReviewOutcome = 'accepted_by_reviewer' | 'rejected_by_reviewer'
export type CandidateReviewEffectiveStatus = 'needs_review' | CandidateReviewOutcome

export interface CandidateReviewDecisionRecord {
  candidate_review_decision_id: string
  case_id: string
  candidate_link_id: string
  correlation_id: string
  decision: CandidateReviewOutcome
  reviewer_user_id: string
  rationale: string | null
  rationale_commitment_sha256: string | null
  created_at: string
}

export interface CandidateReviewView {
  candidate: CandidateLinkRecord
  review_status: CandidateReviewEffectiveStatus
  decision: CandidateReviewDecisionRecord | null
}

export interface CandidateReviewListResponse {
  items: CandidateReviewView[]
}

// --- Phase 6 Part 5: evidence-backed hypotheses -----------------------------

/** Mirrors `HypothesisStatus` in app/modules/graph/hypothesis_models.py
 * exactly -- never a value that reads as a confirmed fact. */
export type HypothesisStatus = 'needs_review' | 'accepted_by_reviewer' | 'rejected_by_reviewer'
export type HypothesisReviewOutcome = 'accepted_by_reviewer' | 'rejected_by_reviewer'

export interface HypothesisRecord {
  hypothesis_id: string
  case_id: string
  status: HypothesisStatus
  created_by: string
  created_at: string
  updated_at: string
  decided_at: string | null
  decided_by: string | null
  supporting_observation_ids: string[]
  supporting_candidate_ids: string[]
  supporting_entity_resolution_candidate_ids: string[]
  statement: string
  statement_commitment_sha256: string
  rationale: string | null
  rationale_commitment_sha256: string | null
}

export interface HypothesisListResponse {
  items: HypothesisRecord[]
  next_cursor: string | null
}

export interface HypothesisCreateSubmission {
  statement: string
  rationale?: string | null
  supporting_observation_ids?: string[]
  supporting_candidate_ids?: string[]
  supporting_entity_resolution_candidate_ids?: string[]
}

export interface HypothesisReviewSubmission {
  decision: HypothesisReviewOutcome
  rationale?: string | null
}

// --- Handoff summary (Gap-Closure WP-4 G3) ----------------------------------

export interface HandoffSummary {
  case_id: string
  open_candidate_count: number
  open_candidates: CandidateLinkRecord[]
  accepted_candidate_count: number
  rejected_candidate_count: number
  open_hypotheses: HypothesisRecord[]
  accepted_hypothesis_count: number
  rejected_hypothesis_count: number
  recent_notes: CaseNoteRecord[]
}

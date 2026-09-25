/**
 * Types mirror real backend shapes exactly -- copied from
 * app/modules/integrity/{api,models}.py. Never invented.
 * See docs/architecture/phase-6-integrity.md.
 */

export interface CheckpointSignatureView {
  signature_id: string
  key_id: string
  algorithm: string
  signature_encoding: string
  public_key_b64: string
  public_key_fingerprint: string
  signed_root_hash: string
  signed_at: string
}

export interface IntegrityCheckpointView {
  checkpoint_id: string
  case_id: string
  start_sequence: number
  end_sequence: number
  leaf_count: number
  root_hash: string
  tree_format_version: string
  created_at: string
  signature: CheckpointSignatureView | null
}

export interface IntegrityCheckpointListResponse {
  case_id: string
  items: IntegrityCheckpointView[]
  limit: number
  offset: number
}

export interface VerificationResult {
  checkpoint_id: string
  case_id: string
  leaf_count_matches: boolean
  root_matches: boolean
  signature_valid: boolean
  ok: boolean
  reason: string | null
}

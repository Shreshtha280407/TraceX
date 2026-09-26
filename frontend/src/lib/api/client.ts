import { useAuthStore } from '../auth/store'
import type {
  CaseAuditEventListResponse,
  CaseCreateRequest,
  CaseMemberAddRequest,
  CaseMemberCandidateListResponse,
  CaseMemberListResponse,
  CaseMemberUpdateRequest,
  CaseMemberView,
  CaseNoteCreateRequest,
  CaseNoteListResponse,
  CaseNoteRecord,
  EvidenceClassification,
  EvidenceIntegrityCheck,
  EvidenceListResponse,
  EvidenceUploadResponse,
  JobView,
  SourceType,
} from './case-types'
import type {
  CandidateListResponse,
  CandidateReviewListResponse,
  CandidateReviewOutcome,
  CandidateReviewView,
  CaseGraphObservationsResponse,
  CorrelationListResponse,
  EntityListResponse,
  EntityResolutionCandidateListResponse,
  EntityReviewDecisionRecord,
  EntityReviewOutcome,
  EntityV1,
  EvidenceObservationsResponse,
  GraphAnalyticsResponse,
  GraphMotifsResponse,
  GraphSnapshotResponse,
  HandoffSummary,
  HypothesisCreateSubmission,
  HypothesisListResponse,
  HypothesisRecord,
  HypothesisReviewOutcome,
  ObservationProvenanceResponse,
} from './graph-types'
import type { IntegrityCheckpointListResponse, VerificationResult } from './integrity-types'
import type {
  AdminProvisionUserRequest,
  AdminResetCredentialsResponse,
  ApiErrorBody,
  CaseView,
  MeResponse,
  MfaEnrollResponse,
  PublicUser,
  PublicUserListResponse,
  TokenPairResponse,
  FirstAdminSetupRequest,
  FirstAdminSetupStatusResponse,
} from './types'

/**
 * Environment-driven base URL (Section 2). Empty string means "same
 * origin, relative paths" -- the dev-time default, proxied to the real
 * backend by Vite (see vite.config.ts and .env.example): the backend sets
 * no CORS headers by design, so a browser cannot call a different origin
 * directly without them.
 */
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? ''

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId: string
  readonly fieldErrors: ApiErrorBody['errors']

  constructor(status: number, body: ApiErrorBody) {
    super(body.error.message)
    this.name = 'ApiError'
    this.status = status
    this.code = body.error.code
    this.requestId = body.error.request_id
    this.fieldErrors = body.errors
  }

  /** ABAC (Section 6): a 403 is a real, expected outcome -- never a crash. */
  get isForbidden(): boolean {
    return this.status === 403
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  body?: unknown
  /** Skip the Authorization header and the 401-refresh-retry dance (login/refresh themselves). */
  skipAuth?: boolean
  /** Extra headers, e.g. `Idempotency-Key` on evidence upload/reprocess. */
  headers?: Record<string, string>
}

async function parseErrorBody(response: Response): Promise<ApiErrorBody> {
  try {
    return (await response.json()) as ApiErrorBody
  } catch {
    return {
      error: { code: 'unknown', message: response.statusText || 'Request failed', request_id: '' },
    }
  }
}

async function rawRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, skipAuth = false, headers: extraHeaders } = options
  const isFormData = body instanceof FormData
  const headers: Record<string, string> = { ...extraHeaders }
  // A FormData body must NOT get an explicit Content-Type: the browser sets
  // one itself (multipart/form-data with the correct boundary), which is
  // impossible to reproduce by hand -- setting it manually silently breaks
  // the upload.
  if (body !== undefined && !isFormData) headers['Content-Type'] = 'application/json'
  if (!skipAuth) {
    const token = useAuthStore.getState().accessToken
    if (token) headers.Authorization = `Bearer ${token}`
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : isFormData ? body : JSON.stringify(body),
  })

  if (response.status === 204) return undefined as T
  if (!response.ok) throw new ApiError(response.status, await parseErrorBody(response))
  return (await response.json()) as T
}

/** True for exactly the paths that must never trigger a refresh-and-retry (avoids infinite loops). */
const AUTH_BOOTSTRAP_PATHS = new Set([
  '/api/v1/auth/login',
  '/api/v1/auth/refresh',
  '/api/v1/auth/mfa/login-verify',
  '/api/v1/setup/first-admin',
])

/**
 * The central typed client every page depends on (Phase 1, Section 8).
 * On a 401 from an authenticated call (not login/refresh themselves),
 * attempts exactly one silent refresh and retries the original request
 * once; if the refresh itself fails, clears the session so `RequireAuth`
 * redirects to Login -- "a clear, immediate redirect to Login on expiry or
 * 401, never a blank/broken screen" (Section 6).
 */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  try {
    return await rawRequest<T>(path, options)
  } catch (error) {
    const canRetry =
      error instanceof ApiError &&
      error.status === 401 &&
      !options.skipAuth &&
      !AUTH_BOOTSTRAP_PATHS.has(path)
    if (!canRetry) throw error

    const refreshToken = useAuthStore.getState().refreshToken
    if (!refreshToken) {
      useAuthStore.getState().clear()
      throw error
    }

    try {
      const tokens = await rawRequest<TokenPairResponse>('/api/v1/auth/refresh', {
        method: 'POST',
        body: { refresh_token: refreshToken },
        skipAuth: true,
      })
      if (!tokens.access_token || !tokens.refresh_token) throw error
      useAuthStore
        .getState()
        .setTokens({ accessToken: tokens.access_token, refreshToken: tokens.refresh_token })
    } catch {
      useAuthStore.getState().clear()
      throw error
    }

    return rawRequest<T>(path, options)
  }
}

// --- Typed endpoint calls, matching app/modules/access_control/api.py exactly ---

export const authApi = {
  login: (email: string, password: string) =>
    apiRequest<TokenPairResponse>('/api/v1/auth/login', {
      method: 'POST',
      body: { email, password },
      skipAuth: true,
    }),

  mfaLoginVerify: (mfaToken: string, code: string) =>
    apiRequest<TokenPairResponse>('/api/v1/auth/mfa/login-verify', {
      method: 'POST',
      body: { mfa_token: mfaToken, code },
      skipAuth: true,
    }),

  logout: (refreshToken: string) =>
    apiRequest<void>('/api/v1/auth/logout', {
      method: 'POST',
      body: { refresh_token: refreshToken },
      skipAuth: true,
    }),

  me: () => apiRequest<MeResponse>('/api/v1/auth/me'),

  changePassword: (currentPassword: string, newPassword: string) =>
    apiRequest<void>('/api/v1/auth/change-password', {
      method: 'POST',
      body: { current_password: currentPassword, new_password: newPassword },
    }),

  mfaEnroll: () => apiRequest<MfaEnrollResponse>('/api/v1/auth/mfa/enroll', { method: 'POST' }),

  mfaVerify: (code: string) =>
    apiRequest<PublicUser>('/api/v1/auth/mfa/verify', { method: 'POST', body: { code } }),
}

export const adminApi = {
  provisionUser: (request: AdminProvisionUserRequest) =>
    apiRequest<PublicUser>('/api/v1/admin/users', { method: 'POST', body: request }),

  listUsers: (limit = 200, offset = 0) =>
    apiRequest<PublicUserListResponse>(`/api/v1/admin/users?limit=${limit}&offset=${offset}`),

  resetCredentials: (userId: string) =>
    apiRequest<AdminResetCredentialsResponse>(`/api/v1/admin/users/${userId}/reset-credentials`, {
      method: 'POST',
    }),
}

export const setupApi = {
  status: () =>
    apiRequest<FirstAdminSetupStatusResponse>('/api/v1/setup/first-admin/status', { skipAuth: true }),

  createFirstAdmin: (request: FirstAdminSetupRequest, setupToken: string) =>
    apiRequest<PublicUser>('/api/v1/setup/first-admin', {
      method: 'POST',
      body: request,
      skipAuth: true,
      headers: { 'X-TraceX-First-Admin-Setup-Token': setupToken },
    }),
}

export const casesApi = {
  get: (caseId: string) => apiRequest<CaseView>(`/api/v1/cases/${caseId}`),

  create: (request: CaseCreateRequest) =>
    apiRequest<CaseView>('/api/v1/cases', { method: 'POST', body: request }),

  addMember: (caseId: string, request: CaseMemberAddRequest) =>
    apiRequest<CaseMemberView>(`/api/v1/cases/${caseId}/members`, {
      method: 'POST',
      body: request,
    }),

  listMembers: (caseId: string) =>
    apiRequest<CaseMemberListResponse>(`/api/v1/cases/${caseId}/members`),

  listMemberCandidates: (caseId: string, limit = 200) =>
    apiRequest<CaseMemberCandidateListResponse>(
      `/api/v1/cases/${caseId}/member-candidates?limit=${limit}`,
    ),

  updateMember: (caseId: string, userId: string, request: CaseMemberUpdateRequest) =>
    apiRequest<CaseMemberView>(`/api/v1/cases/${caseId}/members/${userId}`, {
      method: 'PATCH',
      body: request,
    }),

  deactivateMember: (caseId: string, userId: string) =>
    apiRequest<CaseMemberView>(`/api/v1/cases/${caseId}/members/${userId}`, { method: 'DELETE' }),

  /** Newest-first, bounded to 200 -- matches `list_case_audit_events`'s own cap. */
  listAuditEvents: (caseId: string, limit = 50) =>
    apiRequest<CaseAuditEventListResponse>(`/api/v1/cases/${caseId}/audit?limit=${limit}`),
}

export const evidenceApi = {
  /**
   * Real multipart upload against `POST /cases/{id}/evidence` -- `source_type`/
   * `classification` are real backend enum values (Section 5, page 5), never
   * client-invented labels. `idempotencyKey` lets a retried upload of the
   * same file return the original result instead of a duplicate.
   */
  upload: (
    caseId: string,
    file: File,
    sourceType: SourceType,
    classification: EvidenceClassification,
    options: { parserProfile?: string; idempotencyKey?: string } = {},
  ) => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('source_type', sourceType)
    formData.append('classification', classification)
    if (options.parserProfile) formData.append('parser_profile', options.parserProfile)
    return apiRequest<EvidenceUploadResponse>(`/api/v1/cases/${caseId}/evidence`, {
      method: 'POST',
      body: formData,
      headers: options.idempotencyKey ? { 'Idempotency-Key': options.idempotencyKey } : undefined,
    })
  },

  list: (caseId: string) => apiRequest<EvidenceListResponse>(`/api/v1/cases/${caseId}/evidence`),

  getJob: (caseId: string, jobId: string) =>
    apiRequest<JobView>(`/api/v1/cases/${caseId}/jobs/${jobId}`),

  /** Gap-Closure WP-4 (G7): re-hash the stored object vs. the ingestion-time hash. */
  getIntegrity: (caseId: string, evidenceId: string) =>
    apiRequest<EvidenceIntegrityCheck>(`/api/v1/cases/${caseId}/evidence/${evidenceId}/integrity`),
}

export const notesApi = {
  /** Gap-Closure WP-4 (G3): append-only case narrative -- Investigation Memory's real data source. */
  list: (caseId: string, cursor?: string, limit = 50) =>
    apiRequest<CaseNoteListResponse>(
      `/api/v1/cases/${caseId}/notes?limit=${limit}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`,
    ),

  create: (caseId: string, request: CaseNoteCreateRequest) =>
    apiRequest<CaseNoteRecord>(`/api/v1/cases/${caseId}/notes`, { method: 'POST', body: request }),
}

export const integrityApi = {
  listCheckpoints: (caseId: string, limit = 50, offset = 0) =>
    apiRequest<IntegrityCheckpointListResponse>(
      `/api/v1/cases/${caseId}/integrity/checkpoints?limit=${limit}&offset=${offset}`,
    ),

  verifyCheckpoint: (caseId: string, checkpointId: string) =>
    apiRequest<VerificationResult>(
      `/api/v1/cases/${caseId}/integrity/checkpoints/${checkpointId}/verify`,
      { method: 'POST' },
    ),
}

export const reviewApi = {
  /** Correlation-candidate review (Nipun's Phase 5 links) -- Verify/Reject only, no third option. */
  listCandidates: (caseId: string, limit = 50) =>
    apiRequest<CandidateReviewListResponse>(`/api/v1/cases/${caseId}/candidates?limit=${limit}`),

  getCandidate: (caseId: string, candidateId: string) =>
    apiRequest<CandidateReviewView>(`/api/v1/cases/${caseId}/candidates/${candidateId}`),

  reviewCandidate: (
    caseId: string,
    candidateId: string,
    decision: CandidateReviewOutcome,
    rationale?: string,
  ) =>
    apiRequest<CandidateReviewView>(`/api/v1/cases/${caseId}/candidates/${candidateId}/review`, {
      method: 'POST',
      body: { decision, rationale: rationale ?? null },
    }),

  listHypotheses: (caseId: string, limit = 50, includeRejected = false) =>
    apiRequest<HypothesisListResponse>(
      `/api/v1/cases/${caseId}/hypotheses?limit=${limit}&include_rejected=${includeRejected}`,
    ),

  getHypothesis: (caseId: string, hypothesisId: string) =>
    apiRequest<HypothesisRecord>(`/api/v1/cases/${caseId}/hypotheses/${hypothesisId}`),

  proposeHypothesis: (caseId: string, submission: HypothesisCreateSubmission) =>
    apiRequest<HypothesisRecord>(`/api/v1/cases/${caseId}/hypotheses`, {
      method: 'POST',
      body: submission,
    }),

  reviewHypothesis: (
    caseId: string,
    hypothesisId: string,
    decision: HypothesisReviewOutcome,
    rationale?: string,
  ) =>
    apiRequest<HypothesisRecord>(`/api/v1/cases/${caseId}/hypotheses/${hypothesisId}/review`, {
      method: 'POST',
      body: { decision, rationale: rationale ?? null },
    }),

  getHandoff: (caseId: string) => apiRequest<HandoffSummary>(`/api/v1/cases/${caseId}/handoff`),
}

export const graphApi = {
  /** Bounded Entity/Event snapshot -- the Network Graph component's real data source (Section 7). */
  getSnapshot: (caseId: string, nodeLimit = 500, relationshipLimit = 1000) =>
    apiRequest<GraphSnapshotResponse>(
      `/api/v1/cases/${caseId}/graph?node_limit=${nodeLimit}&relationship_limit=${relationshipLimit}`,
    ),

  getAnalytics: (caseId: string) =>
    apiRequest<GraphAnalyticsResponse>(`/api/v1/cases/${caseId}/analytics`),

  getMotifs: (caseId: string, limit = 100) =>
    apiRequest<GraphMotifsResponse>(`/api/v1/cases/${caseId}/motifs?limit=${limit}`),

  listCorrelations: (caseId: string, limit = 50) =>
    apiRequest<CorrelationListResponse>(`/api/v1/cases/${caseId}/graph/correlations?limit=${limit}`),

  /** Read-only pass-through of Nipun's Phase 5 candidate links -- never decorated with a review status. */
  listRawCandidates: (caseId: string, limit = 50) =>
    apiRequest<CandidateListResponse>(`/api/v1/cases/${caseId}/graph/candidates?limit=${limit}`),

  /** Real observation-level provenance (event_time, lat/lng, mentions) -- Timeline + Map's data source. */
  listObservations: (caseId: string, limit = 200, offset = 0) =>
    apiRequest<CaseGraphObservationsResponse>(
      `/api/v1/cases/${caseId}/graph/observations?limit=${limit}&offset=${offset}`,
    ),

  /** Evidence Viewer's real drill-down data source (Section 5, page 13): every
   * observation yielded by one piece of evidence, each carrying its exact
   * `source_locator` (page/row/frame/timestamp). */
  getEvidenceObservations: (caseId: string, evidenceId: string) =>
    apiRequest<EvidenceObservationsResponse>(
      `/api/v1/cases/${caseId}/evidence/${evidenceId}/observations`,
    ),

  /** Citation drill-down (Section 9 row 13): resolves one observation_id, as
   * cited by a Hypothesis or Candidate Review evidence panel, to its exact
   * source location. */
  getObservationProvenance: (caseId: string, observationId: string) =>
    apiRequest<ObservationProvenanceResponse>(
      `/api/v1/cases/${caseId}/observations/${observationId}/provenance`,
    ),
}

export const entityApi = {
  list: (caseId: string, limit = 200) =>
    apiRequest<EntityListResponse>(`/api/v1/cases/${caseId}/entities?limit=${limit}`),

  get: (entityId: string) => apiRequest<{ entity: EntityV1 }>(`/api/v1/entities/${entityId}`),

  listCandidates: (caseId: string) =>
    apiRequest<EntityResolutionCandidateListResponse>(`/api/v1/cases/${caseId}/entity-candidates`),

  /**
   * Entity-resolution review decision -- the real backend behind Section 5's
   * "Candidate Review" page (its own role text names "entity-resolution
   * candidates" explicitly). `candidate_id` is a query param, not part of
   * the path, matching `entity_api.py`'s real signature exactly.
   */
  reviewResolution: (
    entityId: string,
    candidateId: string,
    decision: EntityReviewOutcome,
    rationale?: string,
  ) =>
    apiRequest<EntityReviewDecisionRecord>(
      `/api/v1/entities/${entityId}/resolution-review?candidate_id=${candidateId}`,
      { method: 'POST', body: { decision, rationale: rationale ?? null } },
    ),
}

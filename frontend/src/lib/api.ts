/** Single typed client for the TraceX backend. Every page imports from here — no inline fetch() elsewhere. */

const API_BASE = `${import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000"}/v1`;

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
}

let authToken: string | null = null;
export function setAuthToken(token: string | null) {
  authToken = token;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (!(init.body instanceof FormData) && init.body) headers.set("Content-Type", "application/json");
  if (authToken) headers.set("Authorization", `Bearer ${authToken}`);
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (!response.ok) {
    let detail: unknown;
    try {
      detail = await response.json();
    } catch {
      detail = response.statusText;
    }
    throw new ApiError(response.status, (detail as { detail?: unknown })?.detail ?? detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

// ---- Types (field names match app/api/routes.py exactly) ----

export type Case = { case_id: string; name: string; synthetic: boolean; created_at: string | null };
export type CaseWithRole = Case & { role: "case_lead" | "analyst" | "reviewer" };
export type CaseMember = { actor: string; role: "case_lead" | "analyst" | "reviewer" };
export type CaseDetail = Case & { members: CaseMember[] };

export type EvidenceSourceRow = {
  source_id: string;
  filename: string;
  sha256: string;
  byte_size: number;
  source_format: string;
  synthetic: boolean;
};

export type ImportJob = {
  job_id: string;
  case_id: string;
  source_id: string;
  state: "queued" | "running" | "checkpointed" | "completed" | "failed";
  stage: string;
  attempt: number;
  lease_expires_at: string | null;
  bytes_read: number;
  rows_seen: number;
  rows_accepted: number;
  rows_quarantined: number;
  snapshot_id: string | null;
  error_code: string | null;
  error_detail: string | null;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
};

export type GraphPath = { nodes: string[]; edge_ids: string[] } | null;

export type Finding = {
  finding_id: string;
  finding_type: string;
  finding_version: number;
  case_id: string;
  snapshot_id: string;
  graph_snapshot_id: string;
  entity_or_transaction_id: string;
  entity_ref: string;
  window_start: string;
  window_end: string;
  rule_id: string;
  rule_version: string;
  claim: string;
  raw_score: number;
  score: number;
  rank: number | null;
  coverage: Record<string, unknown>;
  uncertainty: Record<string, unknown>;
  reason_codes: string[];
  explanation: string;
  evidence_refs: unknown[];
  graph_path: GraphPath;
  feature_vector_hash: string;
  explanations: string[];
  benign_alternatives: string[];
  opposing_evidence: unknown[];
  source_refs: unknown[];
  status: "open" | "triaged" | "dismissed" | "escalated" | "needs_data_review";
};

export type FindingsListResponse = { findings: Finding[]; limit: number; offset: number; method: string; ml_enabled: boolean };

export type ReviewRecord = {
  review_id: string;
  finding_id: string;
  finding_version: number;
  disposition: string;
  reason: string;
  counterevidence_refs: unknown[];
  prior_review_id: string | null;
  created_at: string | null;
};

export type AuditEntry = { audit_id: string; action: string; detail: Record<string, unknown>; created_at: string | null };

export type FindingEvidence = {
  finding: Finding;
  feature_vector: Record<string, unknown>;
  source_refs: unknown[];
  coverage: Record<string, unknown>;
  opposing_evidence: unknown[];
  review_history: ReviewRecord[];
  audit_history: AuditEntry[];
  replay_contract: { snapshot_id: string; graph_snapshot_id: string; rule_id: string; rule_version: string; ml_enabled: boolean };
};

export type FindingsExportBundle = {
  case_id: string;
  method: string;
  ml_enabled: boolean;
  limitations: string[];
  findings: { finding: Finding; feature_vector: Record<string, unknown>; source_refs: unknown[]; opposing_evidence: unknown[]; review_history: ReviewRecord[]; audit_history: AuditEntry[] }[];
};

export type FeatureExportRow = {
  feature_row_id: string;
  entity_ref: string;
  snapshot_id: string;
  graph_snapshot_id: string;
  window_start: string;
  window_end: string;
  coverage: Record<string, unknown>;
  source_refs: unknown[];
  features: Record<string, number | string | null>;
};

export type FeatureExportResponse = {
  case_id: string;
  snapshot_id: string | null;
  feature_schema_version: string;
  ml_enabled: boolean;
  rows: FeatureExportRow[];
};

export type GraphNode = { id: string; type: string; label: string; attributes: Record<string, unknown> };
export type GraphEdge = { id: string; from: string; to: string; type: string; attributes: Record<string, unknown>; uncertainty: number | null };
export type GraphResponse = {
  snapshot_id: string;
  graph_snapshot_id: string;
  coverage: Record<string, unknown>;
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated_nodes: number;
  truncated_edges: number;
  cursor: string | null;
};

// ---- Auth ----

export const api = {
  signup: (display_name: string, password: string) =>
    request<{ token: string; actor: string }>("/auth/signup", { method: "POST", body: JSON.stringify({ display_name, password }) }),
  login: (display_name: string, password: string) =>
    request<{ token: string; actor: string }>("/auth/login", { method: "POST", body: JSON.stringify({ display_name, password }) }),

  // ---- Cases ----
  listCases: () => request<{ cases: CaseWithRole[] }>("/cases"),
  getCase: (caseId: string) => request<CaseDetail>(`/cases/${caseId}`),
  createCase: (name: string, synthetic: boolean) =>
    request<Case>("/cases", { method: "POST", body: JSON.stringify({ name, synthetic }) }),
  addMember: (caseId: string, actor: string, role: "case_lead" | "analyst" | "reviewer") =>
    request<{ case_id: string; actor: string; role: string }>(`/cases/${caseId}/members`, {
      method: "POST",
      body: JSON.stringify({ actor, role }),
    }),
  listSources: (caseId: string) => request<{ sources: EvidenceSourceRow[] }>(`/cases/${caseId}/sources`),

  // ---- Ingestion ----
  createImport: (caseId: string, file: File, idempotencyKey: string) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ import_id: string; job_id: string; source_id: string; idempotent_replay: boolean; state: string }>(
      `/cases/${caseId}/imports`,
      { method: "POST", body: form, headers: { "Idempotency-Key": idempotencyKey } }
    );
  },
  getJob: (jobId: string) => request<ImportJob>(`/jobs/${jobId}`),
  getEvidenceRecord: (sourceId: string, locator: string) =>
    request<{ source_id: string; source_sha256: string; locator_type: string; locator: string; record: unknown }>(
      `/evidence/${sourceId}/records?locator=${encodeURIComponent(locator)}`
    ),

  // ---- Graph ----
  getGraph: (caseId: string, seed: string, depth: number, nodeLimit: number, edgeLimit: number) =>
    request<GraphResponse>(
      `/cases/${caseId}/graph?seed=${encodeURIComponent(seed)}&depth=${depth}&node_limit=${nodeLimit}&edge_limit=${edgeLimit}`
    ),

  // ---- Findings ----
  listFindings: (caseId: string, limit = 50, offset = 0) =>
    request<FindingsListResponse>(`/cases/${caseId}/findings?limit=${limit}&offset=${offset}`),
  getFindingEvidence: (findingId: string) => request<FindingEvidence>(`/findings/${findingId}/evidence`),
  submitReview: (
    findingId: string,
    body: { expected_finding_version: number; disposition: string; reason: string; counterevidence_refs: { evidence_id: string; locator: string }[] }
  ) => request<{ review_id: string; finding: Finding }>(`/findings/${findingId}/reviews`, { method: "POST", body: JSON.stringify(body) }),
  exportFindings: (caseId: string) => request<FindingsExportBundle>(`/cases/${caseId}/findings/export`),

  // ---- Features (Phase 5A / Model & Rules) ----
  exportFeatures: (caseId: string, snapshotId?: string) =>
    request<FeatureExportResponse>(`/cases/${caseId}/features/export${snapshotId ? `?snapshot_id=${snapshotId}` : ""}`),
};

export { API_BASE };

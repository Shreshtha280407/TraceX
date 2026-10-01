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

export type JobProgress = {
  /** null when the format has no cheap record count — render indeterminate. */
  percent: number | null;
  basis: string;
  determinate: boolean;
};

export type ImportJob = {
  job_id: string;
  case_id: string;
  source_id: string;
  state: "queued" | "running" | "checkpointed" | "completed" | "failed";
  stage: string;
  total_records: number | null;
  progress: JobProgress;
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

/** One verified hop of a peeling-chain finding (app.engine.motifs.deterministic
 * ::detect_peeling_chains) — only present for rule_id === "peeling_chain_candidate",
 * and only on findings materialized after this field started being persisted. */
export type PeelOutput = { output_id: string; address: string | null; amount_sats: number; is_spent: boolean };

export type PeelingStep = {
  previous_output_id: string;
  spending_transaction_id: string;
  continuing_output_id: string | null;
  previous_value_sats: number;
  continuing_value_sats: number | null;
  timestamp: string | null;
  edge_ids: string[];
  co_spend_input_address_count: number;
  previous_address: string | null;
  previous_script_type: string | null;
  continuing_address: string | null;
  continuing_script_type: string | null;
  input_count: number;
  output_count: number;
  peel_outputs: PeelOutput[];
  peel_output_total: number;
  co_spend_addresses: string[];
};

/** The whole reviewable peeling pattern: overlapping chain candidates that share
 * transactions (or an origin) merged into one finding. Ids are graph node ids. */
export type FindingPattern = {
  chain_count: number;
  transaction_count: number;
  address_count: number;
  transaction_ids: string[];
  addresses: string[];
  chains: { entity_ref: string; hop_count: number; score: number; transaction_ids: string[] }[];
};

export type MatchedWindow = { window_seconds: number; window_start: string; window_end: string; score: number };

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
  // peeling_chain_candidate-only, and only on findings materialized after this
  // was added to detector_result -- older findings will have these as null.
  hop_count: number | null;
  total_duration_sec: number | null;
  steps: PeelingStep[] | null;
  /** Present on grouped peeling patterns (findings materialized after grouping). */
  pattern?: FindingPattern | null;
  /** Other windows of the same day that met the same rule, merged into this finding. */
  matched_windows?: MatchedWindow[] | null;
};

export type PathSignals = {
  finding_id: string;
  velocity: number | null;
  peel_ratio: number | null;
  cluster_link: number | null;
  entity_risk: number;
  entity_risk_matches: string[];
  entity_risk_path_node_count: number;
  confidence: number;
};

export type FindingsSummary = {
  case_id: string;
  total: number;
  by_status: Record<string, number>;
  open: number;
};

export type FindingsListResponse = {
  findings: Finding[];
  /** Real totals for the whole case — NOT the length of this page. */
  total: number;
  open_total: number;
  limit: number;
  offset: number;
  method: string;
  methods: string[];
  ml_enabled: boolean;
};

/** Must match `ML_RULE_VERSION` in app/ml_release_constants.py — the anomaly
 * stack's frozen release identity, not a value either side computes. */
export const ML_RULE_VERSION = "anomaly-stack-v1";

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
  replay_contract: {
    snapshot_id: string;
    graph_snapshot_id: string;
    rule_id: string;
    rule_version: string;
    ml_enabled: boolean;
    release_id: string | null;
    model_run_id: string | null;
  };
};

export type ChatMessage = { role: "user" | "assistant"; content: string };

export type FindingsExportBundle = {
  case_id: string;
  method: string;
  methods: string[];
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

/** One direct counterparty of the flow source (app/engine/graph/query.py::query_flow).
 * For a transaction source these are addresses (grouped over their UTXOs); for an
 * address source they are the transactions that paid it or spent from it. */
export type FlowCounterparty = {
  id: string;
  type: "address_or_script" | "transaction" | "output";
  label: string;
  /** null when any aggregated UTXO has no committed amount. */
  amount_sats: number | null;
  utxo_count: number;
  spent_count: number;
  /** Up to 5 connecting ids: funding/spending txids, or the UTXOs involved. */
  via: string[];
  timestamp: string | null;
  /** Node to re-centre on when this counterparty becomes the source. */
  source_id: string | null;
  /** Activity beyond the current source: other transactions (address) or other legs (transaction). */
  onward_count: number;
  selectable: boolean;
  reason: string | null;
  /** Open findings in this snapshot whose subject is this node. */
  open_finding_count?: number;
};

export type FlowResponse = {
  snapshot_id: string;
  graph_snapshot_id: string;
  center: {
    id: string;
    type: "address_or_script" | "transaction";
    label: string;
    attributes: Record<string, unknown>;
    timestamp: string | null;
    /** Set when an output was requested and resolved to its address / creating tx. */
    resolved_from: string | null;
    transaction_count: number | null;
    selectable: boolean;
    reason: string | null;
    open_finding_count?: number;
  };
  inputs: FlowCounterparty[];
  outputs: FlowCounterparty[];
  totals: {
    input_count: number;
    output_count: number;
    input_sats: number | null;
    output_sats: number | null;
    dead_end_inputs: number;
    dead_end_outputs: number;
  };
  truncated_inputs: number;
  truncated_outputs: number;
  coverage: Record<string, unknown>;
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

  getGraphFlow: (caseId: string, node: string, limit = 40, graphSnapshotId?: string) =>
    request<FlowResponse>(
      `/cases/${caseId}/graph/flow?node=${encodeURIComponent(node)}&limit=${limit}${
        graphSnapshotId ? `&graph_snapshot_id=${encodeURIComponent(graphSnapshotId)}` : ""
      }`
    ),

  // ---- Findings ----
  listFindings: (caseId: string, limit = 50, offset = 0, ruleIds?: string[]) => {
    const ruleParams = (ruleIds ?? []).map((id) => `rule_id=${encodeURIComponent(id)}`).join("&");
    return request<FindingsListResponse>(
      `/cases/${caseId}/findings?limit=${limit}&offset=${offset}${ruleParams ? `&${ruleParams}` : ""}`
    );
  },
  getFindingsSummary: (caseId: string) => request<FindingsSummary>(`/cases/${caseId}/findings/summary`),
  getFindingEvidence: (findingId: string) => request<FindingEvidence>(`/findings/${findingId}/evidence`),
  getPathSignals: (findingId: string) => request<PathSignals>(`/findings/${findingId}/path-signals`),
  submitReview: (
    findingId: string,
    body: { expected_finding_version: number; disposition: string; reason: string; counterevidence_refs: { evidence_id: string; locator: string }[] }
  ) => request<{ review_id: string; finding: Finding }>(`/findings/${findingId}/reviews`, { method: "POST", body: JSON.stringify(body) }),
  exportFindings: (caseId: string) => request<FindingsExportBundle>(`/cases/${caseId}/findings/export`),
  chatAboutFinding: (findingId: string, question: string, history: ChatMessage[]) =>
    request<{ answer: string; model: string }>(`/findings/${findingId}/chat`, {
      method: "POST",
      body: JSON.stringify({ question, history }),
    }),

  // ---- Features (Phase 5A) ----
  exportFeatures: (caseId: string, snapshotId?: string) =>
    request<FeatureExportResponse>(`/cases/${caseId}/features/export${snapshotId ? `?snapshot_id=${snapshotId}` : ""}`),

  // ---- System (Settings page) ----
  health: () =>
    request<{
      status: string;
      database: string;
      evidence_vault: string;
      /** What this host can give an import now, and the sizes derived from it (app/resources.py). */
      resources?: {
        total_memory_mb: number | null;
        available_memory_mb: number | null;
        memory_budget_mb: number;
        cpu_count: number;
        insert_chunk_rows: number;
        max_ingestion_batch_records: number;
        duckdb_memory_limit_mb: number;
      };
    }>("/healthz"),
  ready: () => request<{ status: string; worker_id: string; heartbeat_at: string }>("/readyz"),
};

export { API_BASE };

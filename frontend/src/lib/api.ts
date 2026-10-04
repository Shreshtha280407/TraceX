/** Single typed client for the TraceX backend. Every page imports from here — no inline fetch() elsewhere. */

const API_BASE = `${import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000"}/v1`;

export type InvestigationGroup = {
  group_id: string; case_id: string; snapshot_id: string; run_id: string; family: string; episode_type: string;
  proposition: string; focal_ref: string; window_start: string; window_end: string;
  member_count: number; transaction_count: number; entity_count: number; representative_finding_id: string;
  status: string; review_version: number;
};
export type InvestigationQueue = {
  policy: string; capacity: number; underlying_findings: number; investigation_groups: number; unresolved_groups: number;
  queued_groups: number; backlog_groups: number; filtered_total: number; scope: string; items: InvestigationGroup[];
};
export type InvestigationDetail = InvestigationGroup & {
  grouping_version: string; procedure_sha256: string; active_generation: boolean; rationale: unknown;
  representative_evidence: StructuredFindingEvidence; member_decisions: Record<string, number>; mixed_member_decisions: boolean;
  group_decision_scope: string; evidence_scope: string;
};

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

export type ScoringMode = "auto_eligible" | "unsupervised" | "synthetic_demo" | "validated_candidate";
export type Case = { case_id: string; name: string; synthetic: boolean; created_at: string | null; scoring_mode?: ScoringMode; candidate_domain?: string | null };
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
  analysis?: { state: "running" | "complete" | "degraded" | "failed" | "unknown"; retry_supported: boolean;
    stages: { name: string; status: string; reason: string | null; duration_seconds: number | null; details?: Record<string, unknown> }[] } | null;
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

/** Immutable finding calibration provenance; historical unpinned rows are unvalidated. */
export type FindingConfidence = {
  /** null when the rule has no calibration (rank by score only) or is a data-consistency check. */
  value: number | null;
  method: "calibrated" | "statistical" | "evidence_check" | "uncalibrated";
  basis?: string | null;
  grade: "good" | "weak" | "unvalidated" | "statistical" | "not_applicable";
  calibration_id?: string | null;
  rule_base_rate?: number | null;
  reliability?: { findings?: number; positives?: number; brier?: number; ece?: number; auc?: number | null };
  /** Theoretical Gaussian tail; distribution/dependence assumptions are unvalidated. */
  anomaly_p_value?: number | null;
  network_corroboration?: number | null;
};

export type Finding = {
  finding_id: string;
  confidence?: FindingConfidence;
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
  family_rank?: number;
  family_total?: number;
  interpretation?: { category: string; review_disposition: string; scope: string };
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
  status: "open" | "triaged" | "confirmed" | "dismissed" | "escalated" | "needs_data_review";
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

// ---- Entities, network, risk (app/api/analytics_routes.py) ----

export type RiskScore = {
  wallet: string;
  kind: "entity" | "address" | "unknown";
  risk: number;
  downstream: number;
  upstream: number;
  tainted_received_sats: number;
  received_sats: number;
  is_seed: boolean;
};

export type EntitySummary = {
  entity_id: string;
  root_address: string;
  address_count: number;
  linking_tx_count: number;
  received_tx_count: number;
  received_sats: number;
  spent_tx_count: number;
  sent_sats: number;
  first_seen: string | null;
  last_seen: string | null;
  risk?: RiskScore | null;
};

export type AnalyticsSummary = Record<string, unknown> & {
  entity_count?: number;
  clustered_addresses?: number;
  largest_entity_addresses?: number;
  wallet_count?: number;
  mixing_excluded_transactions?: number;
  multi_input_transactions?: number;
  embedded_wallets?: number;
  geoip_installed?: boolean;
  geoip_sources?: { file: string; kind: string; licence: string; attribution: string }[];
  endpoint_ips?: number;
  endpoint_scopes?: Record<string, number>;
  observation_country_checks?: Record<string, number>;
  observation_asn_checks?: Record<string, number>;
  relay_concentrations?: number;
  geo_mismatch_endpoints?: number;
  network_findings?: number;
  correlation_wallets_tested?: number;
  observed_transactions?: number;
};

export type EntitiesResponse = {
  analytics_snapshot_id: string;
  snapshot_id: string;
  summary: AnalyticsSummary;
  total: number;
  limit: number;
  offset: number;
  entities: EntitySummary[];
};

export type RelayEndpoint = {
  ip: string;
  spends: number;
  scope: string | null;
  db_country: string | null;
  db_asn: number | null;
  db_as_org: string | null;
  reported_country: string | null;
  reported_asn: string | null;
  country_check: string | null;
  asn_check: string | null;
};

export type NetworkCorrelation = {
  dimension: "endpoint" | "asn";
  wallet: string;
  key: string;
  spends_on_key: number;
  observed_spends: number;
  base_rate: number;
  p_value: number;
  adjusted_p_value: number;
};

export type EntityDetail = {
  wallet: string;
  kind: "entity" | "address";
  entity: EntitySummary | null;
  addresses: string[];
  address_total: number;
  linking_transactions: { txid: string; input_addresses: number; time: string | null }[];
  linking_total: number;
  relay_endpoints: RelayEndpoint[];
  network_correlations: NetworkCorrelation[];
  findings: { finding_id: string; rule_id: string; entity_ref: string; raw_score: number; status: string }[];
  risk: RiskScore | null;
  basis?: string;
  caution?: string;
};

export type SimilarWallets = { wallet: string; status: string; method?: string; similar: { wallet: string; similarity: number }[] };

export type EndpointGeo = {
  ip: string;
  scope: string;
  transactions: number;
  src_observations: number;
  db_country: string | null;
  db_asn: number | null;
  db_as_org: string | null;
  reported_country: string | null;
  reported_asn: string | null;
  country_check: string;
  asn_check: string;
  median_relay_latency_s: number | null;
};

export type GeoIPStatus = {
  installed: boolean;
  directory: string;
  compiled_at?: string | null;
  sources?: { file: string; kind: string; licence: string; attribution: string; rows_v4: number; rows_v6: number }[];
  hint?: string;
};

export type NetworkResponse = {
  summary: AnalyticsSummary;
  top_endpoints: EndpointGeo[];
  countries: { country: string; observations: number }[];
  geoip: GeoIPStatus;
  network_finding_count: number;
};

export type RiskSeedRow = {
  seed_id: string;
  wallet_ref: string;
  label: string;
  reason: string;
  weight: number;
  source: string;
  created_at: string | null;
};

export type RiskRunView = {
  risk_run_id: string;
  snapshot_id: string;
  method_version: string;
  parameters: Record<string, unknown>;
  seeds: { seed_id: string; wallet_ref: string; wallet: string | null; label: string; weight: number; found: boolean }[];
  summary: Record<string, unknown> & { wallets_with_risk?: number; wallets_above_0_5?: number; method?: string };
  scores: RiskScore[];
  created_at: string | null;
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
  review_policy?: { version: string; order: string; meaning: string };
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
export const ML_RULE_VERSION = "anomaly-stack-v2";
/** Every anomaly-stack release shares this prefix (`ML_RULE_PREFIX` in the backend). */
export const ML_RULE_PREFIX = "anomaly-stack-";

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
  structured_evidence: StructuredFindingEvidence;
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

export type EvidenceReference = { evidence_id: string; locator: string; locator_type?: string;
  source_sha256?: string; reference_status?: string };
export type StructuredFindingEvidence = {
  schema: string; proposition: string; responsible_procedure: Record<string, unknown>;
  interpretation: { category: string; review_disposition: string; scope: string };
  supporting_observations: { statement: string; source_refs: EvidenceReference[] }[];
  supporting_refs: EvidenceReference[];
  observed_counter_evidence: { kind: string; statement: string; basis?: string; source_refs: EvidenceReference[] }[];
  counter_evidence_summary: string; unverified_opposing_references: unknown[];
  benign_alternatives: { statement: string; basis: string }[];
  missing_evidence: string[]; coverage: Record<string, unknown>; graph_associations: GraphPath;
  features: Record<string, unknown>; comparison_baselines: Record<string, unknown>;
  review_decisions: ReviewRecord[]; score_provenance: Record<string, unknown>;
  explanation_labels: Record<string, unknown>; contextual_review: Record<string, unknown>;
};


export type FindingsExportBundle = {
  case_id: string;
  total: number;
  limit: number;
  offset: number;
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
  /** Every feature row in scope; `rows` is one page of them when `limit` is set. */
  total?: number;
  limit?: number;
  offset?: number;
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

export type ReceiptActivity = { case_id: string; job_id: string; provisional: boolean; total: number;
  entities: { address: string; transactions: number; participations: number }[] };
export type ReviewQueue = { eligible_scored_transactions: number; threshold_flagged_transactions: number;
  capacity: number; queued_transactions: number; additional_flagged_transactions: number; filtered_total: number;
  snapshot_id: string; items: { transaction: string; score: number; finding_ids: string[]; deterministic_finding_ids: string[] }[] };

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
  /** Common-input-ownership cluster of this address, when it has one. */
  entity_id?: string | null;
  entity_address_count?: number | null;
  /** Propagated risk (0..1) from the case's seed wallets, when a risk run exists. */
  risk?: number | null;
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
  createCase: (name: string, synthetic: boolean, scoring_mode: ScoringMode = "auto_eligible", candidate_domain?: string) =>
    request<Case>("/cases", { method: "POST", body: JSON.stringify({ name, synthetic, scoring_mode, candidate_domain }) }),
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
  getAnalysis: (caseId: string) => request<{ job_id: string; analysis: ImportJob["analysis"] }>(`/cases/${caseId}/analysis`),
  retryAnalysis: (caseId: string, jobId: string) => request<ImportJob>(`/cases/${caseId}/analysis/retry?job_id=${encodeURIComponent(jobId)}`, { method: "POST" }),
  getActivity: (caseId: string, jobId: string, offset = 0) => request<ReceiptActivity>(`/cases/${caseId}/activity?job_id=${encodeURIComponent(jobId)}&limit=20&offset=${offset}`),
  getReviewQueue: (caseId: string, k = 100, offset = 0) => request<ReviewQueue>(`/cases/${caseId}/review-queue?k=${k}&limit=20&offset=${offset}`),
  getInvestigationQueue: (caseId: string, capacity = 100, offset = 0, scope = "queue", reviewState = "") =>
    request<InvestigationQueue>(`/cases/${caseId}/investigation-queue?capacity=${capacity}&limit=20&offset=${offset}&scope=${scope}${reviewState ? `&review_state=${reviewState}` : ""}`),
  getInvestigation: (id: string) => request<InvestigationDetail>(`/investigation-groups/${id}`),
  getInvestigationMembers: (id: string, offset = 0) => request<{total: number; items: {finding: Finding; structured_evidence: unknown}[]}>(`/investigation-groups/${id}/members?limit=20&offset=${offset}`),
  getInvestigationReviews: (id: string, offset = 0) => request<{items: {review_id: string; disposition: string; reason: string; review_version: number; counterevidence_refs: EvidenceReference[]}[]}>(`/investigation-groups/${id}/reviews?offset=${offset}&limit=20`),
  getInvestigationReplacements: (id: string) => request<{items: {prior_id: string; replacement_id: string; reason: string}[]}>(`/investigation-groups/${id}/replacements`),
  reviewInvestigation: (id: string, expected_review_version: number, disposition: string, reason: string, counterevidence_refs: EvidenceReference[] = []) =>
    request<{group: InvestigationGroup}>(`/investigation-groups/${id}/reviews`, {method: "POST", body: JSON.stringify({expected_review_version, disposition, reason, counterevidence_refs})}),
  getEvidenceRecord: (sourceId: string, locator: string) =>
    request<{ source_id: string; source_sha256: string; locator_type: string; locator: string; record: unknown }>(
      `/evidence/${sourceId}/records?locator=${encodeURIComponent(locator)}`
    ),

  // ---- Graph ----
  getGraph: (caseId: string, seed: string, depth: number, nodeLimit: number, edgeLimit: number, cursor?: string, graphSnapshotId?: string) =>
    request<GraphResponse>(
      `/cases/${caseId}/graph?seed=${encodeURIComponent(seed)}&depth=${depth}&node_limit=${nodeLimit}&edge_limit=${edgeLimit}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}${graphSnapshotId ? `&graph_snapshot_id=${encodeURIComponent(graphSnapshotId)}` : ""}`
    ),

  getGraphFlow: (caseId: string, node: string, limit = 40, graphSnapshotId?: string) =>
    request<FlowResponse>(
      `/cases/${caseId}/graph/flow?node=${encodeURIComponent(node)}&limit=${limit}${
        graphSnapshotId ? `&graph_snapshot_id=${encodeURIComponent(graphSnapshotId)}` : ""
      }`
    ),

  // ---- Findings ----
  listFindings: (caseId: string, limit = 50, offset = 0, ruleIds?: string[], reviewState?: string) => {
    const ruleParams = (ruleIds ?? []).map((id) => `rule_id=${encodeURIComponent(id)}`).join("&");
    return request<FindingsListResponse>(
      `/cases/${caseId}/findings?limit=${limit}&offset=${offset}${ruleParams ? `&${ruleParams}` : ""}${reviewState ? `&review_state=${encodeURIComponent(reviewState)}` : ""}`
    );
  },
  getFindingsSummary: (caseId: string) => request<FindingsSummary>(`/cases/${caseId}/findings/summary`),
  getFindingEvidence: (findingId: string) => request<FindingEvidence>(`/findings/${findingId}/evidence`),
  getPathSignals: (findingId: string) => request<PathSignals>(`/findings/${findingId}/path-signals`),
  submitReview: (
    findingId: string,
    body: { expected_finding_version: number; disposition: string; reason: string; counterevidence_refs: { evidence_id: string; locator: string; source_sha256?: string }[] }
  ) => request<{ review_id: string; finding: Finding }>(`/findings/${findingId}/reviews`, { method: "POST", body: JSON.stringify(body) }),
  exportFindings: (caseId: string) => request<FindingsExportBundle>(`/cases/${caseId}/findings/export`),

  // ---- Entities, network correlation, Geo-IP, risk ----
  listEntities: (caseId: string, limit = 50, offset = 0) =>
    request<EntitiesResponse>(`/cases/${caseId}/entities?limit=${limit}&offset=${offset}`),
  getEntity: (caseId: string, wallet: string) =>
    request<EntityDetail>(`/cases/${caseId}/entities/${encodeURIComponent(wallet)}`),
  getSimilarWallets: (caseId: string, wallet: string, limit = 10) =>
    request<SimilarWallets>(`/cases/${caseId}/entities/${encodeURIComponent(wallet)}/similar?limit=${limit}`),
  getNetwork: (caseId: string) => request<NetworkResponse>(`/cases/${caseId}/network`),
  getEndpoint: (caseId: string, ip: string) =>
    request<EndpointGeo & { wallets: { wallet: string; spends: number }[] }>(
      `/cases/${caseId}/network/endpoints/${encodeURIComponent(ip)}`
    ),
  geoipStatus: () => request<GeoIPStatus>("/geoip/status"),
  getRisk: (caseId: string, limit = 200) =>
    request<{ seeds: RiskSeedRow[]; run: RiskRunView | null; analytics_available: boolean }>(
      `/cases/${caseId}/risk?limit=${limit}`
    ),
  addRiskSeed: (caseId: string, body: { wallet_ref: string; label: string; reason: string; weight: number }) =>
    request<{ seed: RiskSeedRow; run: RiskRunView | null }>(`/cases/${caseId}/risk/seeds`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  deleteRiskSeed: (caseId: string, seedId: string) =>
    request<{ deleted: string; run: RiskRunView | null }>(`/cases/${caseId}/risk/seeds/${seedId}`, { method: "DELETE" }),

  // ---- Features (Phase 5A) ----
  exportFeatures: (caseId: string, snapshotId?: string, limit?: number) => {
    const query = [snapshotId ? `snapshot_id=${snapshotId}` : "", limit ? `limit=${limit}` : ""].filter(Boolean).join("&");
    return request<FeatureExportResponse>(`/cases/${caseId}/features/export${query ? `?${query}` : ""}`);
  },
  /** The snapshot's complete feature store as a Parquet file (all rows, compressed). */
  downloadFeaturesParquet: async (caseId: string, snapshotId: string): Promise<Blob> => {
    const headers = new Headers();
    if (authToken) headers.set("Authorization", `Bearer ${authToken}`);
    const response = await fetch(
      `${API_BASE}/cases/${caseId}/features/export.parquet?snapshot_id=${encodeURIComponent(snapshotId)}`,
      { headers }
    );
    if (!response.ok) throw new ApiError(response.status, response.statusText);
    return response.blob();
  },

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
        in_memory_record_limit: number;
        execution_mode_override: string | null;
      };
    }>("/healthz"),
  ready: () => request<{ status: string; worker_id: string; heartbeat_at: string }>("/readyz"),
};

export { API_BASE };

import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, Badge, ConfidenceBadge, FilterPills, methodOf, relativeTime, priorityTier, priorityOf, type PriorityId } from "../components/primitives";
import { api, ML_RULE_PREFIX, type Finding, type FindingsListResponse } from "../lib/api";
import "./FindingsFeed.css";
import { InvestigationQueue } from "../components/InvestigationQueue";

type MlReleaseInfo = { releaseId: string; modelRunId: string; layers: string[] };

type FilterId = "all" | "deterministic" | "ml" | "network" | "reviewed";
const NETWORK_RULE_VERSION = "network-correlation-v1";

export function FindingsFeed() {
  const { caseId } = useParams<{ caseId: string }>();
  const navigate = useNavigate();
  const [response, setResponse] = useState<FindingsListResponse | null>(null);
  const [offset, setOffset] = useState(0);
  const [reviewState, setReviewState] = useState("");
  const [findingsError, setFindingsError] = useState<string | null>(null);
  const [filter, setFilter] = useState<FilterId>("all");
  const [priorityFilter, setPriorityFilter] = useState<PriorityId>("all");
  const [thresholds, setThresholds] = useState<Record<string, number>>({});
  const [mlInfo, setMlInfo] = useState<MlReleaseInfo | null>(null);
  const [queue, setQueue] = useState<Awaited<ReturnType<typeof api.getReviewQueue>> | null>(null);
  const [queueError, setQueueError] = useState<string | null>(null);
  const [capacity, setCapacity] = useState(100);
  const [queueOffset, setQueueOffset] = useState(0);

  useEffect(() => {
    if (!caseId) return;
    let active = true;
    api.getReviewQueue(caseId, capacity, queueOffset).then((result) => {
      if (active) { setQueue(result); setQueueError(null); }
    }).catch((err) => { if (active) { setQueue(null); setQueueError(String(err)); } });
    return () => { active = false; };
  }, [caseId, capacity, queueOffset]);

  useEffect(() => {
    if (!caseId) return;
    let active = true;
    api.listFindings(caseId, 200, offset, undefined, reviewState).then((result) => { if (active) { setResponse(result); setFindingsError(null); } }).catch((error) => { if (active) { setResponse(null); setFindingsError(String(error)); } });
    return () => { active = false; };
  }, [caseId, offset, reviewState]);

  useEffect(() => {
    if (!response) return;
    const uniqueByRule = new Map<string, Finding>();
    for (const finding of response.findings) if (!uniqueByRule.has(finding.rule_id)) uniqueByRule.set(finding.rule_id, finding);
    Promise.all([...uniqueByRule.values()].map((f) => api.getFindingEvidence(f.finding_id).catch(() => null))).then((results) => {
      const merged: Record<string, number> = {};
      let ml: MlReleaseInfo | null = null;
      for (const result of results) {
        const raw = (result?.feature_vector as Record<string, unknown> | undefined)?.rule_thresholds;
        if (raw && typeof raw === "object") Object.assign(merged, raw as Record<string, number>);
        if (result?.replay_contract.ml_enabled && !ml) {
          const layers = (result.feature_vector as Record<string, unknown>).layers;
          ml = {
            releaseId: result.replay_contract.release_id ?? "—",
            modelRunId: result.replay_contract.model_run_id ?? "—",
            layers: layers && typeof layers === "object" ? Object.keys(layers) : [],
          };
        }
      }
      setThresholds(merged);
      setMlInfo(ml);
    });
  }, [response]);

  const findings = useMemo(() => response?.findings ?? [], [response]);
  // Rank percentiles must divide by the case's REAL finding count, not by how
  // many rows this page happened to return, or every tier is wrong once the
  // case has more findings than the page limit.
  const total = response?.total ?? findings.length;
  const openTotal = response?.open_total ?? 0;
  const truncated = total > findings.length;

  const byCategory = useMemo(
    () => ({
      all: findings,
      deterministic: findings.filter((f) => !f.rule_version.startsWith(ML_RULE_PREFIX) && f.rule_version !== NETWORK_RULE_VERSION),
      ml: findings.filter((f) => f.rule_version.startsWith(ML_RULE_PREFIX)),
      network: findings.filter((f) => f.rule_version === NETWORK_RULE_VERSION),
      reviewed: findings.filter((f) => f.status !== "open"),
    }),
    [findings]
  );

  const filtered = byCategory[filter];

  const priorityCounts = useMemo(() => {
    const counts = { high: 0, medium: 0, low: 0 };
    for (const f of filtered) counts[priorityTier(f.family_rank ?? null, f.family_total ?? 0)]++;
    return counts;
  }, [filtered]);

  const filteredByPriority = useMemo(() => {
    if (priorityFilter === "all") return filtered;
    return filtered.filter((f) => priorityTier(f.family_rank ?? null, f.family_total ?? 0) === priorityFilter);
  }, [filtered, priorityFilter]);

  function selectCategory(next: FilterId) {
    setFilter(next);
    setPriorityFilter("all");
  }

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Findings Feed</h1>
          <p className="subtitle">Observed structural patterns, anomaly triage and network measurements — not identity or criminality verdicts</p>
          <label>Reviewer context <select className="inline-control" value={reviewState} onChange={(event) => { setReviewState(event.target.value); setOffset(0); }}>
            <option value="">All dispositions</option><option value="open">Unreviewed</option><option value="escalated">Reviewer escalated</option><option value="dismissed">Dismissed — observations retained</option><option value="needs_data_review">Needs more evidence</option><option value="confirmed">Confirmed proposition</option>
          </select></label>
          {findingsError && <p role="alert">Findings could not be loaded: {findingsError}. Check analysis status and API readiness.</p>}
        </div>
        {response && (
          <div className="ff-counters">
            <span>Unreviewed findings <b>{openTotal.toLocaleString()}</b></span>
            <span>Underlying findings <b>{total.toLocaleString()}</b></span>
          </div>
        )}
      </div>

      {caseId && <InvestigationQueue caseId={caseId} />}
      <h2>Underlying observations — individual history retained</h2>
      {truncated && (
        <p className="coverage-note" style={{ marginBottom: 10 }}>
          Showing {findings.length.toLocaleString()} of {total.toLocaleString()} findings. Families are independently ranked
          and interleaved; raw scores from different families are not compared. Filter counts describe this page.
        </p>
      )}
      <p className="coverage-note">{response?.review_policy?.version}: per-family rank is a workflow priority, not calibrated cross-family risk. An analyst's confirmation concerns only the stated pattern.</p>
      <button type="button" className="btn-ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 200))}>Previous findings page</button>
      <button type="button" className="btn-ghost" disabled={offset + findings.length >= total} onClick={() => setOffset(offset + 200)}>Next findings page</button>

      <div className="two-col">
        <div>
          <NeoCard>
            <h2>Separate ML transaction triage queue</h2>
            <label>Transactions to queue <input className="inline-control" type="number" min={0} max={1000000} value={capacity}
              onChange={(e) => { setCapacity(Math.max(0, Math.floor(Number(e.target.value) || 0))); setQueueOffset(0); }} /></label>
            <p className="coverage-note">A separate queue for the current ML scorer. Threshold flags and deterministic findings remain available below.</p>
            {queueError && <p className="coverage-note">{queueError}</p>}
            {queue && <>
              <p className="coverage-note">{queue.eligible_scored_transactions.toLocaleString()} scored · {queue.threshold_flagged_transactions.toLocaleString()} threshold flagged · capacity {queue.capacity} · {queue.queued_transactions} queued · {queue.additional_flagged_transactions} additional flags outside the queue</p>
              {!queue.items.length && <p className="coverage-note">No flagged transactions in this queue page.</p>}
              {queue.items.map((item) => <p key={item.transaction}>
                <button type="button" className="btn-ghost" onClick={() => navigate(`/findings/${item.finding_ids[0]}`)}>{item.transaction.slice(0, 22)}…</button>
                {" "}score {item.score.toFixed(3)} · {item.deterministic_finding_ids.length} linked deterministic findings
              </p>)}
              <button type="button" className="btn-ghost" disabled={queueOffset === 0} onClick={() => setQueueOffset(Math.max(0, queueOffset - 20))}>Previous queue page</button>
              <button type="button" className="btn-ghost" disabled={queueOffset + 20 >= queue.filtered_total} onClick={() => setQueueOffset(queueOffset + 20)}>Next queue page</button>
            </>}
          </NeoCard>
          <div style={{ marginBottom: 10 }}>
            <FilterPills
              active={filter}
              onChange={selectCategory}
              options={[
                { id: "all", label: `All (${byCategory.all.length})` },
                { id: "deterministic", label: `Deterministic (${byCategory.deterministic.length})` },
                { id: "ml", label: `ML-Flagged (${byCategory.ml.length})` },
                { id: "network", label: `Network (${byCategory.network.length})` },
                { id: "reviewed", label: `Reviewed (${byCategory.reviewed.length})` },
              ]}
            />
          </div>
          <div style={{ marginBottom: 16 }}>
            <FilterPills
              active={priorityFilter}
              onChange={setPriorityFilter}
              options={[
                { id: "all", label: `All priorities (${filtered.length})` },
                { id: "high", label: `High (${priorityCounts.high})` },
                { id: "medium", label: `Medium (${priorityCounts.medium})` },
                { id: "low", label: `Low (${priorityCounts.low})` },
              ]}
            />
          </div>
          {response === null ? (
            <p className="coverage-note">Loading…</p>
          ) : filteredByPriority.length === 0 ? (
            <p className="coverage-note">No findings match this filter.</p>
          ) : (
            filteredByPriority.map((finding) => {
              const priority = priorityOf(finding.family_rank ?? null, finding.family_total ?? 0);
              return (
                <NeoCard key={finding.finding_id} variant="neo-sm" className="finding-card">
                  <div onClick={() => navigate(`/findings/${finding.finding_id}`)}>
                    <div className="finding-card-top">
                      <div className="finding-card-left">
                        <strong>{finding.finding_type.replace(/_/g, " ")}</strong>
                        <Badge tone={methodOf(finding.rule_version).tone}>{methodOf(finding.rule_version).label}</Badge>
                        <Badge tone="warning">{finding.interpretation?.category.replaceAll("_", " ") ?? "observed pattern"}</Badge>
                        <Badge tone="warning">{finding.status}</Badge>
                      </div>
                      <div style={{ display: "flex", gap: 6 }}>
                        <ConfidenceBadge confidence={finding.confidence} />
                        <Badge tone={priority.tone}>{priority.label}</Badge>
                      </div>
                    </div>
                    <span className="mono-id addr">{finding.entity_ref}</span>
                    {(finding.pattern?.chain_count ?? 1) > 1 && (
                      <div className="meta">
                        One pattern: {finding.pattern!.chain_count} overlapping chains · {finding.pattern!.transaction_count} transactions
                      </div>
                    )}
                    {(finding.matched_windows?.length ?? 0) > 1 && (
                      <div className="meta">One finding for {finding.matched_windows!.length} matching windows of the same day</div>
                    )}
                    <div className="meta">
                      {finding.case_id} · {relativeTime(finding.window_end)}
                      {finding.benign_alternatives[0] ? ` · benign alternative: ${finding.benign_alternatives[0]}` : ""}
                    </div>
                    <div className="rank-bar-track">
                      <div
                        className="rank-bar-fill"
                        style={{ width: `${finding.family_rank ? Math.max(6, 100 - ((finding.family_rank - 1) / Math.max((finding.family_total ?? 1) - 1, 1)) * 100) : 6}%` }}
                      />
                    </div>
                    <div className="meta" style={{ marginTop: 4 }}>{finding.family_rank ? `family rank #${finding.family_rank} of ${finding.family_total}` : "unranked"}</div>
                  </div>
                </NeoCard>
              );
            })
          )}
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <NeoCard variant="neo-sm">
            <h2>Model Status</h2>
            <div className="kv-row"><span className="k">Methods present</span><span>{response?.methods.join(", ") ?? "—"}</span></div>
            <div className="kv-row"><span className="k">ML enabled</span><span>{response ? String(response.ml_enabled) : "—"}</span></div>
            {mlInfo && (
              <>
                <div className="kv-row"><span className="k">Release</span><span className="mono-id">{mlInfo.releaseId}</span></div>
                <div className="kv-row"><span className="k">Model run</span><span className="mono-id">{mlInfo.modelRunId}</span></div>
                <div className="kv-row"><span className="k">Layers</span><span>{mlInfo.layers.join(" + ") || "—"}</span></div>
              </>
            )}
            <p className="coverage-note" style={{ marginTop: 8 }}>
              {response?.ml_enabled
                ? "Inspect the recorded scorer/version and case eligibility. Triage priority only — never a verdict."
                : "No ML-scored finding in this case yet — either too few transactions, or none cleared the review budget."}
            </p>
          </NeoCard>
          <NeoCard variant="neo-sm">
            <h2>Detector Thresholds</h2>
            {Object.keys(thresholds).length === 0 ? (
              <p className="coverage-note">Loading from finding feature vectors…</p>
            ) : (
              Object.entries(thresholds).map(([key, value]) => (
                <div className="kv-row" key={key}>
                  <span className="k">{key.replace(/_/g, " ")}</span>
                  <span>{value}</span>
                </div>
              ))
            )}
            <p className="coverage-note" style={{ marginTop: 8 }}>
              Read live from each rule's <span className="mono-id">feature_vector.rule_thresholds</span> — not hardcoded.
            </p>
          </NeoCard>
        </div>
      </div>
    </Shell>
  );
}

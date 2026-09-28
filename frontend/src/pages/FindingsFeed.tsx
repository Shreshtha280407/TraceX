import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, Badge, FilterPills, relativeTime } from "../components/primitives";
import { api, type Finding, type FindingsListResponse } from "../lib/api";
import "./FindingsFeed.css";

type FilterId = "all" | "deterministic" | "ml" | "reviewed";

function priorityOf(rank: number | null, total: number): { label: string; tone: "danger" | "warning" | "muted" } {
  if (rank === null || total === 0) return { label: "LOW PRIORITY", tone: "muted" };
  const percentile = rank / total;
  if (percentile <= 0.33) return { label: "HIGH PRIORITY", tone: "danger" };
  if (percentile <= 0.66) return { label: "MEDIUM PRIORITY", tone: "warning" };
  return { label: "LOW PRIORITY", tone: "muted" };
}

export function FindingsFeed() {
  const { caseId } = useParams<{ caseId: string }>();
  const navigate = useNavigate();
  const [response, setResponse] = useState<FindingsListResponse | null>(null);
  const [filter, setFilter] = useState<FilterId>("all");
  const [thresholds, setThresholds] = useState<Record<string, number>>({});

  useEffect(() => {
    if (!caseId) return;
    api.listFindings(caseId, 200, 0).then(setResponse);
  }, [caseId]);

  useEffect(() => {
    if (!response) return;
    const uniqueByRule = new Map<string, Finding>();
    for (const finding of response.findings) if (!uniqueByRule.has(finding.rule_id)) uniqueByRule.set(finding.rule_id, finding);
    Promise.all([...uniqueByRule.values()].map((f) => api.getFindingEvidence(f.finding_id).catch(() => null))).then((results) => {
      const merged: Record<string, number> = {};
      for (const result of results) {
        const raw = (result?.feature_vector as Record<string, unknown> | undefined)?.rule_thresholds;
        if (raw && typeof raw === "object") Object.assign(merged, raw as Record<string, number>);
      }
      setThresholds(merged);
    });
  }, [response]);

  const findings = response?.findings ?? [];
  const total = findings.length;

  const filtered = useMemo(() => {
    switch (filter) {
      case "deterministic":
        return findings.filter((f) => f.status === "open");
      case "reviewed":
        return findings.filter((f) => f.status !== "open");
      default:
        return findings;
    }
  }, [findings, filter]);

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Findings Feed</h1>
          <p className="subtitle">Deterministic motifs — rule-based priority is the baseline, always available</p>
        </div>
      </div>

      <div className="two-col">
        <div>
          <div style={{ marginBottom: 16 }}>
            <FilterPills
              active={filter}
              onChange={setFilter}
              options={[
                { id: "all", label: "All" },
                { id: "deterministic", label: "Deterministic" },
                { id: "ml", label: "ML-Flagged", disabled: true, tooltip: "Phase 5 not started" },
                { id: "reviewed", label: "Reviewed" },
              ]}
            />
          </div>
          {response === null ? (
            <p className="coverage-note">Loading…</p>
          ) : filtered.length === 0 ? (
            <p className="coverage-note">No findings match this filter.</p>
          ) : (
            filtered.map((finding) => {
              const priority = priorityOf(finding.rank, total);
              return (
                <NeoCard key={finding.finding_id} variant="neo-sm" className="finding-card">
                  <div onClick={() => navigate(`/findings/${finding.finding_id}`)}>
                    <div className="finding-card-top">
                      <div className="finding-card-left">
                        <strong>{finding.finding_type.replace(/_/g, " ")}</strong>
                        <Badge tone="deterministic">deterministic</Badge>
                      </div>
                      <Badge tone={priority.tone}>{priority.label}</Badge>
                    </div>
                    <span className="mono-id addr">{finding.entity_ref}</span>
                    <div className="meta">
                      {finding.case_id} · {relativeTime(finding.window_end)}
                      {finding.benign_alternatives[0] ? ` · benign alternative: ${finding.benign_alternatives[0]}` : ""}
                    </div>
                    <div className="rank-bar-track">
                      <div
                        className="rank-bar-fill"
                        style={{ width: `${finding.rank ? Math.max(6, 100 - ((finding.rank - 1) / Math.max(total - 1, 1)) * 100) : 6}%` }}
                      />
                    </div>
                    <div className="meta" style={{ marginTop: 4 }}>{finding.rank ? `rank #${finding.rank} of ${total}` : "unranked"}</div>
                  </div>
                </NeoCard>
              );
            })
          )}
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <NeoCard variant="neo-sm">
            <h2>Model Status</h2>
            <div className="kv-row"><span className="k">Method</span><span>{response?.method ?? "deterministic-v1"}</span></div>
            <div className="kv-row"><span className="k">ML</span><span>Not started (Phase 5)</span></div>
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

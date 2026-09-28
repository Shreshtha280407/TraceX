import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard, Badge, ErrorBanner } from "../components/primitives";
import { api, ApiError, type CaseDetail, type EvidenceSourceRow } from "../lib/api";

export function CaseSettings() {
  const { caseId } = useParams<{ caseId: string }>();
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [sources, setSources] = useState<EvidenceSourceRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [inviteName, setInviteName] = useState("");
  const [inviteRole, setInviteRole] = useState<"analyst" | "reviewer" | "case_lead">("analyst");
  const [inviting, setInviting] = useState(false);

  function load() {
    if (!caseId) return;
    api.getCase(caseId).then(setDetail).catch((err) => setError(err instanceof ApiError ? String(err.detail) : "Could not load case."));
    api.listSources(caseId).then((r) => setSources(r.sources));
  }
  useEffect(load, [caseId]);

  const isLead = detail?.members.some((m) => m.role === "case_lead");

  async function invite() {
    if (!caseId || !inviteName.trim()) return;
    setInviting(true);
    try {
      await api.addMember(caseId, inviteName.trim(), inviteRole);
      setInviteName("");
      load();
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : "Could not add member.");
    } finally {
      setInviting(false);
    }
  }

  if (!detail) return <Shell>{error ? <ErrorBanner>{error}</ErrorBanner> : <p className="coverage-note">Loading…</p>}</Shell>;

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Case Settings</h1>
          <p className="subtitle">{detail.case_id} · {detail.name}</p>
        </div>
        {isLead && <Badge tone="ml">CASE LEAD ACCESS</Badge>}
      </div>

      {error && <ErrorBanner>{error}</ErrorBanner>}

      <div className="two-col">
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <NeoCard>
            <h2>Case Metadata</h2>
            <div className="kv-row"><span className="k">Case name</span><span>{detail.name}</span></div>
            <div className="kv-row"><span className="k">PS reference</span><span>SIH PS 26146</span></div>
            <div className="kv-row"><span className="k">Created</span><span>{detail.created_at?.slice(0, 10) ?? "—"}</span></div>
            <div className="kv-row"><span className="k">Retention window</span><span>180 days from snapshot</span></div>
            <p className="coverage-note" style={{ marginTop: 8 }}>
              PS reference and retention window are static per-deployment configuration, not per-case backend fields yet.
            </p>
          </NeoCard>

          <NeoCard>
            <h2>Access &amp; Roles</h2>
            <table className="data-table">
              <thead><tr><th>Actor</th><th>Role</th></tr></thead>
              <tbody>
                {detail.members.map((member) => (
                  <tr key={member.actor}>
                    <td>{member.actor}</td>
                    <td><Badge tone="muted">{member.role}</Badge></td>
                  </tr>
                ))}
              </tbody>
            </table>
            {isLead && (
              <div style={{ marginTop: 14, display: "flex", gap: 8, alignItems: "flex-end" }}>
                <div className="form-field" style={{ marginBottom: 0, flex: 1 }}>
                  <label>Invite by name</label>
                  <input value={inviteName} onChange={(e) => setInviteName(e.target.value)} placeholder="display name" />
                </div>
                <select value={inviteRole} onChange={(e) => setInviteRole(e.target.value as typeof inviteRole)} style={{ padding: 10 }}>
                  <option value="analyst">analyst</option>
                  <option value="reviewer">reviewer</option>
                  <option value="case_lead">case_lead</option>
                </select>
                <button type="button" className="btn-mustard" disabled={inviting || !inviteName.trim()} onClick={invite}>
                  + Invite
                </button>
              </div>
            )}
          </NeoCard>

          <NeoCard className="danger-zone">
            <h2>Danger Zone</h2>
            <p className="coverage-note">Archiving freezes ingestion and review; evidence and audit history remain intact and exportable.</p>
            <button type="button" className="btn-ghost" disabled data-tooltip="Not implemented yet">
              Archive this case
            </button>
          </NeoCard>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <NeoCard variant="neo-sm">
            <h2>Data Sources</h2>
            {sources === null ? (
              <p className="coverage-note">Loading…</p>
            ) : sources.length === 0 ? (
              <p className="coverage-note">No sources uploaded yet.</p>
            ) : (
              sources.map((source) => (
                <div className="kv-row" key={source.source_id}>
                  <span className="k">{source.filename}</span>
                  <Badge tone="success">verified</Badge>
                </div>
              ))
            )}
            <p className="coverage-note" style={{ marginTop: 8 }}>SHA-256 hashes recorded in EvidenceSource.</p>
          </NeoCard>
          <NeoCard variant="neo-sm">
            <h2>Policy</h2>
            <p className="coverage-note">Coverage limits are always shown, never hidden — missing outpoints render as partial coverage, not silent gaps.</p>
            <p className="coverage-note" style={{ marginTop: 8 }}>No cross-case view. Switching cases re-scopes every list, panel and permission check.</p>
            <p className="coverage-note" style={{ marginTop: 8 }}>Reviewer decisions are never auto-applied — every merge/verify/reject is a logged, explicit action.</p>
          </NeoCard>
        </div>
      </div>
    </Shell>
  );
}

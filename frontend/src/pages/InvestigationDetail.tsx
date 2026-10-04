import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { NeoCard } from "../components/primitives";
import { References } from "../components/StructuredEvidence";
import { api, type InvestigationDetail as Detail, type Finding } from "../lib/api";

export function InvestigationDetail() {
  const { groupId } = useParams<{groupId: string}>();
  const [detail, setDetail] = useState<Detail | null>(null);
  const [members, setMembers] = useState<Finding[]>([]);
  const [offset, setOffset] = useState(0);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [history, setHistory] = useState<Awaited<ReturnType<typeof api.getInvestigationReviews>>["items"]>([]);
  const [replacements, setReplacements] = useState<Awaited<ReturnType<typeof api.getInvestigationReplacements>>["items"]>([]);
  const [decision, setDecision] = useState("triaged");
  const [reason, setReason] = useState("");
  const [opposingSource, setOpposingSource] = useState("");
  const [opposingLocator, setOpposingLocator] = useState("");
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (!groupId) return;
    let active = true;
    Promise.all([api.getInvestigation(groupId), api.getInvestigationMembers(groupId, offset), api.getInvestigationReviews(groupId, historyOffset), api.getInvestigationReplacements(groupId)]).then(([d, m, h, r]) => {
      if (active) { setDetail(d); setMembers(m.items.map(x => x.finding)); setHistory(h.items); setReplacements(r.items); setError(""); }
    }).catch(e => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, [groupId, offset, historyOffset, revision]);
  async function save() {
    if (!groupId || !detail || !reason.trim() || saving) return;
    setSaving(true);
    try {
      if (Boolean(opposingSource.trim()) !== Boolean(opposingLocator.trim())) throw new Error("Provide both the opposing source ID and exact locator, or leave both blank.");
      const refs = opposingSource.trim() ? [{evidence_id: opposingSource.trim(), locator: opposingLocator.trim()}] : [];
      await api.reviewInvestigation(groupId, detail.review_version, decision, reason.trim(), refs);
      setRevision(x => x + 1); setReason(""); setOpposingSource(""); setOpposingLocator("");
    }
    catch (e) { setError(String(e)); } finally { setSaving(false); }
  }
  return <Shell><h1>Investigation group</h1>{error && <p role="alert">{error}</p>}
    {detail && <>
      <Link to={`/cases/${detail.case_id}/findings`}>Back to review queue</Link>
      <NeoCard><h2>Review proposition</h2><p>{detail.proposition}</p>
        <p>{detail.member_count} member findings · {detail.transaction_count} enumerated transactions · {detail.entity_count} focal entities · {detail.status}</p>
        <p>{detail.window_start} — {detail.window_end} · focal reference {detail.focal_ref}</p>
        <p className="coverage-note">{detail.grouping_version} · {detail.procedure_sha256}<br />{detail.group_decision_scope}</p>
        {!detail.active_generation && <p role="status">Historical immutable generation; current replacements are linked below. Its decision has not been inherited.</p>}
        <details><summary>Grouping rationale and limits</summary><pre>{JSON.stringify(detail.rationale, null, 2)}</pre></details>
        <p>Individual dispositions: {Object.entries(detail.member_decisions).map(([s,n]) => `${s}: ${n}`).join(" · ")}</p>
        {detail.mixed_member_decisions && <p role="status">Mixed individual decisions. The group decision does not override them.</p>}
      </NeoCard>
      <NeoCard><h2>Supporting and opposing evidence</h2>
        <p>{detail.evidence_scope}</p><Link to={`/findings/${detail.representative_finding_id}`}>Open representative evidence, features, confidence and source replay</Link>
        <h3>Supporting observations — representative</h3>
        {detail.representative_evidence.supporting_observations.map((s,i) => <p key={i}>{s.statement}</p>)}
        <References refs={detail.representative_evidence.supporting_refs} label="Reopen group supporting record" />
        <h3>Observed counter-evidence — representative</h3><p>{detail.representative_evidence.counter_evidence_summary}</p>
        {detail.representative_evidence.observed_counter_evidence.map((s,i) => <div key={i}><p>{s.statement}</p><p>{s.basis}</p>
          <References refs={s.source_refs} label="Reopen group opposing record" /></div>)}
        <h3>Plausible benign alternatives — not observed contradictions</h3>
        {detail.representative_evidence.benign_alternatives.map((b,i) => <p key={i}>{b.statement}</p>)}
        <h3>Missing coverage — representative</h3>{detail.representative_evidence.missing_evidence.map((s,i) => <p key={i}>{s}</p>)}
        <h3>Member evidence</h3>{members.map(f => <p key={f.finding_id}><Link to={`/findings/${f.finding_id}`}>{f.claim}</Link> · {f.rule_id}/{f.rule_version} · {f.status}</p>)}
        <button className="btn-ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0,offset-20))}>Previous members page</button>{" "}
        <button className="btn-ghost" disabled={offset+20 >= detail.member_count} onClick={() => setOffset(offset+20)}>Next members page</button>
      </NeoCard>
      <NeoCard><h2>Group proposition decision</h2><p className="coverage-note">No decision is propagated to member findings, transactions, ownership or training labels. Triaged, confirmed or dismissed frees queue capacity; escalated and needs-data remain unresolved.</p>
        <select className="inline-control" value={decision} onChange={e => setDecision(e.target.value)}>{["triaged", "confirmed", "dismissed", "escalated", "needs_data_review", "open"].map(s => <option key={s}>{s}</option>)}</select>{" "}
        <label>Recorded reason <textarea className="inline-control" maxLength={2000} value={reason} onChange={e => setReason(e.target.value)} /></label>{" "}
        <p>Optional opposing reference: cite an actual checked source record and explain its relevance in the reason. A citation is not automatically verified innocence or a detector contradiction.</p>
        <label>Opposing source ID <input className="inline-control" value={opposingSource} onChange={e => setOpposingSource(e.target.value)} /></label>{" "}
        <label>Exact opposing locator <input className="inline-control" value={opposingLocator} onChange={e => setOpposingLocator(e.target.value)} /></label>{" "}
        <button className="btn-ghost" disabled={saving || !reason.trim()} onClick={save}>Record group decision</button>
        {history.map(r => <div key={r.review_id}><p>Decision {r.review_version}: {r.disposition} — {r.reason}</p>
          {!!r.counterevidence_refs.length && <><p>Reviewer-cited opposing references — interpretation is recorded in the reason, not a detector verdict.</p>
            <References refs={r.counterevidence_refs} label="Reopen group review reference" /></>}</div>)}
        <button className="btn-ghost" disabled={historyOffset===0} onClick={() => setHistoryOffset(Math.max(0,historyOffset-20))}>Previous decisions</button>{" "}
        <button className="btn-ghost" disabled={history.length<20} onClick={() => setHistoryOffset(historyOffset+20)}>Next decisions</button>
        {replacements.map(r => <p key={`${r.prior_id}/${r.replacement_id}`}><Link to={`/investigations/${r.prior_id===groupId?r.replacement_id:r.prior_id}`}>Related immutable generation</Link> — {r.reason}</p>)}
      </NeoCard>
    </>}
  </Shell>;
}

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useInvestigationQueue } from "../lib/useInvestigationQueue";
import { NeoCard } from "./primitives";

export function InvestigationQueue({ caseId }: { caseId: string }) {
  const [capacity, setCapacity] = useState(100);
  const [offset, setOffset] = useState(0);
  const [scope, setScope] = useState("queue");
  const [status, setStatus] = useState("");
  const { queue, error } = useInvestigationQueue(caseId, capacity, offset, scope, status);
  const navigate = useNavigate();
  return <NeoCard>
    <h2>Investigation review queue</h2>
    <label>Unresolved group capacity <input className="inline-control" type="number" min={0} max={10000} value={capacity}
      onChange={e => { setCapacity(Math.max(0, Math.min(10000, Math.floor(Number(e.target.value) || 0)))); setOffset(0); }} /></label>{" "}
    <label>View <select className="inline-control" value={scope} onChange={e => { setScope(e.target.value); setOffset(0); }}>
      <option value="queue">Prioritized queue</option><option value="backlog">Additional unresolved groups</option>
      <option value="all">All current groups</option><option value="history">Including prior generations</option>
    </select></label>{" "}
    <label>Group decision <select className="inline-control" value={status} onChange={e => { setStatus(e.target.value); setOffset(0); }}>
      <option value="">All decisions</option>{["open", "escalated", "needs_data_review", "triaged", "confirmed", "dismissed"].map(s => <option key={s}>{s}</option>)}
    </select></label>
    <p className="coverage-note">One task reviews one stated pattern episode, not every member transaction. Queue priority is not a probability. Individual finding history is retained below.</p>
    {error && <p role="alert">Group queue unavailable: {error}. Check that the local API and worker run the current grouped-review release; inspect investigation_grouping. Individual alert counts are not substituted for review tasks.</p>}
    {queue && <>
      <p data-testid="review-workload">{queue.queued_groups.toLocaleString()} groups in your current review queue · {queue.backlog_groups.toLocaleString()} additional unresolved groups</p>
      <p data-testid="group-counts">{queue.underlying_findings.toLocaleString()} underlying findings / {queue.investigation_groups.toLocaleString()} investigation groups / {queue.unresolved_groups.toLocaleString()} unresolved / {queue.queued_groups.toLocaleString()} in your review queue / {queue.backlog_groups.toLocaleString()} additional unresolved groups</p>
      {queue.grouping_coverage?.state === "incomplete" && <p role="alert">Grouping incomplete: {queue.grouping_coverage.ungrouped_findings.toLocaleString()} underlying observations are not grouped yet. These are not extra independent review tasks. {queue.grouping_coverage.reason}</p>}
      {!queue.items.length && <p className="coverage-note">No published groups in this view. This does not certify analysis completion or an empty backlog.</p>}
      {queue.items.map(g => <p key={g.group_id}><button className="btn-ghost" type="button" onClick={() => navigate(`/investigations/${g.group_id}`)}>
        {g.family} · {g.member_count} findings · {g.status}</button><br />{g.window_start} — {g.window_end}</p>)}
      <button className="btn-ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>Previous groups page</button>{" "}
      <button className="btn-ghost" disabled={offset + 20 >= queue.filtered_total} onClick={() => setOffset(offset + 20)}>Next groups page</button>
    </>}
  </NeoCard>;
}

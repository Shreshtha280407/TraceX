import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type InvestigationQueue as Queue } from "../lib/api";
import { NeoCard } from "./primitives";

export function InvestigationQueue({ caseId }: { caseId: string }) {
  const [capacity, setCapacity] = useState(100);
  const [offset, setOffset] = useState(0);
  const [scope, setScope] = useState("queue");
  const [status, setStatus] = useState("");
  const [queue, setQueue] = useState<Queue | null>(null);
  const [error, setError] = useState("");
  const navigate = useNavigate();
  useEffect(() => {
    let active = true;
    api.getInvestigationQueue(caseId, capacity, offset, scope, status).then(result => {
      if (active) { setQueue(result); setError(""); }
    }).catch(e => { if (active) { setQueue(null); setError(String(e)); } });
    return () => { active = false; };
  }, [caseId, capacity, offset, scope, status]);
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
    {error && <p role="alert">Group queue unavailable: {error}. Inspect the investigation_grouping stage.</p>}
    {queue && <>
      <p data-testid="group-counts">{queue.underlying_findings.toLocaleString()} underlying findings / {queue.investigation_groups.toLocaleString()} investigation groups / {queue.unresolved_groups.toLocaleString()} unresolved / {queue.queued_groups.toLocaleString()} in your review queue / {queue.backlog_groups.toLocaleString()} additional unresolved groups</p>
      {!queue.items.length && <p className="coverage-note">No groups in this view. Groups awaiting materialization are not counted as successfully analyzed.</p>}
      {queue.items.map(g => <p key={g.group_id}><button className="btn-ghost" type="button" onClick={() => navigate(`/investigations/${g.group_id}`)}>
        {g.family} · {g.member_count} findings · {g.status}</button><br />{g.window_start} — {g.window_end}</p>)}
      <button className="btn-ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>Previous groups page</button>{" "}
      <button className="btn-ghost" disabled={offset + 20 >= queue.filtered_total} onClick={() => setOffset(offset + 20)}>Next groups page</button>
    </>}
  </NeoCard>;
}

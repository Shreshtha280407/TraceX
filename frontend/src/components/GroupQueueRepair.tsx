import { useState } from "react";
import { api, type InvestigationQueue } from "../lib/api";
import { trackJob } from "../lib/jobRegistry";

/** Explicit, case-authorized recovery, never a write triggered by a queue GET. */
export function GroupQueueRepair({caseId, queue, onRefresh}: {
  caseId: string; queue: InvestigationQueue | null; onRefresh: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [requested, setRequested] = useState(false);
  if (queue?.grouping_coverage?.state !== "incomplete") return null;
  const active = (queue.grouping_coverage.active_jobs ?? 0) > 0;
  async function build() {
    setBusy(true);
    setError("");
    try {
      const result = await api.buildInvestigationQueue(caseId);
      result.jobs.forEach(job => trackJob(caseId, job.job_id));
      setRequested(result.state === "queued");
      onRefresh();
    } catch (err) { setError(String(err)); }
    finally { setBusy(false); }
  }
  return <div>
    {active ? <p className="coverage-note" role="status">Analysis is running. Pending group counts will update when review groups are published.</p> : <>
      <p className="coverage-note">This older or incomplete snapshot has findings without review groups. Generate the actual groups from its saved evidence; do not count every transaction finding as a review.</p>
      <button className="btn-ghost" type="button" disabled={busy} onClick={() => void build()}>
        {busy ? "Queuing review groups…" : "Generate review groups"}
      </button>
      <p className="coverage-note">Group-only recovery does not reimport transactions or rerun ML. Existing evidence and reviews are retained.</p>
    </>}
    {requested && !error && <p className="coverage-note" role="status">Group generation requested. If it fails, check the worker and Evidence Intake before retrying.</p>}
    {error && <p role="alert">Could not generate review groups: {error}</p>}
  </div>;
}

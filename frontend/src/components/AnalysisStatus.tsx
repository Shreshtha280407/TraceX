import { useEffect, useState } from "react";
import { api, ApiError, type ImportJob } from "../lib/api";
import { NoticeBanner } from "./primitives";

export function AnalysisStatus({ caseId }: { caseId: string }) {
  return <CaseAnalysis key={caseId} caseId={caseId} />;
}

function CaseAnalysis({ caseId }: { caseId: string }) {
  const [analysis, setAnalysis] = useState<ImportJob["analysis"]>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    async function refresh() {
      try { const result = await api.getAnalysis(caseId); if (active) { setAnalysis(result.analysis); setError(null); } }
      catch (err) { if (active && !(err instanceof ApiError && err.status === 404)) setError("Analysis status is unavailable; do not assume completion."); }
    }
    void refresh();
    const interval = setInterval(() => void refresh(), 5000);
    return () => { active = false; clearInterval(interval); };
  }, [caseId]);
  if (error) return <NoticeBanner>{error}</NoticeBanner>;
  if (!analysis || analysis.state === "complete") return null;
  const issues = analysis.stages.filter((s) => !["complete", "written", "no_rows_flagged"].includes(s.status));
  return <NoticeBanner>
    Analysis {analysis.state}. Committed evidence remains readable; downstream results may be incomplete.
    {issues.map((s) => <div key={s.name}>{s.name.replaceAll("_", " ")}: {s.status}{s.reason ? ` — ${s.reason}` : ""}</div>)}
    {analysis.retry_supported && <div>Retry failed analysis from Evidence Intake.</div>}
  </NoticeBanner>;
}

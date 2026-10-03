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
  if (!analysis) return null;
  const ml = analysis.stages.find((s) => s.name === "ml_scoring");
  const issues = analysis.stages.filter((s) => !["complete", "written", "no_rows_flagged"].includes(s.status));
  return <NoticeBanner>
    Analysis {analysis.state}. {analysis.state === "complete" ? "All recorded mandatory stages completed; coverage limitations still apply." : "Committed evidence remains readable; downstream results may be incomplete."}
    <div>Active scorer: {String(ml?.details?.release_id ?? "not available")}. Uploads without independent applicable labels have no measured AP or accuracy.</div>
    {ml?.details?.eligibility_decision != null && <details><summary>Model eligibility / fallback decision</summary><pre>{JSON.stringify(ml.details.eligibility_decision, null, 2)}</pre></details>}
    {issues.map((s) => <div key={s.name}>{s.name.replaceAll("_", " ")}: {s.status}{s.reason ? ` — ${s.reason}` : ""}</div>)}
    {analysis.retry_supported && <div>Retry failed analysis from Evidence Intake.</div>}
  </NoticeBanner>;
}

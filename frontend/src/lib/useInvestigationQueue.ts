import { useEffect, useState } from "react";
import { api, type InvestigationQueue } from "./api";
import { useAuth } from "./auth";
import { REVIEW_COUNT_EVENTS } from "./investigationCounts";
import { streamCaseEvents } from "./sse";

/** Refresh durable counts after terminal import/review events and tab focus.
 * Debounce historical SSE replay; neither polling nor fetching a whole case is needed.
 */
export function useInvestigationQueue(caseId: string | undefined, capacity = 100, offset = 0, scope = "queue", status = "") {
  const { token } = useAuth();
  const [revision, setRevision] = useState(0);
  const key = JSON.stringify([caseId, capacity, offset, scope, status]);
  const [result, setResult] = useState<{key: string; queue: InvestigationQueue | null; error: string}>({key: "", queue: null, error: ""});
  useEffect(() => {
    if (!caseId) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = () => { clearTimeout(timer); timer = setTimeout(() => setRevision(n => n + 1), 200); };
    const stop = streamCaseEvents(caseId, token, event => { if (REVIEW_COUNT_EVENTS.has(event.event)) refresh(); });
    window.addEventListener("focus", refresh);
    return () => { stop(); clearTimeout(timer); window.removeEventListener("focus", refresh); };
  }, [caseId, token]);
  useEffect(() => {
    if (!caseId) return;
    let active = true;
    api.getInvestigationQueue(caseId, capacity, offset, scope, status).then(queue => {
      if (active) setResult({key, queue, error: ""});
    }).catch(err => { if (active) setResult({key, queue: null, error: String(err)}); });
    return () => { active = false; };
  }, [caseId, capacity, offset, scope, status, revision, key]);
  // Do not show a previous case/page's counts while its replacement is loading.
  return result.key === key ? result : {queue: null, error: ""};
}

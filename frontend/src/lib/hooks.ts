import { useEffect, useState } from "react";
import { api, type Finding, type ImportJob } from "./api";
import { jobsForCase } from "./jobRegistry";

export function useFindings(caseId: string | null | undefined) {
  const [findings, setFindings] = useState<Finding[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    if (!caseId) return;
    let cancelled = false;
    setFindings(null);
    api
      .listFindings(caseId, 200, 0)
      .then((result) => !cancelled && setFindings(result.findings))
      .catch((err) => !cancelled && setError(err));
    return () => {
      cancelled = true;
    };
  }, [caseId]);

  return { findings, error };
}

const ACTIVE_JOB_STATES = new Set(["queued", "running", "checkpointed"]);

export function useTrackedJobs(caseId: string | null | undefined) {
  const [jobs, setJobs] = useState<ImportJob[]>([]);

  useEffect(() => {
    if (!caseId) return;
    let cancelled = false;
    const ids = jobsForCase(caseId);
    if (ids.length === 0) {
      setJobs([]);
      return;
    }

    async function poll() {
      const results = await Promise.all(ids.map((id) => api.getJob(id).catch(() => null)));
      if (cancelled) return;
      const resolved = results.filter((job): job is ImportJob => job !== null);
      setJobs(resolved);
      return resolved.some((job) => ACTIVE_JOB_STATES.has(job.state));
    }

    let timer: ReturnType<typeof setTimeout> | undefined;
    async function loop() {
      const stillActive = await poll();
      if (!cancelled && stillActive) timer = setTimeout(loop, 4000);
    }
    loop();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [caseId]);

  return jobs;
}

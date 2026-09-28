/** Tracks import job IDs this browser has actually created per case, so Dashboard/Overview
 * stat tiles can poll GET /v1/jobs/{id} for real numbers instead of fabricating an aggregate
 * endpoint that doesn't exist on the backend. */
const KEY = "tracex.jobRegistry";

type Registry = Record<string, string[]>;

function read(): Registry {
  try {
    return JSON.parse(localStorage.getItem(KEY) ?? "{}") as Registry;
  } catch {
    return {};
  }
}

export function trackJob(caseId: string, jobId: string): void {
  const registry = read();
  const existing = registry[caseId] ?? [];
  if (!existing.includes(jobId)) {
    registry[caseId] = [...existing, jobId];
    localStorage.setItem(KEY, JSON.stringify(registry));
  }
}

export function jobsForCase(caseId: string): string[] {
  return read()[caseId] ?? [];
}

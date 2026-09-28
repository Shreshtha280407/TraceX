/** Session-scoped counters derived only from actions this browser tab actually performed —
 * no backend analytics endpoint exists, so anything shown here must come from real activity
 * observed in this session, never a hardcoded number. */
const KEY = "tracex.sessionStats";

type Stats = { findingsReviewed: number; reversedOnAppeal: number; exportsRequested: number };

function read(): Stats {
  try {
    return { findingsReviewed: 0, reversedOnAppeal: 0, exportsRequested: 0, ...JSON.parse(sessionStorage.getItem(KEY) ?? "{}") };
  } catch {
    return { findingsReviewed: 0, reversedOnAppeal: 0, exportsRequested: 0 };
  }
}

function write(stats: Stats): void {
  sessionStorage.setItem(KEY, JSON.stringify(stats));
}

export function recordReview(isReversal: boolean): void {
  const stats = read();
  stats.findingsReviewed += 1;
  if (isReversal) stats.reversedOnAppeal += 1;
  write(stats);
}

export function recordExport(): void {
  const stats = read();
  stats.exportsRequested += 1;
  write(stats);
}

export function getSessionStats(): Stats {
  return read();
}

import { useSyncExternalStore } from "react";

/** Remembers the last case the user visited/selected, so the nav rail's case-scoped links
 * (Evidence Intake, Graph Explorer, …) have somewhere real to go from a non-case-scoped page
 * like Dashboard or Overview, instead of collapsing to that same page. Reactive via
 * useSyncExternalStore so the rail updates the moment a page learns the case ID, not just
 * on next render. */
const KEY = "tracex.lastCaseId";
const listeners = new Set<() => void>();

function getSnapshot(): string | null {
  return localStorage.getItem(KEY);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function setLastCaseId(caseId: string): void {
  if (localStorage.getItem(KEY) === caseId) return;
  localStorage.setItem(KEY, caseId);
  listeners.forEach((listener) => listener());
}

export function useLastCaseId(): string | null {
  return useSyncExternalStore(subscribe, getSnapshot);
}

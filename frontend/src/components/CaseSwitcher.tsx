import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ThemedSelect } from "./ThemedSelect";
import { api, type CaseWithRole } from "../lib/api";

/** Every case-scoped route is /cases/:caseId/<suffix> — capture the suffix so
 * switching cases keeps you on the same kind of page (Graph stays Graph, etc.)
 * instead of bouncing to a fixed default. */
const SUFFIX_PATTERN = /^\/cases\/[^/]+(\/[a-z]+)/;

export function caseScopedSuffix(pathname: string): string {
  const match = pathname.match(SUFFIX_PATTERN);
  return match ? match[1] : "/dashboard";
}

/** Rendered by Shell on every case-scoped page. Without this, the only way to
 * change "which case" was the sidebar rail's silent fallback to whichever case
 * was last visited — a page could end up showing a different case's data than
 * the one you meant, with no visible indication or control. This makes the
 * current case explicit and switchable from anywhere. */
export function CaseSwitcher({ caseId, pathname }: { caseId: string; pathname: string }) {
  const navigate = useNavigate();
  const [cases, setCases] = useState<CaseWithRole[] | null>(null);

  useEffect(() => {
    api.listCases().then((result) => setCases(result.cases));
  }, []);

  const current = cases?.find((c) => c.case_id === caseId);
  const suffix = caseScopedSuffix(pathname);
  const options = current
    ? (cases ?? []).map((c) => ({ value: c.case_id, label: c.name }))
    : [{ value: caseId, label: caseId }, ...(cases ?? []).map((c) => ({ value: c.case_id, label: c.name }))];

  return (
    <div className="case-switcher">
      <span className="case-switcher-label">Case</span>
      <ThemedSelect
        value={caseId}
        ariaLabel="Switch case"
        onChange={(value) => navigate(`/cases/${value}${suffix}`)}
        options={options}
      />
      {current && <span className="badge tone-muted">{current.role.toUpperCase().replace("_", " ")}</span>}
    </div>
  );
}

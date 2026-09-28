import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import { ErrorBanner } from "../components/primitives";

/** Reached right after signup/login when GET /v1/cases comes back empty — a brand-new
 * user has zero case memberships, and POST /members is case-lead-gated, so the only
 * real path forward is creating a case (or waiting to be invited). */
export function Onboarding() {
  const navigate = useNavigate();
  const [checking, setChecking] = useState(true);
  const [name, setName] = useState("");
  const [synthetic, setSynthetic] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .listCases()
      .then((result) => {
        if (result.cases.length > 0) navigate("/dashboard", { replace: true });
        else setChecking(false);
      })
      .catch(() => setChecking(false));
  }, [navigate]);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const created = await api.createCase(name, synthetic);
      navigate(`/cases/${created.case_id}/ingestion`);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : "Could not reach the TraceX backend.");
    } finally {
      setBusy(false);
    }
  }

  if (checking) return null;

  return (
    <div className="onboarding-wrap">
      <form className="neo onboarding-card" onSubmit={onSubmit}>
        <h1 className="display">Create your first case</h1>
        <p className="subtitle">You aren't a member of any case workspace yet.</p>
        {error && <ErrorBanner>{error}</ErrorBanner>}
        <div className="form-field">
          <label htmlFor="case-name">Case name</label>
          <input id="case-name" value={name} onChange={(event) => setName(event.target.value)} required placeholder="PS26146-CASE-004" />
        </div>
        <label className="synthetic-toggle">
          <input type="checkbox" checked={synthetic} onChange={(event) => setSynthetic(event.target.checked)} />
          Synthetic / evaluation case
        </label>
        <button type="submit" className="btn-mustard" disabled={busy || !name.trim()}>
          {busy ? "Creating…" : "Create case"}
        </button>
        <p className="onboarding-footnote">
          Already part of an investigation? Ask that case's lead to add you from Case Settings → Access &amp; Roles.
        </p>
      </form>
    </div>
  );
}

import { useState, type FormEvent } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { ApiError } from "../lib/api";
import { ErrorBanner } from "../components/primitives";
import { CheckIcon } from "../components/icons";
import "./AccessGate.css";

type AccessMode = "login" | "signup";

const FEATURES = [
  "Case-scoped evidence vault — every upload hashed and isolated per case",
  "Bounded UTXO graph explorer, not a wall of raw transactions",
  "Deterministic, reviewable findings — no black-box scores",
  "Unsupervised anomaly ranking layered on top of the rules",
  "Full audit trail and analyst review history on every finding",
  "Offline-capable, with verifiable evidence export",
];

export function AccessGate() {
  const { token, signup, login } = useAuth();
  const [mode, setMode] = useState<AccessMode | null>(null);
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (token) return <Navigate to="/overview" replace />;

  function openAccess(nextMode: AccessMode | null) {
    setError(null);
    setMode(nextMode);
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!mode) return;
    setError(null);
    setBusy(true);
    try {
      if (mode === "signup") await signup(name, password);
      else await login(name, password);
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : "Could not reach the TraceX backend.");
    } finally {
      setBusy(false);
    }
  }

  if (!mode) {
    return (
      <main className="access-gate landing-page">
        <section className="landing-hero" aria-labelledby="landing-title">
          <h1 id="landing-title" className="landing-title" aria-label="TraceX">
            TraceX
          </h1>
          <p className="landing-subtitle">Trace Bitcoin. Preserve the evidence.</p>
          <p className="landing-lede">
            Review case-scoped UTXO evidence, transparent findings, and analyst decisions without turning an observation into a conclusion.
          </p>
          <div className="landing-actions" aria-label="Access TraceX">
            <button type="button" className="btn-mustard landing-primary" onClick={() => openAccess("login")}>
              Sign in
            </button>
            <button type="button" className="landing-secondary" onClick={() => openAccess("signup")}>
              Create workspace
            </button>
          </div>
          <ul className="landing-trust" aria-label="TraceX capabilities">
            <li>Case-scoped</li>
            <li>Evidence-linked</li>
            <li>Offline-capable</li>
          </ul>
          <p className="landing-footnote">Analytical leads are prioritised observations for review, not ownership or wrongdoing claims.</p>
        </section>
      </main>
    );
  }

  return (
    <main className="access-gate auth-stage">
      <div className="auth-layout">
        <aside className="auth-sidebar">
          <p className="auth-sidebar-eyebrow">TRACEX</p>
          <h2 className="auth-sidebar-title">Trace Bitcoin. Preserve the evidence.</h2>
          <ul className="auth-feature-list">
            {FEATURES.map((feature) => (
              <li key={feature}>
                <span className="auth-feature-icon">
                  <CheckIcon />
                </span>
                <span>{feature}</span>
              </li>
            ))}
          </ul>
        </aside>

        <form className="neo auth-card" onSubmit={onSubmit}>
          <button type="button" className="auth-back" onClick={() => openAccess(null)}>
            ← Back to TraceX
          </button>
          <p className="auth-eyebrow">TRACE X / SECURE ACCESS</p>
          <h1 className="display">{mode === "signup" ? "Create your account" : "Welcome back"}</h1>
          <p className="subtitle">
            {mode === "signup" ? "Set up your investigator account, then create your first case workspace." : "Sign in to continue to your case workspace."}
          </p>
          {error && <ErrorBanner>{error}</ErrorBanner>}
          <div className="form-field">
            <label htmlFor="name">Name</label>
            <input id="name" value={name} onChange={(event) => setName(event.target.value)} autoComplete="username" required autoFocus />
          </div>
          <div className="form-field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete={mode === "signup" ? "new-password" : "current-password"}
              minLength={8}
              required
            />
          </div>
          <button type="submit" className="btn-mustard auth-submit" disabled={busy}>
            {busy ? "Please wait…" : mode === "signup" ? "Create account" : "Sign in"}
          </button>
          <button type="button" className="mode-toggle" onClick={() => openAccess(mode === "signup" ? "login" : "signup")}>
            {mode === "signup" ? "Already have an account? Sign in" : "New to TraceX? Create a workspace"}
          </button>
          <div className="trust-badges">
            <span className="badge tone-muted">CASE-SCOPED</span>
            <span className="badge tone-muted">AUDIT-LOGGED</span>
            <span className="badge tone-muted">EVIDENCE-FIRST</span>
          </div>
        </form>
      </div>
    </main>
  );
}

import { useState, type FormEvent } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { ApiError } from "../lib/api";
import { ErrorBanner } from "../components/primitives";
import "./AccessGate.css";

const FEATURES = [
  { title: "Case-isolated workspaces", body: "Every record, key and export is scoped to case_id — no cross-case leakage." },
  { title: "Chain-of-custody evidence", body: "Hash-verified sources, immutable snapshots, append-only audit chain." },
  { title: "Deterministic + ML detection", body: "Explainable rule motifs, Isolation Forest anomaly ranking, benign controls." },
  { title: "Offline-capable deployment", body: "Linux CPU and Mac-native profiles, air-gapped, no external calls." },
];

export function AccessGate() {
  const { token, signup, login } = useAuth();
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (token) return <Navigate to="/dashboard" replace />;

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
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

  return (
    <div className="access-gate">
      <div className="brand-panel">
        <div className="brand-mark">
          <div className="rail-logo">V</div>
          <div>
            <div className="brand-name">TraceX</div>
            <div className="brand-sub">UTXO INTELLIGENCE PLATFORM</div>
          </div>
        </div>
        <h1 className="display">
          Trace the flow.
          <br />
          Prove the network.
        </h1>
        <p className="brand-lede">
          Deterministic and ML-assisted Bitcoin UTXO graph analysis for criminal network investigation — built for
          NCRB, Smart India Hackathon PS 26146.
        </p>
        <ul className="feature-list">
          {FEATURES.map((feature) => (
            <li key={feature.title}>
              <span className="dot tone-warning" />
              <div>
                <div className="feature-title">{feature.title}</div>
                <div className="feature-body">{feature.body}</div>
              </div>
            </li>
          ))}
        </ul>
        <p className="brand-footnote">Synthetic demo case available for evaluation — no production credential required.</p>
      </div>
      <div className="auth-panel">
        <form className="neo auth-card" onSubmit={onSubmit}>
          <h2 className="display">Secure Access</h2>
          <p className="subtitle">Authenticate into a provisioned case workspace</p>
          {error && <ErrorBanner>{error}</ErrorBanner>}
          <div className="form-field">
            <label htmlFor="name">Name</label>
            <input id="name" value={name} onChange={(event) => setName(event.target.value)} autoComplete="username" required />
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
            {busy ? "Please wait…" : mode === "signup" ? "Create Account" : "Access Case Workspace"}
          </button>
          <button type="button" className="mode-toggle" onClick={() => setMode(mode === "signup" ? "login" : "signup")}>
            {mode === "signup" ? "Already have an account? Sign in" : "New to TraceX? Create an account"}
          </button>
          <div className="trust-badges">
            <span className="badge tone-muted">TLS / mTLS</span>
            <span className="badge tone-muted">AUDIT-LOGGED</span>
            <span className="badge tone-muted">CASE-SCOPED RBAC</span>
          </div>
        </form>
      </div>
    </div>
  );
}

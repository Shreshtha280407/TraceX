import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { AnalysisStatus } from "../components/AnalysisStatus";
import { Badge, EmptyState, ErrorBanner, FilterPills, NeoCard, NoticeBanner, StatTile } from "../components/primitives";
import {
  api,
  ApiError,
  type EntitiesResponse,
  type EntityDetail,
  type RiskRunView,
  type RiskSeedRow,
  type SimilarWallets,
} from "../lib/api";

const PAGE = 25;

function btc(sats: number | null | undefined): string {
  if (sats === null || sats === undefined) return "—";
  return `${(sats / 1e8).toLocaleString(undefined, { maximumFractionDigits: 4 })} BTC`;
}

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value > 0 && value < 0.005) return "<1%";
  return `${Math.round(value * 100)}%`;
}

function short(value: string, keep = 10): string {
  return value.length > keep * 2 + 1 ? `${value.slice(0, keep)}…${value.slice(-keep)}` : value;
}

function riskTone(value: number | null | undefined): "danger" | "warning" | "muted" {
  if (!value) return "muted";
  return value >= 0.5 ? "danger" : value >= 0.1 ? "warning" : "muted";
}

type Tab = "entities" | "risk";

/** Entity clusters (common-input-ownership + graph embeddings) and risk
 * propagated from analyst-seeded illicit wallets -- PS focus areas "Entity
 * Clustering" and "Risk Scoring". */
export function EntitiesRisk() {
  const { caseId } = useParams<{ caseId: string }>();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab) ?? "entities";
  const [page, setPage] = useState(0);
  const [entities, setEntities] = useState<EntitiesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(params.get("wallet"));
  const [detail, setDetail] = useState<EntityDetail | null>(null);
  const [similar, setSimilar] = useState<SimilarWallets | null>(null);
  const [lookup, setLookup] = useState("");
  const [seeds, setSeeds] = useState<RiskSeedRow[]>([]);
  const [run, setRun] = useState<RiskRunView | null>(null);
  const [seedForm, setSeedForm] = useState({ wallet_ref: "", reason: "", label: "illicit", weight: 1 });
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!caseId) return;
    setError(null);
    api.listEntities(caseId, PAGE, page * PAGE).then(setEntities).catch((e) => {
      setEntities(null);
      setError(e instanceof ApiError ? String(e.detail) : String(e));
    });
  }, [caseId, page]);

  const refreshRisk = useCallback(() => {
    if (!caseId) return;
    api.getRisk(caseId, 200).then((result) => {
      setSeeds(result.seeds);
      setRun(result.run);
    }).catch(() => undefined);
  }, [caseId]);
  useEffect(refreshRisk, [refreshRisk]);

  useEffect(() => {
    if (!caseId || !selected) {
      setDetail(null);
      setSimilar(null);
      return;
    }
    setDetail(null);
    setSimilar(null);
    api.getEntity(caseId, selected).then(setDetail).catch((e) => setError(e instanceof ApiError ? String(e.detail) : String(e)));
    api.getSimilarWallets(caseId, selected, 8).then(setSimilar).catch(() => setSimilar(null));
  }, [caseId, selected]);

  const select = (wallet: string) => {
    setSelected(wallet);
    const next = new URLSearchParams(params);
    next.set("wallet", wallet);
    setParams(next, { replace: true });
  };

  const submitSeed = async (walletRef?: string) => {
    if (!caseId) return;
    const body = { ...seedForm, wallet_ref: walletRef ?? seedForm.wallet_ref };
    if (!body.wallet_ref || body.reason.trim().length < 3) {
      setError("A seed needs a wallet (address or entity id) and a reason of at least 3 characters.");
      return;
    }
    setBusy(true);
    try {
      await api.addRiskSeed(caseId, body);
      setSeedForm({ ...seedForm, wallet_ref: "", reason: "" });
      refreshRisk();
      if (selected) api.getEntity(caseId, selected).then(setDetail).catch(() => undefined);
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(false);
    }
  };

  const removeSeed = async (seedId: string) => {
    if (!caseId) return;
    setBusy(true);
    try {
      await api.deleteRiskSeed(caseId, seedId);
      refreshRisk();
    } finally {
      setBusy(false);
    }
  };

  if (!caseId) return null;
  const summary = entities?.summary;

  return (
    <Shell>
      <AnalysisStatus caseId={caseId} hideComplete />
      <div className="page-header">
        <div>
          <h1>Entities &amp; Risk</h1>
          <p className="subtitle">
            Wallet clusters proposed by common-input-ownership, behaviour similarity from graph embeddings, and risk
            propagated exposure from analyst-supplied seeds. These are hypotheses and measurements, not common identity, ownership or criminality verdicts.
          </p>
        </div>
        <FilterPills<Tab>
          options={[{ id: "entities", label: "Entity clusters" }, { id: "risk", label: "Risk propagation" }]}
          active={tab}
          onChange={(value) => {
            const next = new URLSearchParams(params);
            next.set("tab", value);
            setParams(next, { replace: true });
          }}
        />
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}

      {summary && (
        <div className="stat-grid">
          <StatTile label="Entity clusters" value={summary.entity_count?.toLocaleString() ?? "—"} sub={`${summary.clustered_addresses?.toLocaleString() ?? 0} addresses clustered`} />
          <StatTile label="Wallets" value={summary.wallet_count?.toLocaleString() ?? "—"} sub="clusters + unclustered addresses" />
          <StatTile label="Largest cluster" value={summary.largest_entity_addresses?.toLocaleString() ?? "—"} sub="addresses" />
          <StatTile label="CoinJoin-shaped skipped" value={summary.mixing_excluded_transactions?.toLocaleString() ?? "—"} sub={`of ${summary.multi_input_transactions?.toLocaleString() ?? 0} multi-input txs`} />
          <StatTile label="Embedded wallets" value={summary.embedded_wallets?.toLocaleString() ?? "—"} sub="spectral graph embedding" />
          <StatTile label="Wallets at risk" value={run?.summary.wallets_with_risk?.toLocaleString() ?? "—"} sub={run ? `${run.summary.wallets_above_0_5 ?? 0} at ≥ 50%` : "add a seed to propagate"} />
        </div>
      )}

      {tab === "entities" ? (
        <div className="two-col">
          <NeoCard>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
              <h2>Entity clusters</h2>
              <form
                style={{ display: "flex", gap: 8 }}
                onSubmit={(e) => {
                  e.preventDefault();
                  if (lookup.trim()) select(lookup.trim());
                }}
              >
                <input
                  className="mono"
                  style={{ background: "var(--surface-3)", border: "1px solid var(--line)", borderRadius: 10, padding: "8px 10px", minWidth: 260 }}
                  placeholder="Look up an address or E-… id"
                  value={lookup}
                  onChange={(e) => setLookup(e.target.value)}
                />
                <button className="btn-ghost" type="submit">Open</button>
              </form>
            </div>
            {entities === null ? (
              <p className="coverage-note">{error ? "Entity analytics are not available yet." : "Loading…"}</p>
            ) : entities.entities.length === 0 ? (
              <EmptyState title="No multi-address entities" body="No two addresses were ever spent together outside CoinJoin-shaped transactions." />
            ) : (
              <>
                <table className="data-table">
                  <thead>
                    <tr><th>Entity</th><th>Addresses</th><th>Linking txs</th><th>Received</th><th>Sent</th><th>Active</th><th>Risk</th></tr>
                  </thead>
                  <tbody>
                    {entities.entities.map((entity) => (
                      <tr
                        key={entity.entity_id}
                        className={`clickable ${selected === entity.entity_id ? "selected" : ""}`}
                        onClick={() => select(entity.entity_id)}
                      >
                        <td className="mono-id">{entity.entity_id}</td>
                        <td>{entity.address_count.toLocaleString()}</td>
                        <td>{entity.linking_tx_count.toLocaleString()}</td>
                        <td>{btc(entity.received_sats)}</td>
                        <td>{btc(entity.sent_sats)}</td>
                        <td className="coverage-note">
                          {entity.first_seen ? new Date(entity.first_seen).toLocaleDateString() : "—"} – {entity.last_seen ? new Date(entity.last_seen).toLocaleDateString() : "—"}
                        </td>
                        <td>{entity.risk ? <Badge tone={riskTone(entity.risk.risk)}>{pct(entity.risk.risk)}</Badge> : <span className="coverage-note">—</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 12 }}>
                  <span className="coverage-note">
                    {page * PAGE + 1}–{Math.min((page + 1) * PAGE, entities.total)} of {entities.total.toLocaleString()} clusters (largest first)
                  </span>
                  <div style={{ display: "flex", gap: 8 }}>
                    <button className="btn-ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
                    <button className="btn-ghost" disabled={(page + 1) * PAGE >= entities.total} onClick={() => setPage(page + 1)}>Next</button>
                  </div>
                </div>
              </>
            )}
          </NeoCard>

          <EntityPanel
            caseId={caseId}
            detail={detail}
            similar={similar}
            selected={selected}
            busy={busy}
            onSelect={select}
            onOpenGraph={(node) => navigate(`/cases/${caseId}/graph?seed=${encodeURIComponent(node)}`)}
            onOpenFinding={(id) => navigate(`/findings/${id}`)}
            onSeed={(wallet, reason) => {
              setSeedForm({ ...seedForm, wallet_ref: wallet, reason });
              void submitSeed(wallet);
            }}
            seedReason={seedForm.reason}
            setSeedReason={(reason) => setSeedForm({ ...seedForm, reason })}
          />
        </div>
      ) : (
        <div className="two-col">
          <NeoCard>
            <h2>Highest propagated exposure</h2>
            {!run ? (
              <EmptyState title="No risk run yet" body="Add a seed wallet on the right; risk is propagated immediately over the case's UTXO value-flow graph." />
            ) : (
              <>
                <p className="coverage-note">{String(run.summary.method ?? "")}</p>
                <table className="data-table">
                  <thead>
                    <tr><th>Wallet</th><th>Risk</th><th>Downstream</th><th>Upstream</th><th>Tainted received</th><th></th></tr>
                  </thead>
                  <tbody>
                    {run.scores.slice(0, 100).map((score) => (
                      <tr key={score.wallet} className="clickable" onClick={() => {
                        const next = new URLSearchParams(params);
                        next.set("tab", "entities");
                        next.set("wallet", score.wallet);
                        setParams(next, { replace: true });
                        setSelected(score.wallet);
                      }}>
                        <td className="mono-id">{short(score.wallet, 9)} {score.kind === "entity" && <Badge tone="deterministic">entity</Badge>}</td>
                        <td><Badge tone={riskTone(score.risk)}>{pct(score.risk)}</Badge></td>
                        <td>{pct(score.downstream)}</td>
                        <td>{pct(score.upstream)}</td>
                        <td>{btc(score.tainted_received_sats)}</td>
                        <td>{score.is_seed ? <Badge tone="danger">SEED</Badge> : null}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </NeoCard>
          <NeoCard variant="neo-sm">
            <h2>Seed wallets</h2>
            <p className="coverage-note">
              A seed is your assertion (e.g. a wallet named in a ransomware report) with its reason — an input to
              propagation, never a TraceX conclusion. Downstream = share of a wallet's received value traceable to a
              seed (haircut method, ×0.85 per hop); upstream = share of its spending that reached a seed.
            </p>
            <div className="form-field">
              <label>Address or entity id</label>
              <input className="mono" value={seedForm.wallet_ref} onChange={(e) => setSeedForm({ ...seedForm, wallet_ref: e.target.value })} placeholder="bc1… or E-…" />
            </div>
            <div className="form-field">
              <label>Reason / provenance</label>
              <textarea value={seedForm.reason} onChange={(e) => setSeedForm({ ...seedForm, reason: e.target.value })} placeholder="Source of the attribution" />
            </div>
            <div className="form-field">
              <label>Weight (0–1)</label>
              <input type="number" min={0.05} max={1} step={0.05} value={seedForm.weight} onChange={(e) => setSeedForm({ ...seedForm, weight: Number(e.target.value) })} />
            </div>
            <button className="btn-mustard" style={{ padding: "11px 18px" }} disabled={busy} onClick={() => void submitSeed()}>
              {busy ? "Propagating…" : "Add seed & propagate"}
            </button>
            <div style={{ marginTop: 16 }}>
              {seeds.length === 0 ? (
                <p className="coverage-note">No seeds in this case.</p>
              ) : (
                seeds.map((seed) => {
                  const resolved = run?.seeds.find((item) => item.seed_id === seed.seed_id);
                  return (
                    <div key={seed.seed_id} className="kv-row" style={{ alignItems: "center" }}>
                      <span>
                        <span className="mono-id">{short(seed.wallet_ref, 8)}</span>{" "}
                        <Badge tone="danger">{seed.label}</Badge>{" "}
                        {resolved && !resolved.found && <Badge tone="warning">not in snapshot</Badge>}
                        <div className="coverage-note">{seed.reason}</div>
                      </span>
                      <button className="btn-ghost" disabled={busy} onClick={() => void removeSeed(seed.seed_id)}>Remove</button>
                    </div>
                  );
                })
              )}
            </div>
          </NeoCard>
        </div>
      )}
    </Shell>
  );
}

function EntityPanel(props: {
  caseId: string;
  detail: EntityDetail | null;
  similar: SimilarWallets | null;
  selected: string | null;
  busy: boolean;
  onSelect: (wallet: string) => void;
  onOpenGraph: (node: string) => void;
  onOpenFinding: (id: string) => void;
  onSeed: (wallet: string, reason: string) => void;
  seedReason: string;
  setSeedReason: (reason: string) => void;
}) {
  const { detail, similar, selected } = props;
  if (!selected) {
    return (
      <NeoCard variant="neo-sm">
        <h2>Cluster detail</h2>
        <p className="coverage-note">Select a cluster, or look up any address, to see its members, the transactions that link them, its relay endpoints and similar wallets.</p>
      </NeoCard>
    );
  }
  if (!detail) {
    return (
      <NeoCard variant="neo-sm"><h2>Cluster detail</h2><p className="coverage-note">Loading {selected}…</p></NeoCard>
    );
  }
  const entity = detail.entity;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <NeoCard variant="neo-sm">
        <h2>{detail.kind === "entity" ? "Entity cluster" : "Unclustered address"}</h2>
        <div className="mono-id" style={{ marginBottom: 10, wordBreak: "break-all" }}>{detail.wallet}</div>
        {detail.caution && <NoticeBanner>{detail.caution}</NoticeBanner>}
        {entity && (
          <>
            <div className="kv-row"><span className="k">Addresses</span><span>{entity.address_count.toLocaleString()}</span></div>
            <div className="kv-row"><span className="k">Linking transactions</span><span>{entity.linking_tx_count.toLocaleString()}</span></div>
            <div className="kv-row"><span className="k">Received / sent</span><span>{btc(entity.received_sats)} / {btc(entity.sent_sats)}</span></div>
          </>
        )}
        <div className="kv-row">
          <span className="k">Propagated risk</span>
          <span>{detail.risk ? <Badge tone={riskTone(detail.risk.risk)}>{pct(detail.risk.risk)}{detail.risk.is_seed ? " · seed" : ""}</Badge> : "—"}</span>
        </div>
        {detail.basis && <p className="coverage-note">{detail.basis}</p>}
        <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
          <button className="btn-ghost" onClick={() => props.onOpenGraph(`address:${detail.addresses[0]}`)}>Open in graph</button>
        </div>
        <div className="form-field" style={{ marginTop: 12 }}>
          <label>Mark as illicit seed — reason</label>
          <input value={props.seedReason} onChange={(e) => props.setSeedReason(e.target.value)} placeholder="e.g. named in report #123" />
        </div>
        <button className="btn-mustard" style={{ padding: "9px 14px" }} disabled={props.busy} onClick={() => props.onSeed(detail.wallet, props.seedReason)}>
          Seed &amp; propagate risk
        </button>
      </NeoCard>

      <NeoCard variant="neo-sm">
        <h2>Member addresses ({detail.address_total.toLocaleString()})</h2>
        <div className="browse-list">
          {detail.addresses.map((address) => (
            <button key={address} className="browse-item" onClick={() => props.onOpenGraph(`address:${address}`)}>
              <span className="mono-id">{address}</span>
            </button>
          ))}
        </div>
        {detail.linking_transactions.length > 0 && (
          <>
            <h2 style={{ marginTop: 16 }}>Linking transactions ({detail.linking_total.toLocaleString()})</h2>
            <div className="browse-list">
              {detail.linking_transactions.map((link) => (
                <button key={link.txid} className="browse-item" onClick={() => props.onOpenGraph(`tx:${link.txid}`)}>
                  <span className="mono-id">{short(link.txid, 12)}</span>
                  <span className="coverage-note">{link.input_addresses} inputs</span>
                </button>
              ))}
            </div>
          </>
        )}
      </NeoCard>

      <NeoCard variant="neo-sm">
        <h2>Relay endpoints (network layer)</h2>
        {detail.relay_endpoints.length === 0 ? (
          <p className="coverage-note">No network observations of this wallet's spends.</p>
        ) : (
          detail.relay_endpoints.map((endpoint) => (
            <div key={endpoint.ip} className="kv-row">
              <span className="mono-id">{endpoint.ip}</span>
              <span>
                {endpoint.spends} spend(s) · {endpoint.scope}
                {endpoint.db_country ? ` · ${endpoint.db_country}` : ""}
                {endpoint.db_asn ? ` · AS${endpoint.db_asn}` : ""}
                {endpoint.country_check === "mismatch" && <> <Badge tone="danger">geo mismatch</Badge></>}
              </span>
            </div>
          ))
        )}
        {detail.network_correlations.map((item) => (
          <NoticeBanner key={`${item.dimension}-${item.key}`}>
            {item.spends_on_key} of {item.observed_spends} spends relayed via {item.dimension} {item.key} (base rate{" "}
            {(item.base_rate * 100).toFixed(2)}%, adjusted p = {item.adjusted_p_value.toExponential(1)}).
          </NoticeBanner>
        ))}
      </NeoCard>

      <NeoCard variant="neo-sm">
        <h2>Behaviourally similar wallets</h2>
        {!similar ? (
          <p className="coverage-note">Loading…</p>
        ) : similar.similar.length === 0 ? (
          <p className="coverage-note">{similar.status}</p>
        ) : (
          <>
            <p className="coverage-note">{similar.method}</p>
            {similar.similar.map((item) => (
              <div key={item.wallet} className="kv-row" style={{ cursor: "pointer" }} onClick={() => props.onSelect(item.wallet)}>
                <span className="mono-id">{short(item.wallet, 10)}</span>
                <span>{item.similarity.toFixed(3)}</span>
              </div>
            ))}
          </>
        )}
      </NeoCard>

      {detail.findings.length > 0 && (
        <NeoCard variant="neo-sm">
          <h2>Findings on this wallet</h2>
          {detail.findings.map((finding) => (
            <div key={finding.finding_id} className="kv-row" style={{ cursor: "pointer" }} onClick={() => props.onOpenFinding(finding.finding_id)}>
              <span><Badge tone="deterministic">{finding.rule_id}</Badge></span>
              <span className="mono-id">{short(finding.entity_ref, 8)}</span>
            </div>
          ))}
        </NeoCard>
      )}
    </div>
  );
}

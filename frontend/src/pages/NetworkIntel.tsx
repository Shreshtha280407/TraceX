import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Shell } from "../components/Shell";
import { Badge, EmptyState, ErrorBanner, NeoCard, StatTile } from "../components/primitives";
import { api, ApiError, type NetworkResponse } from "../lib/api";

const CHECK_LABEL: Record<string, { label: string; tone: "success" | "danger" | "muted" | "warning" }> = {
  verified: { label: "matches Geo-IP", tone: "success" },
  mismatch: { label: "contradicts Geo-IP", tone: "danger" },
  unverifiable_non_public: { label: "non-public IP", tone: "muted" },
  not_in_database: { label: "not in database", tone: "warning" },
  no_geoip_database: { label: "no database", tone: "warning" },
  not_reported: { label: "not reported", tone: "muted" },
};

function CheckBars({ title, checks }: { title: string; checks: Record<string, number> | undefined }) {
  const entries = Object.entries(checks ?? {}).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((sum, [, value]) => sum + value, 0) || 1;
  return (
    <div style={{ marginBottom: 14 }}>
      <div className="coverage-note" style={{ marginBottom: 6 }}>{title}</div>
      {entries.length === 0 ? (
        <span className="coverage-note">—</span>
      ) : (
        entries.map(([key, value]) => (
          <div key={key} style={{ display: "grid", gridTemplateColumns: "160px 1fr 70px", gap: 10, alignItems: "center", marginBottom: 6 }}>
            <Badge tone={CHECK_LABEL[key]?.tone ?? "muted"}>{CHECK_LABEL[key]?.label ?? key}</Badge>
            <div style={{ background: "var(--surface-3)", borderRadius: 6, height: 10, overflow: "hidden" }}>
              <div style={{ width: `${(value / total) * 100}%`, height: "100%", background: "var(--mustard)" }} />
            </div>
            <span className="mono-id" style={{ textAlign: "right" }}>{value.toLocaleString()}</span>
          </div>
        ))
      )}
    </div>
  );
}

/** Network-layer intelligence: offline Geo-IP enrichment of every observed
 * relay endpoint, verification of the dataset's reported country/ASN, and the
 * network <-> wallet correlations TraceX found. */
export function NetworkIntel() {
  const { caseId } = useParams<{ caseId: string }>();
  const navigate = useNavigate();
  const [network, setNetwork] = useState<NetworkResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!caseId) return;
    setNetwork(null);
    setError(null);
    api.getNetwork(caseId).then(setNetwork).catch((e) => setError(e instanceof ApiError ? String(e.detail) : String(e)));
  }, [caseId]);

  if (!caseId) return null;
  const summary = network?.summary;
  const geo = network?.geoip;
  const hasDbIp = (geo?.sources ?? []).some((source) => source.licence === "CC-BY-4.0");

  return (
    <Shell>
      <div className="page-header">
        <div>
          <h1>Network Intelligence</h1>
          <p className="subtitle">
            IP / port / timing observations correlated with the blockchain layer, enriched offline from an open-source
            Geo-IP database. An endpoint is the relay that first reported a transaction — never its owner or origin.
          </p>
        </div>
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      {!network && !error && <p className="coverage-note">Loading…</p>}
      {network && summary && (
        <>
          <div className="stat-grid">
            <StatTile label="Observed transactions" value={summary.observed_transactions?.toLocaleString() ?? "—"} />
            <StatTile label="Endpoint IPs" value={summary.endpoint_ips?.toLocaleString() ?? "—"} sub={Object.entries(summary.endpoint_scopes ?? {}).map(([k, v]) => `${v} ${k}`).join(" · ")} />
            <StatTile label="Geo-IP database" value={geo?.installed ? "Installed" : "Not installed"} sub={geo?.installed ? `compiled ${geo.compiled_at ?? ""}` : "run tracex-geoip download"} />
            <StatTile label="Relay concentrations" value={summary.relay_concentrations ?? 0} sub={`${summary.correlation_wallets_tested?.toLocaleString() ?? 0} wallets tested`} />
            <StatTile label="Geo mismatches" value={summary.geo_mismatch_endpoints ?? 0} sub="endpoints contradicting Geo-IP" />
            <StatTile label="Network findings" value={network.network_finding_count} sub="network-correlation-v1" />
          </div>
          <div className="two-col">
            <NeoCard>
              <h2>Busiest relay endpoints</h2>
              {network.top_endpoints.length === 0 ? (
                <EmptyState title="No network observations" body="This dataset carries no src_ip / dst_ip fields." />
              ) : (
                <table className="data-table">
                  <thead>
                    <tr><th>Endpoint</th><th>Scope</th><th>Txs</th><th>Geo-IP</th><th>Reported</th><th>Check</th><th>Median latency</th></tr>
                  </thead>
                  <tbody>
                    {network.top_endpoints.map((endpoint) => (
                      <tr key={endpoint.ip}>
                        <td className="mono-id">{endpoint.ip}</td>
                        <td><Badge tone={endpoint.scope === "public" ? "deterministic" : "muted"}>{endpoint.scope}</Badge></td>
                        <td>{endpoint.transactions.toLocaleString()}</td>
                        <td>{endpoint.db_country ?? "—"}{endpoint.db_asn ? ` · AS${endpoint.db_asn}` : ""}{endpoint.db_as_org ? <div className="coverage-note">{endpoint.db_as_org}</div> : null}</td>
                        <td>{endpoint.reported_country ?? "—"}{endpoint.reported_asn ? ` · ${endpoint.reported_asn}` : ""}</td>
                        <td><Badge tone={CHECK_LABEL[endpoint.country_check]?.tone ?? "muted"}>{CHECK_LABEL[endpoint.country_check]?.label ?? endpoint.country_check}</Badge></td>
                        <td>{endpoint.median_relay_latency_s === null ? "—" : `${endpoint.median_relay_latency_s.toFixed(1)} s`}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <div style={{ marginTop: 14 }}>
                <button className="btn-ghost" onClick={() => navigate(`/cases/${caseId}/findings`)}>
                  View network-correlation findings
                </button>
              </div>
            </NeoCard>
            <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
              <NeoCard variant="neo-sm">
                <h2>Reported geo vs Geo-IP</h2>
                <CheckBars title="Country, per observation" checks={summary.observation_country_checks} />
                <CheckBars title="ASN, per observation" checks={summary.observation_asn_checks} />
              </NeoCard>
              <NeoCard variant="neo-sm">
                <h2>Observations by country</h2>
                {network.countries.map((row) => (
                  <div key={row.country} className="kv-row"><span>{row.country}</span><span className="mono-id">{row.observations.toLocaleString()}</span></div>
                ))}
              </NeoCard>
              <NeoCard variant="neo-sm">
                <h2>Geo-IP sources</h2>
                {!geo?.installed ? (
                  <p className="coverage-note">{geo?.hint}</p>
                ) : (
                  (geo.sources ?? []).map((source) => (
                    <div key={source.file} className="kv-row">
                      <span className="mono-id">{source.file.split("!").pop()}</span>
                      <span>{source.kind} · {source.licence}</span>
                    </div>
                  ))
                )}
                {hasDbIp && (
                  <p className="coverage-note" style={{ marginTop: 10 }}>
                    <a href="https://db-ip.com" target="_blank" rel="noreferrer">IP Geolocation by DB-IP</a> (CC BY 4.0) · ASN data by{" "}
                    <a href="https://iptoasn.com" target="_blank" rel="noreferrer">IPtoASN</a> (PDDL). Lookups run offline from a local copy.
                  </p>
                )}
              </NeoCard>
            </div>
          </div>
        </>
      )}
    </Shell>
  );
}

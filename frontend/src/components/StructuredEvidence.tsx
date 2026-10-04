import { useState } from "react";
import { api, type EvidenceReference, type StructuredFindingEvidence } from "../lib/api";
import { NeoCard } from "./primitives";
import { RecordPreview } from "./RecordPreview";

export function References({ refs, label }: { refs: EvidenceReference[]; label: string }) {
  const [page, setPage] = useState(0);
  const [raw, setRaw] = useState<{ key: string; record: unknown; csv: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);
  async function reopen(ref: EvidenceReference) {
    setError(null);
    try {
      const result = await api.getEvidenceRecord(ref.evidence_id, ref.locator);
      if (ref.source_sha256 && result.source_sha256 !== ref.source_sha256) throw new Error("Stale source hash: replay refused");
      setRaw({ key: `${ref.evidence_id}:${ref.locator}`, record: result.record, csv: ref.locator_type === "csv_logical_record" });
    } catch (err) { setRaw(null); setError(String(err)); }
  }
  return <div aria-label={label}>
    {error && <p role="alert">{error}</p>}
    {refs.slice(page * 20, (page + 1) * 20).map((ref) => <div key={`${ref.evidence_id}:${ref.locator}`}>
      <button type="button" className="btn-ghost" onClick={() => void reopen(ref)}>{label}: {ref.locator}</button>
      <small> {ref.reference_status} · {ref.evidence_id}</small>
      {raw?.key === `${ref.evidence_id}:${ref.locator}` && <RecordPreview record={raw.record} isCsv={raw.csv} />}
    </div>)}
    {refs.length > 20 && <p>
      <button type="button" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous references</button>
      <button type="button" disabled={(page + 1) * 20 >= refs.length} onClick={() => setPage(page + 1)}>Next references</button>
      {" "}{refs.length} references, 20 per page
    </p>}
  </div>;
}

export function StructuredEvidence({ value }: { value: StructuredFindingEvidence }) {
  return <NeoCard>
    <h2>Structured evidence assessment</h2>
    <h3>Exact proposition</h3><p>{value.proposition}</p>
    <p>{value.interpretation.category.replaceAll("_", " ")} · reviewer state: {value.interpretation.review_disposition}</p>
    <p className="coverage-note">{value.interpretation.scope}</p>
    <h3>Responsible rule/model</h3><pre>{JSON.stringify(value.responsible_procedure, null, 2)}</pre>
    <h3>Supporting observations</h3>
    {value.supporting_observations.map((row, index) => <p key={index}>{row.statement}</p>)}
    <References refs={value.supporting_refs} label="Reopen supporting record" />
    <h3>Observed counter-evidence</h3><p>{value.counter_evidence_summary}</p>
    {value.observed_counter_evidence.map((row, index) => <div key={index}>
      <p>{row.statement}</p><p className="coverage-note">{row.basis}</p>
      <References refs={row.source_refs} label="Reopen opposing record" />
    </div>)}
    {!!value.unverified_opposing_references.length && <p role="alert">Some opposing references are stale or unavailable; they are not verified counter-evidence.</p>}
    <h3>Plausible benign alternatives — not observations</h3>
    {value.benign_alternatives.map((row, index) => <p key={index}>{row.statement} <small>({row.basis})</small></p>)}
    <h3>Missing evidence and checked coverage</h3>
    {value.missing_evidence.map((row) => <p key={row}>{row}</p>)}
    <details><summary>Exact coverage</summary><pre>{JSON.stringify(value.coverage, null, 2)}</pre></details>
    <h3>Supported graph associations</h3>
    <p className="coverage-note">Associations are not identity or origin claims. Use the bounded neighborhood below.</p>
    <details><summary>Stored path, if available</summary><pre>{JSON.stringify(value.graph_associations, null, 2)}</pre></details>
    <details><summary>Feature values and comparison baselines</summary>
      <pre>{JSON.stringify(value.comparison_baselines, null, 2)}</pre><pre>{JSON.stringify(value.features, null, 2)}</pre>
    </details>
    <h3>Reviewer decisions and recorded reasons</h3>
    {!value.review_decisions.length && <p>No decision recorded.</p>}
    {value.review_decisions.map((row) => <p key={row.review_id}>{row.disposition}: {row.reason} · {row.created_at}</p>)}
    <details><summary>Score/calibration provenance and applicability</summary><pre>{JSON.stringify(value.score_provenance, null, 2)}</pre></details>
    <details><summary>ECOD, IF sensitivity and separate structure/burst fusion inputs</summary>
      <pre>{JSON.stringify(value.explanation_labels, null, 2)}</pre>
    </details>
    <p className="coverage-note">Context may warrant review, escalation or dismissal; a structural pattern remains observable even when benign. No arbitrary combined calibrated risk is assigned.</p>
  </NeoCard>;
}

import { useEffect, useState } from 'react'
import { KeyRound, ShieldAlert, ShieldCheck } from 'lucide-react'
import { ActiveCaseGate } from '../components/ActiveCaseGate'
import { Badge } from '../components/Badge'
import { Button } from '../components/Button'
import { Card } from '../components/Card'
import { DataTable, DataTableRow } from '../components/DataTable'
import { EmptyState, ErrorState, ForbiddenState, LoadingState } from '../components/DataState'
import { ApiError, casesApi, integrityApi } from '../lib/api/client'
import type { SecurityAuditEventRecord } from '../lib/api/case-types'
import type { IntegrityCheckpointView, VerificationResult } from '../lib/api/integrity-types'
import { useActiveCaseWorkspace } from '../lib/cases'
import { useHasCaseAction } from '../lib/auth/permissions'
import { formatRelativeTime } from '../lib/format'

const AUDIT_COLUMNS = [
  { key: 'event', header: 'Event' },
  { key: 'outcome', header: 'Outcome' },
  { key: 'when', header: 'When' },
]

/**
 * Page 15 -- Integrity / Audit. Real hash verification, Merkle checkpoints,
 * signatures, and the audit trail (Section 5's own role text), backed by
 * `GET .../integrity/checkpoints` and `POST .../checkpoints/{id}/verify`
 * (never a source-event feed -- this router "deliberately exposes
 * checkpoints, not a feed of source events", per app/modules/integrity/api.py's
 * own docstring), plus the same case audit-event feed already used elsewhere
 * (`casesApi.listAuditEvents`) -- including the entries Phase 3's real
 * review decisions generated.
 */
export function IntegrityAudit() {
  const { cases, loading: casesLoading, activeCaseId, activeCase, setActiveCase } =
    useActiveCaseWorkspace()
  const canVerify = useHasCaseAction('integrity_verify')

  const [checkpoints, setCheckpoints] = useState<IntegrityCheckpointView[]>([])
  const [auditEvents, setAuditEvents] = useState<SecurityAuditEventRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)

  const [verifying, setVerifying] = useState<string | null>(null)
  const [verifyResults, setVerifyResults] = useState<Record<string, VerificationResult>>({})
  const [verifyError, setVerifyError] = useState<string | null>(null)

  useEffect(() => {
    if (!activeCaseId) return
    let cancelled = false
    // oxlint-disable-next-line react/set-state-in-effect
    setLoading(true)
    setError(null)
    setForbidden(false)
    Promise.all([integrityApi.listCheckpoints(activeCaseId), casesApi.listAuditEvents(activeCaseId)])
      .then(([checkpointsRes, auditRes]) => {
        if (cancelled) return
        setCheckpoints(checkpointsRes.items)
        setAuditEvents(auditRes.items)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof ApiError && err.isForbidden) setForbidden(true)
        else setError(err instanceof Error ? err.message : 'Unable to reach the server.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [activeCaseId])

  function verify(checkpointId: string) {
    if (!activeCaseId) return
    setVerifying(checkpointId)
    setVerifyError(null)
    integrityApi
      .verifyCheckpoint(activeCaseId, checkpointId)
      .then((result) => {
        setVerifyResults((prev) => ({ ...prev, [checkpointId]: result }))
      })
      .catch((err: unknown) => {
        setVerifyError(err instanceof Error ? err.message : 'Unable to reach the server.')
      })
      .finally(() => setVerifying(null))
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        {activeCase ? (
          <span className="font-mono text-xs uppercase tracking-wider text-text-faint">
            {activeCase.case_reference}
          </span>
        ) : null}
        <h1 className="text-2xl font-semibold text-text">Integrity / Audit</h1>
        <p className="text-sm text-text-dim">Hash verification, Merkle checkpoints, signatures, audit trail.</p>
      </div>

      {!activeCase ? (
        <ActiveCaseGate
          cases={cases}
          loading={casesLoading}
          activeCaseId={activeCaseId}
          onSelect={setActiveCase}
        />
      ) : forbidden ? (
        <ForbiddenState message="You don't have permission to view this case's integrity trail." />
      ) : error ? (
        <ErrorState message={error} />
      ) : loading ? (
        <LoadingState label="Loading integrity data..." />
      ) : (
        <>
          <div>
            <h2 className="text-sm font-semibold text-text">Merkle checkpoints</h2>
            <p className="text-xs text-text-dim">
              Each checkpoint roots a contiguous range of the case's append-only audit chain.
            </p>
          </div>

          {verifyError ? <ErrorState message={verifyError} /> : null}

          {checkpoints.length === 0 ? (
            <EmptyState
              title="No checkpoints yet"
              description="A checkpoint is created periodically as this case's audit chain grows."
            />
          ) : (
            <div className="flex flex-col gap-2">
              {checkpoints.map((checkpoint) => {
                const result = verifyResults[checkpoint.checkpoint_id]
                return (
                  <Card key={checkpoint.checkpoint_id} className="flex flex-col gap-3 p-4">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="flex flex-col gap-0.5">
                        <span className="text-sm font-medium text-text">
                          Sequence {checkpoint.start_sequence}&ndash;{checkpoint.end_sequence}{' '}
                          <span className="text-text-faint">({checkpoint.leaf_count} leaves)</span>
                        </span>
                        <span className="font-mono text-xs text-text-faint" title={checkpoint.root_hash}>
                          root {checkpoint.root_hash.slice(0, 24)}&hellip;
                        </span>
                        <span className="text-xs text-text-faint">
                          {formatRelativeTime(checkpoint.created_at)} &middot; tree format{' '}
                          {checkpoint.tree_format_version}
                        </span>
                      </div>
                      {canVerify ? (
                        <Button
                          variant="secondary"
                          onClick={() => verify(checkpoint.checkpoint_id)}
                          disabled={verifying === checkpoint.checkpoint_id}
                        >
                          {verifying === checkpoint.checkpoint_id ? 'Verifying...' : 'Verify'}
                        </Button>
                      ) : null}
                    </div>

                    {checkpoint.signature ? (
                      <div className="flex flex-wrap items-center gap-3 rounded-control border border-card-border bg-canvas/5 px-3 py-2 text-xs text-text-dim">
                        <span className="flex items-center gap-1.5">
                          <KeyRound size={12} className="text-text-faint" aria-hidden="true" />
                          {checkpoint.signature.algorithm} &middot;{' '}
                          <span className="font-mono">{checkpoint.signature.public_key_fingerprint}</span>
                        </span>
                        <span>Signed {formatRelativeTime(checkpoint.signature.signed_at)}</span>
                      </div>
                    ) : (
                      <span className="text-xs text-berry">Not yet signed.</span>
                    )}

                    {result ? (
                      result.ok ? (
                        <span className="flex items-center gap-1.5 text-xs font-medium text-palm">
                          <ShieldCheck size={14} aria-hidden="true" />
                          Verified -- leaf count, root hash, and signature all match.
                        </span>
                      ) : (
                        <span className="flex items-center gap-1.5 text-xs font-medium text-crimson">
                          <ShieldAlert size={14} aria-hidden="true" />
                          Verification failed{result.reason ? `: ${result.reason}` : ''} (leaf count{' '}
                          {result.leaf_count_matches ? 'ok' : 'mismatch'}, root{' '}
                          {result.root_matches ? 'ok' : 'mismatch'}, signature{' '}
                          {result.signature_valid ? 'valid' : 'invalid'}).
                        </span>
                      )
                    ) : null}
                  </Card>
                )
              })}
            </div>
          )}

          <div>
            <h2 className="text-sm font-semibold text-text">Audit trail</h2>
            <p className="text-xs text-text-dim">
              The most recent safe, case-scoped audit events -- including entries from every
              candidate/hypothesis review decision.
            </p>
          </div>

          {auditEvents.length === 0 ? (
            <EmptyState title="No audit events yet" description="Actions on this case will appear here." />
          ) : (
            <DataTable columns={AUDIT_COLUMNS}>
              {auditEvents.map((event) => (
                <DataTableRow
                  key={event.event_id}
                  columns={AUDIT_COLUMNS}
                  primary={event.event_type}
                  secondary={event.user_id_nullable ?? undefined}
                  cells={{
                    outcome: (
                      <Badge
                        tone={
                          event.outcome === 'success'
                            ? 'palm'
                            : event.outcome === 'denied'
                              ? 'crimson'
                              : 'berry'
                        }
                      >
                        {event.outcome}
                      </Badge>
                    ),
                    when: (
                      <span className="text-sm text-text-dim">
                        {formatRelativeTime(event.occurred_at)}
                      </span>
                    ),
                  }}
                />
              ))}
            </DataTable>
          )}
        </>
      )}
    </div>
  )
}


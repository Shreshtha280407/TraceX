import { useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, FileStack, ListChecks, ShieldAlert } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import { Badge } from '../components/Badge'
import { Card } from '../components/Card'
import { DataTable, DataTableRow } from '../components/DataTable'
import { EmptyState, ErrorState, LoadingState } from '../components/DataState'
import { StatCard } from '../components/StatCard'
import { casesApi, entityApi, evidenceApi, reviewApi } from '../lib/api/client'
import type { AssignedCase, SecurityAuditEventRecord } from '../lib/api/case-types'
import { useAssignedCases } from '../lib/cases'
import { formatRelativeTime } from '../lib/format'
import { useAuthStore } from '../lib/auth/store'
import { roleHasCaseAction } from '../lib/auth/permissions'

const CASE_COLUMNS = [
  { key: 'case', header: 'Case' },
  { key: 'status', header: 'Status' },
  { key: 'role', header: 'Your role' },
  { key: 'classification', header: 'Classification' },
]

interface PendingQueueItem {
  key: string
  caseId: string
  caseReference: string
  label: string
  path: '/review/candidates' | '/hypotheses'
}

interface DashboardAggregate {
  pendingCandidates: number
  pendingHypotheses: number
  evidenceProcessed: number
  evidenceTotal: number
  alerts: (SecurityAuditEventRecord & { caseReference: string })[]
  recentActivity: (SecurityAuditEventRecord & { caseReference: string })[]
  pendingQueue: PendingQueueItem[]
}

const EMPTY_AGGREGATE: DashboardAggregate = {
  pendingCandidates: 0,
  pendingHypotheses: 0,
  evidenceProcessed: 0,
  evidenceTotal: 0,
  alerts: [],
  recentActivity: [],
  pendingQueue: [],
}

/**
 * Fans out to every real per-case endpoint that exists (Section 8, Phase 2):
 * candidates/hypotheses (for pending-review counts), evidence (for the
 * processed count), and audit events (for alerts + recent activity). One
 * case's failure never blanks the whole dashboard -- `Promise.allSettled`
 * per case, degrading gracefully.
 */
async function loadAggregate(cases: AssignedCase[]): Promise<DashboardAggregate> {
  const perCase = await Promise.allSettled(
    cases.map(async (c) => {
      // ABAC (Section 6): "if the backend would reject an action, the UI
      // should not offer it in the first place." A role that structurally
      // lacks `graph_read`/`evidence_read` (e.g. `viewer` lacks both) would
      // 403 on *every single dashboard load* for these calls -- not from
      // anything the investigator did, just from opening their own
      // dashboard. Live-confirmed this generates a `case_access_denied`
      // audit event per blocked call, which then surfaces as a confusing
      // phantom "alert" with no real cause the investigator can act on.
      // Skipping the call entirely for a role that can never pass it avoids
      // both the wasted round-trip and the self-inflicted audit noise.
      const canReadGraph = roleHasCaseAction(c.role, 'graph_read')
      const canReadEvidence = roleHasCaseAction(c.role, 'evidence_read')
      const [candidates, entityCandidates, hypotheses, evidence, audit] = await Promise.all([
        canReadGraph ? reviewApi.listCandidates(c.case_id).catch(() => ({ items: [] })) : { items: [] },
        // Entity-resolution candidates share the same `graph_read` gate as
        // the correlation-candidate queue above (`entity_api.py`'s
        // `list_entity_candidates`) -- both queues are reviewable on the
        // real Candidate Review page, so both count toward "pending
        // reviews" here.
        canReadGraph ? entityApi.listCandidates(c.case_id).catch(() => ({ items: [] })) : { items: [] },
        canReadGraph
          ? reviewApi.listHypotheses(c.case_id).catch(() => ({ items: [], next_cursor: null }))
          : { items: [], next_cursor: null },
        canReadEvidence ? evidenceApi.list(c.case_id).catch(() => ({ items: [] })) : { items: [] },
        // 200 (the endpoint's real max -- there is no server-side
        // event_type filter to ask for "just the business events") rather
        // than a small page: `case_access_granted` fires on every single
        // case-scoped read, live-confirmed to be volume enough to push
        // genuinely meaningful events (case.create, evidence.upload) out of
        // even a 20-row window entirely, not just out of visual prominence.
        // Still a mitigation, not a guarantee, under heavy enough traffic.
        casesApi.listAuditEvents(c.case_id, 200).catch(() => ({ items: [] })),
      ])
      return { case: c, candidates, entityCandidates, hypotheses, evidence, audit }
    }),
  )

  const aggregate: DashboardAggregate = {
    ...EMPTY_AGGREGATE,
    alerts: [],
    recentActivity: [],
    pendingQueue: [],
  }
  for (const result of perCase) {
    if (result.status !== 'fulfilled') continue
    const { case: c, candidates, entityCandidates, hypotheses, evidence, audit } = result.value
    const pendingCorrelationCandidates = candidates.items.filter(
      (item) => item.review_status === 'needs_review',
    )
    const pendingEntityCandidates = entityCandidates.items.filter(
      (item) => item.effective_status === 'needs_review',
    )
    const pendingHypothesesForCase = hypotheses.items.filter((item) => item.status === 'needs_review')
    aggregate.pendingCandidates += pendingCorrelationCandidates.length + pendingEntityCandidates.length
    aggregate.pendingHypotheses += pendingHypothesesForCase.length
    aggregate.evidenceTotal += evidence.items.length
    aggregate.evidenceProcessed += evidence.items.filter(
      (item) => item.processing_status === 'processed',
    ).length
    // "Queue preview" (Section 5's own role text for this page) -- a few
    // real, specific pending items to jump straight into, not just a
    // count. Capped per case so one noisy case can't crowd out every
    // other assigned case's own pending work.
    for (const item of pendingEntityCandidates.slice(0, 3)) {
      aggregate.pendingQueue.push({
        key: `entity-${item.candidate.entity_resolution_candidate_id}`,
        caseId: c.case_id,
        caseReference: c.case_reference,
        label: `Entity-resolution candidate ${item.candidate.entity_resolution_candidate_id.slice(0, 8)}`,
        path: '/review/candidates',
      })
    }
    for (const item of pendingCorrelationCandidates.slice(0, 3)) {
      aggregate.pendingQueue.push({
        key: `correlation-${item.candidate.candidate_link_id}`,
        caseId: c.case_id,
        caseReference: c.case_reference,
        label: `Correlation candidate ${item.candidate.candidate_link_id.slice(0, 8)}`,
        path: '/review/candidates',
      })
    }
    for (const item of pendingHypothesesForCase.slice(0, 3)) {
      aggregate.pendingQueue.push({
        key: `hypothesis-${item.hypothesis_id}`,
        caseId: c.case_id,
        caseReference: c.case_reference,
        label: item.statement.length > 60 ? `${item.statement.slice(0, 60)}...` : item.statement,
        path: '/hypotheses',
      })
    }
    for (const event of audit.items) {
      // `case_access_granted` fires on every single successful case-scoped
      // API call (confirmed live: browsing this dashboard itself generates
      // one per request) -- real data, but pure access telemetry with zero
      // investigative meaning, and its sheer volume drowns out genuine
      // business events (case.create, evidence.upload, ...) in "recent
      // activity". Excluded from both feeds; `case_access_denied` is kept
      // (outcome != success already routes it to Alerts below) since a
      // denial IS a meaningful signal, unlike a routine grant.
      if (event.event_type === 'case_access_granted') continue
      const withCase = { ...event, caseReference: c.case_reference }
      aggregate.recentActivity.push(withCase)
      if (event.outcome !== 'success') aggregate.alerts.push(withCase)
    }
  }
  aggregate.recentActivity.sort((a, b) => b.occurred_at.localeCompare(a.occurred_at))
  aggregate.alerts.sort((a, b) => b.occurred_at.localeCompare(a.occurred_at))
  aggregate.recentActivity = aggregate.recentActivity.slice(0, 10)
  aggregate.alerts = aggregate.alerts.slice(0, 10)
  aggregate.pendingQueue = aggregate.pendingQueue.slice(0, 8)
  return aggregate
}

function humanizeEventType(eventType: string): string {
  return eventType.replace(/[._]/g, ' ')
}

/**
 * Page 2 -- Dashboard. Every value below is real, fetched from the live
 * backend for this investigator's actual assigned cases -- no fixture data.
 * "Priority" and "candidate bridge"/"contradiction" style alert framing from
 * the approved mockup are dropped: nothing in the real backend backs a
 * priority ranking or bridge/contradiction detection yet, and this phase
 * never fabricates a field to fill that gap. Alerts here are real
 * non-success audit events (denied/failed); recent activity is the full
 * real audit stream, both across every case this investigator can see.
 */
export function Dashboard() {
  const navigate = useNavigate()
  const user = useAuthStore((state) => state.user)
  const setActiveCase = useAuthStore((state) => state.setActiveCase)
  const { cases, loading: casesLoading, error: casesError } = useAssignedCases()
  const [aggregate, setAggregate] = useState<DashboardAggregate>(EMPTY_AGGREGATE)
  const [aggLoading, setAggLoading] = useState(true)

  function openCase(caseId: string) {
    setActiveCase(caseId)
    navigate('/workspace')
  }

  function openPendingItem(item: PendingQueueItem) {
    setActiveCase(item.caseId)
    navigate(item.path)
  }

  useEffect(() => {
    if (casesLoading) return
    let cancelled = false
    // Fetch-on-cases-change with loading state: the standard, correct shape
    // for this pattern -- not the "should have been derived during render"
    // case this lint rule targets.
    // oxlint-disable-next-line react/set-state-in-effect
    setAggLoading(true)
    loadAggregate(cases).then((result) => {
      if (!cancelled) setAggregate(result)
    }).finally(() => {
      if (!cancelled) setAggLoading(false)
    })
    return () => {
      cancelled = true
    }
  }, [cases, casesLoading])

  const greetingName = user?.display_name.split(' ')[0] ?? ''

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold text-text">Good day, {greetingName}</h1>
        <p className="text-sm text-text-dim">
          {casesLoading
            ? 'Loading your assigned cases...'
            : `${cases.length} case${cases.length === 1 ? '' : 's'} assigned${
                aggLoading
                  ? ''
                  : ` · ${aggregate.pendingCandidates + aggregate.pendingHypotheses} item${
                      aggregate.pendingCandidates + aggregate.pendingHypotheses === 1 ? '' : 's'
                    } await your review`
              }`}
        </p>
      </div>

      {casesError ? <ErrorState message={casesError} /> : null}

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard label="Assigned cases" value={casesLoading ? '...' : cases.length} icon={FileStack} />
        <StatCard
          label="Pending reviews"
          value={aggLoading ? '...' : aggregate.pendingCandidates + aggregate.pendingHypotheses}
          icon={ListChecks}
          subtext={
            aggLoading
              ? undefined
              : `${aggregate.pendingCandidates} candidate · ${aggregate.pendingHypotheses} hypothesis`
          }
          deltaTone="berry"
        />
        <StatCard
          label="Active alerts"
          value={aggLoading ? '...' : aggregate.alerts.length}
          icon={AlertTriangle}
          deltaTone="crimson"
        />
        <StatCard
          label="Evidence processed"
          value={aggLoading ? '...' : aggregate.evidenceProcessed}
          icon={CheckCircle2}
          subtext={aggLoading ? undefined : `of ${aggregate.evidenceTotal} uploaded`}
          deltaTone="palm"
        />
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[2fr_1fr]">
        <div className="flex flex-col gap-3">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-text-faint">
            Assigned cases
          </h2>
          {casesLoading ? (
            <LoadingState label="Loading assigned cases..." />
          ) : cases.length === 0 ? (
            <EmptyState
              title="No assigned cases yet"
              description="Create a case to start an investigation, or ask a Case Head to add you to an existing one."
              action={
                <Link to="/cases/new" className="text-xs font-medium text-crimson hover:underline">
                  Create a case &rarr;
                </Link>
              }
            />
          ) : (
            <DataTable columns={CASE_COLUMNS}>
              {cases.map((c) => (
                <DataTableRow
                  key={c.case_id}
                  columns={CASE_COLUMNS}
                  primary={c.case_reference}
                  secondary={c.case_id}
                  onClick={() => openCase(c.case_id)}
                  cells={{
                    status: <Badge tone="steel-neutral">{c.status}</Badge>,
                    role: <span className="text-sm text-text-dim">{c.role.replace(/_/g, ' ')}</span>,
                    classification: <Badge tone="steel-neutral">{c.classification}</Badge>,
                  }}
                />
              ))}
            </DataTable>
          )}

          <h2 className="mt-3 text-sm font-semibold uppercase tracking-wide text-text-faint">
            Pending review queue
          </h2>
          {aggLoading ? (
            <LoadingState label="Loading pending reviews..." />
          ) : aggregate.pendingQueue.length === 0 ? (
            <EmptyState title="Nothing pending" description="No candidates or hypotheses currently need your review." />
          ) : (
            <Card className="flex flex-col divide-y divide-card-border p-0">
              {aggregate.pendingQueue.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  onClick={() => openPendingItem(item)}
                  className="flex items-center justify-between gap-3 p-4 text-left transition-colors hover:bg-canvas/10 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-crimson"
                >
                  <div className="flex flex-col gap-0.5">
                    <span className="text-sm font-medium text-text">{item.label}</span>
                    <span className="text-xs text-text-dim">{item.caseReference}</span>
                  </div>
                  <Badge tone="berry">needs review</Badge>
                </button>
              ))}
            </Card>
          )}
        </div>

        <div className="flex flex-col gap-3">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-text-faint">Alerts</h2>
          {aggLoading ? (
            <LoadingState label="Loading alerts..." />
          ) : aggregate.alerts.length === 0 ? (
            <EmptyState title="No active alerts" description="Nothing needs your attention right now." />
          ) : (
            <Card className="flex flex-col divide-y divide-card-border p-0">
              {aggregate.alerts.map((event) => (
                <div key={event.event_id} className="flex items-start gap-3 p-4">
                  <ShieldAlert size={16} className="mt-0.5 shrink-0 text-crimson" aria-hidden="true" />
                  <div className="flex flex-col gap-0.5">
                    <span className="text-sm font-medium capitalize text-text">
                      {humanizeEventType(event.event_type)}
                    </span>
                    <span className="text-xs text-text-dim">
                      {event.caseReference} &middot; {formatRelativeTime(event.occurred_at)}
                    </span>
                  </div>
                </div>
              ))}
            </Card>
          )}

          <h2 className="mt-3 text-sm font-semibold uppercase tracking-wide text-text-faint">
            Recent activity
          </h2>
          {aggLoading ? (
            <LoadingState label="Loading recent activity..." />
          ) : aggregate.recentActivity.length === 0 ? (
            <EmptyState title="No recent activity" />
          ) : (
            <Card className="flex flex-col divide-y divide-card-border p-0">
              {aggregate.recentActivity.map((event) => (
                <div key={event.event_id} className="flex items-start gap-3 p-4">
                  <CheckCircle2
                    size={16}
                    className={`mt-0.5 shrink-0 ${event.outcome === 'success' ? 'text-palm' : 'text-berry'}`}
                    aria-hidden="true"
                  />
                  <div className="flex flex-col gap-0.5">
                    <span className="text-sm font-medium capitalize text-text">
                      {humanizeEventType(event.event_type)}
                    </span>
                    <span className="text-xs text-text-dim">
                      {event.caseReference} &middot; {formatRelativeTime(event.occurred_at)}
                    </span>
                  </div>
                </div>
              ))}
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}

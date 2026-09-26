import { type FormEvent, useEffect, useMemo, useState } from 'react'
import { Search } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import { Badge } from '../components/Badge'
import { Button } from '../components/Button'
import { DataTable, DataTableRow } from '../components/DataTable'
import { EmptyState, ErrorState, LoadingState } from '../components/DataState'
import { useAssignedCases } from '../lib/cases'
import { useAuthStore } from '../lib/auth/store'
import { ApiError, casesApi } from '../lib/api/client'
import type { CaseMemberCandidateView, CaseMemberDetailView } from '../lib/api/case-types'
import type { CaseRole, ClearanceLevel } from '../lib/api/types'

const COLUMNS = [
  { key: 'case', header: 'Case' },
  { key: 'status', header: 'Status' },
  { key: 'role', header: 'Your role' },
  { key: 'classification', header: 'Classification' },
  { key: 'access', header: 'Access' },
]

const MANAGER_ROLES: CaseRole[] = ['case_owner', 'case_manager']
const CASE_ROLES: CaseRole[] = ['case_owner', 'case_manager', 'investigator', 'analyst', 'reviewer', 'viewer']
const CLEARANCES: ClearanceLevel[] = ['restricted', 'confidential', 'secret']

/**
 * Page 3 -- Case Management. No backend endpoint lists "every case visible
 * to the current user" (only create/get-by-id/status exist -- see
 * lib/cases.ts's docstring), so search/filter here is real but client-side,
 * over the same real, fully-hydrated case list the Dashboard uses. Opening
 * a row sets it as the active case (ABAC) and routes toward Workspace --
 * the real destination once Phase 3 builds it.
 */
export function CaseManagement() {
  const navigate = useNavigate()
  const setActiveCase = useAuthStore((state) => state.setActiveCase)
  const { cases, loading, error, refresh } = useAssignedCases()
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState<'all' | 'open' | 'closed' | 'archived'>('all')
  const [managingCaseId, setManagingCaseId] = useState<string | null>(null)

  const filtered = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase()
    return cases.filter((c) => {
      if (statusFilter !== 'all' && c.status !== statusFilter) return false
      if (!normalizedQuery) return true
      return (
        c.case_reference.toLowerCase().includes(normalizedQuery) ||
        c.case_id.toLowerCase().includes(normalizedQuery)
      )
    })
  }, [cases, query, statusFilter])

  function openCase(caseId: string) {
    setActiveCase(caseId)
    navigate('/workspace')
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-text">Case Management</h1>
          <p className="text-sm text-text-dim">Browse, search, and open your assigned investigations.</p>
        </div>
        <Link to="/cases/new">
          <Button>New Case</Button>
        </Link>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <span className="flex min-w-0 flex-1 max-w-sm items-center gap-2 rounded-control border border-card-border bg-card px-3 py-2">
          <Search size={14} className="shrink-0 text-text-faint" aria-hidden="true" />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search by case reference..."
            className="w-full bg-transparent text-sm text-text placeholder:text-text-faint focus:outline-none"
          />
        </span>
        <select
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value as typeof statusFilter)}
          className="rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-crimson/40"
        >
          <option value="all">All statuses</option>
          <option value="open">Open</option>
          <option value="closed">Closed</option>
          <option value="archived">Archived</option>
        </select>
      </div>

      {error ? <ErrorState message={error} /> : null}

      {managingCaseId ? (
        <CaseAccessPanel
          caseId={managingCaseId}
          onClose={() => setManagingCaseId(null)}
          onMembershipChanged={refresh}
        />
      ) : null}

      {loading ? (
        <LoadingState label="Loading your cases..." />
      ) : cases.length === 0 ? (
        <EmptyState
          title="No assigned cases yet"
          description="Create a case to start an investigation."
          action={
            <Link to="/cases/new">
              <Button className="mt-2">Create a case</Button>
            </Link>
          }
        />
      ) : filtered.length === 0 ? (
        <EmptyState title="No cases match your search" description="Try a different reference or clear the status filter." />
      ) : (
        <DataTable columns={COLUMNS}>
          {filtered.map((c) => (
            <DataTableRow
              key={c.case_id}
              columns={COLUMNS}
              primary={c.case_reference}
              secondary={c.case_id}
              onClick={() => openCase(c.case_id)}
              cells={{
                status: <Badge tone="steel-neutral">{c.status}</Badge>,
                role: <span className="text-sm text-text-dim">{c.role.replace(/_/g, ' ')}</span>,
                classification: <Badge tone="steel-neutral">{c.classification}</Badge>,
                access: MANAGER_ROLES.includes(c.role) ? (
                  <Button
                    type="button"
                    variant="secondary"
                    className="px-3 py-1 text-xs"
                    onClick={(event) => {
                      event.stopPropagation()
                      setManagingCaseId(c.case_id)
                    }}
                  >
                    Manage members
                  </Button>
                ) : (
                  <span className="text-xs text-text-faint">Not permitted</span>
                ),
              }}
            />
          ))}
        </DataTable>
      )}
    </div>
  )
}

function CaseAccessPanel({
  caseId,
  onClose,
  onMembershipChanged,
}: {
  caseId: string
  onClose: () => void
  onMembershipChanged: () => Promise<void>
}) {
  const [members, setMembers] = useState<CaseMemberDetailView[]>([])
  const [candidates, setCandidates] = useState<CaseMemberCandidateView[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [selectedUserId, setSelectedUserId] = useState('')
  const [role, setRole] = useState<CaseRole>('investigator')
  const [clearance, setClearance] = useState<ClearanceLevel>('restricted')
  const [submitting, setSubmitting] = useState(false)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const [memberResponse, candidateResponse] = await Promise.all([
        casesApi.listMembers(caseId),
        casesApi.listMemberCandidates(caseId),
      ])
      setMembers(memberResponse.items)
      setCandidates(candidateResponse.items)
    } catch (err) {
      setError(err instanceof ApiError && err.isForbidden ? 'You are not allowed to manage this case.' : err instanceof ApiError ? err.message : 'Unable to load case access.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    // Load anew only when a different selected case opens this panel.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseId])

  async function assign(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setSuccess(null)
    if (!selectedUserId) {
      setError('Select an active account to assign.')
      return
    }
    setSubmitting(true)
    try {
      await casesApi.addMember(caseId, { user_id: selectedUserId, role, clearance })
      setSelectedUserId('')
      setSuccess('Member assigned to this case.')
      await Promise.all([load(), onMembershipChanged()])
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to assign this member.')
    } finally {
      setSubmitting(false)
    }
  }

  async function saveMember(member: CaseMemberDetailView, nextRole: CaseRole, nextClearance: ClearanceLevel) {
    setError(null)
    setSuccess(null)
    setSubmitting(true)
    try {
      await casesApi.updateMember(caseId, member.user_id, { role: nextRole, clearance: nextClearance })
      setSuccess('Member access updated.')
      await Promise.all([load(), onMembershipChanged()])
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to update member access.')
    } finally {
      setSubmitting(false)
    }
  }

  async function deactivate(member: CaseMemberDetailView) {
    setError(null)
    setSuccess(null)
    setSubmitting(true)
    try {
      await casesApi.deactivateMember(caseId, member.user_id)
      setSuccess('Member access deactivated.')
      await Promise.all([load(), onMembershipChanged()])
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to deactivate member access.')
    } finally {
      setSubmitting(false)
    }
  }

  const assignable = candidates.filter((candidate) => !members.some((member) => member.user_id === candidate.user_id && member.is_active))
  return (
    <section className="rounded-card border border-card-border bg-card p-5">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-text">Case access / manage members</h2>
          <p className="text-sm text-text-dim">Case roles do not grant system administrator access.</p>
        </div>
        <Button type="button" variant="ghost" onClick={onClose}>Close</Button>
      </div>
      {error ? <ErrorState message={error} /> : null}
      {success ? <p className="mb-3 rounded-control border border-palm/30 bg-palm/10 px-3 py-2 text-sm text-palm">{success}</p> : null}
      {loading ? <LoadingState label="Loading case members…" /> : error ? null : (
        <>
          <form className="mb-5 grid gap-3 md:grid-cols-4" onSubmit={assign}>
            <select required value={selectedUserId} onChange={(event) => setSelectedUserId(event.target.value)} className="rounded-control border border-card-border bg-canvas px-3 py-2 text-sm text-text">
              <option value="">Select existing account</option>
              {assignable.map((candidate) => <option key={candidate.user_id} value={candidate.user_id}>{candidate.display_name} &lt;{candidate.email_normalized}&gt;</option>)}
            </select>
            <RoleSelect value={role} onChange={setRole} />
            <ClearanceSelect value={clearance} onChange={setClearance} />
            <Button type="submit" disabled={submitting}>Assign to case</Button>
          </form>
          {members.length === 0 ? <EmptyState title="No case members" description="Assign an existing account to begin." /> : (
            <div className="space-y-2">
              {members.map((member) => <MemberRow key={member.user_id} member={member} disabled={submitting} onSave={saveMember} onDeactivate={deactivate} />)}
            </div>
          )}
        </>
      )}
    </section>
  )
}

function RoleSelect({ value, onChange }: { value: CaseRole; onChange: (value: CaseRole) => void }) {
  return <select value={value} onChange={(event) => onChange(event.target.value as CaseRole)} className="rounded-control border border-card-border bg-canvas px-3 py-2 text-sm text-text">{CASE_ROLES.map((item) => <option key={item} value={item}>{item.replace(/_/g, ' ')}</option>)}</select>
}

function ClearanceSelect({ value, onChange }: { value: ClearanceLevel; onChange: (value: ClearanceLevel) => void }) {
  return <select value={value} onChange={(event) => onChange(event.target.value as ClearanceLevel)} className="rounded-control border border-card-border bg-canvas px-3 py-2 text-sm text-text">{CLEARANCES.map((item) => <option key={item} value={item}>{item}</option>)}</select>
}

function MemberRow({ member, disabled, onSave, onDeactivate }: { member: CaseMemberDetailView; disabled: boolean; onSave: (member: CaseMemberDetailView, role: CaseRole, clearance: ClearanceLevel) => Promise<void>; onDeactivate: (member: CaseMemberDetailView) => Promise<void> }) {
  const [role, setRole] = useState(member.role)
  const [clearance, setClearance] = useState(member.clearance)
  return <div className="grid items-center gap-2 rounded-control border border-card-border p-3 md:grid-cols-[minmax(0,1fr)_10rem_10rem_auto]">
    <div><p className="text-sm font-medium text-text">{member.display_name} {!member.is_active ? <span className="text-text-faint">(inactive)</span> : null}</p><p className="text-xs text-text-dim">{member.email_normalized}</p></div>
    <RoleSelect value={role} onChange={setRole} />
    <ClearanceSelect value={clearance} onChange={setClearance} />
    <div className="flex gap-2"><Button type="button" variant="secondary" disabled={disabled || !member.is_active} onClick={() => void onSave(member, role, clearance)}>Save</Button><Button type="button" variant="ghost" disabled={disabled || !member.is_active} onClick={() => void onDeactivate(member)}>Deactivate</Button></div>
  </div>
}

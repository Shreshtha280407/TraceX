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
import type { CaseMemberDetailView } from '../lib/api/case-types'
import type { CaseRole, ClearanceLevel } from '../lib/api/types'

const COLUMNS = [
  { key: 'case', header: 'Case' },
  { key: 'status', header: 'Status' },
  { key: 'role', header: 'Your role' },
  { key: 'classification', header: 'Classification' },
  { key: 'access', header: 'Access' },
]

const MANAGER_ROLES: CaseRole[] = ['case_owner', 'case_manager']
const OWNER_ASSIGNABLE_ROLES: CaseRole[] = ['case_manager', 'investigator', 'analyst', 'reviewer', 'viewer']
const MANAGER_ASSIGNABLE_ROLES: CaseRole[] = ['investigator', 'analyst', 'reviewer', 'viewer']
const CLEARANCES: ClearanceLevel[] = ['restricted', 'confidential', 'secret']
const CLEARANCE_LABEL: Record<ClearanceLevel, string> = {
  restricted: 'Standard',
  confidential: 'Sensitive',
  secret: 'Highly Sensitive',
}

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
  const user = useAuthStore((state) => state.user)
  const { cases, loading, error, refresh } = useAssignedCases()
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState<'all' | 'open' | 'closed' | 'archived'>('all')
  const [managingCaseId, setManagingCaseId] = useState<string | null>(null)
  const [managingRole, setManagingRole] = useState<CaseRole | null>(null)
  const canCreateCases = user?.system_role === 'case_head'

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
        {canCreateCases ? <Link to="/cases/new"><Button>New Case</Button></Link> : null}
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
          actorRole={managingRole ?? 'viewer'}
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
          action={canCreateCases ? <Link to="/cases/new"><Button className="mt-2">Create a case</Button></Link> : undefined}
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
                classification: <Badge tone="steel-neutral">{CLEARANCE_LABEL[c.classification]}</Badge>,
                access: MANAGER_ROLES.includes(c.role) ? (
                  <Button
                    type="button"
                    variant="secondary"
                    className="px-3 py-1 text-xs"
                    onClick={(event) => {
                      event.stopPropagation()
                      setManagingCaseId(c.case_id)
                      setManagingRole(c.role)
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
  actorRole,
  onClose,
  onMembershipChanged,
}: {
  caseId: string
  actorRole: CaseRole
  onClose: () => void
  onMembershipChanged: () => Promise<void>
}) {
  const [members, setMembers] = useState<CaseMemberDetailView[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [newDisplayName, setNewDisplayName] = useState('')
  const [newEmail, setNewEmail] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newRole, setNewRole] = useState<CaseRole>('investigator')
  const [newClearance, setNewClearance] = useState<ClearanceLevel>('restricted')
  const [memberSearch, setMemberSearch] = useState('')
  const assignableRoles = actorRole === 'case_owner' ? OWNER_ASSIGNABLE_ROLES : MANAGER_ASSIGNABLE_ROLES
  const currentUserId = useAuthStore((state) => state.user?.user_id)

  async function load() {
    setLoading(true)
    setError(null)
    try {
      const memberResponse = await casesApi.listMembers(caseId)
      setMembers(memberResponse.items)
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

  async function createTeamMember(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setSuccess(null)
    if (newPassword.length < 10) {
      setError('Temporary password must be at least 10 characters.')
      return
    }
    setSubmitting(true)
    try {
      await casesApi.createTeamMember(caseId, {
        display_name: newDisplayName,
        email: newEmail,
        password: newPassword,
        role: newRole,
        clearance: newClearance,
      })
      setNewDisplayName('')
      setNewEmail('')
      setNewPassword('')
      setSuccess('Team Member created and assigned to this case. They must change their password and enroll MFA at first login.')
      await Promise.all([load(), onMembershipChanged()])
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to create this Team Member.')
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

  const visibleMembers = members.filter((member) => {
    const query = memberSearch.trim().toLocaleLowerCase()
    return !query || `${member.display_name} ${member.email_normalized}`.toLocaleLowerCase().includes(query)
  })

  return (
    <section className="rounded-card border border-card-border bg-card p-5">
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-text">Case access / manage members</h2>
          <p className="text-sm text-text-dim">Case roles and clearance apply only to this case. Organisation roles cannot be granted here.</p>
        </div>
        <Button type="button" variant="ghost" onClick={onClose}>Close</Button>
      </div>
      {error ? <ErrorState message={error} /> : null}
      {success ? <p className="mb-3 rounded-control border border-palm/30 bg-palm/10 px-3 py-2 text-sm text-palm">{success}</p> : null}
      {loading ? <LoadingState label="Loading case members…" /> : error ? null : (
        <>
          <form className="mb-5 space-y-4 rounded-control border border-card-border p-4" onSubmit={createTeamMember}>
            <div>
              <h3 className="text-base font-semibold text-text">Create Team Member</h3>
              <p className="mt-1 text-sm text-text-dim">
                This creates an ordinary account and assigns it only to this case.
              </p>
            </div>
            <div className="grid gap-4 md:grid-cols-5">
              <label className="flex flex-col gap-1.5 text-sm font-medium text-text">
                Team Member name
                <input
                  required
                  value={newDisplayName}
                  maxLength={200}
                  onChange={(event) => setNewDisplayName(event.target.value)}
                  placeholder="e.g. Anika Rao"
                  autoComplete="name"
                  className="rounded-control border border-card-border bg-canvas px-3 py-2 text-sm font-normal text-text"
                />
              </label>
              <label className="flex flex-col gap-1.5 text-sm font-medium text-text">
                Work email
                <input
                  required
                  type="email"
                  value={newEmail}
                  onChange={(event) => setNewEmail(event.target.value)}
                  placeholder="name@organization.example"
                  autoComplete="email"
                  className="rounded-control border border-card-border bg-canvas px-3 py-2 text-sm font-normal text-text"
                />
              </label>
              <label className="flex flex-col gap-1.5 text-sm font-medium text-text">
                Temporary password
                <input
                  required
                  type="password"
                  minLength={10}
                  value={newPassword}
                  onChange={(event) => setNewPassword(event.target.value)}
                  placeholder="At least 10 characters"
                  autoComplete="new-password"
                  className="rounded-control border border-card-border bg-canvas px-3 py-2 text-sm font-normal text-text"
                />
              </label>
              <label className="flex flex-col gap-1.5 text-sm font-medium text-text">
                Case Role
                <RoleSelect value={newRole} roles={assignableRoles} onChange={setNewRole} />
              </label>
              <label className="flex flex-col gap-1.5 text-sm font-medium text-text">
                Clearance
                <ClearanceSelect value={newClearance} onChange={setNewClearance} />
                <span className="text-xs font-normal text-text-faint">Standard: normal investigation data. Sensitive: limited-access case data. Highly Sensitive: highest protection.</span>
              </label>
            </div>
            <Button type="submit" disabled={submitting}>Create Team Member</Button>
          </form>
          {members.length > 0 ? (
            <label className="mb-4 flex max-w-md items-center gap-2 rounded-control border border-card-border bg-canvas px-3 py-2 text-text-dim">
              <Search size={15} aria-hidden="true" />
              <span className="sr-only">Search Team Members</span>
              <input
                type="search"
                value={memberSearch}
                onChange={(event) => setMemberSearch(event.target.value)}
                placeholder="Search Team Members by name or email"
                className="w-full bg-transparent text-sm text-text placeholder:text-text-faint focus:outline-none"
              />
            </label>
          ) : null}
          {members.length === 0 ? <EmptyState title="No case members" description="Create a Team Member to begin." /> : (
            visibleMembers.length === 0 ? <EmptyState title="No Team Members match your search" description="Try a different name or email." /> : <div className="space-y-2">
              {visibleMembers.map((member) => <MemberRow key={member.user_id} member={member} isCurrentUser={member.user_id === currentUserId} roles={actorRole === 'case_owner' ? ['case_owner', ...OWNER_ASSIGNABLE_ROLES] : MANAGER_ASSIGNABLE_ROLES} disabled={submitting || (actorRole === 'case_manager' && MANAGER_ROLES.includes(member.role))} onSave={saveMember} onDeactivate={deactivate} />)}
            </div>
          )}
        </>
      )}
    </section>
  )
}

function RoleSelect({ value, roles, onChange }: { value: CaseRole; roles: CaseRole[]; onChange: (value: CaseRole) => void }) {
  return <select value={value} onChange={(event) => onChange(event.target.value as CaseRole)} className="rounded-control border border-card-border bg-canvas px-3 py-2 text-sm text-text">{roles.map((item) => <option key={item} value={item}>{item.replace(/_/g, ' ')}</option>)}</select>
}

function ClearanceSelect({ value, onChange }: { value: ClearanceLevel; onChange: (value: ClearanceLevel) => void }) {
  return <select aria-label="Clearance" value={value} onChange={(event) => onChange(event.target.value as ClearanceLevel)} className="rounded-control border border-card-border bg-canvas px-3 py-2 text-sm text-text">{CLEARANCES.map((item) => <option key={item} value={item}>{CLEARANCE_LABEL[item]}</option>)}</select>
}

function MemberRow({ member, isCurrentUser, roles, disabled, onSave, onDeactivate }: { member: CaseMemberDetailView; isCurrentUser: boolean; roles: CaseRole[]; disabled: boolean; onSave: (member: CaseMemberDetailView, role: CaseRole, clearance: ClearanceLevel) => Promise<void>; onDeactivate: (member: CaseMemberDetailView) => Promise<void> }) {
  const [role, setRole] = useState(member.role)
  const [clearance, setClearance] = useState(member.clearance)
  if (isCurrentUser) {
    return <div className="rounded-control border border-card-border p-3"><p className="text-sm font-medium text-text">{member.display_name}</p><p className="text-xs text-text-dim">{member.email_normalized}</p></div>
  }
  return <div className="grid items-center gap-2 rounded-control border border-card-border p-3 md:grid-cols-[minmax(0,1fr)_10rem_10rem_auto]">
    <div><p className="text-sm font-medium text-text">{member.display_name} {!member.is_active ? <span className="text-text-faint">(inactive)</span> : null}</p><p className="text-xs text-text-dim">{member.email_normalized}</p></div>
    <RoleSelect value={role} roles={roles.includes(role) ? roles : [role]} onChange={setRole} />
    <ClearanceSelect value={clearance} onChange={setClearance} />
    <div className="flex gap-2"><Button type="button" variant="secondary" disabled={disabled || !member.is_active} onClick={() => void onSave(member, role, clearance)}>Save</Button><Button type="button" variant="ghost" disabled={disabled || !member.is_active} onClick={() => void onDeactivate(member)}>Deactivate</Button></div>
  </div>
}

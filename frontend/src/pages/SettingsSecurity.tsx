import { type FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { KeyRound, Search, ShieldCheck, UserPlus } from 'lucide-react'
import { Badge } from '../components/Badge'
import { Button } from '../components/Button'
import { Card } from '../components/Card'
import { ApiError, provisioningApi } from '../lib/api/client'
import type { PublicUser } from '../lib/api/types'
import { useAuthStore } from '../lib/auth/store'

function organizationRoleLabel(role: PublicUser['system_role']): string {
  if (role === 'provisioner') return 'Provisioner'
  if (role === 'case_head') return 'Case Head'
  return 'Team Member'
}

function AccountPanel() {
  const user = useAuthStore((state) => state.user)
  const caseMemberships = useAuthStore((state) => state.caseMemberships)
  if (!user) return null

  return (
    <Card className="flex flex-col gap-4 p-6">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-text-faint">Your account</h2>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <Info label="Name" value={user.display_name} />
        <Info label="Email" value={user.email_normalized} mono />
        <div className="flex flex-col gap-1">
          <span className="text-xs text-text-faint">Two-factor authentication</span>
          <Badge tone={user.totp_enabled ? 'palm' : 'berry'}>
            {user.totp_enabled ? 'Enabled' : 'Not enabled'}
          </Badge>
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-xs text-text-faint">Organisation role</span>
          <Badge tone={user.system_role ? 'crimson' : 'steel-neutral'}>
            {organizationRoleLabel(user.system_role)}
          </Badge>
        </div>
      </div>
      {user.system_role === 'provisioner' ? (
        <p className="text-sm text-text-dim">
          Provisioner access is limited to account and Case Head management. It does not grant
          access to cases, evidence, graphs, or investigative records.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          <span className="text-xs text-text-faint">Case access</span>
          {caseMemberships.length === 0 ? (
            <span className="text-sm text-text-dim">No case memberships yet.</span>
          ) : (
            <div className="flex flex-wrap gap-2">
              {caseMemberships.map((membership) => (
                <Badge key={membership.case_id} tone="steel-neutral">
                  {membership.role.replace(/_/g, ' ')} &middot; {membership.clearance}
                </Badge>
              ))}
            </div>
          )}
        </div>
      )}
    </Card>
  )
}

function Info({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs text-text-faint">{label}</span>
      <span className={`text-sm font-medium text-text${mono ? ' font-mono' : ''}`}>{value}</span>
    </div>
  )
}

function CreateCaseHead({ onCreated }: { onCreated: (user: PublicUser) => void }) {
  const [email, setEmail] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setSuccess(null)
    setSubmitting(true)
    try {
      const created = await provisioningApi.createCaseHead({
        email,
        display_name: displayName,
        password,
      })
      onCreated(created)
      setSuccess(`${created.display_name} was created as a Case Head.`)
      setEmail('')
      setDisplayName('')
      setPassword('')
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to create the Case Head.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Card className="flex flex-col gap-4 p-6">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 rounded-control bg-crimson/10 p-2 text-crimson">
          <UserPlus size={18} aria-hidden="true" />
        </span>
        <div>
          <h2 className="text-lg font-semibold text-text">Create a Case Head</h2>
          <p className="mt-1 text-sm text-text-dim">
            Create the organization-level account that will create and lead its own cases.
          </p>
        </div>
      </div>
      <div className="rounded-control border border-card-border bg-canvas/50 px-3 py-2.5 text-sm text-text-dim">
        <span className="font-medium text-text">Before you create the account:</span> choose a
        temporary password to share through an approved channel. The Case Head will be required to
        change it and complete MFA enrollment on first use. Case roles and clearance are assigned
        later inside each individual case.
      </div>
      <Notice error={error} success={success} />
      <form className="space-y-4" onSubmit={submit}>
        <div className="grid gap-4 md:grid-cols-3">
          <label className="flex flex-col gap-1.5 text-sm font-medium text-text">
            Case Head name
            <input
              required
              type="text"
              minLength={1}
              maxLength={200}
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              placeholder="e.g. Priya Sharma"
              autoComplete="name"
              className="rounded-control border border-card-border bg-canvas px-3 py-2 text-sm font-normal text-text"
            />
          </label>
          <label className="flex flex-col gap-1.5 text-sm font-medium text-text">
            Work email
            <input
              required
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
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
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="At least 10 characters"
              autoComplete="new-password"
              className="rounded-control border border-card-border bg-canvas px-3 py-2 text-sm font-normal text-text"
            />
          </label>
        </div>
        <Button type="submit" disabled={submitting} className="w-fit">
          {submitting ? 'Creating…' : 'Create Case Head'}
        </Button>
      </form>
    </Card>
  )
}

function Notice({ error, success }: { error: string | null; success: string | null }) {
  if (error) return <p className="rounded-control border border-crimson/30 bg-crimson/10 px-3 py-2 text-sm text-crimson">{error}</p>
  if (success) return <p className="rounded-control border border-palm/30 bg-palm/10 px-3 py-2 text-sm text-palm">{success}</p>
  return null
}

function CaseHeadsPanel() {
  const [caseHeads, setCaseHeads] = useState<PublicUser[]>([])
  const [searchQuery, setSearchQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [temporaryPassword, setTemporaryPassword] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await provisioningApi.listUsers()
      setCaseHeads(response.items)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to load Case Heads.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    // This effect synchronizes the Case Head directory with the authenticated
    // server state on mount; the loading transition is intentional.
    // oxlint-disable-next-line react/set-state-in-effect
    void load()
  }, [load])

  const visibleCaseHeads = useMemo(() => {
    const query = searchQuery.trim().toLocaleLowerCase()
    if (!query) return caseHeads
    return caseHeads.filter((caseHead) =>
      `${caseHead.display_name} ${caseHead.email_normalized}`.toLocaleLowerCase().includes(query),
    )
  }, [caseHeads, searchQuery])

  async function reset(userId: string) {
    setActionError(null)
    setTemporaryPassword(null)
    setBusyId(userId)
    try {
      const result = await provisioningApi.resetCaseHeadCredentials(userId)
      setTemporaryPassword(result.temporary_password)
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Unable to reset credentials.')
    } finally {
      setBusyId(null)
    }
  }

  async function toggle(user: PublicUser) {
    setActionError(null)
    setBusyId(user.user_id)
    try {
      const updated = await provisioningApi.updateCaseHeadStatus(user.user_id, { is_active: !user.is_active })
      setCaseHeads((current) => current.map((item) => (item.user_id === updated.user_id ? updated : item)))
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : 'Unable to update this Case Head.')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <CreateCaseHead onCreated={(user) => setCaseHeads((current) => [user, ...current])} />
      <Card className="flex flex-col gap-4 p-6">
        <div className="flex items-center gap-2">
          <ShieldCheck size={16} className="text-crimson" aria-hidden="true" />
          <h2 className="text-sm font-semibold uppercase tracking-wide text-text-faint">Case Heads</h2>
        </div>
        <p className="text-xs text-text-dim">
          This account-only directory intentionally contains no case, evidence, graph, or
          audit-record details. Search is limited to Case Head names and email addresses.
        </p>
        <Notice error={error ?? actionError} success={temporaryPassword ? `New temporary password: ${temporaryPassword}` : null} />
        {loading ? <p className="text-sm text-text-dim">Loading Case Heads…</p> : null}
        {!loading && caseHeads.length > 0 ? (
          <label className="flex max-w-lg items-center gap-2 rounded-control border border-card-border bg-canvas px-3 py-2 text-text-dim">
            <Search size={15} aria-hidden="true" />
            <span className="sr-only">Search Case Heads</span>
            <input
              type="search"
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="Search Case Heads by name or email"
              className="w-full bg-transparent text-sm text-text placeholder:text-text-faint focus:outline-none"
            />
          </label>
        ) : null}
        {!loading && caseHeads.length === 0 ? <p className="text-sm text-text-dim">No Case Heads yet.</p> : null}
        {!loading && caseHeads.length > 0 && visibleCaseHeads.length === 0 ? (
          <p className="text-sm text-text-dim">No Case Heads match that search.</p>
        ) : null}
        {!loading ? <div className="space-y-2">{visibleCaseHeads.map((user) => (
          <div key={user.user_id} className="flex flex-wrap items-center justify-between gap-3 rounded-control border border-card-border p-3">
            <div><p className="text-sm font-medium text-text">{user.display_name}</p><p className="text-xs text-text-dim">{user.email_normalized}</p></div>
            <div className="flex items-center gap-2"><Badge tone={user.is_active ? 'palm' : 'steel-neutral'}>{user.is_active ? 'Active' : 'Inactive'}</Badge><Button type="button" variant="secondary" disabled={busyId === user.user_id || !user.is_active} onClick={() => void reset(user.user_id)}><KeyRound size={14} />Reset credentials</Button><Button type="button" variant="ghost" disabled={busyId === user.user_id} onClick={() => void toggle(user)}>{user.is_active ? 'Deactivate' : 'Activate'}</Button></div>
          </div>
        ))}</div> : null}
      </Card>
    </div>
  )
}

export function SettingsSecurity() {
  const user = useAuthStore((state) => state.user)
  const isProvisioner = user?.system_role === 'provisioner'
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold text-text">
          {isProvisioner ? 'Provisioning Console' : 'Settings / Security'}
        </h1>
        <p className="text-sm text-text-dim">
          {isProvisioner
            ? 'Manage your Provisioner account and Case Head accounts.'
            : 'Profile, MFA, and account security.'}
        </p>
      </div>
      <AccountPanel />
      {isProvisioner ? <CaseHeadsPanel /> : null}
    </div>
  )
}

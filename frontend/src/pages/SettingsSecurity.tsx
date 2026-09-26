import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { AlertCircle, CheckCircle2, KeyRound, ShieldCheck, UserPlus } from 'lucide-react'
import { adminApi, ApiError } from '../lib/api/client'
import type { PublicUser } from '../lib/api/types'
import { useAuthStore } from '../lib/auth/store'
import { useIsSystemAdmin } from '../lib/auth/permissions'
import { Badge } from '../components/Badge'
import { Button } from '../components/Button'
import { Card } from '../components/Card'

function AccountPanel() {
  const user = useAuthStore((state) => state.user)
  const caseMemberships = useAuthStore((state) => state.caseMemberships)
  if (!user) return null

  return (
    <Card className="flex flex-col gap-4 p-6">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-text-faint">Your account</h2>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="flex flex-col gap-0.5">
          <span className="text-xs text-text-faint">Name</span>
          <span className="text-sm font-medium text-text">{user.display_name}</span>
        </div>
        <div className="flex flex-col gap-0.5">
          <span className="text-xs text-text-faint">Email</span>
          <span className="font-mono text-sm text-text">{user.email_normalized}</span>
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-xs text-text-faint">Two-factor authentication</span>
          <Badge tone={user.totp_enabled ? 'palm' : 'berry'}>
            {user.totp_enabled ? 'Enabled' : 'Not enabled'}
          </Badge>
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-xs text-text-faint">System role</span>
          <Badge tone={user.system_role === 'admin' ? 'crimson' : 'steel-neutral'}>
            {user.system_role === 'admin' ? 'Administrator' : 'Standard'}
          </Badge>
        </div>
      </div>
      <div className="flex flex-col gap-2">
        <span className="text-xs text-text-faint">Case access</span>
        {caseMemberships.length === 0 ? (
          <span className="text-sm text-text-dim">No case memberships yet.</span>
        ) : (
          <div className="flex flex-wrap gap-2">
            {caseMemberships.map((m) => (
              <Badge key={m.case_id} tone="steel-neutral">
                {m.role.replace(/_/g, ' ')} &middot; {m.clearance}
              </Badge>
            ))}
          </div>
        )}
      </div>
    </Card>
  )
}

function AdminCreateAccountPanel({ onCreated }: { onCreated: (user: PublicUser) => void }) {
  const [email, setEmail] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [asAdmin, setAsAdmin] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [created, setCreated] = useState<{ userId: string; email: string } | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const user = await adminApi.provisionUser({
        email,
        password,
        display_name: displayName,
        system_role: asAdmin ? 'admin' : null,
      })
      setCreated({ userId: user.user_id, email: user.email_normalized })
      onCreated(user)
      setEmail('')
      setDisplayName('')
      setPassword('')
      setAsAdmin(false)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to reach the server.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Card className="flex flex-col gap-4 p-6">
      <div className="flex items-center gap-2">
        <UserPlus size={16} className="text-text-faint" aria-hidden="true" />
        <h2 className="text-sm font-semibold uppercase tracking-wide text-text-faint">
          Create user account
        </h2>
      </div>
      <p className="text-xs text-text-dim">
        The account is created with a forced password change and TOTP enrollment on first login.
        Share the password below with the investigator through a channel outside this app -- it
        will not be shown again.
      </p>

      {error ? (
        <div className="flex items-start gap-2 rounded-control border border-crimson/30 bg-crimson/10 px-3 py-2 text-xs text-crimson">
          <AlertCircle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span>{error}</span>
        </div>
      ) : null}

      {created ? (
        <div className="flex items-start gap-2 rounded-control border border-palm/30 bg-palm/10 px-3 py-2 text-xs text-palm">
          <CheckCircle2 size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span>
            Account created for {created.email}. User ID:{' '}
            <span className="font-mono">{created.userId}</span>
          </span>
        </div>
      ) : null}

      <form className="flex flex-col gap-3" onSubmit={handleSubmit}>
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="font-medium text-text-dim">Email</span>
          <input
            type="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className="rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-crimson/40"
          />
        </label>
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="font-medium text-text-dim">Display name</span>
          <input
            type="text"
            required
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            className="rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-crimson/40"
          />
        </label>
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="font-medium text-text-dim">Initial password (min. 10 characters)</span>
          <input
            type="text"
            required
            minLength={10}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="rounded-control border border-card-border bg-card px-3 py-2 font-mono text-sm text-text focus:outline-none focus:ring-2 focus:ring-crimson/40"
          />
        </label>
        <label className="flex items-center gap-2 text-sm text-text-dim">
          <input
            type="checkbox"
            checked={asAdmin}
            onChange={(event) => setAsAdmin(event.target.checked)}
            className="h-4 w-4 rounded border-card-border"
          />
          Grant system administrator access
        </label>
        <Button type="submit" disabled={submitting} className="mt-1 w-fit">
          {submitting ? 'Creating...' : 'Create account'}
        </Button>
      </form>
    </Card>
  )
}

interface AdminResetCredentialsPanelProps {
  users: PublicUser[]
  usersLoading: boolean
  usersError: string | null
  prefillUserId: string
}

function AdminResetCredentialsPanel({
  users,
  usersLoading,
  usersError,
  prefillUserId,
}: AdminResetCredentialsPanelProps) {
  const [userId, setUserId] = useState(prefillUserId)
  const [syncedPrefillUserId, setSyncedPrefillUserId] = useState(prefillUserId)
  const [armed, setArmed] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  // Adjust state during render (React's documented pattern for "sync local
  // state to a prop, but still allow local edits") rather than an effect --
  // the parent's `prefillUserId` only changes right after a new account is
  // created, and the investigator can still freely pick a different account
  // from the dropdown afterward without this overwriting that choice.
  //
  // Guarded on the ID actually being present in `users`: a plain `<select
  // value={userId}>` with a `value` that matches no `<option>` doesn't stay
  // blank -- the browser silently falls back to whatever option happens to
  // render first. Without this guard, a newly-created account's ID (not yet
  // fetched back if it falls outside the paginated `users` list) would
  // silently select a *different, unrelated* account with no visible error
  // -- and an admin trusting the pre-fill could reset the wrong person's
  // credentials. The parent already prepends the just-created user into
  // `users` before this ID is ever passed down, so this guard is normally
  // satisfied; it stays as the hard backstop against exactly that failure
  // mode if that ever stops being true.
  if (prefillUserId !== syncedPrefillUserId) {
    setSyncedPrefillUserId(prefillUserId)
    if (prefillUserId && users.some((u) => u.user_id === prefillUserId)) {
      setUserId(prefillUserId)
    }
  }

  // The single source of truth for "is a real, currently-listed account
  // selected" -- used by the `<select>`'s own `value`, the reset button's
  // disabled state, and the reset call itself, so all three can never
  // disagree about what's actually selected.
  const selectedUserId = users.some((u) => u.user_id === userId) ? userId : ''

  async function handleReset() {
    if (!armed) {
      setArmed(true)
      return
    }
    setError(null)
    setResult(null)
    setSubmitting(true)
    try {
      const response = await adminApi.resetCredentials(selectedUserId)
      setResult(response.temporary_password)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to reach the server.')
    } finally {
      setSubmitting(false)
      setArmed(false)
    }
  }

  return (
    <Card className="flex flex-col gap-4 p-6">
      <div className="flex items-center gap-2">
        <KeyRound size={16} className="text-text-faint" aria-hidden="true" />
        <h2 className="text-sm font-semibold uppercase tracking-wide text-text-faint">
          Lost-device / lost-password recovery
        </h2>
      </div>
      <p className="text-xs text-text-dim">
        Reissues a one-time temporary password and clears TOTP enrollment for the selected account
        -- the investigator re-enrolls from scratch on next login, and every active session for
        that account is revoked immediately.
      </p>

      {error ? (
        <div className="flex items-start gap-2 rounded-control border border-crimson/30 bg-crimson/10 px-3 py-2 text-xs text-crimson">
          <AlertCircle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span>{error}</span>
        </div>
      ) : null}
      {usersError ? (
        <div className="flex items-start gap-2 rounded-control border border-crimson/30 bg-crimson/10 px-3 py-2 text-xs text-crimson">
          <AlertCircle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span>Could not load accounts: {usersError}</span>
        </div>
      ) : null}
      {result ? (
        <div className="flex items-start gap-2 rounded-control border border-palm/30 bg-palm/10 px-3 py-2 text-xs text-palm">
          <CheckCircle2 size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span>
            New temporary password: <span className="font-mono">{result}</span>
          </span>
        </div>
      ) : null}

      <label className="flex flex-col gap-1.5 text-sm">
        <span className="font-medium text-text-dim">Account</span>
        <select
          required
          // Defensive: a `value` matching no `<option>` makes the browser
          // silently select whatever option renders first, not blank -- see
          // the render-time sync's comment above for why that's dangerous
          // here. `selectedUserId` is '' whenever `userId` isn't actually in
          // `users`, so "no real match" always reads as "nothing selected,"
          // never as a wrong, unnoticed pick.
          value={selectedUserId}
          onChange={(event) => {
            setUserId(event.target.value)
            setArmed(false)
          }}
          disabled={usersLoading || users.length === 0}
          className="rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-crimson/40 disabled:opacity-50"
        >
          <option value="" disabled>
            {usersLoading
              ? 'Loading accounts...'
              : users.length === 0
                ? 'No accounts found'
                : 'Select an account'}
          </option>
          {users.map((u) => (
            <option key={u.user_id} value={u.user_id}>
              {u.display_name} &lt;{u.email_normalized}&gt;
              {u.system_role === 'admin' ? ' (admin)' : ''}
            </option>
          ))}
        </select>
      </label>
      <Button
        type="button"
        variant="secondary"
        disabled={submitting || !selectedUserId}
        onClick={handleReset}
        className="w-fit"
      >
        {submitting ? 'Resetting...' : armed ? 'Confirm reset' : 'Reset credentials'}
      </Button>
    </Card>
  )
}

/**
 * Page 16 -- Settings / Security. The Admin-only sub-section is gated on
 * `system_role === 'admin'` (RBAC, Section 6): not shown at all to anyone
 * else, never a disabled control that errors after a click.
 */
export function SettingsSecurity() {
  const isAdmin = useIsSystemAdmin()
  const [lastCreatedUserId, setLastCreatedUserId] = useState('')
  const [users, setUsers] = useState<PublicUser[]>([])
  const [usersLoading, setUsersLoading] = useState(false)
  const [usersError, setUsersError] = useState<string | null>(null)

  const loadUsers = useCallback(() => {
    if (!isAdmin) return
    setUsersLoading(true)
    setUsersError(null)
    adminApi
      .listUsers()
      .then((response) => setUsers(response.items))
      .catch((err) => setUsersError(err instanceof ApiError ? err.message : 'Unable to reach the server.'))
      .finally(() => setUsersLoading(false))
  }, [isAdmin])

  useEffect(() => {
    // Fetch-on-mount with loading/error state: `setUsersLoading`/
    // `setUsersError` run synchronously the moment this effect fires, which
    // is the standard, correct shape for this pattern (there is no prop or
    // derivable value to compute instead) -- not the "should have been
    // computed during render" case this lint rule targets.
    // oxlint-disable-next-line react/set-state-in-effect
    loadUsers()
  }, [loadUsers])

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold text-text">Settings / Security</h1>
        <p className="text-sm text-text-dim">MFA, roles, permissions, and account security.</p>
      </div>

      <AccountPanel />

      {isAdmin ? (
        <div className="flex flex-col gap-4">
          <div className="flex items-center gap-2">
            <ShieldCheck size={16} className="text-crimson" aria-hidden="true" />
            <h2 className="text-sm font-semibold uppercase tracking-wide text-crimson">
              Admin-only
            </h2>
          </div>
          <AdminCreateAccountPanel
            onCreated={(user) => {
              setLastCreatedUserId(user.user_id)
              // Prepend directly rather than re-fetching: `listUsers` is
              // oldest-first and capped at 200, so on a database with more
              // than 200 pre-existing accounts (true even in this dev
              // environment's own test fixtures), a fresh page would never
              // actually contain the account that was just created --
              // silently dropping it from the picker with no error. See
              // `AdminResetCredentialsPanel`'s render-time sync guard for
              // what happens if an ID it's told to pre-select isn't
              // actually present in the list.
              setUsers((current) => [user, ...current])
            }}
          />
          <AdminResetCredentialsPanel
            users={users}
            usersLoading={usersLoading}
            usersError={usersError}
            prefillUserId={lastCreatedUserId}
          />
        </div>
      ) : null}
    </div>
  )
}

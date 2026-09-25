import { useState } from 'react'
import type { FormEvent } from 'react'
import { AlertCircle, KeyRound, Lock } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { ApiError, authApi } from '../lib/api/client'
import { useAuthStore } from '../lib/auth/store'
import { Button } from '../components/Button'
import { Card } from '../components/Card'

/**
 * Forced first-login password change (Section 6): reachable immediately
 * after a first successful login on an admin-provisioned account, before
 * anything else in the app. Not skippable -- `RequireAuth` redirects back
 * here for as long as `must_change_password` is true.
 */
export function ForcePasswordChange() {
  const navigate = useNavigate()
  const user = useAuthStore((state) => state.user)
  const setIdentity = useAuthStore((state) => state.setIdentity)
  const caseMemberships = useAuthStore((state) => state.caseMemberships)

  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    if (newPassword !== confirmPassword) {
      setError('New password and confirmation do not match.')
      return
    }
    if (newPassword.length < 10) {
      setError('New password must be at least 10 characters.')
      return
    }
    setSubmitting(true)
    try {
      await authApi.changePassword(currentPassword, newPassword)
      const me = await authApi.me()
      setIdentity(me.user, caseMemberships)
      navigate(me.user.totp_enabled ? '/dashboard' : '/mfa/enroll', { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to reach the server.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-shell-topbar px-4 text-shell-text">
      <Card className="w-full max-w-sm !border-shell-text-dim/15 !bg-shell-nav p-8">
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <span className="flex h-10 w-10 items-center justify-center rounded-control bg-crimson text-base font-bold text-shell-text">
            <KeyRound size={18} aria-hidden="true" />
          </span>
          <h1 className="text-lg font-semibold text-shell-text">Set a new password</h1>
          <p className="text-xs text-shell-text-dim">
            {user?.display_name ?? 'Your account'} was created with a temporary password. Choose a
            new one before continuing.
          </p>
        </div>

        {error ? (
          <div className="mb-4 flex items-start gap-2 rounded-control border border-crimson/30 bg-crimson/10 px-3 py-2 text-xs text-crimson">
            <AlertCircle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
            <span>{error}</span>
          </div>
        ) : null}

        <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
          <label className="flex flex-col gap-1.5 text-sm">
            <span className="font-medium text-shell-text-dim">Temporary password</span>
            <span className="flex items-center gap-2 rounded-control border border-shell-text-dim/25 bg-black/10 px-3 py-2">
              <Lock size={16} className="shrink-0 text-shell-text-dim" aria-hidden="true" />
              <input
                type="password"
                autoComplete="current-password"
                required
                value={currentPassword}
                onChange={(event) => setCurrentPassword(event.target.value)}
                className="w-full bg-transparent text-shell-text placeholder:text-shell-text-dim/60 focus:outline-none"
              />
            </span>
          </label>

          <label className="flex flex-col gap-1.5 text-sm">
            <span className="font-medium text-shell-text-dim">New password</span>
            <span className="flex items-center gap-2 rounded-control border border-shell-text-dim/25 bg-black/10 px-3 py-2">
              <Lock size={16} className="shrink-0 text-shell-text-dim" aria-hidden="true" />
              <input
                type="password"
                autoComplete="new-password"
                required
                minLength={10}
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
                className="w-full bg-transparent text-shell-text placeholder:text-shell-text-dim/60 focus:outline-none"
              />
            </span>
          </label>

          <label className="flex flex-col gap-1.5 text-sm">
            <span className="font-medium text-shell-text-dim">Confirm new password</span>
            <span className="flex items-center gap-2 rounded-control border border-shell-text-dim/25 bg-black/10 px-3 py-2">
              <Lock size={16} className="shrink-0 text-shell-text-dim" aria-hidden="true" />
              <input
                type="password"
                autoComplete="new-password"
                required
                minLength={10}
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                className="w-full bg-transparent text-shell-text placeholder:text-shell-text-dim/60 focus:outline-none"
              />
            </span>
          </label>

          <Button type="submit" disabled={submitting} className="mt-2 w-full">
            {submitting ? 'Saving...' : 'Set password and continue'}
          </Button>
        </form>
      </Card>
    </div>
  )
}

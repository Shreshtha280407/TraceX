import { useState } from 'react'
import type { FormEvent } from 'react'
import { AlertCircle, ArrowLeft, KeyRound, Lock, User } from 'lucide-react'
import type { Location } from 'react-router-dom'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { ApiError, authApi } from '../lib/api/client'
import { useAuthStore } from '../lib/auth/store'
import { Button } from '../components/Button'
import { Card } from '../components/Card'

type LoginStep = 'credentials' | 'mfa'

/**
 * Page 1 -- Login / MFA. Wired end-to-end to the real backend (Section 6):
 * single-factor login when the account has no TOTP enrolled yet, a real
 * two-factor challenge once it does. Never pre-fills or auto-submits
 * anything; a wrong password/code always renders the same generic denial
 * the backend returns (never distinguishing "no such account").
 */
export function Login() {
  const navigate = useNavigate()
  const location = useLocation()
  const setTokens = useAuthStore((state) => state.setTokens)
  const setIdentity = useAuthStore((state) => state.setIdentity)

  const [step, setStep] = useState<LoginStep>('credentials')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [mfaToken, setMfaToken] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function completeSession(accessToken: string, refreshToken: string) {
    setTokens({ accessToken, refreshToken })
    const me = await authApi.me()
    setIdentity(me.user, me.case_memberships)
    const redirectTo = (location.state as { from?: Location } | null)?.from ?? null
    if (me.user.must_change_password) navigate('/force-password-change', { replace: true })
    else if (!me.user.totp_enabled) navigate('/mfa/enroll', { replace: true })
    else navigate(redirectTo?.pathname ?? '/dashboard', { replace: true })
  }

  async function handleCredentialsSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const result = await authApi.login(email, password)
      if (result.mfa_required && result.mfa_token) {
        setMfaToken(result.mfa_token)
        setStep('mfa')
      } else if (result.access_token && result.refresh_token) {
        await completeSession(result.access_token, result.refresh_token)
      } else {
        setError('Unexpected response from the server.')
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to reach the server.')
    } finally {
      setSubmitting(false)
    }
  }

  async function handleMfaSubmit(event: FormEvent) {
    event.preventDefault()
    if (!mfaToken) return
    setError(null)
    setSubmitting(true)
    try {
      const result = await authApi.mfaLoginVerify(mfaToken, code)
      if (result.access_token && result.refresh_token) {
        await completeSession(result.access_token, result.refresh_token)
      } else {
        setError('Unexpected response from the server.')
      }
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
            T
          </span>
          <h1 className="text-lg font-semibold text-shell-text">Sign in to TraceX</h1>
          <p className="text-xs text-shell-text-dim">
            Accounts are provisioned by your organization. Two-factor authentication is required.
          </p>
        </div>

        {error ? (
          <div className="mb-4 flex items-start gap-2 rounded-control border border-crimson/30 bg-crimson/10 px-3 py-2 text-xs text-crimson">
            <AlertCircle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
            <span>{error}</span>
          </div>
        ) : null}

        {step === 'credentials' ? (
          <form key="credentials" className="flex flex-col gap-4" onSubmit={handleCredentialsSubmit}>
            <label className="flex flex-col gap-1.5 text-sm">
              <span className="font-medium text-shell-text-dim">Email</span>
              <span className="flex items-center gap-2 rounded-control border border-shell-text-dim/25 bg-black/10 px-3 py-2">
                <User size={16} className="shrink-0 text-shell-text-dim" aria-hidden="true" />
                <input
                  type="email"
                  name="email"
                  autoComplete="username"
                  required
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  className="w-full bg-transparent text-shell-text placeholder:text-shell-text-dim/60 focus:outline-none"
                  placeholder="investigator@tracex.example"
                />
              </span>
            </label>

            <label className="flex flex-col gap-1.5 text-sm">
              <span className="font-medium text-shell-text-dim">Password</span>
              <span className="flex items-center gap-2 rounded-control border border-shell-text-dim/25 bg-black/10 px-3 py-2">
                <Lock size={16} className="shrink-0 text-shell-text-dim" aria-hidden="true" />
                <input
                  type="password"
                  name="password"
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  className="w-full bg-transparent text-shell-text placeholder:text-shell-text-dim/60 focus:outline-none"
                  placeholder="********"
                />
              </span>
            </label>

            <Button type="submit" disabled={submitting} className="mt-2 w-full">
              {submitting ? 'Signing in...' : 'Continue'}
            </Button>
          </form>
        ) : (
          <form key="mfa" className="flex flex-col gap-4" onSubmit={handleMfaSubmit}>
            <button
              type="button"
              onClick={() => {
                setStep('credentials')
                setMfaToken(null)
                setCode('')
                setError(null)
              }}
              className="flex w-fit items-center gap-1.5 text-xs font-medium text-shell-text-dim hover:text-shell-text"
            >
              <ArrowLeft size={14} aria-hidden="true" />
              Back
            </button>

            <label className="flex flex-col gap-1.5 text-sm">
              <span className="font-medium text-shell-text-dim">Authenticator code</span>
              <span className="flex items-center gap-2 rounded-control border border-shell-text-dim/25 bg-black/10 px-3 py-2">
                <KeyRound size={16} className="shrink-0 text-shell-text-dim" aria-hidden="true" />
                <input
                  type="text"
                  inputMode="numeric"
                  pattern="[0-9]{6}"
                  maxLength={6}
                  autoComplete="one-time-code"
                  required
                  value={code}
                  onChange={(event) => setCode(event.target.value.replace(/\D/g, ''))}
                  className="w-full bg-transparent font-mono tracking-[0.4em] text-shell-text placeholder:text-shell-text-dim/60 placeholder:tracking-normal focus:outline-none"
                  placeholder="6-digit code"
                />
              </span>
              <span className="text-xs text-shell-text-dim">
                Enter the 6-digit code from your authenticator app.
              </span>
            </label>

            <Button type="submit" disabled={submitting || code.length !== 6} className="mt-2 w-full">
              {submitting ? 'Verifying...' : 'Verify'}
            </Button>
          </form>
        )}

        <div className="mt-6 border-t border-shell-text-dim/15 pt-4 text-center">
          <Link to="/" className="text-xs font-medium text-shell-text-dim hover:text-shell-text">
            &larr; Back to TraceX
          </Link>
        </div>
      </Card>
    </div>
  )
}

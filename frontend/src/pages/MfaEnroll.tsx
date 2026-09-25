import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { AlertCircle, KeyRound, ShieldCheck } from 'lucide-react'
import { QRCodeSVG } from 'qrcode.react'
import { useNavigate } from 'react-router-dom'
import { ApiError, authApi } from '../lib/api/client'
import { useAuthStore } from '../lib/auth/store'
import { Button } from '../components/Button'
import { Card } from '../components/Card'

/**
 * TOTP enrollment (Section 6): shown once, on first login, if the account
 * has not already been enrolled via an admin-issued QR code. Calls the real
 * `/auth/mfa/enroll` + `/auth/mfa/verify` endpoints -- the QR/secret is
 * never re-fetchable once this screen is left, exactly like the backend's
 * one-time-display contract for it.
 */
export function MfaEnroll() {
  const navigate = useNavigate()
  const setIdentity = useAuthStore((state) => state.setIdentity)
  const caseMemberships = useAuthStore((state) => state.caseMemberships)

  const [enrollment, setEnrollment] = useState<{ secret: string; provisioningUri: string } | null>(
    null,
  )
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const enrollRequested = useRef(false)

  useEffect(() => {
    // `/mfa/enroll` is non-idempotent -- every call regenerates the secret,
    // overwriting whatever was pending server-side. React 18 StrictMode
    // intentionally mounts, cleans up, and remounts effects once in
    // development; a `cancelled`-on-cleanup guard here would make the
    // *first* (StrictMode-cleaned-up) call's response silently ignored
    // even when it's the only real call to ever fire, permanently stuck on
    // "Generating your secret...". The ref instead prevents the request
    // itself from ever firing twice, so exactly one call happens and its
    // response always applies -- no separate cancellation needed.
    if (enrollRequested.current) return
    enrollRequested.current = true
    authApi
      .mfaEnroll()
      .then((response) => {
        setEnrollment({ secret: response.secret, provisioningUri: response.provisioning_uri })
      })
      .catch((err) => {
        setError(err instanceof ApiError ? err.message : 'Unable to reach the server.')
      })
      .finally(() => {
        setLoading(false)
      })
  }, [])

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const user = await authApi.mfaVerify(code)
      setIdentity(user, caseMemberships)
      navigate('/dashboard', { replace: true })
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
            <ShieldCheck size={18} aria-hidden="true" />
          </span>
          <h1 className="text-lg font-semibold text-shell-text">Set up two-factor authentication</h1>
          <p className="text-xs text-shell-text-dim">
            Scan this code with your authenticator app (Google Authenticator, 1Password, Authy...),
            then enter the 6-digit code it shows. It will not be shown again.
          </p>
        </div>

        {error ? (
          <div className="mb-4 flex items-start gap-2 rounded-control border border-crimson/30 bg-crimson/10 px-3 py-2 text-xs text-crimson">
            <AlertCircle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
            <span>{error}</span>
          </div>
        ) : null}

        {loading ? (
          <p className="py-8 text-center text-sm text-shell-text-dim">Generating your secret...</p>
        ) : enrollment ? (
          <>
            <div className="mb-4 flex justify-center rounded-control bg-white p-4">
              <QRCodeSVG value={enrollment.provisioningUri} size={168} />
            </div>
            <p className="mb-4 break-all text-center font-mono text-xs text-shell-text-dim">
              {enrollment.secret}
            </p>

            <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
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
              </label>

              <Button type="submit" disabled={submitting || code.length !== 6} className="mt-2 w-full">
                {submitting ? 'Confirming...' : 'Confirm and continue'}
              </Button>
            </form>
          </>
        ) : null}
      </Card>
    </div>
  )
}

import { type FormEvent, useEffect, useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { AlertCircle, ShieldCheck } from 'lucide-react'
import { Button } from '../components/Button'
import { Card } from '../components/Card'
import { ApiError, setupApi } from '../lib/api/client'

/** One-time private deployment ceremony, never a public signup screen. */
export function FirstAdminSetup() {
  const navigate = useNavigate()
  const [checking, setChecking] = useState(true)
  const [required, setRequired] = useState(false)
  const [organizationName, setOrganizationName] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [setupToken, setSetupToken] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    let live = true
    setupApi
      .status()
      .then((status) => {
        if (live) setRequired(status.setup_required)
      })
      .catch(() => {
        if (live) setRequired(false)
      })
      .finally(() => {
        if (live) setChecking(false)
      })
    return () => {
      live = false
    }
  }, [])

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    if (password !== confirmation) {
      setError('Password confirmation does not match.')
      return
    }
    if (!setupToken.trim()) {
      setError('The one-time deployment setup token is required.')
      return
    }
    setSubmitting(true)
    try {
      await setupApi.createFirstAdmin(
        { organization_name: organizationName, display_name: displayName, email, password },
        setupToken,
      )
      navigate('/login', { replace: true, state: { firstAdminCreated: true } })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Unable to reach the server.')
    } finally {
      // Do not retain secrets after either a successful or failed submission.
      setPassword('')
      setConfirmation('')
      setSetupToken('')
      setSubmitting(false)
    }
  }

  if (checking) {
    return <main className="mx-auto flex min-h-screen max-w-lg items-center px-6 text-sm text-text-dim">Checking deployment setup…</main>
  }
  if (!required) return <Navigate to="/login" replace />

  return (
    <main className="mx-auto flex min-h-screen max-w-lg items-center px-6 py-10">
      <Card className="w-full space-y-5 p-6">
        <div className="flex items-start gap-3">
          <ShieldCheck className="mt-0.5 text-crimson" aria-hidden="true" />
          <div>
            <h1 className="text-xl font-semibold text-text">Private deployment setup</h1>
            <p className="mt-1 text-sm text-text-dim">
              Create TraceX’s first Provisioner. This is a one-time organization setup,
              not public account registration.
            </p>
          </div>
        </div>
        {error ? (
          <div className="flex gap-2 rounded-control border border-crimson/30 bg-crimson/10 p-3 text-sm text-crimson">
            <AlertCircle size={16} className="shrink-0" aria-hidden="true" />
            {error}
          </div>
        ) : null}
        <form className="space-y-3" onSubmit={submit}>
          <SetupField label="Organization name" value={organizationName} setValue={setOrganizationName} />
          <SetupField label="Provisioner name" value={displayName} setValue={setDisplayName} />
          <SetupField label="Provisioner email" type="email" value={email} setValue={setEmail} />
          <SetupField label="Password (min. 10 characters)" type="password" value={password} setValue={setPassword} minLength={10} />
          <SetupField label="Confirm password" type="password" value={confirmation} setValue={setConfirmation} minLength={10} />
          <SetupField label="One-time deployment setup token" type="password" value={setupToken} setValue={setSetupToken} />
          <Button type="submit" disabled={submitting} className="w-full">
            {submitting ? 'Creating Provisioner…' : 'Create first Provisioner'}
          </Button>
        </form>
      </Card>
    </main>
  )
}

function SetupField({
  label,
  value,
  setValue,
  type = 'text',
  minLength,
}: {
  label: string
  value: string
  setValue: (value: string) => void
  type?: string
  minLength?: number
}) {
  return (
    <label className="flex flex-col gap-1.5 text-sm text-text-dim">
      <span className="font-medium">{label}</span>
      <input
        required
        type={type}
        value={value}
        minLength={minLength}
        autoComplete="off"
        onChange={(event) => setValue(event.target.value)}
        className="rounded-control border border-card-border bg-card px-3 py-2 text-text focus:outline-none focus:ring-2 focus:ring-crimson/40"
      />
    </label>
  )
}

import { useEffect, useState } from 'react'
import { ShieldCheck } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Button } from '../components/Button'
import { setupApi } from '../lib/api/client'

/**
 * Page 0 -- public entry surface. The deployment-only setup affordance is
 * discovered from the backend rather than inferred in the browser, so it
 * disappears immediately after the first active administrator is created.
 */
export function Landing() {
  const [setupRequired, setSetupRequired] = useState(false)
  const [checkingSetup, setCheckingSetup] = useState(true)

  useEffect(() => {
    let active = true
    setupApi
      .status()
      .then((status) => {
        if (active) setSetupRequired(status.setup_required)
      })
      // A status outage must never accidentally advertise a public signup.
      .catch(() => {
        if (active) setSetupRequired(false)
      })
      .finally(() => {
        if (active) setCheckingSetup(false)
      })
    return () => {
      active = false
    }
  }, [])

  const primaryPath = setupRequired ? '/setup/first-admin' : '/login'
  const primaryLabel = setupRequired ? 'Set up first administrator' : 'Sign in'

  return (
    <div className="flex min-h-screen flex-col bg-shell-topbar text-shell-text">
      <header className="flex items-center justify-between px-8 py-6">
        <div className="flex items-center gap-3">
          <span className="flex h-9 w-9 items-center justify-center rounded-control bg-crimson text-base font-bold">
            T
          </span>
          <span className="text-lg font-semibold">TraceX</span>
        </div>
        <Link to={primaryPath}>
          <Button variant="secondary" className="!text-shell-text !border-shell-text-dim/40 hover:!bg-white/5">
            {checkingSetup ? 'Loading…' : primaryLabel}
          </Button>
        </Link>
      </header>

      <main className="flex flex-1 flex-col items-center justify-center gap-8 px-6 text-center">
        <span className="flex h-16 w-16 items-center justify-center rounded-full bg-crimson/15 text-crimson">
          <ShieldCheck size={28} aria-hidden="true" />
        </span>
        <div className="flex max-w-xl flex-col gap-4">
          <h1 className="text-3xl font-semibold sm:text-4xl">
            Secure, evidence-first investigation platform
          </h1>
          <p className="text-base text-shell-text-dim">
            TraceX helps authorized investigators build a verifiable, auditable picture of an
            investigation from the evidence they collect -- with every finding traceable back to
            its source.
          </p>
        </div>
        <div className="flex flex-col items-center gap-3 sm:flex-row">
          <Link to={primaryPath}>
            <Button className="px-6 py-2.5 text-base" disabled={checkingSetup}>
              {checkingSetup ? 'Checking deployment…' : primaryLabel}
            </Button>
          </Link>
          {setupRequired ? (
            <span className="text-sm text-shell-text-dim">Private deployment only</span>
          ) : (
            <a href="mailto:access@tracex.example" className="text-sm font-medium text-shell-text-dim hover:text-shell-text">
              Request access &rarr;
            </a>
          )}
        </div>
      </main>

      <footer className="px-8 py-6 text-center text-xs text-shell-text-dim">
        {setupRequired
          ? 'First-administrator setup is a one-time private deployment ceremony, not public registration.'
          : 'Access is provisioned by your organization&apos;s administrator. There is no public self-registration.'}
      </footer>
    </div>
  )
}

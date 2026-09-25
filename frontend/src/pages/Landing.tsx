import { ShieldCheck } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Button } from '../components/Button'

/**
 * Page 0 -- public entry surface. No case data, no internal terminology.
 * UI only; Phase 1 wires the real sign-in flow behind "Sign in".
 */
export function Landing() {
  return (
    <div className="flex min-h-screen flex-col bg-shell-topbar text-shell-text">
      <header className="flex items-center justify-between px-8 py-6">
        <div className="flex items-center gap-3">
          <span className="flex h-9 w-9 items-center justify-center rounded-control bg-crimson text-base font-bold">
            T
          </span>
          <span className="text-lg font-semibold">TraceX</span>
        </div>
        <Link to="/login">
          <Button variant="secondary" className="!text-shell-text !border-shell-text-dim/40 hover:!bg-white/5">
            Sign in
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
          <Link to="/login">
            <Button className="px-6 py-2.5 text-base">Sign in</Button>
          </Link>
          <a href="mailto:access@tracex.example" className="text-sm font-medium text-shell-text-dim hover:text-shell-text">
            Request access &rarr;
          </a>
        </div>
      </main>

      <footer className="px-8 py-6 text-center text-xs text-shell-text-dim">
        Access is provisioned by your organization&apos;s administrator. There is no public
        self-registration.
      </footer>
    </div>
  )
}

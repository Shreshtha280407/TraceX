import { useState } from 'react'
import { CheckCircle2, HelpCircle, ShieldAlert, XCircle } from 'lucide-react'

export type Decision = 'verify' | 'reject' | 'needs_more_evidence'

interface DecisionBarProps {
  onDecide: (decision: Decision) => void | Promise<void>
  disabled?: boolean
}

const DECISIONS: {
  value: Decision
  label: string
  icon: typeof CheckCircle2
  classes: string
}[] = [
  {
    value: 'verify',
    label: 'Verify',
    icon: CheckCircle2,
    classes: 'bg-palm text-shell-text hover:bg-palm/90 border border-transparent',
  },
  {
    value: 'reject',
    label: 'Reject',
    icon: XCircle,
    classes: 'bg-transparent text-crimson border border-crimson/50 hover:bg-crimson/5',
  },
  {
    value: 'needs_more_evidence',
    label: 'Needs more evidence',
    icon: HelpCircle,
    classes: 'bg-transparent text-text-dim border border-card-border hover:bg-canvas/10',
  },
]

/**
 * The never-auto-merge house rule (Section 6/7), enforced once, here --
 * not as a per-page choice. Every merge/verify/reject decision on
 * Candidate Review and Hypotheses goes through this component:
 *
 * - Never pre-selected, never auto-submitted.
 * - A decision requires two deliberate clicks (arm, then confirm) --
 *   never a single accidental click.
 * - Always paired with the persistent audit-trail notice below, whether
 *   armed or not.
 */
export function DecisionBar({ onDecide, disabled = false }: DecisionBarProps) {
  const [armed, setArmed] = useState<Decision | null>(null)
  const [pending, setPending] = useState(false)

  async function handleClick(decision: Decision) {
    if (disabled || pending) return
    if (armed !== decision) {
      setArmed(decision)
      return
    }
    setPending(true)
    try {
      await onDecide(decision)
    } finally {
      setPending(false)
      setArmed(null)
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-2">
        {DECISIONS.map(({ value, label, icon: Icon, classes }) => (
          <button
            key={value}
            type="button"
            disabled={disabled || pending}
            onClick={() => handleClick(value)}
            className={`inline-flex items-center gap-2 rounded-control px-4 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-crimson ${classes} ${
              armed === value ? 'ring-2 ring-offset-1 ring-current' : ''
            }`}
          >
            <Icon size={16} aria-hidden="true" />
            {armed === value ? `Confirm: ${label}` : label}
          </button>
        ))}
      </div>
      <p className="flex items-start gap-1.5 text-xs text-text-faint">
        <ShieldAlert size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
        This decision is logged to the case audit trail and is never applied automatically --
        {armed ? ' click the highlighted button again to confirm.' : ' click a decision to review it before confirming.'}
      </p>
    </div>
  )
}

import { useState } from 'react'
import { ShieldAlert } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

export interface DecisionOption<T extends string> {
  value: T
  label: string
  icon: LucideIcon
  tone: 'palm' | 'crimson' | 'berry' | 'neutral'
}

const TONE_CLASSES: Record<DecisionOption<string>['tone'], string> = {
  palm: 'bg-palm text-shell-text hover:bg-palm/90 border border-transparent',
  crimson: 'bg-transparent text-crimson border border-crimson/50 hover:bg-crimson/5',
  berry: 'bg-transparent text-berry border border-berry/50 hover:bg-berry/5',
  neutral: 'bg-transparent text-text-dim border border-card-border hover:bg-canvas/10',
}

interface DecisionBarProps<T extends string> {
  options: DecisionOption<T>[]
  onDecide: (decision: T) => void | Promise<void>
  disabled?: boolean
}

/**
 * The never-auto-merge house rule (Section 6/7), enforced once, here -- not
 * as a per-page choice. Every review decision on Candidate Review and
 * Hypotheses goes through this component:
 *
 * - Never pre-selected, never auto-submitted.
 * - A decision requires two deliberate clicks (arm, then confirm) -- never
 *   a single accidental click.
 * - Always paired with the persistent audit-trail notice below.
 *
 * `options` is caller-supplied rather than a fixed three-button set: the
 * real backend has no single "review decision" vocabulary shared across
 * every reviewable thing. Correlation-candidate review and hypothesis
 * review each expose exactly two outcomes (`accepted_by_reviewer`/
 * `rejected_by_reviewer` -- see `review_models.py`/`hypothesis_models.py`'s
 * own docstrings: "never itself a verified identity... only records
 * whether a human accepted or rejected"). Entity-resolution review exposes
 * three (`verified_same`/`rejected`/`split`), where `split` only makes
 * sense once a pair is already `verified_same`. None of the three real
 * decision surfaces has a "needs more evidence" option -- an earlier
 * version of this component invented one that called no real endpoint;
 * this generalized shape is the fix.
 */
export function DecisionBar<T extends string>({ options, onDecide, disabled = false }: DecisionBarProps<T>) {
  const [armed, setArmed] = useState<T | null>(null)
  const [pending, setPending] = useState(false)

  async function handleClick(decision: T) {
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
        {options.map(({ value, label, icon: Icon, tone }) => (
          <button
            key={value}
            type="button"
            disabled={disabled || pending}
            onClick={() => handleClick(value)}
            className={`inline-flex items-center gap-2 rounded-control px-4 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-crimson ${TONE_CLASSES[tone]} ${
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

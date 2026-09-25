import type { ReactNode } from 'react'

export type BadgeTone = 'crimson' | 'berry' | 'palm' | 'steel-neutral'

interface BadgeProps {
  tone?: BadgeTone
  children: ReactNode
  className?: string
}

const TONE_CLASSES: Record<BadgeTone, string> = {
  crimson: 'bg-crimson/10 text-crimson',
  berry: 'bg-berry/10 text-berry',
  palm: 'bg-palm/10 text-palm',
  'steel-neutral': 'bg-steel-neutral/10 text-steel-neutral',
}

/**
 * Tinted background + matching tint-text, never a solid saturated fill --
 * that reads too bright against the mid-tone canvas (Section 4).
 */
export function Badge({ tone = 'steel-neutral', children, className = '' }: BadgeProps) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-pill px-2.5 py-0.5 text-xs font-medium ${TONE_CLASSES[tone]} ${className}`}
    >
      {children}
    </span>
  )
}

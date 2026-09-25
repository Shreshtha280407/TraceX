import { AlertCircle, Loader2 } from 'lucide-react'
import type { ReactNode } from 'react'
import { Card } from './Card'

/**
 * Real loading/error/empty states, used consistently across every list/detail
 * view (Section 8, Phase 2: "Every list/detail view has a real empty state,
 * a real loading state, and a real error state -- not just a happy-path
 * render."). Never a fixed or animated fake progress bar standing in for
 * real backend state.
 */
export function LoadingState({ label = 'Loading...' }: { label?: string }) {
  return (
    <Card className="flex items-center justify-center gap-2 p-8 text-sm text-text-dim">
      <Loader2 size={16} className="animate-spin" aria-hidden="true" />
      {label}
    </Card>
  )
}

export function ErrorState({ message }: { message: string }) {
  return (
    <Card className="flex items-start gap-2 border-crimson/30 bg-crimson/5 p-4 text-sm text-crimson">
      <AlertCircle size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
      <span>{message}</span>
    </Card>
  )
}

export function EmptyState({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return (
    <Card className="flex flex-col items-center gap-2 p-8 text-center">
      <span className="text-sm font-medium text-text">{title}</span>
      {description ? <span className="max-w-sm text-xs text-text-dim">{description}</span> : null}
      {action}
    </Card>
  )
}

/** A calm, expected "you don't have access to this" state -- never a generic crash screen (Section 6). */
export function ForbiddenState({ message = "You don't have access to this." }: { message?: string }) {
  return (
    <Card className="flex items-start gap-2 border-berry/30 bg-berry/5 p-4 text-sm text-berry">
      <AlertCircle size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
      <span>{message}</span>
    </Card>
  )
}

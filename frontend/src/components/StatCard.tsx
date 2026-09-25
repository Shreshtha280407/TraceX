import type { LucideIcon } from 'lucide-react'
import { Card } from './Card'

type DeltaTone = 'palm' | 'crimson' | 'berry' | 'steel-neutral'

interface StatCardProps {
  label: string
  value: string | number
  icon: LucideIcon
  subtext?: string
  deltaTone?: DeltaTone
}

const DELTA_CLASSES: Record<DeltaTone, string> = {
  palm: 'text-palm',
  crimson: 'text-crimson',
  berry: 'text-berry',
  'steel-neutral': 'text-text-faint',
}

/** Card surface, label + icon row, large number, small colored delta/subtext line (Section 4). */
export function StatCard({ label, value, icon: Icon, subtext, deltaTone = 'steel-neutral' }: StatCardProps) {
  return (
    <Card className="flex flex-col gap-3 p-5">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium uppercase tracking-wide text-text-faint">{label}</span>
        <Icon size={16} className="text-text-faint" aria-hidden="true" />
      </div>
      <span className="text-3xl font-semibold text-text">{value}</span>
      {subtext ? <span className={`text-xs font-medium ${DELTA_CLASSES[deltaTone]}`}>{subtext}</span> : null}
    </Card>
  )
}

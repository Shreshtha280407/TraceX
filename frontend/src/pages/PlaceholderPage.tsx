import { Badge } from '../components/Badge'
import { Card } from '../components/Card'
import type { PageDef } from '../lib/navigation'

interface PlaceholderPageProps {
  page: PageDef
}

/**
 * Phase 0 routing-shell stub. Every route is reachable and clickable from
 * the start; real data wiring lands in the phase noted on the page's own
 * spec entry (Section 5/8). Never mistake this for finished, live content.
 */
export function PlaceholderPage({ page }: PlaceholderPageProps) {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-semibold text-text">{page.label}</h1>
          <Badge tone="steel-neutral">Phase 0 scaffold</Badge>
        </div>
        <p className="max-w-2xl text-sm text-text-dim">{page.role}</p>
      </div>

      <Card className="flex flex-col items-start gap-2 p-8">
        <span className="font-mono text-xs uppercase tracking-wide text-text-faint">
          {page.section} / #{page.id}
        </span>
        <p className="max-w-lg text-sm text-text-dim">
          This route is wired and reachable, but not yet connected to live backend data. It will
          be built out in the phase covering this screen.
        </p>
      </Card>
    </div>
  )
}

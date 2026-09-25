import { Link } from 'react-router-dom'
import type { AssignedCase } from '../lib/api/case-types'
import { CaseSwitcher } from './CaseSwitcher'
import { EmptyState, LoadingState } from './DataState'

interface ActiveCaseGateProps {
  cases: AssignedCase[]
  loading: boolean
  activeCaseId: string | null
  onSelect: (caseId: string) => void
}

/**
 * Shared "pick which case you're working" gate for every Investigate-section
 * page (7-12) -- they all share one case selection (see
 * `lib/cases.ts::useActiveCaseWorkspace`), so this is the same real
 * loading/empty/picker shape everywhere rather than each page reinventing it.
 */
export function ActiveCaseGate({ cases, loading, activeCaseId, onSelect }: ActiveCaseGateProps) {
  if (loading) return <LoadingState label="Loading your cases..." />
  if (cases.length === 0) {
    return (
      <EmptyState
        title="No assigned cases"
        description="You need an assigned case before you can investigate it."
        action={
          <Link to="/cases/new" className="text-xs font-medium text-crimson hover:underline">
            Create a case &rarr;
          </Link>
        }
      />
    )
  }
  return (
    <div className="max-w-sm">
      <CaseSwitcher cases={cases} activeCaseId={activeCaseId} onChange={onSelect} />
    </div>
  )
}

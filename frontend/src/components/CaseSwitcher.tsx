import type { AssignedCase } from '../lib/api/case-types'

interface CaseSwitcherProps {
  cases: AssignedCase[]
  activeCaseId: string | null
  onChange: (caseId: string) => void
  disabled?: boolean
}

/**
 * ABAC (Section 6): every authenticated screen operates within exactly one
 * active case; switching re-scopes every list/panel/action on the page.
 * Shown wherever a page needs a specific case context (Evidence Upload,
 * Processing Pipeline) beyond whatever the Sidebar's own default picked.
 */
export function CaseSwitcher({ cases, activeCaseId, onChange, disabled = false }: CaseSwitcherProps) {
  return (
    <label className="flex flex-col gap-1.5 text-sm">
      <span className="font-medium text-text-dim">Case</span>
      <select
        value={cases.some((c) => c.case_id === activeCaseId) ? (activeCaseId ?? '') : ''}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled || cases.length === 0}
        className="w-full max-w-sm rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-crimson/40 disabled:opacity-50"
      >
        <option value="" disabled>
          {cases.length === 0 ? 'No assigned cases' : 'Select a case'}
        </option>
        {cases.map((c) => (
          <option key={c.case_id} value={c.case_id}>
            {c.case_reference} &middot; {c.role.replace(/_/g, ' ')}
          </option>
        ))}
      </select>
    </label>
  )
}

import { useMemo, useState } from 'react'
import { Search } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import { Badge } from '../components/Badge'
import { Button } from '../components/Button'
import { DataTable, DataTableRow } from '../components/DataTable'
import { EmptyState, ErrorState, LoadingState } from '../components/DataState'
import { useAssignedCases } from '../lib/cases'
import { useAuthStore } from '../lib/auth/store'

const COLUMNS = [
  { key: 'case', header: 'Case' },
  { key: 'status', header: 'Status' },
  { key: 'role', header: 'Your role' },
  { key: 'classification', header: 'Classification' },
]

/**
 * Page 3 -- Case Management. No backend endpoint lists "every case visible
 * to the current user" (only create/get-by-id/status exist -- see
 * lib/cases.ts's docstring), so search/filter here is real but client-side,
 * over the same real, fully-hydrated case list the Dashboard uses. Opening
 * a row sets it as the active case (ABAC) and routes toward Workspace --
 * the real destination once Phase 3 builds it.
 */
export function CaseManagement() {
  const navigate = useNavigate()
  const setActiveCase = useAuthStore((state) => state.setActiveCase)
  const { cases, loading, error } = useAssignedCases()
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState<'all' | 'open' | 'closed' | 'archived'>('all')

  const filtered = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase()
    return cases.filter((c) => {
      if (statusFilter !== 'all' && c.status !== statusFilter) return false
      if (!normalizedQuery) return true
      return (
        c.case_reference.toLowerCase().includes(normalizedQuery) ||
        c.case_id.toLowerCase().includes(normalizedQuery)
      )
    })
  }, [cases, query, statusFilter])

  function openCase(caseId: string) {
    setActiveCase(caseId)
    navigate('/workspace')
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-text">Case Management</h1>
          <p className="text-sm text-text-dim">Browse, search, and open your assigned investigations.</p>
        </div>
        <Link to="/cases/new">
          <Button>New Case</Button>
        </Link>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <span className="flex min-w-0 flex-1 max-w-sm items-center gap-2 rounded-control border border-card-border bg-card px-3 py-2">
          <Search size={14} className="shrink-0 text-text-faint" aria-hidden="true" />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search by case reference..."
            className="w-full bg-transparent text-sm text-text placeholder:text-text-faint focus:outline-none"
          />
        </span>
        <select
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value as typeof statusFilter)}
          className="rounded-control border border-card-border bg-card px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-crimson/40"
        >
          <option value="all">All statuses</option>
          <option value="open">Open</option>
          <option value="closed">Closed</option>
          <option value="archived">Archived</option>
        </select>
      </div>

      {error ? <ErrorState message={error} /> : null}

      {loading ? (
        <LoadingState label="Loading your cases..." />
      ) : cases.length === 0 ? (
        <EmptyState
          title="No assigned cases yet"
          description="Create a case to start an investigation."
          action={
            <Link to="/cases/new">
              <Button className="mt-2">Create a case</Button>
            </Link>
          }
        />
      ) : filtered.length === 0 ? (
        <EmptyState title="No cases match your search" description="Try a different reference or clear the status filter." />
      ) : (
        <DataTable columns={COLUMNS}>
          {filtered.map((c) => (
            <DataTableRow
              key={c.case_id}
              columns={COLUMNS}
              primary={c.case_reference}
              secondary={c.case_id}
              onClick={() => openCase(c.case_id)}
              cells={{
                status: <Badge tone="steel-neutral">{c.status}</Badge>,
                role: <span className="text-sm text-text-dim">{c.role.replace(/_/g, ' ')}</span>,
                classification: <Badge tone="steel-neutral">{c.classification}</Badge>,
              }}
            />
          ))}
        </DataTable>
      )}
    </div>
  )
}

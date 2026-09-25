import { useCallback, useEffect, useState } from 'react'
import { authApi, casesApi } from './api/client'
import type { AssignedCase } from './api/case-types'
import { useAuthStore } from './auth/store'

/**
 * No backend endpoint lists "every case the current user has access to" --
 * only `POST /cases` (create), `GET /cases/{id}` (single), and
 * `GET /cases/{id}/status` exist (confirmed by reading every
 * `@router.get`/`@router.post` in app/modules/access_control/cases_api.py).
 * `GET /auth/me` already returns every active `case_memberships` row
 * (case_id/role/clearance), so this hydrates each one via a real
 * `GET /cases/{id}` call in parallel -- composing existing real endpoints,
 * not inventing a new one. This keeps Phase 2 entirely frontend-only (no
 * backend change), at the honest cost of no server-side search/pagination
 * across cases; client-side filtering over this list is what Case
 * Management actually offers. A real `GET /api/v1/cases` list-for-user
 * endpoint would remove this limitation if ever added.
 */
export function useAssignedCases() {
  const caseMemberships = useAuthStore((state) => state.caseMemberships)
  const setIdentity = useAuthStore((state) => state.setIdentity)
  const [cases, setCases] = useState<AssignedCase[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const hydrate = useCallback(async () => {
    const activeMemberships = caseMemberships.filter((m) => m.is_active)
    const results = await Promise.allSettled(
      activeMemberships.map((m) => casesApi.get(m.case_id)),
    )
    const hydrated: AssignedCase[] = []
    let anyFailed = false
    results.forEach((result, index) => {
      if (result.status === 'fulfilled') {
        const membership = activeMemberships[index]
        hydrated.push({ ...result.value, role: membership.role, clearance: membership.clearance })
      } else {
        anyFailed = true
      }
    })
    setCases(hydrated)
    setError(anyFailed ? 'Some assigned cases could not be loaded.' : null)
  }, [caseMemberships])

  useEffect(() => {
    setLoading(true)
    hydrate().finally(() => setLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hydrate])

  /**
   * Re-pulls `/me` first (picks up a just-created case's new membership).
   * Updating the store's `case_memberships` changes `hydrate`'s identity,
   * which re-runs the effect above and re-fetches case details -- no
   * separate loading-state toggle needed here.
   */
  const refresh = useCallback(async () => {
    const me = await authApi.me()
    setIdentity(me.user, me.case_memberships)
  }, [setIdentity])

  return { cases, loading, error, refresh }
}

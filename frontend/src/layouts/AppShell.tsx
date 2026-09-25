import { useEffect } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { Sidebar } from '../components/Sidebar'
import { TopBar } from '../components/TopBar'
import { authApi } from '../lib/api/client'
import { useAuthStore } from '../lib/auth/store'
import { PAGES } from '../lib/navigation'

function roleLabel(systemRole: string | null, caseRole: string | null): string {
  if (systemRole === 'admin') return 'Administrator'
  if (caseRole) return caseRole.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
  return 'No active case'
}

/**
 * Authenticated app shell: Evergreen sidebar (224px) + Charcoal top bar
 * (60px) wrapping a Cool Steel canvas content area (Section 3/4).
 */
export function AppShell() {
  const location = useLocation()
  const navigate = useNavigate()
  const currentPage = PAGES.find((page) => page.path === location.pathname)

  const user = useAuthStore((state) => state.user)
  const refreshToken = useAuthStore((state) => state.refreshToken)
  const activeCaseId = useAuthStore((state) => state.activeCaseId)
  const caseMemberships = useAuthStore((state) => state.caseMemberships)
  const clear = useAuthStore((state) => state.clear)
  const setIdentity = useAuthStore((state) => state.setIdentity)

  const activeCaseRole =
    caseMemberships.find((m) => m.case_id === activeCaseId && m.is_active)?.role ?? null

  useEffect(() => {
    // The persisted session's `case_memberships` reflect whatever `/me`
    // returned at login; nothing else updates it. A fresh read on every
    // shell mount (including a plain reload) keeps case-scoped nav (ABAC)
    // correct after a case is created or a membership changes elsewhere --
    // without this, a brand-new case membership would only ever show up
    // after logging out and back in.
    authApi
      .me()
      .then((me) => setIdentity(me.user, me.case_memberships))
      .catch(() => undefined)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function handleLogout() {
    if (refreshToken) {
      // Best-effort: the session is cleared client-side regardless of
      // whether this call succeeds (Section 6: "Logout invalidates the
      // session client- and server-side").
      await authApi.logout(refreshToken).catch(() => undefined)
    }
    clear()
    navigate('/', { replace: true })
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-canvas">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar
          breadcrumb={currentPage?.label ?? 'TraceX'}
          userName={user?.display_name ?? ''}
          userRole={roleLabel(user?.system_role ?? null, activeCaseRole)}
          onLogout={handleLogout}
        />
        <main className="flex-1 overflow-y-auto p-22">
          <Outlet />
        </main>
      </div>
    </div>
  )
}

import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { PAGES } from '../navigation'
import { roleHasCaseAction } from './permissions'
import { useAuthStore } from './store'

/**
 * Guards the two onboarding-only screens (force-password-change,
 * mfa/enroll): reachable as soon as a session exists, even before
 * onboarding is complete -- that is the whole point of these routes.
 */
export function RequireSession() {
  const hasSession = useAuthStore((state) => state.accessToken !== null && state.user !== null)
  const location = useLocation()
  if (!hasSession) return <Navigate to="/login" replace state={{ from: location }} />
  return <Outlet />
}

/**
 * Guards the authenticated app shell (Section 6): no session -> Login;
 * session but the provisioner-forced ceremony isn't finished -> the exact
 * onboarding step still owed, in order (password change before MFA
 * enrollment, matching Section 6's own sequencing); only then the real app.
 */
export function RequireAuth() {
  const user = useAuthStore((state) => state.user)
  const hasSession = useAuthStore((state) => state.accessToken !== null && state.user !== null)
  const location = useLocation()

  if (!hasSession || !user) return <Navigate to="/login" replace state={{ from: location }} />
  if (user.must_change_password) return <Navigate to="/force-password-change" replace />
  if (!user.totp_enabled) return <Navigate to="/mfa/enroll" replace />
  return <Outlet />
}

/**
 * UX-only route narrowing that mirrors the backend's role checks.  It never
 * substitutes for API authorization: direct requests are still denied by
 * ``require_provisioner``, ``require_case_head``, and case ABAC.
 */
export function RequireApplicationRoute() {
  const user = useAuthStore((state) => state.user)
  const activeCaseId = useAuthStore((state) => state.activeCaseId)
  const memberships = useAuthStore((state) => state.caseMemberships)
  const location = useLocation()
  if (!user) return <Navigate to="/login" replace />

  if (user.system_role === 'provisioner' && location.pathname !== '/settings') {
    return <Navigate to="/settings" replace />
  }
  if (location.pathname === '/cases/new' && user.system_role !== 'case_head') {
    return <Navigate to="/cases" replace />
  }

  const page = PAGES.find((item) => item.path === location.pathname)
  if (page?.requiresCaseAction) {
    const role = memberships.find((membership) => membership.case_id === activeCaseId && membership.is_active)?.role
    if (!role || !roleHasCaseAction(role, page.requiresCaseAction)) {
      return <Navigate to={memberships.some((membership) => membership.is_active) ? '/cases' : '/settings'} replace />
    }
  }
  return <Outlet />
}

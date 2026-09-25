import { Navigate, Outlet, useLocation } from 'react-router-dom'
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
 * session but the admin-forced ceremony isn't finished -> the exact
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

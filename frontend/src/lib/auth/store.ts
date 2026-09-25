import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { CaseMembershipView, PublicUser } from '../api/types'

interface AuthState {
  accessToken: string | null
  refreshToken: string | null
  user: PublicUser | null
  caseMemberships: CaseMembershipView[]
  /** ABAC: every authenticated screen operates within exactly one active case. */
  activeCaseId: string | null

  setTokens: (tokens: { accessToken: string; refreshToken: string }) => void
  setIdentity: (user: PublicUser, caseMemberships: CaseMembershipView[]) => void
  setActiveCase: (caseId: string | null) => void
  clear: () => void
}

/**
 * The backend sets no cookie at all -- access tokens travel in the
 * `Authorization` header, refresh tokens in an explicit request body (see
 * docs/architecture/access-control-v1.md's "Token design"). There is no
 * alternative to client-side storage; `persist` keeps a session alive
 * across a reload, matching the spec's "silent refresh where supported"
 * requirement.
 */
export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      accessToken: null,
      refreshToken: null,
      user: null,
      caseMemberships: [],
      activeCaseId: null,

      setTokens: ({ accessToken, refreshToken }) =>
        set({ accessToken, refreshToken }),

      setIdentity: (user, caseMemberships) =>
        set((state) => ({
          user,
          caseMemberships,
          // Default to the first active membership so ABAC-scoped nav has
          // something to scope against immediately after login. Phase 2's
          // Case Management page lets the investigator switch explicitly.
          activeCaseId:
            state.activeCaseId && caseMemberships.some((m) => m.case_id === state.activeCaseId)
              ? state.activeCaseId
              : (caseMemberships.find((m) => m.is_active)?.case_id ?? null),
        })),

      setActiveCase: (caseId) => set({ activeCaseId: caseId }),

      clear: () =>
        set({
          accessToken: null,
          refreshToken: null,
          user: null,
          caseMemberships: [],
          activeCaseId: null,
        }),
    }),
    { name: 'tracex-auth' },
  ),
)

export function isAuthenticated(): boolean {
  return useAuthStore.getState().accessToken !== null && useAuthStore.getState().user !== null
}

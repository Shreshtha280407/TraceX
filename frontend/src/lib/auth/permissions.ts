import { useAuthStore } from './store'
import type { CaseRole } from '../api/types'

/**
 * Case-scoped actions, mirroring `CaseAction` in
 * app/modules/access_control/models.py exactly (names and values). This is
 * a read-only UI mirror of the backend's own policy for nav-hiding
 * purposes only -- it decides what to *show*, never what to *allow*. The
 * backend's `authorize_case_action` is the only real enforcement; a hidden
 * nav item is a UX courtesy, not a security boundary (Section 6: "the UI
 * should not offer it in the first place -- disabled/hidden, not just
 * erroring after the click").
 */
export type CaseAction =
  | 'case_read'
  | 'case_manage'
  | 'member_manage'
  | 'evidence_read'
  | 'evidence_write'
  | 'graph_read'
  | 'integrity_read'
  | 'integrity_verify'
  | 'integrity_export'
  | 'review_decide'
  | 'hypothesis_propose'
  | 'export_case_data'
  | 'case_note_write'
  | 'case_note_read_all'

/** Mirrors `ROLE_ACTIONS` in models.py exactly. Default deny: an action not listed is denied. */
export const ROLE_ACTIONS: Record<CaseRole, CaseAction[]> = {
  case_owner: [
    'case_read',
    'case_manage',
    'member_manage',
    'evidence_read',
    'evidence_write',
    'graph_read',
    'integrity_read',
    'integrity_verify',
    'integrity_export',
    'review_decide',
    'hypothesis_propose',
    'export_case_data',
    'case_note_write',
    'case_note_read_all',
  ],
  case_manager: [
    'case_read',
    'case_manage',
    'member_manage',
    'evidence_read',
    'evidence_write',
    'graph_read',
    'integrity_read',
    'integrity_verify',
    'integrity_export',
    'review_decide',
    'hypothesis_propose',
    'export_case_data',
    'case_note_write',
    'case_note_read_all',
  ],
  investigator: [
    'case_read',
    'evidence_read',
    'evidence_write',
    'graph_read',
    'integrity_read',
    'integrity_verify',
    'hypothesis_propose',
    'case_note_write',
  ],
  analyst: ['case_read', 'evidence_read', 'graph_read', 'integrity_read', 'integrity_verify'],
  reviewer: [
    'case_read',
    'evidence_read',
    'graph_read',
    'integrity_read',
    'integrity_verify',
    'review_decide',
    'case_note_write',
    'case_note_read_all',
  ],
  viewer: ['case_read', 'graph_read'],
}

/**
 * Pure, role-based check (no "active case" involved) -- for code that
 * evaluates permissions per case while iterating several assigned cases at
 * once (e.g. Dashboard's cross-case aggregation), where each case can carry
 * a different role. Same default-deny table as `hasCaseAction`.
 */
export function roleHasCaseAction(role: CaseRole, action: CaseAction): boolean {
  return ROLE_ACTIONS[role].includes(action)
}

export function isSystemAdmin(): boolean {
  return useAuthStore.getState().user?.system_role === 'admin'
}

export function activeCaseRole(): CaseRole | null {
  const { activeCaseId, caseMemberships } = useAuthStore.getState()
  if (!activeCaseId) return null
  return caseMemberships.find((m) => m.case_id === activeCaseId && m.is_active)?.role ?? null
}

/** True only if the active case's membership role permits `action`. False with no active case. */
export function hasCaseAction(action: CaseAction): boolean {
  const role = activeCaseRole()
  if (!role) return false
  return ROLE_ACTIONS[role].includes(action)
}

export function useIsSystemAdmin(): boolean {
  return useAuthStore((state) => state.user?.system_role === 'admin')
}

export function useActiveCaseRole(): CaseRole | null {
  return useAuthStore((state) => {
    if (!state.activeCaseId) return null
    return (
      state.caseMemberships.find((m) => m.case_id === state.activeCaseId && m.is_active)?.role ??
      null
    )
  })
}

export function useHasCaseAction(action: CaseAction): boolean {
  const role = useActiveCaseRole()
  return role !== null && ROLE_ACTIONS[role].includes(action)
}

export function useHasAnyCase(): boolean {
  return useAuthStore((state) => state.caseMemberships.some((m) => m.is_active))
}

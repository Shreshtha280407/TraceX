import type { LucideIcon } from 'lucide-react'
import type { CaseAction } from './auth/permissions'
import {
  Activity,
  FileSearch,
  FileStack,
  Fingerprint,
  FolderKanban,
  GitBranch,
  Lightbulb,
  LayoutDashboard,
  ListChecks,
  NotebookPen,
  ShieldCheck,
  Settings,
  Share2,
  UploadCloud,
  Waypoints,
} from 'lucide-react'

/**
 * Single source of truth for the 17-screen spec (Section 5 of the frontend
 * build prompt). Pages 0-1 are pre-auth and rendered outside the app shell;
 * pages 2-16 are grouped into sidebar sections exactly per the Section 5
 * table's "Section" column.
 */
export type PageSection =
  | 'Public'
  | 'Access'
  | 'Command Center'
  | 'Case'
  | 'Investigate'
  | 'Evidence'
  | 'Memory'
  | 'Trust'
  | 'System'

export interface PageDef {
  id: number
  path: string
  label: string
  section: PageSection
  role: string
  icon: LucideIcon
  /** Rendered inside the authenticated AppShell (sidebar + top bar). */
  inShell: boolean
  /**
   * RBAC/ABAC (Section 6): the case-scoped action required for this page to
   * be worth showing at all. `undefined` means "not case-scoped" (Dashboard,
   * Case Management, Create Case, Settings) -- always shown once
   * authenticated. Mirrors `require_case_*`'s action in the real backend
   * route this page will eventually call.
   */
  requiresCaseAction?: CaseAction
}

export const PAGES: PageDef[] = [
  {
    id: 0,
    path: '/',
    label: 'Landing',
    section: 'Public',
    role: 'Public entry surface: what TraceX is, sign-in / request-access entry point. No case data, no internal terminology exposed.',
    icon: ShieldCheck,
    inShell: false,
  },
  {
    id: 1,
    path: '/login',
    label: 'Login / MFA',
    section: 'Access',
    role: 'Identity verification, session establishment.',
    icon: Fingerprint,
    inShell: false,
  },
  {
    id: 2,
    path: '/dashboard',
    label: 'Dashboard',
    section: 'Command Center',
    role: 'Assigned cases, alerts, pending reviews, recent activity.',
    icon: LayoutDashboard,
    inShell: true,
  },
  {
    id: 3,
    path: '/cases',
    label: 'Case Management',
    section: 'Case',
    role: 'Browse, search, filter, open investigations.',
    icon: FolderKanban,
    inShell: true,
  },
  {
    id: 4,
    path: '/cases/new',
    label: 'Create Case',
    section: 'Case',
    role: 'Case metadata, classification, investigators, access policy.',
    icon: FileStack,
    inShell: true,
  },
  {
    id: 5,
    path: '/evidence/upload',
    label: 'Evidence Upload',
    section: 'Case',
    role: 'Upload FIR / CDR / finance / video / audio / images / chat.',
    icon: UploadCloud,
    inShell: true,
    requiresCaseAction: 'evidence_write',
  },
  {
    id: 6,
    path: '/pipeline',
    label: 'Processing Pipeline',
    section: 'Case',
    role: 'Ingestion, extraction, entity detection, graph projection status.',
    icon: Activity,
    inShell: true,
    // Real page (Phase 2) lists evidence via GET /cases/{id}/evidence, which
    // requires evidence_read -- case_read alone (which every role including
    // viewer has) is not enough and would 403. Was case_read as a Phase 0/1
    // placeholder before this page had a real backing call.
    requiresCaseAction: 'evidence_read',
  },
  {
    id: 7,
    path: '/workspace',
    label: 'Investigation Workspace',
    section: 'Investigate',
    role: 'Graph + evidence + entity panels in one cockpit; the main working surface.',
    icon: Waypoints,
    inShell: true,
    requiresCaseAction: 'graph_read',
  },
  {
    id: 8,
    path: '/timeline',
    label: 'Timeline + Map',
    section: 'Investigate',
    role: 'Spatial + chronological reconstruction, synced to the workspace.',
    icon: GitBranch,
    inShell: true,
    requiresCaseAction: 'graph_read',
  },
  {
    id: 9,
    path: '/entities',
    label: 'Entity Intelligence',
    section: 'Investigate',
    role: 'Entity profiles, identifiers, related events, connected evidence.',
    icon: Share2,
    inShell: true,
    requiresCaseAction: 'graph_read',
  },
  {
    id: 10,
    path: '/review/candidates',
    label: 'Candidate Review',
    section: 'Investigate',
    role: 'Verify / Reject / Needs More Evidence on entity-resolution candidates -- human-in-the-loop, never auto-merge.',
    icon: ListChecks,
    inShell: true,
    requiresCaseAction: 'review_decide',
  },
  {
    id: 11,
    path: '/motifs',
    label: 'Motifs + Correlations',
    section: 'Investigate',
    role: 'Recurring patterns, cross-modal incident threads.',
    icon: FileSearch,
    inShell: true,
    requiresCaseAction: 'graph_read',
  },
  {
    id: 12,
    path: '/hypotheses',
    label: 'Hypotheses + Counter-Evidence',
    section: 'Investigate',
    role: 'Support/contradiction evidence, confidence components, review decision.',
    icon: Lightbulb,
    inShell: true,
    requiresCaseAction: 'hypothesis_propose',
  },
  {
    id: 13,
    path: '/evidence',
    label: 'Evidence Viewer',
    section: 'Evidence',
    role: 'Exact source inspection -- page/row/frame/timestamp drill-down.',
    icon: FileSearch,
    inShell: true,
    requiresCaseAction: 'evidence_read',
  },
  {
    id: 14,
    path: '/memory',
    label: 'Investigation Memory',
    section: 'Memory',
    role: 'Notes, decisions, and handoff/review history.',
    icon: NotebookPen,
    inShell: true,
    requiresCaseAction: 'case_read',
  },
  {
    id: 15,
    path: '/integrity',
    label: 'Integrity / Audit',
    section: 'Trust',
    role: 'Hash verification, Merkle checkpoints, signatures, audit trail.',
    icon: ShieldCheck,
    inShell: true,
    requiresCaseAction: 'integrity_read',
  },
  {
    id: 16,
    path: '/settings',
    label: 'Settings / Security',
    section: 'System',
    role: 'MFA, roles, permissions, sessions, security config. Includes an Admin-only sub-section: create investigator accounts, issue/reset TOTP enrollment, view/revoke active sessions.',
    icon: Settings,
    inShell: true,
  },
]

export const SIDEBAR_SECTION_ORDER: PageSection[] = [
  'Command Center',
  'Case',
  'Investigate',
  'Evidence',
  'Memory',
  'Trust',
  'System',
]

export function sidebarSections(): { section: PageSection; pages: PageDef[] }[] {
  return SIDEBAR_SECTION_ORDER.map((section) => ({
    section,
    pages: PAGES.filter((page) => page.section === section && page.inShell),
  }))
}

import type { ReactNode } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from './layouts/AppShell'
import { RequireApplicationRoute, RequireAuth, RequireSession } from './lib/auth/guards'
import { PAGES } from './lib/navigation'
import { CandidateReview } from './pages/CandidateReview'
import { CaseManagement } from './pages/CaseManagement'
import { CreateCase } from './pages/CreateCase'
import { Dashboard } from './pages/Dashboard'
import { EntityIntelligence } from './pages/EntityIntelligence'
import { EvidenceLibrary } from './pages/EvidenceLibrary'
import { EvidenceViewer } from './pages/EvidenceViewer'
import { ForcePasswordChange } from './pages/ForcePasswordChange'
import { FirstAdminSetup } from './pages/FirstAdminSetup'
import { Hypotheses } from './pages/Hypotheses'
import { IntegrityAudit } from './pages/IntegrityAudit'
import { InvestigationMemory } from './pages/InvestigationMemory'
import { InvestigationWorkspace } from './pages/InvestigationWorkspace'
import { Landing } from './pages/Landing'
import { Login } from './pages/Login'
import { MfaEnroll } from './pages/MfaEnroll'
import { MotifsCorrelations } from './pages/MotifsCorrelations'
import { PlaceholderPage } from './pages/PlaceholderPage'
import { SettingsSecurity } from './pages/SettingsSecurity'
import { TimelineMap } from './pages/TimelineMap'

const SHELL_PAGES = PAGES.filter((page) => page.inShell)

const SHELL_PAGE_ELEMENTS: Record<string, ReactNode> = {
  '/dashboard': <Dashboard />,
  '/cases': <CaseManagement />,
  '/cases/new': <CreateCase />,
  '/evidence-library': <EvidenceLibrary />,
  '/workspace': <InvestigationWorkspace />,
  '/timeline': <TimelineMap />,
  '/entities': <EntityIntelligence />,
  '/review/candidates': <CandidateReview />,
  '/motifs': <MotifsCorrelations />,
  '/hypotheses': <Hypotheses />,
  '/evidence': <EvidenceViewer />,
  '/memory': <InvestigationMemory />,
  '/integrity': <IntegrityAudit />,
  '/settings': <SettingsSecurity />,
}

function App() {
  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route path="/login" element={<Login />} />
      <Route path="/setup/first-admin" element={<FirstAdminSetup />} />

      <Route element={<RequireSession />}>
        <Route path="/force-password-change" element={<ForcePasswordChange />} />
        <Route path="/mfa/enroll" element={<MfaEnroll />} />
      </Route>

      <Route element={<RequireAuth />}>
        <Route element={<RequireApplicationRoute />}>
          <Route element={<AppShell />}>
            {SHELL_PAGES.map((page) => (
              <Route
                key={page.path}
                path={page.path}
                element={SHELL_PAGE_ELEMENTS[page.path] ?? <PlaceholderPage page={page} />}
              />
            ))}
          </Route>
        </Route>
      </Route>

      {/* Legacy normal-user routes deliberately converge on the one case-scoped library. */}
      <Route path="/evidence/upload" element={<Navigate to="/evidence-library" replace />} />
      <Route path="/pipeline" element={<Navigate to="/evidence-library" replace />} />

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}

export default App

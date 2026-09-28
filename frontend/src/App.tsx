import { Navigate, Route, Routes } from "react-router-dom";
import { RequireAuth } from "./lib/auth";
import { AccessGate } from "./pages/AccessGate";
import { Onboarding } from "./pages/Onboarding";
import { CaseDashboard } from "./pages/CaseDashboard";
import { InvestigatorOverview } from "./pages/InvestigatorOverview";
import { EvidenceIntake } from "./pages/EvidenceIntake";
import { GraphExplorer } from "./pages/GraphExplorer";
import { FindingsFeed } from "./pages/FindingsFeed";
import { EvidencePackage } from "./pages/EvidencePackage";
import { ModelRules } from "./pages/ModelRules";
import { CaseSettings } from "./pages/CaseSettings";
import { ExportDeployment } from "./pages/ExportDeployment";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<AccessGate />} />
      <Route path="/onboarding" element={<RequireAuth><Onboarding /></RequireAuth>} />
      <Route path="/dashboard" element={<RequireAuth><CaseDashboard /></RequireAuth>} />
      <Route path="/overview" element={<RequireAuth><InvestigatorOverview /></RequireAuth>} />
      <Route path="/cases/:caseId/ingestion" element={<RequireAuth><EvidenceIntake /></RequireAuth>} />
      <Route path="/cases/:caseId/graph" element={<RequireAuth><GraphExplorer /></RequireAuth>} />
      <Route path="/cases/:caseId/findings" element={<RequireAuth><FindingsFeed /></RequireAuth>} />
      <Route path="/cases/:caseId/model" element={<RequireAuth><ModelRules /></RequireAuth>} />
      <Route path="/cases/:caseId/settings" element={<RequireAuth><CaseSettings /></RequireAuth>} />
      <Route path="/cases/:caseId/export" element={<RequireAuth><ExportDeployment /></RequireAuth>} />
      <Route path="/findings/:findingId" element={<RequireAuth><EvidencePackage /></RequireAuth>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

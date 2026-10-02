import { Navigate, Route, Routes } from "react-router-dom";
import { RequireAuth } from "./lib/auth";
import { AccessGate } from "./pages/AccessGate";
import { CaseDashboard } from "./pages/CaseDashboard";
import { InvestigatorOverview } from "./pages/InvestigatorOverview";
import { EvidenceIntake } from "./pages/EvidenceIntake";
import { GraphExplorer } from "./pages/GraphExplorer";
import { FindingsFeed } from "./pages/FindingsFeed";
import { EvidencePackage } from "./pages/EvidencePackage";
import { Settings } from "./pages/Settings";
import { ExportDeployment } from "./pages/ExportDeployment";
import { EntitiesRisk } from "./pages/EntitiesRisk";
import { NetworkIntel } from "./pages/NetworkIntel";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<AccessGate />} />
      <Route path="/overview" element={<RequireAuth><InvestigatorOverview /></RequireAuth>} />
      <Route path="/settings" element={<RequireAuth><Settings /></RequireAuth>} />
      <Route path="/cases/:caseId/dashboard" element={<RequireAuth><CaseDashboard /></RequireAuth>} />
      <Route path="/cases/:caseId/ingestion" element={<RequireAuth><EvidenceIntake /></RequireAuth>} />
      <Route path="/cases/:caseId/graph" element={<RequireAuth><GraphExplorer /></RequireAuth>} />
      <Route path="/cases/:caseId/findings" element={<RequireAuth><FindingsFeed /></RequireAuth>} />
      <Route path="/cases/:caseId/entities" element={<RequireAuth><EntitiesRisk /></RequireAuth>} />
      <Route path="/cases/:caseId/network" element={<RequireAuth><NetworkIntel /></RequireAuth>} />
      <Route path="/cases/:caseId/export" element={<RequireAuth><ExportDeployment /></RequireAuth>} />
      <Route path="/findings/:findingId" element={<RequireAuth><EvidencePackage /></RequireAuth>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

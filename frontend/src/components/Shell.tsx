import { useEffect, type ReactNode } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { useAuth, initials } from "../lib/auth";
import { setLastCaseId, useLastCaseId } from "../lib/lastCase";
import { ExportIcon, FlagIcon, GearIcon, GraphIcon, GridIcon, HomeIcon, ShieldIcon, UploadIcon } from "./icons";

/** 88px icon rail + avatar badge, present on every authenticated page. Nav order:
 * Home → Dashboard → Evidence Intake → Graph Explorer → Findings Feed → Model & Rules → Case Settings → Export & Deployment.
 * Home (/overview) is the first icon, unscoped, and the post-login landing page.
 * Dashboard and every item after it are case-scoped (/cases/:caseId/...), falling
 * back to the last case visited, or to /overview if none is known yet. "Active" is
 * always matched against the real URL pattern for each destination, never against
 * the (possibly-collapsed) href. */
export function Shell({ children }: { children: ReactNode }) {
  const { actor, logout } = useAuth();
  const { caseId } = useParams();
  const { pathname } = useLocation();

  useEffect(() => {
    if (caseId) setLastCaseId(caseId);
  }, [caseId]);

  const lastCaseId = useLastCaseId();
  const fallbackCaseId = caseId ?? lastCaseId;
  const scoped = (suffix: string) => (fallbackCaseId ? `/cases/${fallbackCaseId}${suffix}` : "/overview");
  const caseScoped = (suffix: string) => new RegExp(`^/cases/[^/]+${suffix}`).test(pathname);

  const items = [
    { to: "/overview", icon: <HomeIcon />, label: "Home", active: pathname === "/overview" },
    { to: scoped("/dashboard"), icon: <GridIcon />, label: "Dashboard", active: caseScoped("/dashboard") },
    { to: scoped("/ingestion"), icon: <UploadIcon />, label: "Evidence Intake", active: caseScoped("/ingestion") },
    { to: scoped("/graph"), icon: <GraphIcon />, label: "UTXO Graph Explorer", active: caseScoped("/graph") },
    { to: scoped("/findings"), icon: <FlagIcon />, label: "Findings Feed", active: caseScoped("/findings") },
    { to: scoped("/model"), icon: <GearIcon />, label: "Model & Rules", active: caseScoped("/model") },
    { to: scoped("/settings"), icon: <ShieldIcon />, label: "Case Settings", active: caseScoped("/settings") },
    { to: scoped("/export"), icon: <ExportIcon />, label: "Export & Deployment", active: caseScoped("/export") },
  ];

  return (
    <div className="shell">
      <nav className="rail">
        <div className="rail-logo">V</div>
        {items.map((item) => (
          <Link key={item.label} to={item.to} className={`nav-icon ${item.active ? "active" : ""}`} title={item.label}>
            {item.icon}
          </Link>
        ))}
        <div className="rail-spacer" />
        <button type="button" className="avatar-badge" title={`${actor} — sign out`} onClick={logout}>
          {initials(actor)}
        </button>
      </nav>
      <div className="content-area">
        <div className="page">{children}</div>
      </div>
    </div>
  );
}

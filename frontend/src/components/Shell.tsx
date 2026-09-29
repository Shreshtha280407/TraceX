import { useEffect, type ReactNode } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { useAuth, initials } from "../lib/auth";
import { setLastCaseId, useLastCaseId } from "../lib/lastCase";
import { CaseSwitcher } from "./CaseSwitcher";
import { ExportIcon, FlagIcon, GraphIcon, GridIcon, HomeIcon, UploadIcon } from "./icons";
import tracexLogo from "../assets/tracex-logo.png";

/** 88px icon rail + avatar badge, present on every authenticated page. Nav order:
 * Home → Dashboard → Evidence Intake → Graph Explorer → Findings Feed → Export & Deployment.
 * Home (/overview) is the first icon, unscoped, and the post-login landing page.
 * Dashboard and every item after it are case-scoped (/cases/:caseId/...), falling
 * back to the last case visited, or to /overview if none is known yet. "Active" is
 * always matched against the real URL pattern for each destination, never against
 * the (possibly-collapsed) href.
 *
 * The avatar circle at the bottom opens /settings (global: account, case settings
 * with its own case picker, deployed model info, system checks, sign-out) rather
 * than signing out directly. */
export function Shell({ children }: { children: ReactNode }) {
  const { actor } = useAuth();
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
    { to: scoped("/export"), icon: <ExportIcon />, label: "Export & Deployment", active: caseScoped("/export") },
  ];

  return (
    <div className="shell">
      <nav className="rail">
        <Link to="/overview" className="rail-logo" title="TraceX">
          <img src={tracexLogo} alt="TraceX" />
        </Link>
        {items.map((item) => (
          <Link key={item.label} to={item.to} className={`nav-icon ${item.active ? "active" : ""}`} title={item.label}>
            {item.icon}
          </Link>
        ))}
        <div className="rail-spacer" />
        <Link to="/settings" className="avatar-badge" title={`${actor} — settings`}>
          {initials(actor)}
        </Link>
      </nav>
      <div className="content-area">
        <div className="page">
          {caseId && <CaseSwitcher caseId={caseId} pathname={pathname} />}
          {children}
        </div>
      </div>
    </div>
  );
}

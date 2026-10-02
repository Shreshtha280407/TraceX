import type { ReactNode } from "react";
import type { FindingConfidence } from "../lib/api";

export function NeoCard({
  variant = "neo",
  className = "",
  children,
}: {
  variant?: "neo" | "neo-sm" | "neo-inset";
  className?: string;
  children: ReactNode;
}) {
  return <div className={`${variant} card ${className}`}>{children}</div>;
}

export function StatTile({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="neo-sm stat-tile">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub !== undefined && <div className="sub">{sub}</div>}
    </div>
  );
}

export type BadgeTone = "deterministic" | "ml" | "danger" | "warning" | "muted" | "success";

export function Badge({ tone, children }: { tone: BadgeTone; children: ReactNode }) {
  return <span className={`badge tone-${tone}`}>{children}</span>;
}

export function Dot({ tone }: { tone: "success" | "danger" | "warning" | "info" | "muted" }) {
  return <span className={`dot tone-${tone}`} />;
}

export function FilterPills<T extends string>({
  options,
  active,
  onChange,
}: {
  options: { id: T; label: string; disabled?: boolean; tooltip?: string }[];
  active: T;
  onChange: (value: T) => void;
}) {
  return (
    <div className="pill-row">
      {options.map((option) => (
        <button
          key={option.id}
          type="button"
          className={`pill ${active === option.id ? "active" : ""} ${option.disabled ? "disabled" : ""}`}
          disabled={option.disabled}
          data-tooltip={option.disabled ? option.tooltip : undefined}
          onClick={() => !option.disabled && onChange(option.id)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function EmptyState({ title, body }: { title: string; body?: ReactNode }) {
  return (
    <div className="empty-state">
      <div className="title">{title}</div>
      {body && <div>{body}</div>}
    </div>
  );
}

export function ErrorBanner({ children }: { children: ReactNode }) {
  return <div className="error-banner">{children}</div>;
}

export function NoticeBanner({ children }: { children: ReactNode }) {
  return <div className="notice-banner">{children}</div>;
}

export type PriorityId = "all" | "high" | "medium" | "low";

export const PRIORITY_META: Record<Exclude<PriorityId, "all">, { label: string; tone: "danger" | "warning" | "muted" }> = {
  high: { label: "HIGH PRIORITY", tone: "danger" },
  medium: { label: "MEDIUM PRIORITY", tone: "warning" },
  low: { label: "LOW PRIORITY", tone: "muted" },
};

/** Same percentile-of-rank tiering used everywhere a finding needs a priority
 * badge, whether ranking a single finding or counting how many fall in each tier. */
export function priorityTier(rank: number | null, total: number): Exclude<PriorityId, "all"> {
  if (rank === null || total === 0) return "low";
  const percentile = rank / total;
  if (percentile <= 0.33) return "high";
  if (percentile <= 0.66) return "medium";
  return "low";
}

export function priorityOf(rank: number | null, total: number) {
  return PRIORITY_META[priorityTier(rank, total)];
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const diffMs = Date.now() - new Date(iso).getTime();
  const minutes = Math.round(diffMs / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hr ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

/** Calibrated / statistical confidence of a finding, with how much to trust it. */
export function ConfidenceBadge({ confidence }: { confidence?: FindingConfidence }) {
  if (!confidence) return null;
  if (confidence.value === null) {
    return (
      <span data-tooltip={confidence.basis ?? confidence.method}>
        <Badge tone="muted">{confidence.method === "evidence_check" ? "data check" : "uncalibrated"}</Badge>
      </span>
    );
  }
  const value = confidence.value >= 0.995 ? ">99" : confidence.value < 0.005 ? "<1" : String(Math.round(confidence.value * 100));
  const tone: BadgeTone = confidence.value >= 0.6 ? "danger" : confidence.value >= 0.25 ? "warning" : "muted";
  const grade = confidence.grade === "good" ? "" : confidence.grade === "weak" ? " · weak calibration" : "";
  const tooltip = confidence.method === "statistical" ? "1 − adjusted p-value of the correlation test" : "Calibrated on labelled synthetic ground truth";
  return (
    <span data-tooltip={tooltip}>
      <Badge tone={tone}>CONF {value}%{grade}</Badge>
    </span>
  );
}

export function methodOf(ruleVersion: string): { label: string; tone: BadgeTone } {
  if (ruleVersion.startsWith("anomaly-stack-")) return { label: "ml", tone: "ml" };
  if (ruleVersion === "network-correlation-v1") return { label: "network", tone: "warning" };
  return { label: "deterministic", tone: "deterministic" };
}

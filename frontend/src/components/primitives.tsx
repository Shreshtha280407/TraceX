import type { ReactNode } from "react";

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

import { Bell, LogOut, Search } from 'lucide-react'

interface TopBarProps {
  breadcrumb: string
  userName: string
  userRole: string
  statusLabel?: string
  /** Provisioners have no operational/case search surface. */
  showInvestigativeControls?: boolean
  onLogout?: () => void
}

/**
 * Charcoal shell, 60px: brand mark, breadcrumb, global search, status dot,
 * notification bell, user avatar + name + role (Section 4).
 */
export function TopBar({
  breadcrumb,
  userName,
  userRole,
  statusLabel = 'All systems normal',
  showInvestigativeControls = true,
  onLogout,
}: TopBarProps) {
  const initial = userName.trim().charAt(0).toUpperCase() || '?'

  return (
    <header className="flex h-topbar shrink-0 items-center justify-between gap-6 bg-shell-topbar px-6 text-shell-text">
      <div className="flex min-w-0 items-center gap-4">
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-control bg-crimson text-sm font-bold text-shell-text">
          T
        </span>
        <span className="truncate font-mono text-xs uppercase tracking-wider text-shell-text-dim">
          {breadcrumb}
        </span>
      </div>

      {showInvestigativeControls ? (
        <div className="flex min-w-0 flex-1 max-w-md items-center gap-2 rounded-control border border-shell-text-dim/20 bg-black/10 px-3 py-1.5">
          <Search size={14} className="shrink-0 text-shell-text-dim" aria-hidden="true" />
          <input
            type="search"
            placeholder="Search cases, entities, evidence IDs..."
            className="w-full bg-transparent text-sm text-shell-text placeholder:text-shell-text-dim focus:outline-none"
          />
        </div>
      ) : (
        <div className="flex-1" />
      )}

      <div className="flex shrink-0 items-center gap-5">
        {showInvestigativeControls ? (
          <>
            <div className="hidden items-center gap-2 sm:flex">
              <span className="h-2 w-2 rounded-full bg-palm" aria-hidden="true" />
              <span className="text-xs text-shell-text-dim">{statusLabel}</span>
            </div>
            <button
              type="button"
              aria-label="Notifications"
              className="relative text-shell-text-dim hover:text-shell-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-crimson"
            >
              <Bell size={18} />
            </button>
          </>
        ) : null}
        <div className="flex items-center gap-2">
          <span className="flex h-8 w-8 items-center justify-center rounded-full bg-shell-nav-2 text-sm font-semibold text-shell-text">
            {initial}
          </span>
          <div className="hidden flex-col leading-tight md:flex">
            <span className="text-sm font-medium text-shell-text">{userName}</span>
            <span className="text-xs text-shell-text-dim">{userRole}</span>
          </div>
        </div>
        {onLogout ? (
          <button
            type="button"
            onClick={onLogout}
            aria-label="Log out"
            className="text-shell-text-dim hover:text-shell-text focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-crimson"
          >
            <LogOut size={18} />
          </button>
        ) : null}
      </div>
    </header>
  )
}

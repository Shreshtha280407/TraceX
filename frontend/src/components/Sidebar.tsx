import { NavLink } from 'react-router-dom'
import { ROLE_ACTIONS } from '../lib/auth/permissions'
import { useAuthStore } from '../lib/auth/store'
import { sidebarSections } from '../lib/navigation'

interface SidebarProps {
  /** Pending-review-style counts keyed by page path; rendered as trailing badges. */
  counts?: Record<string, number>
}

/**
 * Evergreen shell, 224px, sectioned with uppercase 10.5px muted labels;
 * active item via a solid crimson pill; counts shown as small trailing
 * badges (Section 4).
 *
 * RBAC/ABAC (Section 6): a page whose `requiresCaseAction` the active
 * case's role doesn't grant is not shown at all -- never a dead link that
 * only fails after a click. A section that ends up with zero visible pages
 * (e.g. no active case yet) is omitted entirely rather than shown empty.
 */
export function Sidebar({ counts = {} }: SidebarProps) {
  const activeCaseId = useAuthStore((state) => state.activeCaseId)
  const caseMemberships = useAuthStore((state) => state.caseMemberships)
  const activeRole = caseMemberships.find(
    (m) => m.case_id === activeCaseId && m.is_active,
  )?.role

  const sections = sidebarSections()
    .map(({ section, pages }) => ({
      section,
      pages: pages.filter((page) => {
        if (!page.requiresCaseAction) return true
        if (!activeRole) return false
        return ROLE_ACTIONS[activeRole].includes(page.requiresCaseAction)
      }),
    }))
    .filter(({ pages }) => pages.length > 0)

  return (
    <nav className="flex w-sidebar shrink-0 flex-col gap-6 overflow-y-auto bg-shell-nav px-3 py-6">
      {sections.map(({ section, pages }) => (
        <div key={section} className="flex flex-col gap-1">
          <span className="px-3 pb-1 text-[10.5px] font-semibold uppercase tracking-wider text-shell-text-dim">
            {section}
          </span>
          {pages.map((page) => {
            const count = counts[page.path]
            return (
              <NavLink
                key={page.path}
                to={page.path}
                className={({ isActive }) =>
                  `flex items-center gap-3 rounded-control px-3 py-2 text-sm font-medium transition-colors ${
                    isActive
                      ? 'bg-crimson text-shell-text'
                      : 'text-shell-text-dim hover:bg-shell-nav-2 hover:text-shell-text'
                  }`
                }
              >
                <page.icon size={16} className="shrink-0" aria-hidden="true" />
                <span className="min-w-0 flex-1 truncate">{page.label}</span>
                {count ? (
                  <span className="shrink-0 rounded-pill bg-black/20 px-2 py-0.5 text-xs font-semibold">
                    {count}
                  </span>
                ) : null}
              </NavLink>
            )
          })}
        </div>
      ))}
    </nav>
  )
}

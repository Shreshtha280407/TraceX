import type { ReactNode } from 'react'
import { Card } from './Card'

export interface DataTableColumn {
  key: string
  header: string
  /** Right/left align content; status-style columns usually read better centered-left. */
  className?: string
}

interface DataTableProps {
  columns: DataTableColumn[]
  children: ReactNode
}

/** Grid-based header + row shell; rows use DataTableRow (Section 4). */
export function DataTable({ columns, children }: DataTableProps) {
  return (
    <Card className="overflow-hidden">
      <div
        className="grid gap-4 border-b border-card-border px-5 py-3"
        style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(0, 1fr))` }}
      >
        {columns.map((col) => (
          <span key={col.key} className={`text-xs font-medium uppercase tracking-wide text-text-faint ${col.className ?? ''}`}>
            {col.header}
          </span>
        ))}
      </div>
      <div>{children}</div>
    </Card>
  )
}

interface DataTableRowProps {
  columns: DataTableColumn[]
  primary: string
  secondary?: string
  cells: Record<string, ReactNode>
  onClick?: () => void
}

/** Grid-based row: primary/secondary text stack in first column, tinted badges elsewhere, row-divider between rows. */
export function DataTableRow({ columns, primary, secondary, cells, onClick }: DataTableRowProps) {
  const [firstCol, ...restCols] = columns
  const isInteractive = Boolean(onClick)

  return (
    <div
      role={isInteractive ? 'button' : undefined}
      tabIndex={isInteractive ? 0 : undefined}
      onClick={onClick}
      onKeyDown={
        isInteractive
          ? (event) => {
              if (event.key === 'Enter' || event.key === ' ') onClick?.()
            }
          : undefined
      }
      className={`grid items-center gap-4 border-b border-card-border px-5 py-3 last:border-b-0 ${
        isInteractive ? 'cursor-pointer hover:bg-canvas/10 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-crimson' : ''
      }`}
      style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(0, 1fr))` }}
    >
      <div key={firstCol.key} className="flex flex-col">
        <span className="text-sm font-medium text-text">{primary}</span>
        {secondary ? <span className="font-mono text-xs text-text-faint">{secondary}</span> : null}
      </div>
      {restCols.map((col) => (
        <div key={col.key} className={col.className}>
          {cells[col.key]}
        </div>
      ))}
    </div>
  )
}

import type { SourceLocatorView } from './api/graph-types'

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['year', 31536000],
  ['month', 2592000],
  ['week', 604800],
  ['day', 86400],
  ['hour', 3600],
  ['minute', 60],
]

const relativeFormatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })

/** A real relative-time label ("12 min ago") for a real ISO timestamp -- never a fabricated one. */
export function formatRelativeTime(iso: string): string {
  const deltaSeconds = (new Date(iso).getTime() - Date.now()) / 1000
  for (const [unit, secondsInUnit] of UNITS) {
    if (Math.abs(deltaSeconds) >= secondsInUnit) {
      return relativeFormatter.format(Math.round(deltaSeconds / secondsInUnit), unit)
    }
  }
  return deltaSeconds >= -5 ? 'just now' : relativeFormatter.format(Math.round(deltaSeconds), 'second')
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`
}

function formatMsAsClock(ms: number | null): string {
  if (ms === null) return '?'
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

/**
 * Evidence Viewer's real drill-down rendering (Section 5, page 13): every
 * `SourceLocator` field is individually optional (modality-dependent -- a
 * PDF sets `page`, a spreadsheet sets `sheet`/`row`/`column`, a video sets
 * `frame_number`/`time_*_ms`), so this renders only whichever fields the
 * source extractor actually populated, never a fabricated placeholder.
 */
export function formatSourceLocator(locator: SourceLocatorView): { label: string; value: string }[] {
  const parts: { label: string; value: string }[] = []
  if (locator.page !== null) parts.push({ label: 'Page', value: String(locator.page) })
  if (locator.span_start !== null || locator.span_end !== null) {
    parts.push({ label: 'Span', value: `${locator.span_start ?? '?'}–${locator.span_end ?? '?'}` })
  }
  if (locator.sheet !== null) parts.push({ label: 'Sheet', value: locator.sheet })
  if (locator.row !== null) parts.push({ label: 'Row', value: String(locator.row) })
  if (locator.column !== null) parts.push({ label: 'Column', value: String(locator.column) })
  if (locator.json_path !== null) parts.push({ label: 'JSON path', value: locator.json_path })
  if (locator.frame_number !== null) parts.push({ label: 'Frame', value: String(locator.frame_number) })
  if (locator.time_start_ms !== null || locator.time_end_ms !== null) {
    parts.push({
      label: 'Time',
      value: `${formatMsAsClock(locator.time_start_ms)}–${formatMsAsClock(locator.time_end_ms)}`,
    })
  }
  if (locator.message_id !== null) parts.push({ label: 'Message', value: locator.message_id })
  if (
    locator.bbox_x_min !== null &&
    locator.bbox_y_min !== null &&
    locator.bbox_x_max !== null &&
    locator.bbox_y_max !== null
  ) {
    parts.push({
      label: 'Region',
      value: `(${locator.bbox_x_min.toFixed(2)}, ${locator.bbox_y_min.toFixed(2)})–(${locator.bbox_x_max.toFixed(2)}, ${locator.bbox_y_max.toFixed(2)})`,
    })
  }
  return parts
}

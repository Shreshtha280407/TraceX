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

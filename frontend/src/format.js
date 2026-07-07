// Freshness label for an `as_of` ISO timestamp — the app always shows the user
// how fresh a figure is.
//
// The backend stamps naive UTC (datetime.utcnow().isoformat(), no timezone
// suffix). `new Date("...")` on a suffix-less string parses as LOCAL time, so on
// the Fiji host (UTC+12) every figure read as 12 hours old ("12 h ago" for data
// seconds old). Treat a bare timestamp as UTC by appending 'Z' when it carries
// no timezone designator.
export function asUtc(iso) {
  if (typeof iso !== 'string') return iso
  // Has a zone already? (Z, or +hh:mm / -hh:mm after the time component.)
  if (/[zZ]$/.test(iso) || /[+-]\d{2}:?\d{2}$/.test(iso)) return iso
  // Only add Z to an actual date-time (has a 'T'); leave plain dates alone.
  return iso.includes('T') ? `${iso}Z` : iso
}

export function relativeTime(iso, now = Date.now()) {
  if (!iso) return 'never'
  const then = new Date(asUtc(iso)).getTime()
  if (Number.isNaN(then)) return 'unknown'
  const secs = Math.max(0, Math.round((now - then) / 1000))
  if (secs < 60) return 'just now'
  const mins = Math.round(secs / 60)
  if (mins < 60) return `${mins} min ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `${hrs} h ago`
  const days = Math.round(hrs / 24)
  return `${days} d ago`
}

export function num(n) {
  if (n == null) return '—'
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: 2 })
}

export function money(n, currency = 'FJD') {
  if (n == null) return '—'
  return `${currency} ${Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

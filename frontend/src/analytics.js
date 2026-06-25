// Pure helpers for the Analytics page (Phase 5). The backend
// (GET /api/analytics) computes the figures from canonical data and BC's
// reported match; these helpers only shape the response for the tiles/tables
// and gate the admin-only "push to warehouse" action. The app never fabricates
// money — it reflects what the backend returns.

// Only an ADMIN may push the figures to the Azure SQL warehouse.
// Contract (Phase 5): analytics read = any authed user; push = ADMIN.
export function canPushWarehouse(user) {
  return !!user && user.role === 'ADMIN'
}

// On-time-delivery rate as a 0..1 fraction -> a whole-percent string, or '—'
// when there is no sample to measure against.
export function pct(rate, sample) {
  if (sample === 0 || rate == null) return '—'
  return `${Math.round(Number(rate) * 100)}%`
}

// By-vendor spend rows, largest first, with each vendor's share of the total
// (0..1) for the inline bar. Guards a zero/absent total (share 0). Pure: does
// not mutate the input. The backend (GET /api/analytics) keys each row's money
// as `spend` (analytics._spend -> {"vendor": name, "spend": <number>}); we
// expose it as `amount` since Analytics.jsx consumes r.amount.
export function vendorSpendRows(byVendor = [], total = 0) {
  const t = Number(total || 0)
  return [...byVendor]
    .map((v) => {
      const amount = Number(v.spend || 0)
      return {
        vendor: v.vendor || '—',
        amount,
        share: t > 0 ? amount / t : 0,
      }
    })
    .sort((a, b) => b.amount - a.amount)
}

// How a warehouse.push status reads in the UI. The guarded writer returns
// 'skipped:not-configured' until AZURE_SQL_DSN is set; a live write returns
// 'written:<n>' (gateway.warehouse: f"written:{len(rows)}") — or 'ok'/'written'
// — and an 'error:' string on failure.
export function warehouseStatusLabel(status) {
  if (!status) return 'not pushed yet'
  if (status === 'ok' || status === 'written') return 'written'
  if (typeof status === 'string' && status.startsWith('written:')) {
    const n = status.slice('written:'.length)
    return n ? `written — ${n} rows` : 'written'
  }
  if (typeof status === 'string' && status.startsWith('written')) return 'written'
  if (status === 'skipped:not-configured') return 'skipped — warehouse not configured'
  if (typeof status === 'string' && status.startsWith('error:')) {
    return `failed — ${status.slice('error:'.length)}`
  }
  return String(status)
}

// Flatten the {table: status} map the push endpoint returns into sorted rows
// for the result table.
export function pushResultRows(result = {}) {
  return Object.entries(result || {})
    .map(([table, status]) => ({ table, status, label: warehouseStatusLabel(status) }))
    .sort((a, b) => a.table.localeCompare(b.table))
}

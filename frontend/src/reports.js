// Pure helpers for the Reports screen. Each report is a backend register
// (/api/reports/*) serving JSON for the preview table and CSV for download —
// same columns, same order, one backend serializer.

export const REPORTS = [
  {
    key: 'purchase-orders',
    title: 'PO register',
    description: 'Every purchase order with vendor, status and fulfilment progress.',
    path: '/api/reports/purchase-orders',
  },
  {
    key: 'receipts',
    title: 'Receipt log',
    description: 'Every received line (GRN), traceable to its PO and BC receipt.',
    path: '/api/reports/receipts',
  },
  {
    key: 'spend-by-month',
    title: 'Spend by month',
    description: 'Monthly spend per vendor — received quantity × ordered unit price.',
    path: '/api/reports/spend-by-month',
  },
]

// The CSV download href for a report path.
export function csvHref(path) {
  return `${path}?format=csv`
}

// Cell formatting for the preview table: ratios in *_pct columns render as
// percentages, numbers localise, null/undefined shows an em dash.
export function fmtCell(column, value) {
  if (value == null) return '—'
  if (typeof value === 'number') {
    if (column.endsWith('_pct')) return `${(value * 100).toFixed(1)}%`
    return value.toLocaleString(undefined, { maximumFractionDigits: 2 })
  }
  return String(value)
}

// How many preview rows the screen shows (the CSV always has everything).
export const PREVIEW_ROWS = 15

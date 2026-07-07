// Pure helpers for the paper-planning SOP screens (Order Page, Forecasts,
// Shipments). The backend owns the coverage maths (usage basis → months of
// stock → container plans); these only gate mutations by role and shape values
// for display, following the planning.js / receiving.js convention.

import { PLANNER_ROLES } from './planning.js'

// Statuses a shipment moves through (backend contract).
export const SHIPMENT_STATUSES = ['CONFIRMED', 'ON_WATER', 'ARRIVED', 'RECEIVED', 'CANCELLED']

// PO statuses that cannot take a new shipment — the backend 409s on these.
export const NO_SHIPMENT_PO_STATUSES = ['DRAFT', 'CANCELLED', 'CLOSED']

// Can this user drive paper planning (import usage, suggest coverage orders,
// write forecasts, record shipments)? Same OFFICER/ADMIN gate as Phase 4.
export function canPlanPaper(user) {
  return !!user && PLANNER_ROLES.includes(user.role)
}

// Map months-of-stock -> a .badge modifier class, reusing the requisition badge
// palette exactly like poStatusBadge/matchBadge do: red (danger) under half the
// cover target, amber (warn) under cover, green (ok) at/above cover, grey when
// coverage is unknowable (no usage basis → months_of_stock null).
export function coverBadge(months, coverMonths = 3) {
  const m = Number(months)
  if (months == null || !Number.isFinite(m)) return 'draft'
  if (m < coverMonths / 2) return 'rejected'
  if (m < coverMonths) return 'in_approval'
  return 'approved'
}

// Is this grade under the cover target? (row-warn + tile gate; null months —
// no usage basis — is "unknown", not "below").
export function belowCover(months, coverMonths = 3) {
  const m = Number(months)
  if (months == null || !Number.isFinite(m)) return false
  return m < coverMonths
}

// Kilograms -> display tonnes, e.g. 31474 -> '31.5 t'.
export function fmtTonnes(kg) {
  const n = Number(kg)
  if (kg == null || !Number.isFinite(n)) return '—'
  return `${(n / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })} t`
}

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

// 'YYYY-MM' -> 'Jul 2026'. Malformed input comes back unchanged (blank -> '—')
// so a bad period is still visible rather than hidden.
export function monthLabel(period) {
  const m = /^(\d{4})-(\d{2})$/.exec(period || '')
  if (!m) return period || '—'
  const idx = Number(m[2]) - 1
  if (idx < 0 || idx > 11) return period
  return `${MONTH_NAMES[idx]} ${m[1]}`
}

// Shipment status -> .badge modifier, reusing the requisition palette:
// booked (blue) → on the water (amber) → arrived (green) → received (purple).
const SHIPMENT_BADGE = {
  CONFIRMED: 'submitted',
  ON_WATER: 'in_approval',
  ARRIVED: 'approved',
  RECEIVED: 'closed',
  CANCELLED: 'cancelled',
}
export function shipmentStatusBadge(status) {
  return SHIPMENT_BADGE[status] || 'demo'
}

// Which statuses a shipment may advance to from here: forward through the
// lifecycle plus CANCELLED; RECEIVED and CANCELLED are terminal.
export function shipmentNextStatuses(status) {
  const flow = ['CONFIRMED', 'ON_WATER', 'ARRIVED', 'RECEIVED']
  const i = flow.indexOf(status)
  if (i === -1) return [] // CANCELLED / unknown
  const forward = flow.slice(i + 1)
  return forward.length ? [...forward, 'CANCELLED'] : []
}

// Should the record-shipment form show on a PO? Officer/admin AND the PO is in
// a state that can carry a shipment (backend enforces with 403/409).
export function canRecordShipment(user, poStatus) {
  return canPlanPaper(user) && !NO_SHIPMENT_PO_STATUSES.includes(poStatus)
}

// Shape the record-shipment form into the POST body: trims strings, drops
// blanks, drops non-numeric/negative quantities. The backend validates again.
export function buildShipmentPayload(form = {}) {
  const body = {}
  for (const k of ['vessel', 'etd', 'eta', 'notes']) {
    const v = String(form[k] ?? '').trim()
    if (v) body[k] = v
  }
  for (const k of ['rolls', 'weight_kg', 'fcl_count']) {
    const raw = form[k]
    if (raw === '' || raw == null) continue
    const n = Number(raw)
    if (Number.isFinite(n) && n >= 0) body[k] = n
  }
  return body
}

// Forecast rows -> PUT /api/forecasts body. Drops rows missing customer/sku,
// with a malformed period, or a blank/invalid/negative carton qty. Zero is a
// legal quantity (it kills a forecast month). Trims strings.
export function buildForecastPayload(rows = []) {
  const lines = []
  for (const r of rows || []) {
    const customer = String(r.customer ?? '').trim()
    const sku = String(r.sku ?? '').trim()
    const period = String(r.period ?? '').trim()
    if (!customer || !sku) continue
    if (!/^\d{4}-\d{2}$/.test(period)) continue
    if (r.qty_cartons === '' || r.qty_cartons == null) continue
    const qty = Number(r.qty_cartons)
    if (!Number.isFinite(qty) || qty < 0) continue
    lines.push({ customer, sku, period, qty_cartons: qty })
  }
  return { lines }
}

// One month cell from a spreadsheet -> 'YYYY-MM'. Accepts 'YYYY-MM', 'YYYY/MM',
// 'M/YYYY', 'MM/YYYY' (month 1..12); anything else is null.
function normalizePeriod(cell) {
  const s = String(cell ?? '').trim()
  const ym = /^(\d{4})[-/](\d{1,2})$/.exec(s)
  const my = /^(\d{1,2})[-/](\d{4})$/.exec(s)
  let y
  let m
  if (ym) { y = ym[1]; m = ym[2] } else if (my) { m = my[1]; y = my[2] } else return null
  const month = Number(m)
  if (month < 1 || month > 12) return null
  return `${y}-${String(month).padStart(2, '0')}`
}

// Paste-from-Excel -> forecast lines. A tabbed row (Excel copy-paste) splits on
// tabs only; a manual paste with no tab falls back to comma/semicolon. Accepts
// 3 columns [sku, period, cartons] (customer = defaultCustomer) or 4 columns
// [customer, sku, period, cartons] (a blank customer cell falls back to the
// default, as merged customer cells paste blank). Blank lines are ignored
// outright; every other row that can't import — headers (qty cell not a finite
// number >= 0), bad periods, missing customer/sku, wrong column counts — counts
// in `skipped`. Zero cartons is kept (it kills a forecast month).
export function parseForecastPaste(text, defaultCustomer = '') {
  const fallback = String(defaultCustomer ?? '').trim()
  const lines = []
  let skipped = 0
  for (const raw of String(text ?? '').split(/\r?\n/)) {
    if (!raw.trim()) continue
    // Excel copy-paste is tab-delimited: split a tabbed row on tabs ONLY so a
    // comma-bearing customer name ("Visy, Ltd") and a thousands-separated
    // quantity ("42,000") stay in one cell. Only a manual (no-tab) paste falls
    // back to comma/semicolon.
    const tabbed = raw.includes('\t')
    const cells = (tabbed ? raw.split('\t') : raw.split(/[,;]/)).map((c) => c.trim())
    let customer
    let rest
    if (cells.length === 3) {
      customer = fallback
      rest = cells
    } else if (cells.length === 4) {
      customer = cells[0] || fallback
      rest = cells.slice(1)
    } else {
      skipped += 1
      continue
    }
    const [sku, periodCell, qtyCell] = rest
    const period = normalizePeriod(periodCell)
    // Tab-split rows can carry thousands separators ("42,000"); strip them
    // before Number(). Comma/semicolon rows can't (a comma is a delimiter).
    const qtyClean = tabbed ? qtyCell.replace(/,/g, '') : qtyCell
    const qty = qtyClean === '' ? NaN : Number(qtyClean)
    if (!customer || !sku || !period || !Number.isFinite(qty) || qty < 0) {
      skipped += 1
      continue
    }
    lines.push({ customer, sku, period, qty_cartons: qty })
  }
  return { lines, skipped }
}

// Earliest next arrival across order-page rows (next_eta is an ISO date, so
// lexicographic sort is chronological). Null when nothing is inbound.
export function nextArrival(rows = []) {
  const etas = (rows || []).map((r) => r.next_eta).filter(Boolean).sort()
  return etas[0] || null
}

// Gate for 'Create suggested requisition' — mirrors the backend's own
// conditions rather than a proxy metric: the endpoint no-ops when there are no
// container plans and 409s while a coverage requisition is still in flight.
// (below_cover is a display metric: allocated demand can require an order even
// when every grade shows at/above cover.) Returns {enabled, reason}.
export function canCreateSuggestion(page = {}) {
  const open = page.open_coverage_requisition
  if (open) {
    return {
      enabled: false,
      reason: `Coverage requisition ${open.number} is still in flight (${open.status})`,
    }
  }
  if (!(page.container_plans || []).length) {
    return { enabled: false, reason: 'Nothing to order — all grades at or above cover' }
  }
  return { enabled: true, reason: null }
}

// Basis chip text: flags a partially entered forecast window ('FORECAST 1/3')
// so a planner can tell 'months not yet forecast' from a full window.
export function basisLabel(row = {}, coverMonths = 3) {
  const basis = row.basis || 'NONE'
  const covered = Number(row.forecast_periods)
  if (basis === 'FORECAST' && Number.isFinite(covered) && covered > 0 && covered < coverMonths) {
    return `FORECAST ${covered}/${coverMonths}`
  }
  return basis
}

// Freshest stock timestamp across order-page rows (max as_of, matching the
// backend's _latest_as_of convention); null when no row carries one.
export function latestAsOf(rows = []) {
  const stamps = (rows || []).map((r) => r.as_of).filter(Boolean).sort()
  return stamps.length ? stamps[stamps.length - 1] : null
}

// Default value for a <input type="month">: the LOCAL current month. Fiji is
// UTC+12, so toISOString()-based defaults would show last month for the first
// twelve hours of every month.
export function localMonthValue(d = new Date()) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

// Reconciliation rows needing investigation (SOP §9: variance by grade/deckle).
// The backend already sorts flagged-first/biggest-first; this just cuts the
// agreeing tail for display.
export function flaggedVariances(recon = {}) {
  return (recon.rows || []).filter((r) => r.flagged)
}

// Signed-KG display for a variance: '+760 kg' / '−760 kg'; null (no BC figure
// to net against) reads as 'not in BC'.
export function fmtVariance(kg) {
  const n = Number(kg)
  if (kg == null || !Number.isFinite(n)) return 'not in BC'
  const sign = n > 0 ? '+' : n < 0 ? '−' : ''
  return `${sign}${Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 1 })} kg`
}

// RFC-4180 cell: null/undefined -> empty; a cell containing a comma, quote or
// newline is double-quoted with internal quotes doubled. Numbers pass through
// raw (no locale formatting) so the CSV re-imports cleanly.
export function csvCell(v) {
  if (v == null) return ''
  // Numbers pass through untouched so exports re-import cleanly (a negative
  // figure must stay a number, not become quoted text).
  let s = String(v)
  if (typeof v !== 'number' && s && '=+-@\t\r'.includes(s[0])) {
    // Neutralise spreadsheet formula injection: a TEXT cell starting with
    // = + - @ (or a leading tab/CR Excel trims before them) runs as a formula
    // on open, and vendor/SKU/grade strings originate from BC. Prefix a quote
    // so it stays literal text.
    s = `'${s}`
  }
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

const ORDER_PAGE_HEADER = [
  'SKU', 'Grade', 'Deckle mm', 'Basis', 'Forecast periods', 'Monthly use kg',
  '3-mo need kg', 'On hand kg', 'Allocated kg', 'In transit kg', 'Next ETA',
  'Months of stock', 'Suggested order kg', 'Vendor', 'Lead time days', 'Stock as of',
]

// GET /api/planning/order-page -> a CSV the manager can file like the old Visy
// workbook: one row per grade under a fixed header, then (only when something
// needs ordering) a blank line and a 'Container plans' section — each vendor's
// FCL block followed by its ' - SKU' lines. Pure string shaping; the Blob /
// download wiring stays in the page component (no DOM APIs here).
export function orderPageCsv(page = {}) {
  const out = [ORDER_PAGE_HEADER.join(',')]
  for (const r of page.rows || []) {
    out.push([
      r.sku, r.grade, r.deckle_mm, r.basis, r.forecast_periods, r.monthly_usage,
      r.usage_3mo, r.on_hand, r.allocated, r.in_transit, r.next_eta,
      r.months_of_stock, r.requirement_kg, r.vendor, r.lead_time_days, r.as_of,
    ].map(csvCell).join(','))
  }
  const plans = page.container_plans || []
  if (plans.length > 0) {
    out.push('', 'Container plans', ['Vendor', 'Containers', 'Total kg'].join(','))
    for (const p of plans) {
      out.push([p.vendor, p.containers, p.total_kg].map(csvCell).join(','))
      for (const l of p.lines || []) {
        out.push([` - ${l.sku}`, l.requirement_kg, l.order_kg].map(csvCell).join(','))
      }
    }
  }
  return `${out.join('\n')}\n`
}

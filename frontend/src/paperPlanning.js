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

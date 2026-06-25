// Pure helpers for PO receiving (Phase 5), shared by PurchaseOrderDetail. The
// backend is the source of truth — it returns 403 (RBAC), 409 (invalid PO
// state) and 400 (over-receipt) — these only decide what the UI *shows* and how
// it shapes the receive payload, so the page stays honest about a user's role
// and a PO's state.

import { OFFICER_ROLES } from './purchaseOrders.js'

// Statuses a PO can be received against. Contract: PO_ISSUED / ACKNOWLEDGED /
// PARTIALLY_RECEIVED accept a GRN; anything else (DRAFT / RECEIVED / MATCHED /
// CANCELLED) does not.
export const RECEIVABLE_STATUSES = ['PO_ISSUED', 'ACKNOWLEDGED', 'PARTIALLY_RECEIVED']

// Can this user receive goods against a PO? OFFICER / ADMIN only (same as the
// other PO mutations).
export function canReceivePO(user) {
  return !!user && OFFICER_ROLES.includes(user.role)
}

// Should the receive form show? Officer/admin AND the PO is in a receivable
// state.
export function canShowReceiveForm(user, status) {
  return canReceivePO(user) && RECEIVABLE_STATUSES.includes(status)
}

// Per-line received-vs-ordered view. The PO detail (_detail in
// backend/app/domain/purchasing.py) returns each line keyed `po_line_id`,
// ordered `quantity`, and a cumulative `received_qty` (sum of Receipt rows for
// that line). We key the UI row off `po_line_id` (falling back to `id` for
// safety) so the receive form binds each input to the right line and
// buildReceivePayload emits the real po_line_id the backend expects. Derive the
// still-outstanding quantity, clamped at 0 so a (shouldn't-happen) over-receipt
// never shows negative.
export function lineReceiptRows(lines = []) {
  return lines.map((l) => {
    const ordered = Number(l.quantity || 0)
    const received = Number(l.received_qty || 0)
    const outstanding = Math.max(0, ordered - received)
    return {
      id: l.po_line_id ?? l.id,
      sku: l.sku,
      name: l.name,
      ordered,
      received,
      outstanding,
      fully_received: outstanding <= 0,
    }
  })
}

// Build the receive request body from the per-line input map {po_line_id: qty}.
// Drops blank/zero/invalid quantities; trims an optional GRN no + location. Only
// positive-quantity lines are sent. Returns {grn_no?, location?, lines:[...]}.
export function buildReceivePayload({ quantities = {}, grnNo = '', location = '' } = {}) {
  const lines = Object.entries(quantities)
    .map(([po_line_id, q]) => ({ po_line_id, quantity: Number(q) }))
    .filter((l) => Number.isFinite(l.quantity) && l.quantity > 0)
  const body = { lines }
  const grn = grnNo.trim()
  const loc = location.trim()
  if (grn) body.grn_no = grn
  if (loc) body.location = loc
  return body
}

// Does the payload have at least one line to receive?
export function hasReceiptLines(payload) {
  return !!payload && Array.isArray(payload.lines) && payload.lines.length > 0
}

// Validate a draft receipt against the per-line outstanding quantities (so we
// can block an obvious over-receipt client-side; the backend still enforces it
// with a 400). Returns null when ok, else an error string.
export function validateReceipt(payload, rows = []) {
  if (!hasReceiptLines(payload)) return 'Enter a quantity to receive on at least one line.'
  const byId = new Map(rows.map((r) => [r.id, r]))
  for (const l of payload.lines) {
    const r = byId.get(l.po_line_id)
    if (r && l.quantity > r.outstanding) {
      return `Cannot receive ${l.quantity} of ${r.sku} — only ${r.outstanding} outstanding.`
    }
  }
  return null
}

// How the BC 3-way match state reads in the UI + which badge class to use. BC
// owns the match (PO·GRN·invoice); we only reflect what it reports.
const MATCH_BADGE = {
  MATCHED: 'approved',
  PENDING: 'in_approval',
  UNMATCHED: 'draft',
}
export function matchBadge(status) {
  if (!status) return 'demo'
  return MATCH_BADGE[status] || 'demo'
}

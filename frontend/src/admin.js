// Pure helpers for the Admin screen (users, roles, system health). The backend
// enforces everything (last-admin guard, role validity); these only gate what
// the UI shows and shape values for display.

export const ROLE_CODES = ['ADMIN', 'APPROVER', 'OFFICER', 'REQUESTER', 'VIEWER']

// Only an ADMIN sees the Admin screen at all.
export function canAdmin(user) {
  return !!user && user.role === 'ADMIN'
}

// Approval limit -> display text. null/undefined is 'unlimited' (how the ADMIN
// role is seeded), matching the backend contract.
export function fmtLimit(limit) {
  if (limit == null) return 'unlimited'
  const n = Number(limit)
  if (!Number.isFinite(n)) return 'unlimited'
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 })
}

// Approval-limit input -> PATCH body value. Blank means unlimited (null);
// anything non-numeric or negative is invalid -> undefined (caller blocks save).
export function parseLimit(raw) {
  const s = String(raw ?? '').trim()
  if (s === '') return null
  const n = Number(s)
  if (!Number.isFinite(n) || n < 0) return undefined
  return n
}

// Integration mode -> .badge modifier (live green, demo amber, off grey).
export function integrationBadge(mode) {
  return { live: 'live', demo: 'demo', off: 'draft' }[mode] || 'draft'
}

// Outbox status -> .badge modifier, reusing the requisition palette.
export function outboxBadge(status) {
  return {
    PENDING: 'submitted',
    SENDING: 'in_approval',
    SENT: 'approved',
    FAILED: 'rejected',
  }[status] || 'draft'
}

// Would this row edit be blocked as the last active admin? Mirrors the backend
// guard so the UI can disable the control instead of surfacing a 409.
export function isLastActiveAdmin(user, users = []) {
  if (!user || user.role !== 'ADMIN' || !user.active) return false
  return !(users || []).some(
    (u) => u.id !== user.id && u.role === 'ADMIN' && u.active,
  )
}

// Grade-preview response -> the one-line verdict the operator reads first.
// The trap this surfaces: a pattern can MATCH SKUs yet grade none of them
// (no capture group), and the resync would then classify nothing.
export function gradePreviewVerdict(body) {
  if (!body) return ''
  const graded = (body.match_count ?? 0) - (body.ungraded_matches ?? 0)
  if ((body.match_count ?? 0) === 0) {
    return 'No SKUs match this pattern.'
  }
  if (graded === 0) {
    return `${body.match_count} SKUs match but NONE would gain a grade — add a capture group ( ) around the grade part.`
  }
  let s = `${graded} of ${body.total_items} items would gain a grade (${body.distinct_grades} grades).`
  if ((body.ungraded_matches ?? 0) > 0) {
    s += ` ${body.ungraded_matches} match without capturing a grade.`
  }
  return s
}

// Purge summary -> compact display lines for the confirmation banner.
export function purgeSummaryLines(summary) {
  if (!summary) return []
  const removed = `Removed ${summary.items ?? 0} items, ${summary.vendors ?? 0} vendors, `
    + `${summary.customers ?? 0} customers, ${summary.vendor_prices ?? 0} prices, `
    + `${summary.boms ?? 0} BOMs, ${summary.forecasts ?? 0} forecasts, `
    + `${summary.usage_rows ?? 0} usage rows.`
  const lines = [removed]
  if ((summary.skipped_items || []).length) {
    lines.push(`Kept (referenced by orders): ${summary.skipped_items.join(', ')}`)
  }
  if ((summary.skipped_vendors || []).length) {
    lines.push(`Kept vendors (on purchase orders): ${summary.skipped_vendors.join(', ')}`)
  }
  return lines
}

// Pure helpers for purchase-order RBAC + status gating, shared by the
// PurchaseOrders and PurchaseOrderDetail pages. The backend is the source of
// truth (it returns 403/409 on invalid actions); these only decide which
// buttons to *show* so the UI stays honest about a user's role + the PO's
// state.

// Roles allowed to issue a PO / post to BC / drive the outbox.
// Contract (Phase 3): OFFICER / ADMIN mutate; only ADMIN may force outbox.
export const OFFICER_ROLES = ['OFFICER', 'ADMIN']

const BADGE = {
  DRAFT: 'draft',
  PO_ISSUED: 'submitted',
  ACKNOWLEDGED: 'approved',
  PARTIALLY_RECEIVED: 'in_approval',
  RECEIVED: 'closed',
  CANCELLED: 'cancelled',
}

// Map a PO status -> a css modifier class for the .badge element. Reuses the
// requisition badge palette so the UI stays consistent.
export function poStatusBadge(status) {
  return BADGE[status] || 'demo'
}

// Can this user issue / post a purchase order at all?
export function canIssuePO(user) {
  return !!user && OFFICER_ROLES.includes(user.role)
}

// Only an ADMIN may manually drive the integration outbox (retry posting).
export function canProcessOutbox(user) {
  return !!user && user.role === 'ADMIN'
}

// Which action buttons should show on a PO for this user, given its status.
//   issue   — OFFICER/ADMIN, only while DRAFT (DRAFT -> PO_ISSUED)
//   outbox  — ADMIN, once a PO has been issued (retry the BC post)
export function availablePOActions(user, status) {
  return {
    issue: canIssuePO(user) && status === 'DRAFT',
    outbox: canProcessOutbox(user) && status !== 'DRAFT',
  }
}

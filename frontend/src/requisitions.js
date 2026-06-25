// Pure helpers for requisition RBAC + state gating, shared by the Requisitions,
// RequisitionDetail and Approvals pages. The backend is the source of truth
// (it returns 403/409 on invalid actions); these only decide which buttons to
// *show* so the UI stays honest about a user's role + the requisition's state.

// Roles allowed to create / edit / submit / cancel a requisition.
// Contract: REQUESTER / OFFICER / ADMIN. (VIEWER + bare APPROVER cannot raise.)
export const REQUESTER_ROLES = ['REQUESTER', 'OFFICER', 'ADMIN']
// Roles allowed to approve / reject (subject to the approval-limit rule).
export const APPROVER_ROLES = ['OFFICER', 'APPROVER', 'ADMIN']

// Pre-approval states a requisition can be cancelled from.
export const CANCELLABLE = ['DRAFT', 'SUBMITTED', 'IN_APPROVAL']

const BADGE = {
  DRAFT: 'draft',
  SUBMITTED: 'submitted',
  IN_APPROVAL: 'in_approval',
  APPROVED: 'approved',
  REJECTED: 'rejected',
  CANCELLED: 'cancelled',
  CLOSED: 'closed',
}

// Map a status -> a css modifier class for the .badge element.
export function statusBadge(status) {
  return BADGE[status] || 'demo'
}

// Can this user raise / edit / submit / cancel requisitions at all?
export function canRequest(user) {
  return !!user && REQUESTER_ROLES.includes(user.role)
}

// Can this user approve the given estimated amount?
//   role in APPROVER_ROLES AND (limit is null/undefined => unlimited, OR limit >= amount)
export function canApproveAmount(user, amount) {
  if (!user || !APPROVER_ROLES.includes(user.role)) return false
  const limit = user.approval_limit
  if (limit == null) return true // ADMIN / unlimited
  return Number(limit) >= Number(amount || 0)
}

// Which action buttons should show on a requisition for this user, given its
// status, estimated amount and (for cancel) who raised it.
//
// `requesterEmail` is the requisition's requester. cancel is gated on
// ownership/admin to match the backend, which only lets the requester or an
// ADMIN cancel (an OFFICER viewing someone else's req would otherwise be shown a
// Cancel button that 403s). edit/submit stay role-only: the backend's
// _require_owner_or_admin lets an OFFICER act on any requisition there.
export function availableActions(user, status, amount, requesterEmail) {
  const requester = canRequest(user)
  const ownsOrAdmin = !!user && (user.role === 'ADMIN' || user.email === requesterEmail)
  return {
    edit: requester && status === 'DRAFT',
    submit: requester && status === 'DRAFT',
    cancel: ownsOrAdmin && CANCELLABLE.includes(status),
    approve: status === 'IN_APPROVAL' && canApproveAmount(user, amount),
    reject: status === 'IN_APPROVAL' && APPROVER_ROLES.includes(user?.role),
  }
}

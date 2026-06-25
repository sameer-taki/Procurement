import { describe, it, expect } from 'vitest'
import {
  canRequest, canApproveAmount, availableActions, statusBadge,
} from './requisitions.js'

const requester = { role: 'REQUESTER', approval_limit: null, email: 'req@golden.com.fj' }
const officer = { role: 'OFFICER', approval_limit: 5000, email: 'officer@golden.com.fj' }
const approver = { role: 'APPROVER', approval_limit: 50000, email: 'approver@golden.com.fj' }
const admin = { role: 'ADMIN', approval_limit: null, email: 'admin@golden.com.fj' }
const viewer = { role: 'VIEWER', approval_limit: null, email: 'viewer@golden.com.fj' }

describe('canRequest', () => {
  it('lets requester/officer/admin raise', () => {
    expect(canRequest(requester)).toBe(true)
    expect(canRequest(officer)).toBe(true)
    expect(canRequest(admin)).toBe(true)
  })
  it('blocks viewer and bare approver', () => {
    expect(canRequest(viewer)).toBe(false)
    expect(canRequest(approver)).toBe(false)
    expect(canRequest(null)).toBe(false)
  })
})

describe('canApproveAmount', () => {
  it('officer within limit', () => {
    expect(canApproveAmount(officer, 4000)).toBe(true)
    expect(canApproveAmount(officer, 5000)).toBe(true)
    expect(canApproveAmount(officer, 5001)).toBe(false)
  })
  it('approver within larger limit', () => {
    expect(canApproveAmount(approver, 50000)).toBe(true)
    expect(canApproveAmount(approver, 50001)).toBe(false)
  })
  it('admin (null limit) is unlimited', () => {
    expect(canApproveAmount(admin, 9_999_999)).toBe(true)
  })
  it('non-approver roles cannot approve', () => {
    expect(canApproveAmount(requester, 1)).toBe(false)
    expect(canApproveAmount(viewer, 1)).toBe(false)
    expect(canApproveAmount(null, 1)).toBe(false)
  })
})

describe('availableActions', () => {
  it('DRAFT for owning requester: submit + cancel, no approve', () => {
    const a = availableActions(requester, 'DRAFT', 100, requester.email)
    expect(a).toMatchObject({ edit: true, submit: true, cancel: true, approve: false, reject: false })
  })
  it('IN_APPROVAL within officer limit: approve + reject', () => {
    const a = availableActions(officer, 'IN_APPROVAL', 1000, requester.email)
    expect(a.approve).toBe(true)
    expect(a.reject).toBe(true)
    expect(a.submit).toBe(false)
  })
  it('IN_APPROVAL over officer limit: reject but no approve', () => {
    const a = availableActions(officer, 'IN_APPROVAL', 50000, requester.email)
    expect(a.approve).toBe(false)
    expect(a.reject).toBe(true)
  })
  it('viewer gets nothing', () => {
    const a = availableActions(viewer, 'IN_APPROVAL', 100, requester.email)
    expect(a).toMatchObject({ edit: false, submit: false, cancel: false, approve: false, reject: false })
  })
  it('APPROVED is terminal: no cancel', () => {
    expect(availableActions(admin, 'APPROVED', 100, admin.email).cancel).toBe(false)
  })
  it("cancel is gated on ownership/admin (matches backend RBAC)", () => {
    // OFFICER viewing someone else's DRAFT: backend cancel would 403, so no button.
    expect(availableActions(officer, 'DRAFT', 100, requester.email).cancel).toBe(false)
    // Owner can cancel their own.
    expect(availableActions(requester, 'DRAFT', 100, requester.email).cancel).toBe(true)
    // ADMIN can cancel anyone's.
    expect(availableActions(admin, 'DRAFT', 100, requester.email).cancel).toBe(true)
  })
})

describe('statusBadge', () => {
  it('maps known statuses', () => {
    expect(statusBadge('IN_APPROVAL')).toBe('in_approval')
    expect(statusBadge('APPROVED')).toBe('approved')
  })
  it('falls back for unknown', () => {
    expect(statusBadge('WAT')).toBe('demo')
  })
})

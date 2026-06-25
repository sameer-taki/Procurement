import { describe, it, expect } from 'vitest'
import {
  canIssuePO, canProcessOutbox, availablePOActions, poStatusBadge,
} from './purchaseOrders.js'

const requester = { role: 'REQUESTER', email: 'req@golden.com.fj' }
const officer = { role: 'OFFICER', email: 'officer@golden.com.fj' }
const approver = { role: 'APPROVER', email: 'approver@golden.com.fj' }
const admin = { role: 'ADMIN', email: 'admin@golden.com.fj' }
const viewer = { role: 'VIEWER', email: 'viewer@golden.com.fj' }

describe('canIssuePO', () => {
  it('lets officer/admin issue', () => {
    expect(canIssuePO(officer)).toBe(true)
    expect(canIssuePO(admin)).toBe(true)
  })
  it('blocks requester/approver/viewer/null', () => {
    expect(canIssuePO(requester)).toBe(false)
    expect(canIssuePO(approver)).toBe(false)
    expect(canIssuePO(viewer)).toBe(false)
    expect(canIssuePO(null)).toBe(false)
  })
})

describe('canProcessOutbox', () => {
  it('admin only', () => {
    expect(canProcessOutbox(admin)).toBe(true)
    expect(canProcessOutbox(officer)).toBe(false)
    expect(canProcessOutbox(null)).toBe(false)
  })
})

describe('availablePOActions', () => {
  it('DRAFT for officer: issue, no outbox', () => {
    const a = availablePOActions(officer, 'DRAFT')
    expect(a).toMatchObject({ issue: true, outbox: false })
  })
  it('issued PO for admin: outbox retry, no issue', () => {
    const a = availablePOActions(admin, 'PO_ISSUED')
    expect(a).toMatchObject({ issue: false, outbox: true })
  })
  it('issued PO for officer: neither (outbox is admin-only)', () => {
    const a = availablePOActions(officer, 'PO_ISSUED')
    expect(a).toMatchObject({ issue: false, outbox: false })
  })
  it('viewer gets nothing on a draft', () => {
    expect(availablePOActions(viewer, 'DRAFT')).toMatchObject({ issue: false, outbox: false })
  })
})

describe('poStatusBadge', () => {
  it('maps known statuses', () => {
    expect(poStatusBadge('DRAFT')).toBe('draft')
    expect(poStatusBadge('PO_ISSUED')).toBe('submitted')
    expect(poStatusBadge('ACKNOWLEDGED')).toBe('approved')
    expect(poStatusBadge('RECEIVED')).toBe('closed')
  })
  it('falls back for unknown', () => {
    expect(poStatusBadge('WAT')).toBe('demo')
  })
})

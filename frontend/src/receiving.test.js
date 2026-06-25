import { describe, it, expect } from 'vitest'
import {
  RECEIVABLE_STATUSES, canReceivePO, canShowReceiveForm, lineReceiptRows,
  buildReceivePayload, hasReceiptLines, validateReceipt, matchBadge,
} from './receiving.js'

const officer = { role: 'OFFICER' }
const admin = { role: 'ADMIN' }
const requester = { role: 'REQUESTER' }
const approver = { role: 'APPROVER' }
const viewer = { role: 'VIEWER' }

describe('canReceivePO', () => {
  it('lets officer/admin receive', () => {
    expect(canReceivePO(officer)).toBe(true)
    expect(canReceivePO(admin)).toBe(true)
  })
  it('blocks requester/approver/viewer/null', () => {
    expect(canReceivePO(requester)).toBe(false)
    expect(canReceivePO(approver)).toBe(false)
    expect(canReceivePO(viewer)).toBe(false)
    expect(canReceivePO(null)).toBe(false)
  })
})

describe('canShowReceiveForm', () => {
  it('shows for officer/admin in a receivable state', () => {
    for (const s of RECEIVABLE_STATUSES) {
      expect(canShowReceiveForm(officer, s)).toBe(true)
    }
  })
  it('hides in non-receivable states even for an officer', () => {
    expect(canShowReceiveForm(officer, 'DRAFT')).toBe(false)
    expect(canShowReceiveForm(officer, 'RECEIVED')).toBe(false)
    expect(canShowReceiveForm(officer, 'MATCHED')).toBe(false)
    expect(canShowReceiveForm(officer, 'CANCELLED')).toBe(false)
  })
  it('hides for a viewer in a receivable state', () => {
    expect(canShowReceiveForm(viewer, 'PO_ISSUED')).toBe(false)
  })
})

describe('lineReceiptRows', () => {
  // The backend PO detail (_detail) keys each line `po_line_id`, not `id`.
  it('keys the row off the backend\'s po_line_id', () => {
    const rows = lineReceiptRows([
      { po_line_id: 'l1', sku: 'A', name: 'Aye', quantity: 10, received_qty: 4 },
    ])
    expect(rows[0].id).toBe('l1')
  })
  it('derives outstanding and fully_received per line', () => {
    const rows = lineReceiptRows([
      { po_line_id: 'l1', sku: 'A', name: 'Aye', quantity: 10, received_qty: 4 },
      { po_line_id: 'l2', sku: 'B', name: 'Bee', quantity: 5, received_qty: 5 },
      { po_line_id: 'l3', sku: 'C', name: 'Cee', quantity: 3 },
    ])
    expect(rows[0]).toMatchObject({ id: 'l1', ordered: 10, received: 4, outstanding: 6, fully_received: false })
    expect(rows[1]).toMatchObject({ id: 'l2', ordered: 5, received: 5, outstanding: 0, fully_received: true })
    expect(rows[2]).toMatchObject({ id: 'l3', ordered: 3, received: 0, outstanding: 3, fully_received: false })
  })
  it('clamps a (shouldn’t-happen) over-receipt to 0 outstanding', () => {
    const [r] = lineReceiptRows([{ po_line_id: 'l1', quantity: 2, received_qty: 5 }])
    expect(r.outstanding).toBe(0)
    expect(r.fully_received).toBe(true)
  })
  it('falls back to id when po_line_id is absent (safety)', () => {
    const [r] = lineReceiptRows([{ id: 'legacy', quantity: 1 }])
    expect(r.id).toBe('legacy')
  })
  it('handles missing lines', () => {
    expect(lineReceiptRows()).toEqual([])
  })
})

describe('buildReceivePayload', () => {
  it('keeps only positive quantities and casts to number', () => {
    const body = buildReceivePayload({ quantities: { l1: '4', l2: '0', l3: '', l4: '2.5' } })
    expect(body.lines).toEqual([
      { po_line_id: 'l1', quantity: 4 },
      { po_line_id: 'l4', quantity: 2.5 },
    ])
  })
  it('trims an optional GRN no + location, omitting blanks', () => {
    expect(buildReceivePayload({ quantities: { l1: '1' }, grnNo: '  GRN-1 ', location: ' MAIN ' }))
      .toEqual({ lines: [{ po_line_id: 'l1', quantity: 1 }], grn_no: 'GRN-1', location: 'MAIN' })
    expect(buildReceivePayload({ quantities: { l1: '1' }, grnNo: '   ', location: '' }))
      .toEqual({ lines: [{ po_line_id: 'l1', quantity: 1 }] })
  })
  it('drops non-numeric quantities', () => {
    expect(buildReceivePayload({ quantities: { l1: 'abc' } }).lines).toEqual([])
  })
})

describe('hasReceiptLines', () => {
  it('true only when there is at least one line', () => {
    expect(hasReceiptLines({ lines: [{ po_line_id: 'l1', quantity: 1 }] })).toBe(true)
    expect(hasReceiptLines({ lines: [] })).toBe(false)
    expect(hasReceiptLines(null)).toBe(false)
    expect(hasReceiptLines({})).toBe(false)
  })
})

describe('validateReceipt', () => {
  const rows = lineReceiptRows([
    { po_line_id: 'l1', sku: 'A', quantity: 10, received_qty: 4 }, // outstanding 6
    { po_line_id: 'l2', sku: 'B', quantity: 5, received_qty: 5 },  // outstanding 0
  ])
  it('rejects an empty receipt', () => {
    expect(validateReceipt({ lines: [] }, rows)).toMatch(/at least one line/i)
  })
  it('rejects an over-receipt', () => {
    const body = buildReceivePayload({ quantities: { l1: '7' } })
    expect(validateReceipt(body, rows)).toMatch(/only 6 outstanding/i)
  })
  it('accepts a receipt within outstanding', () => {
    const body = buildReceivePayload({ quantities: { l1: '6' } })
    expect(validateReceipt(body, rows)).toBeNull()
  })
  it('does not block lines absent from the rows map (backend still validates)', () => {
    const body = buildReceivePayload({ quantities: { lX: '99' } })
    expect(validateReceipt(body, rows)).toBeNull()
  })
})

describe('matchBadge', () => {
  it('maps known match states', () => {
    expect(matchBadge('MATCHED')).toBe('approved')
    expect(matchBadge('PENDING')).toBe('in_approval')
    expect(matchBadge('UNMATCHED')).toBe('draft')
  })
  it('falls back for unknown/empty', () => {
    expect(matchBadge('WAT')).toBe('demo')
    expect(matchBadge(null)).toBe('demo')
  })
})

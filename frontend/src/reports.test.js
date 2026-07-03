import { describe, it, expect } from 'vitest'
import { PREVIEW_ROWS, REPORTS, csvHref, fmtCell } from './reports.js'

describe('REPORTS', () => {
  it('lists the three registers with backend paths', () => {
    expect(REPORTS.map((r) => r.key)).toEqual([
      'purchase-orders', 'receipts', 'spend-by-month',
    ])
    for (const r of REPORTS) expect(r.path).toMatch(/^\/api\/reports\//)
  })
})

describe('csvHref', () => {
  it('appends the CSV format flag', () => {
    expect(csvHref('/api/reports/receipts')).toBe('/api/reports/receipts?format=csv')
  })
})

describe('fmtCell', () => {
  it('renders *_pct ratios as percentages', () => {
    expect(fmtCell('received_pct', 0.4)).toBe('40.0%')
    expect(fmtCell('received_pct', 1)).toBe('100.0%')
  })
  it('localises plain numbers and dashes out nulls', () => {
    expect(fmtCell('ordered_qty', 25000)).toBe('25,000')
    expect(fmtCell('vendor', null)).toBe('—')
    expect(fmtCell('vendor', undefined)).toBe('—')
    expect(fmtCell('status', 'PO_ISSUED')).toBe('PO_ISSUED')
  })
  it('preview row cap is sane', () => {
    expect(PREVIEW_ROWS).toBeGreaterThan(0)
  })
})

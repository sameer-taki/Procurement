import { describe, it, expect } from 'vitest'
import {
  canPushWarehouse, pct, vendorSpendRows, warehouseStatusLabel, pushResultRows,
} from './analytics.js'

const admin = { role: 'ADMIN' }
const officer = { role: 'OFFICER' }
const viewer = { role: 'VIEWER' }

describe('canPushWarehouse', () => {
  it('admin only', () => {
    expect(canPushWarehouse(admin)).toBe(true)
    expect(canPushWarehouse(officer)).toBe(false)
    expect(canPushWarehouse(viewer)).toBe(false)
    expect(canPushWarehouse(null)).toBe(false)
  })
})

describe('pct', () => {
  it('formats a 0..1 rate as a whole percent', () => {
    expect(pct(0.5, 4)).toBe('50%')
    expect(pct(1, 2)).toBe('100%')
    expect(pct(0.666, 3)).toBe('67%')
  })
  it('shows — when there is no sample', () => {
    expect(pct(0.5, 0)).toBe('—')
    expect(pct(null, 3)).toBe('—')
  })
})

describe('vendorSpendRows', () => {
  // The backend (GET /api/analytics) keys each by_vendor row's money as `spend`.
  it('sorts largest-first and computes each share of total', () => {
    const rows = vendorSpendRows(
      [{ vendor: 'PaperCo', spend: 100 }, { vendor: 'WireCo', spend: 300 }],
      400,
    )
    expect(rows.map((r) => r.vendor)).toEqual(['WireCo', 'PaperCo'])
    expect(rows[0]).toMatchObject({ amount: 300, share: 0.75 })
    expect(rows[1]).toMatchObject({ amount: 100, share: 0.25 })
  })
  it('guards a zero/absent total (share 0)', () => {
    const rows = vendorSpendRows([{ vendor: 'X', spend: 0 }], 0)
    expect(rows[0].share).toBe(0)
  })
  it('does not mutate the input array', () => {
    const input = [{ vendor: 'A', spend: 1 }, { vendor: 'B', spend: 2 }]
    vendorSpendRows(input, 3)
    expect(input.map((v) => v.vendor)).toEqual(['A', 'B'])
  })
  it('handles missing/empty input', () => {
    expect(vendorSpendRows()).toEqual([])
  })
})

describe('warehouseStatusLabel', () => {
  it('reads the guarded skip + ok + error states', () => {
    expect(warehouseStatusLabel('skipped:not-configured')).toMatch(/not configured/i)
    expect(warehouseStatusLabel('ok')).toBe('written')
    expect(warehouseStatusLabel('written')).toBe('written')
    expect(warehouseStatusLabel('error:timeout')).toBe('failed — timeout')
    expect(warehouseStatusLabel(null)).toMatch(/not pushed/i)
  })
  it('handles the live writer\'s written:<n> count', () => {
    // gateway.warehouse returns f"written:{len(rows)}" on a real push.
    expect(warehouseStatusLabel('written:3')).toBe('written — 3 rows')
    expect(warehouseStatusLabel('written:0')).toBe('written — 0 rows')
  })
})

describe('pushResultRows', () => {
  it('flattens a {table: status} map into sorted, labelled rows', () => {
    const rows = pushResultRows({
      spend: 'skipped:not-configured',
      on_time_delivery: 'ok',
    })
    expect(rows.map((r) => r.table)).toEqual(['on_time_delivery', 'spend'])
    expect(rows[0]).toMatchObject({ status: 'ok', label: 'written' })
    expect(rows[1].label).toMatch(/not configured/i)
  })
  it('handles an empty/missing result', () => {
    expect(pushResultRows()).toEqual([])
    expect(pushResultRows({})).toEqual([])
  })
})

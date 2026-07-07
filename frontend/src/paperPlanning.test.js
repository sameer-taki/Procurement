import { describe, it, expect } from 'vitest'
import {
  SHIPMENT_STATUSES,
  basisLabel,
  belowCover,
  buildForecastPayload,
  buildShipmentPayload,
  canCreateSuggestion,
  canPlanPaper,
  canRecordShipment,
  coverBadge,
  csvCell,
  fmtTonnes,
  latestAsOf,
  localMonthValue,
  flaggedVariances,
  fmtVariance,
  monthLabel,
  nextArrival,
  orderPageCsv,
  parseForecastPaste,
  shipmentNextStatuses,
  shipmentStatusBadge,
} from './paperPlanning.js'

const officer = { role: 'OFFICER' }
const admin = { role: 'ADMIN' }
const requester = { role: 'REQUESTER' }
const viewer = { role: 'VIEWER' }
const approver = { role: 'APPROVER' }

describe('canPlanPaper', () => {
  it('lets officer/admin drive paper planning', () => {
    expect(canPlanPaper(officer)).toBe(true)
    expect(canPlanPaper(admin)).toBe(true)
  })
  it('blocks requester/viewer/bare approver and null', () => {
    expect(canPlanPaper(requester)).toBe(false)
    expect(canPlanPaper(viewer)).toBe(false)
    expect(canPlanPaper(approver)).toBe(false)
    expect(canPlanPaper(null)).toBe(false)
  })
})

describe('coverBadge', () => {
  it('is danger (red) under half the cover target', () => {
    expect(coverBadge(0, 3)).toBe('rejected')
    expect(coverBadge(1.49, 3)).toBe('rejected')
  })
  it('is warn (amber) under the cover target', () => {
    expect(coverBadge(1.5, 3)).toBe('in_approval')
    expect(coverBadge(2.99, 3)).toBe('in_approval')
  })
  it('is ok (green) at/above the cover target', () => {
    expect(coverBadge(3, 3)).toBe('approved')
    expect(coverBadge(3.59, 3)).toBe('approved')
  })
  it('is muted (grey) when months is null/unknowable', () => {
    expect(coverBadge(null, 3)).toBe('draft')
    expect(coverBadge(undefined, 3)).toBe('draft')
    expect(coverBadge(NaN, 3)).toBe('draft')
  })
  it('respects a different cover target', () => {
    expect(coverBadge(2.9, 6)).toBe('rejected')
    expect(coverBadge(5, 6)).toBe('in_approval')
    expect(coverBadge(6, 6)).toBe('approved')
  })
  it('defaults the cover target to 3 months', () => {
    expect(coverBadge(2.9)).toBe('in_approval')
    expect(coverBadge(3)).toBe('approved')
  })
})

describe('belowCover', () => {
  it('flags months under the target, not unknown (null)', () => {
    expect(belowCover(2.5, 3)).toBe(true)
    expect(belowCover(3, 3)).toBe(false)
    expect(belowCover(3.59, 3)).toBe(false)
    expect(belowCover(null, 3)).toBe(false)
  })
})

describe('fmtTonnes', () => {
  it('renders kilograms as tonnes with one decimal max', () => {
    expect(fmtTonnes(31474)).toBe('31.5 t')
    expect(fmtTonnes(12500)).toBe('12.5 t')
    expect(fmtTonnes(100000)).toBe('100 t')
    expect(fmtTonnes(0)).toBe('0 t')
  })
  it('shows a dash for null/invalid', () => {
    expect(fmtTonnes(null)).toBe('—')
    expect(fmtTonnes(undefined)).toBe('—')
    expect(fmtTonnes('n/a')).toBe('—')
  })
})

describe('monthLabel', () => {
  it("formats 'YYYY-MM' as 'Mon YYYY'", () => {
    expect(monthLabel('2026-07')).toBe('Jul 2026')
    expect(monthLabel('2025-12')).toBe('Dec 2025')
    expect(monthLabel('2026-01')).toBe('Jan 2026')
  })
  it('passes malformed input through, blank as a dash', () => {
    expect(monthLabel('July 2026')).toBe('July 2026')
    expect(monthLabel('2026-13')).toBe('2026-13')
    expect(monthLabel('')).toBe('—')
    expect(monthLabel(null)).toBe('—')
  })
})

describe('shipmentStatusBadge', () => {
  it('maps every shipment status onto an existing badge class', () => {
    expect(shipmentStatusBadge('CONFIRMED')).toBe('submitted')
    expect(shipmentStatusBadge('ON_WATER')).toBe('in_approval')
    expect(shipmentStatusBadge('ARRIVED')).toBe('approved')
    expect(shipmentStatusBadge('RECEIVED')).toBe('closed')
    expect(shipmentStatusBadge('CANCELLED')).toBe('cancelled')
  })
  it('falls back for unknown statuses', () => {
    expect(shipmentStatusBadge('LOST_AT_SEA')).toBe('demo')
    expect(shipmentStatusBadge(undefined)).toBe('demo')
  })
})

describe('shipmentNextStatuses', () => {
  it('advances forward through the lifecycle plus cancel', () => {
    expect(shipmentNextStatuses('CONFIRMED')).toEqual(['ON_WATER', 'ARRIVED', 'RECEIVED', 'CANCELLED'])
    expect(shipmentNextStatuses('ON_WATER')).toEqual(['ARRIVED', 'RECEIVED', 'CANCELLED'])
    expect(shipmentNextStatuses('ARRIVED')).toEqual(['RECEIVED', 'CANCELLED'])
  })
  it('treats RECEIVED and CANCELLED as terminal', () => {
    expect(shipmentNextStatuses('RECEIVED')).toEqual([])
    expect(shipmentNextStatuses('CANCELLED')).toEqual([])
  })
  it('every advance target is a real shipment status', () => {
    for (const s of SHIPMENT_STATUSES) {
      for (const next of shipmentNextStatuses(s)) {
        expect(SHIPMENT_STATUSES).toContain(next)
      }
    }
  })
})

describe('canRecordShipment', () => {
  it('officer/admin on an issued PO', () => {
    expect(canRecordShipment(officer, 'PO_ISSUED')).toBe(true)
    expect(canRecordShipment(admin, 'PARTIALLY_RECEIVED')).toBe(true)
    expect(canRecordShipment(officer, 'ACKNOWLEDGED')).toBe(true)
  })
  it('blocked while the PO is DRAFT/CANCELLED/CLOSED (backend 409s)', () => {
    expect(canRecordShipment(officer, 'DRAFT')).toBe(false)
    expect(canRecordShipment(admin, 'CANCELLED')).toBe(false)
    expect(canRecordShipment(admin, 'CLOSED')).toBe(false)
  })
  it('blocked for non-officers regardless of state', () => {
    expect(canRecordShipment(viewer, 'PO_ISSUED')).toBe(false)
    expect(canRecordShipment(null, 'PO_ISSUED')).toBe(false)
  })
})

describe('buildShipmentPayload', () => {
  it('trims strings and numbers the quantities, dropping blanks', () => {
    expect(buildShipmentPayload({
      vessel: '  Kota Ratu ', etd: '2026-07-05', eta: '', rolls: '18', weight_kg: '', fcl_count: '2', notes: '',
    })).toEqual({ vessel: 'Kota Ratu', etd: '2026-07-05', rolls: 18, fcl_count: 2 })
  })
  it('drops negative/non-numeric quantities but keeps zero', () => {
    expect(buildShipmentPayload({ rolls: '-3', weight_kg: 'heavy', fcl_count: '0' })).toEqual({ fcl_count: 0 })
  })
  it('an empty form posts an empty body (all fields optional)', () => {
    expect(buildShipmentPayload({})).toEqual({})
    expect(buildShipmentPayload()).toEqual({})
  })
})

describe('buildForecastPayload', () => {
  it('shapes a good row and numbers the quantity', () => {
    expect(buildForecastPayload([
      { customer: ' Fiji Water ', sku: ' CTN-FIJIWATER-1L ', period: '2026-07', qty_cartons: '42000' },
    ])).toEqual({
      lines: [{ customer: 'Fiji Water', sku: 'CTN-FIJIWATER-1L', period: '2026-07', qty_cartons: 42000 }],
    })
  })
  it('keeps zero (kills a forecast month) but drops blank/negative/invalid qty', () => {
    const { lines } = buildForecastPayload([
      { customer: 'A', sku: 'S', period: '2026-07', qty_cartons: 0 },
      { customer: 'A', sku: 'S', period: '2026-08', qty_cartons: '' },
      { customer: 'A', sku: 'S', period: '2026-09', qty_cartons: -5 },
      { customer: 'A', sku: 'S', period: '2026-10', qty_cartons: 'lots' },
    ])
    expect(lines).toEqual([{ customer: 'A', sku: 'S', period: '2026-07', qty_cartons: 0 }])
  })
  it('drops rows missing customer/sku or with a malformed period', () => {
    const { lines } = buildForecastPayload([
      { customer: '', sku: 'S', period: '2026-07', qty_cartons: 1 },
      { customer: 'A', sku: '', period: '2026-07', qty_cartons: 1 },
      { customer: 'A', sku: 'S', period: 'July 2026', qty_cartons: 1 },
    ])
    expect(lines).toEqual([])
  })
  it('handles an empty/missing input without throwing', () => {
    expect(buildForecastPayload()).toEqual({ lines: [] })
    expect(buildForecastPayload([])).toEqual({ lines: [] })
  })
})

describe('nextArrival', () => {
  it('returns the earliest ETA across rows, skipping nulls', () => {
    expect(nextArrival([
      { next_eta: '2026-08-02' },
      { next_eta: null },
      { next_eta: '2026-07-15' },
    ])).toBe('2026-07-15')
  })
  it('null when nothing is inbound', () => {
    expect(nextArrival([{ next_eta: null }, {}])).toBe(null)
    expect(nextArrival([])).toBe(null)
    expect(nextArrival()).toBe(null)
  })
})

describe('canCreateSuggestion', () => {
  it('blocks while a coverage requisition is in flight, naming it', () => {
    const g = canCreateSuggestion({
      container_plans: [{ containers: 2 }],
      open_coverage_requisition: { id: 'x', number: 'REQ-1', status: 'IN_APPROVAL' },
    })
    expect(g.enabled).toBe(false)
    expect(g.reason).toContain('REQ-1')
    expect(g.reason).toContain('IN_APPROVAL')
  })
  it('blocks when there is nothing to order (no container plans)', () => {
    const g = canCreateSuggestion({ container_plans: [], open_coverage_requisition: null })
    expect(g.enabled).toBe(false)
    expect(g.reason).toMatch(/nothing to order/i)
  })
  it('enables when plans exist and nothing is in flight — even if below_cover is 0', () => {
    const g = canCreateSuggestion({
      below_cover: 0,
      container_plans: [{ containers: 1 }],
      open_coverage_requisition: null,
    })
    expect(g).toEqual({ enabled: true, reason: null })
  })
  it('is safe on an empty page object', () => {
    expect(canCreateSuggestion({}).enabled).toBe(false)
  })
})

describe('basisLabel', () => {
  it('flags a partially entered forecast window', () => {
    expect(basisLabel({ basis: 'FORECAST', forecast_periods: 1 }, 3)).toBe('FORECAST 1/3')
    expect(basisLabel({ basis: 'FORECAST', forecast_periods: 2 }, 3)).toBe('FORECAST 2/3')
  })
  it('shows the plain basis for full windows, history, and none', () => {
    expect(basisLabel({ basis: 'FORECAST', forecast_periods: 3 }, 3)).toBe('FORECAST')
    expect(basisLabel({ basis: 'HISTORY', forecast_periods: 1 }, 3)).toBe('HISTORY')
    expect(basisLabel({ basis: 'NONE' }, 3)).toBe('NONE')
    expect(basisLabel({}, 3)).toBe('NONE')
  })
})

describe('latestAsOf', () => {
  it('returns the freshest stamp across rows', () => {
    expect(latestAsOf([
      { as_of: '2026-07-02T01:00:00' },
      { as_of: '2026-07-02T03:00:00' },
      { as_of: null },
    ])).toBe('2026-07-02T03:00:00')
  })
  it('is null when no row carries a stamp', () => {
    expect(latestAsOf([])).toBeNull()
    expect(latestAsOf([{ as_of: null }])).toBeNull()
  })
})

describe('localMonthValue', () => {
  it('formats the LOCAL year-month with zero padding', () => {
    expect(localMonthValue(new Date(2026, 0, 15))).toBe('2026-01')
    expect(localMonthValue(new Date(2026, 11, 1))).toBe('2026-12')
  })
})

describe('flaggedVariances', () => {
  it('keeps only the rows needing investigation', () => {
    const recon = {
      rows: [
        { sku: 'RF135-1000', flagged: true },
        { sku: 'CWT140-1400', flagged: false },
        { sku: 'TL125-1600', flagged: true },
      ],
    }
    expect(flaggedVariances(recon).map((r) => r.sku)).toEqual(['RF135-1000', 'TL125-1600'])
  })
  it('is safe on empty/missing input', () => {
    expect(flaggedVariances({})).toEqual([])
    expect(flaggedVariances()).toEqual([])
  })
})

describe('fmtVariance', () => {
  it('signs the KG figure', () => {
    expect(fmtVariance(-760)).toBe('−760 kg')
    expect(fmtVariance(1234.5)).toBe('+1,234.5 kg')
    expect(fmtVariance(0)).toBe('0 kg')
  })
  it('reads null as missing from BC', () => {
    expect(fmtVariance(null)).toBe('not in BC')
    expect(fmtVariance(undefined)).toBe('not in BC')
  })
})

describe('parseForecastPaste', () => {
  it('parses 3-column rows using the default customer', () => {
    const { lines, skipped } = parseForecastPaste(
      'CTN-FIJIWATER-1L\t2026-07\t42000\nCTN-FIJIWATER-500\t2026-08\t500',
      'Fiji Water',
    )
    expect(lines).toEqual([
      { customer: 'Fiji Water', sku: 'CTN-FIJIWATER-1L', period: '2026-07', qty_cartons: 42000 },
      { customer: 'Fiji Water', sku: 'CTN-FIJIWATER-500', period: '2026-08', qty_cartons: 500 },
    ])
    expect(skipped).toBe(0)
  })
  it('parses 4-column rows with an explicit customer', () => {
    const { lines } = parseForecastPaste('Pure Fiji\tCTN-1\t2026-07\t100', 'Someone Else')
    expect(lines).toEqual([
      { customer: 'Pure Fiji', sku: 'CTN-1', period: '2026-07', qty_cartons: 100 },
    ])
  })
  it('falls back to the default customer when a 4-column customer cell is blank', () => {
    const { lines } = parseForecastPaste('\tCTN-1\t2026-07\t100', 'Fiji Water')
    expect(lines).toEqual([
      { customer: 'Fiji Water', sku: 'CTN-1', period: '2026-07', qty_cartons: 100 },
    ])
  })
  it('tab-splits so a comma-bearing customer name survives (4-col)', () => {
    const { lines, skipped } = parseForecastPaste(
      'Visy, Ltd\tCWT140-1400\t2026-07\t1000',
      '',
    )
    expect(lines).toEqual([
      { customer: 'Visy, Ltd', sku: 'CWT140-1400', period: '2026-07', qty_cartons: 1000 },
    ])
    expect(skipped).toBe(0)
  })
  it('strips thousands separators from a tab-split quantity', () => {
    const { lines, skipped } = parseForecastPaste(
      'Fiji Water\tCTN-FIJIWATER-1L\t2026-07\t42,000',
      '',
    )
    expect(lines).toEqual([
      { customer: 'Fiji Water', sku: 'CTN-FIJIWATER-1L', period: '2026-07', qty_cartons: 42000 },
    ])
    expect(skipped).toBe(0)
  })
  it('strips thousands separators from a 3-col tab quantity (default customer)', () => {
    const { lines } = parseForecastPaste('CTN-FIJIWATER-1L\t2026-07\t1,234,567', 'Fiji Water')
    expect(lines).toEqual([
      { customer: 'Fiji Water', sku: 'CTN-FIJIWATER-1L', period: '2026-07', qty_cartons: 1234567 },
    ])
  })
  it('accepts tab, comma and semicolon separators in the same paste', () => {
    const { lines, skipped } = parseForecastPaste(
      'A;CTN-1;2026-07;10\nB,CTN-2,2026-08,20\nC\tCTN-3\t2026-09\t30',
      '',
    )
    expect(lines.map((l) => [l.customer, l.sku, l.qty_cartons])).toEqual([
      ['A', 'CTN-1', 10], ['B', 'CTN-2', 20], ['C', 'CTN-3', 30],
    ])
    expect(skipped).toBe(0)
  })
  it('handles Windows (CRLF) line endings', () => {
    const { lines, skipped } = parseForecastPaste('CTN-1\t2026-07\t1\r\nCTN-2\t2026-08\t2', 'A')
    expect(lines).toHaveLength(2)
    expect(skipped).toBe(0)
  })
  it('normalizes every period variant to YYYY-MM', () => {
    const { lines, skipped } = parseForecastPaste(
      [
        'CTN-1\t2026-07\t1', // YYYY-MM
        'CTN-2\t7/2026\t1', // M/YYYY
        'CTN-3\t07/2026\t1', // MM/YYYY
        'CTN-4\t2026/07\t1', // YYYY/MM
      ].join('\n'),
      'A',
    )
    expect(lines.map((l) => l.period)).toEqual(['2026-07', '2026-07', '2026-07', '2026-07'])
    expect(skipped).toBe(0)
  })
  it('skips header rows — the qty cell is not a finite number', () => {
    const { lines, skipped } = parseForecastPaste(
      'SKU\tMonth\tCartons\nCTN-1\t2026-07\t10',
      'A',
    )
    expect(lines).toEqual([{ customer: 'A', sku: 'CTN-1', period: '2026-07', qty_cartons: 10 }])
    expect(skipped).toBe(1)
  })
  it('ignores blank lines without counting them as skipped', () => {
    const { lines, skipped } = parseForecastPaste('\n\nCTN-1\t2026-07\t10\n   \n', 'A')
    expect(lines).toHaveLength(1)
    expect(skipped).toBe(0)
  })
  it('skips garbage: bad/out-of-range periods, wrong column counts, missing customer', () => {
    const { lines, skipped } = parseForecastPaste(
      [
        'CTN-1\tJuly 2026\t10', // unparseable period
        'CTN-1\t2026-13\t10', // month out of range
        'lonely-cell', // wrong shape (1 column)
        'A\tB\tCTN-1\t2026-07\t10', // wrong shape (5 columns)
        'CTN-2\t2026-07\t10', // 3 columns but no default customer
      ].join('\n'),
      '',
    )
    expect(lines).toEqual([])
    expect(skipped).toBe(5)
  })
  it('skips negative and blank quantities but keeps zero', () => {
    const { lines, skipped } = parseForecastPaste(
      'CTN-1\t2026-07\t-5\nCTN-2\t2026-07\t\nCTN-3\t2026-07\t0',
      'A',
    )
    expect(lines).toEqual([{ customer: 'A', sku: 'CTN-3', period: '2026-07', qty_cartons: 0 }])
    expect(skipped).toBe(2)
  })
  it('trims cells (and the default customer)', () => {
    const { lines } = parseForecastPaste(' CTN-1 , 2026-07 , 10 ', '  Fiji Water  ')
    expect(lines).toEqual([{ customer: 'Fiji Water', sku: 'CTN-1', period: '2026-07', qty_cartons: 10 }])
  })
  it('is safe on empty/missing input', () => {
    expect(parseForecastPaste('', 'A')).toEqual({ lines: [], skipped: 0 })
    expect(parseForecastPaste(null, 'A')).toEqual({ lines: [], skipped: 0 })
    expect(parseForecastPaste(undefined)).toEqual({ lines: [], skipped: 0 })
  })
})

describe('csvCell', () => {
  it('passes plain values through and blanks null/undefined', () => {
    expect(csvCell('RF135')).toBe('RF135')
    expect(csvCell(31474.5)).toBe('31474.5')
    expect(csvCell(0)).toBe('0')
    expect(csvCell(null)).toBe('')
    expect(csvCell(undefined)).toBe('')
  })
  it('double-quotes cells containing commas, quotes or newlines (RFC 4180)', () => {
    expect(csvCell('Visy, Ltd')).toBe('"Visy, Ltd"')
    expect(csvCell('say "hi"')).toBe('"say ""hi"""')
    expect(csvCell('a\nb')).toBe('"a\nb"')
  })
})

describe('orderPageCsv', () => {
  const page = {
    window: ['2026-07', '2026-08', '2026-09'],
    rows: [
      {
        sku: 'RF135-1000', grade: 'RF135', deckle_mm: 1000, basis: 'FORECAST',
        forecast_periods: 3, monthly_usage: 10491.3, usage_3mo: 31474,
        on_hand: 8000, allocated: 500, in_transit: 25000, next_eta: '2026-07-15',
        months_of_stock: 0.7, requirement_kg: 12000, vendor: 'Visy, Ltd',
        lead_time_days: 45, as_of: '2026-07-02T03:00:00',
      },
      {
        sku: 'CWT140-1400', grade: null, deckle_mm: null, basis: 'NONE',
        forecast_periods: 0, monthly_usage: null, usage_3mo: null,
        on_hand: 3000, allocated: 0, in_transit: 0, next_eta: null,
        months_of_stock: null, requirement_kg: 0, vendor: null,
        lead_time_days: null, as_of: null,
      },
    ],
    container_plans: [
      {
        vendor: 'Visy, Ltd', containers: 2, total_kg: 50000,
        lines: [{ sku: 'RF135-1000', requirement_kg: 31474, order_kg: 50000 }],
      },
    ],
  }

  it('lays out the fixed header, one raw row per grade, then the container-plan section', () => {
    const lines = orderPageCsv(page).split('\n')
    expect(lines[0]).toBe(
      'SKU,Grade,Deckle mm,Basis,Forecast periods,Monthly use kg,3-mo need kg,'
      + 'On hand kg,Allocated kg,In transit kg,Next ETA,Months of stock,'
      + 'Suggested order kg,Vendor,Lead time days,Stock as of',
    )
    expect(lines[1]).toBe(
      'RF135-1000,RF135,1000,FORECAST,3,10491.3,31474,8000,500,25000,'
      + '2026-07-15,0.7,12000,"Visy, Ltd",45,2026-07-02T03:00:00',
    )
    expect(lines[3]).toBe('') // blank line before the section
    expect(lines[4]).toBe('Container plans')
    expect(lines[5]).toBe('Vendor,Containers,Total kg')
    expect(lines[6]).toBe('"Visy, Ltd",2,50000')
    expect(lines[7]).toBe(' - RF135-1000,31474,50000')
  })
  it('renders null as an empty cell, keeping zero as 0', () => {
    const lines = orderPageCsv(page).split('\n')
    expect(lines[2]).toBe('CWT140-1400,,,NONE,0,,,3000,0,0,,,0,,,')
  })
  it('escapes cells containing commas or quotes', () => {
    const csv = orderPageCsv({
      rows: [{ ...page.rows[0], vendor: 'Say "cheese", twice' }],
      container_plans: [],
    })
    expect(csv).toContain('"Say ""cheese"", twice"')
  })
  it('omits the container-plan section when nothing needs ordering', () => {
    const csv = orderPageCsv({ rows: page.rows, container_plans: [] })
    expect(csv).not.toContain('Container plans')
    expect(csv.trimEnd().split('\n')).toHaveLength(3) // header + 2 rows only
  })
  it('an unpriced plan (vendor null) still lists its lines', () => {
    const lines = orderPageCsv({
      rows: [],
      container_plans: [{
        vendor: null, containers: 1, total_kg: 25000,
        lines: [{ sku: 'TL125-1600', requirement_kg: 20000, order_kg: 25000 }],
      }],
    }).split('\n')
    expect(lines[4]).toBe(',1,25000')
    expect(lines[5]).toBe(' - TL125-1600,20000,25000')
  })
  it('is safe on an empty page — just the header', () => {
    expect(orderPageCsv({}).trimEnd().split('\n')).toHaveLength(1)
  })
})

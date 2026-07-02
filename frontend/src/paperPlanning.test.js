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
  fmtTonnes,
  latestAsOf,
  localMonthValue,
  flaggedVariances,
  fmtVariance,
  monthLabel,
  nextArrival,
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

import { describe, it, expect } from 'vitest'
import {
  ROLE_CODES,
  canAdmin,
  fmtLimit,
  gradePreviewVerdict,
  integrationBadge,
  isLastActiveAdmin,
  outboxBadge,
  parseLimit,
  purgeSummaryLines,
} from './admin.js'

describe('canAdmin', () => {
  it('only an ADMIN sees the admin screen', () => {
    expect(canAdmin({ role: 'ADMIN' })).toBe(true)
    expect(canAdmin({ role: 'OFFICER' })).toBe(false)
    expect(canAdmin(null)).toBe(false)
  })
})

describe('fmtLimit', () => {
  it('null is unlimited (the ADMIN seed) and numbers localise', () => {
    expect(fmtLimit(null)).toBe('unlimited')
    expect(fmtLimit(undefined)).toBe('unlimited')
    expect(fmtLimit(5000)).toBe('5,000')
  })
})

describe('parseLimit', () => {
  it('blank means unlimited (null), numbers pass through', () => {
    expect(parseLimit('')).toBeNull()
    expect(parseLimit('  ')).toBeNull()
    expect(parseLimit('7500')).toBe(7500)
    expect(parseLimit(0)).toBe(0)
  })
  it('invalid input is undefined so the caller blocks the save', () => {
    expect(parseLimit('abc')).toBeUndefined()
    expect(parseLimit('-1')).toBeUndefined()
  })
})

describe('badges', () => {
  it('maps integration modes and outbox statuses to the shared palette', () => {
    expect(integrationBadge('live')).toBe('live')
    expect(integrationBadge('demo')).toBe('demo')
    expect(integrationBadge('off')).toBe('draft')
    expect(outboxBadge('FAILED')).toBe('rejected')
    expect(outboxBadge('SENT')).toBe('approved')
    expect(outboxBadge('???')).toBe('draft')
  })
})

describe('isLastActiveAdmin', () => {
  const admin = { id: '1', role: 'ADMIN', active: true }
  it('true when no other active admin exists', () => {
    expect(isLastActiveAdmin(admin, [admin, { id: '2', role: 'OFFICER', active: true }])).toBe(true)
    expect(isLastActiveAdmin(admin, [admin, { id: '3', role: 'ADMIN', active: false }])).toBe(true)
  })
  it('false when another active admin exists, or for non-admins', () => {
    expect(isLastActiveAdmin(admin, [admin, { id: '2', role: 'ADMIN', active: true }])).toBe(false)
    expect(isLastActiveAdmin({ id: '9', role: 'VIEWER', active: true }, [admin])).toBe(false)
  })
  it('covers all role codes in the picker', () => {
    expect(ROLE_CODES).toContain('ADMIN')
    expect(ROLE_CODES).toHaveLength(5)
  })
})

describe('gradePreviewVerdict', () => {
  it('flags the no-capture-group trap loudly', () => {
    const v = gradePreviewVerdict({
      total_items: 13000, match_count: 6, ungraded_matches: 6, distinct_grades: 0,
    })
    expect(v).toMatch(/NONE would gain a grade/)
    expect(v).toMatch(/capture group/)
  })
  it('summarises a healthy preview, noting partial captures', () => {
    const v = gradePreviewVerdict({
      total_items: 13000, match_count: 40, ungraded_matches: 4, distinct_grades: 12,
    })
    expect(v).toMatch(/36 of 13000 items would gain a grade \(12 grades\)/)
    expect(v).toMatch(/4 match without capturing/)
  })
  it('handles zero matches and empty input', () => {
    expect(gradePreviewVerdict({ total_items: 10, match_count: 0 })).toMatch(/No SKUs match/)
    expect(gradePreviewVerdict(null)).toBe('')
  })
})

describe('purgeSummaryLines', () => {
  it('one removed-counts line, plus kept lines only when non-empty', () => {
    const lines = purgeSummaryLines({
      items: 18, vendors: 4, customers: 4, vendor_prices: 20, boms: 3,
      forecasts: 3, usage_rows: 36, skipped_items: ['BOARD-200K'], skipped_vendors: [],
    })
    expect(lines).toHaveLength(2)
    expect(lines[0]).toMatch(/Removed 18 items, 4 vendors, 4 customers/)
    expect(lines[1]).toMatch(/Kept \(referenced by orders\): BOARD-200K/)
  })
  it('empty input -> no lines; missing counts default to 0', () => {
    expect(purgeSummaryLines(null)).toEqual([])
    expect(purgeSummaryLines({})[0]).toMatch(/Removed 0 items/)
  })
})

import { describe, it, expect } from 'vitest'
import {
  ROLE_CODES,
  canAdmin,
  fmtLimit,
  integrationBadge,
  isLastActiveAdmin,
  outboxBadge,
  parseLimit,
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

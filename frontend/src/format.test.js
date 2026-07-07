import { describe, it, expect } from 'vitest'
import { relativeTime, asUtc, num, money } from './format.js'

describe('relativeTime', () => {
  const now = new Date('2026-06-25T12:00:00Z').getTime()
  it('handles empty', () => expect(relativeTime(null, now)).toBe('never'))
  it('just now', () => expect(relativeTime('2026-06-25T11:59:40Z', now)).toBe('just now'))
  it('minutes', () => expect(relativeTime('2026-06-25T11:30:00Z', now)).toBe('30 min ago'))
  it('hours', () => expect(relativeTime('2026-06-25T09:00:00Z', now)).toBe('3 h ago'))
  it('days', () => expect(relativeTime('2026-06-23T12:00:00Z', now)).toBe('2 d ago'))
  it('treats a suffix-less backend timestamp as UTC, not local', () => {
    // The backend stamps datetime.utcnow().isoformat() with no zone. Both forms
    // must read the same age regardless of the runtime's timezone.
    expect(relativeTime('2026-06-25T11:30:00', now)).toBe('30 min ago')
    expect(relativeTime('2026-06-25T11:30:00', now))
      .toBe(relativeTime('2026-06-25T11:30:00Z', now))
  })
})

describe('asUtc', () => {
  it('appends Z to a bare date-time', () => {
    expect(asUtc('2026-07-03T10:00:00')).toBe('2026-07-03T10:00:00Z')
    expect(asUtc('2026-07-03T10:00:00.123456')).toBe('2026-07-03T10:00:00.123456Z')
  })
  it('leaves an already-zoned timestamp alone', () => {
    expect(asUtc('2026-07-03T10:00:00Z')).toBe('2026-07-03T10:00:00Z')
    expect(asUtc('2026-07-03T10:00:00+12:00')).toBe('2026-07-03T10:00:00+12:00')
  })
  it('leaves a plain date (no T) alone and passes non-strings through', () => {
    expect(asUtc('2026-07-03')).toBe('2026-07-03')
    expect(asUtc(null)).toBe(null)
  })
})

describe('formatters', () => {
  it('num formats and handles null', () => {
    expect(num(null)).toBe('—')
    expect(num(15550)).toBe('15,550')
  })
  it('money', () => expect(money(1.95)).toBe('FJD 1.95'))
})

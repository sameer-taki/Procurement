import { describe, it, expect } from 'vitest'
import { relativeTime, num, money } from './format.js'

describe('relativeTime', () => {
  const now = new Date('2026-06-25T12:00:00Z').getTime()
  it('handles empty', () => expect(relativeTime(null, now)).toBe('never'))
  it('just now', () => expect(relativeTime('2026-06-25T11:59:40Z', now)).toBe('just now'))
  it('minutes', () => expect(relativeTime('2026-06-25T11:30:00Z', now)).toBe('30 min ago'))
  it('hours', () => expect(relativeTime('2026-06-25T09:00:00Z', now)).toBe('3 h ago'))
  it('days', () => expect(relativeTime('2026-06-23T12:00:00Z', now)).toBe('2 d ago'))
})

describe('formatters', () => {
  it('num formats and handles null', () => {
    expect(num(null)).toBe('—')
    expect(num(15550)).toBe('15,550')
  })
  it('money', () => expect(money(1.95)).toBe('FJD 1.95'))
})

import { describe, expect, it } from 'vitest'
import { calculateDecorrelatedJitterDelay } from './jitter'

describe('calculateDecorrelatedJitterDelay', () => {
  it('respects base delay and exponential growth ceiling with deterministic random', () => {
    const fixedRandom = () => 1.0
    // Attempt 0: base 25, random=1.0 -> 25
    expect(calculateDecorrelatedJitterDelay(25, 10_000, 0, 0, fixedRandom)).toBe(25)
    // Attempt 1: exp ceiling 50 -> 50
    expect(calculateDecorrelatedJitterDelay(25, 10_000, 1, 25, fixedRandom)).toBe(50)
    // Attempt 2: exp ceiling 100 -> 100
    expect(calculateDecorrelatedJitterDelay(25, 10_000, 2, 50, fixedRandom)).toBe(100)
    // Capped at max delay
    expect(calculateDecorrelatedJitterDelay(8_000, 10_000, 1, 8_000, fixedRandom)).toBe(10_000)
  })

  it('produces jittered values within the safe dynamic interval', () => {
    const minRandom = () => 0.0
    expect(calculateDecorrelatedJitterDelay(100, 10_000, 0, 100, minRandom)).toBe(1)

    const midRandom = () => 0.5
    const val = calculateDecorrelatedJitterDelay(100, 10_000, 2, 200, midRandom)
    expect(val).toBeGreaterThan(0)
    expect(val).toBeLessThanOrEqual(10_000)
  })
})

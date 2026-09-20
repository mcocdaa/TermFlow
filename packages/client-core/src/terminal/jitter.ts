/**
 * Decorrelated Full Jitter Backoff Algorithm.
 *
 * Implements exponential growth with decorrelation and full random jitter
 * to eliminate thundering herd storms on reconnect under poor network conditions.
 *
 * References:
 * AWS Architecture Blog: "Exponential Backoff And Jitter"
 */
export function calculateDecorrelatedJitterDelay(
  baseDelayMs: number,
  maxDelayMs: number,
  attempt: number,
  prevDelayMs: number = baseDelayMs,
  random: () => number = Math.random,
): number {
  const safeBase = Math.max(1, baseDelayMs)
  const safeMax = Math.max(safeBase, maxDelayMs)
  // Exponential ceiling: min(max, base * 2^attempt)
  const expCeiling = Math.min(safeMax, safeBase * 2 ** attempt)
  // Dynamic upper bound incorporating decorrelated previous delay, capped by exponential ceiling
  const high = prevDelayMs > 0 ? Math.min(expCeiling, Math.max(safeBase, prevDelayMs * 3)) : expCeiling
  const r = typeof random === 'function' ? random() : Math.random()
  // Uniform jitter in [1, high]
  return Math.max(1, Math.floor(r * high))
}

export type SummaryScalar = string | number | boolean

export interface SummaryGridEntry {
  key: string
  value: SummaryScalar
}

export interface MatchByMethodEntry {
  key: string
  value: number
}

const MATCH_BY_METHOD_KEYS = [
  'source_key',
  'external_id',
  'legacy_row',
  'fingerprint',
  'ambiguous',
  'none'
] as const

function isSummaryScalar(value: unknown): value is SummaryScalar {
  return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'
}

export function summaryGridEntries(summary: object): SummaryGridEntry[] {
  return Object.entries(summary)
    .filter((entry): entry is [string, SummaryScalar] => isSummaryScalar(entry[1]))
    .map(([key, value]) => ({ key, value }))
}

export function matchByMethodEntries(value: unknown): MatchByMethodEntry[] {
  if (!value || Array.isArray(value) || typeof value !== 'object') return []
  const methods = value as Record<string, unknown>
  return MATCH_BY_METHOD_KEYS.flatMap(key => (
    typeof methods[key] === 'number' ? [{ key, value: methods[key] }] : []
  ))
}

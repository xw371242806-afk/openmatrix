import { useCallback, useEffect, useState } from 'react'

export type RouteKey =
  | 'overview'
  | 'projects'
  | 'analysis'
  | 'results'
  | 'agent'
  | 'trace'
  | 'quality'
  | 'evidence'
  | 'settings'
  | 'matrix'

const ROUTES: readonly RouteKey[] = [
  'overview', 'projects', 'analysis', 'results',
  'agent', 'trace', 'quality', 'evidence', 'settings', 'matrix'
]

/** Pre-redesign page keys, kept so old links and pages keep working. */
const LEGACY_ALIASES: Record<string, RouteKey> = {
  home: 'overview',
  batch: 'analysis',
  runs: 'results',
  chat: 'agent',
  charts: 'quality',
  pdf: 'evidence',
}

export function normalizeRoute(hash: string): RouteKey {
  const raw = hash.replace(/^#\/?/, '').split('?')[0]
  if ((ROUTES as readonly string[]).includes(raw)) return raw as RouteKey
  return LEGACY_ALIASES[raw] ?? 'overview'
}

export function hashSearchParams(hash: string): URLSearchParams {
  const queryAt = hash.indexOf('?')
  return new URLSearchParams(queryAt < 0 ? '' : hash.slice(queryAt + 1))
}

/**
 * Minimal hash router: no dependency, browser back/forward works,
 * every view is deep-linkable (#/projects, #/results, ...).
 */
export function useHashRoute(): [RouteKey, (next: RouteKey) => void] {
  const [route, setRoute] = useState<RouteKey>(() => normalizeRoute(window.location.hash))
  useEffect(() => {
    const onHashChange = () => setRoute(normalizeRoute(window.location.hash))
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])
  const navigate = useCallback((next: RouteKey) => {
    window.location.hash = `/${next}`
  }, [])
  return [route, navigate]
}

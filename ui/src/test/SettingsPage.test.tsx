import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import i18n from '../i18n'
// The p5 patch locale is not merged into the main locale files yet; register
// it here so assertions can use the user-facing copy.
import p5en from '../locales/patches/p5.en.json'
import SettingsPage from '../SettingsPage'

i18n.addResourceBundle('en', 'translation', p5en, true, true)

function mockFetch(handlers: Record<string, (url: string, init?: RequestInit) => Promise<unknown>>) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const entries = Object.entries(handlers).sort((a, b) => b[0].length - a[0].length)
    for (const [pattern, handler] of entries) {
      const exact = pattern.startsWith('=')
      if (exact ? url === pattern.slice(1) : url.includes(pattern)) {
        return new Response(JSON.stringify(await handler(url, init)), { status: 200 })
      }
    }
    return new Response(JSON.stringify({ error: 'not found' }), { status: 404 })
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

describe('SettingsPage', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
    sessionStorage.clear()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('does not persist the master key to localStorage/sessionStorage', async () => {
    mockFetch({
      '=/api/v1/api-keys': async () => ({ api_keys: [] }),
    })
    render(<SettingsPage />)
    fireEvent.change(screen.getByPlaceholderText('Master key'), { target: { value: 'sekret-123' } })
    fireEvent.click(screen.getByText('Connect'))
    await waitFor(() => expect(localStorage.length).toBe(0))
    expect(sessionStorage.length).toBe(0)
    expect(localStorage.getItem('masterKey')).toBeNull()
  })

  it('shows an error toast on 401', async () => {
    const fn = vi.fn(async () => {
      return new Response(JSON.stringify({ detail: 'master key required' }), { status: 401 })
    })
    vi.stubGlobal('fetch', fn)
    render(<SettingsPage />)
    fireEvent.change(screen.getByPlaceholderText('Master key'), { target: { value: 'bad' } })
    fireEvent.click(screen.getByText('Connect'))
    await waitFor(() => expect(screen.getByText(/Unauthorized - check master key/)).toBeInTheDocument(), { timeout: 8000 })
  })

  it('disables Revoke for already-revoked keys', async () => {
    mockFetch({
      '=/api/v1/api-keys': async () => ({
        api_keys: [
          { id: 'k-1', prefix: 'om-', name: 'revoked-key', created_at: '2026', revoked: true },
          { id: 'k-2', prefix: 'om-', name: 'active-key', created_at: '2026', revoked: false },
        ],
      }),
    })
    render(<SettingsPage />)
    fireEvent.change(screen.getByPlaceholderText('Master key'), { target: { value: 'k' } })
    fireEvent.click(screen.getByText('Connect'))
    await waitFor(() => expect(screen.getByText('active-key')).toBeInTheDocument())
    const revokeButtons = screen.getAllByRole('button', { name: 'Revoke' })
    // active key has an enabled Revoke; revoked key's is disabled
    const disabled = revokeButtons.filter((b) => (b as HTMLButtonElement).disabled)
    expect(disabled.length).toBe(1)
    expect(revokeButtons.length).toBe(2)
  })

  it('a stale key-list response does not overwrite after a master-key change', async () => {
    let resolveA: (v: unknown) => void
    const gateA = new Promise((resolve) => { resolveA = resolve })
    let firstCall = true
    mockFetch({
      '=/api/v1/api-keys': async () => {
        if (firstCall) {
          firstCall = false
          return gateA.then(() => ({
            api_keys: [{ id: 'k-a', prefix: 'a', name: 'stale-key', created_at: '', revoked: false }],
          }))
        }
        // second (current) request resolves immediately with fresh (empty) data
        return { api_keys: [] }
      },
    })
    render(<SettingsPage />)
    fireEvent.change(screen.getByPlaceholderText('Master key'), { target: { value: 'old' } })
    fireEvent.click(screen.getByText('Connect'))
    // switch master key while first request is still pending
    fireEvent.change(screen.getByPlaceholderText('Master key'), { target: { value: 'new' } })
    fireEvent.click(screen.getByText('Connect'))
    await waitFor(() => expect(screen.queryByText('stale-key')).toBeNull(), { timeout: 3000 })
    resolveA!(null)
    await new Promise((r) => setTimeout(r, 50))
    // the stale response must not populate the table with stale-key
    expect(screen.queryByText('stale-key')).toBeNull()
  })

  it('renders read-only model routing and system health with honest wording', async () => {
    mockFetch({
      '=/api/v1/api-keys': async () => ({ api_keys: [] }),
      '=/api/v1/model-router': async () => ({
        enabled: true,
        default_model: 'route-default',
        tasks: { batch: { env: 'OPENMATRIX_BATCH_MODEL', model: 'provider-model-x' } },
      }),
      '=/api/v1/health/detailed': async () => ({
        status: 'ok',
        service: 'openmatrix',
        components: { indexer: 'unavailable', database: 'ok' },
      }),
    })
    render(<SettingsPage />)

    await waitFor(() => expect(screen.getByText('route-default')).toBeInTheDocument())
    // default_model is presented as a config value with an honest caveat
    expect(screen.getByText(/routing placeholder/)).toBeInTheDocument()
    expect(screen.getByText('Routing enabled')).toBeInTheDocument()
    expect(screen.getByText('OPENMATRIX_BATCH_MODEL')).toBeInTheDocument()
    expect(screen.getByText('provider-model-x')).toBeInTheDocument()

    // component status rows: mono name + icon/text tag, sorted
    expect(screen.getByText('database')).toBeInTheDocument()
    expect(screen.getByText('unavailable')).toBeInTheDocument()
    expect(screen.getByText('openmatrix')).toBeInTheDocument()
  })
})

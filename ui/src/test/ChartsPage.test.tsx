import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import i18n from '../i18n'
// The p5 patch locale is not merged into the main locale files yet; register
// it here so assertions can use the user-facing copy.
import p5en from '../locales/patches/p5.en.json'
import ChartsPage from '../ChartsPage'

i18n.addResourceBundle('en', 'translation', p5en, true, true)

// jsdom has no canvas; mock echarts-for-react core to a lightweight div
vi.mock('echarts-for-react/lib/core', () => ({
  default: () => <div data-testid="echarts-mock" />,
}))

function mockFetch(handlers: Record<string, () => Promise<unknown>>) {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const entries = Object.entries(handlers).sort((a, b) => b[0].length - a[0].length)
    for (const [pattern, handler] of entries) {
      if (url.includes(pattern)) {
        return new Response(JSON.stringify(await handler()), { status: 200 })
      }
    }
    return new Response(JSON.stringify({ error: 'not found' }), { status: 404 })
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

describe('ChartsPage', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders statistic cards and chart containers from all six endpoints', async () => {
    mockFetch({
      '/status-counts': async () => ({ completed: 5, failed: 1 }),
      '/citation-status-counts': async () => ({ verified_exact: 3, needs_review: 1, not_found: 1 }),
      '/average-answer-length': async () => ({ completed: 42, needs_review: 30 }),
      '/total-cells': async () => ({ total: 9 }),
      '/most-failed-questions': async () => ([{ question: 'q1', fail_count: 2 }]),
      '/citation-verification-rate': async () => ({ rate: 0.75 }),
    })
    render(<ChartsPage />)
    // wait for the data-dependent stat figures, not just the static titles
    await waitFor(() => expect(screen.getByText('9')).toBeInTheDocument())
    expect(screen.getByText('75%')).toBeInTheDocument()
    expect(screen.getAllByText('Total Cells').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Quote Found Rate').length).toBeGreaterThan(0)
    // four charts rendered (echarts mock)
    expect(screen.getAllByTestId('echarts-mock').length).toBe(4)
    expect(screen.getByText('Refresh')).toBeInTheDocument()
  })

  it('accepts the alternate total_cells / verification_rate field names', async () => {
    mockFetch({
      '/status-counts': async () => ({ completed: 1 }),
      '/citation-status-counts': async () => ({ verified_exact: 1 }),
      '/average-answer-length': async () => ({ completed: 10 }),
      '/total-cells': async () => ({ total_cells: 7 }),
      '/most-failed-questions': async () => ([]),
      '/citation-verification-rate': async () => ({ verification_rate: 0.5 }),
    })
    render(<ChartsPage />)
    await waitFor(() => expect(screen.getByText('7')).toBeInTheDocument())
    expect(screen.getByText('50%')).toBeInTheDocument()
  })

  it('shows Empty when datasets are empty', async () => {
    mockFetch({
      '/status-counts': async () => ({}),
      '/citation-status-counts': async () => ({}),
      '/average-answer-length': async () => ({}),
      '/total-cells': async () => ({ total: 0 }),
      '/most-failed-questions': async () => ([]),
      '/citation-verification-rate': async () => ({ rate: 0 }),
    })
    render(<ChartsPage />)
    await waitFor(() => expect(screen.getAllByText('No data').length).toBeGreaterThan(0))
    expect(screen.getAllByText('Total Cells').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Quote Found Rate').length).toBeGreaterThan(0)
    // zero is a real value: the stat cards still show it, never an empty state
    expect(screen.getByText('0%')).toBeInTheDocument()
    expect(screen.getByText('0')).toBeInTheDocument()
  })

  it('surfaces an error when an endpoint fails', async () => {
    const fn = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('total-cells')) {
        return new Response(JSON.stringify({}), { status: 500 })
      }
      return new Response(JSON.stringify(url.includes('status-counts') ? {} :
        url.includes('citation-status') ? {} :
        url.includes('average') ? {} :
        url.includes('most-failed') ? [] : { rate: 0 }), { status: 200 })
    })
    vi.stubGlobal('fetch', fn)
    render(<ChartsPage />)
    await waitFor(() => expect(screen.getByText(/Request failed with status 500/)).toBeInTheDocument())
  })
})

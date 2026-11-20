import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import HomePage from '../HomePage'

function mockFetch(handlers: Record<string, () => Promise<unknown>>) {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    // exact '=url' patterns match equality; otherwise longest-substring wins
    const entries = Object.entries(handlers).sort((a, b) => b[0].length - a[0].length)
    for (const [pattern, handler] of entries) {
      if (pattern.startsWith('=') ? url === pattern.slice(1) : url.includes(pattern)) {
        return new Response(JSON.stringify(await handler()), { status: 200 })
      }
    }
    return new Response(JSON.stringify({ error: 'not found' }), { status: 404 })
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

describe('HomePage (Overview)', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders real stat values from the backend endpoints', async () => {
    mockFetch({
      '=/api/v1/stats/total-cells': async () => ({ total: 42 }),
      '=/api/v1/stats/citation-verification-rate': async () => ({ rate: 0.5 }),
      '=/api/v1/stats/answer-support-rate': async () => ({ rate: 0.25 }),
      '=/api/v1/projects': async () => ({ projects: [
        { id: 'p1', name: 'P1', description: '', created_at: '2026-01-15T10:00:00Z' },
        { id: 'p2', name: 'P2', description: '', created_at: '2026-02-20T10:00:00Z' },
      ] }),
      '=/api/v1/schedules': async () => ({ schedules: [
        { schedule_id: 's1', cron: '* * * * *', objective: 'obj', research_id: 'r1', paused: false },
      ] }),
      '=/api/v1/runs?limit=20': async () => ({ runs: [] }),
    })

    render(<HomePage navigate={vi.fn()} />)

    await waitFor(() => expect(screen.getByText('42')).toBeInTheDocument())
    expect(screen.getByText('50%')).toBeInTheDocument()
    expect(screen.getByText('25%')).toBeInTheDocument()
    expect(screen.getByText('2')).toBeInTheDocument()
    expect(screen.getByText('1')).toBeInTheDocument()

    // the runs digest is fetched as a recent-runs list (bounded, not a total)
    await waitFor(() => {
      const urls = (fetch as ReturnType<typeof vi.fn>).mock.calls.map((call) => String(call[0]))
      expect(urls).toContain('/api/v1/runs?limit=20')
    })
  })

  it('shows "-" with a hint when a stat endpoint fails, never a fabricated 0', async () => {
    mockFetch({
      '=/api/v1/stats/total-cells': async () => { throw new Error('boom') },
      '=/api/v1/stats/citation-verification-rate': async () => { throw new Error('boom') },
      '=/api/v1/projects': async () => { throw new Error('boom') },
      '=/api/v1/schedules': async () => { throw new Error('boom') },
      '=/api/v1/runs?limit=20': async () => ({ runs: [] }),
    })

    render(<HomePage navigate={vi.fn()} />)

    await waitFor(() => expect(screen.getAllByText('-')).toHaveLength(5))
    expect(screen.queryByText('0')).not.toBeInTheDocument()
    expect(screen.queryByText('0%')).not.toBeInTheDocument()
  })

  it('still renders a genuine zero as 0 while other tiles fail independently', async () => {
    mockFetch({
      '=/api/v1/stats/total-cells': async () => { throw new Error('boom') },
      '=/api/v1/stats/citation-verification-rate': async () => ({ rate: 0 }),
      '=/api/v1/projects': async () => ({ projects: [
        { id: 'p1', name: 'P1', description: '', created_at: '2026-01-15T10:00:00Z' },
      ] }),
      '=/api/v1/schedules': async () => ({ schedules: [] }),
      '=/api/v1/runs?limit=20': async () => ({ runs: [] }),
    })

    render(<HomePage navigate={vi.fn()} />)

    await waitFor(() => expect(screen.getAllByText('0')).toHaveLength(1))
    expect(screen.getByText('0%')).toBeInTheDocument()
    expect(screen.getByText('1')).toBeInTheDocument()
    expect(screen.getAllByText('-')).toHaveLength(2)
  })
})

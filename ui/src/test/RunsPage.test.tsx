import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import i18n from '../i18n'
import RunsPage from '../RunsPage'

const { gridProps } = vi.hoisted(() => ({ gridProps: vi.fn() }))

vi.mock('ag-grid-react', () => ({
  AgGridReact: (props: unknown) => {
    gridProps(props)
    return <div data-testid="ag-grid-stub" />
  },
}))

function makeCells(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    run_id: 'run-a',
    cell_id: `c-${i}`,
    document_index: i % 2,
    document_name: i % 2 === 0 ? 'alpha.pdf' : 'beta.pdf',
    question_index: i,
    question: `Question ${i}`,
    status: i === 3 ? 'needs_review' : 'completed',
    answer: `Answer ${i}`,
    quote: `Quote ${i}`,
    citation_status: i === 3 ? 'not_found' : 'verified_exact',
    page: i + 1,
    error: null,
  }))
}

function stubRunsApi() {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/v1/runs?limit=20') {
      return new Response(JSON.stringify({ runs: ['run-a', 'run-b'] }), { status: 200 })
    }
    if (url === '/api/v1/runs/run-a') {
      return new Response(JSON.stringify({ run_id: 'run-a', total: 10, completed: 10, failed: 0, needs_review: 1 }), { status: 200 })
    }
    if (url === '/api/v1/runs/run-b') {
      return new Response(JSON.stringify({ run_id: 'run-b', total: 4, completed: 2, failed: 1, needs_review: 0 }), { status: 200 })
    }
    if (url === '/api/matrix-runs/run-a') {
      return new Response(JSON.stringify({ run_id: 'run-a', cells: makeCells(10) }), { status: 200 })
    }
    return new Response(JSON.stringify({ error: 'not found' }), { status: 404 })
  })
}

describe('RunsPage', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    gridProps.mockClear()
  })

  afterEach(async () => {
    cleanup()
    await i18n.changeLanguage('en')
  })

  it('lists recent runs with honest counts and opens the AG Grid drawer', async () => {
    const fetchMock = stubRunsApi()
    vi.stubGlobal('fetch', fetchMock)
    render(<RunsPage />)

    expect(await screen.findByText('run-a')).toBeInTheDocument()
    expect(screen.getByText('run-b')).toBeInTheDocument()
    expect(screen.getByText(/most recent runs/)).toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('Open run run-a'))
    await waitFor(() => expect(screen.getByTestId('ag-grid-stub')).toBeInTheDocument())
    const last = gridProps.mock.calls[gridProps.mock.calls.length - 1][0] as {
      rowData: unknown[]
      columnDefs: { headerName?: string }[]
    }
    expect(last.rowData).toHaveLength(10)
    expect(last.columnDefs.map((c) => c.headerName)).toContain('Document')
    expect(last.columnDefs.map((c) => c.headerName)).toContain('Supports answer')
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/matrix-runs/run-a',
      expect.objectContaining({ headers: expect.any(Object) }),
    )

    // grid headers translate at render time (per-language), not module scope
    await i18n.changeLanguage('zh')
    await waitFor(() => {
      const latest = gridProps.mock.calls[gridProps.mock.calls.length - 1][0] as {
        columnDefs: { headerName?: string }[]
      }
      expect(latest.columnDefs.map((c) => c.headerName)).toContain('文档')
    })
    await i18n.changeLanguage('en')
  })

  it('filters runs by derived status client-side', async () => {
    vi.stubGlobal('fetch', stubRunsApi())
    render(<RunsPage />)

    expect(await screen.findByText('run-a')).toBeInTheDocument()
    expect(screen.getByText('run-b')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('radio', { name: /^Needs review/ }))
    expect(screen.queryByText('run-b')).not.toBeInTheDocument()
    expect(screen.getByText('run-a')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('radio', { name: /^Failed/ }))
    expect(screen.queryByText('run-a')).not.toBeInTheDocument()
    expect(screen.getByText('run-b')).toBeInTheDocument()
  })

  it('shows an error state with retry when the run list cannot load', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ error: 'boom' }), { status: 500 })))
    render(<RunsPage />)

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByText(/Failed to load runs/)).toBeInTheDocument()

    vi.unstubAllGlobals()
    vi.stubGlobal('fetch', stubRunsApi())
    fireEvent.click(screen.getByRole('button', { name: /Retry/ }))
    expect(await screen.findByText('run-a')).toBeInTheDocument()
  })
})

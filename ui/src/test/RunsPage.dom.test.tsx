import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi, afterEach } from 'vitest'
import RunsPage from '../RunsPage'

describe('RunsPage real grid DOM', () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })
  it('renders five rows and keeps page/evidence visible', async () => {
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(640)
    vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(1152)
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(640)
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(1152)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/runs?limit=20') return new Response(JSON.stringify({ runs: ['r'] }))
      if (url === '/api/v1/runs/r') return new Response(JSON.stringify({ run_id: 'r', total: 5, completed: 5, failed: 0, needs_review: 0 }))
      if (url === '/api/matrix-runs/r') return new Response(JSON.stringify({ cells: Array.from({ length: 5 }, (_, i) => ({ run_id: 'r', document_index: 0, cell_id: `c${i}`, document_name: 'doc', question: 'q', answer: 'answer', quote: 'quote', evidence: 'evidence', page: i + 1, status: 'completed', citation_status: 'verified_exact', support_status: 'supported' })) }))
      return new Response('{}', { status: 404 })
    }))
    render(<RunsPage />)
    await screen.findByText('r')
    fireEvent.click(screen.getByRole('button', { name: /Open run r/i }))
    await waitFor(() => expect(document.querySelectorAll('.ag-row').length).toBe(5), { timeout: 5000 })
    expect(document.querySelector('.ag-row[row-index="0"] [col-id="page"]')).toHaveTextContent('1')
    expect(screen.getByRole('columnheader', { name: /Evidence/i })).toBeInTheDocument()
  })
})

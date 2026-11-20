import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import BatchPage from '../BatchPage'

async function openChainMode() {
  fireEvent.click(screen.getByText('Structured chain'))
}

describe('BatchPage', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  it('submits multiple PDFs and renders document QA cards with citation evidence and export links', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      status: 'success',
      run_id: 'run-123',
      documents_count: 2,
      data: [
        {
          document_name: 'alpha.pdf',
          question: 'What was revenue?',
          answer: '1800 million',
          quote: 'Cloud revenue was 1800 million.',
          citation_status: 'verified_exact',
          page: 1,
        },
        {
          document_name: 'beta.pdf',
          question: 'What was revenue?',
          answer: '950 million',
          quote: 'Software revenue was 950 million.',
          citation_status: 'verified_normalized',
          page: 2,
        },
      ],
    }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    render(<BatchPage />)

    const input = screen.getByLabelText('Choose PDF files') as HTMLInputElement
    const files = [
      new File(['%PDF-1.4 alpha'], 'alpha.pdf', { type: 'application/pdf' }),
      new File(['%PDF-1.4 beta'], 'beta.pdf', { type: 'application/pdf' }),
    ]
    fireEvent.change(input, { target: { files } })
    fireEvent.change(screen.getByLabelText('Analysis questions'), { target: { value: 'What was revenue?' } })
    fireEvent.change(screen.getByLabelText('OpenMatrix bearer token'), { target: { value: 'om-token' } })
    fireEvent.change(screen.getByLabelText('Model service base URL'), { target: { value: 'http://127.0.0.1:3000/v1' } })
    fireEvent.change(screen.getByLabelText('Provider API key'), { target: { value: 'test-key' } })
    fireEvent.change(screen.getByLabelText('Model name'), { target: { value: 'exact-model' } })
    fireEvent.click(screen.getByRole('button', { name: /Review & launch|确认并启动/ }))
    await screen.findByText('Launch this analysis?')
    fireEvent.click(screen.getByRole('button', { name: /Run batch analysis|运行批量分析/ }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const firstCall = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(firstCall[0]).toBe('/api/v1/analyze-batch')
    expect(firstCall[1].headers).toEqual({ Authorization: 'Bearer om-token' })
    expect((await screen.findAllByText('alpha.pdf')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('beta.pdf').length).toBeGreaterThan(0)
    expect(screen.getByText('Cloud revenue was 1800 million.')).toBeInTheDocument()
    expect(screen.getByText('Quote found (normalized)')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Export XLSX' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Export CSV' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /View in results|在结果中查看/ })).toBeInTheDocument()
  })

  it('loads expert preset questions into the textarea', async () => {
    render(<BatchPage />)

    fireEvent.click(screen.getByText('10-K financial diagnosis'))

    expect(screen.getByText('Expert presets are provided for informational purposes only and do not constitute legal or financial advice.')).toBeInTheDocument()
    const questions = screen.getByLabelText('Analysis questions') as HTMLTextAreaElement
    expect(questions.value).toContain('primary risk factors')
    expect(questions.value).toContain('related-party transactions')
  })

  it('submits follow-up chain steps to the authenticated v1 endpoint and renders step cards', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      status: 'success',
      run_id: 'chain-123',
      steps_count: 3,
      data: [
        {
          document_name: 'alpha.pdf',
          step_id: 'revenue',
          depends_on: [],
          question: 'What was total revenue?',
          answer: '125 million dollars.',
          quote: 'Revenue was 125 million dollars.',
          citation_status: 'verified_exact',
          page: 1,
        },
        {
          document_name: 'alpha.pdf',
          step_id: 'margin',
          depends_on: ['revenue'],
          question: 'What margin is implied?',
          answer: '25% margin.',
          quote: 'Gross profit was 31.25 million dollars.',
          citation_status: 'verified_fuzzy',
          page: 2,
        },
      ],
    }), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    render(<BatchPage />)
    await openChainMode()

    fireEvent.change(screen.getByLabelText('Choose the PDF file for chain analysis'), {
      target: { files: [new File(['%PDF-1.4 alpha'], 'alpha.pdf', { type: 'application/pdf' })] },
    })
    fireEvent.change(screen.getByLabelText('OpenMatrix bearer token'), { target: { value: 'om-token' } })
    fireEvent.change(screen.getByLabelText('Model service base URL'), { target: { value: 'http://127.0.0.1:3000/v1' } })
    fireEvent.change(screen.getByLabelText('Provider API key'), { target: { value: 'test-key' } })
    fireEvent.change(screen.getByLabelText('Model name'), { target: { value: 'chain-model' } })

    fireEvent.click(screen.getByText('Raw step DSL'))
    const dsl = await screen.findByLabelText('Chain analysis steps')
    fireEvent.change(dsl, {
      target: { value: 'revenue | What was total revenue?\nrisk | Does A | B apply?\nmargin | What margin | leverage is implied? | deps=revenue,risk' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Review & launch|确认并启动/ }))
    await screen.findByText('Launch this analysis?')
    fireEvent.click(screen.getByRole('button', { name: /Run follow-up chain|运行后续链/ }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/v1/analyze-chain')
    expect(init.headers).toEqual({ Authorization: 'Bearer om-token' })
    const body = init.body as FormData
    expect(body.get('api_key')).toBe('test-key')
    expect(body.get('base_url')).toBe('http://127.0.0.1:3000/v1')
    expect(body.get('model_name')).toBe('chain-model')
    expect(body.get('use_kg')).toBe('false')
    expect(body.get('index_kg')).toBe('false')
    expect(JSON.parse(String(body.get('steps')))).toEqual([
      { id: 'revenue', question: 'What was total revenue?', depends_on: [] },
      { id: 'risk', question: 'Does A | B apply?', depends_on: [] },
      { id: 'margin', question: 'What margin | leverage is implied?', depends_on: ['revenue', 'risk'] },
    ])
    expect(await screen.findByText('Chain run chain-123')).toBeInTheDocument()
    expect(screen.getByText('depends on revenue')).toBeInTheDocument()
    expect(screen.getByText('25% margin.')).toBeInTheDocument()
    expect(screen.getByText('Quote found (fuzzy)')).toBeInTheDocument()
  })

  it('sends KG flags and polls the returned index task', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/v1/analyze-chain') {
        return new Response(JSON.stringify({ status: 'success', run_id: 'chain-9', steps_count: 0, kg_index_task_id: 't-kg123', data: [] }), { status: 200 })
      }
      if (url === '/api/v1/tasks/t-kg123') {
        return new Response(JSON.stringify({ task_id: 't-kg123', status: 'succeeded', operation: 'kg_index_document' }), { status: 200 })
      }
      return new Response(JSON.stringify({ error: 'not found' }), { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<BatchPage />)
    await openChainMode()

    fireEvent.change(screen.getByLabelText('Choose the PDF file for chain analysis'), {
      target: { files: [new File(['%PDF-1.4'], 'alpha.pdf', { type: 'application/pdf' })] },
    })
    fireEvent.change(screen.getByLabelText('Model service base URL'), { target: { value: 'http://127.0.0.1:3000/v1' } })
    fireEvent.change(screen.getByLabelText('Provider API key'), { target: { value: 'test-key' } })
    fireEvent.change(screen.getByLabelText('Model name'), { target: { value: 'chain-model' } })
    fireEvent.click(screen.getByLabelText('Enable knowledge graph retrieval'))
    fireEvent.click(screen.getByLabelText('Build or rebuild knowledge graph index'))
    fireEvent.click(screen.getByRole('button', { name: /Review & launch|确认并启动/ }))
    await screen.findByText('Launch this analysis?')
    fireEvent.click(screen.getByRole('button', { name: /Run follow-up chain|运行后续链/ }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/v1/analyze-chain')
    const body = init.body as FormData
    expect(body.get('use_kg')).toBe('true')
    expect(body.get('index_kg')).toBe('true')
    await waitFor(() => expect(screen.getByText(/t-kg123/)).toBeInTheDocument())
    await waitFor(() => expect(screen.getByText('Succeeded')).toBeInTheDocument())
    vi.unstubAllGlobals()
  })
})


it('downloads PDF, DOCX and PPTX through the REST endpoints', async () => {
  const oldCreate = URL.createObjectURL
  const oldRevoke = URL.revokeObjectURL
  URL.createObjectURL = vi.fn(() => 'blob:fixture')
  URL.revokeObjectURL = vi.fn()
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/export-')) return new Response(new Uint8Array([37,80,68,70,45,49]), { status: 200 })
    return new Response(JSON.stringify({
      status: 'success', run_id: 'run-export', documents_count: 1,
      data: [{ document_name: 'alpha.pdf', question: 'What was revenue?', answer: '1800 million', quote: 'Revenue was 1800 million.', citation_status: 'verified_exact', page: 1 }],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<BatchPage />)
  fireEvent.change(screen.getByLabelText('Choose PDF files'), { target: { files: [new File(['%PDF'], 'alpha.pdf', { type: 'application/pdf' })] } })
  fireEvent.change(screen.getByLabelText('Analysis questions'), { target: { value: 'What was revenue?' } })
  fireEvent.change(screen.getByLabelText('Model service base URL'), { target: { value: 'http://127.0.0.1:3000/v1' } })
  fireEvent.change(screen.getByLabelText('Provider API key'), { target: { value: 'test-key' } })
  fireEvent.change(screen.getByLabelText('Model name'), { target: { value: 'exact-model' } })
  fireEvent.click(screen.getByRole('button', { name: /Review & launch|确认并启动/ }))
  fireEvent.click(await screen.findByRole('button', { name: /Run batch analysis|运行批量分析/ }))
  await screen.findByText('Revenue was 1800 million.')
  for (const [index, format] of ['pdf', 'docx', 'pptx'].entries()) {
    const button = screen.getByRole('button', { name: `Export ${format.toUpperCase()}` })
    expect(button).toBeEnabled()
    fireEvent.click(button)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(index + 2))
    expect(fetchMock.mock.calls[index + 1][0]).toBe(`/api/v1/export-${format}/run-export`)
    await waitFor(() => expect(click).toHaveBeenCalledTimes(index + 1))
    await waitFor(() => expect(button).not.toHaveClass('ant-btn-loading'))
  }
  expect(URL.createObjectURL).toHaveBeenCalledTimes(3)
  click.mockRestore()
  URL.createObjectURL = oldCreate
  URL.revokeObjectURL = oldRevoke
})

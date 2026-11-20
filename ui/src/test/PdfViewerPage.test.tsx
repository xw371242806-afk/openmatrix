import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import * as pdfjsLib from 'pdfjs-dist'
import i18n from '../i18n'
// The p5 patch locale is not merged into the main locale files yet; register
// it here so assertions can use the user-facing copy.
import p5en from '../locales/patches/p5.en.json'
import PdfViewerPage from '../PdfViewerPage'

i18n.addResourceBundle('en', 'translation', p5en, true, true)

vi.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: { workerSrc: '' },
  getDocument: vi.fn(),
}))

function makePage(items: unknown[] = [], convert?: (x: number, y: number) => number[]) {
  return {
    getViewport: vi.fn(({ scale }: { scale: number }) => ({
      width: 600 * scale,
      height: 800 * scale,
      convertToViewportPoint: convert || ((x: number, y: number) => [x * scale, y * scale]),
    })),
    getTextContent: vi.fn().mockResolvedValue({ items }),
    render: vi.fn().mockReturnValue({ promise: Promise.resolve() }),
  }
}

describe('PdfViewerPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('loads a PDF via URL and renders page status', async () => {
    const page = makePage([{ str: 'hello', transform: [1, 0, 0, 1, 10, 20], width: 50, height: 10 }])
    const mockDoc = { numPages: 1, getPage: vi.fn().mockResolvedValue(page) }
    vi.mocked(pdfjsLib.getDocument).mockReturnValue({
      promise: Promise.resolve(mockDoc),
      destroy: vi.fn().mockResolvedValue(undefined),
    } as unknown as pdfjsLib.PDFDocumentLoadingTask)
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
    })

    render(<PdfViewerPage />)
    expect(screen.getByText('No document open')).toBeInTheDocument()
    fireEvent.change(screen.getByPlaceholderText('Enter PDF URL (CORS-enabled)'), {
      target: { value: 'https://example.com/d.pdf' },
    })
    fireEvent.click(screen.getByText('Open from URL'))
    await waitFor(() => expect(screen.getByText('Loaded 1 pages')).toBeInTheDocument())
    expect(screen.getByText('p. 1')).toBeInTheDocument()
  })

  it('highlights a matching quote with a visible label chip', async () => {
    const page = makePage([{ str: 'Revenue was 125 million', transform: [1, 0, 0, 1, 10, 20], width: 50, height: 10 }])
    const mockDoc = { numPages: 1, getPage: vi.fn().mockResolvedValue(page) }
    vi.mocked(pdfjsLib.getDocument).mockReturnValue({
      promise: Promise.resolve(mockDoc),
      destroy: vi.fn().mockResolvedValue(undefined),
    } as unknown as pdfjsLib.PDFDocumentLoadingTask)
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
    })

    const { container } = render(<PdfViewerPage />)
    fireEvent.change(screen.getByPlaceholderText('Enter PDF URL (CORS-enabled)'), {
      target: { value: 'https://example.com/d.pdf' },
    })
    fireEvent.click(screen.getByText('Open from URL'))
    await waitFor(() => expect(screen.getByText('Loaded 1 pages')).toBeInTheDocument())

    fireEvent.change(screen.getByPlaceholderText('Enter quote to highlight'), { target: { value: 'Revenue' } })
    fireEvent.click(screen.getByText('Highlight'))
    const rect = container.querySelector('div[style*="rgba(255, 255, 0, 0.45)"]')
    expect(rect).toBeTruthy()
    expect(screen.getByText('Quote match')).toBeInTheDocument()
  })

  it('destroys the loading task on unmount', async () => {
    const destroy = vi.fn().mockResolvedValue(undefined)
    const page = makePage([])
    const mockDoc = { numPages: 1, getPage: vi.fn().mockResolvedValue(page) }
    vi.mocked(pdfjsLib.getDocument).mockReturnValue({
      promise: Promise.resolve(mockDoc),
      destroy,
    } as unknown as pdfjsLib.PDFDocumentLoadingTask)
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
    })

    const { unmount } = render(<PdfViewerPage />)
    fireEvent.change(screen.getByPlaceholderText('Enter PDF URL (CORS-enabled)'), {
      target: { value: 'https://example.com/d.pdf' },
    })
    fireEvent.click(screen.getByText('Open from URL'))
    await waitFor(() => expect(screen.getByText('Loaded 1 pages')).toBeInTheDocument())
    unmount()
    expect(destroy).toHaveBeenCalled()
  })

  it('surfaces a fetch error', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: false, status: 404 })
    render(<PdfViewerPage />)
    fireEvent.change(screen.getByPlaceholderText('Enter PDF URL (CORS-enabled)'), {
      target: { value: 'https://example.com/missing.pdf' },
    })
    fireEvent.click(screen.getByText('Open from URL'))
    await waitFor(() => expect(screen.getByText('Error')).toBeInTheDocument())
    expect(screen.getByText('HTTP 404')).toBeInTheDocument()
  })

  it('renders pasted locate_quote_bboxes JSON as scaled overlays', async () => {
    const page = makePage([])
    const mockDoc = { numPages: 1, getPage: vi.fn().mockResolvedValue(page) }
    vi.mocked(pdfjsLib.getDocument).mockReturnValue({
      promise: Promise.resolve(mockDoc),
      destroy: vi.fn().mockResolvedValue(undefined),
    } as unknown as pdfjsLib.PDFDocumentLoadingTask)
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
    })

    const { container } = render(<PdfViewerPage />)
    fireEvent.change(screen.getByPlaceholderText('Enter PDF URL (CORS-enabled)'), {
      target: { value: 'https://example.com/d.pdf' },
    })
    fireEvent.click(screen.getByText('Open from URL'))
    await waitFor(() => expect(screen.getByText('Loaded 1 pages')).toBeInTheDocument())

    fireEvent.change(screen.getByLabelText('Bounding-box JSON'), {
      target: { value: JSON.stringify({ matches: [{ page: 1, bbox: [10, 20, 80, 30], quote: 'Revenue quote' }] }) },
    })
    fireEvent.click(screen.getByText('Apply bbox JSON'))

    const highlight = await screen.findByTestId('bbox-highlight')
    expect(highlight).toHaveAttribute('title', 'Revenue quote')
    expect(highlight).toHaveStyle({ left: '12px', top: '24px', width: '84px', height: '12px' })
    expect(container).toHaveTextContent('Loaded 1 bbox highlight')
    expect(screen.getByText('Box match')).toBeInTheDocument()
  })

  it('clears pasted bbox overlays when a different PDF is loaded', async () => {
    const firstPage = makePage([])
    const secondPage = makePage([])
    vi.mocked(pdfjsLib.getDocument)
      .mockReturnValueOnce({
        promise: Promise.resolve({ numPages: 1, getPage: vi.fn().mockResolvedValue(firstPage) }),
        destroy: vi.fn().mockResolvedValue(undefined),
      } as unknown as pdfjsLib.PDFDocumentLoadingTask)
      .mockReturnValueOnce({
        promise: Promise.resolve({ numPages: 1, getPage: vi.fn().mockResolvedValue(secondPage) }),
        destroy: vi.fn().mockResolvedValue(undefined),
      } as unknown as pdfjsLib.PDFDocumentLoadingTask)
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
    })

    render(<PdfViewerPage />)
    fireEvent.change(screen.getByPlaceholderText('Enter PDF URL (CORS-enabled)'), { target: { value: 'https://example.com/first.pdf' } })
    fireEvent.click(screen.getByText('Open from URL'))
    await waitFor(() => expect(screen.getByText('Loaded 1 pages')).toBeInTheDocument())

    fireEvent.change(screen.getByLabelText('Bounding-box JSON'), {
      target: { value: JSON.stringify({ matches: [{ page: 1, bbox: [10, 20, 80, 30], quote: 'First doc quote' }] }) },
    })
    fireEvent.click(screen.getByText('Apply bbox JSON'))
    expect(await screen.findByTestId('bbox-highlight')).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText('Enter PDF URL (CORS-enabled)'), { target: { value: 'https://example.com/second.pdf' } })
    fireEvent.click(screen.getByText('Open from URL'))
    await waitFor(() => expect(pdfjsLib.getDocument).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByTestId('bbox-highlight')).not.toBeInTheDocument())
    expect((screen.getByLabelText('Bounding-box JSON') as HTMLTextAreaElement).value).toBe('')
  })
})

  it('loads retained run evidence from the hash', async () => {
    const scroll = vi.fn()
    HTMLElement.prototype.scrollIntoView = scroll
    window.location.hash = '/evidence?run=run-safe&doc=0&page=2&cell=d0q0'
    const page = makePage([{ str: 'Revenue was 125 million', transform: [1, 0, 0, 1, 10, 20], width: 50, height: 10 }])
    const mockDoc = { numPages: 2, getPage: vi.fn().mockResolvedValue(page) }
    vi.mocked(pdfjsLib.getDocument).mockReturnValue({ promise: Promise.resolve(mockDoc), destroy: vi.fn().mockResolvedValue(undefined) } as unknown as pdfjsLib.PDFDocumentLoadingTask)
    globalThis.fetch = vi.fn()
      .mockResolvedValueOnce({ ok: true, status: 200, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)) })
      .mockResolvedValueOnce({ ok: true, status: 200, json: () => Promise.resolve({ quote: 'Revenue was 125 million', matches: [] }) })
    render(<PdfViewerPage />)
    await waitFor(() => expect(screen.getByText('Loaded 2 pages')).toBeInTheDocument())
    expect(globalThis.fetch).toHaveBeenNthCalledWith(1, '/api/v1/runs/run-safe/documents/0/file', expect.any(Object))
    await waitFor(() => expect(globalThis.fetch).toHaveBeenNthCalledWith(2, '/api/v1/runs/run-safe/cells/d0q0/bboxes', expect.objectContaining({ method: 'POST', body: JSON.stringify({ page: 2 }) })))
    expect(scroll).toHaveBeenCalled()
    window.location.hash = ''
  })

import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Input, message, Spin, Upload } from 'antd'
import { FileAddOutlined, FilePdfOutlined } from '@ant-design/icons'
import { getAccessToken, getPdfFile, postJson } from './api/client'
import { hashSearchParams } from './lib/hashRouter'
import { useTranslation } from 'react-i18next'
import * as pdfjsLib from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import PageHeader from './components/PageHeader'
import EmptyState from './components/EmptyState'
import ErrorState from './components/ErrorState'
import { CitationStatusTag } from './components/StatusTag'
import './pdf.css'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

interface TextSpan {
  x: number
  y: number
  width: number
  height: number
  text: string
}

interface RenderedPage {
  page: pdfjsLib.PDFPageProxy
  viewport: { width: number; height: number }
  spans: TextSpan[]
}

interface BboxMatch {
  page: number
  bbox: [number, number, number, number]
  quote?: string
}

interface PdfStatus {
  key: string
  options?: Record<string, string | number>
}

const SCALE = 1.2

const LEGEND_ITEMS: Array<{ value: string; noteKey: string }> = [
  { value: 'verified_exact', noteKey: 'pdf.legend_verified_exact' },
  { value: 'verified_normalized', noteKey: 'pdf.legend_verified_normalized' },
  { value: 'verified_fuzzy', noteKey: 'pdf.legend_verified_fuzzy' },
  { value: 'not_found', noteKey: 'pdf.legend_not_found' },
  { value: 'not_applicable', noteKey: 'pdf.legend_not_applicable' },
]

const PdfViewerPage = () => {
  const { t } = useTranslation()
  const [pages, setPages] = useState<RenderedPage[]>([])
  const [quote, setQuote] = useState('')
  const [url, setUrl] = useState('')
  const [status, setStatus] = useState<PdfStatus>({ key: 'pdf.ready' })
  const [loading, setLoading] = useState(false)
  const [errorDetail, setErrorDetail] = useState('')
  const [renderFailures, setRenderFailures] = useState<Record<number, string>>({})
  const docRef = useRef<pdfjsLib.PDFDocumentLoadingTask | null>(null)
  const readerRef = useRef<FileReader | null>(null)
  const canvasRefs = useRef<Array<HTMLCanvasElement | null>>([])
  const loadIdRef = useRef(0)
  const [highlightQuery, setHighlightQuery] = useState('')
  const [bboxJson, setBboxJson] = useState('')
  const [bboxMatches, setBboxMatches] = useState<BboxMatch[]>([])
  const [targetPage, setTargetPage] = useState<number | null>(null)

  const cleanup = useCallback(() => {
    loadIdRef.current++
    if (readerRef.current) {
      readerRef.current.abort()
      readerRef.current = null
    }
    if (docRef.current) {
      docRef.current.destroy().catch(() => {})
      docRef.current = null
    }
    setPages([])
    setBboxMatches([])
    setBboxJson('')
  }, [])

  useEffect(() => cleanup, [cleanup])

  const renderPages = useCallback(async (doc: pdfjsLib.PDFDocumentProxy) => {
    const rendered: RenderedPage[] = []
    for (let i = 1; i <= doc.numPages; i++) {
      const page = await doc.getPage(i)
      const viewport = page.getViewport({ scale: SCALE })
      const content = await page.getTextContent()
      const spans: TextSpan[] = []
      for (const item of content.items) {
        if ('str' in item && item.str) {
          const t = item as unknown as { transform: number[]; width: number; height: number; str: string }
          const [a, b, c, d, e, f] = t.transform
          const scaleX = Math.hypot(a, b) || 1
          const vxX = (a / scaleX) * t.width
          const vxY = (b / scaleX) * t.width

          let vyX: number
          let vyY: number
          const scaleY = Math.hypot(c, d)
          if (scaleY > 1e-6) {
            vyX = (c / scaleY) * t.height
            vyY = (d / scaleY) * t.height
          } else {
            vyX = (-b / scaleX) * t.height
            vyY = (a / scaleX) * t.height
          }

          const corners = [
            viewport.convertToViewportPoint(e, f),
            viewport.convertToViewportPoint(e + vxX, f + vxY),
            viewport.convertToViewportPoint(e + vxX + vyX, f + vxY + vyY),
            viewport.convertToViewportPoint(e + vyX, f + vyY),
          ]
          const xs = corners.map((pt) => pt[0])
          const ys = corners.map((pt) => pt[1])
          const minX = Math.min(...xs)
          const maxX = Math.max(...xs)
          const minY = Math.min(...ys)
          const maxY = Math.max(...ys)

          spans.push({
            x: minX,
            y: minY,
            width: maxX - minX,
            height: maxY - minY,
            text: t.str,
          })
        }
      }
      rendered.push({ page, viewport, spans })
    }
    setPages(rendered)
  }, [])

  const drawPage = useCallback(async (rendered: RenderedPage, index: number) => {
    const canvas = canvasRefs.current[index]
    if (!canvas) return
    canvas.width = rendered.viewport.width
    canvas.height = rendered.viewport.height
    try {
      await rendered.page.render({ canvas, viewport: rendered.page.getViewport({ scale: SCALE }) }).promise
      setRenderFailures((prev) => {
        if (!(index in prev)) return prev
        const next = { ...prev }
        delete next[index]
        return next
      })
    } catch (e) {
      // Intentional teardown is silent; real failures surface inline per page.
      const msg = (e as Error).message || ''
      if (msg.includes('destroy') || msg.includes('cancelled') || msg.includes('aborted')) return
      setRenderFailures((prev) => (prev[index] === msg ? prev : { ...prev, [index]: msg }))
    }
  }, [])

  useEffect(() => {
    pages.forEach((p, i) => { drawPage(p, i) })
  }, [pages, drawPage])

  const loadPdf = async (data: ArrayBuffer, loadId?: number) => {
    const currentLoadId = loadId ?? ++loadIdRef.current
    setLoading(true)
    setErrorDetail('')
    setRenderFailures({})
    setBboxMatches([])
    setBboxJson('')
    setStatus({ key: 'pdf.loading' })
    try {
      if (docRef.current) {
        docRef.current.destroy().catch(() => {})
        docRef.current = null
      }
      const task = pdfjsLib.getDocument({ data })
      docRef.current = task
      const doc = await task.promise
      if (loadIdRef.current !== currentLoadId) return
      await renderPages(doc)
      if (loadIdRef.current !== currentLoadId) return
      setStatus({ key: 'pdf.loaded_pages', options: { count: doc.numPages } })
      message.success(t('pdf.pdf_loaded'))
    } catch (e) {
      if (loadIdRef.current !== currentLoadId) return
      const msg = (e as Error).message || ''
      if (msg.includes('destroy') || msg.includes('cancelled') || msg.includes('aborted')) return
      message.error(t('pdf.load_failed', { message: msg }))
      setErrorDetail(msg)
      setStatus({ key: 'pdf.error' })
    } finally {
      if (loadIdRef.current === currentLoadId) setLoading(false)
    }
  }

  const handleFile = (file: File) => {
    if (readerRef.current) {
      readerRef.current.abort()
      readerRef.current = null
    }
    const currentLoadId = ++loadIdRef.current
    setLoading(true)
    setErrorDetail('')
    setRenderFailures({})
    setStatus({ key: 'pdf.reading' })
    const reader = new FileReader()
    readerRef.current = reader
    reader.onload = (e) => {
      if (readerRef.current === reader) readerRef.current = null
      if (loadIdRef.current !== currentLoadId) return
      if (e.target?.result) loadPdf(e.target.result as ArrayBuffer, currentLoadId)
    }
    reader.onerror = () => {
      if (readerRef.current === reader) readerRef.current = null
      if (loadIdRef.current !== currentLoadId) return
      message.error(t('pdf.read_failed'))
      setErrorDetail('')
      setStatus({ key: 'pdf.error' })
      setLoading(false)
    }
    reader.onabort = () => {
      if (readerRef.current === reader) readerRef.current = null
    }
    reader.readAsArrayBuffer(file)
  }

  const handleUrlLoad = async () => {
    if (!url.trim()) return
    const currentLoadId = ++loadIdRef.current
    setLoading(true)
    setErrorDetail('')
    setRenderFailures({})
    setStatus({ key: 'pdf.fetching' })
    try {
      const res = await fetch(url.trim(), { mode: 'cors' })
      if (loadIdRef.current !== currentLoadId) return
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const buf = await res.arrayBuffer()
      if (loadIdRef.current !== currentLoadId) return
      await loadPdf(buf, currentLoadId)
    } catch (e) {
      if (loadIdRef.current !== currentLoadId) return
      const msg = (e as Error).message || ''
      if (msg.includes('destroy') || msg.includes('cancelled') || msg.includes('aborted')) return
      message.error(t('pdf.fetch_failed', { message: msg }))
      setErrorDetail(msg)
      setStatus({ key: 'pdf.error' })
      setLoading(false)
    }
  }

  const isMatch = (span: TextSpan) => {
    if (!highlightQuery) return false
    return span.text.toLowerCase().includes(highlightQuery.toLowerCase())
  }

  const applyBboxJson = () => {
    try {
      const parsed = JSON.parse(bboxJson)
      const rawMatches = Array.isArray(parsed?.matches) ? parsed.matches : []
      const matches: BboxMatch[] = rawMatches.slice(0, 100).flatMap((item: unknown) => {
        const candidate = item as { page?: unknown; bbox?: unknown; quote?: unknown }
        if (!Number.isInteger(candidate.page) || (candidate.page as number) < 1) return []
        if (!Array.isArray(candidate.bbox) || candidate.bbox.length !== 4) return []
        const coords = candidate.bbox.map(Number)
        if (!coords.every(Number.isFinite)) return []
        const [x0, top, x1, bottom] = coords
        if (x1 <= x0 || bottom <= top) return []
        return [{ page: candidate.page as number, bbox: [x0, top, x1, bottom], quote: typeof candidate.quote === 'string' ? candidate.quote : undefined }]
      })
      setBboxMatches(matches)
      setStatus({
        key:
        matches.length === 1
          ? 'pdf.bbox_loaded_one'
          : matches.length > 1
            ? 'pdf.bbox_loaded_many'
            : 'pdf.bbox_empty',
        options: matches.length > 1 ? { count: matches.length } : undefined,
      })
    } catch (e) {
      message.error(t('pdf.invalid_bbox', { message: (e as Error).message }))
    }
  }

  useEffect(() => {
    const params = hashSearchParams(window.location.hash)
    const runId = params.get('run')?.trim() || ''
    const documentIndex = params.get('doc')?.trim() || ''
    const cellId = params.get('cell')?.trim() || ''
    const requestedPage = Number(params.get('page') || '')
    if (!runId || !/^\d+$/.test(documentIndex)) return
    setTargetPage(Number.isInteger(requestedPage) && requestedPage > 0 ? requestedPage : null)
    let cancelled = false
    const loadEvidence = async () => {
      const token = getAccessToken()
      const opts = token ? { token } : {}
      try {
        const data = await getPdfFile(`/api/v1/runs/${encodeURIComponent(runId)}/documents/${encodeURIComponent(documentIndex)}/file`, opts)
        if (cancelled) return
        await loadPdf(data)
        if (cancelled) return
        if (cellId) {
          const cell = await postJson<{ quote?: string; matches?: BboxMatch[] }>(`/api/v1/runs/${encodeURIComponent(runId)}/cells/${encodeURIComponent(cellId)}/bboxes`, { page: requestedPage > 0 ? requestedPage : undefined }, opts)
          if (cancelled) return
          if (cell.quote) { setQuote(cell.quote); setHighlightQuery(cell.quote) }
          if (Array.isArray(cell.matches)) setBboxMatches(cell.matches)
        }
      } catch (e) {
        if (!cancelled) { setErrorDetail((e as Error).message || String(e)); setStatus({ key: 'pdf.error' }) }
      }
    }
    void loadEvidence()
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (!targetPage || pages.length === 0) return
    document.querySelector(`[data-pdf-page="${targetPage}"]`)?.scrollIntoView({ block: 'start' })
  }, [pages, targetPage])

  const isError = status.key === 'pdf.error'
  const hasDocument = pages.length > 0

  return (
    <div className="pdf-page">
      <PageHeader
        title={t('nav.evidence')}
        description={t('pdf.viewer_subtitle')}
      />

      <div className="pdf-workbench">
        <aside className="desk-card pdf-rail">
          <section className="pdf-rail-section" aria-label={t('pdf.section_source')}>
            <h3 className="section-label">{t('pdf.section_source')}</h3>
            <Upload accept=".pdf" maxCount={1} beforeUpload={() => false} showUploadList={false}
              onChange={({ file, fileList }) => {
                const f = (file.originFileObj || fileList?.[fileList.length - 1]?.originFileObj || fileList?.[0]?.originFileObj) as File | undefined
                if (f) handleFile(f)
              }}>
              <Button icon={<FileAddOutlined aria-hidden="true" />}>{t('pdf.upload')}</Button>
            </Upload>
            <div className="pdf-url-row">
              <Input
                aria-label={t('pdf.url_label')}
                placeholder={t('pdf.url_placeholder')}
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                onPressEnter={handleUrlLoad}
                allowClear
              />
              <Button onClick={handleUrlLoad}>{t('pdf.open_url')}</Button>
            </div>
          </section>

          <section className="pdf-rail-section" aria-label={t('pdf.section_quote')}>
            <h3 className="section-label">{t('pdf.section_quote')}</h3>
            <Input
              aria-label={t('pdf.quote_label')}
              placeholder={t('pdf.quote_placeholder')}
              value={quote}
              onChange={(e) => setQuote(e.target.value)}
              onPressEnter={() => setHighlightQuery(quote.trim())}
              allowClear
            />
            <div className="pdf-btn-row">
              <Button
                type="primary"
                onClick={() => setHighlightQuery(quote.trim())}
                disabled={!quote.trim()}
              >
                {t('pdf.highlight')}
              </Button>
              {highlightQuery && (
                <Button onClick={() => { setHighlightQuery(''); setQuote('') }}>{t('pdf.clear')}</Button>
              )}
            </div>
          </section>

          <section className="pdf-rail-section">
            <h3 className="section-label mono">{t('pdf.bbox_label')}</h3>
            <Input.TextArea
              aria-label={t('pdf.bbox_label')}
              placeholder={t('pdf.bbox_placeholder')}
              value={bboxJson}
              onChange={(e) => setBboxJson(e.target.value)}
              rows={5}
              className="mono"
            />
            <div className="pdf-btn-row">
              <Button onClick={applyBboxJson} disabled={!bboxJson.trim()}>{t('pdf.apply_bbox')}</Button>
              {bboxMatches.length > 0 && (
                <Button onClick={() => { setBboxMatches([]); setBboxJson('') }}>{t('pdf.clear_bbox')}</Button>
              )}
            </div>
          </section>

          <section className="pdf-rail-section" aria-label={t('pdf.legend_title')}>
            <h3 className="section-label">{t('pdf.legend_title')}</h3>
            <ul className="pdf-legend">
              {LEGEND_ITEMS.map((item) => (
                <li key={item.value} className="pdf-legend-item">
                  <CitationStatusTag value={item.value} />
                  <span className="pdf-legend-text">{t(item.noteKey)}</span>
                </li>
              ))}
            </ul>
          </section>
        </aside>

        <section className="desk-card pdf-stage-card" aria-label={t('nav.evidence')}>
          {isError ? (
            <ErrorState title={t('pdf.error')} detail={errorDetail} />
          ) : (
            <p className="pdf-status mono" role="status">{t(status.key, status.options)}</p>
          )}

          <div className="pdf-stage">
            {loading && (
              <div className="pdf-stage-loading">
                <Spin size="large" aria-label={t(status.key, status.options)} />
              </div>
            )}

            {!hasDocument && !loading && !isError && (
              <EmptyState
                icon={<FilePdfOutlined />}
                title={t('pdf.empty_title')}
                hint={t('pdf.empty_hint')}
              />
            )}

            {hasDocument && (
              <div className="pdf-scroll">
                {pages.map((p, i) => {
                  const hits = p.spans.filter(isMatch)
                  const firstHit = hits[0]
                  const pageBoxes = bboxMatches.filter((match) => match.page === i + 1)
                  const firstBox = pageBoxes[0]
                  return (
                    <figure key={i} className="pdf-sheet-wrap">
                      <div className="pdf-sheet" data-pdf-page={i + 1}>
                        <canvas ref={(el) => { canvasRefs.current[i] = el }} />
                        {hits.map((span, j) => (
                          <div key={j} style={{
                            position: 'absolute',
                            left: span.x,
                            top: span.y,
                            width: span.width,
                            height: span.height,
                            backgroundColor: 'rgba(255, 255, 0, 0.45)',
                            pointerEvents: 'none',
                          }} />
                        ))}
                        {firstHit && (
                          <div className="pdf-match-chip pdf-match-chip--quote" style={{ left: firstHit.x, top: firstHit.y }}>
                            {t('pdf.quote_match_label')}
                          </div>
                        )}
                        {pageBoxes.map((match, j) => {
                          const [x0, top, x1, bottom] = match.bbox
                          return (
                            <div key={'bbox-' + j} data-testid="bbox-highlight" title={match.quote} style={{
                              position: 'absolute',
                              left: x0 * SCALE,
                              top: top * SCALE,
                              width: (x1 - x0) * SCALE,
                              height: (bottom - top) * SCALE,
                              backgroundColor: 'rgba(24, 144, 255, 0.22)',
                              border: '2px solid rgba(24, 144, 255, 0.85)',
                              pointerEvents: 'none',
                              boxSizing: 'border-box',
                            }} />
                          )
                        })}
                        {firstBox && (
                          <div
                            className="pdf-match-chip pdf-match-chip--bbox"
                            style={{ left: firstBox.bbox[0] * SCALE, top: firstBox.bbox[1] * SCALE }}
                          >
                            {t('pdf.bbox_match_label')}
                          </div>
                        )}
                      </div>
                      <figcaption className="pdf-page-chip mono">{t('common.page_short', { page: i + 1 })}</figcaption>
                      {renderFailures[i] && (
                        <div className="pdf-page-note" role="alert">
                          {t('pdf.page_render_failed', { page: i + 1, message: renderFailures[i] })}
                        </div>
                      )}
                    </figure>
                  )
                })}
              </div>
            )}
          </div>
        </section>
      </div>
    </div>
  )
}

export default PdfViewerPage

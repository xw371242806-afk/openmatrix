import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Button, Drawer, Modal, Segmented, Spin, Tag } from 'antd'
import { NodeExpandOutlined, ReloadOutlined, SyncOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { AgGridReact } from 'ag-grid-react'
import type { ColDef, ICellRendererParams } from 'ag-grid-community'
import { AllCommunityModule, ModuleRegistry, themeQuartz } from 'ag-grid-community'
import { ApiError, downloadFile, getJson } from './api/client'
import type { MatrixCell, MatrixRunDetail, RunCounts } from './api/types'
import PageHeader from './components/PageHeader'
import EmptyState from './components/EmptyState'
import ErrorState from './components/ErrorState'
import { CellStatusTag, CitationStatusTag } from './components/StatusTag'
import './runs.css'

ModuleRegistry.registerModules([AllCommunityModule])

const agTheme = themeQuartz.withParams({
  accentColor: '#A05C2C',
  backgroundColor: '#FFFFFF',
  foregroundColor: '#1C1E22',
  borderColor: '#E9E9E6',
  headerBackgroundColor: '#F5F5F2',
  headerTextColor: '#52565D',
  oddRowBackgroundColor: '#FAFAF8',
  rowHoverColor: '#F1F1EE',
  fontFamily: "'Inter', 'Segoe UI', sans-serif",
  fontSize: 13,
})

function CellStatusRenderer(params: ICellRendererParams) {
  return <CellStatusTag value={params.value == null ? null : String(params.value)} />
}

function CitationRenderer(params: ICellRendererParams) {
  return <CitationStatusTag value={params.value == null ? null : String(params.value)} />
}

function ExpandableTextRenderer(params: ICellRendererParams) {
  const value = params.value == null ? '' : String(params.value)
  const onExpand = params.context?.onExpand as ((value: string, title: string) => void) | undefined
  return value ? <button className="runs-cell-preview" type="button" onClick={() => onExpand?.(value, String(params.colDef?.headerName ?? 'Details'))}>{value}</button> : <span>–</span>
}

function EvidenceRenderer(params: ICellRendererParams<MatrixCell>) {
  const row = params.data
  if (!row || row.page == null) return <span>—</span>
  const href = `#/evidence?run=${encodeURIComponent(row.run_id)}&doc=${encodeURIComponent(String(row.document_index))}&page=${encodeURIComponent(String(row.page))}&cell=${encodeURIComponent(row.cell_id)}`
  return <a href={href}>{`p. ${row.page}`}</a>
}

type RunKind = 'review' | 'failed' | 'completed' | 'progress' | 'unknown'
type ResultsFilter = 'all' | 'review' | 'failed' | 'completed'

function runKind(counts: RunCounts | undefined): RunKind {
  if (!counts) return 'unknown'
  if (counts.needs_review > 0) return 'review'
  if (counts.failed > 0) return 'failed'
  if (counts.total > 0 && counts.completed === counts.total) return 'completed'
  return 'progress'
}

export default function RunsPage() {
  const { t } = useTranslation()
  const [ids, setIds] = useState<string[]>([])
  const [counts, setCounts] = useState<Record<string, RunCounts>>({})
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [filter, setFilter] = useState<ResultsFilter>('all')

  const [selected, setSelected] = useState<string | null>(null)
  const [cells, setCells] = useState<MatrixCell[]>([])
  const [cellsLoading, setCellsLoading] = useState(false)
  const [cellError, setCellError] = useState<string | null>(null)
  const [exporting, setExporting] = useState<'xlsx' | 'csv' | null>(null)
  const [expandedText, setExpandedText] = useState<{ title: string; value: string } | null>(null)

  const reqRef = useRef(0)
  const cellReqRef = useRef(0)

  const loadRuns = useCallback(async () => {
    const req = ++reqRef.current
    setLoading(true)
    setLoadError(null)
    try {
      const data = await getJson<{ runs: string[] }>('/api/v1/runs?limit=20')
      const idList = data.runs ?? []
      if (req !== reqRef.current) return
      setIds(idList)
      const details = await Promise.allSettled(
        idList.map((id) => getJson<RunCounts>(`/api/v1/runs/${encodeURIComponent(id)}`)),
      )
      if (req !== reqRef.current) return
      const map: Record<string, RunCounts> = {}
      details.forEach((d, i) => {
        if (d.status === 'fulfilled') map[idList[i]] = d.value
      })
      setCounts(map)
    } catch (e) {
      if (req !== reqRef.current) return
      setLoadError((e as Error).message)
    } finally {
      if (req === reqRef.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadRuns()
  }, [loadRuns])

  const openRun = (id: string) => {
    setSelected(id)
    setCells([])
    setCellError(null)
    const req = ++cellReqRef.current
    setCellsLoading(true)
    getJson<MatrixRunDetail>(`/api/matrix-runs/${encodeURIComponent(id)}`)
      .then((d) => {
        if (req !== cellReqRef.current) return
        setCells(d.cells ?? [])
      })
      .catch((e) => {
        if (req !== cellReqRef.current) return
        setCellError((e as Error).message)
      })
      .finally(() => {
        if (req === cellReqRef.current) setCellsLoading(false)
      })
  }

  const downloadExport = async (format: 'xlsx' | 'csv') => {
    if (!selected) return
    setExporting(format)
    try {
      await downloadFile(
        `/api/v1/export-${format}/${encodeURIComponent(selected)}`,
        `matrix_${selected}.${format}`,
      )
    } catch (e) {
      setCellError((e as ApiError).message)
    } finally {
      setExporting(null)
    }
  }

  const colDefs = useMemo<ColDef<MatrixCell>[]>(
    () => [
      { field: 'document_name', headerName: t('runs.col_document'), flex: 1, minWidth: 130 },
      { field: 'question', headerName: t('runs.col_question'), flex: 2, minWidth: 170 },
      { field: 'answer', headerName: t('runs.col_answer'), flex: 2, minWidth: 170, cellRenderer: ExpandableTextRenderer },
      { field: 'quote', headerName: t('runs.col_quote'), flex: 2, minWidth: 150, hide: true, cellRenderer: ExpandableTextRenderer },
      { field: 'status', headerName: t('common.status'), width: 150, cellRenderer: CellStatusRenderer },
      { field: 'citation_status', headerName: t('results.col_citation'), width: 175, cellRenderer: CitationRenderer },
      { field: 'support_status', headerName: t('runs.col_support'), width: 175, filter: true, tooltipField: 'support_reason' },
      { field: 'page', headerName: t('runs.col_page'), width: 84, pinned: 'left', type: 'numericColumn' },
      { colId: 'evidence', pinned: 'left', headerName: t('runs.col_evidence'), width: 110, cellRenderer: EvidenceRenderer },
    ],
    [t],
  )

  const gridContext = useMemo(() => ({ onExpand: (value: string, title: string) => setExpandedText({ value, title }) }), [])

  const filtered = useMemo(
    () => ids.filter((id) => {
      if (filter === 'all') return true
      return runKind(counts[id]) === filter
    }),
    [ids, filter, counts],
  )

  const filterCounts = useMemo(() => {
    const tally: Record<Exclude<ResultsFilter, 'all'>, number> = { review: 0, failed: 0, completed: 0 }
    for (const id of ids) {
      const kind = runKind(counts[id])
      if (kind === 'review' || kind === 'failed' || kind === 'completed') tally[kind] += 1
    }
    return tally
  }, [ids, counts])

  const selectedCounts = selected ? counts[selected] : undefined

  return (
    <div className="results-page">
      <PageHeader title={t('results.title')} description={t('results.subtitle')} />
      <p>{t('runs.support_legend')}</p>

      {loading ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: '80px 0' }}>
          <Spin size="large" />
        </div>
      ) : loadError ? (
        <ErrorState title={t('results.load_failed')} detail={loadError} onRetry={() => void loadRuns()} />
      ) : ids.length === 0 ? (
        <EmptyState title={t('runs.no_runs')} hint={t('results.subtitle')} />
      ) : (
        <>
          <div className="results-toolbar">
            <Segmented
              value={filter}
              onChange={(value) => setFilter(String(value) as ResultsFilter)}
              aria-label={t('common.status')}
              options={[
                { value: 'all', label: `${t('results.filter_all')} ${ids.length}` },
                { value: 'review', label: `${t('results.filter_review')} ${filterCounts.review}` },
                { value: 'failed', label: `${t('results.filter_failed')} ${filterCounts.failed}` },
                { value: 'completed', label: `${t('results.filter_completed')} ${filterCounts.completed}` },
              ]}
            />
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span className="recent-note">{t('results.recent_note', { count: ids.length })}</span>
              <Button
                size="small"
                icon={<ReloadOutlined aria-hidden="true" />}
                onClick={() => void loadRuns()}
              >
                {t('common.refresh')}
              </Button>
            </div>
          </div>

          <div className="run-list">
            {filtered.length === 0 ? (
              <EmptyState title={t('results.no_runs_filtered')} />
            ) : (
              filtered.map((id) => {
                const c = counts[id]
                const total = c?.total ?? 0
                const completed = c?.completed ?? 0
                const pct = total > 0 ? Math.round((completed / total) * 100) : 0
                const kind = runKind(c)
                return (
                  <button
                    key={id}
                    type="button"
                    className="run-row"
                    aria-label={t('results.open_row', { id })}
                    onClick={() => openRun(id)}
                  >
                    <span className="run-id">{id}</span>
                    <span className="run-row-main">
                      <span className="run-row-stats">
                        <span className="run-chip">
                          <span className="run-chip-val">{c ? c.completed : '–'}</span>
                          <span className="run-chip-key">{t('runs.done')}</span>
                        </span>
                        {c && c.needs_review > 0 && (
                          <span className="run-chip run-chip--review">
                            <span className="run-chip-val">{c.needs_review}</span>
                            <span className="run-chip-key">{t('runs.review')}</span>
                          </span>
                        )}
                        {c && c.failed > 0 && (
                          <span className="run-chip run-chip--failed">
                            <span className="run-chip-val">{c.failed}</span>
                            <span className="run-chip-key">{t('runs.failed')}</span>
                          </span>
                        )}
                        <span className="run-chip">
                          <span className="run-chip-val">{c ? c.total : '–'}</span>
                          <span className="run-chip-key">{t('runs.total')}</span>
                        </span>
                      </span>
                      <span
                        className="run-progress"
                        role="progressbar"
                        aria-valuenow={pct}
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-label={t('results.progress_aria', { done: completed, total })}
                      >
                        <span className="run-progress-bar" style={{ width: `${pct}%` }} />
                      </span>
                    </span>
                    <span className="run-side">
                      {kind === 'review' && <CellStatusTag value="needs_review" />}
                      {kind === 'failed' && <CellStatusTag value="failed" />}
                      {kind === 'completed' && <CellStatusTag value="completed" />}
                      {kind === 'progress' && (
                        <Tag className="signal-tag signal-info" icon={<SyncOutlined className="spin-icon" aria-hidden="true" />}>
                          {t('runs.tag_in_progress')}
                        </Tag>
                      )}
                      {kind === 'unknown' && <Tag className="signal-tag signal-neutral">{t('common.unknown')}</Tag>}
                    </span>
                  </button>
                )
              })
            )}
          </div>
        </>
      )}

      <Drawer
        open={!!selected}
        onClose={() => setSelected(null)}
        size="80vw"
        title={
          <div className="drawer-head">
            <span style={{ fontSize: 15, fontWeight: 600 }}>{t('runs.drawer_title')}</span>
            <span className="drawer-run-id">{selected}</span>
          </div>
        }
      >
        {selected && (
          <div className="drawer-stats" style={{ marginBottom: 14 }}>
            <span className="run-chip">
              <span className="run-chip-val">{selectedCounts ? selectedCounts.completed : '–'}</span>
              <span className="run-chip-key">{t('runs.done')}</span>
            </span>
            {selectedCounts && selectedCounts.needs_review > 0 && (
              <span className="run-chip run-chip--review">
                <span className="run-chip-val">{selectedCounts.needs_review}</span>
                <span className="run-chip-key">{t('runs.review')}</span>
              </span>
            )}
            {selectedCounts && selectedCounts.failed > 0 && (
              <span className="run-chip run-chip--failed">
                <span className="run-chip-val">{selectedCounts.failed}</span>
                <span className="run-chip-key">{t('runs.failed')}</span>
              </span>
            )}
            <span className="run-chip">
              <span className="run-chip-val">{selectedCounts ? selectedCounts.total : '–'}</span>
              <span className="run-chip-key">{t('runs.total')}</span>
            </span>
            <div className="drawer-actions">
              <Button size="small" loading={exporting === 'xlsx'} onClick={() => void downloadExport('xlsx')}>
                {t('batch.btn_export_xlsx')}
              </Button>
              <Button size="small" loading={exporting === 'csv'} onClick={() => void downloadExport('csv')}>
                {t('batch.btn_export_csv')}
              </Button>
              <Button
                size="small"
                icon={<NodeExpandOutlined aria-hidden="true" />}
                onClick={() => {
                  window.location.hash = `/trace?run=${encodeURIComponent(selected)}`
                }}
              >
                {t('results.open_trace')}
              </Button>
            </div>
          </div>
        )}

        {cellsLoading ? (
          <div style={{ display: 'flex', justifyContent: 'center', padding: '60px 0' }}>
            <Spin size="large" />
          </div>
        ) : cellError ? (
          <ErrorState title={t('common.request_failed')} detail={cellError} onRetry={() => selected && openRun(selected)} />
        ) : cells.length === 0 ? (
          <EmptyState title={t('runs.no_cell_data')} />
        ) : (
          <div className="drawer-grid">
            <AgGridReact
              theme={agTheme}
              rowHeight={72}
              rowData={cells}
              columnDefs={colDefs}
              context={gridContext}
              defaultColDef={{ sortable: true, filter: true, resizable: true }}
            />
          </div>
        )}
        <Modal open={!!expandedText} title={expandedText?.title} footer={null} onCancel={() => setExpandedText(null)}>
          <div className="runs-expanded-text">{expandedText?.value}</div>
        </Modal>
      </Drawer>
    </div>
  )
}

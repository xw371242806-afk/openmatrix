import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Button, Input } from 'antd'
import { useTranslation } from 'react-i18next'
import {
  CheckCircleFilled,
  CloseCircleFilled,
  EyeOutlined,
  NodeIndexOutlined,
  StopOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import PageHeader from './components/PageHeader'
import EmptyState from './components/EmptyState'
import ErrorState from './components/ErrorState'
import { CellStatusTag } from './components/StatusTag'
import type { CellEvent } from './api/types'
import { getAccessToken } from './api/client'
import { hashSearchParams, normalizeRoute, type RouteKey } from './lib/hashRouter'
import './trace.css'

interface LedgerRow {
  key: string
  cell_id: string
  question: string
  status: string
  document_index?: number
  question_index?: number
  observed: string
}

function clientClock(ms: number): string {
  const d = new Date(ms)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function runIdFromHash(hash = window.location.hash): string {
  return hashSearchParams(hash).get('run')?.trim() ?? ''
}

export default function TracePage({ navigate: _navigate }: { navigate?: (key: RouteKey) => void }) {
  const { t } = useTranslation()
  const [runId, setRunId] = useState('')
  const [connected, setConnected] = useState(false)
  const [streamFailed, setStreamFailed] = useState(false)
  const [rows, setRows] = useState<LedgerRow[]>([])
  const [complete, setComplete] = useState(false)
  const [completedRunId, setCompletedRunId] = useState('')
  const [firstAt, setFirstAt] = useState<number | null>(null)
  const [lastAt, setLastAt] = useState<number | null>(null)
  const sourceRef = useRef<{ close: () => void } | null>(null)
  const activeRunRef = useRef('')
  const rowKeyRef = useRef(0)
  const subscriptionGenerationRef = useRef(0)

  const stop = useCallback(() => {
    subscriptionGenerationRef.current += 1
    if (sourceRef.current) {
      sourceRef.current.close()
      sourceRef.current = null
    }
    setConnected(false)
  }, [])

  const subscribe = useCallback(
    (explicit?: string) => {
      const id = (explicit ?? '').trim()
      if (!id) return
      stop()
      const generation = subscriptionGenerationRef.current
      activeRunRef.current = id
      setRows([])
      setComplete(false)
      setCompletedRunId('')
      setStreamFailed(false)
      setFirstAt(null)
      setLastAt(null)

      const isCurrent = () => (
        activeRunRef.current === id
        && subscriptionGenerationRef.current === generation
      )
      const onCell = (raw: string) => {
        // request-id guard: ignore cells from a stale subscription
        if (!isCurrent()) return
        let data: CellEvent | null = null
        try {
          data = JSON.parse(raw) as CellEvent
        } catch {
          return // malformed event — skip rather than crash the stream
        }
        if (!data || typeof data !== 'object') return
        const cellId = typeof data.cell_id === 'string' ? data.cell_id : ''
        const now = Date.now()
        setFirstAt((prev) => prev ?? now)
        setLastAt(now)
        setRows((prev) => {
          if (cellId && prev.some((r) => r.cell_id === cellId)) return prev
          rowKeyRef.current += 1
          const row: LedgerRow = {
            key: cellId || `evt-${rowKeyRef.current}`,
            cell_id: cellId,
            question: typeof data.question === 'string' ? data.question : '',
            status: typeof data.status === 'string' ? data.status : '',
            document_index: typeof data.document_index === 'number' ? data.document_index : undefined,
            question_index: typeof data.question_index === 'number' ? data.question_index : undefined,
            observed: clientClock(now),
          }
          return [row, ...prev]
        })
      }
      const onComplete = () => {
        if (!isCurrent()) return
        setCompletedRunId(id)
        setComplete(true)
        stop()
      }
      const onError = () => {
        if (!isCurrent()) return
        setStreamFailed(true)
        stop()
      }

      const token = getAccessToken()
      if (token) {
        const controller = new AbortController()
        sourceRef.current = { close: () => controller.abort() }
        void fetch(`/api/runs/${encodeURIComponent(id)}/events`, {
          headers: { Authorization: `Bearer ${token}` },
          signal: controller.signal,
        }).then(async (response) => {
          if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`)
          const reader = response.body.getReader()
          const decoder = new TextDecoder()
          let buffer = ''
          while (isCurrent()) {
            const { done, value } = await reader.read()
            buffer += decoder.decode(value, { stream: !done })
            const frames = buffer.split('\n\n')
            buffer = frames.pop() ?? ''
            for (const frame of frames) {
              const event = frame.match(/^event:\s*(.+)$/m)?.[1]
              const data = frame.match(/^data:\s*(.+)$/m)?.[1] ?? ''
              if (event === 'cell') onCell(data)
              else if (event === 'run_complete') onComplete()
              else if (event === 'error') onError()
            }
            if (done) break
          }
        }).catch((error: unknown) => {
          if (error instanceof DOMException && error.name === 'AbortError') return
          onError()
        })
      } else {
        let source: EventSource
        try {
          source = new EventSource(`/api/runs/${encodeURIComponent(id)}/events`)
        } catch {
          setStreamFailed(true)
          return
        }
        sourceRef.current = source
        source.addEventListener('cell', (ev: MessageEvent) => onCell(String(ev.data)))
        source.addEventListener('run_complete', onComplete)
        source.addEventListener('error', onError)
      }
      setConnected(true)
    },
    [stop],
  )

  useEffect(() => {
    const syncFromHash = () => {
      if (normalizeRoute(window.location.hash) !== 'trace') return
      const id = runIdFromHash()
      setRunId(id)
      if (!id) {
        activeRunRef.current = ''
        stop()
        return
      }
      if (activeRunRef.current !== id) subscribe(id)
    }

    syncFromHash()
    window.addEventListener('hashchange', syncFromHash)
    return () => {
      window.removeEventListener('hashchange', syncFromHash)
      stop()
    }
  }, [subscribe, stop])

  const watch = () => {
    if (connected) {
      stop()
      return
    }
    subscribe(runId)
  }

  const counts = useMemo(() => {
    const tally = { completed: 0, needs_review: 0, failed: 0, other: 0 }
    for (const row of rows) {
      if (row.status === 'completed') tally.completed += 1
      else if (row.status === 'needs_review') tally.needs_review += 1
      else if (row.status === 'failed') tally.failed += 1
      else tally.other += 1
    }
    return tally
  }, [rows])

  return (
    <div className="trace-page" data-testid="trace-page">
      <PageHeader title={t('trace.title')} description={t('trace.matrix_scope_note')} />

      <section className="desk-card trace-conn">
        <div className="trace-conn-row">
          <Input
            className="trace-conn-input"
            placeholder={t('trace.run_id_placeholder')}
            value={runId}
            onChange={(e) => setRunId(e.target.value)}
            onPressEnter={() => subscribe(runId)}
            aria-label={t('trace.run_id_placeholder')}
            allowClear
          />
          <Button
            className="trace-conn-button"
            type="primary"
            onClick={watch}
            disabled={!runId.trim()}
            icon={connected ? <StopOutlined aria-hidden="true" /> : <EyeOutlined aria-hidden="true" />}
          >
            {connected ? t('trace.stop') : t('trace.watch')}
          </Button>
        </div>
        <div className="trace-conn-status" role="status" aria-live="polite">
          {connected && (
            <span className="trace-conn-live">
              <SyncOutlined className="spin-icon" aria-hidden="true" />
              <span>{t('trace.streaming')}</span>
            </span>
          )}
          {streamFailed && (
            <span className="trace-conn-failed">
              <CloseCircleFilled aria-hidden="true" />
              <span>{t('trace.stream_error')}</span>
            </span>
          )}
        </div>
      </section>

      {rows.length > 0 && (
        <section className="desk-card" aria-label={t('trace.summary_title')}>
          <h3 className="section-label">{t('trace.summary_title')}</h3>
          <div className="trace-summary-row">
            <span className="trace-chip trace-chip--verified">
              <span className="trace-chip-value mono">{counts.completed}</span>
              {t('status.cell.completed')}
            </span>
            <span className="trace-chip trace-chip--review">
              <span className="trace-chip-value mono">{counts.needs_review}</span>
              {t('status.cell.needs_review')}
            </span>
            <span className="trace-chip trace-chip--failed">
              <span className="trace-chip-value mono">{counts.failed}</span>
              {t('status.cell.failed')}
            </span>
            <span className="trace-chip trace-chip--neutral">
              <span className="trace-chip-value mono">{counts.other}</span>
              {t('trace.count_other')}
            </span>
            <span className="trace-chip trace-chip--total">
              <span className="trace-chip-value mono">{rows.length}</span>
              {t('trace.count_total')}
            </span>
          </div>
          {firstAt !== null && (
            <div className="trace-observed">
              <span>{t('trace.observed_first')}</span>
              <span className="mono">{t('trace.observed_at', { time: clientClock(firstAt) })}</span>
              {lastAt !== null && (
                <>
                  <span>{t('trace.observed_last')}</span>
                  <span className="mono">{t('trace.observed_at', { time: clientClock(lastAt) })}</span>
                </>
              )}
            </div>
          )}
        </section>
      )}

      <section className="desk-card trace-ledger" aria-label={t('trace.ledger_title')}>
        <h3 className="section-label">{t('trace.ledger_title')}</h3>
        {complete && (
          <div className="trace-banner trace-banner--verified" role="status">
            <CheckCircleFilled aria-hidden="true" />
            <span>{t('trace.run_complete', { id: completedRunId })}</span>
          </div>
        )}
        {streamFailed && (
          <ErrorState
            title={t('trace.stream_error')}
            onRetry={() => subscribe(activeRunRef.current || runId)}
          />
        )}
        {rows.length === 0 && !streamFailed ? (
          <EmptyState
            icon={<NodeIndexOutlined />}
            title={t('trace.ledger_empty_title')}
            hint={t('trace.empty')}
          />
        ) : (
          <ul className="trace-rows">
            {rows.map((row) => (
              <li key={row.key} className="trace-row">
                <span className="mono trace-row-time">{row.observed}</span>
                <span className="trace-row-question">{row.question || row.cell_id}</span>
                <span className="mono trace-row-ids">
                  {row.document_index !== undefined && (
                    <span>{t('trace.doc_short', { index: row.document_index })}</span>
                  )}
                  {row.question_index !== undefined && (
                    <span>{t('trace.q_short', { index: row.question_index })}</span>
                  )}
                  {row.cell_id && <span>{row.cell_id}</span>}
                </span>
                <CellStatusTag value={row.status} />
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}

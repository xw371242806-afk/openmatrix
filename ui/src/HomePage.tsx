import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button, Spin, Tag } from 'antd'
import {
  CheckCircleFilled,
  DatabaseOutlined,
  FolderOpenOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  RightOutlined,
  RobotOutlined,
  ThunderboltOutlined,
  WarningFilled,
} from '@ant-design/icons'
import type { ReactNode } from 'react'
import PageHeader from './components/PageHeader'
import StatTile from './components/StatTile'
import EmptyState from './components/EmptyState'
import ErrorState from './components/ErrorState'
import { CellStatusTag } from './components/StatusTag'
import { getJson } from './api/client'
import type { ProjectRecord, RunCounts, ScheduleEntry } from './api/types'
import type { RouteKey } from './lib/hashRouter'
import './home.css'

/* ── Section state ──────────────────────────────────────────
   Every section owns its loading / error / ready state so a
   single failing endpoint never blocks or falsifies the rest. */

type SectionState<T> =
  | { status: 'loading' }
  | { status: 'error'; detail: string }
  | { status: 'ready'; items: T[] }

type TileKey = 'cells' | 'verification' | 'support' | 'projects' | 'schedules'

type TileState = { status: 'loading' | 'ok' | 'error'; text: string | null }

type TileMap = Record<TileKey, TileState>

const INITIAL_TILES: TileMap = {
  cells: { status: 'loading', text: null },
  verification: { status: 'loading', text: null },
  support: { status: 'loading', text: null },
  projects: { status: 'loading', text: null },
  schedules: { status: 'loading', text: null },
}

const QUICK_ACTIONS: { key: string; route: RouteKey; icon: ReactNode }[] = [
  { key: 'overview.quick_new_analysis', route: 'analysis', icon: <ThunderboltOutlined /> },
  { key: 'overview.quick_open_projects', route: 'projects', icon: <FolderOpenOutlined /> },
  { key: 'overview.quick_ask_agent', route: 'agent', icon: <RobotOutlined /> },
  { key: 'overview.quick_review_results', route: 'results', icon: <DatabaseOutlined /> },
]

function errorDetail(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

/** The stats endpoint historically returned `rate`/`total`; render the
   verification figure as a percent without inventing precision. */
function toPercentString(raw: unknown): string | null {
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    const pct = raw >= 0 && raw <= 1 ? raw * 100 : raw
    return `${Math.round(pct)}%`
  }
  if (typeof raw === 'string' && raw.trim()) {
    const s = raw.trim()
    if (s.endsWith('%') && Number.isFinite(Number.parseFloat(s))) return s
    const n = Number(s)
    if (Number.isFinite(n)) {
      const pct = n >= 0 && n <= 1 ? n * 100 : n
      return `${Math.round(pct)}%`
    }
  }
  return null
}

function formatDate(value: string | null | undefined, language: string): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(language)
}

function formatDateTime(value: string | null | undefined, language: string): string | null {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(language)
}

function progressPercent(completed: number, total: number): number {
  if (total <= 0) return 0
  return Math.max(0, Math.min(100, Math.round((completed / total) * 100)))
}

export default function HomePage({ navigate }: { navigate: (key: RouteKey) => void }) {
  const { t, i18n } = useTranslation()
  const [tiles, setTiles] = useState<TileMap>(INITIAL_TILES)
  const [runs, setRuns] = useState<SectionState<RunCounts>>({ status: 'loading' })
  const [projects, setProjects] = useState<SectionState<ProjectRecord>>({ status: 'loading' })
  const [schedules, setSchedules] = useState<SectionState<ScheduleEntry>>({ status: 'loading' })
  const [schedulePersistent, setSchedulePersistent] = useState(true)

  // Request-token refs: discard stale responses after a manual refresh.
  const runsReqRef = useRef(0)
  const projectsReqRef = useRef(0)
  const schedulesReqRef = useRef(0)

  const setTile = useCallback((key: TileKey, next: TileState) => {
    setTiles((prev) => ({ ...prev, [key]: next }))
  }, [])

  const loadCellStat = useCallback(async () => {
    setTile('cells', { status: 'loading', text: null })
    try {
      const data = await getJson<{ total?: unknown; total_cells?: unknown }>('/api/v1/stats/total-cells')
      const raw = data.total_cells ?? data.total
      const n = typeof raw === 'number' ? raw : Number(raw)
      if (raw == null || !Number.isFinite(n)) throw new Error('unexpected payload')
      setTile('cells', { status: 'ok', text: String(n) })
    } catch {
      setTile('cells', { status: 'error', text: null })
    }
  }, [setTile])

  const loadVerificationStat = useCallback(async () => {
    setTile('verification', { status: 'loading', text: null })
    try {
      const data = await getJson<{ verification_rate?: unknown; rate?: unknown }>(
        '/api/v1/stats/citation-verification-rate',
      )
      const text = toPercentString(data.verification_rate ?? data.rate)
      if (text === null) throw new Error('unexpected payload')
      setTile('verification', { status: 'ok', text })
    } catch {
      setTile('verification', { status: 'error', text: null })
    }
  }, [setTile])

  const loadSupportStat = useCallback(async () => {
    setTile('support', { status: 'loading', text: null })
    try {
      const data = await getJson<{ rate?: unknown }>('/api/v1/stats/answer-support-rate')
      const text = toPercentString(data.rate)
      if (text === null) throw new Error('unexpected payload')
      setTile('support', { status: 'ok', text })
    } catch {
      setTile('support', { status: 'error', text: null })
    }
  }, [setTile])

  const loadProjectCount = useCallback(async () => {
    setTile('projects', { status: 'loading', text: null })
    try {
      const data = await getJson<{ projects?: unknown }>('/api/v1/projects')
      if (!Array.isArray(data.projects)) throw new Error('unexpected payload')
      setTile('projects', { status: 'ok', text: String(data.projects.length) })
    } catch {
      setTile('projects', { status: 'error', text: null })
    }
  }, [setTile])

  const loadScheduleCount = useCallback(async () => {
    setTile('schedules', { status: 'loading', text: null })
    try {
      const data = await getJson<{ schedules?: unknown; persistent?: boolean }>('/api/v1/schedules')
      if (!Array.isArray(data.schedules)) throw new Error('unexpected payload')
      setSchedulePersistent(data.persistent === true)
      setTile('schedules', { status: 'ok', text: String(data.schedules.length) })
    } catch {
      setTile('schedules', { status: 'error', text: null })
    }
  }, [setTile])

  const loadRuns = useCallback(async () => {
    const reqId = ++runsReqRef.current
    setRuns({ status: 'loading' })
    try {
      const data = await getJson<{ runs?: unknown }>('/api/v1/runs?limit=20')
      const ids = Array.isArray(data.runs)
        ? data.runs.filter((id): id is string => typeof id === 'string')
        : []
      if (ids.length === 0) {
        if (reqId === runsReqRef.current) setRuns({ status: 'ready', items: [] })
        return
      }
      const settled = await Promise.allSettled(
        ids.map((id) => getJson<RunCounts>(`/api/v1/runs/${encodeURIComponent(id)}`)),
      )
      if (reqId !== runsReqRef.current) return
      const items = settled.flatMap((result, i) =>
        result.status === 'fulfilled'
          ? [{ ...result.value, run_id: result.value.run_id || ids[i] }]
          : [],
      )
      setRuns({ status: 'ready', items })
    } catch (e) {
      if (reqId === runsReqRef.current) setRuns({ status: 'error', detail: errorDetail(e) })
    }
  }, [])

  const loadProjects = useCallback(async () => {
    const reqId = ++projectsReqRef.current
    setProjects({ status: 'loading' })
    try {
      const data = await getJson<{ projects?: unknown }>('/api/v1/projects')
      if (!Array.isArray(data.projects)) throw new Error('unexpected payload')
      if (reqId === projectsReqRef.current) setProjects({ status: 'ready', items: data.projects })
    } catch (e) {
      if (reqId === projectsReqRef.current) setProjects({ status: 'error', detail: errorDetail(e) })
    }
  }, [])

  const loadSchedules = useCallback(async () => {
    const reqId = ++schedulesReqRef.current
    setSchedules({ status: 'loading' })
    try {
      const data = await getJson<{ schedules?: unknown; persistent?: boolean }>('/api/v1/schedules')
      if (!Array.isArray(data.schedules)) throw new Error('unexpected payload')
      setSchedulePersistent(data.persistent === true)
      if (reqId === schedulesReqRef.current) setSchedules({ status: 'ready', items: data.schedules })
    } catch (e) {
      if (reqId === schedulesReqRef.current) setSchedules({ status: 'error', detail: errorDetail(e) })
    }
  }, [])

  const reloadAll = useCallback(() => {
    void loadCellStat()
    void loadVerificationStat()
    void loadSupportStat()
    void loadProjectCount()
    void loadScheduleCount()
    void loadRuns()
    void loadProjects()
    void loadSchedules()
  }, [loadCellStat, loadVerificationStat, loadSupportStat, loadProjectCount, loadScheduleCount, loadRuns, loadProjects, loadSchedules])

  useEffect(() => {
    void reloadAll()
  }, [reloadAll])

  const runsReady = runs.status === 'ready' ? runs.items : []
  const attentionRuns = runsReady.filter((r) => r.needs_review > 0 || r.failed > 0)

  const renderRunRow = (run: RunCounts, compact: boolean) => (
    <button
      key={run.run_id}
      type="button"
      className={compact ? 'ov-run-row ov-run-row--compact' : 'ov-run-row'}
      onClick={() => navigate('results')}
    >
      <span className="mono ov-run-id" title={run.run_id}>{run.run_id}</span>
      <span className="ov-run-progress">
        <span className="ov-run-progress-text">
          {t('overview.run_progress', { done: run.completed, total: run.total })}
        </span>
        {!compact && (
          <span className="ov-progress" aria-hidden="true">
            <span
              className="ov-progress-fill"
              style={{ width: `${progressPercent(run.completed, run.total)}%` }}
            />
          </span>
        )}
      </span>
      <span className="ov-run-tags">
        {run.needs_review > 0 && (
          <>
            <CellStatusTag value="needs_review" />
            <span className="mono ov-tag-count">{run.needs_review}</span>
          </>
        )}
        {run.failed > 0 && (
          <>
            <CellStatusTag value="failed" />
            <span className="mono ov-tag-count">{run.failed}</span>
          </>
        )}
      </span>
      {!compact && <RightOutlined className="ov-row-chevron" aria-hidden="true" />}
    </button>
  )

  return (
    <div className="overview-page">
      <PageHeader
        title={t('overview.title')}
        description={t('overview.subtitle')}
        actions={
          <Button icon={<ReloadOutlined aria-hidden="true" />} onClick={reloadAll}>
            {t('common.refresh')}
          </Button>
        }
      />

      <nav className="ov-quick" aria-label={t('overview.quick_aria')}>
        {QUICK_ACTIONS.map((action) => (
          <button
            key={action.key}
            type="button"
            className="ov-quick-item"
            onClick={() => navigate(action.route)}
          >
            <span className="ov-quick-icon" aria-hidden="true">{action.icon}</span>
            <span className="ov-quick-label">{t(action.key)}</span>
          </button>
        ))}
      </nav>

      <div className="ov-stats">
        <StatTile
          label={t('overview.stat_cells')}
          value={tiles.cells.status === 'ok' ? tiles.cells.text : '-'}
          hint={tiles.cells.status === 'error' ? t('overview.stat_unavailable') : t('overview.stat_cells_hint')}
          loading={tiles.cells.status === 'loading'}
        />
        <StatTile
          label={t('overview.stat_verification')}
          value={tiles.verification.status === 'ok' ? tiles.verification.text : '-'}
          hint={tiles.verification.status === 'error' ? t('overview.stat_unavailable') : t('overview.stat_verification_hint')}
          loading={tiles.verification.status === 'loading'}
        />
        <StatTile label={t('overview.stat_support')} value={tiles.support.status === 'ok' ? tiles.support.text : '-'} hint={tiles.support.status === 'error' ? t('overview.stat_unavailable') : t('overview.stat_support_hint')} loading={tiles.support.status === 'loading'} />
        <StatTile
          label={t('overview.stat_projects')}
          value={tiles.projects.status === 'ok' ? tiles.projects.text : '-'}
          hint={tiles.projects.status === 'error' ? t('overview.stat_unavailable') : t('overview.stat_projects_hint')}
          loading={tiles.projects.status === 'loading'}
        />
        <StatTile
          label={t('overview.stat_schedules')}
          value={tiles.schedules.status === 'ok' ? tiles.schedules.text : '-'}
          hint={tiles.schedules.status === 'error' ? t('overview.stat_unavailable') : t('overview.stat_schedules_hint')}
          loading={tiles.schedules.status === 'loading'}
        />
      </div>

      <section className="desk-card" aria-label={t('overview.attention_title')}>
        <h3 className="section-label">{t('overview.attention_title')}</h3>
        {runs.status === 'loading' && (
          <div className="ov-loading"><Spin /></div>
        )}
        {runs.status === 'error' && (
          <ErrorState title={t('overview.runs_failed')} detail={runs.detail} onRetry={loadRuns} />
        )}
        {runs.status === 'ready' && runs.items.length === 0 && (
          <EmptyState
            icon={<DatabaseOutlined />}
            title={t('overview.runs_empty')}
            hint={t('overview.runs_empty_hint')}
          />
        )}
        {runs.status === 'ready' && runs.items.length > 0 && (
          <>
            {attentionRuns.length === 0 ? (
              <p className="ov-attention-ok">
                <CheckCircleFilled aria-hidden="true" /> {t('overview.attention_none')}
              </p>
            ) : (
              <div className="ov-run-list">{attentionRuns.map((r) => renderRunRow(r, false))}</div>
            )}
            <div className="ov-recent-head">
              <h4 className="section-label">{t('overview.recent_runs_title')}</h4>
              <span className="ov-recent-note">
                {t('overview.recent_runs_note', { count: runs.items.length })}
              </span>
            </div>
            <div className="ov-run-list">{runs.items.map((r) => renderRunRow(r, true))}</div>
          </>
        )}
      </section>

      <div className="ov-columns">
        <section className="desk-card" aria-label={t('overview.projects_title')}>
          <h3 className="section-label">{t('overview.projects_title')}</h3>
          {projects.status === 'loading' && <div className="ov-loading"><Spin /></div>}
          {projects.status === 'error' && (
            <ErrorState title={t('overview.projects_failed')} detail={projects.detail} onRetry={loadProjects} />
          )}
          {projects.status === 'ready' && projects.items.length === 0 && (
            <EmptyState
              icon={<FolderOpenOutlined />}
              title={t('overview.projects_empty')}
              hint={t('overview.projects_empty_hint')}
            />
          )}
          {projects.status === 'ready' && projects.items.length > 0 && (
            <div className="ov-project-list">
              {projects.items.slice(0, 6).map((p) => (
                <button
                  key={p.id}
                  type="button"
                  className="ov-project-row"
                  onClick={() => navigate('projects')}
                >
                  <span className="ov-project-name">{p.name}</span>
                  {p.description && <span className="ov-project-desc">{p.description}</span>}
                  <span className="mono ov-project-date">{formatDate(p.created_at, i18n.language)}</span>
                </button>
              ))}
            </div>
          )}
        </section>

        <section className="desk-card" aria-label={t('overview.schedules_title')}>
          <h3 className="section-label">{t('overview.schedules_title')}</h3>
          {!schedulePersistent && <p className="ov-schedules-note">
            <WarningFilled aria-hidden="true" /> {t('overview.schedules_note')}
          </p>}
          {schedules.status === 'loading' && <div className="ov-loading"><Spin /></div>}
          {schedules.status === 'error' && (
            <ErrorState title={t('overview.schedules_failed')} detail={schedules.detail} onRetry={loadSchedules} />
          )}
          {schedules.status === 'ready' && schedules.items.length === 0 && (
            <EmptyState
              icon={<PauseCircleOutlined />}
              title={t('overview.schedules_empty')}
              hint={t('overview.schedules_empty_hint')}
            />
          )}
          {schedules.status === 'ready' && schedules.items.length > 0 && (
            <div className="ov-schedule-list">
              {schedules.items.map((s) => (
                <div key={s.schedule_id} className="ov-schedule-row">
                  <div className="ov-schedule-head">
                    <span className="mono ov-schedule-id" title={s.schedule_id}>{s.schedule_id}</span>
                    {s.paused ? (
                      <Tag className="signal-tag signal-neutral" icon={<PauseCircleOutlined aria-hidden="true" />}>
                        {t('overview.schedules_paused')}
                      </Tag>
                    ) : (
                      <Tag className="signal-tag signal-info" icon={<PlayCircleOutlined aria-hidden="true" />}>
                        {t('overview.schedules_active')}
                      </Tag>
                    )}
                  </div>
                  {s.objective && <p className="ov-schedule-objective">{s.objective}</p>}
                  <div className="ov-schedule-meta">
                    <span className="mono">{t('overview.schedules_cron')} · {s.cron}</span>
                    {s.next_due && (
                      <span>{t('overview.schedules_next_due')} · {formatDateTime(s.next_due, i18n.language)}</span>
                    )}
                  </div>
                  {s.last_error && (
                    <p className="ov-schedule-error">
                      {t('overview.schedules_last_error')} · {s.last_error}
                    </p>
                  )}
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

import { useCallback, useEffect, useRef, useState } from 'react'
import { Sender } from '@ant-design/x'
import { Button, Collapse, Form, Input, Popconfirm, Spin, Tag, Typography, message } from 'antd'
import { useTranslation } from 'react-i18next'
import {
  CheckCircleFilled,
  CloseCircleFilled,
  CloudUploadOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  FileSearchOutlined,
  InfoCircleFilled,
  MinusCircleOutlined,
  NodeIndexOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  ReloadOutlined,
  RobotOutlined,
  ScheduleOutlined,
  StopOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import BrandMark from './components/BrandMark'
import EmptyState from './components/EmptyState'
import ErrorState from './components/ErrorState'
import Unavailable from './components/Unavailable'
import { PhaseTag, StepStatusTag } from './components/StatusTag'
import { deleteJson, getJson, postJson, ApiError } from './api/client'
import type { ResearchStartResponse, ResearchSummary, ScheduleEntry } from './api/types'
import type { RouteKey } from './lib/hashRouter'
import './chat.css'

interface ScheduleFormValues {
  objective: string
  cron: string
  research_id: string
  context?: string
}

/* Phases after which no further polling or cancellation is offered. */
const TERMINAL_PHASES = ['completed', 'failed', 'cancelled']

/* Only a capped slice of sessions is fetched for the side panel. */
const SESSIONS_CAP = 10

function errorDetail(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

function isContextObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function formatStamp(value: string | null | undefined, language: string): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(language)
}

function generateResearchId(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID()
    }
  } catch {
    // fall through to the manual hex fallback below
  }
  const rand = () => Math.floor(Math.random() * 0xffff).toString(16).padStart(4, '0')
  return `r-${Date.now().toString(16)}-${rand()}${rand()}`
}

function conclusionText(value: unknown): string {
  if (typeof value === 'string') return value
  if (!value || typeof value !== 'object') return ''
  const record = value as Record<string, unknown>
  for (const key of ['summary', 'answer', 'text']) {
    if (typeof record[key] === 'string') return record[key] as string
  }
  if (record.outputs && typeof record.outputs === 'object') {
    for (const output of Object.values(record.outputs).reverse()) {
      let parsed: unknown = output
      if (typeof output === 'string') {
        try { parsed = JSON.parse(output) } catch { return output }
      }
      if (parsed && typeof parsed === 'object') {
        const content = parsed as Record<string, unknown>
        for (const key of ['summary', 'answer', 'text']) {
          if (typeof content[key] === 'string') return content[key] as string
        }
      }
    }
  }
  return JSON.stringify(value)
}

export default function ChatPage({ navigate }: { navigate?: (key: RouteKey) => void }) {
  const { t, i18n } = useTranslation()

  /* ── Sessions side panel ── */
  const [sessionIds, setSessionIds] = useState<string[]>([])
  const [sessions, setSessions] = useState<ResearchSummary[]>([])
  const [sessionsLoading, setSessionsLoading] = useState(true)
  const [sessionsError, setSessionsError] = useState<string | null>(null)

  /* ── Selected session (main column) ── */
  const [session, setSession] = useState<ResearchSummary | null>(null)
  const [starting, setStarting] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [draft, setDraft] = useState('')
  const [contextDraft, setContextDraft] = useState('')
  const [statusUnavailable, setStatusUnavailable] = useState(false)

  /* ── Schedules side panel ── */
  const [schedules, setSchedules] = useState<ScheduleEntry[]>([])
  const [schedulePersistent, setSchedulePersistent] = useState(true)
  const [schedulesLoading, setSchedulesLoading] = useState(true)
  const [schedulesError, setSchedulesError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [creating, setCreating] = useState(false)

  const [scheduleForm] = Form.useForm<ScheduleFormValues>()

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const currentIdRef = useRef('')
  const pollGenerationRef = useRef(0)
  const sessionsRequestRef = useRef(0)
  const schedulesRequestRef = useRef(0)

  const stopPolling = useCallback(() => {
    pollGenerationRef.current += 1
    if (intervalRef.current) {
      clearInterval(intervalRef.current)
      intervalRef.current = null
    }
  }, [])

  const loadSessions = useCallback(async () => {
    const requestId = ++sessionsRequestRef.current
    setSessionsLoading(true)
    setSessionsError(null)
    try {
      const data = await getJson<{ research?: unknown }>('/api/v1/research')
      if (!Array.isArray(data.research)) throw new Error('unexpected payload')
      const ids = data.research.filter((x): x is string => typeof x === 'string')
      // The list endpoint returns ids only; objectives and phases come from
      // the per-id detail endpoint, fetched in parallel and best-effort.
      const capped = ids.slice(0, SESSIONS_CAP)
      const settled = await Promise.allSettled(
        capped.map((id) => getJson<ResearchSummary>(`/api/v1/research/${encodeURIComponent(id)}`)),
      )
      const details = settled
        .map((r) => (r.status === 'fulfilled' ? r.value : null))
        .filter((x): x is ResearchSummary => x !== null)
      if (requestId !== sessionsRequestRef.current) return
      setSessionIds(ids)
      setSessions(details)
      setSessionsLoading(false)
    } catch (e) {
      if (requestId !== sessionsRequestRef.current) return
      setSessionsError(errorDetail(e))
      setSessionsLoading(false)
    }
  }, [])

  const loadSchedules = useCallback(async () => {
    const requestId = ++schedulesRequestRef.current
    setSchedulesLoading(true)
    setSchedulesError(null)
    try {
      const data = await getJson<{ schedules?: unknown; persistent?: boolean }>('/api/v1/schedules')
      if (!Array.isArray(data.schedules)) throw new Error('unexpected payload')
      setSchedulePersistent(data.persistent === true)
      if (requestId !== schedulesRequestRef.current) return
      setSchedules(data.schedules as ScheduleEntry[])
      setSchedulesLoading(false)
    } catch (e) {
      if (requestId !== schedulesRequestRef.current) return
      setSchedulesError(errorDetail(e))
      setSchedulesLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadSessions()
    void loadSchedules()
    return stopPolling
  }, [loadSessions, loadSchedules, stopPolling])

  const startPolling = (id: string) => {
    stopPolling() // never let a previous session's poll overwrite this one
    const generation = pollGenerationRef.current
    intervalRef.current = setInterval(async () => {
      if (currentIdRef.current !== id || pollGenerationRef.current !== generation) return
      try {
        const data = await getJson<ResearchSummary>(`/api/v1/research/${encodeURIComponent(id)}`)
        if (currentIdRef.current !== id || pollGenerationRef.current !== generation) return
        setSession(data)
        if (TERMINAL_PHASES.includes(data.phase)) stopPolling()
      } catch (e) {
        if (currentIdRef.current !== id || pollGenerationRef.current !== generation) return
        if (e instanceof ApiError && e.status === 404) {
          setStatusUnavailable(true)
          stopPolling()
          return
        }
        message.error(t('chat.poll_failed', { message: errorDetail(e) }))
        stopPolling()
      }
    }, 2000)
  }

  const selectSession = async (id: string) => {
    stopPolling()
    currentIdRef.current = id
    setStarting(false)
    setCancelling(false)
    setStatusUnavailable(false)
    const cached = sessions.find((item) => item.research_id === id)
    if (cached) setSession(cached)
    try {
      const detail = await getJson<ResearchSummary>(`/api/v1/research/${encodeURIComponent(id)}`)
      if (currentIdRef.current !== id) return
      setSession(detail)
      if (!TERMINAL_PHASES.includes(detail.phase)) startPolling(id)
    } catch (e) {
      if (currentIdRef.current !== id) return
      if (e instanceof ApiError && e.status === 404) {
        setStatusUnavailable(true)
        return
      }
      message.error(t('agent.session_load_failed', { message: errorDetail(e) }))
    }
  }

  const startResearch = async (text: string, context?: unknown) => {
    const id = generateResearchId()
    currentIdRef.current = id
    stopPolling()
    setStatusUnavailable(false)
    setSession({ research_id: id, objective: text, phase: '', steps: [] })
    setStarting(true)
    setCancelling(false)
    try {
      const body: Record<string, unknown> = { research_id: id, objective: text }
      if (context !== undefined) body.context = context
      const data = await postJson<ResearchStartResponse>('/api/v1/research', body)
      if (currentIdRef.current !== id) return
      setSession((prev) => (prev && prev.research_id === id ? { ...prev, phase: data.phase } : prev))
      startPolling(id)
      void loadSessions()
    } catch (e) {
      if (currentIdRef.current === id) {
        message.error(t('chat.start_failed', { message: errorDetail(e) }))
        setSession(null)
      }
    } finally {
      if (currentIdRef.current === id) setStarting(false)
    }
  }

  const handleSend = (text: string) => {
    const trimmed = (text || '').trim()
    if (!trimmed) return
    if (starting || runInProgress) {
      message.warning(t('chat.already_running'))
      return
    }
    const rawContext = contextDraft.trim()
    let context: unknown
    if (rawContext) {
      try {
        context = JSON.parse(rawContext)
      } catch {
        message.error(t('agent.form_context_invalid'))
        return
      }
      if (!isContextObject(context)) {
        message.error(t('agent.form_context_invalid'))
        return
      }
    }
    setDraft('')
    void startResearch(trimmed, context)
  }

  const cancelResearch = async () => {
    const id = currentIdRef.current
    if (!id || !session || TERMINAL_PHASES.includes(session.phase)) return
    setCancelling(true)
    try {
      const data = await deleteJson<{ cancelled?: boolean }>(`/api/v1/research/${encodeURIComponent(id)}`)
      if (data.cancelled && currentIdRef.current === id) {
        stopPolling()
        setSession((prev) => (prev && prev.research_id === id ? { ...prev, phase: 'cancelled' } : prev))
      }
    } catch (e) {
      message.error(t('chat.cancel_failed', { message: errorDetail(e) }))
    } finally {
      if (currentIdRef.current === id) setCancelling(false)
    }
  }

  const removeSchedule = async (id: string) => {
    try {
      await deleteJson(`/api/v1/schedules/${encodeURIComponent(id)}`)
      message.success(t('agent.schedule_delete_success'))
      void loadSchedules()
    } catch (e) {
      message.error(t('agent.schedule_delete_failed', { message: errorDetail(e) }))
    }
  }

  const submitSchedule = async (values: ScheduleFormValues) => {
    const rawContext = (values.context ?? '').trim()
    let context: unknown
    if (rawContext) {
      try {
        context = JSON.parse(rawContext)
      } catch {
        message.error(t('agent.form_context_invalid'))
        return
      }
      if (!isContextObject(context)) {
        message.error(t('agent.form_context_invalid'))
        return
      }
    }
    setCreating(true)
    try {
      const body: Record<string, unknown> = {
        cron: values.cron.trim(),
        objective: values.objective.trim(),
        research_id: values.research_id.trim(),
      }
      if (rawContext) body.context = context
      await postJson('/api/v1/schedules', body)
      message.success(t('agent.schedules_create_success'))
      scheduleForm.resetFields()
      setCreateOpen(false)
      void loadSchedules()
    } catch (e) {
      message.error(t('agent.schedules_create_failed', { message: errorDetail(e) }))
    } finally {
      setCreating(false)
    }
  }

  const terminal = session !== null && TERMINAL_PHASES.includes(session.phase)
  const runInProgress = session !== null && !terminal

  const sessionsPanel = (
    <section className="desk-card" aria-label={t('agent.sessions_title')}>
      <div className="agent-panel-head">
        <h3 className="section-label">{t('agent.sessions_title')}</h3>
        <Button
          size="small"
          icon={<ReloadOutlined aria-hidden="true" />}
          onClick={() => void loadSessions()}
        >
          {t('common.refresh')}
        </Button>
      </div>
      {sessionsLoading ? (
        <div className="agent-loading" role="status">
          <Spin size="small" />
        </div>
      ) : sessionsError ? (
        <ErrorState title={t('agent.sessions_failed')} detail={sessionsError} onRetry={() => void loadSessions()} />
      ) : sessions.length === 0 ? (
        <EmptyState icon={<RobotOutlined />} title={t('agent.sessions_empty')} hint={t('agent.sessions_empty_hint')} />
      ) : (
        <>
          <ul className="agent-session-list">
            {sessions.map((s) => {
              const active = session?.research_id === s.research_id
              return (
                <li key={s.research_id}>
                  <button
                    type="button"
                    className={`agent-session-item${active ? ' agent-session-item--active' : ''}`}
                    onClick={() => void selectSession(s.research_id)}
                    aria-current={active ? 'true' : undefined}
                  >
                    <span className="agent-session-objective">{s.objective || s.research_id}</span>
                    <span className="agent-session-meta">
                      <span className="mono agent-session-id">{s.research_id}</span>
                      <PhaseTag value={s.phase} />
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
          {sessionIds.length > SESSIONS_CAP && (
            <p className="agent-list-note">{t('agent.sessions_cap', { count: sessions.length })}</p>
          )}
        </>
      )}
    </section>
  )

  const schedulesPanel = (
    <section className="desk-card" aria-label={t('agent.schedules_title')}>
      <div className="agent-panel-head">
        <h3 className="section-label">{t('agent.schedules_title')}</h3>
        <div className="agent-panel-actions">
          <Button
            size="small"
            icon={<ReloadOutlined aria-hidden="true" />}
            onClick={() => void loadSchedules()}
            aria-label={t('common.refresh')}
          >
            {t('common.refresh')}
          </Button>
          <Button
            size="small"
            icon={<PlusOutlined aria-hidden="true" />}
            onClick={() => setCreateOpen((v) => !v)}
            aria-expanded={createOpen}
          >
            {t('agent.schedules_create')}
          </Button>
        </div>
      </div>
      {!schedulePersistent && <p className="agent-volatile-note" role="note">
        <InfoCircleFilled aria-hidden="true" />
        <span>{t('agent.schedules_note')}</span>
      </p>}
      {schedulesLoading ? (
        <div className="agent-loading" role="status">
          <Spin size="small" />
        </div>
      ) : schedulesError ? (
        <ErrorState title={t('agent.schedules_failed')} detail={schedulesError} onRetry={() => void loadSchedules()} />
      ) : schedules.length === 0 ? (
        <EmptyState
          icon={<PauseCircleOutlined />}
          title={t('agent.schedules_empty')}
          hint={t('agent.schedules_empty_hint')}
        />
      ) : (
        <ul className="agent-schedule-list">
          {schedules.map((s) => (
            <li key={s.schedule_id} className="agent-schedule">
              <div className="agent-schedule-head">
                <span className="mono agent-schedule-id">{s.schedule_id}</span>
                {s.paused ? (
                  <Tag className="signal-tag signal-review" icon={<PauseCircleOutlined aria-hidden="true" />}>
                    {t('agent.schedule_paused')}
                  </Tag>
                ) : (
                  <Tag className="signal-tag signal-info" icon={<PlayCircleOutlined aria-hidden="true" />}>
                    {t('agent.schedule_active')}
                  </Tag>
                )}
                <Popconfirm
                  title={t('agent.schedule_delete_confirm')}
                  okText={t('agent.schedule_delete_confirm_ok')}
                  cancelText={t('common.cancel')}
                  onConfirm={() => void removeSchedule(s.schedule_id)}
                >
                  <Button
                    size="small"
                    type="text"
                    danger
                    icon={<DeleteOutlined aria-hidden="true" />}
                    aria-label={t('agent.schedule_delete')}
                  />
                </Popconfirm>
              </div>
              <span className="agent-chip mono" aria-label={t('agent.schedule_cron')}>
                {s.cron}
              </span>
              <span className="agent-schedule-objective" title={s.objective}>
                {s.objective}
              </span>
              <div className="agent-schedule-meta">
                <span className="agent-schedule-meta-pair">
                  <span>{t('agent.schedule_next_due')}</span>
                  <span className="mono">{formatStamp(s.next_due, i18n.language)}</span>
                </span>
                <span className="agent-schedule-meta-pair">
                  <span>{t('agent.schedule_last_fired')}</span>
                  <span className="mono">{formatStamp(s.last_fired, i18n.language)}</span>
                </span>
              </div>
              {s.last_error ? (
                <p className="agent-schedule-error">
                  <WarningOutlined aria-hidden="true" />
                  <span>{s.last_error}</span>
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {createOpen && (
        <div className="agent-schedule-create">
          <p className="agent-form-title">{t('agent.schedules_create_title')}</p>
          <Form<ScheduleFormValues>
            layout="vertical"
            form={scheduleForm}
            onFinish={(values) => void submitSchedule(values)}
            className="agent-schedule-form"
          >
            <Form.Item
              label={t('agent.form_objective')}
              name="objective"
              rules={[{ required: true, message: t('agent.form_objective_required') }]}
            >
              <Input placeholder={t('agent.form_objective_placeholder')} />
            </Form.Item>
            <Form.Item
              label={t('agent.form_cron')}
              name="cron"
              rules={[{ required: true, message: t('agent.form_cron_required') }]}
              extra={t('agent.form_cron_help')}
            >
              <Input className="mono" placeholder={t('agent.form_cron_placeholder')} />
            </Form.Item>
            <Form.Item
              label={t('agent.form_research_id')}
              name="research_id"
              rules={[{ required: true, message: t('agent.form_research_id_required') }]}
            >
              <Input className="mono" placeholder={t('agent.form_research_id_placeholder')} />
            </Form.Item>
            <Form.Item label={t('agent.form_context')} name="context">
              <Input.TextArea rows={3} placeholder={t('agent.form_context_placeholder')} />
            </Form.Item>
            <div className="agent-form-footer">
              <Button onClick={() => setCreateOpen(false)}>{t('common.cancel')}</Button>
              <Button type="primary" htmlType="submit" loading={creating}>
                {t('agent.form_submit')}
              </Button>
            </div>
          </Form>
        </div>
      )}
    </section>
  )

  const composer = (
    <Sender
      value={draft}
      onChange={(value: string) => setDraft(value)}
      onSubmit={handleSend}
      placeholder={t('chat.goal_placeholder')}
    />
  )

  const contextCollapse = (
    <Collapse
      ghost
      className="agent-context-collapse"
      items={[
        {
          key: 'context',
          label: t('agent.context_collapse'),
          children: (
            <>
              <Input.TextArea
                rows={3}
                className="mono"
                aria-label={t('agent.context_collapse')}
                placeholder={t('agent.form_context_placeholder')}
                value={contextDraft}
                onChange={(e) => setContextDraft(e.target.value)}
              />
              <p className="agent-context-hint">{t('agent.context_hint')}</p>
            </>
          ),
        },
      ]}
    />
  )

  return (
    <div className="agent-page">
      <div className="agent-layout">
        <aside className="agent-side">
          {sessionsPanel}
          {schedulesPanel}
        </aside>
        <main className="agent-main">
          {session === null ? (
            <div className="agent-welcome">
              <BrandMark size={56} />
              <h2 className="agent-welcome-title">{t('agent.welcome_title')}</h2>
              <p className="agent-welcome-hint">{t('agent.welcome_hint')}</p>
              <section className="desk-card agent-composer-card" aria-label={t('agent.composer_title')}>
                {composer}
                {contextCollapse}
              </section>
              <div className="agent-chip-row">
                <button
                  type="button"
                  className="agent-chip-btn"
                  onClick={() => setDraft(t('agent.chip_topic_prefill'))}
                >
                  <FileSearchOutlined aria-hidden="true" />
                  <span>{t('agent.chip_topic')}</span>
                </button>
                <button
                  type="button"
                  className="agent-chip-btn"
                  onClick={() => setCreateOpen(true)}
                >
                  <ScheduleOutlined aria-hidden="true" />
                  <span>{t('agent.chip_schedule')}</span>
                </button>
                <button
                  type="button"
                  className="agent-chip-btn"
                  onClick={() => navigate?.('analysis')}
                >
                  <CloudUploadOutlined aria-hidden="true" />
                  <span>{t('agent.chip_analysis')}</span>
                </button>
                <button
                  type="button"
                  className="agent-chip-btn"
                  onClick={() => navigate?.('results')}
                >
                  <DatabaseOutlined aria-hidden="true" />
                  <span>{t('agent.chip_results')}</span>
                </button>
              </div>
            </div>
          ) : (
            <>
              <div className="agent-thread">
                <div className="agent-turn agent-turn--user">
                  <div className="agent-user-bubble" aria-label={t('agent.you_label')}>
                    {session.objective}
                  </div>
                </div>
                <div className="agent-turn agent-turn--agent">
                  <div className="agent-avatar" aria-hidden="true">
                    <RobotOutlined />
                  </div>
                  <div className="agent-work">
                    <div className="agent-work-head">
                      <PhaseTag value={session.phase} />
                      <span className="mono agent-work-id">{session.research_id}</span>
                      {session.owner ? (
                        <span className="agent-meta-value">{t('agent.owner_label')}: {session.owner}</span>
                      ) : null}
                      {!terminal && !statusUnavailable && (
                        <Button
                          danger
                          size="small"
                          icon={<StopOutlined aria-hidden="true" />}
                          onClick={() => void cancelResearch()}
                          disabled={cancelling}
                        >
                          {t('chat.cancel')}
                        </Button>
                      )}
                    </div>

                    {!terminal && !statusUnavailable && (
                      <div className="agent-working" role="status">
                        <Spin size="small" aria-hidden="true" />
                        <span>{t('agent.working')}</span>
                      </div>
                    )}
                    {statusUnavailable && (
                      <div className="agent-band agent-band--neutral" role="status">
                        <InfoCircleFilled aria-hidden="true" />
                        <div>
                          <div>{t('agent.run_archived')}</div>
                          <div className="agent-band-note">{t('agent.run_archived_detail')}</div>
                        </div>
                      </div>
                    )}
                    {session.steps.length === 0 && terminal && (
                      <EmptyState icon={<NodeIndexOutlined />} title={t('chat.no_steps')} />
                    )}
                    {session.conclusion && (
                      <section className="agent-conclusion" aria-label={t('agent.conclusion_title')}>
                        <h3>{t('agent.conclusion_title')}</h3>
                        <Typography.Paragraph>{conclusionText(session.conclusion)}</Typography.Paragraph>
                      </section>
                    )}
                    {session.steps.length > 0 && (
                      <ul className="agent-steps">
                        {session.steps.map((step) => (
                          <li key={step.step_id} className="agent-step">
                            <div className="agent-step-main">
                              <span className="mono agent-step-id">{step.step_id}</span>
                              <span className="agent-chip mono">{step.tool}</span>
                              <StepStatusTag value={step.status} />
                              <span className="mono agent-step-attempts">
                                {t('agent.step_attempts', { count: step.attempts })}
                              </span>
                            </div>
                            {step.error ? <p className="agent-step-error">{step.error}</p> : null}
                            {step.output_preview && <Collapse ghost items={[{ key: step.step_id, label: t('agent.step_output'), children: <pre className="agent-step-output">{step.output_preview}</pre> }]} />}
                            {step.evidence?.map((evidence, index) => {
                              const label = t('agent.source_metadata', { page: evidence.page ?? '?', document: evidence.document_name ?? evidence.evidence_id ?? evidence.chunk_id ?? '?' })
                              if (evidence.run_id && Number.isInteger(evidence.page) && (evidence.page ?? 0) > 0 && Number.isInteger(evidence.document_index) && (evidence.document_index ?? -1) >= 0) {
                                return <a className="agent-evidence-link" key={`${step.step_id}-evidence-${index}`} href={`#/evidence?run=${encodeURIComponent(evidence.run_id)}&doc=${evidence.document_index ?? 0}&page=${evidence.page ?? 1}&cell=${encodeURIComponent(evidence.cell_id ?? '')}`}>{t('agent.open_evidence', { page: evidence.page ?? '?' })}</a>
                              }
                              if (evidence.source_url && /^https?:\/\//i.test(evidence.source_url)) {
                                return <a className="agent-evidence-link" key={`${step.step_id}-evidence-${index}`} href={evidence.source_url} target="_blank" rel="noreferrer">{label}</a>
                              }
                              return <span className="agent-evidence-link" key={`${step.step_id}-evidence-${index}`}>{label}</span>
                            })}
                          </li>
                        ))}
                      </ul>
                    )}

                    {session.phase === 'completed' && (
                      <div className="agent-band agent-band--verified" role="status">
                        <CheckCircleFilled aria-hidden="true" />
                        <div>
                          <div>{t('chat.completed', { objective: session.objective })}</div>
                          {!session.conclusion && <div className="agent-band-note">{t('agent.conclusion_unavailable')}</div>}
                        </div>
                      </div>
                    )}
                    {session.phase === 'failed' && (
                      <div className="agent-band agent-band--failed" role="alert">
                        <CloseCircleFilled aria-hidden="true" />
                        <div>
                          <div>{t('chat.failed', { objective: session.objective })}</div>
                          {session.error ? <div className="agent-band-detail">{session.error}</div> : null}
                          {!session.conclusion && <div className="agent-band-note">{t('agent.conclusion_unavailable')}</div>}
                        </div>
                      </div>
                    )}
                    {session.phase === 'cancelled' && (
                      <div className="agent-band agent-band--neutral" role="status">
                        <MinusCircleOutlined aria-hidden="true" />
                        <div>{t('agent.run_cancelled')}</div>
                      </div>
                    )}

                    <div className="agent-work-limits">
                      <Unavailable title={t('agent.limits_title')} reason={t('agent.plan_not_exposed')} />
                      <Collapse
                        ghost
                        items={[
                          {
                            key: 'limits',
                            label: t('agent.limits_more'),
                            children: <p className="agent-limits-detail">{t('agent.limits_detail')}</p>,
                          },
                        ]}
                      />
                    </div>
                  </div>
                </div>
              </div>
              <section className="desk-card agent-composer agent-composer--dock" aria-label={t('agent.composer_title')}>
                {composer}
              </section>
            </>
          )}
        </main>
      </div>
    </div>
  )
}

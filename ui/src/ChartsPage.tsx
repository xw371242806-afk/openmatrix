import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Empty, Spin } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import ReactEChartsCoreImport from 'echarts-for-react/lib/core'
// Vite 8 consistent-CJS-interop: the default import can arrive as
// { default: Component } instead of the component itself (React error #130).
const ReactEChartsCore = ((ReactEChartsCoreImport as any).default ?? ReactEChartsCoreImport) as typeof ReactEChartsCoreImport
import * as echarts from 'echarts/core'
import { PieChart, BarChart } from 'echarts/charts'
import { GridComponent, TitleComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsOption } from 'echarts'
import { useTranslation } from 'react-i18next'
import PageHeader from './components/PageHeader'
import ErrorState from './components/ErrorState'
import { getJson } from './api/client'
import './charts.css'

echarts.use([PieChart, BarChart, GridComponent, TitleComponent, TooltipComponent, LegendComponent, CanvasRenderer])

/* ── Light palette (matches the Research Desk tokens) ────── */

const PALETTE = {
  verified: '#2E7D4F',
  review: '#96660A',
  failed: '#B3372B',
  info: '#33608A',
  neutral: '#989CA4',
  copper: '#A05C2C',
} as const

const AXIS_TEXT = '#52565D'
const HAIRLINE = '#E5E1D6'

const CELL_STATUS_COLORS: Record<string, string> = {
  completed: PALETTE.verified,
  needs_review: PALETTE.review,
  failed: PALETTE.failed,
  not_disclosed: PALETTE.neutral,
}

const CITATION_STATUS_COLORS: Record<string, string> = {
  verified_exact: PALETTE.verified,
  verified_normalized: PALETTE.verified,
  verified_fuzzy: PALETTE.info,
  not_found: PALETTE.failed,
  not_applicable: PALETTE.neutral,
}

/* ── Defensive parsing: render only what the backend sends ─ */

interface FailedQuestion {
  question: string
  fail_count: number
}

type MetricState<T> =
  | { status: 'loading' }
  | { status: 'error'; detail: string }
  | { status: 'ready'; data: T }

function errorDetail(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

function toCounts(raw: unknown): Record<string, number> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
  const out: Record<string, number> = {}
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    const n = typeof value === 'number' ? value : Number(value)
    if (Number.isFinite(n)) out[key] = n
  }
  return out
}

function toTotalCells(raw: unknown): number | null {
  const payload = raw as { total?: unknown; total_cells?: unknown } | null
  const candidate = payload?.total_cells ?? payload?.total
  const n = typeof candidate === 'number' ? candidate : Number(candidate)
  return candidate != null && Number.isFinite(n) ? n : null
}

function toRatePercent(raw: unknown): string | null {
  const payload = raw as { rate?: unknown; verification_rate?: unknown } | null
  const candidate = payload?.verification_rate ?? payload?.rate
  const n = typeof candidate === 'number' ? candidate : Number(candidate)
  if (candidate == null || !Number.isFinite(n)) return null
  const pct = n >= 0 && n <= 1 ? n * 100 : n
  return `${Math.round(pct * 100) / 100}%`
}

function toFailedQuestions(raw: unknown): FailedQuestion[] {
  if (!Array.isArray(raw)) return []
  return raw.flatMap((item) => {
    const candidate = item as { question?: unknown; fail_count?: unknown }
    if (typeof candidate.question !== 'string' || !candidate.question.trim()) return []
    const n = typeof candidate.fail_count === 'number' ? candidate.fail_count : Number(candidate.fail_count)
    if (!Number.isFinite(n)) return []
    return [{ question: candidate.question, fail_count: n }]
  })
}

/* ── Chart builders ──────────────────────────────────────── */

function pieOption(data: Record<string, number>, colorFor: (key: string) => string): EChartsOption {
  return {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, type: 'scroll', textStyle: { color: AXIS_TEXT, fontSize: 11 } },
    series: [{
      type: 'pie',
      radius: ['42%', '68%'],
      center: ['50%', '44%'],
      avoidLabelOverlap: false,
      label: { show: false },
      labelLine: { show: false },
      itemStyle: { borderColor: '#FFFFFF', borderWidth: 1 },
      data: Object.entries(data).map(([key, value]) => ({
        value,
        name: key,
        itemStyle: { color: colorFor(key) },
      })),
    }],
  }
}

function barOption(
  categories: string[],
  values: number[],
  color: string,
  horizontal = false,
): EChartsOption {
  const truncate = (value: string) => (value.length > 22 ? `${value.slice(0, 22)}…` : value)
  const categoryAxis = {
    type: 'category' as const,
    data: categories,
    axisTick: { alignWithLabel: true },
    axisLabel: { color: AXIS_TEXT, formatter: truncate },
    axisLine: { lineStyle: { color: HAIRLINE } },
  }
  const valueAxis = {
    type: 'value' as const,
    axisLabel: { color: AXIS_TEXT },
    axisLine: { lineStyle: { color: HAIRLINE } },
    splitLine: { lineStyle: { color: HAIRLINE } },
  }
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 8, right: horizontal ? 34 : 16, bottom: 8, top: 12, containLabel: true },
    xAxis: horizontal ? valueAxis : categoryAxis,
    yAxis: horizontal ? categoryAxis : valueAxis,
    series: [{
      type: 'bar',
      data: values,
      barWidth: '55%',
      itemStyle: { color },
      label: {
        show: true,
        position: horizontal ? 'right' : 'top',
        color: AXIS_TEXT,
      },
    }],
  }
}

/* ── Page ────────────────────────────────────────────────── */

type CountsState = MetricState<Record<string, number>>
type FailedState = MetricState<FailedQuestion[]>
type TotalState = MetricState<number | null>
type RateState = MetricState<string | null>

const ChartsPage = () => {
  const { t } = useTranslation()
  const [statusCounts, setStatusCounts] = useState<CountsState>({ status: 'loading' })
  const [citationCounts, setCitationCounts] = useState<CountsState>({ status: 'loading' })
  const [avgAnswerLength, setAvgAnswerLength] = useState<CountsState>({ status: 'loading' })
  const [failedQuestions, setFailedQuestions] = useState<FailedState>({ status: 'loading' })
  const [totalCells, setTotalCells] = useState<TotalState>({ status: 'loading' })
  const [verificationRate, setVerificationRate] = useState<RateState>({ status: 'loading' })
  const [loading, setLoading] = useState(true)
  const roundRef = useRef(0)

  const loadAll = useCallback(() => {
    const round = ++roundRef.current
    setLoading(true)
    setStatusCounts({ status: 'loading' })
    setCitationCounts({ status: 'loading' })
    setAvgAnswerLength({ status: 'loading' })
    setFailedQuestions({ status: 'loading' })
    setTotalCells({ status: 'loading' })
    setVerificationRate({ status: 'loading' })

    const inflight: Array<Promise<void>> = []

    const run = async <T,>(
      url: string,
      parse: (raw: unknown) => T,
      setState: (next: MetricState<T>) => void,
    ) => {
      try {
        const raw = await getJson<unknown>(url)
        if (round !== roundRef.current) return // stale round — a newer refresh owns the state
        setState({ status: 'ready', data: parse(raw) })
      } catch (e) {
        if (round !== roundRef.current) return
        setState({ status: 'error', detail: errorDetail(e) })
      }
    }

    inflight.push(run('/api/v1/stats/status-counts', toCounts, setStatusCounts))
    inflight.push(run('/api/v1/stats/citation-status-counts', toCounts, setCitationCounts))
    inflight.push(run('/api/v1/stats/average-answer-length', toCounts, setAvgAnswerLength))
    inflight.push(run('/api/v1/stats/most-failed-questions?limit=8', toFailedQuestions, setFailedQuestions))
    inflight.push(run('/api/v1/stats/total-cells', toTotalCells, setTotalCells))
    inflight.push(run('/api/v1/stats/citation-verification-rate', toRatePercent, setVerificationRate))
    void Promise.allSettled(inflight).then(() => {
      if (round === roundRef.current) setLoading(false)
    })
  }, [])

  useEffect(() => {
    loadAll()
  }, [loadAll])

  const renderChartBody = (
    state: CountsState | FailedState,
    buildOption: () => EChartsOption,
  ) => {
    if (state.status === 'loading') {
      return <div className="quality-loading"><Spin /></div>
    }
    if (state.status === 'error') {
      return <ErrorState detail={state.detail} onRetry={loadAll} />
    }
    const isEmpty = Array.isArray(state.data) ? state.data.length === 0 : Object.keys(state.data).length === 0
    if (isEmpty) {
      return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('charts.no_data')} />
    }
    return <ReactEChartsCore option={buildOption()} echarts={echarts} style={{ height: 240 }} />
  }

  const renderStatBody = (state: TotalState | RateState) => {
    if (state.status === 'loading') {
      return <div className="quality-loading"><Spin /></div>
    }
    if (state.status === 'error') {
      return <ErrorState detail={state.detail} onRetry={loadAll} />
    }
    return (
      <div className="quality-big mono" aria-live="polite">
        {state.data ?? '—'}
      </div>
    )
  }

  return (
    <div className="quality-page">
      <PageHeader
        title={t('quality.title')}
        description={t('quality.subtitle')}
        actions={
          <Button
            icon={<ReloadOutlined aria-hidden="true" />}
            onClick={loadAll}
            loading={loading}
          >
            {t('charts.refresh')}
          </Button>
        }
      />

      <div className="quality-grid">
        <section className="desk-card quality-card" aria-label={t('charts.cell_status')}>
          <div className="quality-card-head">
            <h3 className="quality-card-title">{t('charts.cell_status')}</h3>
            <p className="quality-card-hint">{t('quality.cell_status_hint')}</p>
          </div>
          <div className="quality-card-body">
            {renderChartBody(statusCounts, () =>
              pieOption(statusCounts.status === 'ready' ? statusCounts.data : {}, (key) =>
                CELL_STATUS_COLORS[key] ?? PALETTE.copper),
            )}
          </div>
        </section>

        <section className="desk-card quality-card" aria-label={t('charts.citation_status')}>
          <div className="quality-card-head">
            <h3 className="quality-card-title">{t('charts.citation_status')}</h3>
            <p className="quality-card-hint">{t('quality.citation_status_hint')}</p>
          </div>
          <div className="quality-card-body">
            {renderChartBody(citationCounts, () =>
              pieOption(citationCounts.status === 'ready' ? citationCounts.data : {}, (key) =>
                CITATION_STATUS_COLORS[key] ?? PALETTE.neutral),
            )}
          </div>
        </section>

        <section className="desk-card quality-card" aria-label={t('charts.avg_answer_length')}>
          <div className="quality-card-head">
            <h3 className="quality-card-title">{t('charts.avg_answer_length')}</h3>
            <p className="quality-card-hint">{t('quality.avg_answer_length_hint')}</p>
          </div>
          <div className="quality-card-body">
            {renderChartBody(avgAnswerLength, () => {
              const data = avgAnswerLength.status === 'ready' ? avgAnswerLength.data : {}
              return barOption(Object.keys(data), Object.values(data), PALETTE.copper)
            })}
          </div>
        </section>

        <section className="desk-card quality-card" aria-label={t('charts.total_cells')}>
          <div className="quality-card-head">
            <h3 className="quality-card-title">{t('charts.total_cells')}</h3>
            <p className="quality-card-hint">{t('quality.total_cells_hint')}</p>
          </div>
          <div className="quality-card-body">
            {renderStatBody(totalCells)}
          </div>
        </section>

        <section className="desk-card quality-card" aria-label={t('charts.most_failed_questions')}>
          <div className="quality-card-head">
            <h3 className="quality-card-title">{t('charts.most_failed_questions')}</h3>
            <p className="quality-card-hint">{t('quality.most_failed_questions_hint')}</p>
          </div>
          <div className="quality-card-body">
            {renderChartBody(failedQuestions, () => {
              const data = failedQuestions.status === 'ready' ? failedQuestions.data : []
              return barOption(
                data.map((q) => q.question),
                data.map((q) => q.fail_count),
                PALETTE.failed,
                true,
              )
            })}
          </div>
        </section>

        <section className="desk-card quality-card" aria-label={t('charts.verification_rate')}>
          <div className="quality-card-head">
            <h3 className="quality-card-title">{t('charts.verification_rate')}</h3>
            <p className="quality-card-hint">{t('quality.verification_rate_hint')}</p>
          </div>
          <div className="quality-card-body">
            {renderStatBody(verificationRate)}
          </div>
        </section>
      </div>
    </div>
  )
}

export default ChartsPage

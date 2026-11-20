import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { Tag } from 'antd'
import {
  CheckCircleFilled,
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleFilled,
  ExceptionOutlined,
  HourglassOutlined,
  MinusCircleOutlined,
  MinusOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  StopOutlined,
  SyncOutlined,
} from '@ant-design/icons'

export type SignalKind = 'verified' | 'review' | 'failed' | 'info' | 'progress' | 'neutral'

interface SignalSpec {
  kind: SignalKind
  icon?: ReactNode
  /** Rotating icon for in-flight states (disabled under reduced motion). */
  spin?: boolean
}

/** Status is always carried by icon + text; color is an aid, never the signal. */
function SignalTag({ spec, label }: { spec: SignalSpec; label: string }) {
  return (
    <Tag
      className={`signal-tag signal-${spec.kind}`}
      icon={spec.spin ? <SyncOutlined className="spin-icon" /> : spec.icon}
    >
      {label}
    </Tag>
  )
}

const VERIFIED: SignalSpec = { kind: 'verified', icon: <CheckCircleFilled /> }
const REVIEW: SignalSpec = { kind: 'review', icon: <ExceptionOutlined /> }
const FAILED: SignalSpec = { kind: 'failed', icon: <CloseCircleFilled /> }
const INFO: SignalSpec = { kind: 'info', icon: <CheckCircleOutlined /> }
const PROGRESS: SignalSpec = { kind: 'progress', spin: true }
const NEUTRAL: SignalSpec = { kind: 'neutral', icon: <MinusCircleOutlined /> }

const REGISTRY: Record<string, Record<string, SignalSpec>> = {
  citation: {
    verified_exact: VERIFIED,
    verified_normalized: VERIFIED,
    verified_fuzzy: INFO,
    not_found: FAILED,
    not_applicable: NEUTRAL,
  },
  cell: {
    completed: VERIFIED,
    needs_review: REVIEW,
    failed: FAILED,
    not_disclosed: NEUTRAL,
  },
  phase: {
    pending: { kind: 'neutral', icon: <ClockCircleOutlined /> },
    planning: { kind: 'info', icon: <PlayCircleOutlined /> },
    running: PROGRESS,
    paused: { kind: 'review', icon: <PauseCircleOutlined /> },
    completed: VERIFIED,
    failed: FAILED,
    cancelled: { kind: 'neutral', icon: <StopOutlined /> },
  },
  step: {
    pending: { kind: 'neutral', icon: <ClockCircleOutlined /> },
    running: PROGRESS,
    succeeded: VERIFIED,
    failed: FAILED,
    skipped: { kind: 'neutral', icon: <MinusOutlined /> },
  },
  task: {
    queued: { kind: 'neutral', icon: <HourglassOutlined /> },
    running: PROGRESS,
    succeeded: VERIFIED,
    failed: FAILED,
    cancelled: { kind: 'neutral', icon: <StopOutlined /> },
  },
}

function useStatusLabel(namespace: string, value: string | null | undefined): { spec: SignalSpec; label: string } | null {
  const { t, i18n } = useTranslation()
  if (!value) return null
  const key = `status.${namespace}.${value}`
  const spec = REGISTRY[namespace]?.[value] ?? NEUTRAL
  const label = i18n.exists(key) ? t(key) : value
  return { spec, label }
}

export function CitationStatusTag({ value }: { value: string | null | undefined }) {
  const entry = useStatusLabel('citation', value)
  if (!entry) return null
  return <SignalTag spec={entry.spec} label={entry.label} />
}

export function CellStatusTag({ value }: { value: string | null | undefined }) {
  const entry = useStatusLabel('cell', value)
  if (!entry) return null
  return <SignalTag spec={entry.spec} label={entry.label} />
}

export function PhaseTag({ value }: { value: string | null | undefined }) {
  const entry = useStatusLabel('phase', value)
  if (!entry) return null
  return <SignalTag spec={entry.spec} label={entry.label} />
}

export function StepStatusTag({ value }: { value: string | null | undefined }) {
  const entry = useStatusLabel('step', value)
  if (!entry) return null
  return <SignalTag spec={entry.spec} label={entry.label} />
}

export function TaskStatusTag({ value }: { value: string | null | undefined }) {
  const entry = useStatusLabel('task', value)
  if (!entry) return null
  return <SignalTag spec={entry.spec} label={entry.label} />
}

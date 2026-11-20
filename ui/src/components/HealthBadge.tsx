import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Popover, Spin } from 'antd'
import { getJson } from '../api/client'
import type { HealthDetailed } from '../api/types'

export type HealthState = 'checking' | 'live' | 'offline'

interface HealthBadgeProps {
  state: HealthState
}

/**
 * Backend health pill. Click opens the /api/v1/health/detailed component
 * report (fetched on demand). Inherited from the shell poller — the badge
 * itself never probes the API.
 */
export default function HealthBadge({ state }: HealthBadgeProps) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<HealthDetailed | null>(null)
  const [detailState, setDetailState] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')

  const label =
    state === 'live'
      ? t('common.backend_live')
      : state === 'offline'
        ? t('common.backend_offline')
        : t('common.checking')

  const loadDetail = async () => {
    setDetailState('loading')
    try {
      setDetail(await getJson<HealthDetailed>('/api/v1/health/detailed'))
      setDetailState('ready')
    } catch {
      setDetailState('error')
    }
  }

  const onOpenChange = (next: boolean) => {
    setOpen(next)
    if (next && detailState === 'idle') void loadDetail()
  }

  const content = (
    <div className="health-detail">
      {detailState === 'loading' && <Spin size="small" />}
      {detailState === 'error' && <span>{t('shell.health_detail_failed')}</span>}
      {detailState === 'ready' && detail && (
        <>
          <div className="health-detail-row">
            <span className="health-detail-key">{t('shell.health_status')}</span>
            <span>{detail.status ?? t('common.unknown')}</span>
          </div>
          {detail.service && (
            <div className="health-detail-row">
              <span className="health-detail-key">{t('shell.health_service')}</span>
              <span>{detail.service}</span>
            </div>
          )}
          {detail.components && Object.entries(detail.components).map(([name, value]) => (
            <div className="health-detail-row" key={name}>
              <span className="health-detail-key">{name}</span>
              <span>{value}</span>
            </div>
          ))}
        </>
      )}
    </div>
  )

  return (
    <Popover content={content} trigger="click" placement="bottomRight" open={open} onOpenChange={onOpenChange}>
      <button type="button" className={`health-badge health-badge--${state}`} aria-label={label}>
        <span className="health-badge-dot" aria-hidden="true" />
        <span className="health-badge-label">{label}</span>
      </button>
    </Popover>
  )
}

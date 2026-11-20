import type { ReactNode } from 'react'
import { FileTextOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'

interface EmptyStateProps {
  icon?: ReactNode
  title: string
  hint?: string
  action?: ReactNode
}

export default function EmptyState({ icon, title, hint, action }: EmptyStateProps) {
  const { t } = useTranslation()
  return (
    <div className="empty-state" role="status">
      <div className="empty-state-icon" aria-hidden="true">{icon ?? <FileTextOutlined />}</div>
      <div className="empty-state-title">{title}</div>
      {hint && <div className="empty-state-hint">{hint}</div>}
      {action ?? null}
      {!action && !hint && <span className="visually-hidden">{t('common.empty_state_note')}</span>}
    </div>
  )
}

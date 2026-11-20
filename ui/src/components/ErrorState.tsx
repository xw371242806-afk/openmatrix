import { CloseCircleFilled, ReloadOutlined } from '@ant-design/icons'
import { Button } from 'antd'
import { useTranslation } from 'react-i18next'

interface ErrorStateProps {
  title?: string
  detail?: string
  onRetry?: () => void
}

export default function ErrorState({ title, detail, onRetry }: ErrorStateProps) {
  const { t } = useTranslation()
  return (
    <div className="error-state" role="alert">
      <div className="error-state-head">
        <CloseCircleFilled aria-hidden="true" />
        <span>{title ?? t('common.request_failed')}</span>
      </div>
      {detail && <pre className="error-state-detail">{detail}</pre>}
      {onRetry && (
        <Button size="small" icon={<ReloadOutlined aria-hidden="true" />} onClick={onRetry}>
          {t('common.retry')}
        </Button>
      )}
    </div>
  )
}

import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { LockOutlined } from '@ant-design/icons'

interface UnavailableProps {
  title: string
  reason?: string
  icon?: ReactNode
}

/**
 * Honest placeholder for capabilities the current backend does not
 * expose. Renders a real note — never a fake button or fabricated data.
 */
export default function Unavailable({ title, reason, icon }: UnavailableProps) {
  const { t } = useTranslation()
  return (
    <div className="unavailable" role="note">
      <div className="unavailable-head">
        {icon ?? <LockOutlined aria-hidden="true" />}
        <span>{title}</span>
      </div>
      {reason && <p className="unavailable-reason">{reason}</p>}
      <span className="visually-hidden">{t('common.unavailable_note')}</span>
    </div>
  )
}

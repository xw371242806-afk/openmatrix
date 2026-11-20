import { useEffect, useState } from 'react'
import { Button, Input, Popover } from 'antd'
import { KeyOutlined, LogoutOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import {
  clearAccessToken,
  hasAccessToken,
  setAccessToken,
  subscribeAccessToken,
} from '../api/client'

interface AccessTokenControlProps {
  onChange?: () => void
}

export default function AccessTokenControl({ onChange }: AccessTokenControlProps) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const [configured, setConfigured] = useState(hasAccessToken())

  useEffect(() => subscribeAccessToken(setConfigured), [])

  const connect = () => {
    const token = draft.trim()
    if (!token) return
    setAccessToken(token)
    setDraft('')
    setOpen(false)
    onChange?.()
  }

  const clear = () => {
    clearAccessToken()
    setDraft('')
    setOpen(false)
    onChange?.()
  }

  const content = (
    <div className="access-token-popover">
      <label className="access-token-label" htmlFor="openmatrix-access-token">
        {t('shell.access_token_label')}
      </label>
      <Input.Password
        id="openmatrix-access-token"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onPressEnter={connect}
        placeholder={t('shell.access_token_placeholder')}
        autoComplete="off"
      />
      <p className="access-token-note">{t('shell.access_token_note')}</p>
      <div className="access-token-actions">
        {configured && (
          <Button icon={<LogoutOutlined aria-hidden="true" />} onClick={clear}>
            {t('shell.access_token_clear')}
          </Button>
        )}
        <Button type="primary" disabled={!draft.trim()} onClick={connect}>
          {t('shell.access_token_connect')}
        </Button>
      </div>
    </div>
  )

  return (
    <Popover content={content} trigger="click" open={open} onOpenChange={setOpen} placement="bottomRight">
      <button
        type="button"
        className={`access-token-badge${configured ? ' access-token-badge--configured' : ''}`}
        aria-label={configured ? t('shell.access_token_configured') : t('shell.access_token_required')}
      >
        <KeyOutlined aria-hidden="true" />
        <span>{configured ? t('shell.access_token_configured') : t('shell.access_token_required')}</span>
      </button>
    </Popover>
  )
}

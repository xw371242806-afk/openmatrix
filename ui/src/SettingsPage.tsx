import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import {
  Alert, Button, Form, Input, Modal, Popconfirm, Segmented, Spin, Table, Tag, message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  CheckCircleFilled,
  CloseCircleFilled,
  CopyOutlined,
  ExceptionOutlined,
  MinusCircleOutlined,
  ReloadOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import i18n, { persistLanguage } from './i18n'
import { ApiError, deleteJson, getJson, postJson } from './api/client'
import type { HealthDetailed, ModelRouterConfig } from './api/types'
import PageHeader from './components/PageHeader'
import EmptyState from './components/EmptyState'
import ErrorState from './components/ErrorState'
import './settings.css'

interface ApiKeyRow {
  id: string
  prefix: string
  name: string
  created_at: string
  revoked: boolean
}

interface AuditEntry {
  id: string
  action: string
  user: string
  timestamp: string
  details: string
}

type Loadable<T> =
  | { status: 'loading' }
  | { status: 'error'; detail: string }
  | { status: 'ready'; data: T }

/** Component/value health is always icon + text, never color alone. */
function HealthValueTag({ value }: { value: string }) {
  const spec: Record<string, { cls: string; icon: ReactNode }> = {
    ok: { cls: 'signal-verified', icon: <CheckCircleFilled /> },
    unavailable: { cls: 'signal-review', icon: <ExceptionOutlined /> },
    error: { cls: 'signal-failed', icon: <CloseCircleFilled /> },
  }
  const entry = spec[value] ?? { cls: 'signal-neutral', icon: <MinusCircleOutlined /> }
  return (
    <Tag className={`signal-tag ${entry.cls}`} icon={entry.icon}>{value}</Tag>
  )
}

const SettingsPage = () => {
  const { t } = useTranslation()
  const [masterKey, setMasterKey] = useState('')
  const [apiKeys, setApiKeys] = useState<ApiKeyRow[]>([])
  const [audit, setAudit] = useState<AuditEntry[]>([])
  const [loadingApiKeys, setLoadingApiKeys] = useState(false)
  const [loadingAudit, setLoadingAudit] = useState(false)
  const [loadingCreate, setLoadingCreate] = useState(false)
  const [secret, setSecret] = useState('')
  const [secretOpen, setSecretOpen] = useState(false)
  const [routerState, setRouterState] = useState<Loadable<ModelRouterConfig | null>>({ status: 'loading' })
  const [loadingRouter, setLoadingRouter] = useState(false)
  const [healthState, setHealthState] = useState<Loadable<HealthDetailed | null>>({ status: 'loading' })
  const [loadingHealth, setLoadingHealth] = useState(false)
  const [form] = Form.useForm()
  const keyReqIdRef = useRef(0)
  const routerReqIdRef = useRef(0)
  const healthReqIdRef = useRef(0)

  /* ── Authed key/audit calls: Bearer master key, per call ── */

  const surfaceFailure = (e: unknown) => {
    if (e instanceof ApiError) {
      if (e.status === 401) {
        message.error(t('settings.unauthorized'))
      } else {
        message.error(t('settings.request_failed', { status: e.status }))
      }
    }
    // Network-level failures stay silent here, as before.
  }

  const requireKey = (key: string): boolean => {
    if (!key.trim()) {
      message.warning(t('settings.connect_first'))
      return false
    }
    return true
  }

  const loadApiKeys = async (key = masterKey) => {
    if (!requireKey(key)) return
    const reqId = ++keyReqIdRef.current
    setLoadingApiKeys(true)
    try {
      const data = await getJson<{ api_keys?: unknown }>('/api/v1/api-keys', { token: key })
      const rows = Array.isArray(data.api_keys) ? (data.api_keys as ApiKeyRow[]) : []
      if (reqId === keyReqIdRef.current) setApiKeys(rows)
    } catch (e) {
      surfaceFailure(e)
    } finally {
      if (reqId === keyReqIdRef.current) setLoadingApiKeys(false)
    }
  }

  const loadAudit = async () => {
    if (!requireKey(masterKey)) return
    setLoadingAudit(true)
    try {
      const data = await getJson<{ audit?: unknown }>('/api/v1/audit?limit=20', { token: masterKey })
      const rows = Array.isArray(data.audit) ? (data.audit as AuditEntry[]) : []
      setAudit(rows)
    } catch (e) {
      surfaceFailure(e)
    } finally {
      setLoadingAudit(false)
    }
  }

  const revokeKey = async (id: string) => {
    if (!requireKey(masterKey)) return
    try {
      await deleteJson(`/api/v1/api-keys/${encodeURIComponent(id)}`, { token: masterKey })
      message.success(t('settings.key_revoked'))
      loadApiKeys()
    } catch (e) {
      surfaceFailure(e)
    }
  }

  const createKey = async (name: string) => {
    if (!requireKey(masterKey)) return
    setLoadingCreate(true)
    try {
      const data = await postJson<{ secret?: unknown }>(
        `/api/v1/api-keys?name=${encodeURIComponent(name)}`,
        undefined,
        { token: masterKey },
      )
      setSecret(typeof data.secret === 'string' ? data.secret : '')
      setSecretOpen(true)
      message.success(t('settings.key_created'))
      form.resetFields()
      loadApiKeys()
    } catch (e) {
      surfaceFailure(e)
    } finally {
      setLoadingCreate(false)
    }
  }

  const connect = () => {
    if (!masterKey.trim()) {
      message.warning(t('settings.enter_master_key'))
      return
    }
    // master key changed: invalidate any in-flight key-list requests and clear stale rows
    keyReqIdRef.current += 1
    setApiKeys([])
    loadApiKeys()
  }

  /* ── Unauthenticated reads: model router + detailed health ─ */

  const loadRouter = useCallback(async () => {
    const reqId = ++routerReqIdRef.current
    setLoadingRouter(true)
    setRouterState({ status: 'loading' })
    try {
      const raw = await getJson<unknown>('/api/v1/model-router')
      if (reqId !== routerReqIdRef.current) return
      setRouterState({
        status: 'ready',
        data: raw && typeof raw === 'object' ? (raw as ModelRouterConfig) : null,
      })
    } catch (e) {
      if (reqId !== routerReqIdRef.current) return
      setRouterState({ status: 'error', detail: e instanceof Error ? e.message : String(e) })
    } finally {
      if (reqId === routerReqIdRef.current) setLoadingRouter(false)
    }
  }, [])

  const loadHealth = useCallback(async () => {
    const reqId = ++healthReqIdRef.current
    setLoadingHealth(true)
    setHealthState({ status: 'loading' })
    try {
      const raw = await getJson<unknown>('/api/v1/health/detailed')
      if (reqId !== healthReqIdRef.current) return
      setHealthState({
        status: 'ready',
        data: raw && typeof raw === 'object' ? (raw as HealthDetailed) : null,
      })
    } catch (e) {
      if (reqId !== healthReqIdRef.current) return
      setHealthState({ status: 'error', detail: e instanceof Error ? e.message : String(e) })
    } finally {
      if (reqId === healthReqIdRef.current) setLoadingHealth(false)
    }
  }, [])

  useEffect(() => {
    // Do not auto-fetch the key list without a management key. Router and
    // detailed health use the global in-memory access token when auth is on.
    void loadRouter()
    void loadHealth()
  }, [loadRouter, loadHealth])

  const taskEntries = Object.entries(
    (routerState.status === 'ready' && routerState.data?.tasks) || {},
  )

  const componentEntries = Object.entries(
    (healthState.status === 'ready' && healthState.data?.components) || {},
  ).sort(([a], [b]) => a.localeCompare(b))

  const columns: ColumnsType<ApiKeyRow> = [
    {
      title: t('settings.id'),
      dataIndex: 'id',
      key: 'id',
      render: (id: string) => <span className="mono settings-cell-mono">{id}</span>,
    },
    {
      title: t('settings.prefix'),
      dataIndex: 'prefix',
      key: 'prefix',
      render: (prefix: string) => <span className="mono settings-cell-mono">{prefix}</span>,
    },
    { title: t('settings.name'), dataIndex: 'name', key: 'name' },
    { title: t('settings.created_at'), dataIndex: 'created_at', key: 'created_at' },
    {
      title: t('settings.status'),
      dataIndex: 'revoked',
      key: 'revoked',
      render: (revoked: boolean) =>
        revoked ? (
          <Tag className="signal-tag signal-failed" icon={<CloseCircleFilled aria-hidden="true" />}>
            {t('settings.revoked')}
          </Tag>
        ) : (
          <Tag className="signal-tag signal-verified" icon={<CheckCircleFilled aria-hidden="true" />}>
            {t('settings.active')}
          </Tag>
        ),
    },
    {
      title: t('settings.action'),
      key: 'action',
      render: (_, record) => (
        <Popconfirm
          title={t('settings.revoke_confirm')}
          onConfirm={() => revokeKey(record.id)}
          okText={t('settings.yes')}
          cancelText={t('settings.no')}
        >
          <Button danger size="small" disabled={record.revoked}>{t('settings.revoke')}</Button>
        </Popconfirm>
      ),
    },
  ]

  const auditColumns: ColumnsType<AuditEntry> = [
    {
      title: t('settings.id'),
      dataIndex: 'id',
      key: 'id',
      render: (id: string) => <span className="mono settings-cell-mono">{id}</span>,
    },
    { title: t('settings.action'), dataIndex: 'action', key: 'action' },
    { title: t('settings.user'), dataIndex: 'user', key: 'user' },
    {
      title: t('settings.timestamp'),
      dataIndex: 'timestamp',
      key: 'timestamp',
      render: (ts: string) => <span className="mono settings-cell-mono">{ts}</span>,
    },
    { title: t('settings.details'), dataIndex: 'details', key: 'details' },
  ]

  return (
    <div className="settings-page">
      <PageHeader
        title={t('nav.settings')}
        description={t('settings.subtitle')}
      />

      {/* 外观与语言 */}
      <section className="desk-card" aria-label={t('settings.section_appearance')}>
        <h3 className="section-label">{t('settings.section_appearance')}</h3>
        <div className="settings-language-row">
          <span className="settings-language-label">{t('settings.language')}</span>
          <Segmented
            aria-label={t('common.language')}
            options={[
              { label: '中文', value: 'zh' },
              { label: 'English', value: 'en' },
            ]}
            value={i18n.language?.startsWith('zh') ? 'zh' : 'en'}
            onChange={(v) => void persistLanguage(String(v) === 'zh' ? 'zh' : 'en')}
          />
        </div>
      </section>

      {/* 访问与密钥 */}
      <section className="desk-card" aria-label={t('settings.section_access')}>
        <h3 className="section-label">{t('settings.section_access')}</h3>
        <p className="settings-note">{t('settings.byok_note')}</p>

        <div className="settings-connect-row">
          <Input.Password
            placeholder={t('settings.master_key')}
            value={masterKey}
            onChange={(e) => setMasterKey(e.target.value)}
            onPressEnter={connect}
            autoComplete="off"
          />
          <Button type="primary" onClick={connect}>{t('settings.connect')}</Button>
        </div>

        <Spin spinning={loadingApiKeys}>
          <div className="settings-table-scroll">
            <Table dataSource={apiKeys} columns={columns} rowKey="id" pagination={false} size="small" />
          </div>
        </Spin>

        <Form form={form} layout="inline" className="settings-create-form"
          onFinish={(values) => createKey((values as { name: string }).name)}>
          <Form.Item name="name" rules={[{ required: true, message: t('settings.key_name_required') }]}>
            <Input placeholder={t('settings.key_name')} />
          </Form.Item>
          <Form.Item>
            <Button type="primary" htmlType="submit" loading={loadingCreate} disabled={!masterKey.trim()}>
              {t('settings.create_key')}
            </Button>
          </Form.Item>
        </Form>
      </section>

      {/* 模型路由（只读展示） */}
      <section className="desk-card" aria-label={t('settings.section_routing')}>
        <div className="settings-section-head">
          <h3 className="section-label">{t('settings.section_routing')}</h3>
          <Button
            size="small"
            icon={<ReloadOutlined aria-hidden="true" />}
            onClick={loadRouter}
            loading={loadingRouter}
          >
            {t('common.refresh')}
          </Button>
        </div>

        {routerState.status === 'loading' && (
          <div className="settings-loading"><Spin /></div>
        )}
        {routerState.status === 'error' && (
          <ErrorState title={t('settings.router_load_failed')} detail={routerState.detail} onRetry={loadRouter} />
        )}
        {routerState.status === 'ready' && !routerState.data && (
          <EmptyState icon={<SettingOutlined />} title={t('settings.router_empty')} />
        )}
        {routerState.status === 'ready' && routerState.data && (
          <div className="settings-kv">
            <div className="settings-kv-row">
              <span className="mono settings-kv-key">enabled</span>
              {routerState.data.enabled ? (
                <Tag className="signal-tag signal-verified" icon={<CheckCircleFilled aria-hidden="true" />}>
                  {t('settings.router_enabled')}
                </Tag>
              ) : (
                <Tag className="signal-tag signal-neutral" icon={<MinusCircleOutlined aria-hidden="true" />}>
                  {t('settings.router_disabled')}
                </Tag>
              )}
            </div>
            <div className="settings-kv-row">
              <span className="mono settings-kv-key">default_model</span>
              <span className="mono settings-kv-value">{routerState.data.default_model || '—'}</span>
            </div>
            <p className="settings-note">{t('settings.router_default_note')}</p>
            {taskEntries.length === 0 ? (
              <p className="settings-note">{t('settings.router_no_tasks')}</p>
            ) : (
              taskEntries.map(([task, cfg]) => (
                <div key={task} className="settings-kv-row">
                  <span className="mono settings-kv-key">{task}</span>
                  <span className="settings-kv-task">
                    <span className="mono settings-kv-env">{cfg?.env || '—'}</span>
                    <span aria-hidden="true">→</span>
                    <span className="mono settings-kv-value">{cfg?.model || '—'}</span>
                  </span>
                </div>
              ))
            )}
          </div>
        )}
      </section>

      {/* 系统健康 */}
      <section className="desk-card" aria-label={t('settings.section_system')}>
        <div className="settings-section-head">
          <h3 className="section-label">{t('settings.section_system')}</h3>
          <Button
            size="small"
            icon={<ReloadOutlined aria-hidden="true" />}
            onClick={loadHealth}
            loading={loadingHealth}
          >
            {t('common.refresh')}
          </Button>
        </div>

        {healthState.status === 'loading' && (
          <div className="settings-loading"><Spin /></div>
        )}
        {healthState.status === 'error' && (
          <ErrorState title={t('settings.system_load_failed')} detail={healthState.detail} onRetry={loadHealth} />
        )}
        {healthState.status === 'ready' && !healthState.data && (
          <EmptyState icon={<SettingOutlined />} title={t('settings.system_empty')} />
        )}
        {healthState.status === 'ready' && healthState.data && (
          <div className="settings-kv">
            {healthState.data.service && (
              <div className="settings-kv-row">
                <span className="settings-kv-key">{t('settings.system_service')}</span>
                <span className="mono settings-kv-value">{healthState.data.service}</span>
              </div>
            )}
            {healthState.data.status && (
              <div className="settings-kv-row">
                <span className="settings-kv-key">{t('settings.status')}</span>
                <HealthValueTag value={healthState.data.status} />
              </div>
            )}
            {componentEntries.length === 0 && !healthState.data.status && !healthState.data.service ? (
              <EmptyState icon={<SettingOutlined />} title={t('settings.system_empty')} />
            ) : (
              componentEntries.map(([name, value]) => (
                <div key={name} className="settings-kv-row">
                  <span className="mono settings-kv-key">{name}</span>
                  <HealthValueTag value={String(value)} />
                </div>
              ))
            )}
          </div>
        )}
      </section>

      {/* 审计日志 */}
      <section className="desk-card" aria-label={t('settings.section_audit')}>
        <div className="settings-section-head">
          <h3 className="section-label">{t('settings.section_audit')}</h3>
          <Button
            size="small"
            icon={<ReloadOutlined aria-hidden="true" />}
            onClick={loadAudit}
            loading={loadingAudit}
            disabled={!masterKey.trim()}
          >
            {t('settings.refresh_audit')}
          </Button>
        </div>

        <Spin spinning={loadingAudit}>
          <div className="settings-table-scroll">
            <Table dataSource={audit} columns={auditColumns} rowKey="id" pagination={false} size="small" />
          </div>
        </Spin>
      </section>

      <Modal
        open={secretOpen}
        title={t('settings.new_api_key')}
        onCancel={() => setSecretOpen(false)}
        footer={[
          <Button
            key="copy"
            icon={<CopyOutlined />}
            onClick={async () => {
              // Clipboard API is not always available (insecure contexts); fall back to
              // execCommand. Only report success when a copy path actually ran.
              let copied = false
              if (navigator.clipboard?.writeText) {
                try {
                  await navigator.clipboard.writeText(secret)
                  copied = true
                } catch {
                  copied = false
                }
              }
              if (!copied) {
                const ta = document.createElement('textarea')
                ta.value = secret
                ta.setAttribute('readonly', '')
                ta.style.position = 'fixed'
                ta.style.left = '-9999px'
                document.body.appendChild(ta)
                ta.select()
                try {
                  copied = document.execCommand('copy')
                } finally {
                  document.body.removeChild(ta)
                }
              }
              if (copied) {
                message.success(t('settings.copied'))
              } else {
                message.error(t('settings.copy_failed'))
              }
            }}
          >
            {t('settings.copy')}
          </Button>,
          <Button key="close" onClick={() => setSecretOpen(false)}>{t('settings.close')}</Button>,
        ]}
      >
        <Alert
          message={t('settings.copy_once')}
          description={t('settings.secret_warning')}
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
        />
        <pre className="mono settings-secret">{secret}</pre>
      </Modal>
    </div>
  )
}

export default SettingsPage

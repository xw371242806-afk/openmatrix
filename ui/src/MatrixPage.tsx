import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Alert, Button, Input, Form, Spin, Switch, message, Typography, Card, Space, Tag } from 'antd'
import { RobotOutlined, DatabaseOutlined, CheckCircleOutlined } from '@ant-design/icons'
import { getJson, runMatrixPipeline } from './api/client'
import type { MatrixEnvelope, MatrixRunResult, MatrixStatus } from './api/client'
import PageHeader from './components/PageHeader'
import ModelProviderPicker from './components/ModelProviderPicker'
import type { ModelProviderValue } from './components/ModelProviderPicker'
import './matrix.css'

const { Text, Paragraph } = Typography

export default function MatrixPage() {
  const { t } = useTranslation()
  const [loading, setLoading] = useState(false)
  const [trace, setTrace] = useState<string[]>([])
  const [result, setResult] = useState<MatrixRunResult | null>(null)
  const [status, setStatus] = useState<MatrixStatus | null>(null)
  const requestIdRef = useRef(0)
  const [provider, setProvider] = useState<ModelProviderValue>({
    apiKey: '',
    baseUrl: 'http://127.0.0.1:3000/v1',
    modelName: 'gpt-4o-mini',
  })
  const [useOwnProvider, setUseOwnProvider] = useState(false)
  
  const [form] = Form.useForm()

  useEffect(() => {
    let cancelled = false
    void getJson<MatrixStatus>('/api/matrix/status')
      .then((next) => {
        if (!cancelled) setStatus(next)
      })
      .catch(() => {
        if (!cancelled) setStatus(null)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const handleRun = async (values: {
    query: string
    index: string
  }) => {
    const serverProviderAvailable = status?.server_llm_configured === true && !useOwnProvider
    if (!serverProviderAvailable && (!provider.baseUrl.trim() || !provider.modelName.trim() || !provider.apiKey.trim())) {
      message.error(t('batch.provider_required'))
      return
    }
    const requestId = ++requestIdRef.current
    setLoading(true)
    setTrace([])
    setResult(null)
    setTrace([t('matrix.submitting')])

    try {
      const res = serverProviderAvailable
        ? await runMatrixPipeline(values.query, values.index)
        : await runMatrixPipeline(values.query, values.index, provider)
      if (requestId !== requestIdRef.current) return
      setTrace([
        t('matrix.compiled'),
        t('matrix.recalled', { count: res.processed_chunks }),
        t('matrix.shared_count', { count: res.shared_board_findings.length }),
        t('matrix.gates_returned'),
      ])
      setResult(res)
    } catch (err: unknown) {
      if (requestId !== requestIdRef.current) return
      const detail = err instanceof Error ? err.message : String(err)
      setTrace([])
      message.error(t('matrix.failed', { detail }))
    } finally {
      if (requestId === requestIdRef.current) setLoading(false)
    }
  }

  return (
    <div className="matrix-container">
      <PageHeader title={t('matrix.title')} description={t('matrix.description')} />

      <Tag color="gold" style={{ marginBottom: 12 }}>{t('matrix.experimental')}</Tag>

      {status && !status.ready && (
        <Alert
          type="warning"
          showIcon
          title={t('matrix.config_warning')}
          description={<>{t('matrix.config_description')} <a href="/docs/MATRIX-DEEP-DIVE-SETUP.md" target="_blank" rel="noreferrer">{t('matrix.setup_link')}</a></>}
          style={{ marginBottom: 16 }}
        />
      )}

      <Card>
        <Form form={form} layout="vertical" onFinish={handleRun} initialValues={{ index: 'default_matrix' }}>
          <Form.Item label={t('matrix.index_label')} name="index" rules={[{ required: true }]}>
            <Input prefix={<DatabaseOutlined />} placeholder={t('matrix.index_placeholder')} />
          </Form.Item>
          <Form.Item label={t('matrix.query_label')} name="query" rules={[{ required: true }]}>
            <Input.TextArea rows={3} placeholder={t('matrix.query_placeholder')} />
          </Form.Item>
          {status?.server_llm_configured && (
            <div className="matrix-provider-switch">
              <Switch checked={useOwnProvider} onChange={setUseOwnProvider} aria-label={t('matrix.use_own_provider')} />
              <span>{t('matrix.use_own_provider')}</span>
            </div>
          )}
          {(!status?.server_llm_configured || useOwnProvider) && (
            <ModelProviderPicker
              value={provider}
              onChange={setProvider}
              apiKeyLabel={t('matrix.api_key_label')}
              baseUrlLabel={t('matrix.base_url_label')}
              modelNameLabel={t('matrix.model_label')}
            />
          )}
          {status?.server_llm_configured && !useOwnProvider && <Typography.Text type="secondary">{t('matrix.server_provider_note')}</Typography.Text>}
          <Form.Item>
            <Button type="primary" htmlType="submit" icon={<RobotOutlined aria-hidden="true" />} loading={loading} disabled={!status?.ready} size="large">
              {loading ? t('matrix.executing') : t('matrix.launch')}
            </Button>
          </Form.Item>
        </Form>
      </Card>

      {trace.length > 0 && (
        <div className="matrix-trace-card">
          {trace.map((t, i) => (
            <div key={i} className="trace-step">
              <Spin size="small" style={{ marginRight: 8, visibility: loading ? 'visible' : 'hidden' }} />
              {t} {(!loading || i < trace.length - 1) && <CheckCircleOutlined style={{ color: '#52c41a' }} />}
            </div>
          ))}
        </div>
      )}

      {result && result.envelope && (() => {
        const envelope = result.envelope as MatrixEnvelope
        return (
        <div className="envelope-card">
          <div className="envelope-section claims-sec">
            <h3>{t('matrix.claims')}</h3>
            <ul className="findings-list">
              {envelope.CLAIMS?.map((c, i) => <li key={i}>{c}</li>)}
            </ul>
            {typeof result.envelope === 'string' && <Paragraph>{result.envelope}</Paragraph>}
          </div>
          
          {envelope.UNKNOWNS && (
            <div className="envelope-section unknowns-sec">
              <h3>{t('matrix.unknowns')}</h3>
              <ul className="findings-list">
                {envelope.UNKNOWNS.map((c, i) => <li key={i}>{c}</li>)}
              </ul>
            </div>
          )}

          {envelope.GATES && (
            <div className="envelope-section gates-sec">
              <h3>{t('matrix.gates')}</h3>
              <Space wrap>
                {Object.entries(envelope.GATES).map(([k, v]) => (
                  <Tag color={v === 'PASS' ? 'green' : 'orange'} key={k}>{k}: {v}</Tag>
                ))}
              </Space>
            </div>
          )}

          <div className={`envelope-section ${envelope.VERDICT === 'GO' ? 'verdict-go' : 'verdict-sec'}`}>
            <h3>{t('matrix.verdict')}</h3>
            <Text strong style={{ fontSize: 18 }}>{envelope.VERDICT ?? 'COMPLETED'}</Text>
          </div>
        </div>
        )
      })()}
      
      {result && result.xeno_ir && (
        <Card title={t('matrix.diagnostics')} size="small" style={{ marginTop: 24, background: '#fafafa' }}>
          <Paragraph><strong>{t('matrix.xeno_ir')}:</strong> <code>{result.xeno_ir}</code></Paragraph>
          <Paragraph><strong>{t('matrix.processed_chunks')}:</strong> {result.processed_chunks}</Paragraph>
          <Paragraph><strong>{t('matrix.shared_findings')}:</strong> {result.shared_board_findings?.length || 0}</Paragraph>
        </Card>
      )}
    </div>
  )
}

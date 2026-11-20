import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Collapse, Flex, Input, Modal, Segmented, Select, Switch, Tag, Typography, message } from 'antd'
import { CheckCircleFilled, DeleteOutlined, InboxOutlined, PlusOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { EXPERT_PRESETS } from './expertPresets'
import { ApiError, downloadFile, postForm, setAccessToken } from './api/client'
import type { AnalysisCellData, AnalyzeBatchResponse, AnalyzeChainResponse } from './api/types'
import PageHeader from './components/PageHeader'
import EmptyState from './components/EmptyState'
import ErrorState from './components/ErrorState'
import EvidenceCard from './components/EvidenceCard'
import ModelProviderPicker from './components/ModelProviderPicker'
import { TaskStatusTag } from './components/StatusTag'
import './batch.css'

const { Paragraph } = Typography

interface ChainRow {
  id: string
  question: string
  deps: string[]
}

type AnalysisMode = 'batch' | 'chain'

/** Preserves the original DSL semantics: `id | question | deps=a,b`,
 *  pipes inside questions survive the round-trip. */
function parseChainSteps(text: string): ChainRow[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line, index) => {
      const parts = line.split('|').map((part) => part.trim())
      const rawId = parts[0] || `step_${index + 1}`
      const lastPart = parts[parts.length - 1] ?? ''
      const rawDeps = lastPart.toLowerCase().startsWith('deps=') ? parts.pop()?.slice(5).trim() ?? '' : ''
      const rawQuestion = parts.slice(1).join(' | ') || rawId
      const deps = rawDeps.split(',').map((dep) => dep.trim()).filter(Boolean)
      return { id: rawId, question: rawQuestion, deps }
    })
}

function serializeChainRows(rows: ChainRow[]): string {
  return rows
    .map((row) => {
      const segments = [row.id || '', row.question]
      if (row.deps.length) segments.push(`deps=${row.deps.join(',')}`)
      return segments.filter((segment, i) => i < 2 || segment !== '').join(' | ')
    })
    .join('\n')
}

interface DropzoneProps {
  ariaLabel: string
  hint: string
  multiple?: boolean
  onFiles: (files: File[]) => void
}

/** Styled label over a real, focusable file input — keyboard and
 *  screen-reader semantics stay native; drag-and-drop is additive. */
function Dropzone({ ariaLabel, hint, multiple, onFiles }: DropzoneProps) {
  const [over, setOver] = useState(false)
  return (
    <label
      className={`dropzone${over ? ' dropzone--over' : ''}`}
      onDragOver={(e) => {
        e.preventDefault()
        setOver(true)
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault()
        setOver(false)
        const picked = Array.from(e.dataTransfer.files ?? []).filter((f) =>
          f.name.toLowerCase().endsWith('.pdf'),
        )
        if (picked.length) onFiles(multiple ? picked : picked.slice(0, 1))
      }}
    >
      <InboxOutlined className="dropzone-icon" aria-hidden="true" />
      <span className="dropzone-hint">{hint}</span>
      <input
        type="file"
        className="dropzone-input"
        accept="application/pdf,.pdf"
        multiple={multiple}
        aria-label={ariaLabel}
        onChange={(e) => {
          const picked = Array.from(e.currentTarget.files ?? [])
          if (picked.length) onFiles(picked)
          e.currentTarget.value = ''
        }}
      />
    </label>
  )
}

const DEFAULT_CHAIN_ROWS: ChainRow[] = [
  { id: 'revenue', question: 'What was total revenue?', deps: [] },
  { id: 'margin', question: 'What margin is implied by the revenue answer?', deps: ['revenue'] },
]

export default function BatchPage() {
  const { t } = useTranslation()
  const [mode, setMode] = useState<AnalysisMode>('batch')

  const [files, setFiles] = useState<File[]>([])
  const [chainFile, setChainFile] = useState<File | null>(null)
  const [questions, setQuestions] = useState('')
  const [token, setToken] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [modelName, setModelName] = useState('')

  const [loading, setLoading] = useState(false)
  const [chainLoading, setChainLoading] = useState(false)
  const [runError, setRunError] = useState<string | null>(null)
  const [exporting, setExporting] = useState<'xlsx' | 'csv' | 'pdf' | 'docx' | 'pptx' | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)

  const [result, setResult] = useState<AnalyzeBatchResponse | null>(null)
  const [chainResult, setChainResult] = useState<AnalyzeChainResponse | null>(null)

  const [chainRows, setChainRows] = useState<ChainRow[]>(DEFAULT_CHAIN_ROWS)
  const [useKg, setUseKg] = useState(false)
  const [indexKg, setIndexKg] = useState(false)
  const [kgTaskId, setKgTaskId] = useState<string | null>(null)
  const [kgTaskStatus, setKgTaskStatus] = useState<string | null>(null)
  const kgTimerRef = useRef<number | null>(null)

  useEffect(() => {
    return () => {
      if (kgTimerRef.current !== null) window.clearTimeout(kgTimerRef.current)
    }
  }, [])

  const questionCount = useMemo(
    () => questions.split('\n').map((q) => q.trim()).filter(Boolean).length,
    [questions],
  )

  const chainDsl = useMemo(() => serializeChainRows(chainRows), [chainRows])

  const grouped = useMemo(() => {
    const groups = new Map<string, AnalysisCellData[]>()
    for (const cell of result?.data ?? []) {
      const name = cell.document_name || t('batch.document')
      groups.set(name, [...(groups.get(name) ?? []), cell])
    }
    return Array.from(groups.entries())
  }, [result, t])

  const chainGrouped = useMemo(() => {
    const groups = new Map<string, AnalysisCellData[]>()
    for (const cell of chainResult?.data ?? []) {
      const name = cell.document_name || t('batch.document')
      groups.set(name, [...(groups.get(name) ?? []), cell])
    }
    return Array.from(groups.entries())
  }, [chainResult, t])

  const providerReady = apiKey.trim() !== '' && baseUrl.trim() !== '' && modelName.trim() !== ''
  const tokenOpts = token.trim() ? { token: token.trim() } : {}

  /* Guided readiness: each requirement reports done/todo with a plain
     reason, so the user always knows what to do next — no dead clicks. */
  const readiness = useMemo(() => {
    if (mode === 'batch') {
      return [
        {
          ok: files.length > 0,
          done: t('guide.files_done', { count: files.length }),
          todo: t('guide.files_todo'),
        },
        {
          ok: questionCount > 0,
          done: t('guide.questions_done', { count: questionCount }),
          todo: t('guide.questions_todo'),
        },
        {
          ok: providerReady,
          done: t('guide.model_done'),
          todo: t('guide.model_todo'),
        },
      ]
    }
    return [
      {
        ok: !!chainFile,
        done: t('guide.chain_file_done'),
        todo: t('guide.chain_file_todo'),
      },
      {
        ok: chainRows.length > 0 && chainRows.every((row) => row.id.trim() && row.question.trim()),
        done: t('guide.chain_steps_done', { count: chainRows.length }),
        todo: t('guide.chain_steps_todo'),
      },
      {
        ok: providerReady,
        done: t('guide.model_done'),
        todo: t('guide.model_todo'),
      },
    ]
  }, [mode, files.length, questionCount, providerReady, chainFile, chainRows, t])

  const allReady = readiness.every((item) => item.ok)

  const pollKgTask = (taskId: string) => {
    void fetch(`/api/v1/tasks/${encodeURIComponent(taskId)}`, { headers: token.trim() ? { Authorization: `Bearer ${token.trim()}` } : undefined })
      .then(async (res) => {
        const data = await res.json().catch(() => null) as { status?: string } | null
        if (res.ok && data && typeof data.status === 'string') {
          setKgTaskStatus(data.status)
          if (data.status === 'queued' || data.status === 'running') {
            kgTimerRef.current = window.setTimeout(() => pollKgTask(taskId), 2000)
          }
        } else {
          setKgTaskStatus('failed')
        }
      })
      .catch(() => setKgTaskStatus('failed'))
  }

  const runBatch = async () => {
    if (!files.length) { message.error(t('batch.upload_pdf')); return }
    const questionList = questions.split('\n').map((q) => q.trim()).filter(Boolean)
    if (!questionList.length) { message.error(t('batch.add_question')); return }
    if (!providerReady) { message.error(t('batch.provider_required')); return }
    const body = new FormData()
    for (const file of files) body.append('files', file)
    body.append('questions', JSON.stringify(questionList))
    body.append('max_concurrency', '5')
    body.append('api_key', apiKey.trim())
    body.append('base_url', baseUrl.trim())
    body.append('model_name', modelName.trim())
    setLoading(true)
    setRunError(null)
    try {
      setResult(await postForm<AnalyzeBatchResponse>('/api/v1/analyze-batch', body, tokenOpts))
    } catch (e) {
      setRunError((e as Error).message)
      message.error((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  const runChain = async () => {
    if (!chainFile) { message.error(t('batch.upload_chain_pdf')); return }
    if (!chainRows.length || chainRows.some((row) => !row.id.trim() || !row.question.trim())) {
      message.error(t('batch.add_chain_step'))
      return
    }
    if (!providerReady) { message.error(t('batch.provider_required')); return }
    const body = new FormData()
    body.append('file', chainFile)
    body.append('steps', JSON.stringify(chainRows.map((row) => ({ id: row.id.trim(), question: row.question, depends_on: row.deps }))))
    body.append('use_kg', String(useKg))
    body.append('index_kg', String(indexKg))
    body.append('api_key', apiKey.trim())
    body.append('base_url', baseUrl.trim())
    body.append('model_name', modelName.trim())
    setChainLoading(true)
    setRunError(null)
    setKgTaskId(null)
    setKgTaskStatus(null)
    try {
      const chain = await postForm<AnalyzeChainResponse>('/api/v1/analyze-chain', body, tokenOpts)
      setChainResult(chain)
      if (chain.kg_index_task_id) {
        setKgTaskId(chain.kg_index_task_id)
        pollKgTask(chain.kg_index_task_id)
      }
    } catch (e) {
      setRunError((e as Error).message)
      message.error((e as Error).message)
    } finally {
      setChainLoading(false)
    }
  }

  const downloadExport = async (format: 'xlsx' | 'csv' | 'pdf' | 'docx' | 'pptx') => {
    if (!result?.run_id) return
    setExporting(format)
    try {
      await downloadFile(
        `/api/v1/export-${format}/${encodeURIComponent(result.run_id)}`,
        `matrix_${result.run_id}.${format}`,
        tokenOpts,
      )
    } catch (e) {
      message.error(t('batch.export_failed', { status: (e as ApiError).status ?? '-' }))
    } finally {
      setExporting(null)
    }
  }

  const updateRow = (index: number, patch: Partial<ChainRow>) => {
    setChainRows((rows) => rows.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  }

  const cellBlock = (cell: AnalysisCellData, key: string) => (
    <div className="cell-block" key={key}>
      <div className="cell-question">{cell.question}</div>
      <Paragraph className="cell-answer">{cell.answer || cell.error || t('batch.no_answer')}</Paragraph>
      {cell.error && cell.answer ? <div className="cell-error">{cell.error}</div> : null}
      <div className="dep-chips">
        {cell.step_id ? <Tag className="mono">{cell.step_id}</Tag> : null}
        {cell.depends_on?.length ? (
          <Tag>{t('batch.depends_on', { steps: cell.depends_on.join(', ') })}</Tag>
        ) : null}
        <EvidenceCard
          filename={cell.document_name || t('batch.document')}
          page={cell.page ?? null}
          quote={cell.quote ?? null}
          status={cell.citation_status ?? null}
        />
      </div>
    </div>
  )

  return (
    <div className="analysis-page">
      <PageHeader title={t('nav.analysis')} description={t('batch.page_subtitle')} />

      <Segmented
        className="analysis-mode"
        value={mode}
        onChange={(value) => setMode(String(value) as AnalysisMode)}
        aria-label={t('batch.mode_aria')}
        options={[
          { value: 'batch', label: t('batch.mode_batch') },
          { value: 'chain', label: t('batch.mode_chain') },
        ]}
      />

      <div className="analysis-grid">
        <section className="desk-card analysis-setup">
          {mode === 'batch' ? (
            <>
              <div className="section-label">{t('batch.pdf_files')}</div>
              <Dropzone
                ariaLabel={t('batch.pdf_files_aria')}
                hint={t('batch.dropzone_hint')}
                multiple
                onFiles={(picked) => setFiles((prev) => [...prev, ...picked])}
              />
              {files.length > 0 && (
                <>
                  <div className="file-count">{t('batch.dropzone_count', { count: files.length })}</div>
                  <div className="file-chips">
                    {files.map((file, i) => (
                      <Tag
                        key={`${file.name}-${i}`}
                        closable
                        onClose={() => setFiles((prev) => prev.filter((_, j) => j !== i))}
                      >
                        {file.name}
                      </Tag>
                    ))}
                  </div>
                </>
              )}
              <Alert
                type="warning"
                showIcon
                title={t('batch.expert_disclaimer')}
                style={{ marginTop: 16, marginBottom: 12 }}
              />
              <div className="section-label">{t('batch.expert_preset')}</div>
              <div className="preset-chips">
                {EXPERT_PRESETS.map((preset) => (
                  <button
                    key={preset.value}
                    type="button"
                    className="preset-chip"
                    aria-label={`${t('batch.expert_preset_aria')}: ${preset.label}`}
                    onClick={() =>
                      setQuestions((prev) => (prev ? `${prev}\n${preset.questions.join('\n')}` : preset.questions.join('\n')))
                    }
                  >
                    {preset.label}
                  </button>
                ))}
              </div>
              <div className="section-label" style={{ marginTop: 16 }}>{t('batch.questions')}</div>
              <Input.TextArea
                aria-label={t('batch.questions_aria')}
                rows={5}
                placeholder={t('batch.questions_placeholder')}
                value={questions}
                onChange={(e) => setQuestions(e.target.value)}
              />
            </>
          ) : (
            <>
              <div className="section-label">{t('batch.pdf_files')}</div>
              <Dropzone
                ariaLabel={t('batch.chain_pdf_aria')}
                hint={t('batch.chain_dropzone_hint')}
                onFiles={(picked) => setChainFile(picked[0] ?? null)}
              />
              {chainFile && <div className="file-count">{chainFile.name}</div>}
              <Alert
                type="info"
                showIcon
                title={t('batch.chain_help')}
                style={{ marginTop: 16, marginBottom: 12 }}
              />
              <div className="section-label">{t('batch.structured_editor')}</div>
              {chainRows.map((row, index) => (
                <div className="step-row" key={index}>
                  <Input
                    value={row.id}
                    aria-label={`${t('batch.step_id')} ${index + 1}`}
                    placeholder={t('batch.step_id')}
                    onChange={(e) => updateRow(index, { id: e.target.value })}
                  />
                  <Input
                    value={row.question}
                    aria-label={`${t('batch.step_question')} ${index + 1}`}
                    placeholder={t('batch.step_question')}
                    onChange={(e) => updateRow(index, { question: e.target.value })}
                  />
                  <Select
                    mode="multiple"
                    allowClear
                    value={row.deps}
                    aria-label={`${t('batch.step_deps')} ${index + 1}`}
                    placeholder={t('batch.step_deps_placeholder')}
                    options={chainRows.filter((_, i) => i !== index).map((r) => ({ value: r.id, label: r.id }))}
                    onChange={(deps: string[]) => updateRow(index, { deps })}
                  />
                  <Button
                    className="step-remove"
                    type="text"
                    icon={<DeleteOutlined aria-hidden="true" />}
                    aria-label={t('batch.remove_step', { index: index + 1 })}
                    onClick={() => setChainRows((rows) => rows.filter((_, i) => i !== index))}
                  />
                </div>
              ))}
              <Button
                type="dashed"
                icon={<PlusOutlined aria-hidden="true" />}
                onClick={() => setChainRows((rows) => [...rows, { id: `step_${rows.length + 1}`, question: '', deps: [] }])}
              >
                {t('batch.add_step')}
              </Button>
              <Collapse
                items={[{ key: 'dsl', label: t('batch.raw_dsl'), children: (
                  <Input.TextArea
                    aria-label={t('batch.chain_steps_aria')}
                    rows={4}
                    value={chainDsl}
                    onChange={(e) => setChainRows(parseChainSteps(e.target.value))}
                  />
                ) }]}
                style={{ marginTop: 12 }}
              />
              <div style={{ marginTop: 16 }}>
                <Flex vertical gap={12}>
                  <span>
                    <Switch aria-label={t('batch.kg_use_aria')} checked={useKg} onChange={setUseKg} />{' '}
                    <span className="field-note" style={{ marginTop: 0 }}>{t('batch.kg_use_label')} — {t('batch.kg_use_extra')}</span>
                  </span>
                  <span>
                    <Switch aria-label={t('batch.kg_index_aria')} checked={indexKg} onChange={setIndexKg} />{' '}
                    <span className="field-note" style={{ marginTop: 0 }}>{t('batch.kg_index_label')} — {t('batch.kg_index_extra')}</span>
                  </span>
                </Flex>
              </div>
            </>
          )}
        </section>

        <aside className="desk-card analysis-scope">
          <div className="section-label">{t('batch.scope_title')}</div>
          {mode === 'batch' ? (
            <>
              <div className="scope-row"><span className="scope-key">{t('batch.scope_files')}</span><span className="scope-val">{files.length}</span></div>
              <div className="scope-row"><span className="scope-key">{t('batch.scope_questions')}</span><span className="scope-val">{questionCount}</span></div>
              <div className="scope-row"><span className="scope-key">{t('batch.scope_cells')}</span><span className="scope-val">{files.length * questionCount}</span></div>
              <p className="scope-note">{t('batch.scope_note')}</p>
            </>
          ) : (
            <>
              <div className="scope-row"><span className="scope-key">{t('batch.scope_files')}</span><span className="scope-val">{chainFile ? 1 : 0}</span></div>
              <div className="scope-row"><span className="scope-key">{t('batch.scope_steps')}</span><span className="scope-val">{chainRows.length}</span></div>
              <p className="scope-note">{t('batch.chain_help')}</p>
            </>
          )}

          <div className="section-label" style={{ marginTop: 18 }}>{t('batch.model_access')}</div>
          <Input.Password
            aria-label={t('batch.bearer_token_aria')}
            placeholder={t('batch.bearer_placeholder')}
            value={token}
            onChange={(e) => {
              setToken(e.target.value)
              setAccessToken(e.target.value)
            }}
          />
          <p className="field-note">{t('batch.om_token_note')}</p>
          <ModelProviderPicker
            value={{ apiKey, baseUrl, modelName }}
            onChange={({ apiKey: nextKey, baseUrl: nextBaseUrl, modelName: nextModel }) => {
              setApiKey(nextKey)
              setBaseUrl(nextBaseUrl)
              setModelName(nextModel)
            }}
          />

          {/* Readiness checklist — always visible, updates as you fill in */}
          <ul className="guide-list" aria-label={t('guide.checklist_aria')}>
            {readiness.map((item) => (
              <li key={item.todo} className={`guide-item${item.ok ? ' guide-item--ok' : ''}`}>
                <CheckCircleFilled
                  className="guide-item-icon"
                  aria-hidden="true"
                />
                <span>{item.ok ? item.done : item.todo}</span>
              </li>
            ))}
          </ul>

          <Button
            type="primary"
            block
            disabled={!allReady}
            title={allReady ? undefined : t('guide.not_ready')}
            loading={mode === 'batch' ? loading : chainLoading}
            onClick={() => setConfirmOpen(true)}
            style={{ marginTop: 12 }}
          >
            {allReady ? t('guide.review_launch') : t('guide.not_ready')}
          </Button>
        </aside>
      </div>

      <Modal
        open={confirmOpen}
        onCancel={() => setConfirmOpen(false)}
        onOk={() => {
          setConfirmOpen(false)
          void (mode === 'batch' ? runBatch() : runChain())
        }}
        okText={mode === 'batch' ? t('batch.run_batch') : t('batch.run_chain')}
        cancelText={t('common.cancel')}
        title={t('guide.confirm_title')}
      >
        <ul className="guide-confirm">
          {mode === 'batch' ? (
            <>
              <li><span className="guide-confirm-key">{t('batch.scope_files')}</span><span className="mono">{files.length}</span></li>
              <li><span className="guide-confirm-key">{t('batch.scope_questions')}</span><span className="mono">{questionCount}</span></li>
              <li><span className="guide-confirm-key">{t('batch.scope_cells')}</span><span className="mono">{files.length * questionCount}</span></li>
              <li><span className="guide-confirm-key">{t('guide.confirm_model')}</span><span className="mono">{modelName}</span></li>
            </>
          ) : (
            <>
              <li><span className="guide-confirm-key">{t('batch.scope_files')}</span><span className="mono">1</span></li>
              <li><span className="guide-confirm-key">{t('batch.scope_steps')}</span><span className="mono">{chainRows.length}</span></li>
              <li><span className="guide-confirm-key">{t('guide.confirm_model')}</span><span className="mono">{modelName}</span></li>
              {(useKg || indexKg) && (
                <li><span className="guide-confirm-key">{t('guide.confirm_kg')}</span><span className="mono">{[useKg ? t('batch.kg_use_label') : '', indexKg ? t('batch.kg_index_label') : ''].filter(Boolean).join(' + ')}</span></li>
              )}
            </>
          )}
          <li className="guide-confirm-note">{t('guide.confirm_note')}</li>
        </ul>
      </Modal>

      {runError && (
        <ErrorState title={t('batch.run_error_title')} detail={runError} />
      )}

      {chainResult && (
        <section className="desk-card">
          <div className="result-head">
            <Flex gap={10} align="center" wrap="wrap">
              <span className="result-title">{t('batch.chain_run', { id: chainResult.run_id })}</span>
              <Tag className="mono">{chainResult.status}</Tag>
              <Tag>{t('batch.steps_count', { count: chainResult.steps_count })}</Tag>
            </Flex>
            <Button size="small" onClick={() => { window.location.hash = '/results' }}>
              {t('batch.view_results')}
            </Button>
          </div>
          {chainGrouped.map(([doc, cells]) => (
            <Collapse
              key={doc}
              defaultActiveKey={[doc]}
              items={[{
                key: doc,
                label: <span className="mono">{doc}</span>,
                children: cells.map((cell, i) => cellBlock(cell, `${doc}-${cell.step_id ?? ''}-${i}`)),
              }]}
            />
          ))}
          {kgTaskId && (
            <div className="kg-status" role="status">
              <span>{t('batch.kg_task', { id: kgTaskId })}</span>
              <TaskStatusTag value={kgTaskStatus} />
            </div>
          )}
        </section>
      )}

      {result && (
        <section className="desk-card">
          <div className="result-head">
            <Flex gap={10} align="center" wrap="wrap">
              <span className="result-title">{t('batch.run', { id: result.run_id })}</span>
              <Tag className="mono">{result.status}</Tag>
              <Tag>{t('batch.documents_count', { count: result.documents_count })}</Tag>
            </Flex>
            <div className="result-actions">
              <Button
                size="small"
                loading={exporting === 'xlsx'}
                onClick={() => void downloadExport('xlsx')}
              >
                {t('batch.btn_export_xlsx')}
              </Button>
              <Button
                size="small"
                loading={exporting === 'csv'}
                onClick={() => void downloadExport('csv')}
              >
                {t('batch.btn_export_csv')}
              </Button>
              <Button size="small" loading={exporting === 'pdf'} onClick={() => void downloadExport('pdf')}>
                {t('batch.btn_export_pdf')}
              </Button>
              <Button size="small" loading={exporting === 'docx'} onClick={() => void downloadExport('docx')}>
                {t('batch.btn_export_docx')}
              </Button>
              <Button size="small" loading={exporting === 'pptx'} onClick={() => void downloadExport('pptx')}>
                {t('batch.btn_export_pptx')}
              </Button>
              <Button size="small" onClick={() => { window.location.hash = '/results' }}>
                {t('batch.view_results')}
              </Button>
            </div>
          </div>
          {grouped.length === 0 ? (
            <EmptyState title={t('batch.empty_results')} />
          ) : (
            grouped.map(([doc, cells]) => (
              <Collapse
                key={doc}
                defaultActiveKey={[doc]}
                items={[{
                  key: doc,
                  label: <span className="mono">{doc}</span>,
                  children: cells.map((cell, i) => cellBlock(cell, `${doc}-${i}`)),
                }]}
              />
            ))
          )}
        </section>
      )}

      {!result && !chainResult && !runError && (
        <EmptyState title={t('batch.empty_results')} />
      )}
    </div>
  )
}

import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Button,
  Drawer,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Select,
  Spin,
  Switch,
  Table,
  Tabs,
  Tag,
  message,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  CheckCircleFilled,
  FolderOpenOutlined,
  PlusOutlined,
  SearchOutlined,
  ThunderboltOutlined,
  UploadOutlined,
} from '@ant-design/icons'
import PageHeader from './components/PageHeader'
import EmptyState from './components/EmptyState'
import ErrorState from './components/ErrorState'
import Unavailable from './components/Unavailable'
import EvidenceCard from './components/EvidenceCard'
import { deleteJson, getJson, postForm, postJson } from './api/client'
import type { DocumentRecord, ProjectRecord, SearchHit } from './api/types'
import type { RouteKey } from './lib/hashRouter'
import './projects.css'

type SearchMode = 'bm25' | 'dense' | 'fusion'

interface ProjectFormValues {
  project_id: string
  name: string
  description?: string
}

interface DocumentFormValues {
  doc_id: string
  filename: string
  content: string
  page_count?: number | null
  indexed?: boolean
}

function errorDetail(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

function formatDate(value: string | null | undefined, language: string): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(language)
}

export default function ProjectsPage({ navigate }: { navigate?: (key: RouteKey) => void }) {
  const { t, i18n } = useTranslation()

  const [projects, setProjects] = useState<ProjectRecord[]>([])
  const [projectsLoading, setProjectsLoading] = useState(true)
  const [projectsError, setProjectsError] = useState<string | null>(null)
  const [filter, setFilter] = useState('')

  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [documents, setDocuments] = useState<DocumentRecord[]>([])
  const [docLoading, setDocLoading] = useState(false)
  const [docError, setDocError] = useState<string | null>(null)

  const [question, setQuestion] = useState('')
  const [mode, setMode] = useState<SearchMode>('bm25')
  const [limit, setLimit] = useState(3)
  const [searched, setSearched] = useState(false)
  const [searchLoading, setSearchLoading] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const [hits, setHits] = useState<SearchHit[]>([])

  const [projectDrawerOpen, setProjectDrawerOpen] = useState(false)
  const [docDrawerOpen, setDocDrawerOpen] = useState(false)
  const [uploading, setUploading] = useState(false)
  const uploadInputRef = useRef<HTMLInputElement>(null)

  const [projectForm] = Form.useForm<ProjectFormValues>()
  const [documentForm] = Form.useForm<DocumentFormValues>()

  // Request-id refs: responses from a superseded project/search are discarded
  // so stale data can never leak into the currently selected project.
  const projectsReqRef = useRef(0)
  const docReqIdRef = useRef(0)
  const searchReqIdRef = useRef(0)
  const selectedIdRef = useRef<string | null>(null)

  const loadProjects = useCallback(async () => {
    const reqId = ++projectsReqRef.current
    setProjectsLoading(true)
    setProjectsError(null)
    try {
      const data = await getJson<{ projects?: unknown }>('/api/v1/projects')
      if (!Array.isArray(data.projects)) throw new Error('unexpected payload')
      if (reqId !== projectsReqRef.current) return
      setProjects(data.projects as ProjectRecord[])
      setProjectsLoading(false)
    } catch (e) {
      if (reqId !== projectsReqRef.current) return
      setProjectsError(errorDetail(e))
      setProjectsLoading(false)
    }
  }, [])

  const fetchDocuments = useCallback(async (id: string) => {
    const reqId = ++docReqIdRef.current
    setDocLoading(true)
    try {
      const data = await getJson<{ documents?: unknown }>(
        `/api/v1/projects/${encodeURIComponent(id)}/documents`,
      )
      const list = Array.isArray(data.documents) ? (data.documents as DocumentRecord[]) : []
      if (reqId !== docReqIdRef.current) return // stale - a newer project owns the document list
      setDocuments(list)
      setDocError(null)
      setDocLoading(false)
    } catch (e) {
      if (reqId !== docReqIdRef.current) return
      setDocError(errorDetail(e))
      setDocLoading(false)
    }
  }, [])

  const runSearch = useCallback(async (rawQuestion: string) => {
    const projectId = selectedIdRef.current
    const trimmed = rawQuestion.trim()
    if (!projectId || !trimmed) return
    const reqId = ++searchReqIdRef.current
    setSearched(true)
    setSearchLoading(true)
    setSearchError(null)
    try {
      const data = await postJson<{ hits?: unknown }>(
        `/api/v1/projects/${encodeURIComponent(projectId)}/search`,
        {
          question: trimmed,
          limit: Math.max(1, Math.min(100, Math.round(limit) || 3)),
          mode,
        },
      )
      const list = Array.isArray(data.hits) ? (data.hits as SearchHit[]) : []
      if (reqId !== searchReqIdRef.current) return // stale - a newer search owns the state
      setHits(list)
      setSearchLoading(false)
    } catch (e) {
      if (reqId !== searchReqIdRef.current) return
      setSearchError(errorDetail(e))
      setSearchLoading(false)
    }
  }, [limit, mode])

  useEffect(() => {
    void loadProjects()
  }, [loadProjects])

  useEffect(() => {
    // Keep the ref in sync with the committed selection so async mutation
    // handlers can check which project is currently selected.
    selectedIdRef.current = selectedId
    // Project switch: reset project-scoped state so documents/hits from a
    // previous project never leak into the newly selected one, and invalidate
    // any in-flight document/search requests.
    docReqIdRef.current += 1
    searchReqIdRef.current += 1
    setDocuments([])
    setHits([])
    setQuestion('')
    setSearched(false)
    setDocError(null)
    setSearchError(null)
    setDocLoading(false)
    setSearchLoading(false)
    if (selectedId) void fetchDocuments(selectedId)
  }, [selectedId, fetchDocuments])

  const createProject = async (values: ProjectFormValues) => {
    try {
      await postJson('/api/v1/projects', {
        project_id: values.project_id,
        name: values.name,
        description: values.description,
      })
      setProjectDrawerOpen(false)
      projectForm.resetFields()
      await loadProjects()
      // Auto-select the freshly created project so the user lands in its
      // detail view instead of having to hunt for it in the list.
      setSelectedId(values.project_id)
    } catch (e) {
      message.error(errorDetail(e) || t('projects.create_project_failed'))
    }
  }

  const removeProject = async (id: string) => {
    try {
      await deleteJson(`/api/v1/projects/${encodeURIComponent(id)}`)
      if (selectedIdRef.current === id) setSelectedId(null)
      await loadProjects()
    } catch (e) {
      message.error(errorDetail(e) || t('projects.delete_project_failed'))
    }
  }

  const createDocument = async (values: DocumentFormValues) => {
    const projectId = selectedIdRef.current
    if (!projectId) return
    const body: Record<string, unknown> = {
      doc_id: values.doc_id,
      filename: values.filename,
      content: values.content,
      indexed: values.indexed === true,
    }
    if (typeof values.page_count === 'number') body.page_count = values.page_count
    try {
      await postJson(`/api/v1/projects/${encodeURIComponent(projectId)}/documents`, body)
      setDocDrawerOpen(false)
      documentForm.resetFields()
      // Refresh only if the project this mutation targeted is still selected;
      // otherwise the list now belongs to another project.
      if (selectedIdRef.current === projectId) void fetchDocuments(projectId)
    } catch (e) {
      message.error(errorDetail(e) || t('projects.create_document_failed'))
    }
  }

  const uploadDocument = async (file: File) => {
    const projectId = selectedIdRef.current
    if (!projectId) return
    setUploading(true)
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('indexed', 'true')
      await postForm('/api/v1/projects/' + encodeURIComponent(projectId) + '/upload', form)
      message.success(t('projects.upload_success'))
      if (selectedIdRef.current === projectId) void fetchDocuments(projectId)
    } catch (e) {
      message.error(errorDetail(e) || t('projects.upload_failed'))
    } finally {
      setUploading(false)
    }
  }

  const removeDocument = async (id: string) => {
    const projectId = selectedIdRef.current
    try {
      await deleteJson(`/api/v1/documents/${encodeURIComponent(id)}`)
      if (projectId && selectedIdRef.current === projectId) void fetchDocuments(projectId)
    } catch (e) {
      message.error(errorDetail(e) || t('projects.delete_document_failed'))
    }
  }

  const normalizedFilter = filter.trim().toLowerCase()
  const filteredProjects = normalizedFilter
    ? projects.filter((p) =>
        p.name.toLowerCase().includes(normalizedFilter) ||
        (p.description ?? '').toLowerCase().includes(normalizedFilter) ||
        p.id.toLowerCase().includes(normalizedFilter))
    : projects

  const selectedProject = projects.find((p) => p.id === selectedId) ?? null

  const documentColumns: ColumnsType<DocumentRecord> = [
    {
      title: t('projects.filename'),
      dataIndex: 'filename',
      key: 'filename',
      render: (filename: string) => <span className="prj-file-name">{filename}</span>,
    },
    {
      title: t('projects.status'),
      dataIndex: 'status',
      key: 'status',
      width: 130,
      render: (status: string) => {
        if (status === 'indexed') {
          return (
            <Tag className="signal-tag signal-verified" icon={<CheckCircleFilled aria-hidden="true" />}>
              {t('projects.doc_status_indexed')}
            </Tag>
          )
        }
        if (status === 'uploaded') {
          return (
            <Tag className="signal-tag signal-neutral" icon={<UploadOutlined aria-hidden="true" />}>
              {t('projects.doc_status_uploaded')}
            </Tag>
          )
        }
        return <Tag className="signal-tag signal-neutral">{status}</Tag>
      },
    },
    {
      title: t('projects.page_count'),
      dataIndex: 'page_count',
      key: 'page_count',
      width: 90,
      render: (value: number | null | undefined) => (typeof value === 'number' ? value : '-'),
    },
    {
      title: t('projects.created_at'),
      dataIndex: 'created_at',
      key: 'created_at',
      width: 130,
      render: (value: string) => (
        <span className="mono prj-file-date">{formatDate(value, i18n.language)}</span>
      ),
    },
    {
      title: t('projects.actions'),
      key: 'actions',
      width: 90,
      render: (_, record) => (
        <Popconfirm
          title={t('projects.delete_document')}
          okText={t('projects.yes')}
          cancelText={t('projects.no')}
          onConfirm={() => void removeDocument(record.id)}
        >
          <Button size="small" danger>{t('projects.delete')}</Button>
        </Popconfirm>
      ),
    },
  ]

  const searchTab = (
    <div className="prj-search">
      <Input.Search
        size="large"
        enterButton={t('projects.search')}
        placeholder={t('projects.question_placeholder')}
        aria-label={t('projects.question_placeholder')}
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        onSearch={(value) => void runSearch(value)}
        loading={searchLoading}
      />
      <div className="prj-search-controls">
        <div className="prj-ctl">
          <span className="prj-ctl-label">{t('projects.search_mode')}</span>
          <Select<SearchMode>
            value={mode}
            onChange={setMode}
            aria-label={t('projects.search_mode')}
            className="prj-mode-select"
            options={[
              { value: 'bm25', label: t('projects.search_mode_bm25') },
              { value: 'dense', label: t('projects.search_mode_dense') },
              { value: 'fusion', label: t('projects.search_mode_fusion') },
            ]}
          />
        </div>
        <div className="prj-ctl">
          <span className="prj-ctl-label">{t('projects.search_limit')}</span>
          <InputNumber
            min={1}
            max={100}
            value={limit}
            onChange={(value) => setLimit(typeof value === 'number' && Number.isFinite(value) ? value : 3)}
            aria-label={t('projects.search_limit')}
          />
        </div>
      </div>

      {searchError && (
        <ErrorState
          title={t('projects.search_failed')}
          detail={searchError}
          onRetry={() => void runSearch(question)}
        />
      )}
      {!searched && !searchError && (
        <EmptyState
          icon={<SearchOutlined />}
          title={t('projects.search_no_query_title')}
          hint={t('projects.search_no_query')}
        />
      )}
      {searched && !searchError && (
        searchLoading ? (
          <div className="prj-loading"><Spin /></div>
        ) : hits.length === 0 ? (
          <EmptyState icon={<SearchOutlined />} title={t('projects.no_hits')} />
        ) : (
          <div className="prj-hits">
            {hits.map((hit, index) => (
              <EvidenceCard
                key={`${hit.document_id}-${index}`}
                filename={hit.filename}
                page={hit.page}
                section={hit.section}
                quote={hit.quote}
                status={hit.citation_status}
              />
            ))}
          </div>
        )
      )}
    </div>
  )

  const deliveryTab = (
    <div className="prj-unavailable-block">
      <Unavailable
        title={t('projects.delivery_title')}
        reason={t('projects.delivery_reason')}
        icon={<ThunderboltOutlined />}
      />
      <Button
        type="primary"
        className="prj-unavailable-action"
        onClick={() => navigate?.('analysis')}
      >
        {t('projects.delivery_run_analysis')}
      </Button>
    </div>
  )

  const activityTab = (
    <Unavailable
      title={t('projects.activity_title')}
      reason={t('projects.activity_reason')}
    />
  )

  const documentsTab = (
    <div className="prj-documents">
      {docError ? (
        <ErrorState
          title={t('projects.fetch_documents_failed')}
          detail={docError}
          onRetry={() => selectedId && void fetchDocuments(selectedId)}
        />
      ) : (
        <Table<DocumentRecord>
          size="small"
          rowKey="id"
          loading={docLoading}
          dataSource={documents}
          columns={documentColumns}
          pagination={false}
          locale={{
            emptyText: (
              <EmptyState
                title={t('projects.no_documents')}
                hint={t('projects.no_documents_hint')}
              />
            ),
          }}
        />
      )}
    </div>
  )

  return (
    <div className="projects-page">
      <PageHeader
        title={t('projects.title')}
        description={t('projects.subtitle')}
        actions={
          <Button
            type="primary"
            icon={<PlusOutlined aria-hidden="true" />}
            onClick={() => setProjectDrawerOpen(true)}
          >
            {t('projects.new_project')}
          </Button>
        }
      />

      <div className="prj-layout">
        <aside className="desk-card prj-list" aria-label={t('projects.title')}>
          <div className="prj-list-head">
            <h3 className="section-label">{t('projects.title')}</h3>
            <span className="mono prj-list-count">{projects.length}</span>
          </div>
          <Input
            allowClear
            placeholder={t('projects.filter_placeholder')}
            aria-label={t('projects.filter_placeholder')}
            prefix={<SearchOutlined aria-hidden="true" />}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <div className="prj-list-items">
            {projectsLoading && <div className="prj-loading"><Spin /></div>}
            {!projectsLoading && projectsError && (
              <ErrorState
                title={t('projects.fetch_projects_failed')}
                detail={projectsError}
                onRetry={() => void loadProjects()}
              />
            )}
            {!projectsLoading && !projectsError && filteredProjects.length === 0 && (
              projects.length === 0 ? (
                <EmptyState
                  icon={<FolderOpenOutlined />}
                  title={t('projects.no_projects')}
                  hint={t('projects.no_projects_hint')}
                />
              ) : (
                <EmptyState title={t('projects.filter_no_match')} />
              )
            )}
            {filteredProjects.map((project) => (
              <button
                key={project.id}
                type="button"
                className={project.id === selectedId ? 'prj-item prj-item--active' : 'prj-item'}
                onClick={() => setSelectedId(project.id)}
                aria-current={project.id === selectedId ? 'true' : undefined}
              >
                <span className="prj-item-name">{project.name}</span>
                {project.description && <span className="prj-item-desc">{project.description}</span>}
                <span className="mono prj-item-date">{formatDate(project.created_at, i18n.language)}</span>
              </button>
            ))}
          </div>
          <Button
            block
            icon={<PlusOutlined aria-hidden="true" />}
            onClick={() => setProjectDrawerOpen(true)}
          >
            {t('projects.new_project')}
          </Button>
        </aside>

        <section className="prj-detail">
          {!selectedProject ? (
            <div className="desk-card prj-detail-placeholder">
              <EmptyState
                icon={<FolderOpenOutlined />}
                title={t('projects.select_hint_title')}
                hint={t('projects.select_hint')}
              />
            </div>
          ) : (
            <>
              <div className="desk-card prj-detail-head">
                <div className="prj-detail-text">
                  <h3 className="prj-detail-name">{selectedProject.name}</h3>
                  <div className="prj-detail-meta">
                    <span className="mono">{selectedProject.id}</span>
                    <span aria-hidden="true">·</span>
                    <span className="mono">{formatDate(selectedProject.created_at, i18n.language)}</span>
                  </div>
                  {selectedProject.description && (
                    <p className="prj-detail-desc">{selectedProject.description}</p>
                  )}
                </div>
                <div className="prj-detail-actions">
                  <Button
                    type="primary"
                    icon={<PlusOutlined aria-hidden="true" />}
                    onClick={() => setDocDrawerOpen(true)}
                  >
                    {t('projects.add_document')}
                  </Button>
                  <Button icon={<UploadOutlined aria-hidden="true" />} loading={uploading}
                    onClick={() => uploadInputRef.current?.click()}>
                    {t('projects.upload_file')}
                  </Button>
                  <input ref={uploadInputRef} type="file" accept=".pdf,.docx,.txt,.md,.text" hidden
                    aria-label={t('projects.upload_file')} disabled={uploading}
                    onChange={(event) => {
                      const file = event.target.files?.[0]
                      event.target.value = ''
                      if (file) void uploadDocument(file)
                    }} />
                  <Popconfirm
                    title={t('projects.delete_project')}
                    okText={t('projects.yes')}
                    cancelText={t('projects.no')}
                    onConfirm={() => void removeProject(selectedProject.id)}
                  >
                    <Button danger>{t('projects.delete')}</Button>
                  </Popconfirm>
                </div>
              </div>
              <div className="desk-card prj-tabs-card">
                <Tabs
                  defaultActiveKey="documents"
                  items={[
                    { key: 'documents', label: t('projects.tab_documents'), children: documentsTab },
                    { key: 'search', label: t('projects.tab_search'), children: searchTab },
                    { key: 'delivery', label: t('projects.tab_delivery'), children: deliveryTab },
                    { key: 'activity', label: t('projects.tab_activity'), children: activityTab },
                  ]}
                />
              </div>
            </>
          )}
        </section>
      </div>

      <Drawer
        title={t('projects.new_project')}
        open={projectDrawerOpen}
        onClose={() => setProjectDrawerOpen(false)}
        size={420}
      >
        <Form form={projectForm} layout="vertical" onFinish={(values) => void createProject(values)}>
          <Form.Item name="project_id" label={t('projects.project_id')} rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="name" label={t('projects.name')} rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="description" label={t('projects.description')}>
            <Input.TextArea rows={3} />
          </Form.Item>
          <div className="prj-drawer-footer">
            <Button onClick={() => setProjectDrawerOpen(false)}>{t('projects.cancel')}</Button>
            <Button type="primary" htmlType="submit">{t('projects.create')}</Button>
          </div>
        </Form>
      </Drawer>

      <Drawer
        title={t('projects.add_document')}
        open={docDrawerOpen}
        onClose={() => setDocDrawerOpen(false)}
        size={480}
      >
        <Form form={documentForm} layout="vertical" onFinish={(values) => void createDocument(values)}>
          <p className="prj-form-note">{t('projects.doc_paste_note')}</p>
          <Form.Item name="doc_id" label={t('projects.document_id')} rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="filename" label={t('projects.filename')} rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="content" label={t('projects.content')} rules={[{ required: true }]}>
            <Input.TextArea rows={5} />
          </Form.Item>
          <Form.Item name="page_count" label={t('projects.page_count')}>
            <InputNumber min={0} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="indexed" valuePropName="checked" initialValue={true} label={t('projects.mark_indexed')}>
            <Switch />
          </Form.Item>
          <div className="prj-drawer-footer">
            <Button onClick={() => setDocDrawerOpen(false)}>{t('projects.cancel')}</Button>
            <Button type="primary" htmlType="submit">{t('projects.add')}</Button>
          </div>
        </Form>
      </Drawer>
    </div>
  )
}

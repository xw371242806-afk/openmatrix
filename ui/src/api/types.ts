/**
 * Response shapes actually returned by the OpenMatrix backend.
 * Fields the API does not provide are deliberately absent — the UI must
 * never invent data the backend does not send.
 */

/* ── Matrix runs ─────────────────────────────────────────── */

export interface RunCounts {
  run_id: string
  total: number
  completed: number
  failed: number
  needs_review: number
}

export interface MatrixCell {
  run_id: string
  cell_id: string
  document_index: number
  document_name: string
  question_index: number
  question: string
  status: string
  answer: string | null
  quote: string | null
  support_status?: 'supported' | 'partial' | 'unsupported' | 'unchecked' | null
  support_reason?: string | null
  citation_status: string | null
  page: number | null
  error: string | null
}

export interface MatrixRunDetail {
  run_id: string
  cells: MatrixCell[]
}

export interface CellEvent {
  type?: string
  run_id?: string
  cell_id?: string
  document_index?: number
  question_index?: number
  question?: string
  status?: string
}

/* ── Research agent ──────────────────────────────────────── */
/* REST exposes ONLY: research_id, objective, owner, phase, error and
   steps[].{step_id, tool, status, attempts, error}. Plan internals,
   step dependencies/arguments/outputs, events, conclusion, evidence and
   created_at are NOT available over REST. */

export interface ResearchEvidence {
  chunk_id?: string | null
  evidence_id?: string | null
  run_id?: string | null
  page?: number | null
  document_index?: number | null
  cell_id?: string | null
  document_name?: string | null
  quote?: string | null
  source_url?: string | null
}

export interface ResearchStep {
  step_id: string
  tool: string
  status: string
  attempts: number
  error?: string | null
  output_preview?: string | null
  evidence?: ResearchEvidence[]
}

export interface ResearchSummary {
  research_id: string
  objective: string
  owner?: string | null
  phase: string
  error?: string | null
  conclusion?: string | Record<string, unknown> | null
  steps: ResearchStep[]
}

export interface ResearchStartResponse {
  task_id: string
  operation: string
  phase: string
}

/* ── Projects & documents ────────────────────────────────── */

export interface ProjectRecord {
  id: string
  name: string
  description?: string | null
  created_at: string
}

export interface DocumentRecord {
  id: string
  project_id: string
  filename: string
  content_path?: string | null
  page_count?: number | null
  status: string
  provenance?: string | null
  created_at: string
}

export interface SearchHit {
  document_id: string
  filename: string
  page: number | null
  section?: number | null
  quote: string
  citation_status: string
}

/* ── Schedules (in-memory; may be lost on service restart) ─ */

export interface ScheduleEntry {
  schedule_id: string
  cron: string
  objective: string
  research_id: string
  last_fired?: string | null
  next_due?: string | null
  last_error?: string | null
  paused: boolean
}

/* ── Model router & health ───────────────────────────────── */

export interface ModelRouteTask {
  env?: string
  model?: string
}

export interface ModelRouterConfig {
  enabled: boolean
  default_model: string
  tasks?: Record<string, ModelRouteTask>
}

export interface HealthDetailed {
  status?: string
  service?: string
  components?: Record<string, string>
}

/* ── Analysis responses ──────────────────────────────────── */

export interface AnalysisCellData {
  document_name?: string
  document_index?: number
  question: string
  answer?: string | null
  quote?: string | null
  support_status?: 'supported' | 'partial' | 'unsupported' | 'unchecked' | null
  support_reason?: string | null
  citation_status?: string | null
  page?: number | null
  error?: string | null
  step_id?: string
  depends_on?: string[]
}

export interface AnalyzeBatchResponse {
  status: string
  data: AnalysisCellData[]
  run_id: string
  documents_count?: number
}

export interface AnalyzeChainResponse {
  status: string
  run_id: string
  data: AnalysisCellData[]
  steps_count?: number
  kg_index_task_id?: string | null
}

/* ── Statistics ──────────────────────────────────────────── */

export interface NameCount {
  name: string
  count: number
}

import i18n from '../i18n'

/**
 * Unified API error. Carries the HTTP status so callers can react to
 * specific failures (e.g. 401/403) without parsing messages.
 */
export class ApiError extends Error {
  readonly status: number
  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * OpenMatrix access token — memory only.
 * Never persisted to storage, never logged, never rendered, and never
 * included in error output. This authenticates requests TO OpenMatrix;
 * it is distinct from BYOK provider API keys, which are only ever sent
 * as form fields of a single analysis request and never stored here.
 */
let accessToken = ''
const tokenListeners = new Set<(configured: boolean) => void>()

function notifyTokenListeners(): void {
  const configured = accessToken.length > 0
  tokenListeners.forEach((listener) => listener(configured))
}

export function setAccessToken(token: string): void {
  accessToken = token.trim()
  notifyTokenListeners()
}

export function clearAccessToken(): void {
  accessToken = ''
  notifyTokenListeners()
}

export function hasAccessToken(): boolean {
  return accessToken.length > 0
}

export function getAccessToken(): string {
  return accessToken
}

export function subscribeAccessToken(listener: (configured: boolean) => void): () => void {
  tokenListeners.add(listener)
  listener(hasAccessToken())
  return () => tokenListeners.delete(listener)
}

export interface RequestOptions {
  /** Per-call token override (e.g. the Settings master key). */
  token?: string
  signal?: AbortSignal
}

function authHeaders(opts: RequestOptions): Record<string, string> {
  const bearer = opts.token ?? accessToken
  return bearer ? { Authorization: `Bearer ${bearer}` } : {}
}

async function errorFromResponse(res: Response): Promise<ApiError> {
  if (res.status === 401 || res.status === 403) {
    if (res.status === 401 && typeof window !== 'undefined') window.dispatchEvent(new Event('openmatrix-auth-required'))
    // Auth failures never echo request credentials.
    return new ApiError(i18n.t('errors.auth_failed'), res.status)
  }
  let detail = ''
  try {
    const body: unknown = await res.json()
    if (body && typeof body === 'object') {
      const err = (body as { error?: unknown }).error
      if (typeof err === 'string' && err.trim()) detail = err
    }
  } catch {
    // Non-JSON body — fall through to the generic status message.
  }
  return new ApiError(detail || `${i18n.t('errors.request_failed_status')} ${res.status}`, res.status)
}

export async function getJson<T>(url: string, opts: RequestOptions = {}): Promise<T> {
  const res = await fetch(url, { headers: authHeaders(opts), signal: opts.signal })
  if (!res.ok) throw await errorFromResponse(res)
  return res.json() as Promise<T>
}

export async function postJson<T>(url: string, body: unknown, opts: RequestOptions = {}): Promise<T> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders(opts) },
    body: JSON.stringify(body),
    signal: opts.signal,
  })
  if (!res.ok) throw await errorFromResponse(res)
  return res.json() as Promise<T>
}

export async function postForm<T>(url: string, form: FormData, opts: RequestOptions = {}): Promise<T> {
  const res = await fetch(url, {
    method: 'POST',
    headers: authHeaders(opts),
    body: form,
    signal: opts.signal,
  })
  if (!res.ok) throw await errorFromResponse(res)
  return res.json() as Promise<T>
}

export async function deleteJson<T>(url: string, opts: RequestOptions = {}): Promise<T> {
  const res = await fetch(url, { method: 'DELETE', headers: authHeaders(opts), signal: opts.signal })
  if (!res.ok) throw await errorFromResponse(res)
  return res.json() as Promise<T>
}

/** Server-generated file download (blob). Never parses the body as JSON. */
export async function downloadFile(url: string, filename: string, opts: RequestOptions = {}): Promise<void> {
  const res = await fetch(url, { headers: authHeaders(opts), signal: opts.signal })
  if (!res.ok) throw await errorFromResponse(res)
  const blob = await res.blob()
  const objectUrl = URL.createObjectURL(blob)
  try {
    const anchor = document.createElement('a')
    anchor.href = objectUrl
    anchor.download = filename
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  } finally {
    URL.revokeObjectURL(objectUrl)
  }
}

export interface MatrixEnvelope {
  CLAIMS?: string[]
  UNKNOWNS?: string[]
  GATES?: Record<string, string>
  VERDICT?: string
}

export interface MatrixRunResult {
  xeno_ir: string
  processed_chunks: number
  shared_board_findings: string[]
  envelope: MatrixEnvelope | string
  raw_findings: unknown[]
}

export interface MatrixStatus {
  ready: boolean
  xlat_configured: boolean
  server_llm_configured: boolean
  semantic_index: string
}

export interface MatrixRunOptions {
  apiKey?: string
  baseUrl?: string
  modelName?: string
}

export async function runMatrixPipeline(
  query: string,
  index = 'default_matrix',
  matrix: MatrixRunOptions = {},
  opts: RequestOptions = {},
): Promise<MatrixRunResult> {
  return postJson<MatrixRunResult>('/api/matrix/run', {
    query,
    index,
    api_key: matrix.apiKey?.trim() || undefined,
    base_url: matrix.baseUrl?.trim() || undefined,
    model_name: matrix.modelName?.trim() || undefined,
  }, opts)
}

/** Fetch an authenticated retained source PDF as bytes. */
export async function getPdfFile(url: string, opts: RequestOptions = {}): Promise<ArrayBuffer> {
  const res = await fetch(url, { headers: authHeaders(opts), signal: opts.signal })
  if (!res.ok) throw await errorFromResponse(res)
  return res.arrayBuffer()
}

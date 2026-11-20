import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import MatrixPage from '../MatrixPage'
import i18n from '../i18n'

describe('MatrixPage', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    void i18n.changeLanguage('zh')
  })

  it('shows an honest configuration warning when semantic search is unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      ready: false,
      xlat_configured: false,
      server_llm_configured: false,
      semantic_index: '.openmatrix/semantic-index',
    }), { status: 200 })))

    render(<MatrixPage />)

    expect(await screen.findByText('矩阵语义检索尚未配置')).toBeInTheDocument()
    expect(screen.getByLabelText('模型 API Key（仅随本次请求发送，不保存）')).toBeInTheDocument()
  })

  it('surfaces a failed matrix request instead of rendering a success envelope', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      if (String(input).endsWith('/api/matrix/status')) {
        return new Response(JSON.stringify({
          ready: true,
          xlat_configured: true,
          server_llm_configured: true,
          semantic_index: '.openmatrix/semantic-index',
        }), { status: 200 })
      }
      return new Response(JSON.stringify({ error: 'index is empty' }), { status: 422 })
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<MatrixPage />)
    await screen.findByLabelText('目标知识索引')
    fireEvent.change(screen.getByLabelText('深度检索意图'), {
      target: { value: '提取营收' },
    })
    const launch = screen.getByRole('button', { name: /启动矩阵分析/i })
    await waitFor(() => expect(launch).toBeEnabled())
    fireEvent.click(launch)

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    const runBody = JSON.parse(String(fetchMock.mock.calls[1][1]?.body)) as Record<string, unknown>
    expect(runBody).not.toHaveProperty('api_key')
    expect(runBody).not.toHaveProperty('base_url')
    expect(runBody).not.toHaveProperty('model_name')
    expect(screen.queryByText('🟢 确凿事实')).not.toBeInTheDocument()
  })

  it('sends provider fields only after explicitly enabling BYOK with server configuration', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      if (String(input).endsWith('/api/matrix/status')) {
        return new Response(JSON.stringify({ ready: true, xlat_configured: true, server_llm_configured: true, semantic_index: '.openmatrix/semantic-index' }), { status: 200 })
      }
      return new Response(JSON.stringify({ error: 'index is empty' }), { status: 422 })
    })
    vi.stubGlobal('fetch', fetchMock)
    render(<MatrixPage />)
    await screen.findByRole('switch', { name: '使用自己的模型服务' })
    fireEvent.click(screen.getByRole('switch', { name: '使用自己的模型服务' }))
    fireEvent.change(screen.getByLabelText('模型 API 地址'), { target: { value: 'https://api.deepseek.com' } })
    fireEvent.change(screen.getByLabelText('模型名称'), { target: { value: 'deepseek-flash' } })
    fireEvent.change(screen.getByLabelText('模型 API Key（仅随本次请求发送，不保存）'), { target: { value: 'provider-key' } })
    fireEvent.change(screen.getByLabelText('深度检索意图'), { target: { value: '提取营收' } })
    await waitFor(() => expect(screen.getByRole('button', { name: /启动矩阵分析/i })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /启动矩阵分析/i }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    const body = JSON.parse(String(fetchMock.mock.calls[1][1]?.body))
    expect(body).toMatchObject({ api_key: 'provider-key', base_url: 'https://api.deepseek.com', model_name: 'deepseek-flash' })
  })
})


it('disables launch until matrix status is ready and links setup guidance', async () => {
  await i18n.changeLanguage('zh')
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    ready: false, xlat_configured: false, server_llm_configured: true,
    semantic_index: '.openmatrix/semantic-index',
  }), { status: 200 })))
  render(<MatrixPage />)
  expect(await screen.findByText('查看配置说明')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '启动矩阵分析' })).toBeDisabled()
})

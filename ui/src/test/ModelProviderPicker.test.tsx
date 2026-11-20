import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import ModelProviderPicker, { MODEL_PROVIDER_PRESETS, type ModelProviderValue } from '../components/ModelProviderPicker'
import i18n from '../i18n'

function Harness({ initial }: { initial: ModelProviderValue }) {
  const [value, setValue] = React.useState(initial)
  return <ModelProviderPicker value={value} onChange={setValue} />
}

describe('ModelProviderPicker', () => {
  beforeEach(async () => {
    await i18n.changeLanguage('en')
    vi.stubGlobal('fetch', vi.fn())
  })
  afterEach(() => vi.unstubAllGlobals())

  it('selects a preset and clears a key from another provider', async () => {
    const user = userEvent.setup()
    render(<Harness initial={{ apiKey: 'secret-key', baseUrl: 'https://custom.example/v1', modelName: 'old-model' }} />)
    await user.click(screen.getByLabelText('Provider preset'))
    await user.click(screen.getByText('OpenCode Go'))
    expect(screen.getByLabelText('Provider API key')).toHaveValue('')
    expect(screen.getByLabelText('Model service base URL')).toHaveValue(MODEL_PROVIDER_PRESETS.opencodeGo.baseUrl)
    expect(screen.getByLabelText('Model name')).toHaveValue(MODEL_PROVIDER_PRESETS.opencodeGo.modelName)
  })

  it('posts discovery credentials in JSON body and exposes returned models', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ models: ['deepseek-chat', 'deepseek-reasoner'] }), { status: 200 }))
    const user = userEvent.setup()
    render(<Harness initial={{ apiKey: 'secret-key', baseUrl: 'https://api.deepseek.com', modelName: 'deepseek-flash' }} />)
    await user.click(screen.getByRole('button', { name: 'Fetch models' }))
    await waitFor(() => expect(screen.getByLabelText('Model name')).toBeInTheDocument())
    await user.click(screen.getByLabelText('Model name'))
    await waitFor(() => expect(screen.getByText('deepseek-reasoner')).toBeInTheDocument())
    const [url, init] = fetchMock.mock.calls[0]
    expect(String(url)).toBe('/api/models')
    expect(String(url)).not.toContain('secret-key')
    expect(JSON.parse(String(init?.body))).toEqual({ base_url: 'https://api.deepseek.com', api_key: 'secret-key' })
  })

  it('clears the key when the base URL is manually changed', () => {
    render(<Harness initial={{ apiKey: 'secret-key', baseUrl: 'https://api.deepseek.com', modelName: 'deepseek-flash' }} />)
    fireEvent.change(screen.getByLabelText('Model service base URL'), { target: { value: 'https://other.example/v1' } })
    expect(screen.getByLabelText('Provider API key')).toHaveValue('')
  })

  it('shows a safe generic error without rendering the provider error text', async () => {
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockRejectedValue(new Error('secret-key leaked by provider'))
    const user = userEvent.setup()
    render(<Harness initial={{ apiKey: 'secret-key', baseUrl: 'https://api.deepseek.com', modelName: 'deepseek-flash' }} />)
    await user.click(screen.getByRole('button', { name: 'Fetch models' }))
    await waitFor(() => expect(screen.getByText('Model discovery failed. Check the provider settings and try again.')).toBeInTheDocument())
    expect(screen.queryByText(/secret-key leaked/)).not.toBeInTheDocument()
  })

  it('ignores an old response after switching provider', async () => {
    let resolve!: (response: Response) => void
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(() => new Promise<Response>((done) => { resolve = done }))
    const user = userEvent.setup()
    render(<Harness initial={{ apiKey: 'old-key', baseUrl: 'https://api.deepseek.com', modelName: 'deepseek-flash' }} />)
    await user.click(screen.getByRole('button', { name: 'Fetch models' }))
    await user.click(screen.getByLabelText('Provider preset'))
    await user.click(screen.getByText('OpenCode Go'))
    resolve(new Response(JSON.stringify({ models: ['stale-model'] }), { status: 200 }))
    await waitFor(() => expect(screen.queryByText('stale-model')).not.toBeInTheDocument())
  })
})

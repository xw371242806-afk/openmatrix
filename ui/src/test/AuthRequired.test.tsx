import { it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import App from '../App'
import { clearAccessToken } from '../api/client'

it('shows a top-level access token prompt after a 401 response', async () => {
  clearAccessToken()
  vi.stubGlobal('fetch', vi.fn(async (url: RequestInfo | URL) => new Response(JSON.stringify(
    String(url).endsWith('/health') ? { status: 'ok' } : { error: 'authentication required' },
  ), { status: String(url).endsWith('/health') ? 200 : 401 })))
  render(<App />)
  await waitFor(() => expect(screen.getByText('Set an access token in the upper-right corner.')).toBeInTheDocument())
})

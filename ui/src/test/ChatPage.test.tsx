import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ChatPage from '../ChatPage'
import i18n from '../i18n'
import p4en from '../locales/patches/p4.en.json'
import p4zh from '../locales/patches/p4.zh.json'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status })
}

type FetchMock = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>

function stubFetch(fn: FetchMock) {
  const wrapped = vi.fn(fn)
  vi.stubGlobal('fetch', wrapped)
  return wrapped
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

describe('ChatPage', () => {
  beforeAll(() => {
    // The agent.* namespace ships as a locale patch; register it so the
    // console renders real copy instead of raw keys.
    i18n.addResourceBundle('en', 'translation', p4en, true, true)
    i18n.addResourceBundle('zh', 'translation', p4zh, true, true)
  })

  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('sends a research goal, shows the objective and starts polling to a terminal phase', async () => {
    let polls = 0
    stubFetch(async (input, init) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/v1/research' && method === 'POST') {
        return jsonResponse({ task_id: 'r-1', operation: 'research_run', phase: 'running' })
      }
      if (url === '/api/v1/research' && method === 'GET') {
        return jsonResponse({ research: [] })
      }
      if (url.startsWith('/api/v1/research/')) {
        polls += 1
        if (polls === 1) {
          return jsonResponse({ research_id: 'r-1', objective: 'analyze the filing', phase: 'running', error: null, steps: [] })
        }
        return jsonResponse({
          research_id: 'r-1',
          objective: 'analyze the filing',
          phase: 'completed',
          error: null,
          steps: [{ step_id: 's1', tool: 'retrieve_context', status: 'succeeded', attempts: 1, error: null }],
        })
      }
      return jsonResponse({ error: 'not found' }, 404)
    })
    const user = userEvent.setup()
    render(<ChatPage />)
    await user.type(screen.getByPlaceholderText(/Enter a research goal/), 'analyze the filing')
    await user.keyboard('{Enter}')

    // the submitted objective is echoed in the session header card
    await waitFor(() => expect(screen.getAllByText('analyze the filing').length).toBeGreaterThan(0), { timeout: 4000 })
    // polling reaches a terminal phase (phase tag rendered)
    await waitFor(() => expect(screen.getAllByText('Completed').length).toBeGreaterThan(0), { timeout: 8000 })
    expect(screen.getAllByText('retrieve_context').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Succeeded').length).toBeGreaterThan(0)
    expect(polls).toBeGreaterThan(1)
  }, 15000)

  it('offers a cancel button while running and cancels via DELETE', async () => {
    let deleted = false
    stubFetch(async (input, init) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url.startsWith('/api/v1/research/') && method === 'DELETE') {
        deleted = true
        return jsonResponse({ research_id: 'r-9', cancelled: true })
      }
      if (url === '/api/v1/research' && method === 'GET') {
        return jsonResponse({ research: [] })
      }
      if (url.startsWith('/api/v1/research/')) {
        return jsonResponse({ research_id: 'r-9', objective: 'do it', phase: 'running', error: null, steps: [] })
      }
      return jsonResponse({ task_id: 'r-9', operation: 'research_run', phase: 'running' })
    })
    const user = userEvent.setup()
    render(<ChatPage />)
    await user.type(screen.getByPlaceholderText(/Enter a research goal/), 'do it')
    await user.keyboard('{Enter}')
    const cancelBtn = await screen.findByRole('button', { name: 'Cancel' }, { timeout: 4000 })
    expect(cancelBtn).toBeInTheDocument()
    await user.click(cancelBtn)
    expect(deleted).toBe(true)
    await screen.findByText('This research run was cancelled.')
  }, 10000)

  it('does not let an in-flight poll overwrite a successful cancellation', async () => {
    const poll = deferred<Response>()
    let pollStarted = false
    stubFetch(async (input, init) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/v1/research' && method === 'POST') {
        return jsonResponse({ task_id: 'r-race', operation: 'research_run', phase: 'running' })
      }
      if (url === '/api/v1/research' && method === 'GET') return jsonResponse({ research: [] })
      if (url === '/api/v1/schedules' && method === 'GET') return jsonResponse({ schedules: [] })
      if (url.startsWith('/api/v1/research/') && method === 'DELETE') {
        return jsonResponse({ research_id: 'r-race', cancelled: true })
      }
      if (url.startsWith('/api/v1/research/')) {
        pollStarted = true
        return poll.promise
      }
      return jsonResponse({ error: 'not found' }, 404)
    })
    const user = userEvent.setup()
    render(<ChatPage />)
    await user.type(screen.getByPlaceholderText(/Enter a research goal/), 'race cancellation')
    await user.keyboard('{Enter}')
    await screen.findByRole('button', { name: 'Cancel' })

    await waitFor(() => expect(pollStarted).toBe(true), { timeout: 4000 })
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByText('This research run was cancelled.')).toBeInTheDocument()

    poll.resolve(jsonResponse({
      research_id: 'r-race',
      objective: 'race cancellation',
      phase: 'running',
      error: null,
      steps: [],
    }))
    await waitFor(() => expect(screen.getByText('This research run was cancelled.')).toBeInTheDocument())

    expect(screen.getByText('This research run was cancelled.')).toBeInTheDocument()
    expect(screen.queryByText('Working…')).not.toBeInTheDocument()
  }, 10000)

  it('does not let an old cancellation response stop polling for a newly selected session', async () => {
    const deletion = deferred<Response>()
    let selectedPolls = 0
    let oldId = ''
    stubFetch(async (input, init) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/v1/research' && method === 'GET') return jsonResponse({ research: ['r-next'] })
      if (url === '/api/v1/schedules' && method === 'GET') return jsonResponse({ schedules: [] })
      if (url === '/api/v1/research' && method === 'POST') {
        oldId = (JSON.parse(String(init?.body)) as { research_id: string }).research_id
        return jsonResponse({ task_id: 'r-old', operation: 'research_run', phase: 'running' })
      }
      if (oldId && url.endsWith(`/${oldId}`) && method === 'DELETE') return deletion.promise
      if (url.includes('/r-next')) {
        selectedPolls += 1
        return jsonResponse({
          research_id: 'r-next',
          objective: 'Next session',
          phase: 'running',
          error: null,
          steps: [],
        })
      }
      return jsonResponse({ research_id: 'r-old', objective: 'Old session', phase: 'running', error: null, steps: [] })
    })
    const user = userEvent.setup()
    render(<ChatPage />)
    expect(await screen.findByText('Next session')).toBeInTheDocument()
    await user.type(screen.getByPlaceholderText(/Enter a research goal/), 'Old session')
    await user.keyboard('{Enter}')
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))
    await user.click(screen.getByText('Next session'))
    await waitFor(() => expect(screen.getAllByText('Next session').length).toBe(2))

    const pollsBeforeOldCancellation = selectedPolls
    deletion.resolve(jsonResponse({ research_id: 'r-old', cancelled: true }))

    await waitFor(() => expect(selectedPolls).toBeGreaterThan(pollsBeforeOldCancellation), { timeout: 4000 })
  }, 10000)

  it('blocks a second submit while a pending/non-terminal run exists', async () => {
    let researchPosts = 0
    stubFetch(async (input, init) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/v1/research' && method === 'POST') {
        researchPosts += 1
        return jsonResponse({ task_id: `r-${researchPosts}`, operation: 'research_run', phase: 'pending' })
      }
      if (url === '/api/v1/research' && method === 'GET') {
        return jsonResponse({ research: [] })
      }
      return jsonResponse({ research_id: 'r-x', objective: 'o', phase: 'pending', error: null, steps: [] })
    })
    const user = userEvent.setup()
    render(<ChatPage />)

    await user.type(screen.getByPlaceholderText(/Enter a research goal/), 'first goal')
    await user.keyboard('{Enter}')
    await screen.findByText('Pending', {}, { timeout: 4000 })

    // second submit must be blocked (still one POST)
    await user.type(screen.getByPlaceholderText(/Enter a research goal/), 'second goal')
    await user.keyboard('{Enter}')
    expect(researchPosts).toBe(1)
  }, 10000)

  it('lists sessions with phases and loads one into the main view on click', async () => {
    stubFetch(async (input, init) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/v1/research' && method === 'GET') {
        return jsonResponse({ research: ['r-a', 'r-b'] })
      }
      if (url.endsWith('/api/v1/research/r-a')) {
        return jsonResponse({ research_id: 'r-a', objective: 'Map vendor risk', phase: 'running', error: null, steps: [] })
      }
      if (url.endsWith('/api/v1/research/r-b')) {
        return jsonResponse({ research_id: 'r-b', objective: 'Draft summary memo', phase: 'completed', error: null, steps: [] })
      }
      return jsonResponse({ error: 'not found' }, 404)
    })
    const user = userEvent.setup()
    render(<ChatPage />)

    expect(await screen.findByText('Map vendor risk', {}, { timeout: 4000 })).toBeInTheDocument()
    expect(screen.getByText('Draft summary memo')).toBeInTheDocument()
    expect(screen.getAllByText('Running').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Completed').length).toBeGreaterThan(0)

    await user.click(screen.getByText('Map vendor risk'))
    // selecting loads the session into the main header too
    await waitFor(() => expect(screen.getAllByText('Map vendor risk').length).toBe(2))
  }, 10000)

  it('keeps a listed session when its detail status returns 404', async () => {
    let detailCalls = 0
    stubFetch(async (input, init) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/v1/research' && method === 'GET') return jsonResponse({ research: ['r-missing'] })
      if (url === '/api/v1/schedules' && method === 'GET') return jsonResponse({ schedules: [] })
      if (url.endsWith('/r-missing')) {
        detailCalls += 1
        if (detailCalls === 1) {
          return jsonResponse({ research_id: 'r-missing', objective: 'Missing status', phase: 'running', error: null, steps: [] })
        }
        return jsonResponse({ error: 'not found' }, 404)
      }
      return jsonResponse({ error: 'not found' }, 404)
    })
    const user = userEvent.setup()
    render(<ChatPage />)

    await user.click(await screen.findByText('Missing status'))

    expect(screen.getAllByText('Missing status').length).toBe(2)
    expect(screen.getByText('Run status unavailable')).toBeInTheDocument()
  }, 10000)

  it('does not let an older session-list response replace a newer refresh', async () => {
    const firstList = deferred<Response>()
    let listCalls = 0
    stubFetch(async (input, init) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/v1/research' && method === 'GET') {
        listCalls += 1
        if (listCalls === 1) return firstList.promise
        return jsonResponse({ research: ['r-new'] })
      }
      if (url === '/api/v1/research/r-new') {
        return jsonResponse({ research_id: 'r-new', objective: 'New list', phase: 'completed', steps: [] })
      }
      if (url === '/api/v1/research/r-old') {
        return jsonResponse({ research_id: 'r-old', objective: 'Old list', phase: 'completed', steps: [] })
      }
      if (url === '/api/v1/schedules') return jsonResponse({ schedules: [] })
      return jsonResponse({ error: 'not found' }, 404)
    })
    const user = userEvent.setup()
    render(<ChatPage />)

    await user.click(screen.getAllByRole('button', { name: 'Refresh' })[0])
    expect(await screen.findByText('New list')).toBeInTheDocument()
    firstList.resolve(jsonResponse({ research: ['r-old'] }))

    await waitFor(() => expect(screen.getByText('New list')).toBeInTheDocument())
    expect(screen.queryByText('Old list')).not.toBeInTheDocument()
  }, 10000)

  it('creates and deletes a schedule', async () => {
    let created = false
    let deleted = false
    let postBody: Record<string, unknown> | null = null
    const schedule = {
      schedule_id: 'sch-9',
      cron: '0 9 * * 1-5',
      objective: 'Morning brief',
      research_id: 'r-1',
      next_due: null,
      last_fired: null,
      last_error: null,
      paused: true,
    }
    stubFetch(async (input, init) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/v1/schedules' && method === 'GET') {
        return jsonResponse({ schedules: created ? [schedule] : [] })
      }
      if (url === '/api/v1/schedules' && method === 'POST') {
        created = true
        postBody = JSON.parse(String(init?.body)) as Record<string, unknown>
        return jsonResponse({ ...schedule, status: 'registered' })
      }
      if (url === '/api/v1/schedules/sch-9' && method === 'DELETE') {
        deleted = true
        return jsonResponse({ deleted: true })
      }
      if (url === '/api/v1/research' && method === 'GET') {
        return jsonResponse({ research: [] })
      }
      return jsonResponse({ error: 'not found' }, 404)
    })
    const user = userEvent.setup()
    render(<ChatPage />)

    await user.click(await screen.findByRole('button', { name: 'New schedule' }))
    await user.type(screen.getByLabelText('Objective'), 'Morning brief')
    await user.type(screen.getByLabelText('Cron'), '0 9 * * 1-5')
    await user.type(screen.getByLabelText('Research ID'), 'r-1')
    await user.click(screen.getByRole('button', { name: 'Register' }))

    await waitFor(() => expect(created).toBe(true))
    expect(postBody).toMatchObject({ cron: '0 9 * * 1-5', objective: 'Morning brief', research_id: 'r-1' })
    expect(await screen.findByText('sch-9')).toBeInTheDocument()
    expect(screen.getByText('Paused')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(await screen.findByRole('button', { name: 'Yes, delete' }))
    await waitFor(() => expect(deleted).toBe(true))
  }, 10000)

  it('rejects an invalid context JSON client-side without posting', async () => {
    let postCalls = 0
    stubFetch(async (input, init) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/v1/schedules' && method === 'GET') {
        return jsonResponse({ schedules: [] })
      }
      if (url === '/api/v1/schedules' && method === 'POST') {
        postCalls += 1
        return jsonResponse({ schedule_id: 's', cron: '* * * * *', objective: 'o', research_id: 'r', status: 'registered' })
      }
      if (url === '/api/v1/research' && method === 'GET') {
        return jsonResponse({ research: [] })
      }
      return jsonResponse({ error: 'not found' }, 404)
    })
    const user = userEvent.setup()
    render(<ChatPage />)

    await user.click(await screen.findByRole('button', { name: 'New schedule' }))
    await user.type(screen.getByLabelText('Objective'), 'Morning brief')
    await user.type(screen.getByLabelText('Cron'), '0 9 * * 1-5')
    await user.type(screen.getByLabelText('Research ID'), 'r-1')
    await user.type(screen.getByLabelText('Context (optional JSON)'), 'not json')
    await user.click(screen.getByRole('button', { name: 'Register' }))

    expect(await screen.findByText('Context must be valid JSON')).toBeInTheDocument()
    expect(postCalls).toBe(0)
  }, 10000)

  it.each(['null', '[]'])('rejects research context %s because it is not an object', async (context) => {
    let postCalls = 0
    stubFetch(async (input, init) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/v1/research' && method === 'POST') postCalls += 1
      if (url === '/api/v1/research' && method === 'GET') return jsonResponse({ research: [] })
      if (url === '/api/v1/schedules' && method === 'GET') return jsonResponse({ schedules: [] })
      return jsonResponse({ error: 'not found' }, 404)
    })
    const user = userEvent.setup()
    render(<ChatPage />)

    await user.click(screen.getByText('Research context (optional JSON)'))
    await user.click(screen.getByLabelText('Research context (optional JSON)'))
    await user.paste(context)
    await user.type(screen.getByPlaceholderText(/Enter a research goal/), 'reject context')
    await user.keyboard('{Enter}')

    expect((await screen.findAllByText('Context must be valid JSON')).length).toBeGreaterThan(0)
    expect(postCalls).toBe(0)
  }, 10000)

  it.each(['null', '[]'])('rejects schedule context %s because it is not an object', async (context) => {
    let postCalls = 0
    stubFetch(async (input, init) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (url === '/api/v1/schedules' && method === 'POST') postCalls += 1
      if (url === '/api/v1/research' && method === 'GET') return jsonResponse({ research: [] })
      if (url === '/api/v1/schedules' && method === 'GET') return jsonResponse({ schedules: [] })
      return jsonResponse({ error: 'not found' }, 404)
    })
    const user = userEvent.setup()
    render(<ChatPage />)

    await user.click(await screen.findByRole('button', { name: 'New schedule' }))
    await user.type(screen.getByLabelText('Objective'), 'Morning brief')
    await user.type(screen.getByLabelText('Cron'), '0 9 * * 1-5')
    await user.type(screen.getByLabelText('Research ID'), 'r-1')
    await user.click(screen.getByLabelText('Context (optional JSON)'))
    await user.paste(context)
    await user.click(screen.getByRole('button', { name: 'Register' }))

    expect((await screen.findAllByText('Context must be valid JSON')).length).toBeGreaterThan(0)
    expect(postCalls).toBe(0)
  }, 10000)
})

it('renders the conclusion, expandable step output, and source metadata', async () => {
  stubFetch(async (input) => {
    const url = String(input)
    if (url === '/api/v1/research') return jsonResponse({ research: ['r-output'] })
    if (url === '/api/v1/schedules') return jsonResponse({ schedules: [] })
    if (url.endsWith('/r-output')) return jsonResponse({ research_id: 'r-output', objective: 'Find FY2025 revenue', phase: 'completed', conclusion: { outputs: { s1: JSON.stringify({ summary: 'FY2025 revenue was $1.4B.' }) } }, error: null, steps: [{ step_id: 's1', tool: 'retrieve_context', status: 'succeeded', attempts: 1, error: null, output_preview: 'Revenue evidence', evidence: [{ page: 89, document_name: '10-K' }] }] })
    return jsonResponse({ error: 'not found' }, 404)
  })
  const user = userEvent.setup()
  render(<ChatPage />)
  await user.click(await screen.findByText('Find FY2025 revenue'))
  expect(await screen.findByText('FY2025 revenue was $1.4B.')).toBeInTheDocument()
  await user.click(screen.getByText('Step output'))
  expect(await screen.findByText('Revenue evidence')).toBeInTheDocument()
  expect(screen.getByText(/page 89/i)).toBeInTheDocument()
})

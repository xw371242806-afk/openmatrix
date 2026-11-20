import { describe, it, expect, beforeAll, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import TracePage from '../TracePage'
import i18n from '../i18n'
import p4en from '../locales/patches/p4.en.json'
import p4zh from '../locales/patches/p4.zh.json'
import { clearAccessToken } from '../api/client'

class MockEventSource {
  static instances: MockEventSource[] = []
  url: string
  readyState = 0
  listeners = new Map<string, (ev: MessageEvent) => void>()
  closed = false

  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }

  addEventListener(type: string, cb: (ev: MessageEvent) => void) {
    this.listeners.set(type, cb)
  }

  close() {
    this.closed = true
  }

  emit(type: string, data: unknown) {
    const cb = this.listeners.get(type)
    if (cb) cb({ data: JSON.stringify(data) } as MessageEvent)
  }
}

const originalEventSource = window.EventSource

describe('TracePage', () => {
  beforeAll(() => {
    // The new trace.* keys ship as a locale patch; register them so the
    // ledger renders real copy instead of raw keys.
    i18n.addResourceBundle('en', 'translation', p4en, true, true)
    i18n.addResourceBundle('zh', 'translation', p4zh, true, true)
  })

  beforeEach(() => {
    clearAccessToken()
    MockEventSource.instances = []
    ;(window as { EventSource?: unknown }).EventSource = MockEventSource
    window.location.hash = ''
  })

  afterEach(() => {
    ;(window as { EventSource?: unknown }).EventSource = originalEventSource
    window.location.hash = ''
  })

  it('renders the page and disables watch without a run id', () => {
    render(<TracePage />)
    expect(screen.getByTestId('trace-page')).toBeDefined()
    const watch = screen.getByRole('button', { name: /Watch/ }) as HTMLButtonElement
    expect(watch.disabled).toBe(true)
  })

  it('subscribes to the SSE stream and renders incoming cell events', async () => {
    render(<TracePage />)
    const input = screen.getByPlaceholderText('Enter a matrix run id')
    fireEvent.change(input, { target: { value: 'run-123' } })
    fireEvent.click(screen.getByRole('button', { name: /Watch/ }))

    await waitFor(() => expect(MockEventSource.instances.length).toBe(1))
    const source = MockEventSource.instances[0]
    expect(source.url).toContain('/api/runs/run-123/events')

    source.emit('cell', {
      cell_id: 'c1',
      question: 'What was revenue?',
      status: 'completed',
      document_index: 0,
      question_index: 1,
    })
    expect(await screen.findByText('What was revenue?')).toBeDefined()
    // row tag and live-tally chip both carry the status label
    expect(screen.getAllByText('Completed').length).toBe(2)
    // per-row metadata: document/question indexes and the cell id in mono
    expect(screen.getByText('doc 0')).toBeDefined()
    expect(screen.getByText('q 1')).toBeDefined()
    expect(screen.getByText('c1')).toBeDefined()
    // live tally counts the received cell
    expect(screen.getByText('Total')).toBeDefined()
    // the connection indicator flips to the streaming state
    expect(screen.getByText('Streaming…')).toBeDefined()

    source.emit('run_complete', { type: 'run_complete', run_id: 'run-123' })
    expect(await screen.findByText(/Run run-123 complete/)).toBeDefined()
  })

  it('ignores duplicate cell ids and malformed events', async () => {
    render(<TracePage />)
    fireEvent.change(screen.getByPlaceholderText('Enter a matrix run id'), {
      target: { value: 'run-dup' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Watch/ }))
    await waitFor(() => expect(MockEventSource.instances.length).toBe(1))
    const source = MockEventSource.instances[0]

    source.emit('cell', { cell_id: 'c1', question: 'Q1', status: 'completed' })
    source.emit('cell', { cell_id: 'c1', question: 'Q1', status: 'completed' })
    source.listeners.get('cell')!({ data: 'not-json' } as MessageEvent)

    await waitFor(() => expect(screen.getAllByText('Q1').length).toBe(1))
  })

  it('prefills and auto-connects once from a #/trace?run= hash handoff', async () => {
    window.location.hash = '/trace?run=run-abc'
    render(<TracePage />)

    await waitFor(() => expect(MockEventSource.instances.length).toBe(1))
    expect(MockEventSource.instances[0].url).toContain('/api/runs/run-abc/events')
    const input = screen.getByPlaceholderText('Enter a matrix run id') as HTMLInputElement
    expect(input.value).toBe('run-abc')

    // the handoff connects once — no extra subscriptions on re-render
    fireEvent.change(input, { target: { value: 'run-abc' } })
    expect(MockEventSource.instances.length).toBe(1)
  })

  it('parses reordered hash params and decodes the run id exactly once', async () => {
    window.location.hash = '/trace?view=live&run=run%252Fencoded&extra=1'
    render(<TracePage />)

    await waitFor(() => expect(MockEventSource.instances.length).toBe(1))
    expect(MockEventSource.instances[0].url).toBe('/api/runs/run%252Fencoded/events')
    const input = screen.getByPlaceholderText('Enter a matrix run id') as HTMLInputElement
    expect(input.value).toBe('run%2Fencoded')
  })

  it('re-subscribes when the trace query changes through hash navigation', async () => {
    window.location.hash = '/trace?run=run-first'
    render(<TracePage />)

    await waitFor(() => expect(MockEventSource.instances.length).toBe(1))
    const first = MockEventSource.instances[0]

    window.history.replaceState(null, '', '#/trace?mode=live&run=run-second')
    fireEvent(window, new HashChangeEvent('hashchange'))

    await waitFor(() => expect(MockEventSource.instances.length).toBe(2))
    expect(first.closed).toBe(true)
    expect(MockEventSource.instances[1].url).toBe('/api/runs/run-second/events')
    expect((screen.getByPlaceholderText('Enter a matrix run id') as HTMLInputElement).value).toBe('run-second')

    window.history.replaceState(null, '', '#/trace?run=run-first')
    fireEvent(window, new HashChangeEvent('hashchange'))

    await waitFor(() => expect(MockEventSource.instances.length).toBe(3))
    expect(MockEventSource.instances[1].closed).toBe(true)
    expect(MockEventSource.instances[2].url).toBe('/api/runs/run-first/events')
  })

  it('stops an active subscription from the watch/stop toggle', async () => {
    render(<TracePage />)
    fireEvent.change(screen.getByPlaceholderText('Enter a matrix run id'), {
      target: { value: 'run-stop' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Watch/ }))
    await waitFor(() => expect(MockEventSource.instances.length).toBe(1))
    const source = MockEventSource.instances[0]

    const stopBtn = screen.getByRole('button', { name: /Stop/ }) as HTMLButtonElement
    fireEvent.click(stopBtn)
    expect(source.closed).toBe(true)
    // after stopping, the toggle returns to its watch state
    expect(screen.getByRole('button', { name: /Watch/ })).toBeDefined()
  })

  it('ignores events queued by an old subscription after reconnecting to the same run', async () => {
    render(<TracePage />)
    const input = screen.getByPlaceholderText('Enter a matrix run id')
    fireEvent.change(input, { target: { value: 'run-same' } })
    fireEvent.click(screen.getByRole('button', { name: /Watch/ }))
    await waitFor(() => expect(MockEventSource.instances.length).toBe(1))
    const first = MockEventSource.instances[0]

    fireEvent.click(screen.getByRole('button', { name: /Stop/ }))
    fireEvent.click(screen.getByRole('button', { name: /Watch/ }))
    await waitFor(() => expect(MockEventSource.instances.length).toBe(2))

    first.emit('cell', { cell_id: 'stale', question: 'Stale event', status: 'completed' })
    expect(screen.queryByText('Stale event')).not.toBeInTheDocument()
    expect(MockEventSource.instances[1].closed).toBe(false)
  })
})

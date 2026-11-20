import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import ProjectsPage from '../ProjectsPage'
import i18n from '../i18n'
// The p2 patch locale is not merged into the main locale files yet; register
// it here so assertions can use the user-facing English copy.
import p2en from '../locales/patches/p2.en.json'

i18n.addResourceBundle('en', 'translation', p2en, true, true)

function mockFetch(handlers: Record<string, () => Promise<unknown>>) {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    // exact '=url' patterns match equality; otherwise longest-substring wins
    const entries = Object.entries(handlers).sort((a, b) => b[0].length - a[0].length)
    for (const [pattern, handler] of entries) {
      if (pattern.startsWith('=') ? url === pattern.slice(1) : url.includes(pattern)) {
        return new Response(JSON.stringify(await handler()), { status: 200 })
      }
    }
    return new Response(JSON.stringify({ error: 'not found' }), { status: 404 })
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

function selectProject(name: string) {
  fireEvent.click(screen.getByRole('button', { name: new RegExp(`^${name}`) }))
}

describe('ProjectsPage', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  it('switching projects clears prior search hits and documents', async () => {
    mockFetch({
      '=/api/v1/projects': async () => ({ projects: [
        { id: 'A', name: 'Project A', description: '', created_at: '' },
        { id: 'B', name: 'Project B', description: '', created_at: '' },
      ] }),
      '/projects/A/documents': async () => ({ documents: [
        { id: 'a1', filename: 'a.txt', status: 'indexed', page_count: 1, created_at: '' },
      ] }),
      '/projects/B/documents': async () => ({ documents: [] }),
      '/search': async () => ({ hits: [
        { document_id: 'a1', filename: 'a.txt', page: 1, quote: 'hit from A', citation_status: 'verified_exact' },
      ] }),
    })

    render(<ProjectsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /Project A/ })).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /Project B/ })).toBeInTheDocument()

    // open project A, wait for its documents to load
    selectProject('Project A')
    await waitFor(() => expect(screen.getByText('a.txt')).toBeInTheDocument())

    // run a search on project A -> a hit appears
    fireEvent.click(screen.getByRole('tab', { name: 'Search' }))
    fireEvent.change(screen.getByPlaceholderText('Enter question'), { target: { value: 'revenue' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(screen.getByText('hit from A')).toBeInTheDocument())

    // switch to project B
    selectProject('Project B')
    // project A's search hit and question must be cleared, and documents reset
    await waitFor(() => expect(screen.queryByText('hit from A')).toBeNull())
    expect(screen.queryByText('a.txt')).toBeNull()
    expect((screen.getByPlaceholderText('Enter question') as HTMLInputElement).value).toBe('')
  })

  it('a stale project-A document response does not overwrite project-B documents', async () => {
    let resolveA: (v: unknown) => void
    const gateA = new Promise((resolve) => { resolveA = resolve })
    mockFetch({
      '=/api/v1/projects': async () => ({ projects: [
        { id: 'A', name: 'Project A', description: '', created_at: '' },
        { id: 'B', name: 'Project B', description: '', created_at: '' },
      ] }),
      // Project A's documents request hangs until manually resolved
      '/projects/A/documents': () => gateA.then(() => ({ documents: [
        { id: 'a1', filename: 'a.txt', status: 'indexed', page_count: 1, created_at: '' },
      ] })),
      '/projects/B/documents': async () => ({ documents: [
        { id: 'b1', filename: 'b.txt', status: 'indexed', page_count: 1, created_at: '' },
      ] }),
    })

    render(<ProjectsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /Project A/ })).toBeInTheDocument())

    // open project A (its documents request is gated)
    selectProject('Project A')
    // quickly switch to project B
    await new Promise((r) => setTimeout(r, 20))
    selectProject('Project B')
    await waitFor(() => expect(screen.getByText('b.txt')).toBeInTheDocument(), { timeout: 3000 })

    // now release project A's stale response
    resolveA!(null)
    // project B's documents must remain - A's stale response is discarded
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.getByText('b.txt')).toBeInTheDocument()
    expect(screen.queryByText('a.txt')).toBeNull()
  })
})

it('uploads a file into the selected project and refreshes the document list', async () => {
  const calls=mockFetch({
    '=/api/v1/projects': async () => ({ projects: [{ id:'A',name:'Project A',created_at:'' }] }),
    '/projects/A/documents': async () => ({ documents: [] }),
    '/projects/A/upload': async () => ({ document: { id:'uploaded' } }),
  })
  render(<ProjectsPage />)
  await waitFor(() => expect(screen.getByRole('button', {name:/Project A/})).toBeInTheDocument())
  selectProject('Project A')
  const input=await screen.findByLabelText('Upload file')
  fireEvent.change(input,{target:{files:[new File(['PDF'],'annual.pdf',{type:'application/pdf'})]}})
  await waitFor(() => expect(calls).toHaveBeenCalledWith('/api/v1/projects/A/upload',expect.objectContaining({method:'POST',body:expect.any(FormData)})))
})

it('labels unpaged text as a section rather than a physical page', async () => {
  mockFetch({
    '=/api/v1/projects': async () => ({projects:[{id:'A',name:'Project A',created_at:''}]}),
    '/projects/A/documents': async () => ({documents:[]}),
    '/search': async () => ({hits:[{document_id:'d',filename:'text.txt',page:null,section:2,quote:'Revenue evidence',citation_status:'verified_exact'}]}),
  })
  render(<ProjectsPage />)
  await waitFor(() => expect(screen.getByRole('button',{name:/Project A/})).toBeInTheDocument())
  selectProject('Project A')
  fireEvent.click(screen.getByRole('tab',{name:'Search'}))
  fireEvent.change(screen.getByPlaceholderText('Enter question'),{target:{value:'revenue'}})
  fireEvent.click(screen.getByRole('button',{name:'Search'}))
  await waitFor(() => expect(screen.getByText('section 2')).toBeInTheDocument())
})

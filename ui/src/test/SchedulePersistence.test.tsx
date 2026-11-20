import {render,screen,waitFor} from '@testing-library/react'
import {it,expect,vi,afterEach} from 'vitest'
import HomePage from '../HomePage'
import en from '../locales/en.json'
afterEach(() => vi.unstubAllGlobals())
it.each([true,false])('shows the memory warning only when persistent=%s is false',async(persistent) => {
  vi.stubGlobal('fetch',vi.fn(async(input: RequestInfo|URL) => new Response(JSON.stringify(String(input)==='/api/v1/schedules' ? {schedules:[],persistent} : {total:0,total_cells:0,rate:0,runs:[],projects:[]}),{headers:{'Content-Type':'application/json'}})))
  render(<HomePage navigate={() => {}} />)
  await screen.findByText(en.overview.schedules_empty)
  await waitFor(() => {
    if(persistent) expect(screen.queryByText(en.overview.schedules_note)).not.toBeInTheDocument()
    else expect(screen.getByText(en.overview.schedules_note)).toBeInTheDocument()
  })
})

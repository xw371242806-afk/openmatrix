import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import AccessTokenControl from '../components/AccessTokenControl'
import { clearAccessToken, hasAccessToken } from '../api/client'

describe('AccessTokenControl', () => {
  afterEach(() => {
    cleanup()
    clearAccessToken()
  })

  it('stores the token in memory and can clear it', async () => {
    render(<AccessTokenControl />)

    fireEvent.click(screen.getByRole('button', { name: 'Set access token' }))
    fireEvent.change(screen.getByLabelText('OpenMatrix access token'), {
      target: { value: '  secret-token  ' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))

    expect(hasAccessToken()).toBe(true)
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)

    fireEvent.click(screen.getByRole('button', { name: 'Access configured' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(hasAccessToken()).toBe(false)
  })
})

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import i18n, { initialLanguage, persistLanguage } from '../i18n'

describe('browser language selection', () => {
  beforeEach(() => localStorage.clear())
  afterEach(() => { localStorage.clear(); vi.unstubAllGlobals() })

  it('uses English for en-US', async () => {
    vi.stubGlobal('navigator', { language: 'en-US' })
    expect(initialLanguage()).toBe('en')
    await i18n.changeLanguage('en')
    expect(i18n.t('nav.overview')).toBe('Overview')
  })

  it('uses Chinese for zh-CN', async () => {
    vi.stubGlobal('navigator', { language: 'zh-CN' })
    expect(initialLanguage()).toBe('zh')
    await i18n.changeLanguage('zh')
    expect(i18n.t('nav.overview')).toBe('概览首页')
  })

  it('gives a saved choice priority', async () => {
    localStorage.setItem('openmatrix.lang', 'en')
    vi.stubGlobal('navigator', { language: 'zh-CN' })
    expect(initialLanguage()).toBe('en')
    await i18n.changeLanguage('en')
    expect(i18n.t('nav.overview')).toBe('Overview')
  })


  it('persistLanguage writes localStorage and changes i18n', async () => {
    vi.stubGlobal('navigator', { language: 'en-US' })
    await persistLanguage('zh')
    expect(localStorage.getItem('openmatrix.lang')).toBe('zh')
    expect(i18n.t('nav.overview')).toBe('概览首页')
    await persistLanguage('en')
    expect(localStorage.getItem('openmatrix.lang')).toBe('en')
    expect(i18n.t('nav.overview')).toBe('Overview')
  })

})

it('ships research conclusion copy in the production resources', async () => {
  const en = (await import('../locales/en.json')).default
  const zh = (await import('../locales/zh.json')).default
  expect(en.agent.conclusion_title).toBe('Conclusion')
  expect(en.agent.step_output).toBe('Step output')
  expect(zh.agent.conclusion_title).toBe('结论')
})

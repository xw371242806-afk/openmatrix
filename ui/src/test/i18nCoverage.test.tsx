import { afterEach, describe, expect, it } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import i18n from '../i18n'
import en from '../locales/en.json'
import zh from '../locales/zh.json'
import ChartsPage from '../ChartsPage'
import BatchPage from '../BatchPage'
import TracePage from '../TracePage'
import SettingsPage from '../SettingsPage'

function flatten(value: unknown, prefix = ''): Record<string, string> {
  if (typeof value === 'string') return { [prefix]: value }
  if (!value || typeof value !== 'object') return {}
  return Object.entries(value as Record<string, unknown>).reduce<Record<string, string>>(
    (out, [key, child]) => Object.assign(out, flatten(child, prefix ? `${prefix}.${key}` : key)),
    {},
  )
}

const auditedPages = [
  'BatchPage.tsx', 'ProjectsPage.tsx', 'TracePage.tsx', 'ChartsPage.tsx',
  'ChatPage.tsx', 'PdfViewerPage.tsx', 'SettingsPage.tsx',
]

const sourceModules = import.meta.glob('../*.tsx', {
  eager: true,
  query: '?raw',
  import: 'default',
}) as Record<string, string>

afterEach(async () => {
  cleanup()
  await i18n.changeLanguage('en')
})

describe('i18n coverage', () => {
  it('keeps English and Chinese locale key sets identical and non-empty', () => {
    const enFlat = flatten(en)
    const zhFlat = flatten(zh)
    expect(Object.keys(zhFlat).sort()).toEqual(Object.keys(enFlat).sort())
    expect(Object.values(enFlat).every((value) => value.trim().length > 0)).toBe(true)
    expect(Object.values(zhFlat).every((value) => value.trim().length > 0)).toBe(true)
  })

  it('contains no known hard-coded user-facing English copy in audited secondary pages', () => {
    const combined = auditedPages.map((name) => sourceModules[`../${name}`] ?? '').join('\n')
    expect(combined.length).toBeGreaterThan(1000)
    const auditedNamespaces = ['batch.', 'projects.', 'trace.', 'charts.', 'chat.', 'pdf.', 'settings.']
    const forbiddenUserCopy = Object.entries(flatten(en))
      .filter(([key]) => auditedNamespaces.some((prefix) => key.startsWith(prefix)))
      .map(([, value]) => value.replace(/\{\{[^}]+\}\}/g, '').trim())
      .filter((value) => value.length >= 10)
    expect(forbiddenUserCopy.length).toBeGreaterThan(50)
    for (const phrase of forbiddenUserCopy) expect(combined).not.toContain(phrase)
  })

  it('re-renders text and accessible names across secondary pages when locale changes', async () => {
    await act(async () => { await i18n.changeLanguage('zh') })
    render(<ChartsPage />)
    expect(await screen.findAllByText('单元格总数')).not.toHaveLength(0)
    await act(async () => { await i18n.changeLanguage('en') })
    await waitFor(() => expect(screen.getAllByText('Total Cells')).not.toHaveLength(0))

    cleanup()
    await act(async () => { await i18n.changeLanguage('zh') })
    render(<BatchPage />)
    expect(screen.getByLabelText('选择 PDF 文件')).toBeInTheDocument()
    await act(async () => { await i18n.changeLanguage('en') })
    await waitFor(() => expect(screen.getByLabelText('Choose PDF files')).toBeInTheDocument())

    cleanup()
    await act(async () => { await i18n.changeLanguage('zh') })
    render(<TracePage />)
    expect(screen.getByPlaceholderText('输入矩阵运行 ID')).toBeInTheDocument()
    await act(async () => { await i18n.changeLanguage('en') })
    await waitFor(() => expect(screen.getByPlaceholderText('Enter a matrix run id')).toBeInTheDocument())

    cleanup()
    await act(async () => { await i18n.changeLanguage('zh') })
    render(<SettingsPage />)
    expect(screen.getByPlaceholderText('主密钥')).toBeInTheDocument()
    await act(async () => { await i18n.changeLanguage('en') })
    await waitFor(() => expect(screen.getByPlaceholderText('Master key')).toBeInTheDocument())
  })

  it('keeps English fallback configured and interpolates dynamic values in both locales', async () => {
    expect(JSON.stringify(i18n.options.fallbackLng)).toContain('en')
    await i18n.changeLanguage('zh')
    expect(i18n.t('batch.documents_count', { count: 2 })).toBe('2 个文档')
    expect(i18n.t('pdf.loaded_pages', { count: 3 })).toBe('已加载 3 页')
    await i18n.changeLanguage('en')
    expect(i18n.t('batch.documents_count', { count: 2 })).toBe('2 documents')
    expect(i18n.t('pdf.loaded_pages', { count: 3 })).toBe('Loaded 3 pages')
  })
})

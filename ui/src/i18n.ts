import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import en from './locales/en.json'
import zh from './locales/zh.json'

export function persistLanguage(language: 'zh' | 'en') {
  try {
    window.localStorage.setItem('openmatrix.lang', language)
  } catch {
    // Privacy mode may block storage; session language still changes.
  }
  return i18n.changeLanguage(language)
}

export function initialLanguage(): 'zh' | 'en' {
  try {
    const saved = window.localStorage.getItem('openmatrix.lang')
    if (saved === 'zh' || saved === 'en') return saved
  } catch {
    // Privacy mode may block storage; browser language is still usable.
  }
  return navigator.language.toLowerCase().startsWith('zh') ? 'zh' : 'en'
}

const providerTranslations = {
  en: {
    provider: {
      section_label: 'Model provider',
      preset_label: 'Provider preset',
      deepseek: 'DeepSeek official',
      opencode_go: 'OpenCode Go',
      custom: 'Custom provider',
      discover_models: 'Fetch models',
      discovery_required: 'Enter the provider API key and base URL first.',
      discovery_empty: 'The provider returned no model identifiers.',
      discovery_failed: 'Model discovery failed. Check the provider settings and try again.',
      memory_only: 'Provider keys stay in memory and are sent only in the model request body.',
      source: 'Provider model',
    },
    matrix: {
      use_own_provider: 'Use my own model service',
      server_provider_note: 'Using the server-configured model service. Provider credentials are not sent.',
    },
  },
  zh: {
    provider: {
      section_label: '模型提供商',
      preset_label: '提供商预设',
      deepseek: 'DeepSeek 官方',
      opencode_go: 'OpenCode Go',
      custom: '自定义提供商',
      discover_models: '获取模型',
      discovery_required: '请先填写提供商 API 密钥和 Base URL。',
      discovery_empty: '提供商没有返回模型标识符。',
      discovery_failed: '获取模型失败，请检查提供商设置后重试。',
      memory_only: '提供商密钥只保存在内存中，并仅随模型请求正文发送。',
      source: '提供商模型',
    },
    matrix: {
      use_own_provider: '使用自己的模型服务',
      server_provider_note: '当前使用服务器配置的模型服务，不会发送提供商凭据。',
    },
  },
} as const

i18n
  .use(initReactI18next)
  .init({
    resources: {
      en: { translation: { ...en, provider: providerTranslations.en.provider, matrix: { ...en.matrix, ...providerTranslations.en.matrix } } },
      zh: { translation: { ...zh, provider: providerTranslations.zh.provider, matrix: { ...zh.matrix, ...providerTranslations.zh.matrix } } },
    },
    lng: initialLanguage(),
    fallbackLng: 'en',
    interpolation: {
      escapeValue: false, 
    },
  })

export default i18n

import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { AutoComplete, Button, Input, Select, Space, Typography, message } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { useTranslation } from 'react-i18next'
import { postJson } from '../api/client'

export interface ModelProviderValue {
  apiKey: string
  baseUrl: string
  modelName: string
}

export const MODEL_PROVIDER_PRESETS = {
  deepseek: {
    value: 'deepseek',
    baseUrl: 'https://api.deepseek.com',
    modelName: 'deepseek-flash',
  },
  opencodeGo: {
    value: 'opencode-go',
    baseUrl: 'https://opencode.ai/zen/go/v1',
    modelName: 'deepseek-v4-flash',
  },
} as const

type PresetValue = keyof typeof MODEL_PROVIDER_PRESETS | 'custom'

interface ModelProviderPickerProps {
  value: ModelProviderValue
  onChange: (value: ModelProviderValue) => void
  showApiKey?: boolean
  disabled?: boolean
  apiKeyLabel?: string
  baseUrlLabel?: string
  modelNameLabel?: string
}

function identifyPreset(value: ModelProviderValue): PresetValue {
  const match = Object.entries(MODEL_PROVIDER_PRESETS).find(([, preset]) =>
    preset.baseUrl === value.baseUrl && preset.modelName === value.modelName,
  )
  return (match?.[0] as PresetValue | undefined) ?? 'custom'
}

export default function ModelProviderPicker({
  value,
  onChange,
  showApiKey = true,
  disabled = false,
  apiKeyLabel,
  baseUrlLabel,
  modelNameLabel,
}: ModelProviderPickerProps) {
  const { t } = useTranslation()
  const idPrefix = useId()
  const ids = { preset: `${idPrefix}-preset`, apiKey: `${idPrefix}-api-key`, baseUrl: `${idPrefix}-base-url`, model: `${idPrefix}-model` }
  const [customPreset, setCustomPreset] = useState(false)
  const preset = customPreset ? 'custom' : identifyPreset(value)
  const [models, setModels] = useState<string[]>([])
  const [discovering, setDiscovering] = useState(false)
  const requestId = useRef(0)
  const controller = useRef<AbortController | null>(null)

  useEffect(() => () => {
    requestId.current += 1
    controller.current?.abort()
  }, [])

  useEffect(() => {
    requestId.current += 1
    controller.current?.abort()
    controller.current = null
    setDiscovering(false)
    setModels([])
  }, [value.baseUrl, value.apiKey])

  const modelOptions = useMemo(() => {
    const values = value.modelName && !models.includes(value.modelName) ? [value.modelName, ...models] : models
    return values.map((model) => ({ value: model, label: model }))
  }, [models, value.modelName])

  const update = (partial: Partial<ModelProviderValue>) => onChange({ ...value, ...partial })

  const invalidateDiscovery = () => {
    requestId.current += 1
    controller.current?.abort()
    controller.current = null
    setDiscovering(false)
    setModels([])
  }

  const updateProvider = (partial: Partial<ModelProviderValue>) => {
    const baseChanged = partial.baseUrl !== undefined && partial.baseUrl !== value.baseUrl
    const keyChanged = partial.apiKey !== undefined && partial.apiKey !== value.apiKey
    if (baseChanged || keyChanged) invalidateDiscovery()
    const next = baseChanged ? { ...partial, apiKey: '' } : partial
    onChange({ ...value, ...next })
  }

  const selectPreset = (next: PresetValue) => {
    invalidateDiscovery()
    setCustomPreset(next === 'custom')
    if (next === 'custom') {
      if (preset !== 'custom') onChange({ ...value, apiKey: '' })
      return
    }
    const selected = MODEL_PROVIDER_PRESETS[next]
    onChange({ ...value, apiKey: '', baseUrl: selected.baseUrl, modelName: selected.modelName })
  }

  const discoverModels = async () => {
    const baseUrl = value.baseUrl.trim()
    const apiKey = value.apiKey.trim()
    if (!baseUrl || !apiKey) {
      message.error(t('provider.discovery_required'))
      return
    }
    const id = ++requestId.current
    controller.current?.abort()
    const nextController = new AbortController()
    controller.current = nextController
    setDiscovering(true)
    try {
      const response = await postJson<{ models?: unknown }>('/api/models', {
        base_url: baseUrl,
        api_key: apiKey,
      }, { signal: nextController.signal })
      if (id !== requestId.current) return
      const discovered = Array.isArray(response.models)
        ? response.models.filter((model): model is string => typeof model === 'string' && model.trim().length > 0)
        : []
      setModels(Array.from(new Set(discovered)))
      if (discovered.length === 0) message.info(t('provider.discovery_empty'))
    } catch (error) {
      if (id !== requestId.current || (error instanceof DOMException && error.name === 'AbortError')) return
      message.error(t('provider.discovery_failed'))
    } finally {
      if (id === requestId.current) setDiscovering(false)
    }
  }

  return (
    <div className="model-provider-picker" aria-label={t('provider.section_label')}>
      <Typography.Text strong>{t('provider.section_label')}</Typography.Text>
      <label className="field-note" htmlFor={ids.preset}>{t('provider.preset_label')}</label>
      <Select
        id={ids.preset}
        aria-label={t('provider.preset_label')}
        value={preset}
        disabled={disabled}
        onChange={(next) => selectPreset(next as PresetValue)}
        options={[
          { value: 'deepseek', label: t('provider.deepseek') },
          { value: 'opencodeGo', label: t('provider.opencode_go') },
          { value: 'custom', label: t('provider.custom') },
        ]}
        style={{ width: '100%', marginTop: 8 }}
      />
      {showApiKey && (
        <>
          <label className="field-note" htmlFor={ids.apiKey}>{apiKeyLabel ?? t('batch.provider_key_aria')}</label>
          <Input.Password
          id={ids.apiKey}
          aria-label={apiKeyLabel ?? t('batch.provider_key_aria')}
          autoComplete="off"
          disabled={disabled}
          value={value.apiKey}
          onChange={(event) => updateProvider({ apiKey: event.target.value })}
          style={{ marginTop: 8 }}
          />
        </>
      )}
      <label className="field-note" htmlFor={ids.baseUrl}>{baseUrlLabel ?? t('batch.base_url_aria')}</label>
      <Input
        id={ids.baseUrl}
        aria-label={baseUrlLabel ?? t('batch.base_url_aria')}
        disabled={disabled}
        value={value.baseUrl}
        onChange={(event) => { setCustomPreset(true); updateProvider({ baseUrl: event.target.value }) }}
        style={{ marginTop: 8 }}
      />
      <label className="field-note" htmlFor={ids.model}>{modelNameLabel ?? t('batch.model_name_aria')}</label>
      <Space.Compact style={{ display: 'flex', marginTop: 8 }}>
        {models.length > 0 ? (
          <AutoComplete
            id={ids.model}
            aria-label={modelNameLabel ?? t('batch.model_name_aria')}
            disabled={disabled}
            value={value.modelName}
            options={modelOptions}
            onChange={(modelName) => { setCustomPreset(true); update({ modelName }) }}
            placeholder={t('batch.model_placeholder')}
            style={{ flex: 1 }}
          />
        ) : (
          <Input
            id={ids.model}
            aria-label={modelNameLabel ?? t('batch.model_name_aria')}
            disabled={disabled}
            value={value.modelName}
            onChange={(event) => { setCustomPreset(true); update({ modelName: event.target.value }) }}
            placeholder={t('batch.model_placeholder')}
            style={{ flex: 1 }}
          />
        )}
        <Button
          aria-label={t('provider.discover_models')}
          icon={<ReloadOutlined aria-hidden="true" />}
          loading={discovering}
          disabled={disabled || !showApiKey || discovering}
          onClick={() => void discoverModels()}
        >
          {t('provider.discover_models')}
        </Button>
      </Space.Compact>
      <Typography.Text type="secondary" className="field-note">{t('provider.memory_only')}</Typography.Text>
    </div>
  )
}

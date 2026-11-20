import { describe, expect, it } from 'vitest'
import { EXPERT_PRESETS } from '../expertPresets'

describe('EXPERT_PRESETS catalog', () => {
  it('contains stable unique preset values with reusable question lists', () => {
    expect(EXPERT_PRESETS.length).toBeGreaterThanOrEqual(3)
    expect(new Set(EXPERT_PRESETS.map((preset) => preset.value)).size).toBe(EXPERT_PRESETS.length)
    for (const preset of EXPERT_PRESETS) {
      expect(preset.label.trim()).toBeTruthy()
      expect(preset.value.trim()).toBeTruthy()
      expect(preset.questions.length).toBeGreaterThan(0)
      expect(preset.questions.every((question) => question.trim().length > 0)).toBe(true)
    }
  })
})

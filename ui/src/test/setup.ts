
import i18n from '../i18n'
// Existing component tests assert English copy. Keep the test locale explicit;
// runtime language switching is covered separately by i18nCoverage.test.ts and
// the Playwright smoke suite.
void i18n.changeLanguage('en')
import '@testing-library/jest-dom/vitest'

// antd v6 responsive components require matchMedia in jsdom
if (!window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }) as unknown as MediaQueryList
}

// antd uses getComputedStyle / ResizeObserver in some components
if (!window.ResizeObserver) {
  class RO {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  window.ResizeObserver = RO as unknown as typeof ResizeObserver
}

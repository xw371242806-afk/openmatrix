import { useEffect, useState, lazy, Suspense } from 'react'
import { Button, ConfigProvider, Drawer, Segmented, Spin } from 'antd'
import {
  BarChartOutlined,
  FilePdfOutlined,
  FolderOutlined,
  MenuOutlined,
  MonitorOutlined,
  RobotOutlined,
  SettingOutlined,
  ThunderboltOutlined,
  DatabaseOutlined,
  ProfileOutlined,
} from '@ant-design/icons'
import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { persistLanguage } from './i18n'
import { researchDeskTheme } from './theme'
import { useHashRoute } from './lib/hashRouter'
import type { RouteKey } from './lib/hashRouter'
import BrandMark from './components/BrandMark'
import HealthBadge from './components/HealthBadge'
import AccessTokenControl from './components/AccessTokenControl'
import type { HealthState } from './components/HealthBadge'
import './design-tokens.css'
import './app.css'
import ProjectsPage from './ProjectsPage'
import BatchPage from './BatchPage'
import SettingsPage from './SettingsPage'
import HomePage from './HomePage'
import RunsPage from './RunsPage'

const ChartsPage = lazy(() => import('./ChartsPage'))
const ChatPage = lazy(() => import('./ChatPage'))
const PdfViewerPage = lazy(() => import('./PdfViewerPage'))
const TracePage = lazy(() => import('./TracePage'))
const MatrixPage = lazy(() => import('./MatrixPage'))

function useHealth(): HealthState {
  const [state, setState] = useState<HealthState>('checking')
  useEffect(() => {
    let cancelled = false
    const check = () =>
      fetch('/api/v1/health')
        .then((r) => {
          if (!cancelled) setState(r.ok ? 'live' : 'offline')
        })
        .catch(() => {
          if (!cancelled) setState('offline')
        })
    check()
    const timer = setInterval(check, 30000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])
  return state
}

interface NavItem {
  key: RouteKey
  icon: ReactNode
  label: string
}

export default function App() {
  const [route, navigate] = useHashRoute()
  const health = useHealth()
  const { t, i18n } = useTranslation()
  const [navOpen, setNavOpen] = useState(false)
  const [authVersion, setAuthVersion] = useState(0)
  const [authRequired, setAuthRequired] = useState(false)
  useEffect(() => {
    const onAuthRequired = () => setAuthRequired(true)
    window.addEventListener('openmatrix-auth-required', onAuthRequired)
    return () => window.removeEventListener('openmatrix-auth-required', onAuthRequired)
  }, [])

  useEffect(() => {
    setNavOpen(false)
  }, [route])

  const groups: { key: string; items: NavItem[] }[] = [
    {
      key: 'workspace',
      items: [
        { key: 'overview', icon: <ProfileOutlined />, label: t('nav.overview') },
        { key: 'projects', icon: <FolderOutlined />, label: t('nav.projects') },
        { key: 'analysis', icon: <ThunderboltOutlined />, label: t('nav.analysis') },
        { key: 'results', icon: <DatabaseOutlined />, label: t('nav.results') },
      ],
    },
    {
      key: 'intelligence',
      items: [
        { key: 'agent', icon: <RobotOutlined />, label: t('nav.agent') },
        { key: 'trace', icon: <MonitorOutlined />, label: t('nav.trace') },
        { key: 'matrix', icon: <ThunderboltOutlined />, label: 'Matrix Deep Dive' },
        { key: 'quality', icon: <BarChartOutlined />, label: t('nav.quality') },
      ],
    },
    {
      key: 'system',
      items: [
        { key: 'evidence', icon: <FilePdfOutlined />, label: t('nav.evidence') },
        { key: 'settings', icon: <SettingOutlined />, label: t('nav.settings') },
      ],
    },
  ]

  const pageTitle = groups.flatMap((g) => g.items).find((i) => i.key === route)?.label ?? t('nav.overview')

  const renderPage = () => {
    switch (route) {
      case 'overview': return <HomePage navigate={navigate} />
      case 'results': return <RunsPage />
      case 'analysis': return <BatchPage />
      case 'projects': return <ProjectsPage />
      case 'trace': return <TracePage />
      case 'quality': return <ChartsPage />
      case 'agent': return <ChatPage />
      case 'evidence': return <PdfViewerPage />
      case 'settings': return <SettingsPage />
      case 'matrix': return <MatrixPage />
      default: return <HomePage navigate={navigate} />
    }
  }

  const languageValue = i18n.language?.startsWith('zh') ? 'zh' : 'en'

  const changeLanguage = (value: string) => {
    const language = value === 'zh' ? 'zh' : 'en'
    void persistLanguage(language)
  }

  const navContent = (
    <>
      {groups.map(({ key, items }) => (
        <div key={key} className="nav-group">
          <div className="nav-group-label">{t(`nav.group_${key}`)}</div>
          {items.map(({ key: itemKey, icon, label }) => (
            <button
              key={itemKey}
              className={`nav-item${route === itemKey ? ' active' : ''}`}
              onClick={() => navigate(itemKey)}
              aria-current={route === itemKey ? 'page' : undefined}
            >
              <span className="nav-icon">{icon}</span>
              <span className="nav-label">{label}</span>
            </button>
          ))}
        </div>
      ))}
    </>
  )

  return (
    <ConfigProvider theme={researchDeskTheme}>
      <div className="app-shell">
        <aside className="rail">
          <div className="rail-brand">
            <BrandMark size={30} />
            <div className="rail-brand-text">
              <div className="brand-name">OpenMatrix</div>
              <div className="brand-sub">{t('shell.brand_sub')}</div>
            </div>
          </div>
          <nav className="rail-nav" aria-label={t('shell.nav_aria')}>
            {navContent}
          </nav>
          <div className="rail-footer">
            <span className="rail-footer-note">{t('shell.self_hosted_note')}</span>
          </div>
        </aside>

        <div className="main-area">
          <header className="topbar">
            <div className="topbar-left">
              <Button
                className="topbar-menu-btn"
                type="text"
                icon={<MenuOutlined aria-hidden="true" />}
                aria-label={t('shell.open_menu')}
                onClick={() => setNavOpen(true)}
              />
              <h1 className="topbar-title">{pageTitle}</h1>
            </div>
            <div className="topbar-right">
              <Segmented
                size="small"
                value={languageValue}
                onChange={(value) => changeLanguage(String(value))}
                options={[
                  { value: 'zh', label: '中文' },
                  { value: 'en', label: 'English' },
                ]}
                aria-label={t('common.language')}
              />
              <AccessTokenControl onChange={() => { setAuthRequired(false); setAuthVersion((value) => value + 1) }} />
              <HealthBadge key={authVersion} state={health} />
            </div>
          </header>

          <main className="page-content">
            {authRequired && <div role="alert">{t('shell.auth_required')}</div>}
            <Suspense fallback={<div style={{ padding: 64, display: 'flex', justifyContent: 'center' }}><Spin size="large" /></div>}>
              <div key={`${route}-${authVersion}`}>{renderPage()}</div>
            </Suspense>
          </main>
        </div>

        <Drawer
          placement="left"
          open={navOpen}
          onClose={() => setNavOpen(false)}
          title={
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <BrandMark size={24} />
              <span className="brand-name" style={{ fontSize: 15 }}>OpenMatrix</span>
            </div>
          }
          styles={{ body: { padding: '10px 8px' } }}
          size={264}
        >
          {navContent}
        </Drawer>
      </div>
    </ConfigProvider>
  )
}



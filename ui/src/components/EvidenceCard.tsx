import type { ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { FileTextOutlined } from '@ant-design/icons'
import { CitationStatusTag } from './StatusTag'

interface EvidenceCardProps {
  filename: string
  page?: number | string | null
  section?: number | null
  quote?: string | null
  status?: string | null
  footer?: ReactNode
}

/**
 * A single piece of citable evidence: the source quote, where it came
 * from (file + page) and its verification status.
 */
export default function EvidenceCard({ filename, page, section, quote, status, footer }: EvidenceCardProps) {
  const { t } = useTranslation()
  return (
    <article className="evidence-card">
      <blockquote className="evidence-quote">{quote || t('common.no_quote')}</blockquote>
      <div className="evidence-meta">
        <FileTextOutlined className="evidence-meta-icon" aria-hidden="true" />
        <span className="evidence-file" title={filename}>{filename}</span>
        {page != null ? (
          <span className="evidence-page">{t('common.page_short', { page })}</span>
        ) : section != null ? (
          <span className="evidence-page">{t('projects.section', { section })}</span>
        ) : null}
        <CitationStatusTag value={status} />
      </div>
      {footer}
    </article>
  )
}

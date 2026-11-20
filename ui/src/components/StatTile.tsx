import type { ReactNode } from 'react'
import { Spin } from 'antd'

interface StatTileProps {
  label: string
  value?: ReactNode
  hint?: string
  loading?: boolean
}

export default function StatTile({ label, value, hint, loading }: StatTileProps) {
  return (
    <div className="stat-tile">
      <div className="stat-tile-label">{label}</div>
      {loading ? (
        <Spin size="small" aria-label={label} />
      ) : (
        <div className="stat-tile-value">{value}</div>
      )}
      {hint && <div className="stat-tile-hint">{hint}</div>}
    </div>
  )
}

/**
 * OpenMatrix brand mark — an original "document stack" glyph: text
 * lines on an ink tile with a copper annotation bar. Not derived from
 * any third-party brand.
 */
export default function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 28 28" aria-hidden="true" focusable="false">
      <rect x="0.5" y="0.5" width="27" height="27" rx="6" fill="#23262C" />
      <rect x="7" y="7" width="14" height="2.6" rx="1.3" fill="#F6F4EF" />
      <rect x="7" y="12" width="10" height="2.6" rx="1.3" fill="#F6F4EF" opacity="0.72" />
      <rect x="7" y="17" width="6" height="2.6" rx="1.3" fill="#F6F4EF" opacity="0.48" />
      <rect x="15" y="17" width="6" height="2.6" rx="1.3" fill="#A05C2C" />
    </svg>
  )
}

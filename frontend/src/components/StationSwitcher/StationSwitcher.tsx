import type { StationInfo } from '../../types'

interface Props {
  stations: StationInfo[]
  active: string
  onChange: (id: string) => void
}

export function StationSwitcher({ stations, active, onChange }: Props) {
  if (stations.length <= 1) return null

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 6,
      padding: '4px 6px',
      background: 'var(--surface)',
      border: '1px solid var(--line)',
      borderRadius: 'var(--r-md)',
      flexShrink: 0,
    }}>
      <span style={{
        fontSize: 'var(--fs-xs)', color: 'var(--text-3)',
        fontFamily: 'Inter, monospace',
        letterSpacing: '0.06em', paddingRight: 4,
      }}>
        КС:
      </span>
      {stations.map(s => (
        <button
          key={s.id}
          onClick={() => onChange(s.id)}
          title={s.display_name}
          style={{
            padding: '3px 12px',
            borderRadius: 'var(--r-sm)',
            border: '1px solid',
            borderColor: s.id === active ? 'var(--accent)' : 'var(--line)',
            background: s.id === active ? 'var(--accent-glow)' : 'transparent',
            color: s.id === active ? 'var(--accent)' : 'var(--text-2)',
            fontFamily: 'Inter, monospace',
            fontSize: 'var(--fs-xs)',
            fontWeight: s.id === active ? 600 : 400,
            cursor: 'pointer',
            transition: 'background-color var(--dur-fast) var(--ease-standard), color var(--dur-fast) var(--ease-standard), border-color var(--dur-fast) var(--ease-standard)',
            letterSpacing: '0.04em',
          }}
          onMouseEnter={e => {
            if (s.id !== active) {
              (e.currentTarget as HTMLElement).style.borderColor = 'var(--accent)'
              ;(e.currentTarget as HTMLElement).style.color = 'var(--accent)'
            }
          }}
          onMouseLeave={e => {
            if (s.id !== active) {
              (e.currentTarget as HTMLElement).style.borderColor = 'var(--line)'
              ;(e.currentTarget as HTMLElement).style.color = 'var(--text-2)'
            }
          }}
        >
          {s.id}
          {s.live_data && (
            <span style={{
              display: 'inline-block', width: 5, height: 5,
              borderRadius: '50%', background: 'var(--ok)',
              marginLeft: 5, verticalAlign: 'middle',
            }} />
          )}
        </button>
      ))}
    </div>
  )
}

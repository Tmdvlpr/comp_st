import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        bg:        'var(--bg)',
        surface:   'var(--surface)',
        surface2:  'var(--surface-2)',
        surface3:  'var(--surface-3)',
        line:      'var(--line)',
        line2:     'var(--line-2)',
        accent:    'var(--accent)',
        crit:      'var(--crit)',
        warn:      'var(--warn)',
        info:      'var(--info)',
        ok:        'var(--ok)',
        mauve:     'var(--mauve)',
        teal:      'var(--teal)',
        t1:        'var(--text-1)',
        t2:        'var(--text-2)',
        t3:        'var(--text-3)',
      },
      borderRadius: {
        sm: 'var(--r-sm)',
        md: 'var(--r-md)',
        lg: 'var(--r-lg)',
      },
      fontFamily: {
        ui:   ['Geist', 'Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'monospace'],
      },
    },
  },
  plugins: [],
} satisfies Config

# CS Monitor AI — Design System

Visual design system for **CS Monitor AI**, a 24/7 real-time anomaly-monitoring operator
dashboard for a gas compressor station (Russian: «компрессорная станция», КС), station
«Охангаронская КС» with three gas-pumping units ГПА-1, ГПА-2, ГПА-3. Audience: control-room
operators. Tone: industrial control-room, immersive dark glassmorphism, precise, calm, dense.
All UI copy is in **Russian**.

## Brand & Aesthetic
- Immersive dark "mission control" look with subtle gradient background and frosted-glass panels.
- Information-dense but scannable; severity color is the primary scan signal.
- Not playful/consumer. Modern, professional, high-contrast, quiet motion.

## Color — Dark theme (primary)
| Token | Value | Use |
|-------|-------|-----|
| bg | `#0d1426` | deep base for small elements/buttons |
| background gradient | purple radial `rgba(96,66,168,0.34)` top-left + blue radial `rgba(40,86,168,0.22)` top-right + diagonal `#1a1533 → #141b30 → #0c1222` | fixed app background |
| surface | `rgba(26,31,56,0.78)` + 12px blur | glass card |
| surface-2 | `rgba(38,44,74,0.82)` | deeper glass |
| surface-3 | `rgba(52,58,94,0.88)` | highest glass |
| line | `rgba(132,140,200,0.16)` | hairline border |
| line-2 | `rgba(132,140,200,0.30)` | emphasized border |
| text | `#E6ECF6` | primary text |
| text-1 | `#FFFFFF` | key numbers |
| text-2 | `#B7C0D6` | secondary |
| text-3 | `#9BA5BC` | tertiary (still AA on bg) |
| accent | `#58A6FF` | interactive/brand |
| accent-2 | `#7CC0FF` | accent hover |
| accent-strong | `#2563c4` | filled buttons (white text) |

## Severity palette (the critical scan colors)
| Severity | Color | Ink (text on tint) | RU label |
|----------|-------|--------------------|----------|
| crit | `#FF5C6C` | `#FF8893` | КРИТ |
| warn | `#F5B14C` | `#F7C06B` | ВНИМ |
| info | `#8C93B0` | `#C5CBDF` | ИНФО |
| ok | `#3FB950` | `#6EE67D` | НОРМА |

- **Severity badge**: uppercase monospace, background = severity color at 20% opacity, text = ink variant, wide letter-spacing, 3px radius.
- **Severity dot**: 6px circle in severity color; crit dot has a soft glow and may pulse.
- Never encode severity by color alone — always pair with label/icon (colorblind-safe).

## Light theme (secondary)
bg `#e9ecf6`; white glass surfaces `rgba(255,255,255,0.72/0.86/0.92)`; text `#1b2138` / `#0a0e1c`;
accent `#2f7ff0`; severity crit `#d8394a` / warn `#c2780f` / info `#6b7494` / ok `#1f9d54`
(ink variants darker: crit `#b3142a`, warn `#8a5400`, ok `#136b39`, info `#474f6b`).

## Typography
- UI font: **Inter** (system-ui fallback).
- Numeric / technical / tags: **JetBrains Mono** (monospace). SCADA tags & sensor ids are ALWAYS mono
  (e.g. `GPA-1.GPA-1.PD.PV`, `gas_pressure_out_gpa__GPA1`).
- Scale: 12 xs (mono labels), 14 base body, 16 md, 20 lg (section titles), 30 xl (page/KPI), 56 2xl (hero/kiosk).
- Line-height: 1.25 tight (headings/numbers), 1.5 base, 1.75 relaxed.
- Letter-spacing utilities: 0.04em wide, 0.10em wider (for uppercase badges/labels).
- Max line length ~60–80 chars (measure 34rem / 50rem) for prose.

## Shape, spacing, elevation
- Radii: 8px small, 12px medium, 18px large (cards).
- Spacing scale (multiples of 4): 4, 8, 12, 16, 24, 32. Keep a consistent rhythm.
- Shadows: md `0 8px 30px rgba(0,0,0,0.35)`, lg `0 18px 54px rgba(0,0,0,0.5)`.
- Scrollbars: thin (4px), track transparent, thumb `line-2`.

## Core components
- **Card**: 18px radius, glass `surface`, hairline `line` border, `shadow-md`, 12px backdrop blur.
- **Chip**: pill (12px radius), mono 11px, `surface-2`, `text-2`; hover → accent border; active → solid `accent-strong` bg + white bold text.
- **Badge (severity)**: see severity palette.
- **Button (primary)**: `accent-strong` bg, white text, 8–12px radius.
- **Input/field**: glass surface, hairline border, accent focus ring.
- **Focus ring**: 2px solid `accent`, 2px offset (WCAG 2.4.7).

## Motion (quiet, purposeful — always-on screen)
- Duration tokens: instant 50ms, fast 100ms, normal 200ms, moderate 300ms, slow 400ms.
- Easing: standard `cubic-bezier(0.2,0,0,1)`, decelerate (enter), accelerate (exit), spring `cubic-bezier(0.34,1.56,0.64,1)` (pop/badge).
- Patterns: crit items soft pulse; slow top news-ticker scroll; diagonal sweep shimmer for loading skeletons; fade-up / scale-in entrances (staggered).
- Must fully respect `prefers-reduced-motion` (durations collapse, infinite loops stop).

## Accessibility
- WCAG AA text contrast (≥4.5:1) in both themes.
- Severity conveyed by color + text/icon.
- Visible keyboard focus, full keyboard nav, aria labels on controls, reduced-motion honored.

## Content / language
- All visible copy in Russian. Views: «Мониторинг», «Схема», «Двигатель». Severity: КРИТ/ВНИМ/ИНФО/НОРМА.
- Time shown human-friendly («12:35», «обновлено 2 мин назад»); technical timestamps monospace.
